"""Adapter for AIR Communities sites (Apartment Income REIT's own site platform).

Signal: the platform's asset path `/etc.clientlibs/apartmentIncomeReit/` on the home page. Those
pages also reference Entrata, so before this adapter they matched `entrata`, which reads nothing,
and fell to the LLM tier -- whose page resolver handed it ONE floor-plan page. Across the 25
confirmed AIR communities (DEN, LAS, MCO, TPA, ATL) that stored 92 listed units against ~635 on
the sites, and on 5 LAS sites every unit beds=null: that page's visible text never states a bed
count (beds_null_groups, 2026-09-30).

The data is structured and complete (survey 2026-09-30: 197 of 197 plan pages, 635 units, JSON-LD
count == visible count on every page):
  * plans: every `/floor-plan/<n>-bedroom|studio/<design>.html` linked from `/residences.html` or
    the home page (3 of the 25 answer /residences.html with 404; the home page links premier
    variants /residences.html omits);
  * each plan page's schema.org JSON-LD: a `FloorPlan` (name, numberOfBedrooms,
    numberOfBathroomsTotal, floorSize) and an `ItemList` "Available Units" of `Apartment`s
    (name, floorSize, description "Unit #5-201, Starting at $1222.0").
Each listed unit's price is an observed rent -> unit_rents. The page's words decide the labels:
"Starting at" per unit -> rent_basis starting_at; "We roll required monthly fees into one clear
price" -> rent_type allin (unspecified when a site does not say it). Beds come from the page's
own numberOfBedrooms, never from the URL, price or size. A plan listing no unit is kept as an
advertised plan at available=0.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "air_communities"
SIGNAL = "/etc.clientlibs/apartmentIncomeReit/"
# a multi-phase site prefixes one phase's plans with its property id:
# /100163135/floor-plan/1-bedroom/design-1a-o.html (TPA Crosstown Walk, 14 of 46 units)
_PLAN_PATH_RE = re.compile(r"^(?:/\d+)?/floor-plan/[^/]+/[^/]+\.html$", re.I)
_PRICE_RE = re.compile(r"Starting at \$\s*([\d,]+(?:\.\d+)?)", re.I)
_ALLIN_RE = re.compile(r"one clear price|roll required monthly fees", re.I)


def has_signal(html: str) -> bool:
    return SIGNAL.lower() in (html or "").lower()


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


def _num(v) -> float | None:
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return n if n == n else None           # NaN is not a number here


def _plan_links(html: str, base: str) -> list[str]:
    out = []
    for a in BeautifulSoup(html or "", "html.parser").find_all("a", href=True):
        u = urljoin(base + "/", a["href"].split("#")[0])
        p = urlparse(u)
        if p.netloc == urlparse(base).netloc and _PLAN_PATH_RE.match(p.path):
            out.append(f"{p.scheme}://{p.netloc}{p.path}")
    return out


def _ld_nodes(html: str) -> list[dict]:
    nodes = []
    for sc in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict):
                nodes += [n for n in (d.get("@graph") or [d]) if isinstance(n, dict)]
    return nodes


_NAME_FILLER = {"the", "at", "of", "apartments", "apartment", "apts", "homes", "townhomes", "and"}


def _name_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _NAME_FILLER}


def _names_community(html: str, name: str) -> bool:
    """True when the page's ApartmentComplex JSON-LD name or <title> carries every word of the
    community's name ("Aldridge At Town Village" in "Aldridge at Town Village - Marietta, GA -
    Home"). An empty name can identify nothing, so it never vouches for a move."""
    want = _name_tokens(name)
    if not want:
        return False
    labels = [str(n.get("name") or "") for n in _ld_nodes(html) if n.get("@type") == "ApartmentComplex"]
    t = re.search(r"<title[^>]*>([^<]*)", html or "", re.I)
    labels += [t.group(1)] if t else []
    return any(want <= _name_tokens(lbl) for lbl in labels)


def _same_zip(html: str, address: str) -> bool:
    """True when the page's ApartmentComplex postalCode is the ZIP in the community's address.
    A name is not identity ("Capri" is in "Capri North"; two properties can share a name --
    Codex P1), so a host move also needs this. No ZIP on either side: refused."""
    want = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    if not want:
        return False
    for n in _ld_nodes(html):
        if n.get("@type") == "ApartmentComplex" and isinstance(n.get("address"), dict):
            got = str(n["address"].get("postalCode") or "")[:5]
            if got and got == want[-1]:
                return True
    return False


def _fail(msg: str) -> ExtractResult:
    return ExtractResult(scrape_error=f"{NAME}: {msg}"[:300], extractor_used=NAME, flag_review=True)


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static
    from rentdb.concessions import scan_concession

    url = (community.get("url") or "").strip()
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return _fail("no community URL")
    base = f"{p.scheme}://{p.netloc}"
    host = p.netloc.lower()

    def same_host(u: str | None) -> bool:
        return bool(u) and urlparse(u).netloc.lower() == host

    # The HOME page's own redirect is the community's move, not a foreign page: 3 of the 25
    # (Citi Lakes, Solace Timacuan, Aldridge) moved domain -- www.citilakes.com/ answers at
    # www.citilakesapartments.com/, the very page dispatch matched as AIR. Its final host becomes
    # the host every later page must stay on (live re-scrape 2026-09-30).
    for _attempt in (1, 2):
        try:
            first = fetch_static(base + "/")
        except Exception:
            continue
        if first.status < 400:
            moved = urlparse(getattr(first, "final_url", None) or base + "/")
            if moved.scheme and moved.netloc and moved.netloc.lower() != host:
                # ...but only when the destination NAMES this community:
                # a stale domain can redirect to a SISTER AIR property, whose plans would then
                # pass every same-host check and be stored as this community's rents.
                if not (_names_community(first.html, community.get("name") or "")
                        and _same_zip(first.html, community.get("address") or "")):
                    return _fail(f"home page redirect to {moved.netloc} does not name "
                                 f"{community.get('name')!r} at its ZIP, refusing another property's read")
                base, host = f"{moved.scheme}://{moved.netloc}", moved.netloc.lower()
            break

    def get(url: str):
        """The page, "404" when the site says it is gone, or None when it could not be read
        (after ONE retry). A page that ENDS on another host is unreadable, never this
        community's."""
        for _attempt in (1, 2):
            try:
                page = fetch_static(url)
            except Exception:
                continue
            if page.status == 404:
                return "404"
            if page.status < 400 and same_host(getattr(page, "final_url", None) or url):
                return page
        return None

    # Every failure except a 404 fails the WHOLE read: a partial read stored as live_data would
    # replace a complete snapshot and stop the chain.
    home = get(base + "/")
    if not hasattr(home, "html"):
        return _fail(f"home page unreadable, refusing a partial read: {base}/")
    residences = get(base + "/residences.html")          # 404 on 3 of the 25 sites: fine
    if residences is None:
        return _fail(f"/residences.html unreadable, refusing a partial read: {base}/residences.html")
    texts = [home.html] + ([residences.html] if hasattr(residences, "html") else [])
    plan_urls = [u for h in texts for u in _plan_links(h, base)]
    plan_urls = list(dict.fromkeys(plan_urls))
    if not plan_urls:
        return _fail("no /floor-plan/ pages linked from home or /residences.html")

    units: list[UnitGroup] = []
    seen_plans = set()
    for plan_url in plan_urls:
        page = get(plan_url)                              # 404 = the site removed that plan
        if page == "404":
            continue
        if page is None:
            return _fail(f"plan page unreadable after a retry, refusing a partial read: {plan_url}")
        nodes = _ld_nodes(page.html)
        fp = next((n for n in nodes if n.get("@type") == "FloorPlan"), None)
        items = next((n for n in nodes if n.get("@type") == "ItemList"), None)
        if fp is None:
            return _fail(f"plan page unreadable (no FloorPlan JSON-LD), refusing a partial read: {plan_url}")
        # the plan's OWN canonical url: must be this host, and names the plan once however it
        # was linked -- a prefixed or re-cased alias of one plan is not a second plan
        fp_url = str(fp.get("url") or fp.get("@id") or plan_url).split("#")[0]
        if not same_host(fp_url):
            # a plan this site links but whose own record names another host: not this
            # community's, and skipping it would store a partial read
            return _fail(f"plan page's own JSON-LD names another host ({fp_url}), refusing a partial read: {plan_url}")
        # an ALIAS is the same canonical AND the same units; two phases that share a generic
        # canonical but list different units are two plans
        unit_names = tuple(sorted(str(((el or {}).get("item") or {}).get("name") or "")
                                  for el in (items or {}).get("itemListElement") or []))
        key = (urlparse(fp_url).path.lower(), unit_names)
        if key in seen_plans:
            continue
        seen_plans.add(key)
        texts.append(page.html)
        beds = _num(fp.get("numberOfBedrooms"))
        plan_sf = _num((fp.get("floorSize") or {}).get("value"))
        rents, sizes = [], []
        listed = 0
        for el in (items or {}).get("itemListElement") or []:
            apt = (el or {}).get("item") or {}
            size = apt.get("floorSize") or {}
            listed += 1
            m = _PRICE_RE.search(str(size.get("description") or apt.get("description") or ""))
            price = _num(m.group(1)) if m else None
            if price and price > 0:
                rents.append(price)
            sf = _num(size.get("value"))
            if sf and sf > 0:
                sizes.append(int(round(sf)))
        if not sizes and plan_sf and plan_sf > 0:
            sizes = [int(round(plan_sf))]
        units.append(UnitGroup(
            beds=int(beds) if beds is not None else None,
            sf_min=min(sizes) if sizes else None,
            sf_max=max(sizes) if sizes else None,
            available=listed,
            rent_min=int(round(min(rents))) if rents else None,
            rent_max=int(round(max(rents))) if rents else None,
            rent_type="unspecified",           # set below from the site's own words
            rent_basis="starting_at" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths([_num(fp.get("numberOfBathroomsTotal"))]),
            plan_name=(str(fp.get("name") or "").strip() or None),
            flag_review=bool(listed) and len(rents) < listed,
        ))

    if not units:
        return _fail("no plan page of this community carried a FloorPlan")
    all_text = " ".join(BeautifulSoup(h, "html.parser").get_text(" ", strip=True) for h in texts[:2])
    allin = any(_ALLIN_RE.search(h) for h in texts)
    for g in units:
        g.rent_type = "allin" if allin else "unspecified"
    concession_weeks, raw_concession_text = scan_concession(all_text)
    return ExtractResult(
        units=units,
        concession_weeks=concession_weeks,
        raw_concession_text=raw_concession_text,
        extractor_used=NAME,
        flag_review=any(g.flag_review for g in units),
    )
