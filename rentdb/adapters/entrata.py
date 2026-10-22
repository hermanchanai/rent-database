"""Adapter for Entrata prospect-portal leasing sites (Greystar Summerwell,
RangeWater, and many others nationally).

The marketing domain (e.g. summerwellcedarcrest.com) is an Entrata website
template with NO rent on it — the availability data lives on the property's
prospect portal:

    https://my<name>.prospectportal.com/<city>/<slug>/availability/

That page renders one `.fp-card` per available floor plan, each carrying:
    heading            floor-plan name ("Sunshine")
    "N Bed / M Bath — X,XXX sq. ft"
    .pp-base-rent-amount   base rent ("$3,213/month")
    "Available <date>"
    Specials           concession text ("Lease Now and Get Two Months Free")

Everything is served as static HTML — curl_cffi reads it fine (headless
Playwright does NOT, which is why these sites failed the generic LLM tier).
Rent type: "base" (.pp-base-rent-amount is the base monthly rent).

National vendor: this same pattern serves Entrata leasing in every metro.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from . import cues
from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession

NAME = "entrata"
SF_BAND = 50
ENTRATA_SIGNALS = (
    "prospectportal.com",
    "commoncf.entrata.com",
    "entrata's accessibility statement",
)

_PP_LINK_RE = re.compile(r"https?://[a-z0-9-]+\.prospectportal\.com/[a-z0-9-]+/[a-z0-9-]+/", re.I)
_BBS_RE = re.compile(r"(\d+)\s*Bed\s*/\s*(\d+(?:\.\d+)?)\s*Bath.*?([\d,]{3,})\s*sq", re.I | re.S)
_RENT_RE = re.compile(r"\$([\d,]{3,})")

# --- bed / SF off one floor-plan card --------------------------------------
# `_BBS_RE` above needs a literal "N Bed", so every "Studio / 1 Bath" plan fell
# through to beds=None and the whole group went invisible downstream (PHX mf
# golden, Northbend, 2026-08-09). Read the two fields separately instead, and
# treat a studio as what it is: 0 beds.
_BEDS_TEXT_RE = re.compile(r"(\d+)\s*Bed", re.I)
# The card line is "N Bed / M Bath — X,XXX sq. ft", so the bath count has always been
# sitting in the same string beds is read from; nothing stored it until UnitGroup
# gained a baths field (2026-08-13).
_BATHS_TEXT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)
_STUDIO_RE = re.compile(r"\b(?:studio|efficiency)\b", re.I)
_SF_TEXT_RE = re.compile(r"([\d,]{3,})\s*\+?\s*sq", re.I)
# Entrata's own tracking blob on the "View Details" link — last-resort bed source
# for templates that render the bed/bath line differently: data-eptracking='{"bedroom":"Studio",...}'
_EPTRACK_BEDS_RE = re.compile(r'"bedroom"\s*:\s*"([^"]*)"', re.I)

# --- what the `.availability` span means -----------------------------------
# ORDER MATTERS: "Waitlist Available" contains "Available", so the not-bookable
# set has to be tested before the generic availability check.
_AVAIL_COUNT_RE = re.compile(r"(\d+)\s+Units?\s+Available", re.I)
_COMING_SOON_RE = re.compile(r"coming\s+soon", re.I)
_NOT_BOOKABLE_RE = re.compile(
    r"wait\s*-?\s*list|contact\s+us|call\s+for|inquire|check\s+back|"
    r"not\s+available|unavailable|sold\s+out|fully\s+leased|no\s+availab",
    re.I,
)
_AVAILABLE_RE = re.compile(r"\bavailab", re.I)
_MONTHS_FREE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)
_WEEKS_FREE_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)


def _concession(text: str) -> tuple[int | None, str | None]:
    """Page-level concession. Months first, which is the order both call sites read.

    a past bug: was two inline copies of the same regex pair, neither carrying the
    shared named-bed-type veto, so "8 weeks FREE! On One bedroom Unit..." stored 8
    weeks against every unit on the property.
    """
    return local_concession(text, _WEEKS_FREE_RE, _MONTHS_FREE_RE, months_first=True)


# The prospect-portal subdomain is the definitive Entrata signal (some templates
# don't emit the literal "entrata" string, e.g. RangeWater).
_PP_ANY_RE = re.compile(r"https?://([a-z0-9-]+)\.prospectportal\.com(/[a-z0-9-/]*)?", re.I)


def matches(community: dict) -> bool:
    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return False
    if "prospectportal.com" in url.lower():
        return True
    try:
        from rentdb.fetchers import fetch_static
        fr = fetch_static(url)
    except Exception:
        return False
    low = fr.html.lower()
    if fr.status >= 400:
        return False
    if any(signal in low for signal in ENTRATA_SIGNALS):
        return True
    return _is_razz_entrata(low)


# Razz Interactive builds Entrata-backed sites that emit NONE of ENTRATA_SIGNALS —
# no prospectportal host, no commoncf.entrata.com, no accessibility string. The
# inventory is server-rendered into a Vike SSR blob instead. `_parse_razz_inventory`
# below has handled exactly that shape since the Austin wave, but nothing ever routed
# to it: matches() declined the row, every later adapter declined it too, and it fell
# through the LLM tail into needs_adapter. 15 of the 86 unparseable Tampa/Nashville
# communities were this, and the existing parser returned real groups and rents on
# 15/15 when called directly (triage 2026-08-20).
#
# BOTH markers are required. `vike_pageContext` is a Vike/Vite SSR marker, not an
# Entrata one — matching on it alone would hand entrata every Vike-built site on the
# fleet, and a wrong adapter claiming a row is worse than no adapter, because the row
# stops falling through to the LLM tail that would otherwise have read it.
_RAZZ_MARKER = 'id="vike_pagecontext"'
_RAZZ_INVENTORY = '"$inventory"'


def _is_razz_entrata(low_html: str) -> bool:
    return _RAZZ_MARKER in low_html and _RAZZ_INVENTORY in low_html


_CONVENTIONAL_SEG_RE = re.compile(r"/conventional(?:/|$)", re.I)


def _candidate_availability_urls(community: dict) -> list[str]:
    """Entrata portals come in two path shapes:
      1. https://<sub>.prospectportal.com/<city>/<slug>/availability/   (Summerwell)
      2. https://<sub>.prospectportal.com/availability/                 (RangeWater)
    Return candidate availability URLs to try in order; the caller keeps the first
    that yields floor-plan cards.
    """
    url = (community.get("url") or "").strip()
    html = ""
    # A tracked URL that already IS a floor-plan page (HOU Hanover Montrose's
    # .../houston/hanover-montrose/conventional/) is the first candidate.
    own = [url] if url.startswith("http") and _is_listing_page(url) else []
    if "prospectportal.com" not in url.lower():
        try:
            from rentdb.fetchers import fetch_static
            html = fetch_static(url).html
        except Exception:
            return own
        m = _PP_LINK_RE.search(html)
        if m:
            content = m.group(0)  # .../<city>/<slug>/
        else:
            content = None
    else:
        content = url.rstrip("/") + "/"

    cands: list[str] = []
    if content:
        base = content.rstrip("/")
        if base.endswith("availability"):
            cands.append(content)
        else:
            cands.append(base + "/availability/")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for link in soup.find_all("a", href=True):
            href = link["href"]
            # A path SEGMENT, trailing slash optional (Modera Walsh Heights links
            # `/aledo/modera-walsh-heights/conventional`); `/conventional-x` is not one.
            if _CONVENTIONAL_SEG_RE.search(urlparse(urljoin(url, href)).path or ""):
                u = urljoin(url, href)
                if u not in cands:
                    cands.append(u)
    # Root-of-subdomain fallback (shape 2), derived from any prospectportal link.
    src = html or url
    ms = _PP_ANY_RE.search(src)
    if ms:
        sub = ms.group(1)
        root = f"https://{sub}.prospectportal.com"
        for ep in ("/availability/", "/floorplans/"):
            u = root + ep
            if u not in cands:
                cands.append(u)
    return own + [c for c in cands if c not in own]


# A floor-plan page's own path. `/<city>/<slug>/conventional/` is where the newer
# portals (availability.<domain>, <sub>.prospectportal.com) list plans; the older
# `/availability/` and `/Apartments/module/availability/` paths on those portals now
# redirect to the portal ROOT, which lists nothing (2026-09-25, 8 survey comps).
_LISTING_PATH_RE = re.compile(r"/(?:conventional|availability|floorplans?)(?:/|$)", re.I)
# Any portal link that names the property's /<city>/<slug>/ pair. The portal root
# never links /conventional/ on every template, but it always links the map or the
# contact card under the same pair.
_PORTAL_PAIR_RE = re.compile(
    r"^/([a-z0-9-]+)/([a-z0-9-]+)/(?:conventional|map-and-directions|guest-card|"
    r"photos|amenities|floorplans?)(?:/|$)", re.I)
_PORTAL_NON_PAIR = {"apartments", "module", "terms-of-use", "privacy-policy"}
# The plan container every Entrata floor-plan template renders, even with no plan in
# it. Its presence is what makes an empty page mean "no availability".
_PLAN_CONTAINER_SEL = (".fp-overview, .fp-group-list, #floorplan-overview-content, "
                       ".fp-engrain-container, #available-units, .fp-container")


def _is_listing_page(final_url: str) -> bool:
    """Did this fetch land on a floor-plan page, rather than a portal/marketing root?"""
    from urllib.parse import urlparse
    path = urlparse(final_url).path or "/"
    return path.rstrip("/") != "" and bool(_LISTING_PATH_RE.search(path))


def _portal_conventional_urls(html: str, base_url: str) -> list[str]:
    """`<host>/<city>/<slug>/conventional/` for every /<city>/<slug>/ pair a portal page links."""
    from urllib.parse import urlparse
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    # <form action> too: ATL Skymor Buford's portal root names its pair ONLY there.
    targets = [a["href"] for a in soup.find_all("a", href=True)]
    targets += [f["action"] for f in soup.find_all("form", action=True)]
    for target in targets:
        u = urlparse(urljoin(base_url, target))
        m = _PORTAL_PAIR_RE.match(u.path or "")
        if not m or m.group(1).lower() in _PORTAL_NON_PAIR:
            continue
        cand = f"{u.scheme}://{u.netloc}/{m.group(1)}/{m.group(2)}/conventional/"
        if cand not in out:
            out.append(cand)
    return out


def _int(s: str | None) -> int | None:
    if not s:
        return None
    m = re.search(r"[\d,]+", s)
    return int(m.group(0).replace(",", "")) if m else None


def _band(sqft: int | None) -> int | None:
    return round(sqft / SF_BAND) * SF_BAND if sqft else None


def _scope_cards(cards, scope: str) -> list:
    """The `.fp-card`s whose printed title (`.fp-title`) matches the manifest's plan_scope
    (one portal leasing a BFR village and an MF building). Case-insensitive search."""
    rx = re.compile(scope, re.I)
    kept = []
    for card in cards:
        title = card.select_one(".fp-title")
        if title is not None and rx.search(title.get_text(" ", strip=True)):
            kept.append(card)
    return kept


def extract(community: dict) -> ExtractResult:
    url = (community.get("url") or "").strip()
    # a manifest plan_scope keeps this community to its own plans on a portal shared
    # with another product. Only the `.fp-card` grid carries a per-plan title to scope by;
    # every other path refuses rather than store the whole page.
    scope = (community.get("plan_scope") or "").strip() or None
    # Pages that answered but list no plans — a portal or marketing root. Their links
    # name the property's /<city>/<slug>/ pair, which is where the plans actually are.
    roots: list[tuple[str, str]] = []
    if url:
        try:
            from rentdb.fetchers import fetch_static
            homepage = fetch_static(url)
            razz = _parse_razz_inventory(homepage.html)
            if razz is not None:
                if scope:
                    return ExtractResult(scrape_error="entrata: plan_scope set but the razz inventory "
                                                      "has no plan card to scope by",
                                         extractor_used=NAME, flag_review=True)
                return razz
            if homepage.status < 400:
                roots.append((homepage.html, homepage.final_url or url))
        except Exception:
            pass

    cands = _candidate_availability_urls(community)
    if not cands:
        return ExtractResult(scrape_error="entrata: could not resolve prospectportal availability url",
                             extractor_used=NAME, flag_review=True)
    st = {"last_err": None, "reached": False, "listing": False}
    soup, cards, items = _first_plan_page(cands, st, roots)
    if not (cards or items):
        # Second pass, only when the first found nothing: the portal's
        # /<city>/<slug>/conventional/ page (Story Riverside, Mabry, Margot, Heartwood,
        # Everstead, Hardy Springs — 2026-09-25). Costs no fetch on a healthy site.
        extra: list[str] = []
        for html, base in roots:
            extra += [u for u in _portal_conventional_urls(html, base)
                      if u not in cands and u not in extra]
        st["last_err"] = None if st["reached"] else st["last_err"]
        soup, cards, items = _first_plan_page(extra, st, [])

    if not (cards or items):
        if st["listing"]:
            # A floor-plan page answered and listed no plan: that IS no availability.
            return ExtractResult(units=[], extractor_used=NAME)
        # Never saying no_availability off a page that is not a floor-plan page: a
        # portal root lists nothing whether or not the property has homes to lease.
        # A fetch error counts only if nothing answered at all, or if it came from the
        # portal's own /conventional/ page — a stale /floorplans/ 404 on a live portal
        # is not a dead site.
        if st["last_err"] or not st["reached"]:
            return ExtractResult(
                scrape_error=f"entrata: availability fetch failed: {st['last_err']}"[:200],
                extractor_used=NAME, flag_review=True)
        return ExtractResult(
            scrape_error="entrata: reached the portal but no floor-plan page",
            extractor_used=NAME, flag_review=True)

    if scope:
        if not cards:
            return ExtractResult(scrape_error="entrata: plan_scope set but this template has no "
                                              "plan card to scope by",
                                 extractor_used=NAME, flag_review=True)
        kept = _scope_cards(cards, scope)
        if not kept:
            # A renamed plan is not an empty community: never no_availability.
            return ExtractResult(scrape_error=f"entrata: plan_scope {scope!r} matched none of "
                                              f"{len(cards)} plan cards"[:200],
                                 extractor_used=NAME, flag_review=True)
        # The concession is read off the KEPT cards only -- the page around them speaks for
        # every product on the portal.
        conc_weeks, raw_conc = _concession(" ".join(c.get_text(" ", strip=True) for c in kept))
        return ExtractResult(units=_parse_fp_cards(kept), concession_weeks=conc_weeks,
                             raw_concession_text=raw_conc, extractor_used=NAME,
                             plan_scope_applied=True)

    # concession is usually property-wide; take the strongest signal on the page
    page_text = soup.get_text(" ", strip=True)
    conc_weeks, raw_conc = _concession(page_text)

    if cards:
        units = _parse_fp_cards(cards)
    elif isinstance(items[0], dict):
        units = _parse_embedded_units(items)
    elif "floorplan-item" in (items[0].get("class") or []):
        units = _parse_floorplan_items(items)
    elif "unit-item" in (items[0].get("class") or []):
        units = _parse_unit_items(items)
    else:
        units = _parse_fp_items(items)
    return ExtractResult(units=units, concession_weeks=conc_weeks, raw_concession_text=raw_conc, extractor_used=NAME)


def _first_plan_page(cands: list[str], st: dict, roots: list[tuple[str, str]]):
    """(soup, .fp-card cards, li plan items) off the first candidate that lists plans.

    `st` accumulates across calls: `reached` (any 2xx), `listing` (a 2xx that landed
    on a floor-plan path), `last_err`. A 2xx that is NOT a floor-plan page is appended
    to `roots` so the caller can read the /<city>/<slug>/ pair off its links.
    """
    # fetch_static, not a bare one-profile curl_cffi GET: Entrata's Cloudflare answers
    # 403/429 to one TLS profile and 200 to the next often enough to matter (3 of 8
    # survey comps on one 2026-09-25 pass), and it throttles per host.
    from rentdb.fetchers import fetch_static
    for avail in cands:
        try:
            resp = fetch_static(avail)
        except Exception as e:
            st["last_err"] = f"{type(e).__name__}: {e}"
            continue
        if resp.status >= 400:
            st["last_err"] = f"HTTP {resp.status}"
            continue
        st["reached"] = True
        final = resp.final_url or avail
        soup = BeautifulSoup(resp.html, "html.parser")
        cards = soup.select(".fp-card")
        items = [] if cards else (soup.select("li.fp-group-item, li.fp-grid-item")
                                  or soup.select(_CONDENSED_ROW_SEL)
                                  or soup.select("#available-units li.unit-item")
                                  or soup.select("#see-all-floor-plans li.floorplan-item"))
        if not (cards or items):
            items = _embedded_property_units(resp.html)
        if cards or items:
            return soup, cards, items
        # A floor-plan PATH is not enough: a Cloudflare challenge can answer 200 there.
        # It must also carry the template's own plan container, empty.
        if _is_listing_page(final) and soup.select_one(_PLAN_CONTAINER_SEL) is not None:
            st["listing"] = True
        else:
            roots.append((resp.html, final))
    return None, [], []


def _card_status_text(card) -> str:
    """The text that says whether this plan can actually be leased.

    Prefer the portal's own `.availability` span. Falling back to the whole card
    means reading the Specials block too, where a "call for details" would wrongly
    zero the plan — so drop that block first.
    """
    el = card.select_one(".availability")
    if el is not None:
        return el.get_text(" ", strip=True)
    clone = BeautifulSoup(str(card), "html.parser")
    for special in clone.select(".fp-special-content, .fp-special-details"):
        special.decompose()
    return clone.get_text(" ", strip=True)


def _card_availability(card) -> tuple[int, int]:
    """(available, coming_soon) for one floor-plan card.

    A plan with nothing reservable is NOT availability (SOP step 6: "Call for
    Pricing" / "Contact Us" is no availability, not future availability). The old
    code matched a bare `\\bavailable\\b`, so "Waitlist Available" scored 1 — which
    is what reported 25 units on a site listing 14, and let a waitlist plan's
    indicative rent become the group's rent_max.
    """
    txt = _card_status_text(card)
    if _COMING_SOON_RE.search(txt):
        return 0, 1
    if _NOT_BOOKABLE_RE.search(txt):
        return 0, 0
    m = _AVAIL_COUNT_RE.search(txt)
    if m:
        return int(m.group(1)), 0
    return (1, 0) if _AVAILABLE_RE.search(txt) else (0, 0)


def _card_beds_baths_sf(card) -> tuple[int | None, float | None, int | None]:
    """(beds, baths, sqft) for one floor-plan card. "Studio"/"Efficiency" is 0 beds."""
    el = card.select_one(".fp-details .dynamic-text") or card.select_one(".dynamic-text")
    txt = el.get_text(" ", strip=True) if el is not None else card.get_text(" ", strip=True)

    beds = None
    m = _BEDS_TEXT_RE.search(txt)
    if m:
        beds = int(m.group(1))
    elif _STUDIO_RE.search(txt):
        beds = 0
    else:
        link = card.select_one("[data-eptracking]")
        if link is not None:
            me = _EPTRACK_BEDS_RE.search(link.get("data-eptracking") or "")
            if me:
                raw = me.group(1).strip()
                if raw.isdigit():
                    beds = int(raw)
                elif _STUDIO_RE.search(raw):
                    beds = 0

    mb = _BATHS_TEXT_RE.search(txt)
    baths = None
    if mb:
        try:
            baths = float(mb.group(1))
        except ValueError:
            baths = None

    ms = _SF_TEXT_RE.search(txt)
    return beds, baths, _int(ms.group(1)) if ms else None


_CARD_DOLLAR_RE = re.compile(r"\$\s*([\d,]+)")


def _card_rents(card) -> tuple[int | None, int | None]:
    """(base rent, all-in total) for one `.fp-card`.

    NEVER one grouped selector: `select_one(".pp-base-rent-amount, .fee-transparency-text")`
    returns whichever comes first in the document, and on the fee-transparency card the
    outer `.fee-transparency-text` ("Total Monthly Leasing Price Starting from $2,987")
    wraps the base amount ("$2,802+/month") — so the TOTAL was stored as base rent, $185
    high on every priced plan at ATL Skymor Buford (2026-09-25). The base element wins;
    the wrapper's own first figure, when it differs, is the all-in total. A card with
    only the one price element keeps reading it as the rent, with no all-in.
    """
    base_el = card.select_one(".pp-base-rent-amount")
    if base_el is not None:
        base = _int(base_el.get_text())
        wrap = card.select_one(".fee-transparency-text")
        m = _CARD_DOLLAR_RE.search(wrap.get_text(" ", strip=True)) if wrap is not None else None
        total = int(m.group(1).replace(",", "")) if m else None
        return base, (total if total and total != base else None)
    el = card.select_one(".fee-transparency-text")
    return (_int(el.get_text()) if el is not None else None), None


def _parse_fp_cards(cards) -> list[UnitGroup]:
    """Group a prospect-portal availability page's `.fp-card` list into UnitGroups.

    SF comes from EVERY advertised card, availability and rent only from the
    bookable ones — that split is the golden's own convention: Northbend's 3BR/4BR
    are advertised (so the bed type and its SF range survive for recall) but every
    plan is waitlist, so they carry available=0 and null rents rather than the
    indicative price the portal shows behind the waitlist.
    """
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for card in cards:
        beds, baths, sqft = _card_beds_baths_sf(card)
        # Rent = the base-rent element ONLY. Do not regex $-tokens off the card text:
        # those include the security deposit ("$1,500") and total-leasing-price, which
        # would corrupt the rent. A card with no base-rent element gets null rent.
        rent, allin = _card_rents(card)
        available, soon = _card_availability(card)
        groups[(beds, _band(sqft))].append({"rent": rent, "allin": allin, "sqft": sqft, "avail": available,
                                            "soon": soon, "baths": baths,
                                            "cue_text": card.get_text(" ", strip=True)})

    units: list[UnitGroup] = []
    for (beds, _sf_band), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)):
        # Only a bookable plan's rent is a rent. A waitlist/"Contact Us" price is
        # not obtainable by any renter and must never reach v_latest_unit_groups.
        rents = [m["rent"] for m in members if m["rent"] and (m["avail"] or m["soon"])]
        allins = [m["allin"] for m in members if m["allin"] and m["rent"] and (m["avail"] or m["soon"])]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        available = sum(m["avail"] for m in members)
        coming_soon = sum(m["soon"] for m in members)
        cue_text = " ".join(m.get("cue_text") or "" for m in members)
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=available,
            coming_soon=coming_soon,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_allin_min=min(allins) if allins else None,
            rent_allin_max=max(allins) if allins else None,
            # One `rents` entry per bookable CARD, so this list is already per-unit.
            unit_rents=observed_rents(rents),
            structure_type=cues.structure_type_from_text(cue_text),
            stories=cues.stories_from_text(cue_text),
            garage=cues.garage_from_text(cue_text),
            baths=observed_baths(m.get("baths") for m in members),
            # Advertised only — nothing listed to verify. Same convention as
            # sightmap._add_advertised_layouts (G0.3, 2026-07-15).
            flag_review=not rents and not available and not coming_soon,
        ))
    return units


def _parse_razz_inventory(html: str) -> ExtractResult | None:
    """Parse Entrata inventory embedded by Razz Interactive sites."""
    soup = BeautifulSoup(html, "html.parser")
    payload = soup.find("script", id="vike_pageContext")
    if payload is None or not payload.string:
        return None
    try:
        data = json.loads(payload.string)
        inventory = data["initialStoreState"]["$inventory"]
        models = {str(m["id"]): m for m in inventory.get("models", [])}
        source_units = inventory.get("units", [])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None

    by_model: dict[str, list[dict]] = {}
    for unit in source_units:
        available_raw = unit.get("available")
        if not available_raw:
            continue
        by_model.setdefault(str(unit.get("model_id")), []).append(unit)

    units: list[UnitGroup] = []
    for model_id, members in by_model.items():
        model = models.get(model_id, {})
        # Listed = available (2026-07-14): every unit in the inventory feed
        # with an `available` value is an on-market listing — a future move-in
        # date does not make it "coming soon" in the site's own UI.
        available = len(members)
        coming_soon = 0
        rents: list[int] = []
        sfs: list[int] = []
        # ONE entry per member, for unit_rents. `rents` above flattens each unit's
        # {min,max} lease-term pair into two entries — right for the endpoints,
        # wrong as a per-unit list (it would double-count every unit whose min
        # equals its max, which is most of them). A unit priced across lease terms
        # contributes its LOW end, the same end the market-rent rule reads.
        per_unit: list[int] = []
        for member in members:
            rent = member.get("rent") or {}
            for value in (rent.get("min"), rent.get("max")):
                if value:
                    rents.append(int(round(float(value))))
            unit_rent = rent.get("min") or rent.get("max")
            if unit_rent:
                per_unit.append(int(round(float(unit_rent))))
            sqft = member.get("sqft") or {}
            for value in (sqft.get("min"), sqft.get("max")):
                if value:
                    sfs.append(int(round(float(value))))

        if not rents:
            model_rent = model.get("rent") or {}
            rents = [
                int(round(float(v)))
                for v in (model_rent.get("min"), model_rent.get("max"))
                if v
            ]
            # This fallback is the MODEL's advertised range, not observed units —
            # per_unit stays empty so the group reports None rather than laundering
            # a plan-level range into two fake unit observations.
        if not sfs:
            model_sqft = model.get("sqft") or {}
            sfs = [
                int(round(float(v)))
                for v in (model_sqft.get("min"), model_sqft.get("max"))
                if v
            ]
        beds = model.get("beds")
        units.append(
            UnitGroup(
                beds=int(beds) if beds is not None else None,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=available,
                coming_soon=coming_soon,
                rent_min=min(rents) if rents else None,
                rent_max=max(rents) if rents else None,
                rent_type="base",
                unit_rents=observed_rents(per_unit),
                flag_review=not rents,
            )
        )

    page_text = soup.get_text(" ", strip=True)
    conc_weeks, raw_conc = _concession(page_text)
    return ExtractResult(
        units=units,
        concession_weeks=conc_weeks,
        raw_concession_text=raw_conc,
        extractor_used=NAME,
        flag_review=any(u.flag_review for u in units),
    )


# --- `li.fp-group-item` (list) / `li.fp-grid-item` (grid) plan templates ----------
# Added 2026-09-25 (Marlowe Centennial Hills, Amber Vista, Hardy Springs). One <li>
# per floor plan: `.fp-name`, `.bed-bath` "1 bd / 1.5 ba", `.sq-feet` "Sq. Ft 1,066",
# `.pp-base-rent-amount` "$1,899+/month" (or "—" when the plan has nothing to lease),
# and the count either in `.available-units` "4 Available" (grid) or in the action
# button's aria-label "2 Available in A1" / "Only One Left in B1" / "Get Notified of
# A2" (list). The "+" marks the plan's FLOOR, not one unit's rent.
_ITEM_BEDS_RE = re.compile(r"(\d+)\s*(?:bd|bed)", re.I)
_ITEM_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ba\b|bath)", re.I)
_ITEM_COUNT_RE = re.compile(r"(\d+)\s+(?:Units?\s+)?Available", re.I)
_ITEM_ONE_LEFT_RE = re.compile(r"\bonly\s+one\s+left\b", re.I)
_ITEM_RENT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)(\s*\+)?")


# `.fp-overview.condensed` (2026-10-05: Avana River Park, Town Creek, Lakewood, Avana
# Collins Creek / Lexington / City Park / South Oaks, Pines of Woodforest): a TABLE, one
# `li.fp-row` per plan under `ul.fp-group-list` after a `li.fp-row.head` header row, with the
# same `.bed-bath` / `.sq-feet` / `.fp-col.rent` columns as the list template and the status
# on a `button.availability-link` ("2 Available", "Only One Left!", "Available Oct 13, 2026",
# "Get Notified") -- rendered twice per row (mobile + desktop), identical. Read by
# `_parse_fp_items`. Before, the page had the plan container and no plan this adapter knew,
# which it stored as an affirmed no_availability.
_CONDENSED_ROW_SEL = ".fp-group-list li.fp-row:not(.head)"
# "From $1,293 per month" (Town Creek) is a plan floor, like the list template's "+".
_ITEM_FROM_RE = re.compile(r"^\s*(?:from|starting\s+(?:at|from))\b", re.I)


def _item_availability(item) -> tuple[int, int]:
    """(available, coming_soon) for one plan <li>.

    A count ("2 Available in A1") wins; "Only One Left" is 1; an uncounted "Available
    Oct 06, 2026" / "Available Now" button is ONE listed home (Amber Vista — listed =
    available, a future date included). "Wait List" / "Get Notified" / "Contact Us" = 0.
    """
    parts = [el.get_text(" ", strip=True) for el in item.select(".available-units")]
    for b in item.select("button.primary-action, button.availability-link"):
        parts += [b.get("aria-label") or "", b.get_text(" ", strip=True)]
    # " | ", not " ": a dated button followed by the next part ("Available Oct 13, 2026" +
    # "Available Oct 13, 2026", the condensed row's mobile + desktop copies) must not read as
    # "2026 Available" -- a count of 2,026.
    txt = " | ".join(dict.fromkeys(p for p in parts if p))
    if _COMING_SOON_RE.search(txt):
        return 0, 1
    m = _ITEM_COUNT_RE.search(txt)
    if m:
        return int(m.group(1)), 0
    if _ITEM_ONE_LEFT_RE.search(txt):
        return 1, 0
    if _NOT_BOOKABLE_RE.search(txt):
        return 0, 0
    return (1, 0) if _AVAILABLE_RE.search(txt) else (0, 0)


def _item_rent_el(item):
    """The plan's BASE rent element. `.pp-base-rent-amount` where the template has one;
    otherwise the `.fp-col.rent` column, but only when that column is titled plain
    "Rent" — on the base/total templates the same column's `.fee-transparency-text` is
    the TOTAL monthly leasing price, which is not a base rent."""
    el = item.select_one(".pp-base-rent-amount")
    if el is not None:
        return el
    col = item.select_one(".fp-col.rent")
    title = col.select_one(".fp-col-title") if col is not None else None
    # "Rent" or "Rent*" (condensed,) -- never "Base Rent*" / "Total ...". And never a
    # figure the column itself calls the total ("Total Monthly Leasing Price From $1,133", the
    # grid template): the house basis is BASE.
    if title is not None and title.get_text(" ", strip=True).rstrip("* ").lower() == "rent":
        el = col.select_one(".fee-transparency-text")
        if el is not None and "total" in el.get_text(" ", strip=True).lower():
            return None
        return el
    return None


def _parse_fp_items(items) -> list[UnitGroup]:
    """Group plan <li>s into UnitGroups on the same (beds, 50-sf band) key as cards.

    `unit_rents` stays None: a plan row publishes its floor ("$1,409+") and a count,
    never the members, and the project rules forbids synthesizing the list from rent_min.
    Only a leasable plan's price is a rent — a "Get Notified" plan keeps its SF and bed
    type (recall) with null rent, the `.fp-card` convention.
    """
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for item in items:
        bb_el = item.select_one(".bed-bath")
        bb = bb_el.get_text(" ", strip=True) if bb_el is not None else item.get_text(" ", strip=True)
        mb = _ITEM_BEDS_RE.search(bb)
        beds = int(mb.group(1)) if mb else (0 if _STUDIO_RE.search(bb) else None)
        mba = _ITEM_BATHS_RE.search(bb)
        baths = float(mba.group(1)) if mba else None
        sf_el = item.select_one(".sq-feet")
        sqft = _int(sf_el.get_text(" ", strip=True)) if sf_el is not None else None
        rent, floor = None, False
        rent_el = _item_rent_el(item)
        if rent_el is not None:
            rent_txt = rent_el.get_text(" ", strip=True)
            mr = _ITEM_RENT_RE.search(rent_txt)
            if mr:
                rent = int(round(float(mr.group(1).replace(",", ""))))
                floor = bool(mr.group(2)) or bool(_ITEM_FROM_RE.search(rent_txt))
        available, soon = _item_availability(item)
        groups[(beds, _band(sqft))].append({"rent": rent, "floor": floor, "sqft": sqft,
                                            "avail": available, "soon": soon, "baths": baths,
                                            "cue_text": item.get_text(" ", strip=True)})

    units: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)):
        priced = [m for m in members if m["rent"] and (m["avail"] or m["soon"])]
        rents = [m["rent"] for m in priced]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        available = sum(m["avail"] for m in members)
        coming_soon = sum(m["soon"] for m in members)
        cue_text = " ".join(m["cue_text"] for m in members)
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=available,
            coming_soon=coming_soon,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis=("starting_at" if any(m["floor"] for m in priced)
                        else "actual" if rents else "unspecified"),
            unit_rents=None,
            structure_type=cues.structure_type_from_text(cue_text),
            stories=cues.stories_from_text(cue_text),
            garage=cues.garage_from_text(cue_text),
            baths=observed_baths(m.get("baths") for m in members),
            flag_review=not rents and not available and not coming_soon,
        ))
    return units


# --- `#available-units li.unit-item` (Engrain "apex" unit list) --------------------
# Added 2026-09-25 (CHI Enclave at 127th). One <li> per UNIT: `.unit-bed-bath`
# "1 Bed, 1 Bath" / "Studio, 1 Bath", `.unit-sq-feet` "854 SqFt", `.unit-price`
# "Starting at $1,930" (the unit's price at its best lease term), `.unit-availability`
# "Available Now" / "Available 10/13/2026". Unit NUMBERS repeat across buildings; the
# `unit-item-<id>` class is the unit's identity. Per-unit, so unit_rents is observed.
_UNIT_ID_RE = re.compile(r"^unit-item-(\d+)$")


def _parse_unit_items(items) -> list[UnitGroup]:
    from collections import defaultdict
    seen: set[str] = set()
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        uid = next((m.group(1) for c in (it.get("class") or []) for m in [_UNIT_ID_RE.match(c)] if m), None)
        if uid is not None:
            if uid in seen:
                continue
            seen.add(uid)
        bb_el = it.select_one(".unit-bed-bath")
        bb = bb_el.get_text(" ", strip=True) if bb_el is not None else ""
        mb = _ITEM_BEDS_RE.search(bb)
        beds = int(mb.group(1)) if mb else (0 if _STUDIO_RE.search(bb) else None)
        mba = _ITEM_BATHS_RE.search(bb)
        sf_el = it.select_one(".unit-sq-feet")
        price_el = it.select_one(".unit-price")
        mr = _ITEM_RENT_RE.search(price_el.get_text(" ", strip=True)) if price_el is not None else None
        av_el = it.select_one(".unit-availability")
        av = av_el.get_text(" ", strip=True) if av_el is not None else ""
        soon = bool(_COMING_SOON_RE.search(av))
        if _NOT_BOOKABLE_RE.search(av) and not soon:
            continue  # a waitlisted unit is not a listing
        sqft = _int(sf_el.get_text(" ", strip=True)) if sf_el is not None else None
        groups[(beds, _band(sqft))].append({
            "rent": int(round(float(mr.group(1).replace(",", "")))) if mr else None,
            "floor": bool(price_el is not None and re.search(r"starting|from", price_el.get_text(), re.I)),
            "sqft": sqft, "soon": soon, "baths": float(mba.group(1)) if mba else None,
            "cue_text": it.get_text(" ", strip=True)})

    units: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                      kv[0][1] or 0)):
        rents = [m["rent"] for m in members if m["rent"] and m["rent"] > 0]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        cue_text = " ".join(m["cue_text"] for m in members)
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=sum(1 for m in members if not m["soon"]),
            coming_soon=sum(1 for m in members if m["soon"]),
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis=("starting_at" if any(m["floor"] for m in members) else "actual") if rents else "unspecified",
            unit_rents=observed_rents(rents),
            structure_type=cues.structure_type_from_text(cue_text),
            stories=cues.stories_from_text(cue_text),
            garage=cues.garage_from_text(cue_text),
            baths=observed_baths(m["baths"] for m in members),
            flag_review=not rents,
        ))
    return units


# --- `#see-all-floor-plans li.floorplan-item` (apex template, plan list only) --------
# Added 2026-09-25 (ATL Hanover Midtown, PHX Rise at Midtown — fleet A/B sample). Same
# template as Enclave at 127th, but the page carries ONLY the plan list: `.unit-bed-bath`
# "1 Bed, 1 Bath, 776 SqFt", `.unit-floor-plan-count` "5 Available" / "Only One Left",
# "Starting from $2,117/month"; "Get Notified" on a plan with nothing to lease. The
# price is the plan's floor, so unit_rents stays None.
_PLAN_SF_RE = re.compile(r"([\d,]{3,})\+?\s*(?:&nbsp;?|\xa0)?\s*SqFt", re.I)


def _parse_floorplan_items(items) -> list[UnitGroup]:
    from collections import defaultdict
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for it in items:
        bb_el = it.select_one(".unit-bed-bath")
        bb = bb_el.get_text(" ", strip=True) if bb_el is not None else ""
        mb = _ITEM_BEDS_RE.search(bb)
        beds = int(mb.group(1)) if mb else (0 if _STUDIO_RE.search(bb) else None)
        mba = _ITEM_BATHS_RE.search(bb)
        msf = _PLAN_SF_RE.search(bb)
        sqft = int(msf.group(1).replace(",", "")) if msf else None
        cnt_el = it.select_one(".unit-floor-plan-count")
        cnt_txt = cnt_el.get_text(" ", strip=True) if cnt_el is not None else ""
        mc = _ITEM_COUNT_RE.search(cnt_txt)
        available = int(mc.group(1)) if mc else (1 if _ITEM_ONE_LEFT_RE.search(cnt_txt) else 0)
        rent, _allin = _card_rents(it) if it.select_one(".pp-base-rent-amount, .fee-transparency-text") else (None, None)
        groups[(beds, _band(sqft))].append({"rent": rent, "sqft": sqft, "avail": available,
                                            "baths": float(mba.group(1)) if mba else None,
                                            "cue_text": it.get_text(" ", strip=True)})
    units: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                      kv[0][1] or 0)):
        rents = [m["rent"] for m in members if m["rent"] and m["avail"]]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        available = sum(m["avail"] for m in members)
        cue_text = " ".join(m["cue_text"] for m in members)
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=available,
            coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="starting_at" if rents else "unspecified",
            unit_rents=None,
            structure_type=cues.structure_type_from_text(cue_text),
            stories=cues.stories_from_text(cue_text),
            garage=cues.garage_from_text(cue_text),
            baths=observed_baths(m["baths"] for m in members),
            flag_review=not rents and not available,
        ))
    return units


# --- `"floorplanUnits":{"propertyUnits":[...]}` (script-rendered template) --------------
# Added 2026-09-25 (CHI Seasons at Plainfield). The floor-plan page has no plan markup and
# no "$" in its HTML; the units ride in an embedded JSON blob: {bedroom, sqft, availableOn,
# minAdvertisedBaseRent, maxAdvertisedBaseRent, unitspaceId, ...}. min..max spans LEASE
# TERMS, so the unit's rent is the min (the OLE/razz low-end convention). Per unit, so
# unit_rents is observed.
_PROPERTY_UNITS_RE = re.compile(r'"propertyUnits"\s*:\s*\[')


def _embedded_property_units(html: str) -> list[dict]:
    out: dict = {}
    dec = json.JSONDecoder()
    for m in _PROPERTY_UNITS_RE.finditer(html or ""):
        try:
            arr, _end = dec.raw_decode(html, m.end() - 1)
        except ValueError:
            continue
        for u in arr if isinstance(arr, list) else []:
            if isinstance(u, dict):
                out[u.get("unitspaceId") or u.get("id") or id(u)] = u
    return list(out.values())


def _parse_embedded_units(units: list[dict]) -> list[UnitGroup]:
    from collections import defaultdict

    def num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for u in units:
        beds = num(u.get("bedroom"))
        sqft = num(u.get("sqft"))
        rent = num(u.get("minAdvertisedBaseRent")) or num(u.get("minRent"))
        groups[(int(beds) if beds is not None else None, _band(int(sqft)) if sqft else None)].append(
            {"rent": int(round(rent)) if rent and rent > 0 else None,
             "sqft": int(sqft) if sqft else None, "baths": num(u.get("bathroom"))})
    out: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                      kv[0][1] or 0)):
        rents = [m["rent"] for m in members if m["rent"]]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        out.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=len(members),
            coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths(m["baths"] for m in members),
            flag_review=not rents,
        ))
    return out
