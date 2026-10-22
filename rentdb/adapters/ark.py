"""Adapter for ARK Homes For Rent (arkhomesforrent.com/communities/...).

Site structure (captured April 2026):
  - Each available home is an <li data-property-id="..."> card
  - .card-details__rent .rent      -> "$2,029"
  - .card-details__stats .beds     -> "3"
  - .card-details__stats .baths    -> "3.5"
  - .card-details__stats .sqft     -> "1919"
  - .card-tags span                -> "Special Offer" | "Premium" | "Coming Soon"
  - <a href="/properties/...">     -> individual home address

Concession (page-level): looks for "<N> weeks free" or "<N> months free"
in the full page text. "Special Offer" alone is just a badge, not a concession.

Coverage: community pages on www.arkhomesforrent.com only. One-off branded
sites owned by ARK (e.g., livemabry.com) fall through to Tier 2.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents, local_concession

NAME = "ark"

RENT_RE = re.compile(r"\$\s?([1-9](?:,?\d{3}))(?!\d)")
CONCESSION_WEEKS_RE = re.compile(r"(\d+)\s*weeks?\s*free", re.I)
CONCESSION_MONTHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*months?\s*free", re.I)
SF_BAND = 50  # ±50 sq ft per planning doc §5b


@dataclass
class _RawListing:
    rent: int | None
    beds: int | None
    baths: float | None
    sf: int | None
    status: str  # "available" | "coming_soon" | "unknown"


def matches(community: dict) -> bool:
    url = (community.get("url") or "").lower()
    return "arkhomesforrent.com" in url


def extract(community: dict) -> ExtractResult:
    """Fetch + parse an ARK community page. Returns ExtractResult."""
    # Local import keeps the adapter module importable without running network
    from rentdb.fetchers import fetch_static

    url = community.get("url") or ""
    try:
        fetch = fetch_static(url)
    except Exception as e:
        return ExtractResult(
            scrape_error=f"fetch failed: {type(e).__name__}: {e}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )

    if fetch.status >= 400:
        return ExtractResult(
            scrape_error=f"HTTP {fetch.status}",
            extractor_used=NAME,
            flag_review=True,
        )

    return _parse(fetch.html)


def _parse(html: str) -> ExtractResult:
    soup = BeautifulSoup(html, "html.parser")

    # ------------------------------------------------------------------
    # 1. Raw listings — one per <li data-property-id>
    # ------------------------------------------------------------------
    cards = soup.select("li[data-property-id]")
    raw: list[_RawListing] = []
    for li in cards:
        raw.append(_parse_card(li))

    # Fallback: if the selector missed (e.g., template change), try by rent span
    if not raw:
        for rent_el in soup.select("span.rent"):
            # climb up a few levels to find a card container
            container = rent_el
            for _ in range(8):
                container = container.parent
                if container is None:
                    break
                if container.name in ("li", "article", "div") and (
                    container.select_one(".card-details__stats") is not None
                ):
                    break
            raw.append(_parse_card(container or rent_el.parent))

    # ------------------------------------------------------------------
    # 2. Concession (page-level)
    # ------------------------------------------------------------------
    page_text = soup.get_text(separator=" ", strip=True)
    concession_weeks, raw_concession_text = _extract_concession(page_text)

    # ------------------------------------------------------------------
    # 3. Group by beds + SF band (counts available + coming_soon separately)
    # ------------------------------------------------------------------
    groups = _group_by_beds_and_sf(raw)

    # Flag review if we parsed cards but couldn't get rent/sf on most
    partial_parse = sum(1 for r in raw if r.rent is None or r.sf is None)
    flag_review = False
    if raw and partial_parse / len(raw) > 0.3:
        flag_review = True

    return ExtractResult(
        units=groups,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession_text,
        flag_review=flag_review,
        extractor_used=NAME,
        scrape_error=None if raw else "no listings found",
    )


# ---------------------------------------------------------------------------
# Card parsing
# ---------------------------------------------------------------------------
def _parse_card(card) -> _RawListing:
    if card is None:
        return _RawListing(rent=None, beds=None, baths=None, sf=None, status="unknown")

    rent = _parse_rent(card.select_one("span.rent"))
    stats = card.select_one(".card-details__stats")
    beds = _parse_int(stats.select_one(".beds") if stats else None)
    baths = _parse_float(stats.select_one(".baths") if stats else None)
    sf = _parse_int(stats.select_one(".sqft") if stats else None)
    status = _parse_status(card)
    return _RawListing(rent=rent, beds=beds, baths=baths, sf=sf, status=status)


def _parse_rent(el) -> int | None:
    if el is None:
        return None
    m = RENT_RE.search(el.get_text())
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_int(el) -> int | None:
    if el is None:
        return None
    m = re.search(r"\d+", el.get_text())
    return int(m.group()) if m else None


def _parse_float(el) -> float | None:
    if el is None:
        return None
    m = re.search(r"\d+(?:\.\d+)?", el.get_text())
    return float(m.group()) if m else None


def _parse_status(card) -> str:
    tags_text = " ".join(
        (t.get_text() or "").lower() for t in card.select(".card-tags, .card-tags *")
    )
    if "coming soon" in tags_text:
        return "coming_soon"
    # Anything else (Special Offer, Premium, or no tag) = available
    return "available"


# ---------------------------------------------------------------------------
# Concession extraction
# ---------------------------------------------------------------------------
def _extract_concession(text: str) -> tuple[int | None, str | None]:
    """Return (concession_weeks, raw_text) for the strongest hit in the page."""
    # a past bug: this adapter's own patterns, plus the shared named-bed-type
    # veto. A per-floorplan offer keeps its banner and drops the weeks.
    return local_concession(text, CONCESSION_WEEKS_RE, CONCESSION_MONTHS_RE)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------
def _group_by_beds_and_sf(listings: list[_RawListing]) -> list[UnitGroup]:
    """Bucket listings by (beds, SF band ±50). Per planning §5b.

    Counts `available` and `coming_soon` separately within each band.
    Rent min/max is computed across both (overall market signal for the band).
    """
    # Sort by beds then sf to make banding deterministic.
    sortable = [r for r in listings if r.beds is not None]
    sortable.sort(key=lambda r: (r.beds or 0, r.sf or 0))

    buckets: dict[tuple[int, int], list[_RawListing]] = defaultdict(list)
    # Greedy banding: assign each listing to an existing band if within
    # 2*SF_BAND; otherwise start a new band. Band key is (beds, band_center_sf).
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
        rents = [r.rent for r in items if r.rent is not None]
        avail_count = sum(1 for r in items if r.status == "available")
        soon_count = sum(1 for r in items if r.status == "coming_soon")
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(sfs) if sfs else None,
                sf_max=max(sfs) if sfs else None,
                available=avail_count,
                coming_soon=soon_count,
                rent_min=min(rents) if rents else None,
                rent_max=max(rents) if rents else None,
                unit_rents=observed_rents(rents),
                baths=observed_baths(r.baths for r in items),
                flag_review=not rents or not sfs,
            )
        )

    # Also handle listings where beds is None — single "unknown" group flagged
    unknown = [r for r in listings if r.beds is None]
    if unknown:
        rents = [r.rent for r in unknown if r.rent is not None]
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
                unit_rents=observed_rents(rents),
                baths=observed_baths(r.baths for r in unknown),
                flag_review=True,
            )
        )

    # Stable sort for reproducibility
    groups.sort(key=lambda g: (g.beds or -1, g.sf_min or -1))
    return groups
