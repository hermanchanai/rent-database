"""Adapter for RentCafe MARKETPLACE listing pages: www.rentcafe.com/apartments/<st>/<city>/<slug>/default.aspx.

These are RentCafe's own listing site, not the property's website, so the property-site
`rentcafe` adapter cannot read them (it builds `<url>/availableunits` paths). Before this
adapter all 56 such communities fell to the LLM tier, which copied each unit's price RANGE:

    3106   $1,373 - $3,231   Now          <- "Base rent" for ONE unit (LAS Spur, 2026-09-29)

RentCafe prices every unit across lease terms and prints the spread; the high end is a very
short lease. The page's own summary names the standard-lease rent as the LOW end ("1-bedroom
apartments from $1,373"). Stored as the range it was 2.5-3.5x wide (Spur 1bd $1,371-$3,874) and
red on rents_dq.llm_rent_spread_by_host. So each unit's rent here is the LOW end of
its range (or the single price when the page prints one): an observed rent a renter can pay,
so it goes into unit_rents. The lease-term high is never stored.

The page is structured, so nothing is read from free text:
    [data-floorplan-id][data-rent]   one per plan: data-name, data-beds ("1 Bed" / "Studio"),
                                     data-baths, data-size ("819 Sqft" / "1,312 - 1,321 Sqft"),
                                     data-rent (the plan's own range)
    tr.fp-unit                       one per LISTED unit inside its plan: data-unit-rent,
                                     data-unit-size ("Ask for pricing" = listed, unpriced)

"Show more units": the page lists only the first few units of a plan and fetches the rest on
click. Such a plan's count is a FLOOR, so the group is flagged.

The PLAN's own range (data-rent) is never a rent. On ATL Renew Carrollton the unit column is
"Total monthly cost" while the plan lows ($847 / $947 / $1,047) sit on another basis entirely
(its units start at $1,480, and "One Bedroom" has no hidden units at all). So rents come from
LISTED units only; a plan low below every listed unit only flags the group for review.

rent_type follows the plan's own unit-column header: "Base rent" -> base, "Total monthly cost"
-> allin, anything else -> unspecified.

A listing RentCafe has removed redirects to a CITY SEARCH page carrying other communities'
prices (ATL Brighton Way -> Smyrna search). The page's canonical link names that search, not
this listing, so the adapter refuses it rather than read a neighbour's rents as this one's.

Added 2026-09-29 for (found by rents_dq.llm_rent_spread_by_host, 0060).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import STATUS_URL_DEAD, ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "rentcafe_marketplace"

# anchored: `.../default.aspx/availableunits` is not a listing
_URL_RE = re.compile(r"^https?://(?:www\.)?rentcafe\.com/apartments/([a-z]{2})/([^/?#]+)/([^/?#]+)"
                     r"/default\.aspx(?:[?#]|$)", re.I)
_PLAN_SEL = "[data-floorplan-id][data-rent]"
# a sign is captured so "$-1 - $3,231" reads as a sentinel, not as "$3,231"
_MONEY_RE = re.compile(r"\$\s*(-?[\d,]+)")
_INT_RE = re.compile(r"(\d[\d,]*)")
_BEDS_RE = re.compile(r"(\d+)\s*Bed", re.I)
_BATHS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*Bath", re.I)


def matches(community: dict) -> bool:
    return bool(_URL_RE.search((community.get("url") or "").strip()))


def _listing_key(url: str) -> str | None:
    """'<st>/<city>/<slug>' of a marketplace listing URL, lower-case; None otherwise."""
    m = _URL_RE.search(url or "")
    return "/".join(g.lower() for g in m.groups()) if m else None


def _money(text: str | None) -> list[int]:
    out = []
    for v in _MONEY_RE.findall(text or ""):
        try:
            out.append(int(v.replace(",", "")))
        except ValueError:
            continue
    return out


def _low(text: str | None) -> int | None:
    """The LOW end of a unit's "$1,373 - $3,231" (or its single price); None when unpriced.

    Any non-positive endpoint makes the whole reading a sentinel (None): dropping it and
    keeping the survivor would store the lease-term HIGH as the rent ("$0 - $3,231" -> 3231,
    Codex). A reversed range still yields its smaller endpoint."""
    vals = _money(text)
    if not vals or any(v <= 0 for v in vals):
        return None
    return min(vals)


def _sizes(text: str | None) -> list[int]:
    if not text or "sq" not in text.lower():
        return []
    return [int(v.replace(",", "")) for v in _INT_RE.findall(text) if int(v.replace(",", "")) > 0]


def _beds(text: str | None) -> int | None:
    if not text:
        return None
    if "studio" in text.lower():
        return 0
    m = _BEDS_RE.search(text)
    return int(m.group(1)) if m else None


def _baths(text: str | None) -> float | None:
    m = _BATHS_RE.search(text or "")
    return float(m.group(1)) if m else None


def _fail(msg: str) -> ExtractResult:
    return ExtractResult(scrape_error=f"{NAME}: {msg}"[:300], extractor_used=NAME, flag_review=True)


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    key = _listing_key(url)
    if not key:
        return _fail("not a rentcafe.com/apartments listing url")
    try:
        page = fetch_static(url)
    except Exception as e:  # noqa: BLE001
        return _fail(f"fetch failed: {type(e).__name__}: {e}")
    if page.status >= 400:
        return _fail(f"fetch failed: status={page.status}")

    # The listing, or a search page RentCafe redirected a removed listing to? BOTH the URL
    # the fetch ended on and (when present) the page's canonical link must be this listing.
    # A proven redirect-away is a FINAL url_dead: handed on, the LLM tier would read the
    # search page's other communities as this one.
    soup = BeautifulSoup(page.html or "", "html.parser")
    final = getattr(page, "final_url", None) or url
    canon = next((urljoin(final, link.get("href") or "") for link in soup.find_all("link")
                  if "canonical" in [r.lower() for r in (link.get("rel") or [])] and link.get("href")),
                 None)
    for seen in (final, canon):
        if seen is not None and _listing_key(seen) != key:
            return ExtractResult(
                scrape_error=f"{NAME}: listing removed or moved: page is "
                             f"{urlparse(seen).path or seen}, not this listing"[:300],
                extractor_used=NAME, flag_review=True, status=STATUS_URL_DEAD)

    # one element per plan: a responsive copy (same data-floorplan-id) or a plan nested in
    # another is not a second plan
    plans, seen_ids = [], set()
    for p in soup.select(_PLAN_SEL):
        pid = p.get("data-floorplan-id")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)
        plans.append(p)
    if not plans:
        return _fail("listing page carries no floor plans")

    units: list[UnitGroup] = []
    for p in plans:
        rows, seen_units = [], set()
        for r in p.select("tr.fp-unit"):
            # a row belongs to its NEAREST plan element, once per unit id
            uid = r.get("data-unit-id") or id(r)
            if r.find_parent(attrs={"data-floorplan-id": True, "data-rent": True}) is not p or uid in seen_units:
                continue
            seen_units.add(uid)
            rows.append(r)
        lows = [_low(r.get("data-unit-rent")) for r in rows]
        priced = [v for v in lows if v]
        unit_sizes = [s for r in rows for s in _sizes(r.get("data-unit-size"))]
        sizes = unit_sizes or _sizes(p.get("data-size"))
        plan_low = _low(p.get("data-rent"))
        text = " ".join(t.strip().lower() for t in p.find_all(string=True) if t.strip())
        show_more = "show more units" in text
        # what the price column IS comes from its own header cell, never plan prose
        heads = {th.get_text(" ", strip=True).lower() for th in p.select("thead th")}
        # a plan low below every listed unit: a hidden unit, or a figure on another basis
        # (Renew Carrollton) -- either way not a rent we observed, so it only flags
        plan_disagrees = bool(priced and plan_low and plan_low < min(priced))
        rent_type = ("allin" if "total monthly cost" in heads
                     else "base" if "base rent" in heads else "unspecified")
        units.append(UnitGroup(
            beds=_beds(p.get("data-beds")),
            sf_min=min(sizes) if sizes else None,
            sf_max=max(sizes) if sizes else None,
            available=len(rows),
            rent_min=min(priced) if priced else None,
            rent_max=max(priced) if priced else None,
            rent_type=rent_type,
            rent_basis="actual" if priced else "unspecified",
            unit_rents=observed_rents(priced),
            baths=observed_baths([_baths(p.get("data-baths"))]),
            plan_name=(p.get("data-name") or "").strip() or None,
            flag_review=bool(rows) and (show_more or plan_disagrees or len(priced) < len(rows)),
        ))

    from .rentcafe import _extract_concession
    concession_weeks, raw_concession_text = _extract_concession(soup.get_text(" ", strip=True))
    return ExtractResult(units=units, concession_weeks=concession_weeks,
                         raw_concession_text=raw_concession_text, extractor_used=NAME,
                         flag_review=any(u.flag_review for u in units))
