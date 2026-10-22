"""Adapter for sites that embed Engrain SightMap (sightmap.com/embed/<id>).

This is a *vendor-level* adapter, not an operator-level one — it works on any
community page that embeds SightMap, regardless of operator. Per scout 2026-04
+ 2026-05-17 recon, 33 of 120 ATL communities embed SightMap (27.5%), including
all 5 Quinn Residences + all 5 Greystar Summerwell + 6 Covey Homes + others.

Three-hop flow:
  1. Fetch community URL (static HTML via curl_cffi).
  2. Find sightmap.com/embed/<embed_id> reference.
  3. Fetch the SightMap embed page; pull the API URL from window.__APP_CONFIG__.
  4. Fetch the API (returns JSON with floor_plans + units).

Concession (page-level): same regex pattern as ARK adapter — looks for
"<N> weeks free" or "<N> months free" in the COMMUNITY page text (not the
SightMap API; concession info lives on the parent site).

Rent type: SightMap exposes `price` (base) and `total_price` (all-in inc fees)
separately when the community configures fees. We populate both rent_min/max
(base) and rent_allin_min/max (when total differs from price).

Availability: listed = available (decision 2026-07-14). Any unit carrying a
price or a move-in date is ON THE MARKET and counts as available — site UIs
count these units as available regardless of how far out the date is. Units
with neither price nor date are building inventory ("unlisted"), not counted.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from . import cues
from .base import (ExtractResult, UnitGroup, local_concession,
                   observed_baths, observed_plan_name,
                   observed_rents)

NAME = "sightmap"

# Find embed URLs in arbitrary HTML. Patterns seen:
#   <iframe src="https://sightmap.com/embed/<id>" ...>
#   <iframe src="https://sightmap.com/embed/<id>?enable_api=1&origin=...">
#   data-src="https://sightmap.com/embed/<id>?..."
EMBED_RE = re.compile(
    r"https?://sightmap\.com/embed/([a-z0-9]{6,20})", re.I
)

# Sub-pages to try when the homepage carries no embed (see extract()). Ordered by
# observed frequency; the loop stops at the first hit, so a healthy site that still
# embeds on its homepage costs zero extra fetches.
EMBED_SUBPATHS: tuple[str, ...] = (
    "/floorplans/", "/floor-plans/", "/availability/", "/apartments/",
)

# Uniform-date veto thresholds (see _parse_api). Only bites when the feed covers
# most of the building AND every listed unit shares one date.
_UNIFORM_DATE_MIN_SHARE = 0.85
_UNIFORM_DATE_MIN_UNITS = 50

# window.__APP_CONFIG__ = {...}  on the SightMap embed page
APP_CONFIG_RE = re.compile(
    r"window\.__APP_CONFIG__\s*=\s*(\{.+?\})\s*$",
    re.MULTILINE,
)

# Concession patterns (same as ARK)
CONCESSION_WEEKS_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)
CONCESSION_MONTHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)

SF_BAND = 50  # ±50 sq ft per planning doc §5b


def matches(community: dict) -> bool:
    """True if the community's page embeds SightMap.

    Note: makes a network call. Acceptable because adapter dispatch happens
    once per community per weekly run, and a static fetch is ~100ms.
    """
    url = (community.get("url") or "").strip()
    if not url:
        return False
    from rentdb.fetchers import fetch_static  # local import — keep module importable offline
    try:
        fetch = fetch_static(url)
    except Exception:
        return False
    if fetch.status >= 400:
        return False
    return EMBED_RE.search(fetch.html) is not None


def extract(community: dict) -> ExtractResult:
    """Fetch + parse a SightMap-backed community page."""
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    if not url:
        return ExtractResult(
            scrape_error="no community URL",
            extractor_used=NAME,
            flag_review=True,
        )

    # Hop 1 — community page
    try:
        page = fetch_static(url)
    except Exception as e:
        return ExtractResult(
            scrape_error=f"community fetch failed: {type(e).__name__}: {e}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    if page.status >= 400:
        return ExtractResult(
            scrape_error=f"community page HTTP {page.status}",
            extractor_used=NAME,
            flag_review=True,
        )

    # Find the SightMap embed reference.
    #
    # 2026-07-28: the embed is NO LONGER on most community homepages — it moved to
    # the floor-plans sub-page. Measured that day: 4 of 5 live sightmap sites
    # (Bria, Siena South, Skyview by Zen, Sablewood) served a 200-OK homepage with
    # zero "sightmap" references while /floorplans/ carried the embed; only
    # Clearview still had it on the homepage. Deterministic across chrome131 /
    # safari17_0 / firefox133, so it is a vendor page-structure change, not bot
    # mitigation. Searching only the homepage silently broke the adapter for most
    # of its ~329 communities. Fall forward through the usual sub-paths.
    m = EMBED_RE.search(page.html)
    if not m:
        for path in EMBED_SUBPATHS:
            try:
                sub = fetch_static(url.rstrip("/") + path)
            except Exception:  # noqa: BLE001 — a dead sub-path is not fatal, try the next
                continue
            if sub.status >= 400:
                continue
            m = EMBED_RE.search(sub.html)
            if m:
                break
    if not m:
        return ExtractResult(
            scrape_error="no sightmap embed found on community page or floor-plan sub-pages",
            extractor_used=NAME,
            flag_review=True,
        )
    # every embed on the page the first one was found on, not just the first.
    return _extract_embed(community, page.html, _embed_ids(m.string))


def extract_rendered(community: dict, html: str) -> ExtractResult:
    """Read the SightMap embed found in the browser-RENDERED page.

    the pipeline's Tier-2 path renders the page for the LLM. On ~44 communities the embed
    is injected by JavaScript, so `extract` (static homepage + subpaths) never sees it and
    the LLM, which cannot read an iframe, answered "no pricing visible" (Belt and Main
    5de0177a: embed 1ywym051wq0 only after render). The embed id is all hops 2-3 need.
    """
    m = EMBED_RE.search(html or "")
    if not m:
        return ExtractResult(scrape_error="no sightmap embed in the rendered page",
                             extractor_used=NAME, flag_review=True)
    return _extract_embed(community, html, _embed_ids(html))


# Words that name the KIND of property, not which one: dropped before a community name is
# compared with a SightMap feed's asset name, so "Channel Point" binds "Channel Point
# Apartments" and a name made only of these ("The Residences") binds nothing.
_GENERIC_NAME_WORDS = frozenset(
    "the a an at on of and by apartments apartment apts apt homes home townhomes townhome "
    "townhouses residences residence living community luxury rentals sightmap".split())


def _name_tokens(name: Optional[str]) -> frozenset[str]:
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and ")).split()
    return frozenset(w for w in words if w not in _GENERIC_NAME_WORDS)


def _bind_score(community_name: Optional[str], asset_name: Optional[str]) -> int:
    """How well a SightMap feed's asset name names this community, after dropping generic
    words: 2 = the same tokens ("Channel Point" / "Channel Point Apartments"), 1 = one name's
    tokens contain the other's ("The Crest at Long Beach" / "The Crest"), 0 = no binding."""
    a, b = _name_tokens(community_name), _name_tokens(asset_name)
    if not (a and b):
        return 0
    return 2 if a == b else 1 if (a <= b or b <= a) else 0


def _binds(community_name: Optional[str], asset_name: Optional[str]) -> bool:
    return _bind_score(community_name, asset_name) > 0


_EMBED_TITLE_RE = re.compile(r"<title>\s*(.*?)\s*(?:-\s*SightMap\s*)?</title>", re.I | re.S)


def _embed_ids(html: str) -> list[str]:
    """Every DISTINCT SightMap embed id in `html`, in page order (a tab link and its iframe
    repeat the same id)."""
    ids: list[str] = []
    for m in EMBED_RE.finditer(html or ""):
        if m.group(1).lower() not in {i.lower() for i in ids}:
            ids.append(m.group(1))
    return ids


def _extract_embed(community: dict, page_html: str, embed_ids: list[str]) -> ExtractResult:
    """Hops 2-3 for the community's OWN feed. `page_html` is the community page: the
    inventory-only fallback and the concession scan read it.

    One embed on the page is the community's, as it always was. Several (Channel Point's
    floor-plans page also links The Crest's SightMap, FIRST, and production stored The Crest's
    units): feeds are read in page order and the first whose asset name binds the community
    name wins; a feed that LOADS but names another community is skipped; none binding is a
    refusal -- another community's units are never stored.

    A feed that FAILS to load ends the read with its error, exactly as the first embed's
    failure always did. RentCafe templates print a junk id ("<id>aspx_MenuLink" in a
    data-selenium-id) before the real link; its 404 has always handed the page to the
    rentcafe adapter, and reading on to the real embed would silently switch those
    communities' extractor (Canvas at Willow Park, rent chain contract corpus)."""
    if len(embed_ids) == 1:
        api_data = _fetch_feed(embed_ids[0])
        if isinstance(api_data, ExtractResult):
            return api_data
    else:
        # Every feed is read: the first loose match is not identity ("The Crest Apartments"
        # contains the tokens of "The Crest at Long Beach"'s rival as well as its own), so the
        # single BEST binding wins and a tie at the best score is a refusal too.
        scored = []
        for embed_id in embed_ids:
            feed = _fetch_feed(embed_id)
            if isinstance(feed, ExtractResult):
                return feed
            name = (((feed.get("data") or {}).get("asset") or {}).get("name")
                    or feed.get("_embed_title"))
            scored.append((_bind_score(community.get("name"), name), embed_id, name, feed))
        best = max(s for s, *_ in scored)
        winners = [t for t in scored if t[0] == best]
        if best == 0 or len(winners) > 1:
            why = "none bound" if best == 0 else "ambiguous"
            return ExtractResult(
                units=[],
                scrape_error=(f"several sightmap embeds, {why} to {community.get('name')!r}: "
                              + "; ".join(f"{e}: {n!r}" for _s, e, n, _f in scored))[:200],
                extractor_used=NAME,
                flag_review=True,
            )
        api_data = winners[0][3]
    return _read_feed(community, page_html, api_data)


def _fetch_feed(embed_id: str) -> "dict | ExtractResult":
    """Hops 2-3 (embed page -> API JSON) for one embed id: the parsed feed, or the error
    result that stops the read."""
    from rentdb.fetchers import fetch_static

    embed_url = f"https://sightmap.com/embed/{embed_id}"

    # Hop 2 — SightMap embed page
    try:
        embed_page = fetch_static(embed_url)
    except Exception as e:
        return ExtractResult(
            scrape_error=f"embed fetch failed: {type(e).__name__}: {e}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    if embed_page.status >= 400:
        return ExtractResult(
            scrape_error=f"embed page HTTP {embed_page.status}",
            extractor_used=NAME,
            flag_review=True,
        )

    title = _EMBED_TITLE_RE.search(embed_page.html or "")
    api_url = _extract_api_url(embed_page.html)
    if not api_url:
        return ExtractResult(
            scrape_error="could not extract SightMap API URL from embed page",
            extractor_used=NAME,
            flag_review=True,
        )

    # Hop 3 — SightMap API
    try:
        api_resp = fetch_static(api_url)
    except Exception as e:
        return ExtractResult(
            scrape_error=f"sightmap api fetch failed: {type(e).__name__}: {e}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    if api_resp.status >= 400:
        return ExtractResult(
            scrape_error=f"sightmap api HTTP {api_resp.status}",
            extractor_used=NAME,
            flag_review=True,
        )
    try:
        api_data = json.loads(api_resp.html)
    except json.JSONDecodeError as e:
        return ExtractResult(
            scrape_error=f"sightmap api JSON parse: {e}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    if not isinstance(api_data, dict):
        return ExtractResult(scrape_error="sightmap api JSON is not an object",
                             extractor_used=NAME, flag_review=True)
    # the embed page's own name for the map, for a feed whose API omits data.asset.name
    api_data["_embed_title"] = title.group(1) if title else None
    return api_data


def _read_feed(community: dict, page_html: str, api_data: dict) -> ExtractResult:
    """The community's own feed -> units (+ the marketing-page fallbacks and concession)."""
    # Parse + group units
    groups, parse_warnings = _parse_api(api_data, _safe_public_units(community))

    # Inventory-only fallback (2026-07-16): some SightMap embeds are a PRICE-LESS
    # map — the units feed carries no price/date, so _parse_api returns
    # availability-unknown groups with null rent (SRQ Stellar / TPA Vireo class).
    # The real pricing lives on the MARKETING page, usually as an embedded
    # RealPage availability JSON. Recover it deterministically; if the marketing
    # page exposes no parseable pricing (a JS-only feed, e.g. JAX Harper Trout
    # River), hand off to the LLM tail by returning empty units + a parse-failure
    # error (the pipeline re-routes when scrape_error is set AND units is empty).
    if _is_inventory_only(parse_warnings):
        data = api_data.get("data") or {}
        recovered = _marketing_page_pricing(
            page_html, data.get("floor_plans") or [], data.get("units") or []
        )
        if recovered is not None:
            groups, parse_warnings = recovered, []
        else:
            return ExtractResult(
                units=[],
                scrape_error="sightmap inventory-only; marketing-page pricing not "
                             "recoverable (routing to LLM)",
                extractor_used=NAME,
                flag_review=True,
            )

    # Concession from community page text (page-level)
    community_text = BeautifulSoup(page_html, "html.parser").get_text(separator=" ", strip=True)
    concession_weeks, raw_concession_text = _extract_concession(community_text)

    return ExtractResult(
        units=groups,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession_text,
        flag_review=bool(parse_warnings),
        extractor_used=NAME,
        scrape_error="; ".join(parse_warnings) if parse_warnings else None,
    )


# ---------------------------------------------------------------------------
# Inventory-only marketing-page pricing fallback (2026-07-16)
# ---------------------------------------------------------------------------
INVENTORY_ONLY_MARKER = "inventory only"

# RealPage availability objects embedded in the marketing page (the price-less-
# SightMap-map class, e.g. SRQ Stellar / TPA Vireo): flat JSON objects carrying
# ApartmentNumber + FloorPlanType + SqFt + MinPrice + DateAvailable — the exact
# pricing the SightMap embed omits. `[^{}]` keeps the match to a single flat
# object; json.loads on each (any nested-object variant simply fails to match
# and is skipped).
_RP_UNIT_RE = re.compile(r'\{[^{}]*?"ApartmentNumber"[^{}]*?\}')
_RP_BEDS_RE = re.compile(r"(\d+)\s*Bedroom", re.I)
_RP_STUDIO_RE = re.compile(r"studio", re.I)


def _is_inventory_only(parse_warnings: list[str]) -> bool:
    return any(INVENTORY_ONLY_MARKER in w for w in parse_warnings)


def _rp_beds(fptype: Optional[str]) -> Optional[int]:
    """Bedroom count from a RealPage FloorPlanType string ('2Bedroom', 'Studio')."""
    if not fptype:
        return None
    if _RP_STUDIO_RE.search(fptype):
        return 0
    m = _RP_BEDS_RE.search(fptype)
    return int(m.group(1)) if m else None


def _marketing_page_pricing(
    page_html: str,
    floor_plans_raw: list[dict],
    units_raw: list[dict],
) -> Optional[list[UnitGroup]]:
    """Recover pricing from a price-less SightMap community's marketing page.

    Targets the RealPage availability JSON embedded in the page (the Stellar/
    Vireo pattern): each listed unit is a flat object with ApartmentNumber /
    FloorPlanType / SqFt / MinPrice / DateAvailable. Returns grouped UnitGroups
    (base rent, listed = available), with advertised zero-availability bed types
    folded in from the SightMap `floor_plans` catalog. Returns None when no
    RealPage pricing is embedded — the caller then routes to the LLM tail.

    NOTE: RealPage `SqFt` is the interior area (~200sf below the marketing SF the
    site headlines), the same offset as the SightMap `area` field — so beds /
    rent / availability are exact, SF is approximate.
    """
    raw: list[_RawUnit] = []
    for m in _RP_UNIT_RE.finditer(page_html):
        try:
            o = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        if "MinPrice" not in o and "DisplayPrice" not in o:
            continue
        price = o.get("MinPrice")
        price_int = int(float(price)) if isinstance(price, (int, float)) and price else None
        date_av = o.get("DateAvailable") or o.get("MinAvailable")
        listed = bool(date_av) or bool(price_int)
        if not listed:
            continue
        sf = o.get("SqFt")
        raw.append(_RawUnit(
            beds=_rp_beds(o.get("FloorPlanType")),
            sf=int(sf) if isinstance(sf, (int, float)) and sf else None,
            price=price_int,
            total_min=None,
            total_max=None,
            status="available",
            available_on=date_av if isinstance(date_av, str) else None,
        ))

    if not any(r.price is not None for r in raw):
        return None  # no embedded RealPage pricing — caller hands off to the LLM

    groups = _group_by_beds_and_sf(raw)
    # Fold in advertised bed types the marketing feed didn't price (zero-avail),
    # using the SightMap floor_plans catalog we already fetched.
    groups = _add_advertised_layouts(groups, floor_plans_raw, units_raw)
    return groups


# ---------------------------------------------------------------------------
# Embed page → API URL
# ---------------------------------------------------------------------------
def _extract_api_url(embed_html: str) -> Optional[str]:
    """Pull the API URL out of window.__APP_CONFIG__ on a SightMap embed page.

    Shape:
      window.__APP_CONFIG__ = {"sightmaps":[{"href":"https://sightmap.com/app/api/v1/<account>/sightmaps/<id>", ...}], ...}
    """
    m = APP_CONFIG_RE.search(embed_html)
    if not m:
        # Fallback: scan for any /app/api/v1/...sightmaps/<id> reference in raw HTML
        m2 = re.search(r'(https?://sightmap\.com/app/api/v1/[a-z0-9]+/sightmaps/\d+)', embed_html)
        return m2.group(1) if m2 else None
    try:
        # The captured JSON has escaped slashes (\/). json.loads handles that.
        cfg = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    sightmaps = cfg.get("sightmaps") or []
    if not sightmaps:
        return None
    href = sightmaps[0].get("href")
    return href


# ---------------------------------------------------------------------------
# API → UnitGroups
# ---------------------------------------------------------------------------
def _parse_api(api_data: dict, public_record_units: int | None = None) -> tuple[list[UnitGroup], list[str]]:
    """Parse the SightMap API JSON into UnitGroup objects.

    Returns (groups, warnings).
    """
    data = api_data.get("data") or {}
    floor_plans_raw = data.get("floor_plans") or []
    units_raw = data.get("units") or []
    warnings: list[str] = []

    floor_plans_raw, units_raw, dropped = _drop_placeholder_plans(floor_plans_raw, units_raw)
    if dropped:
        warnings.append(f"sightmap: dropped {len(dropped)} placeholder floor plan(s): "
                        + ", ".join(dropped))

    # Build floor_plan_id → (beds, baths) lookup
    fp_lookup: dict[str, tuple[int | None, float | None]] = {}
    fp_names: dict[str, str] = {}
    for fp in floor_plans_raw:
        fp_id = str(fp.get("id"))
        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")
        fp_lookup[fp_id] = (
            int(beds) if isinstance(beds, (int, float)) else None,
            float(baths) if isinstance(baths, (int, float)) else None,
        )
        fp_names[fp_id] = str(fp.get("name") or "")

    raw: list[_RawUnit] = []
    for u in units_raw:
        fp_id = str(u.get("floor_plan_id"))
        beds, baths = fp_lookup.get(fp_id, (None, None))
        sf = u.get("area")
        sf_int = int(sf) if isinstance(sf, (int, float)) else None

        price = u.get("price")
        price_int = int(price) if isinstance(price, (int, float)) else None

        # total_price is [min, max] when present; equal values when flat pricing
        total_price = u.get("total_price")
        total_min = total_max = None
        if isinstance(total_price, list) and total_price:
            try:
                total_min = int(total_price[0]) if total_price[0] is not None else None
                total_max = int(total_price[-1]) if total_price[-1] is not None else None
            except (TypeError, ValueError):
                pass

        # available_on may be missing/null/date string.
        # LISTED = carries a price or a date. SightMap is a property MAP — its
        # units endpoint returns the WHOLE building; bare units (no price, no
        # date) are inventory, not availability (golden-eval catch 2026-07-14:
        # Avalon reported 126 "available" = its entire unit count; site showed 4).
        available_on_str = u.get("available_on")
        # LISTED = a real per-unit price OR a move-in date. `total_price` is NOT a
        # listing signal: the AUS Residences Of Siena South feed populates it on all
        # 292 units including the 184 that carry neither a price nor a date, which
        # made the whole building read as available (ratio 1.00, flag_review=false).
        # Requiring price-or-date yields 108 there and changes nothing on the five
        # other fixtures measured 2026-07-28.
        listed = price_int is not None or bool(available_on_str)
        status = _classify_status(available_on_str, listed)

        raw.append(_RawUnit(
            beds=beds,
            sf=sf_int,
            price=price_int,
            total_min=total_min,
            total_max=total_max,
            status=status,
            available_on=available_on_str,
            plan_name=fp_names.get(str(u.get("floor_plan_id")), ""),
            baths=baths,
        ))

    if not raw:
        warnings.append("no units in API response")

    listed_units = [r for r in raw if r.status != "unlisted"]

    # UNIFORM-DATE VETO (2026-07-28). Some embeds stamp ONE identical available_on
    # across the entire feed — LAS Bria returned 256 units all dated 2026-07-28 with
    # a price, while its live site listed 40. A single date shared by every unit
    # carries no per-unit availability information, so counting them is a confident
    # fabrication. Distinguish it from a real lease-up by date VARIETY, not by ratio:
    # healthy feeds spread across many dates (Sablewood 9, Skyview 6) even at high
    # occupancy. Guarded by share-of-building so a small uniform batch can't trip it.
    if listed_units and public_record_units and public_record_units >= _UNIFORM_DATE_MIN_UNITS:
        dated = [r for r in listed_units if r.available_on]
        dates = {r.available_on for r in dated}
        share = len(listed_units) / public_record_units
        # EVERY listed unit must carry that one date. Counting distinct dates alone
        # would fire on a feed of many price-only listings plus a single dated one
        # (one distinct date), nulling availability that is legitimately listed under
        # the standing price-or-date rule. A cross-model review finding.
        if (len(dates) == 1 and len(dated) == len(listed_units)
                and share >= _UNIFORM_DATE_MIN_SHARE):
            warnings.append(
                f"sightmap uniform-date feed: {len(listed_units)} units all dated "
                f"{dates.pop()} ({share:.0%} of public_record_units) — availability nulled")
            # Group from listed_units, NOT raw: unlisted inventory can still carry a
            # total_price, and feeding it in here would let non-listings move
            # rent_allin_min/max. (The else-branch below may pass raw because there
            # NOTHING is listed, so raw is all there is.) Codex review,
            # finding 2.
            groups = _group_by_beds_and_sf(listed_units, availability_known=False)
            groups = _add_advertised_layouts(groups, floor_plans_raw, units_raw)
            return groups, warnings

    if listed_units:
        # Market data: group only the units actually on the market. Unlisted
        # inventory would otherwise add bed groups the site doesn't advertise.
        groups = _group_by_beds_and_sf(listed_units)
        # Re-add any advertised bed type with zero listed units so it isn't lost
        # (golden catch: Trailside NoDa's advertised 3BR — no live inventory).
        groups = _add_advertised_layouts(groups, floor_plans_raw, units_raw)
    else:
        # Inventory-only tenant (embed carries no prices/dates — e.g. Avalon
        # uses SightMap purely as a map). Keep bed/SF structure, but
        # availability is UNKNOWN — null + flag, never the building count.
        if raw:
            warnings.append("sightmap embed has no priced/dated units — "
                            "inventory only; availability unknown")
        groups = _group_by_beds_and_sf(raw, availability_known=False)
    return groups, warnings


# A leasable home is never this small; anything at or under this is a staging or
# placeholder record in the operator's SightMap account, not inventory.
_MIN_PLAUSIBLE_UNIT_SF = 100
_PLACEHOLDER_PLAN_NAMES = {"temp", "test", "testing", "model", "placeholder", "tbd",
                           "do not use", "donotuse", "dummy", "sample", "n/a"}


def _drop_placeholder_plans(
    floor_plans_raw: list[dict],
    units_raw: list[dict],
) -> tuple[list[dict], list[dict], list[str]]:
    """Remove staging/placeholder floor plans before anything reads them.

    Aventerra at Dobson Ranch publishes a plan literally named `TEMP` with
    `bedroom_count=0`, `area=1` and one unit priced $1,209 — and every run
    surfaced it as a genuine 0-bed group (it is the "pipeline shows 0-bed, site
    doesn't" precision miss in the PHX mf golden). A 1-sf "studio" at $1,209 is
    exactly the row that must never reach v_latest_unit_groups -> the rent-comp consumer.

    Measured 2026-08-10: 22 groups across 15 communities fleet-wide carry an
    SF <= 5, so this is not one community's typo.

    Two independent signals, either is enough: a placeholder NAME, or an area
    below any leasable size. Name matching is exact against the set below (after
    casefolding/trimming) — never a substring, or a real plan like "Temple" or
    "Modelo" would be dropped with it.
    """
    dropped_ids: set[str] = set()
    dropped_names: list[str] = []
    kept_plans: list[dict] = []
    for fp in floor_plans_raw:
        name = str(fp.get("name") or "").strip()
        area = fp.get("area")
        junk_name = name.casefold() in _PLACEHOLDER_PLAN_NAMES
        junk_area = isinstance(area, (int, float)) and 0 < area < _MIN_PLAUSIBLE_UNIT_SF
        if junk_name or junk_area:
            dropped_ids.add(str(fp.get("id")))
            dropped_names.append(name or f"id={fp.get('id')}")
            continue
        kept_plans.append(fp)

    kept_units = []
    for u in units_raw:
        if str(u.get("floor_plan_id")) in dropped_ids:
            continue
        # A unit can carry a junk area even when its plan looks fine.
        area = u.get("area")
        if isinstance(area, (int, float)) and 0 < area < _MIN_PLAUSIBLE_UNIT_SF:
            continue
        kept_units.append(u)
    return kept_plans, kept_units, dropped_names


def _add_advertised_layouts(
    groups: list[UnitGroup],
    floor_plans_raw: list[dict],
    units_raw: list[dict],
) -> list[UnitGroup]:
    """Zero-availability floor-plan enumeration (G0.3 convention, 2026-07-15).

    The units feed only yields LISTED units, so an advertised bed type with no
    live inventory drops out of `groups` entirely (golden catch: Trailside NoDa's
    advertised 3BR — zero listed units, so the bed group vanished, 17/18 recall).
    The SightMap API's `floor_plans` array is plan-level truth — any advertised
    bed type not already covered joins as a zero-availability group (available=0,
    rent null, flag_review) so the bed type is never lost.

    SF is recovered from that bed type's own units when present (a plan can carry
    unlisted-only inventory); null otherwise — SightMap `floor_plans` objects
    carry no size field, only bedroom/bathroom counts.
    """
    covered = {g.beds for g in groups if g.beds is not None}

    # floor_plan_id -> beds, then beds -> SF samples drawn from that type's units.
    fp_beds: dict[str, int | None] = {}
    for fp in floor_plans_raw:
        beds = fp.get("bedroom_count")
        fp_beds[str(fp.get("id"))] = int(beds) if isinstance(beds, (int, float)) else None

    sf_by_beds: dict[int, list[int]] = defaultdict(list)
    for u in units_raw:
        beds = fp_beds.get(str(u.get("floor_plan_id")))
        if beds is None:
            continue
        sf = u.get("area")
        if isinstance(sf, (int, float)):
            sf_by_beds[beds].append(int(sf))

    # Plan NAMES per bed type — an advertised plan still names its own structure,
    # and skipping the cues here left "The Oak Two-Story Townhome" storing None
    # purely because nothing was listed that week.
    names_by_beds: dict[int, list[str]] = defaultdict(list)
    for fp in floor_plans_raw:
        b = fp_beds.get(str(fp.get("id")))
        if b is not None and fp.get("name"):
            names_by_beds[b].append(str(fp["name"]))

    advertised_beds = {b for b in fp_beds.values() if b is not None}
    for beds in sorted(advertised_beds - covered):
        sfs = sf_by_beds.get(beds) or []
        plan_text = " ".join(names_by_beds.get(beds, []))
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=0,
                coming_soon=0,
                rent_min=None,
                rent_max=None,
                rent_type="base",
                structure_type=cues.structure_type_from_text(plan_text),
                stories=cues.stories_from_text(plan_text),
                garage=cues.garage_from_text(plan_text),
                # The catalog names this plan even with nothing listed, and the
                # fin-model's live-rent column matches on that name (2026-08-20).
                plan_name=observed_plan_name(names_by_beds.get(beds, [])),
                flag_review=True,   # advertised only — nothing listed to verify
            )
        )
    groups.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return groups


@dataclass
class _RawUnit:
    beds: int | None
    sf: int | None
    price: int | None              # base
    total_min: int | None          # all-in min
    total_max: int | None          # all-in max
    status: str                    # "available" | "coming_soon" | "unknown"
    available_on: str | None
    # The floor plan's NAME ("The Oak — Two-Story Townhome"). SightMap's feed
    # carries no other prose, and an operator-written plan name IS page language.
    plan_name: str = ""
    # floor_plans[].bathroom_count, carried per unit so the group can apply
    # observed_baths. Defaulted because the RealPage marketing-feed path above
    # builds _RawUnit from a payload that has no bath field at all.
    baths: float | None = None


def _classify_status(available_on: Optional[str], listed: bool = True) -> str:
    # Listed = available (semantics decision 2026-07-14): a unit with a price
    # OR a move-in date is on the market — the site UI counts it as available
    # even when the date is in the future. A unit with neither is building
    # inventory, NOT on the market. SightMap has no explicit "coming soon"
    # label, so this adapter never emits coming_soon.
    if listed or available_on:
        return "available"
    return "unlisted"


# ---------------------------------------------------------------------------
# Grouping (same logic as ARK adapter; consider extracting to base.py later)
# ---------------------------------------------------------------------------
def _group_by_beds_and_sf(listings: list[_RawUnit],
                          availability_known: bool = True) -> list[UnitGroup]:
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
        prices = [r.price for r in items if r.price is not None]
        totals = [(r.total_min, r.total_max) for r in items if r.total_min is not None]
        avail_count = sum(1 for r in items if r.status == "available")
        soon_count = sum(1 for r in items if r.status == "coming_soon")
        plan_text = " ".join(sorted({r.plan_name for r in items if r.plan_name}))

        rent_min = min(prices) if prices else None
        rent_max = max(prices) if prices else None

        # Populate rent_allin only when site shows a different total
        rent_allin_min = rent_allin_max = None
        if totals and prices:
            tmin = min(t[0] for t in totals if t[0] is not None)
            tmax = max(t[1] for t in totals if t[1] is not None)
            if tmin != min(prices) or tmax != max(prices):
                rent_allin_min = tmin
                rent_allin_max = tmax

        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=avail_count if availability_known else None,
                coming_soon=soon_count if availability_known else None,
                rent_min=rent_min,
                rent_max=rent_max,
                rent_type="base",  # SightMap `price` is the base rent
                rent_allin_min=rent_allin_min,
                rent_allin_max=rent_allin_max,
                unit_rents=observed_rents(prices),
                baths=observed_baths(r.baths for r in items),
                structure_type=cues.structure_type_from_text(plan_text),
                stories=cues.stories_from_text(plan_text),
                garage=cues.garage_from_text(plan_text),
                # NOT plan_text: that is every name in the group joined for the cue
                # regexes. The stored name must be the ONE name the group agrees on,
                # or nothing (2026-08-20).
                plan_name=observed_plan_name(r.plan_name for r in items),
                flag_review=(not prices or not sfs) or not availability_known,
            )
        )

    # Listings with no beds (rare) — single flagged group
    unknown = [r for r in listings if r.beds is None]
    if unknown:
        prices = [r.price for r in unknown if r.price is not None]
        avail_count = sum(1 for r in unknown if r.status == "available")
        soon_count = sum(1 for r in unknown if r.status == "coming_soon")
        groups.append(
            UnitGroup(
                beds=None,
                sf_min=None,
                sf_max=None,
                available=avail_count if availability_known else None,
                coming_soon=soon_count if availability_known else None,
                rent_min=min(prices) if prices else None,
                rent_max=max(prices) if prices else None,
                rent_type="base",
                unit_rents=observed_rents(prices),
                flag_review=True,
            )
        )

    groups.sort(key=lambda g: (g.beds or -1, g.sf_min or -1))
    return groups


# ---------------------------------------------------------------------------
# Concession (page-level)
# ---------------------------------------------------------------------------
def _extract_concession(text: str) -> tuple[int | None, str | None]:
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, CONCESSION_WEEKS_RE, CONCESSION_MONTHS_RE)


def _safe_public_units(community: dict) -> int | None:
    """community['public_record_units'] as an int, or None. Seed rows sometimes carry '' or a str."""
    v = (community or {}).get("public_record_units")
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
