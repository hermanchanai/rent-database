"""Adapter for FirstKey Homes community pages (firstkeyhomes.com/rental-community/...).

FirstKey renders its listings client-side from a state blob embedded in every page:
`<script id="s2a.io-state">`, HTML-entity-escaped JSON (&q; = ") whose DB_STATE is a
LokiJS dump. In its "…Branch_local/Search" collection:

  * `get-all-communitiesproperties-by-id:[…{"communityId":"<slug>"}]` — the ids
    (`voyager`) of every home in THIS community;
  * `get-all-properties-by-communities-*` — the homes that carry an `availableAt`,
    i.e. the listed ones: address, bedrooms, bathrooms, area, rent, unitStatus.

A listed home is kept only if its id is in the community's own id list, so a page
template that ever mixes in another community's homes cannot contaminate this one.
unitStatus 3 / 2 / 1 is the page's "Move-in Ready" / "Available Soon" / "Coming Soon";
the last is coming_soon (FirstKey's own label). Each home has its own rent, so
unit_rents is observed. Rents exclude FirstKey's monthly fees ("Plus additional fees").

Added 2026-09-25 for GSP Meadow Springs.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "firstkey"
SF_BAND = 50

_URL_RE = re.compile(r"firstkeyhomes\.com/rental-community/[a-z]{2}/([a-z0-9-]+)", re.I)
_STATE_RE = re.compile(r'<script[^>]*id="s2a\.io-state"[^>]*>(.*?)</script>', re.S)
_ENTITIES = (("&q;", '"'), ("&s;", "'"), ("&l;", "<"), ("&g;", ">"), ("&a;", "&"))


def matches(community: dict) -> bool:
    return bool(_URL_RE.search(community.get("url") or ""))


def _search_docs(html: str) -> list[dict]:
    m = _STATE_RE.search(html or "")
    if not m:
        return []
    raw = m.group(1)
    for ent, ch in _ENTITIES:
        raw = raw.replace(ent, ch)
    state = json.loads(raw)
    db = state.get("DB_STATE")
    db = json.loads(db) if isinstance(db, str) else db
    for col in (db or {}).get("collections", []):
        if col.get("name", "").endswith("Branch_local/Search"):
            return col.get("data") or []
    return []


def _doc(docs: list[dict], *needles: str):
    """The data of the first Search doc whose query key contains every needle."""
    for d in docs:
        key = str(((d.get("_jsonit") or {}).get("_meta") or {}).get("id") or "")
        if all(n in key for n in needles):
            return d.get("data") or []
    return None


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    m = _URL_RE.search(url)
    if not m:
        return ExtractResult(scrape_error="firstkey: not a rental-community url",
                             extractor_used=NAME, flag_review=True)
    slug = m.group(1).lower()
    try:
        page = fetch_static(url)
    except Exception as e:  # noqa: BLE001
        return ExtractResult(scrape_error=f"firstkey: fetch failed: {type(e).__name__}: {e}"[:200],
                             extractor_used=NAME, flag_review=True)
    if page.status >= 400:
        return ExtractResult(scrape_error=f"firstkey: fetch failed: status={page.status}",
                             extractor_used=NAME, flag_review=True)
    try:
        docs = _search_docs(page.html)
    except (ValueError, TypeError, AttributeError) as e:
        return ExtractResult(scrape_error=f"firstkey: state blob unreadable: {e}"[:200],
                             extractor_used=NAME, flag_review=True)
    ours = _doc(docs, "get-all-communitiesproperties-by-id", f'"communityId":"{slug}"')
    listed = _doc(docs, "get-all-properties-by-communities")
    if ours is None or listed is None:
        return ExtractResult(scrape_error="firstkey: community or listing query missing from page state",
                             extractor_used=NAME, flag_review=True)
    ids = {str(d.get("voyager")) for d in ours if d.get("voyager")}

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in listed:
        if str(p.get("voyager")) not in ids or not p.get("availableAt"):
            continue
        area = p.get("area")
        sqft = int(area) if isinstance(area, (int, float)) and area > 0 else None
        beds = p.get("bedrooms")
        groups[(int(beds) if beds is not None else None,
                round(sqft / SF_BAND) * SF_BAND if sqft else None)].append({
            "rent": int(round(p["rent"])) if isinstance(p.get("rent"), (int, float)) else None,
            "sqft": sqft, "baths": p.get("bathrooms"), "soon": p.get("unitStatus") == 1})

    units: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                      kv[0][1] or 0)):
        rents = [x["rent"] for x in members if x["rent"] and x["rent"] > 0]
        sfs = [x["sqft"] for x in members if x["sqft"]]
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=sum(1 for x in members if not x["soon"]),
            coming_soon=sum(1 for x in members if x["soon"]),
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths(x["baths"] for x in members),
            flag_review=not rents,
        ))
    return ExtractResult(units=units, extractor_used=NAME,
                         flag_review=any(u.flag_review for u in units))
