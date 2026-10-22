"""Adapter for AvalonBay communities (avaloncommunities.com).

Operator-level adapter (like ARK/AMH — URL match, no probe fetch). Built for
the G0.4 golden-eval fix (2026-07-15): Avalon pages embed SightMap purely as a
price-less MAP, so the sightmap adapter matched first and reported building
inventory with unknown availability. The real listings live in a JSON state
blob embedded in the community page itself.

Page structure: server-rendered payload contains `"units":[{...}]` where each
unit carries:
    bedroomNumber, bathroomNumber, squareFeet, unitStatus,
    startingAtPricesUnfurnished.prices.{price, totalPrice, netEffectivePrice},
    availableDateUnfurnished, promotions[], concessions[]

Rent type: `price` is base rent; `totalPrice` includes required monthly fees —
so we populate rent_min/max from price and rent_allin_* when totalPrice
differs (site shows both).

Availability: listed = available (semantics decision 2026-07-14). Every unit
in the embedded state is an on-market listing regardless of move-in date.

Concession: from each unit's promotions/concessions arrays when present, else
the shared page-text banner regexes.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from bs4 import BeautifulSoup

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession

NAME = "avalon"

SF_BAND = 50

CONCESSION_WEEKS_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)
CONCESSION_MONTHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)

_UNITS_MARKER = '"units":['


def matches(community: dict) -> bool:
    """Pure URL check — AvalonBay hosts every community on its own domain."""
    url = (community.get("url") or "").lower()
    return "avaloncommunities.com" in url


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    if not url:
        return ExtractResult(scrape_error="no community URL", extractor_used=NAME, flag_review=True)

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

    units_raw = _extract_units_json(page.html)
    if units_raw is None:
        return ExtractResult(
            scrape_error="avalon: no embedded units JSON found",
            extractor_used=NAME,
            flag_review=True,
        )

    groups = _group_units(units_raw)
    concession_weeks, raw_concession_text, unit_scoped = _concession(units_raw, page.html)

    return ExtractResult(
        units=groups,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession_text,
        concession_unit_scoped=unit_scoped,
        flag_review=unit_scoped or any(g.flag_review for g in groups),
        extractor_used=NAME,
        # A page with an empty units[] is a valid zero-availability read, not
        # an error — the pipeline classifies it no_availability.
    )


# ---------------------------------------------------------------------------
def _extract_units_json(html: str) -> list[dict] | None:
    """Bracket-match every `"units":[...]` blob; return the first that parses
    into a list of unit dicts (has unitId). The payload repeats — variants are
    equivalent for our fields."""
    for m in re.finditer(re.escape(_UNITS_MARKER), html):
        start = m.start() + len('"units":')
        depth = 0
        end = None
        for j in range(start, min(len(html), start + 2_000_000)):
            ch = html[j]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end is None:
            continue
        raw = html[start:end]
        for attempt in (raw, raw.replace('\\"', '"')):
            try:
                units = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(units, list) and (
                not units or (isinstance(units[0], dict) and "unitId" in units[0])
            ):
                return units
    return None


def _unit_prices(u: dict) -> tuple[int | None, int | None]:
    """(base, total) monthly price for a unit, unfurnished."""
    prices = ((u.get("startingAtPricesUnfurnished") or {}).get("prices")) or {}
    base = prices.get("price")
    total = prices.get("totalPrice")
    return (
        int(base) if isinstance(base, (int, float)) and base else None,
        int(total) if isinstance(total, (int, float)) and total else None,
    )


def _group_units(units_raw: list[dict]) -> list[UnitGroup]:
    buckets: dict[tuple[int | None, int], list[dict]] = defaultdict(list)
    band_centers: dict[int | None, list[int]] = defaultdict(list)
    for u in sorted(
        units_raw,
        key=lambda x: (x.get("bedroomNumber") or 0, x.get("squareFeet") or 0),
    ):
        beds = u.get("bedroomNumber")
        beds = int(beds) if isinstance(beds, (int, float)) else None
        sf = u.get("squareFeet")
        sf = int(sf) if isinstance(sf, (int, float)) else 0
        center = None
        for existing in band_centers[beds]:
            if abs(sf - existing) <= 2 * SF_BAND:
                center = existing
                break
        if center is None:
            center = sf
            band_centers[beds].append(center)
        buckets[(beds, center)].append(u)

    groups: list[UnitGroup] = []
    for (beds, _center), members in buckets.items():
        sfs = [int(m["squareFeet"]) for m in members if m.get("squareFeet")]
        bases: list[int] = []
        totals: list[int] = []
        for m in members:
            b, t = _unit_prices(m)
            if b:
                bases.append(b)
            if t:
                totals.append(t)
        rent_allin_min = rent_allin_max = None
        if totals and bases and (min(totals) != min(bases) or max(totals) != max(bases)):
            rent_allin_min, rent_allin_max = min(totals), max(totals)
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                # Listed = available: every embedded unit is an on-market listing.
                available=len(members),
                coming_soon=0,
                rent_min=min(bases) if bases else None,
                rent_max=max(bases) if bases else None,
                rent_type="base",
                rent_allin_min=rent_allin_min,
                rent_allin_max=rent_allin_max,
                unit_rents=observed_rents(bases),
                baths=observed_baths(m.get("bathroomNumber") for m in members),
                flag_review=not bases or not sfs or beds is None,
            )
        )
    groups.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return groups


def _concession(units_raw: list[dict], html: str) -> tuple[int | None, str | None, bool]:
    """(weeks, raw, unit_scoped). A unit's promotions/concessions are a UNIT-level source. (the comp rule, 2026-09-26): a unit-level count is the community's only when EVERY
    listed unit carries a countable promotion with the SAME count; otherwise the page banner's
    count; otherwise NULL with the unit offers kept as raw text. The first countable unit
    promotion used to win outright, so unit order decided every unit's NER.

    `unit_scoped` is True when weeks were refused on that rule: the pipeline must not re-derive a
    count from the unit raw (merge_concession), while its landing-page sweep still reads the
    rendered Specials panel -- the page banner the rule falls back to.
    """
    per_unit: list[dict[int, str]] = []   # each unit's countable offers: count -> raw
    offers: list[str] = []                 # distinct countable unit offers, first-seen order
    fallback_raw: str | None = None
    for u in units_raw:
        mine: dict[int, str] = {}
        for key in ("promotions", "concessions"):
            for item in u.get(key) or []:
                text = item if isinstance(item, str) else json.dumps(item)
                weeks, raw = _scan_concession_text(text)
                # a past bug: a per-floorplan promo yields raw with NO weeks; keep the first
                # uncountable banner as a fallback and keep looking for a countable one.
                if weeks is not None:
                    mine.setdefault(weeks, raw)
                    if raw not in offers:
                        offers.append(raw)
                elif raw and fallback_raw is None:
                    fallback_raw = raw
        per_unit.append(mine)
    # The counts EVERY listed unit carries (a unit's promotions all apply to it, so a bonus
    # some units add never cancels the offer they all share -- Codex re-review P2); the
    # largest of them is site-level.
    shared = set.intersection(*(set(m) for m in per_unit)) if per_unit else set()
    if shared:
        n = max(shared)
        return n, per_unit[0][n], False
    # Fall back to the page banner text.
    page_text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    weeks, raw = _scan_concession_text(page_text)
    if weeks is not None:
        return weeks, raw, False
    if offers:
        return None, " | ".join(offers), True
    # Nothing countable anywhere. Keep a banner, preferring the unit-level one -- that
    # is the precedence this function has always had.
    return None, fallback_raw or raw, False


def _scan_concession_text(text: str) -> tuple[int | None, str | None]:
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, CONCESSION_WEEKS_RE, CONCESSION_MONTHS_RE)
