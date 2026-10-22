"""Adapter for sites running on RentCafe (a property-management vendor's leasing platform).

Vendor-level adapter — works on any community whose marketing site is a
RentCafe template. Detection: the page (or its `/availableunits` subpath)
serves HTML with `td-card-rent` cells and links to `*.rentcafe.com`.

Coverage in the Atlanta seed (2026-05-17 probe):
  - Plus an unknown number of long-tail RentCafe sites that went through LLM
    on the first orchestrator run because we hit the homepage, not /availableunits.

Two-hop flow:
  1. Try `<community_url>/availableunits` directly (RentCafe convention).
  2. If that 404s, fall back to the homepage and look for an availability link.

Parse pattern:
  - `<h2>FloorPlanName</h2>` followed by "N Bedrooms | M Bathrooms" text
  - then a <table> with rows of td-card-* cells:
      td-card-name      = unit number ("")
      td-card-sqft      = "Sq. Ft.: 1,805"
      td-card-rent      = "$2,199.00"
      td-card-deposit   = "$2,250.00"
      td-card-available = "Available" or "6/12/2026"

Concession: extracted from page-level text (same regex as ARK/SightMap).
Rent type: "base" — RentCafe `td-card-rent` is the base monthly rent, EXCEPT on the
inclusive-charge template ("Total Monthly Leasing Price"), where it is base + mandatory fees
and the base comes from the row's `data-fp_base_charge`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from . import cues
from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession
from rentdb.keys import advertised_plan_key   # one definition of the join key (rentdb/keys.py)

NAME = "rentcafe"

SF_BAND = 50

# Concession patterns (same as ARK)
CONCESSION_WEEKS_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)
CONCESSION_MONTHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)

# Heuristic: page is RentCafe-backed if any of these appear in the HTML
RENTCAFE_SIGNALS = (
    "rentcafe.com/",
    "securecafe.com/",
    "cdngeneralmvc.rentcafe.com",
    "resource.rentcafe.com",
    'class="td-card-rent"',
    'data-selenium-id="Rent',
)

# Floor plan heading patterns
BEDS_RE = re.compile(r"(\d+)\s*[Bb]edroom", re.I)
BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[Bb]athroom", re.I)
# The short form. BATHS_RE needs the literal "Bathroom", so the "1 Bed | 1 Bath" and
# "2 BA" templates carried a bath count this adapter could not see (2026-08-13).
_PLAN_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[Bb]ath\b|BA\b)", re.I)
# The plan's own heading: "Studio | 1 Bathroom", "2 Bedrooms | 2.5 Bathrooms". A plan
# section can ALSO carry marketing prose naming other bed counts, and BEDS_RE takes the
# first "N Bedroom" anywhere and has no Studio form: TPA Grady Square's studio card read
# 3bd off "Urban Flat, 1, 2 & 3 Bedrooms + 2 & 3 Bedroom Townhomes" (2026-09-24).
_PLAN_HEADING_BEDS_RE = re.compile(
    r"\b(?:(Studio)|(\d+)\s*Bedrooms?)\s*\|\s*\d+(?:\.\d+)?\s*Bathrooms?\b", re.I)


def _heading_beds(text: str) -> int | None:
    """Bed count from a "<Studio | N Bedroom(s)> | <n> Bathroom(s)" plan heading, else None."""
    m = _PLAN_HEADING_BEDS_RE.search(text or "")
    if not m:
        return None
    return 0 if m.group(1) else int(m.group(2))


def _baths_from_text(text: str) -> float | None:
    """Bath count from a plan heading, long form first then the short one."""
    for rx in (BATHS_RE, _PLAN_BATHS_RE):
        m = rx.search(text or "")
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None
# Two shapes had to be added here (2026-08-10, the beds_null_groups dig):
#   - 3-digit footages. The old `\d{1,2}(?:,?\d{3})` needed 4+ digits, so every
#     sub-1,000 sf plan failed — "Approximately 690.00 square feet" matched nothing.
#   - a decimal tail. RentCafe writes "1,092.00 square feet", and the trailing
#     ".00" sat between the number and the unit, breaking even 4-digit values.
# Cost of missing it: beds were only accepted alongside a SF match, so the whole
# community fell into the beds=None catch-all (Avilla Holloway, 20 of 20 units).
SF_RE = re.compile(r"(\d{1,2},\d{3}|\d{3,5})(?:\.\d+)?\s*(?:sq\.?\s*ft|square)", re.I)


def matches(community: dict) -> bool:
    """True if the community's homepage shows RentCafe signals.

    Network call (same pattern as SightMap adapter)."""
    url = (community.get("url") or "").strip()
    if not url:
        return False
    from rentdb.fetchers import fetch_static
    try:
        fetch = fetch_static(url)
    except Exception:
        return False
    if fetch.status >= 400:
        return False
    return any(sig in fetch.html for sig in RENTCAFE_SIGNALS)


def extract(community: dict) -> ExtractResult:
    """Fetch known inventory paths, parse the first usable RentCafe layout, and
    merge in ADVERTISED-but-unpriced floor plans as zero-availability groups.

    Zero-avail enumeration (G0.3, convention from the Casata golden): the
    securecafe feed only carries PRICED plans, but the marketing /floorplans
    grid advertises every bed type — Avilla showed 1 of 3 advertised plans.
    units[] must include every advertised bed type, available=0 when unlisted.
    """
    from rentdb.fetchers import fetch_static

    base_url = (community.get("url") or "").strip().rstrip("/")
    if not base_url:
        return ExtractResult(scrape_error="no community URL", extractor_used=NAME, flag_review=True)

    # Fetch every candidate once (static, ~100ms each). We need more than the
    # first priced page: advertised plans usually live on a DIFFERENT path
    # (/floorplans marketing grid) than the priced feed (/availableunits).
    pages: list[tuple[str, object]] = []
    for candidate in (
        f"{base_url}/availableunits",
        f"{base_url}/floor-plans",
        f"{base_url}/floorplans",
        base_url,
    ):
        try:
            page = fetch_static(candidate)
        except Exception:
            continue
        if page.status >= 400 and (page.status != 500 or len(page.html) < 10_000):
            continue
        pages.append((candidate, page))

    if not pages:
        return ExtractResult(
            scrape_error="rentcafe: all inventory paths failed",
            extractor_used=NAME,
            flag_review=True,
        )

    # The site's own complete unit list, read once across every page fetched: it lives on
    # /floorplans while the unit CARDS live on /availableunits (AUS Rivers Edge, 2026-09-24).
    listed = _embedded_unit_list(pages)
    # The other template's list, `ysi.unitsList` (DFW Park Hill, ATL Satellite Square).
    ysi = _ysi_unit_list(pages)

    parse_warnings: list[str] = []
    best: tuple[list[UnitGroup], object] | None = None   # (priced groups, page)
    advertised: list[UnitGroup] = []
    # Keyed by id() of the advertised UnitGroup, not by (beds, sf): two REAL plans can
    # share that key and must not share one cap delta (a past bug). `advertised` and
    # `capped_deltas` are only ever replaced together, so every key in the surviving dict
    # names a group the surviving list still holds. `plan_pages` travels with them too.
    capped_deltas: dict[int, int] = {}
    plan_pages: dict[int, str] = {}
    # The grid is read off EVERY page before any priced parse, because the priced parse
    # now needs it: `_prove_plan_headers` checks the cards on /availableunits against the
    # plans the grid publishes, and /floorplans is fetched after /availableunits.
    for candidate, page in pages:
        deltas: dict[int, int] = {}
        hrefs: dict[int, str] = {}
        plans = _parse_advertised_plans(BeautifulSoup(page.html, "html.parser"), deltas, hrefs)
        if len(plans) > len(advertised):
            advertised = plans
            capped_deltas = deltas
            plan_pages = {k: urljoin(candidate + "/", h) for k, h in hrefs.items()}

    misattributed: list[str] = []
    page_html: dict[str, str | None] = {}

    def plan_html(plan: UnitGroup) -> str | None:
        """The plan's OWN page, fetched at most once per URL; None when there is none."""
        url = plan_pages.get(id(plan))
        if not url:
            return None
        if url not in page_html:
            try:
                got = fetch_static(url)
                page_html[url] = got.html if got.status < 400 else None
            except Exception:
                page_html[url] = None
        return page_html[url]

    def plan_codes(plan: UnitGroup) -> set[str] | None:
        """Unit codes the plan's OWN page lists; None when there is no page to ask."""
        html = plan_html(plan)
        return _page_unit_codes(html) if html else None

    def prove(raw: list[_RawUnit]) -> list[_RawUnit]:
        return _prove_plan_headers(raw, advertised, plan_codes, misattributed)

    # The advertised plans priced off their own pages, and the groups the unit cards alone
    # would have made. Both stay empty unless a plan page was actually used, and then
    # `_merge_advertised` runs exactly as it did before that step existed.
    covered: set[int] = set()
    card_groups: list[list[UnitGroup]] = []

    def collect(raw: list[_RawUnit], contested: set[str]) -> list[_RawUnit]:
        covered.clear()
        card_groups.clear()
        added = _units_from_plan_pages(raw, contested, advertised, plan_html, covered, ysi)
        # `ysi.unitsList` fills what neither the cards nor the plan pages showed.
        # Its units are named by their records, not inferred, so they belong in the band
        # basis beside the cards: left out, the grid re-emitted their plans with their own
        # availability on top (Roswell Village's Elliot, 9 listed + the grid's 1).
        late = _units_the_cards_missed(raw + added, ysi)
        if covered:
            # Plan-page units a record proves are record-named too, so they stand
            # in the basis beside `late`; left out, DFW Avion's (1, 822) grid variant stopped
            # being absorbed by the 746-821 band those same units had formed as records.
            card_groups.append(_group_by_beds_and_sf(
                list(raw) + late + [u for u in added if u.stated]))
        return added + late

    # id() of every priced group whose members' plan an embedded record STATES, for
    # `_refuse_misattributed_rents`. Rebuilt per parse, so it describes `best` alone.
    stated_groups: set[int] = set()
    for _candidate, page in pages:
        if best is None:
            stated_groups.clear()
            groups, warnings = _parse(page.html, listed, prove, collect,
                                      ysi=ysi, stated=stated_groups)
            if groups or not warnings:
                best = (groups, page)
            else:
                parse_warnings = warnings

    if best is None or not any(g.rent_min for g in best[0]):
        # Nothing PRICED on the marketing domain — no groups, or groups with availability
        # and no rent. A SecureCafe
        # SecureCafe leasing portal it links may carry the prices (Liberty Square,
        # Sweetwater Springs — 2026-09-25). Costs no fetch on a site that already priced;
        # the portal replaces the marketing groups only when it has a price.
        portal = _securecafe_portal(pages)
        if portal is not None and (best is None or any(g.rent_min for g in portal[0])):
            best = portal
            stated_groups.clear()

    if best is not None:
        groups, page = best
    elif advertised:
        # No priced layout anywhere, but the site advertises plans — return
        # them as zero-availability groups rather than failing the community.
        groups, page = [], pages[0][1]
    else:
        return ExtractResult(
            scrape_error="; ".join(parse_warnings) or "rentcafe: no supported inventory layout",
            extractor_used=NAME,
            flag_review=True,
        )

    # Cross-check the PRICED groups against the advertised floors before merging: a
    # group whose price belongs to another bed type has its rent dropped. Runs here, on
    # `groups` only, because the advertised entries are the reference and cannot
    # contradict themselves. (The Hadley, SRQ golden 2026-08-24.)
    condemned_keys: set[tuple[int, int | None]] = set()
    groups = _refuse_misattributed_rents(groups, advertised, misattributed, condemned_keys,
                                         exempt=stated_groups)

    # Availability as it stood before the grid was consulted — the priced count for a
    # priced group, 0 for an advertised-only one — so the inflation guard can put the
    # pre-2026-08-24 numbers back rather than guess a smaller wrong number.
    pre_grid = {id(g): g.available for g in groups}

    # A condemned group counted somebody else's units; re-read its count off its own plan
    # card. Untouched groups keep the priced count, which on healthy sites already equals
    # the grid sum for its SF band.
    groups = _recount_condemned_from_grid(groups, advertised, condemned_keys)

    # The priced unit list caps at `data-max` too — ten rows of a real fifteen. Add back
    # only what the site's own disagreeing counts prove was hidden (a past bug).
    groups = _restore_capped_priced_counts(groups, advertised, capped_deltas)

    merged = _merge_advertised(groups, advertised, covered,
                               card_groups[0] if card_groups else None)
    merged = _refuse_impossible_availability(
        merged, [pre_grid.get(id(g), 0) for g in merged],
        _safe_public_units(community), misattributed)

    page_text = BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True)
    concession_weeks, raw_concession_text = _extract_concession(page_text)
    return ExtractResult(
        units=merged,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession_text,
        flag_review=any(group.flag_review for group in merged),
        scrape_error=("rentcafe: plan attribution untrusted, rent dropped — "
                      + "; ".join(misattributed)) if misattributed else None,
        extractor_used=NAME,
    )


# --- the SecureCafe leasing portal ("onlineleasing") ------------------------
# Some RentCafe-family marketing sites print no price anywhere and link out to
# `<x>.securecafe.com/onlineleasing/<slug>/`. Two portal shapes carry prices:
#   * `availableunits.aspx?myOlePropertyId=<id>` — one `table.availableUnits` per plan,
#     captioned "Floor Plan: 1A - 1 Bedroom, 1.50 Bathrooms", one `tr.AvailUnitRow`
#     per unit: " | 928 | $1,661-$2,743". The range is that unit's price across
#     LEASE TERMS; its low end is the unit's rent (the end the market-rent rule reads,
#     same convention as razz). The high end is never a rent.
#   * `floorplans` — `var pageData = { floorplans: [ {beds, baths, sqft, lowPrice,
#     highPrice, availableCount, ...} ] }`, used when availableunits bounces to the
#     applicant login. Plan-level, so no per-unit list.
_OLE_BASE_RE = re.compile(r"https?://[a-z0-9-]+\.securecafe\.com/onlineleasing/[a-z0-9_-]+/", re.I)
_OLE_PROP_RE = re.compile(r"myOlePropertyId=(\d+)", re.I)
_OLE_BEDS_RE = re.compile(r"(\d+)\s*Bedrooms?\b", re.I)
_OLE_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bathrooms?\b", re.I)
_OLE_RENT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_PAGE_DATA_RE = re.compile(r"\bvar\s+pageData\s*=\s*\{")
_JS_KEY_RE = re.compile(r"^(\s*)([A-Za-z_]\w*)\s*:", re.M)


def _securecafe_portal(pages) -> tuple[list[UnitGroup], object] | None:
    """(groups, page) off the SecureCafe portal a fetched page links, or None."""
    from rentdb.fetchers import fetch_static

    base = next((m.group(0) for _c, pg in pages
                 for m in [_OLE_BASE_RE.search(pg.html or "")] if m), None)
    if base is None:
        return None
    try:
        fp = fetch_static(base + "floorplans")
    except Exception:  # noqa: BLE001 — an unreachable portal is just "no portal"
        return None
    if fp.status >= 400:
        return None
    prop = _OLE_PROP_RE.search(fp.html)
    if prop:
        try:
            au = fetch_static(f"{base}availableunits.aspx?myOlePropertyId={prop.group(1)}")
        except Exception:  # noqa: BLE001
            au = None
        if au is not None and au.status < 400:
            raw = _ole_unit_rows(au.html)
            if raw:
                return _group_by_beds_and_sf(raw), au
    plans = _page_data_plans(fp.html)
    return (plans, fp) if plans else None


def _ole_unit_rows(html: str) -> list["_RawUnit"]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[_RawUnit] = []
    for table in soup.select("table.availableUnits"):
        cap_el = table.find("caption")
        cap = cap_el.get_text(" ", strip=True) if cap_el is not None else ""
        mb = _OLE_BEDS_RE.search(cap)
        beds = int(mb.group(1)) if mb else (0 if re.search(r"\bstudio\b", cap, re.I) else None)
        mba = _OLE_BATHS_RE.search(cap)
        baths = float(mba.group(1)) if mba else None
        for row in table.select("tr.AvailUnitRow"):
            sf_el = row.select_one("td[data-label^='Sq']")
            rent_el = row.select_one("td[data-label='Rent']")
            name_el = row.find("th")
            sf = _parse_int_label(sf_el.get_text(" ", strip=True)) if sf_el is not None else None
            m = _OLE_RENT_RE.search(rent_el.get_text(" ", strip=True)) if rent_el is not None else None
            rent = int(round(float(m.group(1).replace(",", "")))) if m else None
            out.append(_RawUnit(beds=beds, baths=baths, sf=sf, rent=rent, status="available",
                                unit_number=name_el.get_text(strip=True) if name_el is not None else None,
                                cue_text=cap))
    return out


def _page_data_plans(html: str) -> list[UnitGroup]:
    m = _PAGE_DATA_RE.search(html or "")
    if not m:
        return []
    end = html.find("\n};", m.end())
    if end < 0:
        return []
    body = _JS_KEY_RE.sub(r'\1"\2":', html[m.end() - 1:end + 2])
    try:
        plans = json.loads(body).get("floorplans") or []
    except (ValueError, AttributeError):
        return []

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for fp in plans:
        beds = _num(fp.get("beds"))
        sqft = _num(str(fp.get("sqft") or "").replace(",", ""))
        grouped[(int(beds) if beds is not None else None,
                 int(sqft) if sqft else None)].append(fp)

    groups: list[UnitGroup] = []
    for (beds, sqft), members in grouped.items():
        available = sum(int(_num(f.get("availableCount")) or 0) for f in members)
        rents: list[int] = []
        for f in members:
            if not (_num(f.get("availableCount")) or 0):
                continue    # an unleasable plan's "from" price is not a rent
            lo, hi = _num(f.get("lowPrice")) or 0, _num(f.get("highPrice")) or 0
            rents += [int(round(v)) for v in (lo, hi) if v > 0]
        groups.append(UnitGroup(
            beds=beds, sf_min=sqft, sf_max=sqft,
            available=available, coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            unit_rents=None,
            baths=observed_baths(_num(f.get("baths")) for f in members),
            flag_review=not rents and not available,
        ))
    groups.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return groups


@dataclass
class _RawUnit:
    beds: int | None
    baths: float | None
    sf: int | None
    rent: int | None
    status: str  # "available" | "coming_soon" | "unknown"
    unit_number: str | None
    rent_allin: int | None = None
    # Descriptive page text this unit sits in (plan section + card). Fed to
    # cues.py for structure_type/stories/garage — observed-only, no new fetch.
    cue_text: str = ""
    # True when the unit's beds/SF come from an embedded record that names its plan
    # (`var available_units`, `ysi.unitsList`), not from the DOM heading above a card
    #. A group made only of such units is exempt from the price-coincidence guard.
    stated: bool = False
    # The `ysi.unitsList` record's FloorplanId. Set only on records `_ysi_unit_list` keeps,
    # i.e. on a plan whose published AvailableCount equals its record count.
    plan_ref: str | None = None


def _parse(html: str, listed: list[_RawUnit] = (),
           prove: Callable[[list[_RawUnit]], list[_RawUnit]] | None = None,
           collect: Callable[[list[_RawUnit], set[str]], list[_RawUnit]] | None = None,
           *, ysi: list[_RawUnit] = (), stated: set[int] | None = None,
           ) -> tuple[list[UnitGroup], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    warnings: list[str] = []
    raw: list[_RawUnit] = []

    # Find all rent cells, then for each, walk up to the floor plan section
    # to determine beds/baths.
    rent_tds = soup.find_all("td", class_="td-card-rent")
    if not rent_tds:
        return _parse_card_layout(soup, listed, prove, collect, ysi=ysi, stated=stated)

    # Build floor-plan map: walk up from each rent cell to find the enclosing
    # section that has a <h2> followed by bedroom/bathroom text.
    for rent_td in rent_tds:
        row = rent_td.find_parent("tr")
        if not row:
            continue

        unit = _parse_row(row)

        # Determine beds/baths from the enclosing floor plan section
        beds, baths = _find_floorplan_beds_baths(rent_td)
        unit.beds = beds
        unit.baths = baths

        raw.append(unit)

    raw += _units_the_cards_missed(raw, listed)
    groups = _group_by_beds_and_sf(raw, stated)
    if not groups:
        warnings.append("parsed cells but no usable groups (missing beds/baths?)")
    return groups, warnings


def _parse_row(row) -> _RawUnit:
    """Pull rent/sqft/available from a single <tr>."""
    cells = {}
    for cls in ("td-card-name", "td-card-rent", "td-card-sqft", "td-card-available"):
        c = row.find("td", class_=cls)
        if c:
            cells[cls] = c.get_text(" ", strip=True)

    rent_int = _parse_currency(cells.get("td-card-rent"))
    rent_allin = None
    # THE INCLUSIVE-CHARGE TEMPLATE (ATL Parkwood Terrace). The cell prints only
    # "Total Monthly Leasing Price  Rent: $2,426.50 ..." -- base rent PLUS mandatory monthly
    # fees -- so the first currency is the total, and it was stored as rent_type="base":
    # +$128.50 on every Parkwood unit, $28-$166 across the 8 of 60 rent-contract corpus
    # communities that run this template. The base is in the row's own info button,
    # `data-fp_base_charge="$2,298.00 ..."`, beside `data-fp_inclusive_charge`; the per-plan
    # page prints it as "Base rent $2,298.00". House rent basis is base;
    # the total goes to rent_allin, which is what the schema has that field for.
    base = None
    base_line = _BASE_RENT_RE.search(cells.get("td-card-rent") or "")
    if base_line:
        base = int(base_line.group(1).replace(",", ""))
    else:
        rent_td = row.find("td", class_="td-card-rent")
        attr = rent_td.find(attrs={"data-fp_base_charge": True}) if rent_td else None
        if attr is not None:
            base = _parse_currency(attr.get("data-fp_base_charge"))
    # A $0.00 / blank base is a sentinel, never a price: keep what the cell printed.
    if base is not None and base > 0:
        if rent_int is not None and rent_int > base:
            rent_allin = rent_int
        rent_int = base
    sf_int = _parse_int_label(cells.get("td-card-sqft"), prefix=r"(?:Sq\.?\s*Ft\.?\s*:?\s*)?")
    name = cells.get("td-card-name")

    # Listed = available (semantics decision 2026-07-14): every row on the
    # availability table is a priced, on-market unit. A future move-in date
    # ("Date: 6/12/2026") does NOT make it "coming soon" — the site's own UI
    # counts it as available, and so do the golden labels.
    status = "available"

    return _RawUnit(
        beds=None,
        baths=None,
        sf=sf_int,
        rent=rent_int,
        status=status,
        unit_number=name,
        rent_allin=rent_allin,
    )


# "Base rent $2,298.00 · 14-month term" / "Base rent from $1,864" -- the card layout's wording.
_BASE_RENT_RE = re.compile(r"Base\s+rent\s+(?:from\s+)?\$\s*([\d,]+)", re.I)


# `var available_units = [ {...}, ... ];` — the unit list the page's own script renders.
_AVAILABLE_UNITS_RE = re.compile(r"\bvar\s+available_units\s*=\s*(?=\[)")


# RentCafe's own field label in front of a unit number. The table layout's `td-card-name`
# cell reads "Apartment:-101" and `_parse_row` keeps the whole cell.
_UNIT_FIELD_LABEL_RE = re.compile(r"^\s*Apartments?\s*:\s*", re.I)


def _unit_key(code: str | None) -> str | None:
    """A unit number in one comparable form: "# 225SAN", "#225san", "225SAN" and the table
    cell "Apartment: #225SAN" agree.

    THE LABEL (CLT Beckett Farms,). `_parse_row` stores the table's whole
    `td-card-name` text, "Apartment:-101", so the key was "APARTMENT:-101".
    `_units_the_cards_missed` then found a row the embedded list "lacked" and refused the
    whole join: The Altavista's five list-only units were never counted (stored 5 of 10).
    """
    key = _UNIT_FIELD_LABEL_RE.sub("", code or "").strip().lstrip("#").strip().upper()
    return key or None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _embedded_unit_list(pages) -> list[_RawUnit]:
    """Every unit the site's embedded `available_units` list publishes, across all pages.

    THE DEFECT THIS REPAIRS (AUS Rivers Edge, 2026-09-24). On this template
    `/availableunits` renders ONE plan's detail view, C1's, and its unit cards were the
    only units the adapter read. C2's six homes and D1's five stored available=0, rent
    null, while C1 was correct. Legacy (Pflugerville) runs the same template and lost 10
    of 11 units the same way. The complete list is a script literal on `/floorplans`: one
    record per unit, each with its own FloorPlanID, Beds, SquareFeet, UnitCode, base rent
    (`IpmDisplayMinRent`, the "Base rent $2,299" on the card) and all-in (`RentCol`,
    "Total Monthly Leasing Price").

    Each record states its own plan, so nothing is inferred from where it sits in the DOM.
    That DOM-position inference is the weakness behind `_refuse_misattributed_rents`.
    Deduped on UnitCode, which is also the key the join in `_units_the_cards_missed` uses.
    A record the site flags `bRestrictPublish` is withheld from its own page and is not a
    listing. [] when no page carries the list or it does not decode.
    """
    out: list[_RawUnit] = []
    seen: set[str] = set()
    for _url, page in pages:
        m = _AVAILABLE_UNITS_RE.search(page.html)
        if not m:
            continue
        try:
            rows, _ = json.JSONDecoder().raw_decode(page.html, m.end())
        except ValueError:
            continue
        for r in rows if isinstance(rows, list) else ():
            if not isinstance(r, dict) or r.get("bRestrictPublish"):
                continue
            key, beds = _unit_key(str(r.get("UnitCode") or "")), _num(r.get("Beds"))
            if key is None or beds is None or beds < 0 or key in seen:
                continue
            seen.add(key)
            sf, rent = _num(r.get("SquareFeet")), _num(r.get("IpmDisplayMinRent"))
            rent = int(rent) if rent and rent > 0 else None
            allin = _parse_currency(r.get("RentCol"))
            out.append(_RawUnit(
                beds=int(beds),
                baths=_num(r.get("Baths")),
                sf=int(sf) if sf and sf > 0 else None,
                rent=rent,
                # Listed = available (2026-07-14): a future AvailableDate is still on-market.
                status="available",
                unit_number=str(r.get("UnitCode")).strip(),
                # Only an all-in ABOVE the base is information. Equal means no fees bundled.
                rent_allin=allin if allin and rent and allin > rent else None,
                cue_text=" ".join(str(t) for t in (r.get("FloorPlanName"),
                                                   r.get("FloorPlanDescription"),
                                                   *(r.get("Amenities") or ())) if t),
                stated=True,
            ))
    return out


# `ysi.unitsList = [...]` / `ysi.floorplansList = [...]`: the other RentCafe template's unit
# and plan lists, script literals on /floorplans (sometimes /availableunits too).
_YSI_UNITS_RE = re.compile(r"\bysi\.unitsList\s*=\s*(?=\[)")
_YSI_PLANS_RE = re.compile(r"\bysi\.floorplansList\s*=\s*(?=\[)")


def _ysi_unit_list(pages) -> list[_RawUnit]:
    """Every unit the site's `ysi.unitsList` publishes, on PROOF that the site counts it.

    THE DEFECT (M4 contract oracle). DFW Park Hill's `/availableunits` shows nine
    unit cards under the S1 (studio) heading; its `/floorplans` carries the complete list,
    35 records, each naming its own plan: those nine are A1 one-bedrooms, and B1 unit 04206
    (1,084 sf, $1,845, AvailableDate 2027-02-02) is one of nine two-bedrooms. ATL Satellite
    Square's lone card, 00021, sits under the TH-A heading (1,644 sf); its record says TH-B,
    1,782 sf, and the list's second record, 00052 (TH-C, 1,711 sf, $3,695), is on no card
    at all. Neither list was read: `_embedded_unit_list` knows only `var available_units`.

    PUBLICATION PROOF, PER PLAN. A record is used only when its plan's own entry in
    `ysi.floorplansList` (same page) publishes an `AvailableCount` EQUAL to the number of
    records naming that plan. The count is the site stating how many of that plan it has
    listed, so an equal count is the site counting every record, including a far-dated one;
    a plan whose records the count does not match is dropped whole. Measured over the 21 of
    60 sampled communities carrying the list: every plan's count equals its records, and
    where the page server-renders the unit cards (Kessler, Ovation at Town Madison, North
    Park, Avion, Roswell, The Ovilla, Satellite) every record is on the page, far-dated
    ones included (Kessler 7202, 2028-01-07). The one shortfall is a DISPLAY cap (The
    Brookstone renders 5 of B1B's 7, `maxNumberOfUnitsToDisplay = 5`, while its count says
    7), which is an under-report of the list, not the truth (B57,).

    THE PRICE. `MinRent` is the base rent unless the plan `ShowInclusiveCharges`: then it
    is base + the plan's bundled fees, to the cent (CB Lofts 535: card base $1,237, card
    total $1,683, record 1683.82; Roswell Village's every plan). A record on such a plan
    keeps its listing and gets NO rent: the base is not printed per unit and is never
    derived here. Non-positive MinRent is a sentinel: listed, unpriced.
    Deduped on UnitCode across pages. [] when no page carries both lists.
    """
    out: list[_RawUnit] = []
    seen: set[str] = set()
    for _url, page in pages:
        um, pm = _YSI_UNITS_RE.search(page.html), _YSI_PLANS_RE.search(page.html)
        if not um or not pm:
            continue
        try:
            rows, _ = json.JSONDecoder().raw_decode(page.html, um.end())
            plans, _ = json.JSONDecoder().raw_decode(page.html, pm.end())
        except ValueError:
            continue
        if not isinstance(rows, list) or not isinstance(plans, list):
            continue
        by_id = {p.get("Id"): p for p in plans if isinstance(p, dict) and p.get("Id") is not None}
        members: dict[object, int] = defaultdict(int)
        for r in rows:
            if isinstance(r, dict):
                members[r.get("FloorplanId")] += 1
        for r in rows:
            if not isinstance(r, dict):
                continue
            plan = by_id.get(r.get("FloorplanId"))
            if plan is None or _num(plan.get("AvailableCount")) != members[r.get("FloorplanId")]:
                continue
            key, beds = _unit_key(str(r.get("UnitCode") or "")), _num(r.get("Beds"))
            if key is None or beds is None or beds < 0 or key in seen:
                continue
            seen.add(key)
            sf, rent = _num(r.get("SqFt")), _num(r.get("MinRent"))
            base = rent is not None and rent > 0 and not plan.get("ShowInclusiveCharges")
            out.append(_RawUnit(
                beds=int(beds),
                baths=_num(r.get("Baths")),
                sf=int(sf) if sf and sf > 0 else None,
                rent=int(rent) if base else None,
                # Listed = available (2026-07-14): a future AvailableDate is still on-market.
                status="available",
                unit_number=str(r.get("UnitCode")).strip(),
                cue_text=" ".join(str(t) for t in (r.get("FloorplanName"),
                                                   *(r.get("Amenities") or ())) if t),
                stated=True,
                plan_ref=str(r.get("FloorplanId")),
            ))
    return out


def _take_stated_plans(cards: list[_RawUnit], records: list[_RawUnit]) -> None:
    """A unit card whose code an embedded record carries takes the RECORD's plan.

    A card's beds and SF are read off the plan heading above it, which this template does
    not always scope to the card (The Yancey,). The record names the unit's plan
    outright: ATL Satellite Square's card 00021 sits under TH-A (1,644 sf) while its record
    says TH-B, 1,782 sf; DFW Park Hill's nine cards under S1 are A1 one-bedrooms by theirs.
    Where the record's (beds, SF) differ, the card takes them (and the record's baths, when
    it states them). The card's own rent, all-in and cue text stand: the card prints them.
    Either way the card is marked `stated` and needs no proof from `_prove_plan_headers`.
    """
    by_key: dict[str, _RawUnit] = {}
    for r in records:
        by_key.setdefault(_unit_key(r.unit_number), r)
    for u in cards:
        rec = by_key.get(_unit_key(u.unit_number))
        if rec is None or rec.beds is None or rec.sf is None:
            continue
        if advertised_plan_key(rec.beds, rec.sf) != advertised_plan_key(u.beds, u.sf):
            u.beds = rec.beds
            u.sf = rec.sf
            if rec.baths is not None:
                u.baths = rec.baths
        u.stated = True


def _units_the_cards_missed(cards: list[_RawUnit], listed) -> list[_RawUnit]:
    """The embedded-list units the page's unit cards did not show, or [] if the join fails.

    🔴 ADD ONLY, AND ONLY ON A PROVEN JOIN. The card-parsed units are kept exactly as
    they were (Rivers Edge C1 is field-for-field what its cards said), and a listed unit
    joins only when its UnitCode appears on no card. The join is trusted only when EVERY
    card unit is found in the list. A card with no unit number, or one the list does not
    contain, means the two sources may name the same home differently. Adding the list
    then could count one home twice, so nothing is added and the output is today's. That
    under-counts, the safe direction for this adapter family. A page with no unit cards at
    all has nothing to reconcile, and the whole list stands.
    """
    if not listed:
        return []
    listed_keys = {_unit_key(u.unit_number) for u in listed}
    shown = {_unit_key(u.unit_number) for u in cards}
    if None in shown or not shown <= listed_keys:
        return []
    return [u for u in listed if _unit_key(u.unit_number) not in shown]


_CARD_UNIT_RE = re.compile(r"Apartment:\s*#?\s*([A-Za-z0-9-]+)", re.I)


def _page_unit_codes(html: str) -> set[str] | None:
    """Every unit code a plan's own page lists on its unit cards. None when it lists none:
    a page with no cards is silent, not a witness that the plan has no units."""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    codes = {_unit_key(c) for c in _CARD_UNIT_RE.findall(text)} - {None}
    return codes or None


def _prove_plan_headers(raw: list[_RawUnit],
                        advertised: list[UnitGroup],
                        plan_codes: Callable[[UnitGroup], set[str] | None],
                        reasons: list[str]) -> list[_RawUnit]:
    """Keep, move or refuse the unit cards under each plan heading, on PROOF of their plan.

    THE DEFECT (CLT The Yancey, 2026-09-25). `/availableunits` rendered the S1 heading,
    "Studio | 1 Bathroom | Up to 496 Sq. Ft.", over four unit cards that are A1's: 1 Bed,
    620 sf, $1,745 · $1,810 · $1,845 · $1,810, exactly what
    /floorplans/a1 lists. The card parse takes beds and SF from the heading above a card,
    so the community stored a 1bd's rents as a studio. `_refuse_misattributed_rents` cannot
    see this shape: $1,745 is above the studio floor and equals no other bed type's floor
    except in coincidence, and that guard only fires on an exact match.

    THE TRIGGER is the grid contradicting the heading. RentCafe's plan card prints
    "Starting at $X" for the plan, and X is the lowest rent among that plan's listed units:
    the base rent on most sites, the all-in on some (AUS Rivers Edge), so either minimum of
    the cards agreeing counts. When neither is one of the heading plan's own advertised
    floors, the heading's attribution is in doubt. S1 starts at $1,349; its cards start at
    $1,745. A heading whose plan advertises no floor has nothing to contradict it and is
    left exactly as it was, and so is every heading that agrees, with no extra fetch.

    THE PROOF, per candidate plan: the heading's own plan, and every plan whose floor IS
    the cards' minimum.
      * The plan's own page, when the grid links one: every card's unit code must appear
        on it. The page is the site stating the membership outright, so it decides; a page
        that lists other codes DISPROVES the plan even if its grid numbers line up.
      * No page to ask: the grid alone, and only on floor == the cards' minimum AND
        published count == the number of cards, both exact.
    Exactly one (beds, SF) proven -> the cards take that plan's beds, SF and baths (or stay,
    when it is the heading's own). None, or two -> REFUSED: the cards are dropped, so no
    rent is stored for them under any bed type. Their plan's availability still comes from
    the grid through `_merge_advertised`, as a rent-null, flag_review group, and the reason
    goes to scrape_error. Keeping them at the heading's key with rent nulled would count
    the same homes twice: once there, once on their real plan's grid card.

    BLAST RADIUS, measured the way `_merge_advertised`'s was: 60 of the 2,777 communities
    whose latest live_data snapshot used this adapter, random.seed(20260925), every page
    fetched once to disk and HEAD and this version run against the same bytes. 59 graded.

        changed                 10 of 60, every one a MOVE proven by the plan's own page
                                (the heading plan's page listed other unit codes; the new
                                plan's page listed exactly the cards' codes)
        refused                 0
        moved across bed types  6: studio->1bd (DFW Ovation at Galatyn Park), studio->2bd
                                (LAX The Meadows), 1bd->2bd (ATL The Jackson, Gwinnett
                                Pointe; AUS Madison at the Arboretum and ATL Walden
                                Crossing, which `_refuse_misattributed_rents` had nulled
                                and now carry their rents)
        same bed, wrong SF      4: PHX Woodbridge, ATL Glenn Perimeter, HOU Sterling Bay,
                                NSH The Annaline
        priced groups           133 -> 135
        total available       1,169 -> 1,154
        over public_record_units         0

    The availability drop is mostly double counting REMOVED: a misattributed block counted
    once under the heading and again on its real plan's grid card (Ovation's 8, The
    Meadows' 6). The rest is `_merge_advertised`'s +/-SF_BAND containment applied at the
    RIGHT SF, which absorbs a neighbouring 1-3 unit plan into the priced band (Sterling Bay,
    The Annaline): an under-count, the safe direction, and that rule's own measured trade.
    Detail: `.tmp/rentcafe_plan_header_blast_radius_2026-09-25.txt` in the main tree.
    """
    blocks: dict[tuple[int, int | None], list[_RawUnit]] = defaultdict(list)
    for u in raw:
        if u.beds is not None and u.rent is not None and u.rent > 0:
            blocks[advertised_plan_key(u.beds, u.sf)].append(u)
    refused: set[int] = set()
    for key, units in blocks.items():
        own = [p for p in advertised if advertised_plan_key(p.beds, p.sf_min) == key]
        own_floors = {p.rent_min for p in own if p.rent_min is not None}
        lo, n = min(u.rent for u in units), len(units)
        allins = [u.rent_allin for u in units if u.rent_allin]
        # The grid's "Starting at" is the base floor on most sites and the ALL-IN floor on
        # others: AUS Rivers Edge's C1 card reads $2,289, its cards' base minimum is $2,199
        # and their all-in minimum $2,289. Either one agreeing is the heading agreeing.
        minima = {lo, min(allins)} if allins else {lo}
        if not own_floors or own_floors & minima:
            continue
        codes = {_unit_key(u.unit_number) for u in units}
        witnesses = own + [p for p in advertised
                           if p.rent_min in minima and advertised_plan_key(p.beds, p.sf_min) != key]
        proven: list[UnitGroup] = []
        for p in witnesses:
            listed = plan_codes(p) if None not in codes else None
            if listed is not None:
                if codes <= listed:
                    proven.append(p)
            elif p.rent_min in minima and p.available == n:
                proven.append(p)
        owners = {advertised_plan_key(p.beds, p.sf_min) for p in proven}
        if len(owners) == 1:
            beds, sf = owners.pop()
            baths = next((p.baths for p in proven if p.baths is not None), None)
            if advertised_plan_key(beds, sf) != key:
                for u in units:
                    u.beds, u.sf, u.baths = beds, sf, baths
            continue
        reasons.append(
            f"plan header {key[0]}bd/{key[1]}sf lists {n} unit(s) from ${lo} but advertises "
            f"{sorted(own_floors)}; "
            + (f"{len(owners)} plans fit" if owners else "no plan's page or grid entry proves them")
        )
        refused.update(id(u) for u in units)
    return [u for u in raw if id(u) not in refused]


def _card_price_agrees(card: _RawUnit, record: _RawUnit) -> bool:
    """A plan-page card's base rent is supported by its `ysi` record."""
    if record.rent is not None:
        return card.rent == record.rent
    return (card.rent is not None and card.rent > 0 and card.rent_allin is not None
            and card.rent < card.rent_allin)


def _units_from_plan_pages(raw: list[_RawUnit],
                           contested: set[str],
                           advertised: list[UnitGroup],
                           plan_html: Callable[[UnitGroup], str | None],
                           covered: set[int],
                           records: list[_RawUnit] = ()) -> list[_RawUnit]:
    """The priced units each advertised plan's OWN page lists that the cards did not show.

    THE GAP (CLT The Yancey, 2026-09-25). `/availableunits` on this template renders ONE
    plan's detail view, so only that plan's cards were ever priced: A1's four at
    $1,745-$1,845. Every other plan came in from the grid through `_merge_advertised` with
    its availability and rent null, among them the studio's ten units at $1,349+ and all
    eleven two-bedrooms, so vendor's 2bd baseline had nothing to compare against. Each plan's
    own page, `/floorplans/<plan>` off its grid card, lists that plan's unit cards with
    their codes and prices. `_embedded_unit_list` closed the same gap on AUS Rivers Edge,
    whose `/floorplans` carries the whole list as a script; The Yancey has no such script.

    ATTRIBUTION IS THE GRID CARD'S, never the page's heading: the heading above the cards
    is exactly what `_prove_plan_headers` exists to distrust. A page is used only when it
    agrees with its grid card on BOTH numbers the grid publishes:
      * its card count equals the plan's published count, exactly. More would add units
        the grid does not claim; fewer would lower a count the grid does claim. Either
        way the page is not the plan's whole list, and the grid stands as it did.
      * its cheapest card, base or all-in, IS the plan's "Starting at", as in
        `_prove_plan_headers`. A page rendering some other plan's cards fails this.
    and it is dropped, with the grid left in charge of that plan, when
      * any card lacks a code, or one code repeats, so it cannot be deduped;
      * one of its codes is also on ANOTHER plan's page: at least one of the two is not
        scoped to its plan and nothing says which, so neither is used;
      * it lists a card `_prove_plan_headers` refused: the site already contradicted
        itself about that unit;
      * it lists a code the cards already showed under a DIFFERENT bed type.
    A code the cards already showed, at the same bed type, is not added again: the cards'
    reading stands, so no unit is ever counted twice. A card with no code among the cards
    turns the whole step off, as `_units_the_cards_missed` does, because nothing could be
    deduped against it.

    FETCH COST: one page per plan that publishes a positive count AND a floor, and whose
    exact (beds, SF) the cards do not already fill; a plan saying "Inquire for details"
    has nothing to read. Pages `_prove_plan_headers` already fetched are reused. On a site
    whose cards cover every plan, as on the multi-heading layout, no page is fetched.

    THE STATED COUNT (ATL Roswell Village). A grid card reading "Available Now" is
    published as 1, a floor (`_plan_availability`), so the count test refused every plan
    page on that template: Elliot's page lists 9 cards, Sandburg's 2, Emmerson's 4. Every
    plan there `ShowInclusiveCharges`, so the `ysi.unitsList` records carry no base rent,
    and those units were stored listed and unpriced while their pages print "Base rent".
    `records` is that list. A page ALSO passes the count test when its codes are EXACTLY
    one plan's records: `_ysi_unit_list` keeps a plan's records only when the plan's own
    AvailableCount equals them, so that is the site stating the count and the membership.
    The floor test and every refusal below apply unchanged. The pre-fetch skip also asks
    whether every recorded plan with a member at the grid plan's (beds, SF) is wholly on
    the cards, so a plan the cards only partly show is still fetched; the proof decides.

    `covered` receives id() of every plan whose page was used, including one whose every
    card the unit cards already showed; `_merge_advertised` then never emits it again.

    BLAST RADIUS, measured the way `_prove_plan_headers`'s was: 60 of the 2,779 communities
    whose latest live_data snapshot used this adapter, random.seed(20260925), every page
    fetched once to disk, HEAD (an earlier change) and this version run against the same bytes, then
    both replayed from the cache alone (0 misses, identical output). 59 graded.

        changed                 13 of 59
        bed types newly priced  18, across 12 communities
        priced groups           149 -> 181
        observed unit rents     929 -> 1,046
        bed type LOST its rent  0
        total available         1,085 -> 1,088, down in 0, over public_record_units 0
        fetches per community   4.2 -> 5.3; 16 communities fetch more, at most +11

    The +3 available is two communities where HEAD's +/-SF_BAND absorption had swallowed a
    listed plan: DFW The Ovilla's A2 (619 sf, 2 units, beside the 600 band) and DFW Ovation
    at Galatyn Park's A7 (735 sf, beside 750). Both now equal their grid totals, 17
    and 25, every unit a distinct code. Detail:
    `.tmp/rentcafe_per_plan_pages_blast_radius_2026-09-25.txt` in the main tree.
    """
    shown: dict[str, _RawUnit] = {}
    for u in raw:
        key = _unit_key(u.unit_number)
        if key is None:
            return []
        shown[key] = u
    at_key: dict[tuple[int | None, int | None], int] = defaultdict(int)
    for u in raw:
        at_key[advertised_plan_key(u.beds, u.sf)] += 1
    by_plan: dict[str, set[str]] = defaultdict(set)
    plans_at: dict[tuple[int | None, int | None], set[str]] = defaultdict(set)
    record_of: dict[str, _RawUnit] = {}
    for r in records:
        if r.plan_ref is not None:
            by_plan[r.plan_ref].add(_unit_key(r.unit_number))
            plans_at[advertised_plan_key(r.beds, r.sf)].add(r.plan_ref)
            record_of[_unit_key(r.unit_number)] = r
    stated_sets = list(by_plan.values())

    reads: list[tuple[UnitGroup, list[_RawUnit], set[str]]] = []
    for p in advertised:
        key = advertised_plan_key(p.beds, p.sf_min)
        # Skip only when the cards fill the grid's count AND every recorded plan with a
        # member at this key is wholly on the cards. By plan identity, not by key: a plan
        # spans SF values (Whitman: 1716 at 1,467, 1011 at 1,358), so a key count says
        # nothing about the member at the other size.
        # A grid count of 0 is not "no units" when the site's records list units of this plan
        # the cards never showed (PHX Avilla Grand / DFW Avilla Grove). That grid reads
        # "Available Homes for The Retreat" with no number, so every plan parsed available=0 and
        # was skipped here, while ysi.floorplansList stated AvailableCount 7 = its 7 records.
        # Those records carry no base (the plans ShowInclusiveCharges), so the plan page is
        # the only place the rents are printed. The page is then admitted only by the
        # stated-count proof below (its codes EXACTLY one plan's records), as on.
        unshown = any(not by_plan[ref] <= shown.keys() for ref in plans_at[key])
        if (p.beds is None or p.sf_min is None or p.rent_min is None
                or (p.available <= 0 and not unshown)
                or (at_key[key] >= p.available
                    and all(by_plan[ref] <= shown.keys() for ref in plans_at[key]))):
            continue
        html = plan_html(p)
        if not html:
            continue
        units = _read_unit_cards(BeautifulSoup(html, "html.parser"))
        codes = [_unit_key(u.unit_number) for u in units]
        if not units or None in codes or len(set(codes)) != len(codes):
            continue
        # A page HEAD would have fetched AND admitted (cards short of the grid's count, page
        # equal to it) is read exactly as before. Any other page reached here, by the stated
        # count or by the identity skip above, must be exactly one plan's records, and its
        # units take those records' size.
        by_record = not (at_key[key] < p.available and len(units) == p.available)
        if by_record and set(codes) not in stated_sets:
            continue
        # ... and every card must agree with its record's price. A record that carries a
        # base (non-inclusive plan) must equal the card's; an inclusive plan's record has
        # none, so the card must SHOW one beside a larger total. Otherwise the card parser's
        # "Starting at" fallback is the TOTAL, and it would be stored as base rent.
        if by_record and not all(_card_price_agrees(u, record_of[_unit_key(u.unit_number)])
                                 for u in units):
            continue
        rents = [u.rent for u in units if u.rent is not None and u.rent > 0]
        allins = [u.rent_allin for u in units if u.rent_allin]
        minima = ({min(rents)} if rents else set()) | ({min(allins)} if allins else set())
        if p.rent_min not in minima:
            continue
        reads.append((p, units, set(codes), by_record))

    claims: dict[str, int] = defaultdict(int)
    for _p, _units, codes, _stated in reads:
        for c in codes:
            claims[c] += 1
    added: list[_RawUnit] = []
    for p, units, codes, by_record in reads:
        if any(claims[c] > 1 for c in codes) or codes & contested:
            continue
        if any(shown[c].beds != p.beds for c in codes & shown.keys()):
            continue
        covered.add(id(p))
        for u in units:
            if _unit_key(u.unit_number) in shown:
                continue
            rec = record_of.get(_unit_key(u.unit_number)) if by_record else None
            if rec is not None and rec.beds is not None and rec.sf is not None:
                # Trusted on the records, so each unit takes its record's size: Whitman's
                # page says "Up to 1,467 Sq. Ft." over 1011, whose record says 1,358.
                u.beds = rec.beds
                u.sf = rec.sf
                u.baths = rec.baths if rec.baths is not None else p.baths
                u.stated = True
            else:
                u.beds, u.sf, u.baths = p.beds, p.sf_min, p.baths
            added.append(u)
    return added


# RentCafe labels EVERY unit card "Apartment: # 101" — its own field label for the
# unit number, printed on townhome and single-family sites exactly as on apartments.
# Feeding that to the structure cue stamped every RentCafe community "mf", including
# the townhome ones. Caught by test_rentcafe_reads_the_plan_heading before it shipped.
# "Starting at $1,745.00" / "Starting at: $1,745" on a marketing plan card. Deliberately
# NOT the unit-row form: a unit row's price is an actual, a plan card's is a floor.
_PLAN_START_PRICE_RE = re.compile(r"Starting\s+at:?\s*\$\s?([\d,]{3,7})", re.I)


_UNIT_LABEL_RE = re.compile(r"Apartments?:\s*#?\s*[A-Za-z0-9-]+", re.I)
# The newer card ALSO prints its buttons' accessible text on every card, townhome sites
# included: "Calculate My Costs for apartment #225SAN" and "Apply Now for Apartment
# 225SAN, opens in a new tab". Two "apartment" hits per card and none for any other
# class stamped AUS Rivers Edge and AUS Legacy (both townhome BFR) 'mf' (2026-09-24).
# The unit code must hold a digit, so "for apartment homes" is prose and stays.
_UNIT_BUTTON_RE = re.compile(r"\bfor\s+apartment\s*#?\s*(?=[A-Za-z-]*\d)[A-Za-z0-9-]+", re.I)
# Two more template lines kept 48 of 115 rentcafe BFR communities 'mf' after the button
# strip (replay, 2026-09-24). The footer disclaimer "Not all features (are) available in
# every apartment" sits inside the card's parent plan_text. The auto plan description
# "One bedroom apartment with 1 bathroom. Approximately 690.00 square feet" is on the
# whole Avilla family and on Brighton/Fiore/Sereno Townhomes. The analyst ruled the
# description template text (2026-09-24): same noun on every product class, nothing
# beyond beds/baths/SF. Only the generator's sentence is stripped, in the forms the
# replay saw: "Three-bedroom ...", "A 3-D image of a one-bedroom apartment with ..."
# (image alt text), and without the "Approximately" sentence. Prose a property wrote
# ("this charming one-bedroom, one-bathroom apartment") does not match, and neither
# does "Washer/Dryer in every apartment".
_FOOTER_DISCLAIMER_RE = re.compile(
    r"\bNot\s+all\s+features\s+(?:are\s+)?available\s+in\s+every\s+apartment\b", re.I)
_PLAN_DESCRIPTION_RE = re.compile(
    r"\b(?:Studio|(?:One|Two|Three|Four|Five)[-\s]bedroom)\s+apartment\s+with\s+"
    r"(?:\d+(?:\.\d+)?|one|two|three|four)\s*bathrooms?\.", re.I)
# The self-guided-tour button on the /floorplans cards ("Self-Guided Tour for The Retreat
# Explore the apartment home at your own pace.") was the only "apartment" on the 2bd plan
# of PHX Olive Queen Creek, Sienna at South Mountain and Soleil at Canyon Trails (replay,
# 2026-09-24). Each tour-button type has one fixed description, identical across sites;
# the analyst ruled this one template text (2026-09-24). Only that sentence is stripped.
_TOUR_WIDGET_RE = re.compile(
    r"\bExplore\s+the\s+apartment\s+home\s+at\s+your\s+own\s+pace\b", re.I)
_CUE_BOILERPLATE_RES = (_UNIT_LABEL_RE, _UNIT_BUTTON_RE, _FOOTER_DISCLAIMER_RE,
                        _PLAN_DESCRIPTION_RE, _TOUR_WIDGET_RE)


def _cue_text(plan_text: str, card_text: str) -> str:
    """Descriptive text for the structure/stories/garage cues, boilerplate removed.

    `plan_text` is a PARENT of the card, so it already contains the card's words —
    the label has to be stripped from the joined string, not from one half.
    """
    text = f"{plan_text} {card_text}"
    for pattern in _CUE_BOILERPLATE_RES:
        text = pattern.sub(" ", text)
    return text


def _read_unit_cards(soup: BeautifulSoup) -> list[_RawUnit]:
    """Every unit card on a page ("Apartment: # 246 ... Starting at: $1,745"), one per unit
    code, with beds/baths/SF read off the nearest plan heading above it."""
    raw: list[_RawUnit] = []
    seen_units: set[str] = set()
    for card in soup.select("div.card"):
        text = card.get_text(" ", strip=True)
        unit_match = re.search(r"Apartment:\s*#?\s*([A-Za-z0-9-]+)", text, re.I)
        if not unit_match:
            continue
        unit_number = unit_match.group(1)
        if unit_number in seen_units:
            continue
        seen_units.add(unit_number)

        section = card
        beds = None
        baths = None
        sf = None
        plan_text = ""
        for _ in range(8):
            section = section.parent
            if section is None:
                break
            section_text = section.get_text(" ", strip=True)
            plan_text = section_text
            bed_match = BEDS_RE.search(section_text)
            sf_match = SF_RE.search(section_text)
            if bed_match and sf_match:
                # The plan heading outranks the first "N Bedroom" in the section, which
                # can be marketing prose (Grady Square, see _PLAN_HEADING_BEDS_RE).
                heading_beds = _heading_beds(section_text)
                beds = heading_beds if heading_beds is not None else int(bed_match.group(1))
                bath_match = BATHS_RE.search(section_text)
                baths = float(bath_match.group(1)) if bath_match else None
                sf = int(sf_match.group(1).replace(",", ""))
                break
            # Fallback (2026-07-28): requiring "N Bedroom" AND a sq-ft string together
            # missed "1 Bed | 1 Bath" and Studio cards — this is LAS LVB's path, where
            # its only priced units landed in the beds-None catch-all. Resolve beds on
            # its own; SF stays optional and is filled below if a match turns up later.
            # 2026-08-10: that fallback used only the narrower `_PLAN_BEDS_RE`, which
            # needs a word boundary right after "Bed" and so can never match "Bedroom" —
            # exactly the wording `BEDS_RE` above handles. So when a plan published its
            # size in a shape SF_RE missed, the successful bed match was thrown away and
            # the units fell through to beds=None anyway. Try BEDS_RE here too.
            alt_sf = sf_match or _PLAN_SF_RE.search(section_text)
            if _PLAN_STUDIO_RE.search(section_text):
                beds = 0
            elif _PLAN_BEDS_RE.search(section_text):
                beds = int(_PLAN_BEDS_RE.search(section_text).group(1))
            elif bed_match:
                beds = int(bed_match.group(1))
            if beds is not None:
                bath_match = BATHS_RE.search(section_text)
                baths = float(bath_match.group(1)) if bath_match else None
                if alt_sf:
                    sf = int(alt_sf.group(1).replace(",", ""))
                break

        # "from" is optional (2026-08-10): on the "Base rent $1,864.00 · 18-month
        # term" wording this fell through to the all-in "Total Monthly Leasing
        # Price" and stored THAT as rent_type="base" — $2,039 instead of $1,864 on
        # Avilla Holloway, a ~9% overstatement reaching the rent-comp consumer.
        base_match = re.search(
            r"Base\s+rent\s+(?:from\s+)?\$\s*([\d,]+)", text, re.I
        )
        starting_match = re.search(r"Starting\s+at:\s*\$\s*([\d,]+)", text, re.I)
        rent = int((base_match or starting_match).group(1).replace(",", "")) if (
            base_match or starting_match
        ) else None

        allin_match = re.search(
            r"(?:Total\s+Monthly\s+Leasing\s+Price|Estimated\s+Monthly\s+Cost)"
            r"\s+Starting\s+at:\s*\$\s*([\d,]+)",
            text,
            re.I,
        )
        rent_allin = (
            int(allin_match.group(1).replace(",", "")) if allin_match and base_match else None
        )
        # Listed = available (2026-07-14): a priced card with a future
        # "Date Available:" is still an on-market listing.
        status = "available"
        raw.append(
            _RawUnit(
                beds=beds,
                baths=baths,
                sf=sf,
                rent=rent,
                status=status,
                unit_number=unit_number,
                rent_allin=rent_allin,
                cue_text=_cue_text(plan_text, text),
            )
        )
    return raw


def _parse_card_layout(soup: BeautifulSoup,
                       listed: list[_RawUnit] = (),
                       prove: Callable[[list[_RawUnit]], list[_RawUnit]] | None = None,
                       collect: Callable[[list[_RawUnit], set[str]], list[_RawUnit]] | None = None,
                       *, ysi: list[_RawUnit] = (), stated: set[int] | None = None,
                       ) -> tuple[list[UnitGroup], list[str]]:
    """Parse RentCafe's newer Bootstrap card layout.

    The v2 layout renders each apartment in ``div.card`` and places the
    floor-plan bed/SF metadata on the nearest containing section. It may show
    base and all-in monthly pricing side by side.
    """
    raw = _read_unit_cards(soup)
    had_cards = bool(raw)
    # `ysi.unitsList` is read only beside unit cards: on a page with none (The Brookstone,
    # North Park) another parse already reads the page's own rendered list, as before.
    ysi = list(ysi) if had_cards else []
    # A card an embedded record names takes the record's plan and needs no proof.
    _take_stated_plans(raw, [*listed, *ysi])
    # The cards' bed type and SF came from whichever plan heading sits above them, which
    # this template does not always scope to the cards (The Yancey, 2026-09-25). Checked
    # before the embedded list joins: those records state their own plan and need no proof.
    contested: set[str] = set()
    unproven = [u for u in raw if not u.stated]
    if prove is not None and unproven:
        before = {_unit_key(u.unit_number) for u in unproven}
        kept = {id(u) for u in prove(unproven)}
        raw = [u for u in raw if u.stated or id(u) in kept]
        contested = before - {_unit_key(u.unit_number) for u in raw} - {None}
    raw += _units_the_cards_missed(raw, listed)
    # Only on a page that showed unit cards: that is the one-plan detail view this reads
    # past. A page with none is some other template and is left exactly as it was.
    if collect is not None and had_cards:
        raw += collect(raw, contested)
    # `ysi.unitsList` joins LAST, after the plan pages: a plan page's card prints the base
    # rent, the all-in and the plan's own text, a record only `MinRent` (sometimes the
    # all-in, see `_ysi_unit_list`). So the list fills only what no card showed. With a
    # `collect`, extract's closure does it (its units must join the band basis there).
    else:
        raw += _units_the_cards_missed(raw, ysi)
    if raw:
        return _group_by_beds_and_sf(raw, stated), []

    custom = _parse_custom_floorplans(soup)
    if custom:
        return custom, []

    html_low = str(soup).lower()
    text = soup.get_text(" ", strip=True)
    if (
        "availableunit-optimized" in html_low
        or ("securecafe.com/" in html_low and "floor plan" in text.lower())
    ):
        return [], []
    return [], ["no RentCafe table or apartment cards found"]


_PLAN_BEDS_RE = re.compile(r"\b(\d)\s*Beds?\b", re.I)
_PLAN_STUDIO_RE = re.compile(r"\bStudio\b", re.I)
_PLAN_SF_RE = re.compile(r"([\d,]{3,5})\s*Sq\.?\s*Ft", re.I)

# "6 Available" / "2 Apartments Available" — the count the marketing grid publishes on a
# plan card, and the only per-plan availability figure on a RentCafe site that is actually
# SCOPED to its plan (`/availableunits` is not — see `_refuse_misattributed_rents`).
#
# 🔴 THE LOOKBEHIND IS THE WHOLE POINT. A first cut without it read the DEPOSIT: LAS
# Kaleidoscope and NSH Newport render "Deposit: $400 AVAILABLE for A1", a dollar amount
# butted against the next link's label, and that scored 400 and 250 available per plan —
# 1,600 against a 208-unit community and 1,000 against a 235-unit one. Measurement caught
# it, not review; see the 2026-08-24 blast-radius run.
_PLAN_AVAIL_COUNT_RE = re.compile(
    r"(?<![$\d.,])\b(\d{1,3})\s+(?:Apartments?|Units?|Homes?|Residences?)?\s*Available\b",
    re.I,
)

# "Available Now" / "Available from 8/26/2026" — the site asserting the plan HAS a listed
# unit while declining to say how many. HSV Providence Place publishes this on all ten of
# its plans and a count on none of them.
_PLAN_AVAIL_NOW_RE = re.compile(r"\bAvailable\s+(?:Now\b|from\s+\d)", re.I)

# A single floor plan with more listed units than this is not a count, it is a parse error.
# The largest genuine per-plan figure in the 60-community sample was 10 (RDU Walton Farms).
_PLAN_AVAIL_MAX = 99


# RentCafe renders the same per-plan count up to THREE times per card, in
# `span.fp-availability`, and on a large community they DISAGREE. HOU Smart Living's
# Monterey card publishes 10 (collapsed mobile header) · 10 (desktop block) · 15
# (mobile-only block) — `data-max="10"` is a DISPLAY CAP and the unit list caps with it,
# rendering exactly ten rows. Only the mobile-only block carries the real number.
_AVAIL_SPAN = "span.fp-availability"


def _card_span_counts(card) -> list[int]:
    """Every availability count THIS plan's own spans publish, in document order.

    Empty when the card publishes none, or when its spans name MORE THAN ONE plan — a
    section card wrapping others (NSH Newport nests them that way), where the largest
    count belongs to somebody else. Restricting to `span.fp-availability` is the other
    bound: a marketing banner elsewhere in the card is not one of these.
    """
    spans = card.select(_AVAIL_SPAN)
    if not spans or len({sp.get("data-floorplan-id") for sp in spans}) > 1:
        return []
    counts = []
    for sp in spans:
        m = _PLAN_AVAIL_COUNT_RE.search(sp.get_text(" ", strip=True))
        if m and int(m.group(1)) <= _PLAN_AVAIL_MAX:
            counts.append(int(m.group(1)))
    return counts


def _card_plan_id(card) -> str | None:
    """The ONE floor plan this card names, or None when it does not name exactly one.

    `data-floorplan-id` on `span.fp-availability` is per-plan, and `_card_span_counts`
    has relied on it since a past bug to reject a card whose spans name two plans. Here it
    answers the other question: whether two cards sharing a (beds, SF) key are the same
    plan read twice or two different plans.

    None on BOTH failure modes, and they are different failures with the same safe
    answer. A card with no `span.fp-availability` at all publishes no identity (CLT The
    Wrenley's A2 and A2A are genuinely different plans and neither card carries one). A
    card whose spans name SEVERAL plans is a section wrapper (LAX La Serena's "2
    Bedrooms" card renders plans 2075087 and 1978989 inside it). Neither can claim to be
    somebody's distinct sibling.
    """
    spans = card.select(_AVAIL_SPAN)
    if not spans:
        return None
    ids = {sp.get("data-floorplan-id") for sp in spans}
    if len(ids) != 1:
        return None
    only = ids.pop()
    return only if only else None


def _card_plan_name(card) -> str:
    """The plan this card names, as the grid prints it. "" when it names none.

    `.card-title` FIRST, then `h3`, then `h2`, and the order is the whole point. In the
    flat layout the title IS the plan (CLT The Wrenley: `A2` / `A2A`) and `h3` is a tour
    link ("Self-Guided Tour for A2"). In the NESTED layout there is no `.card-title` at
    all: the wrapper's `h2` is the SECTION ("1 Bedroom") and both the wrapper and the plan
    card inside it carry the same `h3` ("A1"). Preferring `h3` over `h2` is therefore what
    makes a wrapper and its child agree on a name and collapse, instead of the wrapper's
    section heading reading as a plan of its own. (HOU Crenshaw Grand, ATL Wayfern, LAS
    Collage and HOU The Park at Research Forest all render that shape.)
    """
    el = (card.select_one(".card-title") or card.select_one("h3") or card.select_one("h2"))
    return el.get_text(" ", strip=True) if el else ""


def _card_contains(outer, inner) -> bool:
    """True when `outer` is a DOM ancestor of `inner` — i.e. it wraps it.

    Structure, not a heuristic: a section card literally contains the plan card it
    renders, so the two are the same inventory read twice. This is the same ground truth
    the sweep graded `data-floorplan-id` against rather than letting the id mark its
    own homework, and it is what a text-prefix rule could not see.
    """
    node = inner.parent
    while node is not None:
        if node is outer:
            return True
        node = node.parent
    return False


def _card_published_availability(card) -> int | None:
    """The largest count this plan's own spans publish. None when there are none to read.

    🔴 WHY MAX AND NOT A WRAPPER RULE. The obvious structural rule — take the count
    outside the `d-none d-md-block` desktop wrapper — does not discriminate: the collapsed
    mobile header sits outside it too and is capped identically (10 · 15 on the Monterey
    card, both outside). The value is the only thing separating them, so the count is the
    max, bounded as `_card_span_counts` describes. `data-original` is not the answer
    either: on that same card it reads 10, the capped number.
    """
    counts = _card_span_counts(card)
    return max(counts) if counts else None


def _card_cap_delta(card) -> int:
    """How many listed units this card's DISPLAY CAP hides. 0 unless the cap is PROVEN.

    Three conditions, all required, because this number is added to a PRICED group and an
    unearned delta invents inventory:
      * the card's spans declare a `data-max` — the cap the site itself states;
      * the SMALLEST published count equals it, i.e. the list really was truncated there;
      * the LARGEST exceeds it, which is the untruncated figure.
    Smart Living's Monterey: `data-max="10"`, counts 10 · 10 · 15 -> 5 hidden.

    🔴 A DISAGREEMENT IS NOT BY ITSELF A CAP. Returning max - min for any disagreement
    made 4 and 5 under `data-max="10"` worth a delta of 1, raising availability although
    nothing was truncated — and an inflation that small clears
    `_refuse_impossible_availability` every time.

    A DELTA rather than a replacement on purpose: adding back the hidden units cannot
    double-count units the priced feed already listed, which replacing a count with a
    band sum could.
    """
    counts = _card_span_counts(card)
    if len(counts) < 2:
        return 0
    caps = {sp.get("data-max") for sp in card.select(_AVAIL_SPAN)}
    if len(caps) != 1:
        return 0
    cap = caps.pop()
    if cap is None or not str(cap).isdigit():
        return 0
    cap = int(cap)
    lo, hi = min(counts), max(counts)
    return hi - cap if lo == cap and hi > cap else 0


# The plan's own page: "/floorplans/a1" on the grid card's "Availability for A1" link.
_PLAN_PAGE_HREF_RE = re.compile(r"/floor-?plans/[^/?#]+/?$", re.I)


def _card_plan_href(card) -> str | None:
    """The link to this plan's own page, as the card writes it. None when it has none,
    or when it links SEVERAL plan pages (a section wrapper speaks for no single plan)."""
    hrefs = {a["href"].split("?")[0].split("#")[0] for a in card.select("a[href]")
             if _PLAN_PAGE_HREF_RE.search(a["href"].split("?")[0].split("#")[0])}
    return hrefs.pop() if len(hrefs) == 1 else None


def _plan_availability(text: str, card=None) -> int:
    """How many units the grid says this plan has listed. 0 when it does not say.

    Three cases, in order of how much the site actually committed to:
      "6 Available"            -> 6, the published count
      "Available Now" / dated  -> 1, the FLOOR of what the site asserted. It can only
                                 understate, never inflate, and it is what separates
                                 "live, count unpublished" from "nothing listed" — the
                                 distinction `available` exists downstream to carry.
      "Inquire for details",   -> 0. LAS Tides' Playa plan says this and offers only
      "Call for details",         "Add to waitlist"; its plan page has no availability
      silence                     link at all. 0 is the honest read, not a floor of 1.
    """
    if card is not None:
        published = _card_published_availability(card)
        if published is not None:
            return published
    m = _PLAN_AVAIL_COUNT_RE.search(text)
    if m:
        n = int(m.group(1))
        return n if n <= _PLAN_AVAIL_MAX else 0
    return 1 if _PLAN_AVAIL_NOW_RE.search(text) else 0


def _parse_advertised_plans(soup: BeautifulSoup,
                            capped_deltas: dict[int, int] | None = None,
                            plan_pages: dict[int, str] | None = None,
                            ) -> list[UnitGroup]:
    """Parse the marketing floorplan grid: div.card blocks reading
    "The Alcove | 1 Bed | 1 Bath | 690 Sq. Ft." with NO price and NO unit rows.

    These are ADVERTISED plans, carried so a bed type never disappears just because the
    priced feed has nothing to say about it.

    AVAILABILITY (2026-08-24). `available` used to be hardcoded 0 here, which made every
    advertised-only plan look unrentable: LAS Tides on Charleston stored 1bd=2 / 2bd=0 /
    2bd=0 / 3bd=0 against a grid reading 6 / 1 / waitlist / 2, and `the rent-comp consumer`
    reads `available` to decide whether a bed type is live. The grid states the count per
    plan and states it next to that plan's own beds and SF, so it is taken from there;
    `_plan_availability` says what each form of words is worth.

    THE CARD IS PASSED, NOT JUST ITS TEXT (a past bug, 2026-09-19). The count is published
    up to three times per card and the first one in document order -- the only one a text
    search can reach -- is capped at `data-max`. Smart Living's Monterey read 10 against a
    true 15, under-reporting a live plan silently and in the SAFE direction, which is why
    it survived a golden round. `_card_published_availability` reads the card's structure;
    a card with no `span.fp-availability` at all (HSV Providence Place, The Hadley) falls
    through to the text path unchanged.

    TWO REAL PLANS AT ONE (beds, SF) KEEP THEIR OWN COUNTS (a past bug, 2026-09-21). Cards
    used to be deduped on (beds, sf) alone, keeping max(), which is right for a NESTED
    card and wrong for a genuine sibling pair -- ATL The Crest at Acworth publishes A1 (5
    available) and A2 (8 available), both 1 Bed / 777 Sq. Ft., and the community stored 8.
    A key now SPLITS when every card on it names itself with a distinct `data-floorplan-id`
    (`_card_plan_id`), and collapses on max() otherwise, exactly as before. The deltas come
    back per PLAN rather than per key for the same reason: an uncapped plan beside a capped
    one must not inherit its sibling's delta.

    🔴 ONE UNREADABLE CARD POISONS THE WHOLE KEY, which is why this needs two passes over
    the cards rather than a decision taken as each arrives. A card that names no single
    plan may BE the section wrapper for the very plans beside it (LAX La Serena's "2
    Bedrooms" card renders both 2075087 and 1978989 inside itself); folding its count into
    one sibling with max() while ALSO emitting the others would count the same units twice,
    and that is this row's stated FAIL. So the split requires every card on the key to
    resolve, not merely the ones being split apart.

    MEASURED, 60 live communities sampled 2026-09-21 (seed 20260921) out of the 2,281 whose
    latest live snapshot used this adapter: 419 (beds, SF) keys, 63 of them carrying more
    than one card. 52 of those 63 are structurally DISJOINT cards (real sibling plans) and
    11 are NESTED. Only 10 of the 52 resolve to distinct ids and split; the other 42 have at
    least one card with no `span.fp-availability` at all and stay collapsed -- an honest
    under-count, which is the safe direction for a comp set, rather than a guess promoted to
    evidence. All 11 nested collisions still collapse. Detail: `.tmp/rentcafe_sibling_plans_blast_radius_2026-09-21.txt`.
    """
    # PASS 1 -- read every card the grid offers, keeping each one's own identity.
    cards: list[dict] = []
    for card in soup.select("div.card"):
        text = card.get_text(" ", strip=True)
        if not text or "Apartment:" in text:      # unit card, not a plan card
            continue
        sf_m = _PLAN_SF_RE.search(text)
        beds_m = _PLAN_BEDS_RE.search(text)
        if beds_m:
            beds = int(beds_m.group(1))
        elif _PLAN_STUDIO_RE.search(text):
            beds = 0
        else:
            continue
        if not sf_m:                               # bed chip without SF = nav noise
            continue
        sf = int(sf_m.group(1).replace(",", ""))
        cards.append({
            "key": advertised_plan_key(beds, sf),
            "beds": beds,
            "sf": sf,
            "card": card,
            "name": _card_plan_name(card),
            "plan_id": _card_plan_id(card),
            "avail": _plan_availability(text, card),
            "delta": _card_cap_delta(card),
            "href": _card_plan_href(card),
            "text": text,
        })

    # PASS 2 -- decide, per key, whether its cards are separable AT ALL.
    #
    # Two cards can share a (beds, sf) key for two unrelated reasons, and only one of them
    # is additive. LAS Montecito Pointe really does run TANGIER and TANGIER GOLD as separate
    # 1bd/716sf plans. But NSH Newport NESTS its cards -- the "1 Bedroom" section card and
    # the A1 card inside it are the same inventory read twice -- so summing would
    # double-count. `data-floorplan-id` settles it where the markup publishes one: LAX La
    # Serena's nested pair carries the SAME id 1978987 on both cards, while ATL The Crest at
    # Acworth's A1 and A2 carry 4561572 and 4561573. Where it does not, MAX remains the only
    # aggregate wrong in the safe direction under both readings.
    by_key: dict[tuple[int, int | None], list[dict]] = defaultdict(list)
    for c in cards:
        by_key[c["key"]].append(c)
    splits: dict[tuple[int, int | None], bool] = {}
    for key, group in by_key.items():
        ids = [c["plan_id"] for c in group]
        if all(i is not None for i in ids):
            # Every card names its own plan, so the SITE has answered — trust it over any
            # reading of the headings. Two cards on one id are one plan however their
            # titles read (a mobile block and a desktop block).
            splits[key] = len(set(ids)) == len(ids)
        else:
            # 🔴 NO USABLE ID: fall back to STRUCTURE plus the plan NAME (a past bug).
            # `data-floorplan-id` resolved only 10 of 52 structurally-disjoint collisions
            # in the 2026-09-21 sweep; the other 42 are real sibling pairs where at least
            # one card publishes no `span.fp-availability` at all, and they stayed
            # collapsed on max() with the smaller plan's units dropped.
            #
            # Both limbs are load-bearing and neither would do alone. DOM ancestry rules
            # out the section-wrapper case outright — a wrapper literally contains the
            # plan card — which is exactly what a leading-TEXT rule could not see: a
            # wrapper's text opens with its section heading, so all 10 nested pairs read
            # as "distinctly named" and would have been counted twice, this row's stated
            # FAIL. And the name rules out one plan rendered twice in two disjoint blocks,
            # which structure cannot see. Measured over the same 60-community cache:
            # 52 of 52 disjoint collisions split, 11 of 11 nested still collapse, zero
            # nested false splits.
            # 🔴 AND IT STILL HONOURS THE IDS IT DOES HAVE. The fallback fires when ANY
            # card lacks an id, and deciding purely on names then threw away the ids of the
            # cards that HAVE one: two cards on id `77` beside one id-less sibling read as
            # three distinct plans and emitted 3 + 3 + 1 for a plan the site publishes
            # three of. That is INFLATION, the one direction this family must never go, and
            # `_merge_advertised` carries it straight through. Two cards sharing an id are
            # one plan whatever their titles say, so a shared id blocks the split outright.
            # Collapsing under-counts that key, which is the same safe direction as an
            # unnamed card poisoning it: the decision is per KEY, so partial grouping is
            # not expressible here, and an under-count never invents a unit.
            present = [i for i in ids if i is not None]
            names = [c["name"] for c in group]
            nested = any(_card_contains(x["card"], y["card"])
                         for x in group for y in group if x is not y)
            splits[key] = (not nested
                           and all(names)
                           and len(set(names)) == len(names)
                           and len(set(present)) == len(present))

    plans: list[UnitGroup] = []
    seen: dict[tuple[int, int | None], UnitGroup] = {}

    def _charge_delta(group: UnitGroup, delta: int) -> None:
        """Record a card's proven cap delta against the PLAN it belongs to.

        Keyed on the object, not on (beds, sf): siblings share that key by definition, and
        charging both of them one shared delta is half of what this row is about. The
        groups stay alive in the returned list for as long as the dict is consulted --
        `extract` only ever replaces `advertised` and `capped_deltas` together.
        """
        if capped_deltas is not None and delta:
            capped_deltas[id(group)] = max(capped_deltas.get(id(group), 0), delta)

    for c in cards:
        key = c["key"]
        if key in seen and not splits[key]:
            prior = seen[key]
            prior.available = max(prior.available, c["avail"])
            _charge_delta(prior, c["delta"])
            continue
        # The grid publishes "Starting at $1,745.00" per plan. This used to be dropped,
        # which left `_refuse_misattributed_rents` with nothing to cross-check against --
        # and that blindness is what let The Hadley ship a 3-bedroom's rent on its
        # 1-bedroom group (SRQ golden, 2026-08-24). It is a FLOOR, not an actual rent, so
        # it is recorded with rent_basis="starting_at" (migration 0019) and never as
        # rent_max.
        text = c["text"]
        # The marketing card carries the same footer disclaimer and auto plan description
        # as the unit cards. Unstripped, they left Avilla's advertised 2bd/3bd 'mf' after
        # the unit-card path was fixed (2026-09-24). Baths still reads the raw text.
        cue_text = _cue_text("", text)
        adv_m = _PLAN_START_PRICE_RE.search(text)
        adv_rent = int(adv_m.group(1).replace(",", "")) if adv_m else None
        plans.append(
            UnitGroup(
                beds=c["beds"],
                sf_min=c["sf"],
                sf_max=c["sf"],
                available=c["avail"],
                # NOT coming_soon. `coming_soon` is reserved for advertised-but-unlisted
                # plans (the project rules); a grid count is a count of LISTED units, and a future
                # move-in date does not demote one (availability semantics, 2026-07-14).
                coming_soon=0,
                rent_min=adv_rent,
                rent_max=None,
                rent_type="base",
                rent_basis="starting_at" if adv_rent is not None else "unspecified",
                # An advertised plan with nothing listed still HAS a structure --
                # the plan card names it. unit_rents stays None because there are
                # no UNITS to observe; that is a different question.
                structure_type=cues.structure_type_from_text(cue_text),
                stories=cues.stories_from_text(cue_text),
                garage=cues.garage_from_text(cue_text),
                baths=_baths_from_text(text),
                flag_review=True,   # advertised only -- no listing to verify against
            )
        )
        _charge_delta(plans[-1], c["delta"])
        if plan_pages is not None and c["href"]:
            plan_pages[id(plans[-1])] = c["href"]
        seen.setdefault(key, plans[-1])
    return plans


def _parse_custom_floorplans(soup: BeautifulSoup) -> list[UnitGroup]:
    """Parse WordPress floor-plan cards that link leasing through SecureCafe."""
    groups: list[UnitGroup] = []
    for card in soup.select(".floor-plan"):
        text = card.get_text(" ", strip=True)
        count_match = re.search(r"(\d+)\s+(?:homes?|units?)\s+available", text, re.I)
        bed_match = re.search(r"(\d+)\s+Bed", text, re.I)
        sf_match = re.search(r"([\d,]+)\s+SF", text, re.I)
        rent_match = re.search(r"Base\s+Rent\s+from\s*\$\s*([\d,]+)", text, re.I)
        if not count_match or not bed_match or not sf_match or not rent_match:
            continue
        rent = int(rent_match.group(1).replace(",", ""))
        sqft = int(sf_match.group(1).replace(",", ""))
        groups.append(
            UnitGroup(
                beds=int(bed_match.group(1)),
                sf_min=sqft,
                sf_max=sqft,
                available=int(count_match.group(1)),
                rent_min=rent,
                rent_max=rent,
                rent_type="base",
                baths=_baths_from_text(text),
            )
        )
    return groups


def _find_floorplan_beds_baths(rent_td) -> tuple[int | None, float | None]:
    """Walk up the DOM to find the nearest <h2> floor plan heading + bed/bath text."""
    cur = rent_td
    for _ in range(15):  # safety bound
        cur = cur.find_parent()
        if cur is None:
            break
        # Look for h2 within this parent's preceding siblings or the parent text
        text = cur.get_text(" ", strip=True)
        # Quick check: does this section contain "N Bedroom(s) | M.M Bathroom(s)"?
        m_beds = BEDS_RE.search(text)
        m_baths = BATHS_RE.search(text)
        if m_beds and m_baths:
            try:
                return int(m_beds.group(1)), float(m_baths.group(1))
            except ValueError:
                pass
        # Fallbacks (2026-07-28): requiring BOTH "N Bedroom" AND "N Bathroom" missed the
        # "1 Bed | 1 Bath" template and every Studio plan, dumping those units into the
        # beds-None catch-all where nothing downstream could read them (86 rentcafe
        # communities affected). The strict pair is still tried first, so no
        # currently-matching layout changes.
        #
        # 2026-08-13: each fallback now also returns whatever bath count its own text
        # carries, via _baths_from_text (the short "1 Bath" form BATHS_RE cannot see).
        # These used to hard-return None with the comment "baths are never stored" —
        # true until UnitGroup.baths existed, and the reason the "1 Bed | 1 Bath"
        # template would otherwise still yield a blank Bath cell on 86 communities.
        alt_baths = _baths_from_text(text)
        if _PLAN_STUDIO_RE.search(text):
            return 0, alt_baths
        m_alt = _PLAN_BEDS_RE.search(text)
        if m_alt:
            try:
                return int(m_alt.group(1)), alt_baths
            except ValueError:
                pass
        # Same rescue as the card layout (2026-08-10): `_PLAN_BEDS_RE` cannot match
        # "Bedroom", so a section that says "1 Bedroom" with no parseable bathroom
        # text used to fall through to beds=None.
        if m_beds:
            try:
                return int(m_beds.group(1)), alt_baths
            except ValueError:
                pass
    return None, None


def _parse_currency(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"\$\s?([0-9][0-9,]*)(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_int_label(s: str | None, prefix: str = "") -> int | None:
    if not s:
        return None
    m = re.search(rf"{prefix}([0-9][0-9,]*)", s)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_concession(text: str) -> tuple[int | None, str | None]:
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, CONCESSION_WEEKS_RE, CONCESSION_MONTHS_RE)


# ---------------------------------------------------------------------------
# Grouping (same logic as ARK/SightMap — should be extracted to shared helper)
# ---------------------------------------------------------------------------
def _group_by_beds_and_sf(listings: list[_RawUnit],
                          stated: set[int] | None = None) -> list[UnitGroup]:
    """`stated`, when given, receives id() of every group whose EVERY member is `stated`
    (its plan named by an embedded record,). A mixed group is never added."""
    sortable = [r for r in listings if r.beds is not None]
    sortable.sort(key=lambda r: (r.beds or 0, r.sf or 0))

    buckets: dict[tuple[int, int], list[_RawUnit]] = defaultdict(list)
    band_centers: dict[int, list[int]] = defaultdict(list)
    for r in sortable:
        beds_key = r.beds
        sf = r.sf or 0
        center = None
        for existing in band_centers[beds_key]:
            if abs(sf - existing) <= 2 * SF_BAND:
                center = existing
                break
        if center is None:
            center = sf
            band_centers[beds_key].append(center)
        buckets[(beds_key, center)].append(r)

    groups: list[UnitGroup] = []
    for (beds, _center), items in buckets.items():
        sfs = [r.sf for r in items if r.sf is not None]
        # `is not None` keeps BOTH 0 and -1. RentCafe renders an unpriced unit as
        # "Rent: $0.00" and `_parse_currency` reads that as the integer 0, so a
        # whole group could report rent_min=rent_max=0 (a past bug). $0 is not a
        # rent; the units stay listed and the group stays in the set.
        rents = [r.rent for r in items if r.rent is not None and r.rent > 0]
        allin_rents = [r.rent_allin for r in items if r.rent_allin is not None and r.rent_allin > 0]
        avail_count = sum(1 for r in items if r.status == "available")
        soon_count = sum(1 for r in items if r.status == "coming_soon")
        cue_text = " ".join(r.cue_text for r in items if r.cue_text)
        group = (
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=avail_count,
                coming_soon=soon_count,
                rent_min=min(rents) if rents else None,
                rent_max=max(rents) if rents else None,
                rent_type="base",
                rent_allin_min=min(allin_rents) if allin_rents else None,
                rent_allin_max=max(allin_rents) if allin_rents else None,
                # BASE rents, matching rent_min/rent_max. Mixing an all-in member
                # into this list would make the lowest-five mean incomparable to
                # the endpoints sitting beside it.
                unit_rents=observed_rents(rents),
                structure_type=cues.structure_type_from_text(cue_text),
                stories=cues.stories_from_text(cue_text),
                garage=cues.garage_from_text(cue_text),
                # Already read off the plan heading by _find_floorplan_beds_baths —
                # this is the carry-through that was missing, not a new parse.
                baths=observed_baths(r.baths for r in items),
                flag_review=not rents or not sfs,
            )
        )
        if stated is not None and all(r.stated for r in items):
            stated.add(id(group))
        groups.append(group)

    unknown = [r for r in listings if r.beds is None]
    if unknown:
        # Same sentinel rule as the bedded groups above (a past bug).
        rents = [r.rent for r in unknown if r.rent is not None and r.rent > 0]
        allin_rents = [r.rent_allin for r in unknown if r.rent_allin is not None and r.rent_allin > 0]
        avail_count = sum(1 for r in unknown if r.status == "available")
        soon_count = sum(1 for r in unknown if r.status == "coming_soon")
        groups.append(
            UnitGroup(
                beds=None,
                sf_min=None,
                sf_max=None,
                available=avail_count,
                coming_soon=soon_count,
                rent_min=min(rents) if rents else None,
                rent_max=max(rents) if rents else None,
                rent_type="base",
                rent_allin_min=min(allin_rents) if allin_rents else None,
                rent_allin_max=max(allin_rents) if allin_rents else None,
                unit_rents=observed_rents(rents),
                flag_review=True,
            )
        )

    groups.sort(key=lambda g: (g.beds or -1, g.sf_min or -1))
    return groups


def _refuse_misattributed_rents(priced: list[UnitGroup],
                                advertised: list[UnitGroup],
                                reasons: list[str] | None = None,
                                condemned: set[tuple[int, int | None]] | None = None,
                                exempt: set[int] = frozenset(),
                                ) -> list[UnitGroup]:
    """Null the rent on any priced group whose price belongs to a DIFFERENT bed type.

    THE DEFECT THIS CATCHES (The Hadley, SRQ golden 2026-08-24). `/availableunits` on some
    RentCafe sites renders ONE plan's detail template — a single `<h2>`, one
    "1 Bedroom | 1 Bathroom" header — and then lists unit rows that the page never scoped
    to that plan. `_find_floorplan_beds_baths` walks up the DOM and takes the first
    bed/bath text in an ancestor's concatenated text, so every unit inherits whichever
    heading sits first in the subtree. The Hadley shipped
    `beds=1 sf=739 rent=2295-2445` — the Director's THREE-bedroom price, to the dollar —
    while its 2bd and 3bd groups carried no rent at all.

    IT DOES NOT GUESS THE RIGHT PLAN. Guessing is what produced the wrong number. The
    marketing grid states a floor per bed type; if a group's observed rent_min sits closer
    to some OTHER bed type's floor than to its own, the attribution cannot be trusted and
    the rent goes to null + flag_review. Square footage and availability are kept — those
    were never in doubt; only the price was.

    Deliberately silent when there is nothing to check against: no advertised plans, or
    only one bed type, means there is no other plan the price could have come from.
    """
    reasons = reasons if reasons is not None else []
    condemned = condemned if condemned is not None else set()
    # 🔴 THE ACCUSATION BASIS IS THE PRE-SPLIT VIEW OF THE GRID, ON PURPOSE. It is
    # last-wins by bed type over ONE entry per (beds, SF) -- the first card's price, which
    # is exactly the list this function saw before a past bug split sibling cards apart.
    # Without that, splitting a key changes which sibling speaks for its bed type and so
    # changes whose rent gets condemned somewhere else entirely: a 1bd floor of $1,000
    # beside two 2bd siblings advertising $1,600 then $1,500 leaves floors[2] at 1500, and
    # a premium 1bd honestly priced at $1,500 is nulled.
    #
    # The obvious alternative -- accuse against EVERY advertised floor -- was implemented
    # and measured over the 60-community cache first and rejected. With three to eight
    # floors per bed type an exact collision stops being evidence, and it newly condemned
    # LAS Playa Vista (1bd $1,050 over its own $1,000 floor), HOU Harbor Cove ($1,606) and
    # AUS Monterey Ranch ($1,215): premium units above their own floor, which is the exact
    # failure this guard's docstring records from its first, blunter version.
    floors: dict[int, int] = {}
    _seen_keys: set[tuple[int, int | None]] = set()
    for g in advertised:
        if g.beds is None:
            continue
        key = advertised_plan_key(g.beds, g.sf_min)
        if key in _seen_keys:       # a split sibling; the pre-split grid had one entry
            continue
        # THE KEY IS CLAIMED BEFORE THE PRICE IS CHECKED. "Starting at $X" is per card and
        # some cards carry none; skipping a priceless FIRST sibling before recording its
        # key let the SECOND sibling's price into this basis -- the exact leak the basis
        # exists to stop.
        _seen_keys.add(key)
        if g.rent_min is None:
            continue
        floors[g.beds] = g.rent_min
    # 🔴 THE SELF-EXEMPTION NEEDS EVERY ONE OF A BED TYPE'S OWN FLOORS, not whichever
    # plan the grid printed last. `floors` above is a dict comprehension, so one plan per
    # bed type survives it; that was already arbitrary across two SF plans of one bed
    # type, and a past bug put a second entry on the SAME (beds, SF) key and made it bite.
    # Siblings advertising $1,500 and $1,400 at 1bd/777sf, beside a 2bd plan advertising
    # $1,500: floors[1] becomes 1400, so a one-bedroom honestly priced at its own plan's
    # $1,500 stops matching its own floor, matches the 2bd floor to the dollar, and is
    # condemned -- rent AND `unit_rents` nulled on a correct read.
    #
    # ONLY THE EXEMPTION WIDENS. Widening the ACCUSATION the same way was tried and
    # measured first, and it is a different change with a real cost: with three to eight
    # floors per bed type an exact collision becomes coincidence rather than evidence, and
    # over the same 60-community cache it newly condemned LAS Playa Vista (1bd $1,050 over
    # its own $1,000 floor), HOU Harbor Cove ($1,606) and AUS Monterey Ranch ($1,215) --
    # premium units sitting above their own floor, which this guard's docstring names as
    # the exact thing the first, blunter version got wrong. Accusation therefore stays on
    # `floors` as built above, so condemnation is unchanged from HEAD everywhere and this
    # edit can only ever SAVE a rent, never take one.
    own_floors: dict[int, set[int]] = defaultdict(set)
    for g in advertised:
        if g.beds is not None and g.rent_min is not None:
            own_floors[g.beds].add(g.rent_min)
    if len(floors) < 2:
        return priced
    for g in priced:
        if g.beds is None or g.rent_min is None or g.beds not in floors:
            continue
        # A group whose every unit an embedded record places on its plan: the record
        # is the site naming the plan, so a price coinciding with another bed type's floor is
        # coincidence, not evidence.
        if id(g) in exempt:
            continue
        # EXACT equality with another bed type's advertised floor, and not with its own.
        #
        # The first version of this used "nearest advertised floor" and it was far too
        # blunt: measured on 30 random live rentcafe communities it nulled 9 of them
        # (30%), which across ~1,430 live rentcafe communities would have destroyed more
        # real rents than the bug it fixes. Real units legitimately sit ABOVE their own
        # plan's floor and can overtake a higher bed type's floor while doing so — PHX
        # Adiamo Palm Valley (1bd $1,721 against floors 1255/1433), PHX Acacia Pointe
        # (1bd $1,215 against 925/1099) and AUS Sedona Springs are all honest premium
        # units, not misattribution.
        #
        # A plan's floor price reproduced TO THE DOLLAR as a different bed type's minimum
        # is not a coincidence — it is the same string read off the wrong plan. That is
        # the only signal precise enough to act on. It keeps The Hadley (1bd 2295 = 3bd
        # floor 2295), TPA Fisherman's Landing (1950 = 1950), CLT The Grayson
        # (1483 = 1483), NSH Sumner Estates (1349 = 1349) and PHX The Link (895 = 895).
        # Innocent when the price is one of MY OWN plans' floors -- any of them, not
        # whichever card the grid happened to print last.
        if g.rent_min in own_floors[g.beds]:
            continue
        # ... or when its ALL-IN is one of them. A grid that advertises the total
        # while the rows now store the base puts the two on different bases; the base is
        # then compared with other bed types' TOTAL floors, and a to-the-dollar collision
        # would null a correct read. The group's own all-in matching its own floor is the
        # site pricing THIS plan. Only saves a rent; accusation is unchanged.
        if g.rent_allin_min is not None and g.rent_allin_min in own_floors[g.beds]:
            continue
        exact = [b for b, f in floors.items() if f == g.rent_min and b != g.beds]
        if exact:
            best_beds = exact[0]
            # The reason goes to the SNAPSHOT's scrape_error, never onto UnitGroup:
            # weekly_snapshots.units has a fixed JSONB shape (the project rules) and the writer
            # does not reshape, so an ad-hoc field here would be silently dropped or,
            # worse, silently persisted.
            reasons.append(
                f"{g.beds}bd rent {g.rent_min} is EXACTLY the advertised {best_beds}bd "
                f"floor ({floors[best_beds]}); its own floors are "
                f"{sorted(own_floors[g.beds]) or 'none'}"
            )
            g.rent_min = None
            g.rent_max = None
            g.unit_rents = None
            # The row's all-in came off the same misplaced prices (made table rows
            # carry it): the wrong plan's totals go with its base.
            g.rent_allin_min = None
            g.rent_allin_max = None
            g.flag_review = True
            # (beds, sf_min), not beds. Condemnation is a property of THIS GROUP; a
            # same-bed sibling at another SF was never accused of anything and must not
            # be recounted with it.
            condemned.add(advertised_plan_key(g.beds, g.sf_min))
    return priced


def _recount_condemned_from_grid(priced: list[UnitGroup],
                                 advertised: list[UnitGroup],
                                 condemned_keys: set[tuple[int, int | None]]
                                 ) -> list[UnitGroup]:
    """Re-read a CONDEMNED group's availability off its own plan card.

    A group the misattribution guard condemned did not merely get the wrong price — it
    counted units the page never scoped to it. LAS Tides on Charleston stored
    `beds=1 sf=700 avail=2`, and those two units are C-212 and T-077 at $1,575, which
    /floorplans/3-bed-2-bath-b lists as the THREE-bedroom's. Left alone they are counted
    twice over: once as a phantom 1bd pair and once as the real 3bd pair. Its own plan
    card — Reef, 1 Bed, 700 Sq. Ft. — says 6, and /floorplans/reef lists 6.

    ONLY CONDEMNED GROUPS, and only on an exact (beds, sf_min) match to a plan card.
    On a healthy site the priced count already equals the sum of the grid cards falling in
    its SF band — LAS Montecito Pointe 2bd/945 priced 10 = grid 3+5+2, PHX The Highland
    1bd priced 8 = grid 3+4+1, RDU Walton Farms 1bd priced 14 = grid 4+4+6 — so a
    grid-wins rule applied everywhere would be churn at best and, where SF banding merges
    two plans into one group, a silent undercount. The condemnation is the evidence, and
    it is the same evidence that condemned the rent.

    🔴 IT ONLY EVER RAISES, NEVER ZEROES. A card that publishes no availability language
    is silent, not claiming zero, and the first cut of this function could not tell the
    difference: PHX Vintage at Scottsdale's grid states plan name, beds, bath, SF and
    prose and nothing else, while `/availableunits` lists five real dated units under an
    `<h2>Pinnacle</h2>`. Recounting from that silent card took the community 5 -> 0 in the
    blast-radius run. So the replacement happens only when the card states a positive
    count, which makes this change one-way: it can raise a plan toward what the site
    publishes and can never erase inventory.

    A condemned group with NO matching card keeps today's count for the same reason. PHX
    Ascent 1829's is `beds=2 sf=440` — 440sf being the STUDIO's footprint, itself a
    symptom of the misattribution — so nothing matches and nothing changes.
    """
    if not condemned_keys:
        return priced
    # MAX across the cards on a key, not last-wins. Sibling plans can share (beds, sf)
    # since a past bug split them, and a dict comprehension would silently hand this
    # function whichever one the grid printed last. Max is what the single merged entry
    # used to carry, so a condemned group's recount is unchanged by that split.
    cards: dict[tuple[int, int | None], int] = {}
    for p in advertised:
        k = advertised_plan_key(p.beds, p.sf_min)
        cards[k] = max(cards.get(k, 0), p.available)
    for g in priced:
        key = advertised_plan_key(g.beds, g.sf_min)
        if key not in condemned_keys:
            continue
        card = cards.get(key)
        # 🔴 RAISE ONLY, and that is now enforced rather than merely documented.
        # `if card:` accepted any positive number, including one BELOW the observed
        # count, which contradicted this function's own promise and could delete real
        # units. It is not hypothetical: the HOU/mf round proved a rentcafe card can
        # publish a CAPPED count — Smart Living's Monterey card reads 10 against a true
        # 14 — so a capped card would have erased four listed units.
        if card is not None and card > g.available:
            g.available = card
    return priced


def _restore_capped_priced_counts(priced: list[UnitGroup],
                                  advertised: list[UnitGroup],
                                  capped_deltas: dict[int, int],
                                  ) -> list[UnitGroup]:
    """Add back the listed units a plan page's DISPLAY CAP hid from the PRICED feed.

    🔴 WHY THE PARSER FIX ALONE DOES NOTHING HERE. `_card_published_availability` makes the
    ADVERTISED plan carry the site's uncapped count, but `_merge_advertised` only emits a
    plan the priced feed never saw, so on Smart Living the corrected 15 was discarded and
    the priced group's own 10 stood. The priced count is capped by the SAME mechanism: the
    plan page renders exactly `data-max` unit rows, ten of a real fifteen. Measured over 60
    live communities, the parser fix moved the stored availability of exactly ZERO of them.

    🔴 THIS IS DELIBERATELY NOT "THE GRID WINS". `_recount_condemned_from_grid` documents
    the measured reason the grid must not override a healthy priced count: on a healthy
    site the priced count ALREADY equals the sum of the grid cards in its SF band (LAS
    Montecito Pointe 2bd/945 priced 10 = grid 3+5+2), so a grid-wins rule is churn at best
    and an undercount where SF banding merged two plans. That decision stands untouched.
    What fires here is narrower: only a card on which the SITE ITSELF published two
    DISAGREEING counts, which is the site telling us its own list is truncated. A card with
    one count, or agreeing counts, has a delta of 0 and nothing happens.

    A DELTA UNDER A CEILING, and each capped plan is assigned to exactly one group. The delta is added,
    never a replacement, and the result is clamped to what the grid publishes for the band
    — so the step is idempotent and cannot stack with `_recount_condemned_from_grid`. Adding the hidden
    units cannot double-count the units already listed; replacing the count with a band sum
    could, and a plan whose SF sits inside two groups' +/-SF_BAND windows would otherwise be
    added to both. `_refuse_impossible_availability` remains the community-level backstop.
    """
    if not capped_deltas:
        return priced

    def band_of(g):
        return g.sf_min, (g.sf_max if g.sf_max is not None else g.sf_min)

    def owner(beds, sf):
        """The ONE priced group a plan card belongs to, or None.

        🔴 CONTAINMENT BEFORE TOLERANCE. Two priced 1bd groups at 700-800 and 810-810
        both reach an 810 card once +/-SF_BAND is added, and taking whichever came first
        robbed the group that actually contains it while inventing units in the one that
        does not.
        """
        cands = [g for g in priced if g.beds == beds and g.sf_min is not None]
        inside = [g for g in cands if band_of(g)[0] <= sf <= band_of(g)[1]]
        if inside:
            return inside[0]
        near = [g for g in cands
                if band_of(g)[0] - SF_BAND <= sf <= band_of(g)[1] + SF_BAND]
        if not near:
            return None
        return min(near, key=lambda g: min(abs(sf - band_of(g)[0]), abs(sf - band_of(g)[1])))

    # Each capped plan is charged to exactly one group, so no group pays for another's card.
    hidden: dict[int, int] = {}
    for p in advertised:
        if p.beds is None or p.sf_min is None:
            continue
        # By PLAN, not by (beds, sf). Two real plans can share that key, and looking the
        # delta up by it charged the capped plan's hidden units to its uncapped sibling as
        # well -- once per card, so a key with two cards paid twice (a past bug).
        delta = capped_deltas.get(id(p), 0)
        if not delta:
            continue
        g = owner(p.beds, p.sf_min)
        if g is not None:
            hidden[id(g)] = hidden.get(id(g), 0) + delta
    if not hidden:
        return priced

    # 🔴 THE BUDGET IS PER BED TYPE, NOT PER GROUP, and that is what makes inflation
    # structurally impossible rather than merely unobserved. A PER-GROUP ceiling ignores
    # the same plan's units sitting in a SIBLING group: fifteen listed units of one plan,
    # five grouped at 700 sf and ten at 850, with the card badging 10 · 10 · 15, had the
    # 850 group raised to 15 while the 700 group still held its 5 — twenty units for a
    # plan the site lists fifteen of, `flag_review` false, and far below `public_record_units` so
    # `_refuse_impossible_availability` never saw it. INFLATING is worse than the bug this
    # delta exists to repair, which only ever under-reported.
    #
    # The grid is the authority on a capped plan's bed type, so the room to grow is what
    # the grid publishes for that bed type minus what the priced feed already observed.
    # Where the priced feed already holds as much as the grid states, the cap truncated
    # the BADGE and not the feed, and the delta is correctly zero. Where the grid is the
    # narrower picture, the room is negative and clamped to 0 — RAISE-ONLY, never trim a
    # priced count down to a grid that lists fewer plans than the feed.
    grid_by_beds: dict[int, int] = {}
    for p in advertised:
        if p.beds is not None:
            grid_by_beds[p.beds] = grid_by_beds.get(p.beds, 0) + p.available
    priced_by_beds: dict[int, int] = {}
    for g in priced:
        if g.beds is not None:
            priced_by_beds[g.beds] = priced_by_beds.get(g.beds, 0) + g.available
    room = {b: max(0, grid_by_beds.get(b, 0) - seen) for b, seen in priced_by_beds.items()}

    for g in priced:
        add = hidden.get(id(g), 0)
        if not add or g.beds is None:
            continue
        add = min(add, room.get(g.beds, 0))
        if add <= 0:
            continue
        g.available += add
        room[g.beds] -= add
    return priced


def _merge_advertised(priced: list[UnitGroup], advertised: list[UnitGroup],
                      covered: set[int] = frozenset(),
                      card_groups: list[UnitGroup] | None = None) -> list[UnitGroup]:
    """Join every advertised PLAN the priced feed did not already see.

    Their advertised floor is parsed (`_refuse_misattributed_rents` needs it) but is NOT
    emitted: publishing a floor price for a bed type would put a number into
    `rents.market_rent()` that no renter is being offered. Availability, unlike price, IS
    emitted as of 2026-08-24 — the grid states it per plan, and a plan the site advertises
    as available reading 0 is what made real bed types invisible to comp sets.

    KEYED ON THE PLAN, NOT THE BED TYPE (2026-09-18). Until now this dropped an advertised
    plan whenever ANY priced group shared its bed count, so a plan the site displays with
    "Contact for Availability" vanished — SF and all — the moment one sibling plan was
    priced. HOU Smart Living stored a 2bd band of 1,052-1,113 against a site advertising
    The Sonoma at 1,231, which is 2 of the 3 sf_max misses that took HOU/mf to 11/14 79%.
    The SF-band convention (`workflows/golden_eval_austin_bfr_mf_sop.md`) says the band
    spans every advertised plan for the bed count; `_parse_advertised_plans` already reads
    them, and this was the only seam losing them.

    "Already saw" is deliberately NOT an exact (beds, sf) match. An earlier change measured that
    rule and rejected it: SF banding legitimately merges several plans into one priced
    group, so exact matching re-splits them into duplicate rent-null rows — PHX The Rey
    Downtown 9 groups -> 21, on 11 of 60 sampled communities with no availability problem
    at all. A plan is the same plan when its SF lands inside its bed type's priced band, or
    within SF_BAND of either edge — the adapter's own grouping tolerance, so the two
    descriptions of one plan reconcile. Only a plan outside every priced band for its bed
    count is new. Understating a band by <=SF_BAND stays inside the eval's own
    max(5%, 60 sf) tolerance; a phantom zero-availability row does not.

    BLAST RADIUS, measured the way an earlier change was — 60 of 2,241 live rentcafe communities,
    seeded sample, every page fetched once and both versions run against the same bytes:

        graded 59 (1 serves no inventory layout)
        group set changed on   26 of 59
        total groups          276 -> 343
        total available     1,118 -> 1,172
        WATCHLIST fail conditions (duplicate group for one plan; summed availability over
        public_record_units):  0 and 0

    The availability movement is recovered inventory, not double counting, and the
    containment test is what makes that structural: a plan the priced group already covers
    lands inside its band and is never re-emitted, so the only counts added belong to plans
    the priced feed never listed. AUS 422 at the Lake prices ONE 1bd plan (497 sf, 1 unit)
    against a grid publishing eighteen; AUS Emerson At Trace prices one of six. Leaving
    those at 0 would re-commit the error an earlier change exists to stop.

    A PRICED GROUP WITH NO SF IS "ALREADY COUNTED", NOT "ABSENT" (a past bug, 2026-09-21).
    Such a group carries a real bed count but cannot be placed against any band, and it used
    to contribute nothing at all — so the grid's cards for that bed type were appended with
    their own availability on top of listings it had already counted, INFLATING. Its bed type
    is now a BUDGET: its own count is how many grid units it can account for, spent down
    against that bed type's uncovered cards. The plan, its SF and its `flag_review` always
    survive (the SF-band convention needs them) and the bed type stays live on the priced
    group's count, while no unit is counted twice. Zeroing every card instead — the first
    cut — traded the overcount for an UNDERCOUNT, since a group holding 2 units cannot be
    three different plans at once.

    A PLAN READ OFF ITS OWN PAGE IS NAMED, NOT INFERRED (2026-09-25). `covered` holds the
    advertised plans `_units_from_plan_pages` priced from their own pages: their units are
    in `priced` by construction and the plan is never emitted again. Every OTHER plan is
    tested against `card_groups`, the groups the unit cards alone make, which is exactly the
    test this function ran before plan pages were read. A page-derived band must not absorb
    a neighbour: it holds only the codes its own pages listed, so a plan within SF_BAND of
    it is NOT inside it. The Yancey's A2.1 (745 sf, "2 Available", no page link) sits 14 sf
    below a 759-833 band built from five other plans' pages, and the bare band test dropped
    its two units. `card_groups` is None when no plan page was used, and then nothing here
    differs from before.
    """
    bands: dict[int, list[tuple[int, int]]] = defaultdict(list)
    # 🔴 A PRICED GROUP THIS CANNOT PLACE STILL COUNTED ITS UNITS (a past bug). A group with
    # a real bed count but no SF — `_parse` builds bedded groups with
    # `sf_min=min(sfs) if sfs else None`, so a layout whose rows carry beds and no square
    # footage yields exactly that — contributes no band, so every advertised card for its
    # bed type failed `_already_priced` and was appended WITH ITS OWN AVAILABILITY, on top
    # of the listings that group had already counted. A priced (1bd, sf=None, available=2)
    # against a grid (1bd, 700 sf, available=2) stored FOUR. That INFLATES, the direction
    # this family of bugs otherwise never goes, and `_refuse_impossible_availability` is
    # blind to it while the doubled count stays under `public_record_units`.
    # 🔴 AN UNPLACEABLE PRICED GROUP IS "ALREADY COUNTED", NOT "ABSENT" (a past bug).
    # A group with a real bed count but no SF cannot be placed against any band, and it used
    # to contribute nothing at all — so every advertised card for its bed type failed
    # `_already_priced` and was appended WITH ITS OWN AVAILABILITY, on top of the listings it
    # had already counted. Priced (1bd, sf=None, 2) against a grid (1bd, 700sf, 2) stored
    # FOUR. That INFLATES, and `_refuse_impossible_availability` is blind to it while the
    # doubled count stays under `public_record_units`.
    reconcile: set[int] = set()
    for g in (card_groups if card_groups is not None else priced):
        if g.beds is None:
            # The `unknown` bucket belongs to no bed type, so it can claim no bed type's
            # cards; suppressing on it would zero every advertised plan on any site with one.
            continue
        if g.sf_min is None or g.sf_max is None:
            reconcile.add(g.beds)
            continue
        bands[g.beds].append((g.sf_min, g.sf_max))

    unpriced = []
    for p in advertised:
        if id(p) in covered or _already_priced(p, bands):
            continue
        p.rent_min = None
        p.rent_basis = "unspecified"
        unpriced.append(p)

    # 🔴 RECONCILE AT THE BED-TYPE TOTAL, NEVER PER CARD. Two narrower rules were tried and
    # both were wrong. Zeroing every card of the bed type traded the overcount for an
    # UNDERCOUNT — a group holding 2 units cannot be three different plans at once. Spending
    # the group's count down against the UNCOVERED cards then charged the same missing units
    # twice whenever a COVERED card had already been dropped in favour of a smaller priced
    # count: priced (700sf, 2) + (None, 2) against grid (700sf, 4) + (1200sf, 5) stored 7
    # where the site publishes 9.
    #
    # Missing SF gives no basis for deciding WHICH plan the no-SF units belong to, so no
    # per-card allocation can be justified. The bed type is instead landed on
    # max(what the priced feed observed, what the grid publishes) — never below either
    # source, so it can neither invent units nor discard observed ones — and the difference
    # is handed to the appended cards in grid order, each capped at its own published count.
    # WHICH card shows the remainder is a guess; the bed-type TOTAL is not.
    for beds in reconcile:
        observed = sum(g.available for g in priced if g.beds == beds)
        published = sum(p.available for p in advertised if p.beds == beds)
        cards = [p for p in unpriced if p.beds == beds]
        remaining = max(observed, published) - observed
        for p in cards:
            take = min(p.available, max(0, remaining))
            remaining -= take
            p.available = take
            p.coming_soon = 0

    merged = priced + unpriced
    merged.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return merged


def _already_priced(plan: UnitGroup, bands: dict[int, list[tuple[int, int]]]) -> bool:
    """True when `plan` is a priced group's own plan, seen again on the marketing grid.

    A bed type the priced feed never mentioned has no bands at all, so every one of its
    plans is new — the earlier behaviour, unchanged. A plan with no SF cannot be placed
    against a band; it is dropped rather than published as an unlocatable group, which is
    what the old bed-count rule did with it anyway.
    """
    if plan.beds is None or plan.sf_min is None:
        return True
    for lo, hi in bands.get(plan.beds, ()):
        if lo - SF_BAND <= plan.sf_min <= hi + SF_BAND:
            return True
    return False


def _safe_public_units(community: dict) -> int | None:
    """community['public_record_units'] as an int, or None. Seed rows sometimes carry '' or a str.

    Same helper as `sightmap._safe_public_units`; kept local rather than shared because the
    two adapters have no other coupling and six lines is cheaper than a new import edge.
    """
    v = (community or {}).get("public_record_units")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _refuse_impossible_availability(merged: list[UnitGroup],
                                    before: list[int],
                                    public_record_units: int | None,
                                    reasons: list[str]) -> list[UnitGroup]:
    """A community cannot have more units listed than it HAS. Revert if it claims to.

    🔴 THE FAILURE MODE THIS EXISTS FOR is the sightmap inflation shape — LAS Bria once
    reporting 256 available against 40 live, with flag_review false. Inventing an
    availability count is more dangerous than leaving one at zero, which is why this
    change was split out of an earlier change rather than written into it. If the grid-derived
    counts push a community past its own public_record_units, every one of them is wrong about
    something and the pre-2026-08-24 numbers are restored wholesale — a partial repair
    would just be a smaller guess.
    """
    if not public_record_units:
        return merged
    total = sum(g.available for g in merged)
    if total <= public_record_units:
        return merged
    for g, was in zip(merged, before):
        g.available = was
        g.flag_review = True
    reasons.append(
        f"advertised availability {total} exceeds public_record_units {public_record_units}; "
        f"grid counts refused, availability reverted"
    )
    return merged
