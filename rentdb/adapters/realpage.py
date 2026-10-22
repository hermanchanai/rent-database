"""Adapter for RealPage/OneSite floor-plan and unit inventory."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession

NAME = "realpage"
SF_BAND = 50

SIGNALS = (
    "api.ws.realpage.com",
    "cs-cdn.realpage.com",
    "c-leasestar-api.realpage.com",
    "onlineleasing.realpage.com",
)
_PROPERTY_RE = re.compile(r"""propertyId\s*=\s*['"](\d+)""", re.I)
_API_KEY_RE = re.compile(r"""apiKey\s*:\s*['"]([^'"]+)""", re.I)
_WEEKS_FREE_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)
_MONTHS_FREE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)


def matches(community: dict) -> bool:
    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return False
    try:
        from rentdb.fetchers import fetch_static
        page = fetch_static(url)
    except Exception:
        return False
    low = page.html.lower()
    return page.status < 400 and any(signal in low for signal in SIGNALS)


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    base = (community.get("url") or "").strip().rstrip("/")
    pages = []
    for candidate in (
        base + "/Floor-plans.aspx",
        base + "/floor-plan-availability/",
        base,
    ):
        try:
            page = fetch_static(candidate)
        except Exception:
            continue
        if page.status < 400:
            pages.append(page)
            if _PROPERTY_RE.search(page.html) and _API_KEY_RE.search(page.html):
                break
    if not pages:
        return ExtractResult(
            scrape_error="realpage: site fetch failed",
            extractor_used=NAME,
            flag_review=True,
        )

    html = pages[-1].html
    property_match = _PROPERTY_RE.search(html)
    api_keys = _API_KEY_RE.findall(html)
    if not property_match or not api_keys:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if re.search(r"\bcoming soon\b", text, re.I):
            return ExtractResult(units=[], extractor_used=NAME)
        return ExtractResult(
            scrape_error="realpage: property id or public API key not found",
            extractor_used=NAME,
            flag_review=True,
        )

    property_id = property_match.group(1)
    headers = {"referer": base + "/"}
    floorplans_payload = None
    winning_key = None
    for api_key in reversed(api_keys):
        response = cr.get(
            f"https://api.ws.realpage.com/v2/property/{property_id}/floorplans",
            headers={**headers, "x-ws-authkey": api_key},
            impersonate="chrome131",
            timeout=25,
        )
        if response.status_code == 200:
            floorplans_payload = response.json()
            winning_key = api_key
            break
    if winning_key is None:
        return ExtractResult(
            scrape_error="realpage: public inventory API rejected site key",
            extractor_used=NAME,
            flag_review=True,
        )

    terms = ",".join(str(n) for n in range(1, 25))
    units_url = (
        f"https://api.ws.realpage.com/v2/property/{property_id}/units"
        f"?available=true&honordisplayorder=true&siteid={property_id}"
        f"&bestprice=true&leaseterm={terms}&dateneeded={date.today().isoformat()}"
    )
    response = cr.get(
        units_url,
        headers={**headers, "x-ws-authkey": winning_key},
        impersonate="chrome131",
        timeout=25,
    )
    source_units = []
    if response.status_code == 200:
        payload = response.json().get("response") or {}
        source_units = payload.get("units") or []

    floorplans = ((floorplans_payload or {}).get("response") or {}).get("floorplans") or []
    groups = _merge_unlisted_floorplans(_group_units(source_units), floorplans)

    page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    concession_weeks, raw_concession = _concession(page_text)
    return ExtractResult(
        units=groups,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession,
        extractor_used=NAME,
        flag_review=any(group.flag_review for group in groups),
    )


def _group_units(source_units: list[dict]) -> list[UnitGroup]:
    buckets: dict[tuple[int | None, int], list[dict]] = defaultdict(list)
    for unit in source_units:
        if unit.get("hidden") or unit.get("displayed") is False:
            continue
        beds = _int(unit.get("numberOfBeds"))
        sqft = _int(unit.get("squareFeet"))
        center = round((sqft or 0) / SF_BAND) * SF_BAND
        buckets[(beds, center)].append(unit)

    groups: list[UnitGroup] = []
    for (beds, _), members in sorted(buckets.items(), key=lambda item: (item[0][0] or 0, item[0][1])):
        sfs = [_int(m.get("squareFeet")) for m in members]
        rents = [_money(m.get("rent")) for m in members]
        allin = [_money(m.get("totalRent")) for m in members]
        # Listed = available (2026-07-14): every displayed unit is an on-market
        # listing regardless of internalAvailableDate.
        available = len(members)
        coming_soon = 0
        rents = [v for v in rents if v]
        allin = [v for v in allin if v]
        sfs = [v for v in sfs if v]
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=available,
                coming_soon=coming_soon,
                rent_min=min(rents) if rents else None,
                rent_max=max(rents) if rents else None,
                rent_type="base",
                rent_allin_min=min(allin) if allin else None,
                rent_allin_max=max(allin) if allin else None,
                unit_rents=observed_rents(rents),
                # The UNITS feed says numberOfBaths; the floorplans feed spells the
                # same fact bathRooms. Units win — they are what we group on.
                baths=observed_baths(m.get("numberOfBaths") for m in members),
                flag_review=not rents,
            )
        )
    return groups


def _merge_unlisted_floorplans(groups: list[UnitGroup],
                               floorplans: list[dict]) -> list[UnitGroup]:
    """Add any floorplans-feed bed type the UNITS feed did not cover.

    THE DEFECT THIS FIXES (ATL Union Landing, ATL/mf golden 2026-08-25). `/floorplans`
    returned all three of that property's plans — 1bd 841sf $1,216, 2bd 1,068sf $1,453,
    3bd 1,240sf $1,673, the same three its own site prints — while `/units` returned 18
    real listed units (18 distinct ids, verified), 9 x 2bd and 9 x 3bd and no 1bd. The
    floorplans feed was consulted only `if not groups`, so a plan with nothing currently
    listed vanished the moment ANY other plan had a unit. One missing bed group was the
    whole of ATL/mf's bed-recall failure, 14/15 against a >=95% bar.

    KEYED ON BED COUNT, exactly like `rentcafe._merge_advertised`. An SF-band key is the
    obvious generalisation and it is the wrong one: it was built and measured on the
    rentcafe side and reshaped the group set on 11 of 60 sampled communities, because SF
    banding legitimately merges several plans into one group.

    THE MERGED GROUP CARRIES NO AVAILABILITY AND NO RENT.

    `numberOfUnitsDisplay` is not a count — all three Union Landing plans report 3,
    including the two the units feed shows nine real units for. `available=0` here is a
    positive finding rather than a guess: the units feed enumerates every listed unit and
    contains no 1bd.

    No rent, matching `rentcafe._merge_advertised`. The feed publishes minimumMarketRent
    and the site prints it, but a bed type with nothing listed has no price a renter can
    pay and `rents.market_rent()` cannot tell that from a real one. This trades the recall
    miss for a rent_min miss in the golden grade, which is the right way round: recall is a
    gate axis, and a phantom rent reaching a comp set is the expensive error.

    WHEN THE UNITS FEED IS ENTIRELY EMPTY nothing changes — the long-standing wholesale
    fallback to `_group_floorplans` still runs, rents and all. That asymmetry is
    deliberate: with no units feed the floorplans feed is the only evidence there is, but
    once it is answering for other plans its silence about THIS plan is itself evidence.
    """
    if not groups:
        return _group_floorplans(floorplans)
    covered = {g.beds for g in groups if g.beds is not None}
    for floorplan in floorplans:
        beds = _int(floorplan.get("bedRooms"))
        if beds is None or beds in covered:
            continue
        covered.add(beds)
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=_int(floorplan.get("minimumSquareFeet")),
                sf_max=_int(floorplan.get("maximumSquareFeet")),
                available=0,
                coming_soon=0,
                rent_min=None,
                rent_max=None,
                rent_type="base",
                baths=observed_baths([floorplan.get("bathRooms")]),
                flag_review=True,   # published plan, nothing listed against it
            )
        )
    groups.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return groups


def _group_floorplans(floorplans: list[dict]) -> list[UnitGroup]:
    """The /units feed came back empty: the floorplans feed is the only evidence left.

    AVAILABILITY IS UNKNOWN ON THIS PATH. `numberOfUnitsDisplay` is not a count
    (see `_merge_unlisted_floorplans`): Page Oaks RDU reported 99 on all three plans
    while /units listed 5, 2 and 3, and those 99s were stored as 297 available homes in a
    188-unit community. It still decides which plans are advertised (> 0), but it is
    never stored as `available` — None + flag_review, the same "availability unknown"
    shape sightmap uses for an inventory-only embed.
    """
    groups = []
    for floorplan in floorplans:
        count = _int(floorplan.get("numberOfUnitsDisplay")) or 0
        if count <= 0:
            continue
        rent = _money(floorplan.get("minimumMarketRent"))
        max_rent = _money(floorplan.get("maximumMarketRent")) or rent
        groups.append(
            UnitGroup(
                beds=_int(floorplan.get("bedRooms")),
                sf_min=_int(floorplan.get("minimumSquareFeet")),
                sf_max=_int(floorplan.get("maximumSquareFeet")),
                available=None,
                coming_soon=None,
                rent_min=rent,
                rent_max=max_rent,
                rent_type="base",
                flag_review=True,
            )
        )
    return groups


def _int(value) -> int | None:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return None


def _money(value) -> int | None:
    try:
        amount = float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None
    return int(round(amount)) if amount > 0 else None


def _concession(text: str) -> tuple[int | None, str | None]:
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, _WEEKS_FREE_RE, _MONTHS_FREE_RE)
