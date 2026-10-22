"""Adapter for Apartments247 (apts247) leasing sites.

The page is drawn by script from a SAME-ORIGIN API:
  GET <origin>/api/v1/community_info/          -> objects[0].api_key (a public widget
                                                   key the site serves every visitor)
  GET <origin>/api/v3/floorplans/all/?api_key= -> plans, each with its listed `units`
Same-origin, so the feed is this property's and no scope check is needed. The key is
never logged or stored; it is re-read every run.

Each unit has its own rent ("$3000"), so unit_rents is observed; listed = available
(a future available_date still counts). A plan with no unit ("Call for details.") is
kept with its SF and no price, for bed-type recall. A plan shows at most
`units_to_show` units (MaxCountStrategy): a plan whose list HITS that cap may hold more,
so its group is flagged — the count is a floor.

Added 2026-09-25 for CHI The Belvedere Apartments.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import urlparse

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "apartments247"
SF_BAND = 50
SIGNAL = "static2.apts247.info"
_MONEY_RE = re.compile(r"\$\s*([\d,]+)")


def has_signal(html: str) -> bool:
    return SIGNAL in (html or "")


def matches(community: dict) -> bool:
    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return False
    try:
        from rentdb.fetchers import fetch_static
        page = fetch_static(url)
    except Exception:
        return False
    return page.status < 400 and has_signal(page.html)


def _money(v) -> int | None:
    m = _MONEY_RE.search(str(v or ""))
    return int(m.group(1).replace(",", "")) if m else None


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _err(msg: str) -> ExtractResult:
    return ExtractResult(scrape_error=f"apartments247: {msg}"[:200], extractor_used=NAME,
                         flag_review=True)


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    u = urlparse((community.get("url") or "").strip())
    if not u.scheme or not u.netloc:
        return _err("no usable community url")
    origin = f"{u.scheme}://{u.netloc}/"
    try:
        info = fetch_static(origin + "api/v1/community_info/")
        if info.status >= 400:
            return _err(f"community_info HTTP {info.status}")
        key = (json.loads(info.html).get("objects") or [{}])[0].get("api_key")
        if not key:
            return _err("community_info carries no api_key")
        fps_resp = fetch_static(origin + f"api/v3/floorplans/all/?api_key={key}")
        if fps_resp.status >= 400:
            return _err(f"floorplans HTTP {fps_resp.status}")
        plans = json.loads(fps_resp.html)
    except (ValueError, TypeError, AttributeError, IndexError) as e:
        return _err(f"unreadable API response: {type(e).__name__}")
    except Exception as e:  # noqa: BLE001 — transport errors
        return _err(f"fetch failed: {type(e).__name__}")
    if not isinstance(plans, list):
        return _err("floorplans response is not a list")

    groups: dict[tuple, dict] = defaultdict(lambda: {"units": [], "plans": []})
    for fp in plans:
        beds = fp.get("bed")
        beds = int(beds) if isinstance(beds, (int, float)) else None
        units = fp.get("units") or []
        cap = fp.get("units_to_show") or 0
        truncated = bool(cap) and len(units) >= cap
        plan_sf = _num(fp.get("sq_ft"))
        if not units:
            band = round(plan_sf / SF_BAND) * SF_BAND if plan_sf else None
            groups[(beds, band)]["plans"].append({"sqft": int(plan_sf) if plan_sf else None,
                                                  "baths": _num(fp.get("bath"))})
            continue
        for un in units:
            sf = _num(un.get("sq_ft")) or plan_sf
            band = round(sf / SF_BAND) * SF_BAND if sf else None
            groups[(beds, band)]["units"].append({
                "rent": _money(un.get("rent")), "sqft": int(sf) if sf else None,
                "baths": _num(un.get("bath")) or _num(fp.get("bath")), "truncated": truncated})

    out: list[UnitGroup] = []
    for (beds, _b), g in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                kv[0][1] or 0)):
        units, plans_ = g["units"], g["plans"]
        rents = [x["rent"] for x in units if x["rent"] and x["rent"] > 0]
        sfs = [x["sqft"] for x in units + plans_ if x["sqft"]]
        out.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=len(units),
            coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths(x["baths"] for x in units + plans_),
            flag_review=(not rents) or any(x["truncated"] for x in units),
        ))
    return ExtractResult(units=out, extractor_used=NAME,
                         flag_review=any(g.flag_review for g in out))
