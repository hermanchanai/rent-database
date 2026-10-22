"""Adapter for Rise48 Communities' own site platform (rise48communities.com) -- ledger.

Since ~2026-10 every Rise48 vanity domain (www.risecamelback.com, ...) answers with a 301 to
https://rise48communities.com/<st>/<city>/<slug>, a Next.js app. 42 confirmed communities (PHX 28,
DFW 11, CLT 3, survey 2026-10-08). The page carries no Entrata / SightMap / RentCafe / Knock
signal, so `detect_vendors` returned [] and all of them fell to the paid LLM tier.

Signal: the page's `<link rel="canonical">` on rise48communities.com. Static fetches follow the
301, so dispatch sees the operator's page under the vanity URL the manifest stores.

Data (server-rendered, no XHR): `<canonical>/floor-plans` prints every plan card and every listed
unit row, and its React Server Components flight payload (`self.__next_f.push`) carries the same
records as JSON: `model = {plans: [{floorplan: {name, beds, baths, sqft_min, sqft_max,
monthly_fees, units: [<raw feed>]}, units: [{homeNumber, price, moveLabel}]}], totalUnits,
planCount}`. The entry's own `units` are the rows the page LISTS (they sum to totalUnits); the
floorplan's feed also holds homes months out that the page does not list, so it is never read --
a plan without its row list fails the read.
One UnitGroup per plan (the site's own plan; sibling plans share beds and SF: "A1 Classic" /
"A1 Silver" / "A1 Platinum"). Each listed unit's price -- the rent the row prints, "Base rent
... $675 PER MONTH" -- is an observed rent -> unit_rents (rent_type base, rent_basis actual). The
plan card prints "Required monthly fees $125 | Estimated monthly total $800" = base + fees, so the
all-in range is the base range plus that plan's fee. A "Waitlist" plan lists no unit: kept at
available=0, unpriced (its card price is not a listed unit's rent). The special is the
`section.pc-promo` banner, normalised by the shared `scan_concession`.

Refusals (a wrong rent is worse than none): a page whose canonical leaves rise48communities.com, a
redirect to a property whose page does not NAME this community (a stale vanity domain can point
at a sister property, often in the same ZIP), a floor-plans page without the payload. The page's own
`totalUnits` disagreeing with the rows read, or `pricingDegraded: true`, flags every group. A
complete payload listing nothing (totalUnits 0, every plan Waitlist) is an affirmed zero:
status no_availability, so the chain stops instead of handing the page to the LLM tier.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import STATUS_NOAVAIL, STATUS_URL_DEAD, ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "rise48"
HOST = "rise48communities.com"
_CANON_RE = re.compile(
    r'<link[^>]+rel="canonical"[^>]+href="https://(?:www\.)?rise48communities\.com(/[a-z]{2}/[^/"]+/[^/"#?]+)[^"]*"',
    re.I)
_PUSH_RE = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.S)
_NAME_FILLER = {"the", "at", "on", "of", "apartments", "apartment", "apts", "homes", "and"}


def has_signal(html: str) -> bool:
    return bool(_CANON_RE.search(html or ""))


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


def _fail(msg: str) -> ExtractResult:
    return ExtractResult(scrape_error=f"{NAME}: {msg}"[:300], extractor_used=NAME, flag_review=True)


def _moved(why: str) -> ExtractResult:
    """A FINAL url_dead: the community's URL no longer serves this community (it lands on a
    sister property). Terminal, like rentcafe_marketplace's moved listing: handed on, the
    Tier-2 render would read the same sister page and the LLM store its rents as this
    community's. Nothing is stored as a unit."""
    return ExtractResult(scrape_error=f"{NAME}: community moved: {why}"[:300], extractor_used=NAME,
                         flag_review=True, status=STATUS_URL_DEAD)


def _flight(html: str) -> str:
    out = []
    for raw in _PUSH_RE.findall(html or ""):
        try:
            item = json.loads(raw)
        except ValueError:
            continue
        if len(item) > 1 and isinstance(item[1], str):
            out.append(item[1])
    return "".join(out)


def _find_model(o):
    if isinstance(o, dict):
        if isinstance(o.get("plans"), list) and "totalUnits" in o:
            return o
        o = list(o.values())
    if isinstance(o, list):
        for v in o:
            r = _find_model(v)
            if r is not None:
                return r
    return None


def _model(html: str) -> dict | None:
    """The floor-plans page's `model` record, decoded from the ONE flight row that carries it."""
    for line in _flight(html).split("\n"):
        if '"totalUnits"' not in line or '"plans"' not in line:
            continue
        _key, _, payload = line.partition(":")
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        m = _find_model(data)
        if m is not None:
            return m
    return None


def _ld_complex(html: str) -> dict:
    for sc in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or sc.get_text() or "")
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            for n in ((d.get("@graph") or [d]) if isinstance(d, dict) else []):
                if isinstance(n, dict) and n.get("@type") == "ApartmentComplex":
                    return n
    return {}


def _tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _NAME_FILLER]


_STREET_ABBR = {
    "drive": "dr", "road": "rd", "avenue": "ave", "av": "ave", "street": "st", "boulevard": "blvd",
    "lane": "ln", "court": "ct", "circle": "cir", "parkway": "pkwy", "place": "pl", "highway": "hwy",
    "trail": "trl", "terrace": "ter", "square": "sq", "point": "pt", "pointe": "pt",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}


def _street_key(street: str) -> tuple[str, ...]:
    """A street line as comparable tokens: lower-case, punctuation dropped, USPS suffix and
    directional words abbreviated ("2379 Apollo Road" == "2379 Apollo Rd", "5038 North 55th
    Avenue" == "5038 N 55th Ave"). Empty unless it starts with a house number -- a street name
    alone identifies nothing."""
    toks = [_STREET_ABBR.get(t, t) for t in re.findall(r"[a-z0-9]+", (street or "").lower())]
    return tuple(toks) if toks and toks[0].isdigit() else ()


def _same_street_address(node: dict, address: str) -> bool:
    """The page's own streetAddress EQUALS the manifest's street line (and, when both state one,
    the ZIPs agree). Equal, never near: 7200 and 7210 Wallace Rd are two properties."""
    page = node.get("address") if isinstance(node.get("address"), dict) else {}
    mine = _street_key((address or "").split(",")[0])
    if not mine or mine != _street_key(str(page.get("streetAddress") or "")):
        return False
    want_zip = re.findall(r"\b(\d{5})(?:-\d{4})?\b", address or "")
    got_zip = str(page.get("postalCode") or "")[:5]
    return not (want_zip and got_zip) or want_zip[-1] == got_zip


def _is_this_community(html: str, community: dict) -> bool:
    """The page is this community: its ApartmentComplex NAME equals ours, or -- the only other
    path -- its own street address equals the manifest's.

    Names compare as filler-free, space-free strings ("Rise Preserve" / "Rise at the Preserve",
    "Rise North Ridge" / "Rise Northridge") and must be EQUAL, not contained: "Rise at the
    Meadows" is every word of "Rise at Highland Meadows" and a different property. A shared ZIP is NOT identity: Rise48 runs several properties per ZIP. The address path exists for rebrands the manifest still carries under the old
    name -- W Flats Apartments = Rise Blue Ridge, 7200 Wallace Rd; Arrowood Crossing = Rise
    Sunridge; Shiloh Oaks = Rise Apollo Heights -- and two properties never
    share a street address."""
    node = _ld_complex(html)
    want = _tokens(community.get("name") or "")
    got = _tokens(str(node.get("name") or ""))
    if want and got and "".join(want) == "".join(got):
        return True
    return _same_street_address(node, community.get("address") or "")


def _int(v) -> int | None:
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return int(round(n)) if n == n else None


def _promo_text(html: str) -> str:
    sec = BeautifulSoup(html or "", "html.parser").select_one("section.pc-promo")
    return " ".join(sec.get_text(" ", strip=True).split()) if sec else ""


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static
    from rentdb.concessions import scan_concession

    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return _fail("no community URL")

    def get(u: str):
        """(page, None) on a 2xx/3xx, else (None, why) -- `why` keeps the FETCH cause (HTTP status
        or the transport exception), which base.classify_status reads to record url_dead with its
        persistence-gated retry instead of needs_adapter. A spent
        rate-limit budget is not a fetch failure: it propagates to the pipeline's `throttled`."""
        from rentdb.fetchers import throttle
        why = "no attempt"
        for _attempt in (1, 2):
            try:
                page = fetch_static(u)
            except throttle.DeadlineExceeded:
                raise
            except Exception as e:  # noqa: BLE001 -- transport failure: timeout, DNS, SSL, reset
                why = f"fetch failed ({type(e).__name__}: {e})"
                continue
            if page.status < 400:
                return page, None
            why = f"fetch failed: HTTP {page.status}"
        return None, why

    home, why = get(url)
    if home is None:
        return _fail(f"community page {why}: {url}")
    m = _CANON_RE.search(home.html or "")
    if not m:
        return _fail(f"no rise48communities.com canonical on {url} (final {getattr(home, 'final_url', url)})")
    base = f"https://{HOST}{m.group(1)}"
    if not _is_this_community(home.html, community):
        return _moved(f"page is {m.group(1)}, which names neither {community.get('name')!r} nor its "
                      f"street address -- not this community")

    fp, why = get(base + "/floor-plans")
    if fp is None:
        return _fail(f"floor-plans page {why}: {base}/floor-plans")
    # The floor-plans response is a SECOND page: it must still be this community's -- same host,
    # same path (no redirect to a sister or a portfolio page), and naming this community
    #.
    landed = urlparse(getattr(fp, "final_url", None) or base + "/floor-plans")
    if (landed.netloc.lower().removeprefix("www.") != HOST
            or landed.path.rstrip("/").lower() != (m.group(1) + "/floor-plans").lower()
            or not _is_this_community(fp.html, community)):
        return _moved(f"floor-plans page is {landed.netloc}{landed.path}, not this community's")
    model = _model(fp.html)
    if model is None:
        return _fail(f"no plans payload on {base}/floor-plans")

    degraded = '"pricingDegraded":true' in _flight(fp.html).replace(" ", "")
    groups: list[UnitGroup] = []
    read = 0
    for entry in model.get("plans") or []:
        plan = (entry or {}).get("floorplan") or {}
        if not plan:
            continue
        seen, rents = [], []
        unpriced = 0
        # The LISTED homes are the rows the page shows -- the plan entry's own `units`
        # (homeNumber, price), which sum to the page's totalUnits. `floorplan.units` is the raw
        # feed and also carries homes months out that the page does not list (Avalon A1 Classic:
        # 7 in the feed, 4 rows on the page, 2026-10-08). No row list at all -> read the feed,
        # flagged, rather than nothing.
        rows = entry.get("units")
        if not isinstance(rows, list):
            # Without the listed rows only the raw feed is left, and it holds homes the page
            # does not list: an unusable parse, never a read.
            return _fail(f"plan {plan.get('name')!r} carries no listed-row list on {base}/floor-plans; "
                         f"refusing the raw feed (it holds unlisted homes)")
        # Every listed ROW is a home. A home number is NOT unique on a multi-building site
        # (Rise Sunridge prints "Home 103" three times in A1 Classic, three move-in dates, three
        # homes), so rows are never de-duplicated by it; the page's own totalUnits is what
        # catches a payload that repeats a row (2026-10-08, live dry run of round 8).
        for u in rows:
            if not isinstance(u, dict) or not (u.get("homeNumber") or u.get("unit_number")):
                unpriced += 1          # a row we cannot read: counted against completeness below
                continue
            seen.append(u)
            r = _int(u.get("price") if "price" in u else u.get("rent"))
            if r and r > 0:
                rents.append(r)
            else:
                unpriced += 1
        read += len(seen)
        fee = _int(plan.get("monthly_fees"))
        lo, hi = (min(rents), max(rents)) if rents else (None, None)
        sizes = [s for s in (_int(plan.get("sqft_min")), _int(plan.get("sqft_max"))) if s and s > 0]
        beds = _int(plan.get("beds"))
        groups.append(UnitGroup(
            beds=beds if beds is not None and beds >= 0 else None,
            sf_min=min(sizes) if sizes else None,
            sf_max=max(sizes) if sizes else None,
            available=len(seen),
            rent_min=lo,
            rent_max=hi,
            rent_type="base",
            rent_allin_min=lo + fee if rents and fee is not None and fee >= 0 else None,
            rent_allin_max=hi + fee if rents and fee is not None and fee >= 0 else None,
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths([plan.get("baths")]),
            plan_name=(str(plan.get("name") or "").strip() or None),
            flag_review=bool(unpriced) or degraded,
        ))
    if not groups:
        return _fail(f"plans payload on {base}/floor-plans lists no plan")
    total = _int(model.get("totalUnits"))
    # An unreadable total (null, a string) leaves completeness unknown: flagged like a mismatch,
    # never a clean read (unknown -> flag_review; Codex round-6 P2 on).
    short = total is None or total != read
    # The plan list must be complete too: a plan entry dropped or unreadable (a Waitlist plan
    # carries no unit, so totalUnits cannot see it) or a planCount that is missing/unreadable
    # leaves the read unreconciled -- flagged, and never an affirmed zero.
    plan_count = _int(model.get("planCount"))
    short = short or plan_count is None or plan_count != len(groups)
    if short:
        for g in groups:
            g.flag_review = True
    # The operator's own complete listing, reconciled to zero (every plan Waitlist, totalUnits 0):
    # an AFFIRMED zero. Stated as no_availability so the chain stops here -- a dry catalog
    # would fall through to the LLM tier, which can invent units.
    affirmed_zero = total == 0 and read == 0 and not degraded and not short

    promo = _promo_text(fp.html) or _promo_text(home.html)
    weeks, raw = scan_concession(promo) if promo else (None, None)
    return ExtractResult(
        units=groups,
        concession_weeks=weeks,
        raw_concession_text=raw,
        extractor_used=NAME,
        flag_review=short or degraded or any(g.flag_review for g in groups),
        status=STATUS_NOAVAIL if affirmed_zero else "",
    )
