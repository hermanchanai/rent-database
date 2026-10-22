"""Per-unit schema.org offers in a page's JSON-LD -- a generic, html-only reader.

Run from `adapters.rendered` on the page the browser rendered for the LLM tier, after every
vendor adapter. 13 of the 731 "no pricing visible" pages in the 2026-10-04 diagnosis carried
offer prices in JSON-LD that nothing read (only `air_communities` parsed JSON-LD, and only on
its own platform). Ten of them are one shape, read here:

    ApartmentComplex {url, accommodationFloorPlan: [FloorPlan {name, numberOfRooms,
                      numberOfBathroomsTotal, floorSize}],
                      makesOffer: [OfferForLease {price, availability, url,
                                   itemOffered: Apartment {identifier,
                                                accommodationFloorPlan: {name}}}]}

(the Swifty site platform, `swifty-media.s3...` assets: Richmond 07b4f286, Mark at 2600
2372c37f, Cedar at The Branch c80e0cea, ... -- the block is injected client-side, so the static
homepage has none). Each offer is ONE listed unit: its price is an observed rent (unit_rents),
the offers per floor plan are that plan's listed count.

What this NEVER reads, by design:
  * a property-level `priceRange` or `AggregateOffer` -- a range is not a unit, and the
    "$1120 - $4075" + "47 available" pair on 22 Repli-template pages is no per-unit data;
  * offers on any node that is not an `ApartmentComplex` (a same-host Organization or
    portfolio node), or an offer whose complex `url` is another host -- operator portfolio pages (Cortland,
    Brookfield) carry other properties' rents; a complex with no `url` cannot vouch either;
  * a hand-authored template block (literal "PASTE-" placeholders: Edenfield Shores' 25
    offers all dated 2026-03/04 under "PASTE-FLOORPLAN-URL-HERE", on a page whose visible text
    says "Call our office") -- a frozen SEO snippet is not today's inventory;
  * plan-level catalog offers with no unit (Tall Timbers' OfferCatalog "1 Bedroom - $1416"):
    whether an advertised plan price with no listed unit may be stored is the open
    rentcafe/Parkwood policy question, not this reader's to settle.

Beds, in order: FloorPlan.numberOfBedrooms; else the words of the plan name or the offer's
plan URL ("studio", "2-bedroom", "1-bed"); else FloorPlan.numberOfRooms ONLY on the Swifty
platform, where it is the bedroom count (120 of 120 offers whose URL states beds agree,
2026-10-05; A1 studio = "0"). A source that disagrees with another skips the offer. An offer
whose beds stay unknown is skipped and the result flagged -- never a beds-null group.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "jsonld_offers"
SWIFTY = "swifty-media.s3"
_OFFER_TYPES = {"Offer", "OfferForLease"}
_GONE = ("outofstock", "soldout", "discontinued")
_BEDS_RE = re.compile(r"(?<![\d.])(\d)\s*-?\s*(?:bed|br\b|bd\b|bedroom)", re.I)
_STUDIO_RE = re.compile(r"\bstudio\b", re.I)


def _host(u: str | None) -> str:
    return (urlparse(u or "").netloc or "").lower().removeprefix("www.")


def _types(n: dict) -> set[str]:
    t = n.get("@type")
    return set(t) if isinstance(t, list) else {t}


def _nodes(html: str) -> list[dict]:
    out = []
    for sc in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict):
                out += [n for n in (d.get("@graph") or [d]) if isinstance(n, dict)]
    return out


def _num(v) -> float | None:
    try:
        n = float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def _words_beds(*texts) -> int | None:
    """Beds the WORDS state, or None; two texts stating different counts -> None."""
    found = set()
    for t in texts:
        t = str(t or "")
        if _STUDIO_RE.search(t):
            found.add(0)
        found |= {int(m.group(1)) for m in _BEDS_RE.finditer(t)}
    return found.pop() if len(found) == 1 else None


def _beds(fp: dict, plan_name: str, offer_url: str, swifty: bool) -> tuple[int | None, bool]:
    """(beds, conflict). Explicit field > words > Swifty numberOfRooms."""
    explicit = _num(fp.get("numberOfBedrooms"))
    path = urlparse(offer_url or "").path
    words = _words_beds(plan_name, path)
    rooms = _num(fp.get("numberOfRooms")) if swifty else None
    cands = [int(x) for x in (explicit, words, rooms) if x is not None]
    if not cands:
        return None, False
    if len(set(cands)) > 1:
        return None, True
    return cands[0], False


def extract_html(community: dict, html: str) -> ExtractResult | None:
    """Per-unit makesOffer rents from `html`, or None when the page has none this reads."""
    if not html or "makesOffer" not in html:
        return None
    if "PASTE-" in html:          # a hand-authored template block, see the module docstring
        return None
    want = _host(community.get("url"))
    swifty = SWIFTY in html
    plans: dict[str, dict] = {}
    skipped = conflicts = 0
    for node in _nodes(html):
        offers = node.get("makesOffer")
        if not isinstance(offers, list) or not offers:
            continue
        if "ApartmentComplex" not in _types(node):
            continue              # an Organization / portfolio node's offers are not this community's
        if not want or _host(node.get("url")) != want:
            continue              # another property's offers, or a complex that names no host
        fps = {str(f.get("name") or "").strip(): f
               for f in node.get("accommodationFloorPlan") or [] if isinstance(f, dict)}
        for o in offers:
            if not isinstance(o, dict) or not (_types(o) & _OFFER_TYPES):
                continue          # AggregateOffer and anything else: never a unit
            if any(g in str(o.get("availability") or "").lower() for g in _GONE):
                continue
            item = o.get("itemOffered") if isinstance(o.get("itemOffered"), dict) else {}
            unit_id = str(item.get("identifier") or item.get("name") or "").strip()
            price = _num(o.get("price"))
            if not unit_id or price is None or price <= 0:
                skipped += 1
                continue
            fp_ref = item.get("accommodationFloorPlan") if isinstance(item.get("accommodationFloorPlan"), dict) else {}
            plan_name = str(fp_ref.get("name") or "").strip()
            fp = fps.get(plan_name, fp_ref)
            beds, conflict = _beds(fp, plan_name, str(o.get("url") or ""), swifty)
            if beds is None:
                skipped += 1
                conflicts += conflict
                continue
            sf = _num((fp.get("floorSize") or {}).get("value")) if isinstance(fp.get("floorSize"), dict) else None
            p = plans.setdefault(plan_name or f"_{beds}", {
                "beds": beds, "sf": int(round(sf)) if sf and sf > 0 else None,
                "baths": _num(fp.get("numberOfBathroomsTotal")), "name": plan_name or None,
                "units": {}})
            if p["beds"] != beds:
                skipped += 1
                continue
            p["units"][unit_id] = price      # one unit listed twice is one unit

    if not plans:
        return None
    units = []
    for p in plans.values():
        rents = sorted(p["units"].values())
        units.append(UnitGroup(
            beds=p["beds"], sf_min=p["sf"], sf_max=p["sf"],
            available=len(rents),
            rent_min=int(round(rents[0])), rent_max=int(round(rents[-1])),
            rent_type="unspecified", rent_basis="unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths([p["baths"]]),
            plan_name=p["name"],
        ))
    note = None
    if skipped:
        note = (f"{NAME}: skipped {skipped} offer(s) with no unit, price or bed count"
                + (f" ({conflicts} with conflicting bed counts)" if conflicts else ""))
    return ExtractResult(units=units, extractor_used=NAME, flag_review=bool(skipped),
                         scrape_error=note)
