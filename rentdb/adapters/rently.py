"""Adapter for homes listed through Rently (self-showing platform).

Some BFR operators publish their available homes ONLY on Rently. The leasing site
links out to one of two Rently pages, both JS shells over a public JSON feed:

  * a MANAGER search page, `https://u<managerID>.rently.com/propertiesSearch2`
    (ResiHome / ResiBuilt ATL: Chandler Station, Rosebud Place, Jodeco Landing,
    Herrington Heights) -> `secure.rently.com/api/properties/searchQuery?managerID=`
  * a COMMUNITY page, `homes.rently.com/apartments-for-rent/<name>/<communityID>`
    (Elmington, HSV Lakeside Residences) -> the renter app's `/communities/<id>/units`

Each feed row is one listed home with its own rent, so `unit_rents` is observed.
Listed = available (2026-07-14); Rently's own "Coming Soon" label is coming_soon.

🔴 A manager ID can be a whole management company. Rows are kept only when the feed's
`manager_name` names the community (a ZIP match is NOT identity —); a feed with
rows and none of them ours is REFUSED with a scrape_error, never written. The
community feed is scoped by its id and needs no such check.

Added 2026-09-25 for five survey comps that had never produced a reading.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import unquote

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "rently"
SF_BAND = 50

_MANAGER_RE = re.compile(r"\bu(\d{3,9})\.rently\.com", re.I)
# The id is the last path segment. The name segment is URL-encoded on the page
# ("Lakeside%20Residences") and carries a space once decoded, so it may hold one.
_COMMUNITY_RE = re.compile(r"homes\.rently\.com/apartments-for-rent/[^/\"'<>\n]{1,120}/(\d{3,9})", re.I)

MANAGER_FEED = ("https://secure.rently.com/api/properties/searchQuery?pc=1&bathrooms=&bedrooms="
                "&min=&max=&managerID={id}&query=&latitude=&longitude=&city=&premium=&ifram=false")
# The renter web app's API gateway. It is only discoverable from a 3.4 MB JS bundle
# (homes.rently.com/assets/*.js), so it is pinned here; if Rently moves it the fetch
# fails with an HTTP error naming this URL rather than reading as "no homes".
COMMUNITY_FEED = ("https://r4vyup271c.execute-api.us-east-1.amazonaws.com/prod/api/communities/"
                  "{id}/units?floorplan_type=all&page={page}&from_web=true")
_MAX_PAGES = 10

_COMING_SOON_RE = re.compile(r"coming\s+soon", re.I)
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")
_GENERIC = {"the", "at", "of", "rental", "rentals", "homes", "home", "townhomes", "townhome",
            "apartments", "residences", "community", "villas"}


def _search_text(html: str) -> str:
    # Lakeside's link sits inside an Outlook safelink, fully percent-encoded.
    return html + "\n" + unquote(unquote(html))


def has_signal(html: str) -> bool:
    if not html:
        return False
    text = _search_text(html)
    return bool(_MANAGER_RE.search(text) or _COMMUNITY_RE.search(text))


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


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _GENERIC}


def _zip(addr: str) -> str | None:
    m = _ZIP_RE.search((addr or "").strip())
    return m.group(1) if m else None


def _is_ours(row: dict, community: dict) -> bool:
    """Only a manager_name that NAMES the community is identity. A ZIP match is not:
    two communities of one manager share ZIPs, and a row naming the OTHER one used to
    pass on its ZIP. Unknown rows are refused, not guessed."""
    name_toks = _tokens(community.get("name") or "")
    return bool(name_toks) and name_toks <= _tokens(row.get("manager_name") or "")


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else None
    except ValueError:
        return None


_SIZELESS_OUTLIER_X = 4
# A size-less home with NO sized same-bed peer has nothing to be 4x of (again,
# 2026-10-18: the 1,738 sf sibling left the feed and `size: 0, price: 23058` stood alone, so
# an earlier change let it through). The only judge left is the dollar ceiling rents_dq applies to a
# no-SF group (migration 0059, $8,000 default); the rently fleet's highest real rent in 120
# days is $4,246.
_SIZELESS_ABS_CEILING = 8_000


def _group(homes: list[dict]) -> list[UnitGroup]:
    """homes: [{beds, baths, sqft, rent, soon}] -> one group per (beds, 50-sf band)."""
    # A SIZE-LESS home priced over 4x its same-bed SIZED siblings is a feed typo, not a rent
    # (1801 Townhomes NSH, `size: 0, price: 23058` beside 1,738 sf at $2,529). No SF means
    # no per-SF test downstream can see it, so it is judged here, against the community's own
    # homes. The home stays listed (available) at rent None -- missing rent beats wrong rent.
    # Only an OBSERVED bed count makes two homes peers: beds=None is not a type.
    sized: dict = defaultdict(list)
    for h in homes:
        if h["beds"] is not None and h["sqft"] and h["rent"] and h["rent"] > 0:
            sized[h["beds"]].append(h["rent"])
    for h in homes:
        peers = sorted(sized.get(h["beds"]) or []) if h["beds"] is not None else []
        if not h["sqft"] and h["rent"]:
            typo = (h["rent"] > _SIZELESS_OUTLIER_X * peers[len(peers) // 2]) if peers \
                else h["rent"] > _SIZELESS_ABS_CEILING
            if typo:
                h["rent"] = None
                h["suppressed"] = True
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for h in homes:
        band = round(h["sqft"] / SF_BAND) * SF_BAND if h["sqft"] else None
        groups[(h["beds"], band)].append(h)
    units: list[UnitGroup] = []
    for (beds, _b), members in sorted(groups.items(), key=lambda kv: (kv[0][0] if kv[0][0] is not None else -1,
                                                                      kv[0][1] or 0)):
        rents = [m["rent"] for m in members if m["rent"] and m["rent"] > 0]
        sfs = [m["sqft"] for m in members if m["sqft"]]
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=sum(1 for m in members if not m["soon"]),
            coming_soon=sum(1 for m in members if m["soon"]),
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths(m["baths"] for m in members),
            # a suppressed price is null PLUS review even when a sibling's price survives
            flag_review=not rents or any(m.get("suppressed") for m in members),
        ))
    return units


def _manager_homes(mid: str, community: dict) -> tuple[list[dict] | None, str | None]:
    from rentdb.fetchers import fetch_static
    url = MANAGER_FEED.format(id=mid)
    resp = fetch_static(url)
    if resp.status >= 400:
        return None, f"rently: manager feed HTTP {resp.status} ({url})"
    try:
        rows = json.loads(resp.html).get("property_data") or []
    except (ValueError, AttributeError) as e:
        return None, f"rently: manager feed not JSON: {e}"[:200]
    rows = [r for r in rows if not r.get("for_sale")]
    ours = [r for r in rows if _is_ours(r, community)]
    if rows and not ours:
        names = sorted({r.get("manager_name") or "?" for r in rows})[:3]
        return None, (f"rently: manager {mid} feed lists {len(rows)} home(s) and none is "
                      f"this community's (managers {names}) — refused, not written")[:200]
    homes = []
    for r in ours:
        fp = r.get("floorplan") or {}
        beds = _num(fp.get("bedrooms"))
        homes.append({"beds": int(beds) if beds is not None else None,
                      "baths": _num(fp.get("bathrooms")),
                      "sqft": int(_num(fp.get("size"))) if _num(fp.get("size")) else None,
                      "rent": int(round(_num(fp.get("rent")))) if _num(fp.get("rent")) else None,
                      "soon": bool(_COMING_SOON_RE.search(r.get("ready_date_text") or ""))})
    return homes, None


def _community_homes(cid: str) -> tuple[list[dict] | None, str | None]:
    from rentdb.fetchers import fetch_static
    homes: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        url = COMMUNITY_FEED.format(id=cid, page=page)
        resp = fetch_static(url)
        if resp.status >= 400:
            return None, f"rently: community feed HTTP {resp.status} ({url})"
        try:
            data = json.loads(resp.html)
        except ValueError as e:
            return None, f"rently: community feed not JSON: {e}"[:200]
        for u in data.get("units") or []:
            beds = _num(u.get("bedrooms"))
            homes.append({"beds": int(beds) if beds is not None else None,
                          "baths": _num(u.get("bathrooms")),
                          "sqft": int(_num(u.get("size"))) if _num(u.get("size")) else None,
                          "rent": int(round(_num(u.get("price")))) if _num(u.get("price")) else None,
                          "soon": False})
        if page >= int(data.get("pages") or 1):
            break
    return homes, None


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static
    url = (community.get("url") or "").strip()
    try:
        page = fetch_static(url)
    except Exception as e:  # noqa: BLE001
        return ExtractResult(scrape_error=f"rently: fetch failed: {type(e).__name__}: {e}"[:200],
                             extractor_used=NAME, flag_review=True)
    if page.status >= 400:
        return ExtractResult(scrape_error=f"rently: fetch failed: status={page.status}",
                             extractor_used=NAME, flag_review=True)
    text = _search_text(page.html)
    mc = _COMMUNITY_RE.search(text)
    mm = _MANAGER_RE.search(text)
    if mc:
        homes, err = _community_homes(mc.group(1))
    elif mm:
        homes, err = _manager_homes(mm.group(1), community)
    else:
        return ExtractResult(scrape_error="rently: no Rently listing link on the page",
                             extractor_used=NAME, flag_review=True)
    if err:
        return ExtractResult(scrape_error=err, extractor_used=NAME, flag_review=True)
    units = _group(homes)
    return ExtractResult(units=units, extractor_used=NAME,
                         flag_review=any(u.flag_review for u in units))
