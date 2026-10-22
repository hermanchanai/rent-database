"""Adapter for public Knock Doorway inventory used by BB Living."""

from __future__ import annotations

import re
from collections import defaultdict

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession

NAME = "knock"
SF_BAND = 50
_INIT_RE = re.compile(
    r"""knockDoorway\.init\(\\?["'][^"']+\\?["'],\s*\\?["']community\\?["'],\s*\\?["']([a-f0-9]+)""",
    re.I,
)
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
    return page.status < 400 and _INIT_RE.search(page.html) is not None


# from the 2026-09-19 rotation on, the Doorway API fails INTERMITTENTLY on valid ids --
# the community lookup answers 404 {"error": "property cannot be found"} and the units feed 500
# {"message": "...Failed to retrieve property unit data from core api..."}, and the very next
# call answers 200 (measured live 2026-10-05: 30 sequential calls on one community, one 404 and
# one 500; three of the 18 failing communities failed one extract() and read on the next).
# So a 5xx, a 429, a transport error, and a 404 from the COMMUNITY LOOKUP (the one place a
# transient 404 was observed) are retried a bounded number of times; anything still failing
# falls through exactly as before (scrape_error + flag_review -> the next tier).
_RETRIES = 2
_RETRY_BACKOFF_S = 1.0


def _api_get(url: str, *, retry_404: bool):
    """GET a Doorway API url, retrying a transient failure.

    Returns (response, error_text, attempts); error_text is set (response may be None) only
    when the last attempt failed. Every attempt goes through the shared per-host throttle: `wait_for` honours a 429/503 cooldown / Retry-After and the
    enclosing community `deadline()` budget (raising DeadlineExceeded, which the pipeline owns),
    and `note_response` arms that cooldown. The first attempt adds no pacing of its own; a
    retry waits at least `_RETRY_BACKOFF_S * attempt` since the host's last request.
    """
    from rentdb.fetchers import throttle

    resp, err, attempts = None, None, 0
    for attempt in range(_RETRIES + 1):
        attempts = attempt + 1
        throttle.wait_for(url, min_interval=_RETRY_BACKOFF_S * attempt)
        try:
            resp = cr.get(url, impersonate="chrome131", timeout=25)
        except Exception as exc:  # transport: timeout / reset / DNS
            resp, err = None, f"{type(exc).__name__}: {exc}"
            continue
        throttle.note_response(url, resp.status_code, resp.headers.get("Retry-After"))
        # 429 too: note_response just armed the host cooldown, and the next
        # wait_for honours it -- bounded by the community deadline.
        transient = (resp.status_code >= 500 or resp.status_code == 429
                     or (retry_404 and resp.status_code == 404))
        if not transient:
            return resp, None, attempts
        err = f"HTTP {resp.status_code}"
    return resp, err, attempts


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    try:
        page = fetch_static(url)
    except Exception as exc:
        return ExtractResult(
            scrape_error=f"knock: site fetch failed: {type(exc).__name__}: {exc}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    match = _INIT_RE.search(page.html)
    if not match:
        return ExtractResult(
            scrape_error="knock: community id not found",
            extractor_used=NAME,
            flag_review=True,
        )

    community_id = match.group(1)
    community_response, err, n = _api_get(
        f"https://doorway-api.knockrentals.com/v1/property/community/{community_id}",
        retry_404=True,
    )
    if err is None and community_response.status_code >= 400:
        err = f"HTTP {community_response.status_code}"
    if err is not None:
        return ExtractResult(
            scrape_error=f"knock: community API {err} (attempts={n})"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    property_id = community_response.json()["property"]["id"]
    units_response, err, n = _api_get(
        f"https://doorway-api.knockrentals.com/v1/property/{property_id}/units",
        retry_404=False,
    )
    if err is None and units_response.status_code >= 400:
        err = f"HTTP {units_response.status_code}"
    if err is not None:
        return ExtractResult(
            scrape_error=f"knock: units API {err} (attempts={n})"[:200],
            extractor_used=NAME,
            flag_review=True,
        )

    units_data = units_response.json().get("units_data") or {}
    source_units = units_data.get("units") or []
    groups = _group_units(source_units)
    groups = _add_advertised_layouts(groups, units_data.get("layouts") or [])
    page_text = BeautifulSoup(page.html, "html.parser").get_text(" ", strip=True)
    concession_weeks, raw_concession = _concession(page_text)
    return ExtractResult(
        units=groups,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession,
        extractor_used=NAME,
        flag_review=any(group.flag_review for group in groups),
    )


def _add_advertised_layouts(groups: list[UnitGroup], layouts: list[dict]) -> list[UnitGroup]:
    """Zero-availability plan enumeration (G0.3 convention, 2026-07-15).

    The units feed only carries AVAILABLE units, so a bed type with nothing
    listed vanishes (golden catch: Bridge at Goodnight Ranch's advertised 1BR).
    The feed's `layouts` array is plan-level truth — any advertised bed type
    with no unit group joins as available=0 / rent-null / flag_review.
    """
    covered = {g.beds for g in groups if g.beds is not None}
    seen: set[tuple[int, int | None]] = set()
    for layout in layouts:
        if layout.get("deletedAt") or layout.get("deletedByVendor"):
            continue
        beds = _int(layout.get("bedrooms"))
        if beds is None or beds in covered:
            continue
        sf = _int(layout.get("area"))
        key = (beds, sf)
        if key in seen:
            continue
        seen.add(key)
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=sf,
                sf_max=sf,
                available=0,
                coming_soon=0,
                rent_min=None,
                rent_max=None,
                rent_type="base",
                flag_review=True,   # advertised only — nothing listed to verify
            )
        )
    groups.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return groups


def _group_units(source_units: list[dict]) -> list[UnitGroup]:
    buckets: dict[tuple[int | None, int], list[dict]] = defaultdict(list)
    for unit in source_units:
        if not unit.get("available") or unit.get("leased") or unit.get("hidden"):
            continue
        beds = _int(unit.get("bedrooms"))
        sqft = _int(unit.get("area"))
        center = round((sqft or 0) / SF_BAND) * SF_BAND
        buckets[(beds, center)].append(unit)

    groups = []
    for (beds, _), members in sorted(buckets.items(), key=lambda item: (item[0][0] or 0, item[0][1])):
        rents = [_money(m.get("displayPrice") or m.get("price")) for m in members]
        sfs = [_int(m.get("area")) for m in members]
        # Listed = available (2026-07-14): the feed is already filtered to
        # available/not-leased/not-hidden — a future availableOn date does not
        # make a listed unit "coming soon".
        available = len(members)
        coming_soon = 0
        rents = [v for v in rents if v]
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
                unit_rents=observed_rents(rents),
                # units_data.units[].bathrooms, alongside the bedrooms/area this
                # adapter already reads. Verified on the live Doorway feed.
                baths=observed_baths(m.get("bathrooms") for m in members),
                flag_review=not rents,
            )
        )
    return groups


def _int(value) -> int | None:
    try:
        return int(round(float(str(value).replace(",", ""))))
    except (TypeError, ValueError):
        return None


def _money(value) -> int | None:
    parsed = _int(value)
    return parsed if parsed and parsed > 0 else None


def _concession(text: str) -> tuple[int | None, str | None]:
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, _WEEKS_FREE_RE, _MONTHS_FREE_RE)
