"""Adapter for AMH (American Homes 4 Rent) BFR communities.

AMH BFR communities live on communities.amh.com/communities/<slug> — a Webflow
marketing page whose availability is served by a per-community JSON map widget:

    GET https://api.ah4rc.com/mapjson/?community=<slug>

The response is a site-map: `levels[].locations[]`, where each location is either an
amenity (id "amenitycenter", skip) or a home (id like "GA17643"). Each home's
`description` is an HTML popup card carrying the fields we need by CSS class:
    .address          "405 Levon Court"
    .city-state-zip   "Loganville, GA 30052"
    .status           "NOW AVAILABLE" | "COMING SOON" | ...
    .price            "$2500"   (.price.month = "/month")
    .detail.bed       "4"
    .detail.bath      "2.5"
    .detail (sqft)    "2326"

This is a per-community adapter (one API call per community, keyed on the slug in
the seed URL) — NOT a central-inventory fetch. National operator: the same
communities.amh.com/ah4rc pattern serves AMH BFR in every metro, so this adapter
carries to Phoenix/Dallas/Houston/etc. for free.

Rent type: "unspecified" — AMH shows a single monthly number on the map card,
not labeled base vs all-in. Concessions are not published on the map card, so
concession_weeks stays None (unknown from this source), not a fabricated 0.
"""

from __future__ import annotations

import html as _htmlmod
import json
import re
from collections import defaultdict

from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from .base import STATUS_NOAVAIL, ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "amh"
SF_BAND = 50

_SLUG_RE = re.compile(r"/communities/([a-z0-9-]+)", re.I)
_HOME_ID_RE = re.compile(r"^[A-Z]{2}\d+$")   # e.g. GA17643 (skips "amenitycenter")
_INT_RE = re.compile(r"\d[\d,]*")
# Baths need their own pattern: _INT_RE reads "2.5" as 2, and half-baths are real.
_BATH_RE = re.compile(r"\d+(?:\.\d+)?")
_MAPJSON = "https://api.ah4rc.com/mapjson/?community={slug}"


def matches(community: dict) -> bool:
    url = (community.get("url") or "").lower()
    return "communities.amh.com" in url or "ah4rc.com" in url


def _int(s: str | None) -> int | None:
    if not s:
        return None
    m = _INT_RE.search(s)
    return int(m.group(0).replace(",", "")) if m else None


def _bath(s: str | None) -> float | None:
    """'2.5' -> 2.5. The .detail.bath cell is a bare number, no prose."""
    if not s:
        return None
    m = _BATH_RE.search(s)
    return float(m.group(0)) if m else None


def _parse_home(desc_html: str) -> dict | None:
    """Parse one location's popup HTML into a flat home dict, or None if not a listing."""
    soup = BeautifulSoup(desc_html, "html.parser")
    price = soup.select_one(".price")
    addr = soup.select_one(".address")
    if price is None and addr is None:
        return None  # amenity / non-listing card
    status = (soup.select_one(".status").get_text(strip=True) if soup.select_one(".status") else "").upper()
    bed = soup.select_one(".detail.bed")
    bath = soup.select_one(".detail.bath")
    # sqft = the .detail div that is neither bed nor bath (a bare 3-4 digit number)
    sqft = None
    for d in soup.select(".detail"):
        cls = set(d.get("class") or [])
        if "bed" in cls or "bath" in cls:
            continue
        v = _int(d.get_text())
        if v and v >= 300:
            sqft = v
            break
    return {
        "address": addr.get_text(strip=True) if addr else None,
        "status": status,
        "rent": _int(price.get_text()) if price else None,
        "beds": _int(bed.get_text()) if bed else None,
        "baths": _bath(bath.get_text()) if bath else None,
        "sqft": sqft,
    }


def _band_key(sqft: int | None) -> int | None:
    if sqft is None:
        return None
    return round(sqft / SF_BAND) * SF_BAND


def extract(community: dict) -> ExtractResult:
    url = community.get("url") or ""
    m = _SLUG_RE.search(url)
    if not m:
        return ExtractResult(scrape_error="amh: no community slug in url", extractor_used=NAME, flag_review=True)
    slug = m.group(1)
    try:
        resp = cr.get(_MAPJSON.format(slug=slug), impersonate="chrome131", timeout=20)
    except Exception as e:
        return ExtractResult(scrape_error=f"amh: mapjson fetch failed: {type(e).__name__}: {e}"[:200],
                             extractor_used=NAME, flag_review=True)
    if resp.status_code >= 400:
        return ExtractResult(scrape_error=f"amh: mapjson HTTP {resp.status_code}", extractor_used=NAME, flag_review=True)
    try:
        data = resp.json()
    except Exception as e:
        return ExtractResult(scrape_error=f"amh: mapjson JSON parse: {e}"[:200], extractor_used=NAME, flag_review=True)

    locations = []
    levels = data.get("levels") if isinstance(data, dict) else None
    # A feed is only evidence of "nothing listed" when it is SHAPED like one: a `levels` list
    # whose every level carries a `locations` list. A 200 `{}` or an error object is not an
    # empty feed -- it is an unread one.
    feed_well_formed = (isinstance(levels, list) and bool(levels)
                        and all(isinstance(lv, dict) and isinstance(lv.get("locations"), list)
                                for lv in levels))
    for lvl in levels if isinstance(levels, list) else []:
        locations += (lvl.get("locations") or []) if isinstance(lvl, dict) else []
    homes = []
    n_home_locations = 0
    feed_ids: set[str] = set()
    for loc in locations:
        loc_id = str(loc.get("id", ""))
        if not _HOME_ID_RE.match(loc_id):
            continue
        n_home_locations += 1
        h = _parse_home(loc.get("description", "") or "")
        if h and (h["rent"] or h["beds"]):
            h["id"] = loc_id
            homes.append(h)
            # Only a feed home we actually READ shadows its page card:
            # an id whose popup failed to parse must not suppress the card that carries it.
            feed_ids.add(loc_id)

    # Advertised model plans from the community page (zero-avail enumeration,
    # G0.3 convention 2026-07-15): the mapjson widget only carries LISTED homes,
    # so a bed type with nothing on the market vanishes (golden catch: Addison
    # advertises 3/4/5-bed models; the map had one 4-bed home).
    page_html = _community_page(url)
    if page_html is not None and not _is_this_community(page_html, slug):
        # A stale URL redirected to a generic AMH page, a sister community or a challenge
        # page. None of it is this community's evidence: no models,
        # no cards, and above all no affirmed zero. Unknown, never zero.
        page_html = None
    models = _advertised_models(url, page_html)

    # THE PAGE'S HOME CARDS JOIN THE FEED. The feed is not the whole inventory: TPA
    # One Tampa community's was empty while its page server-rendered ten priced homes, and MCO
    # Windward Hills' feed carried 3 of the page's 6 (2026-09-28 survey, 7 of 68 communities
    # short). Both sources name a home by the same listing id, so a card whose id the feed
    # already holds is skipped -- the feed stays authoritative for the homes it carries.
    cards = _page_homes(page_html)
    homes += [h for h in cards if h["id"] not in feed_ids]
    # A feed home keeps the feed's rent / beds / SF, but the page card's explicit "Coming
    # soon!" badge still sets its status (Codex P2): the feed popup prints NOW
    # AVAILABLE on every home -- even ones pre-leasing for January -- so it cannot tell.
    soon_ids = {c["id"] for c in cards if c["status"] == "COMING SOON"}
    for h in homes:
        if h.get("id") in soon_ids and h["id"] in feed_ids:
            h["status"] = "COMING SOON"

    # AN AFFIRMED ZERO. A result with no rent and no availability otherwise reads as a
    # dry catalog, the chain falls to the LLM, and on Hoke O' Kelly (ATL, 2026-09-27) llm_luna
    # stored 5 + 3 "available" off a page listing none. So when the site's own sources BOTH say
    # nothing is listed, amh says so explicitly (status=no_availability) and the chain stops.
    # BOTH, because the feed alone is NOT the inventory: a Tampa community's feed was empty the
    # same day its community page server-rendered four priced home cards (LAS Serene Park one).
    # So: the feed holds no home location (entries that fail to parse are our miss), AND the
    # community page (its canonical link names this slug) renders no home card. A page we could not
    # fetch is unknown, never zero.
    # The card test is the listing-id element, not its price: a home
    # listed without a price is still listed. And the #Available-Homes section must be
    # THERE -- a page whose layout moved holds no cards because we cannot find them.
    affirmed_zero = (feed_well_formed and n_home_locations == 0 and page_html is not None
                     and _listing_cards(page_html) == [])

    if not homes and not models:
        # Community fetched fine but has no listed homes -> genuinely no availability.
        return ExtractResult(units=[], extractor_used=NAME,
                             status=STATUS_NOAVAIL if affirmed_zero else "")

    # Group by beds + SF band. available = "NOW AVAILABLE"/"AVAILABLE"; coming_soon = "COMING SOON".
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for h in homes:
        groups[(h["beds"], _band_key(h["sqft"]))].append(h)

    units: list[UnitGroup] = []
    for (beds, _sf_band), members in sorted(groups.items(), key=lambda kv: (kv[0][0] or 0, kv[0][1] or 0)):
        avail = sum(1 for h in members if "AVAILABLE" in h["status"])
        soon = sum(1 for h in members if "COMING SOON" in h["status"] or "SOON" in h["status"])
        # any home neither available nor coming-soon (e.g. leased/pending) -> not counted in either
        rents = [h["rent"] for h in members if h["rent"]]
        sfs = [h["sqft"] for h in members if h["sqft"]]
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=avail,
            coming_soon=soon,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="unspecified",
            # AMH lists individual HOMES, so one entry per home already.
            unit_rents=observed_rents(rents),
            baths=observed_baths(h.get("baths") for h in members),
            # a listed home with no price is null rent PLUS review, never clean live_data
            #
            # Only LEASABLE homes count: a leased/pending record is not inventory.
            flag_review=any(not h["rent"] for h in members
                            if "AVAILABLE" in h["status"] or "SOON" in h["status"]),
        ))

    # Any advertised bed type with no listed home joins as available=0.
    covered = {u.beds for u in units if u.beds is not None}
    by_beds: dict[int, list[int]] = defaultdict(list)
    for beds, sf in models:
        if beds not in covered and sf:
            by_beds[beds].append(sf)
    for beds, sfs in sorted(by_beds.items()):
        units.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs),
            sf_max=max(sfs),
            available=0,
            coming_soon=0,
            rent_min=None,
            rent_max=None,
            rent_type="unspecified",
            flag_review=True,   # advertised model only — nothing listed
        ))
    units.sort(key=lambda g: (g.beds if g.beds is not None else -1, g.sf_min or -1))
    return ExtractResult(units=units, extractor_used=NAME,
                         flag_review=any(u.flag_review and (u.available or u.coming_soon)
                                         for u in units),
                         status=STATUS_NOAVAIL if affirmed_zero else "")


_MODEL_BEDS_RE = re.compile(r"(\d+)\s*-\s*bed", re.I)


# A home card the community page server-renders: `<div data-listing-id="FL19610 - ...">`
# under #Available-Homes, holding `<div class="listing-price">$ 2740 /month` beside its
# address, beds and sqft (a Tampa community, 2026-09-27).
_CANONICAL_RE = re.compile(r'<link\b[^>]*\brel="canonical"[^>]*>', re.I)
_HREF_RE = re.compile(r'\bhref="([^"]*)"', re.I)


def _is_this_community(page_html: str, slug: str) -> bool:
    """True when the page's canonical link is /communities/<slug> (every saved AMH page
    carries one). A redirect target or a challenge page is not this community's page."""
    tag = _CANONICAL_RE.search(page_html)
    m = _HREF_RE.search(tag.group(0)) if tag else None
    return bool(m) and m.group(1).rstrip("/").lower().endswith("/communities/" + slug.lower())


def _listing_cards(page_html: str | None):
    """The #Available-Homes `[data-listing-id]` cards, [] when the section holds none,
    None when there is no such section to read (unknown, never zero)."""
    if not page_html:
        return None
    soup = BeautifulSoup(page_html, "html.parser")
    section = soup.select_one("#Available-Homes")
    if section is None:
        return None
    return section.select("[data-listing-id]")


def _card_status(card) -> str:
    """'COMING SOON' when the card shows the site's own coming-soon badge, else 'NOW AVAILABLE'.

    a Tampa community 2026-09-27: six of ten cards print
    `<div class="listing-coming-soon-pill"><div class="listing-pill-text coming-soon">Coming
    soon!</div><div class="listing-pill-text date">Oct 7</div>` -- the explicit amh label
    the project rules reserves coming_soon for (the feed path reads the same label off `.status`).
    Every other pill ('New construction', 'AMH Prelease \u2022 Available Oct 21') is a listed
    home and stays available. A pill Webflow hides (`w-condition-invisible`, on it or an
    ancestor inside the card) is not shown to anyone and says nothing.
    """
    for pill in card.select(".listing-coming-soon-pill"):
        chain = [pill]
        for el in pill.parents:
            if el is card:
                break
            chain.append(el)
        hidden = any("w-condition-invisible" in (el.get("class") or []) for el in chain)
        if not hidden and "coming soon" in pill.get_text(" ", strip=True).lower():
            return "COMING SOON"
    return "NOW AVAILABLE"


def _page_homes(page_html: str | None) -> list[dict]:
    """The homes the community page lists under #Available-Homes, as `_parse_home` dicts.

    Each is `<div data-listing-id="FL00000 - Example Community">` holding `.listing-price`
    ("$ 2740 /month"), `.listing-bed` / `.listing-bath` / `.listing-sqft` (a number then a
    unit word) and the street as the first plain div after the price. Every card is in the
    "Available homes" section, so each is listed: available, unless the card badges itself
    "Coming soon!" (`_card_status`,). A card with neither rent nor beds is not a listing
    and is dropped, as in the feed.
    """
    homes = []
    for card in _listing_cards(page_html) or []:
        price = card.select_one(".listing-price")
        home_id = (card.get("data-listing-id") or "").split(" ", 1)[0]

        def spec(cls: str, card=card) -> str | None:
            el = card.select_one(f".{cls} .listing-spec")
            return el.get_text(strip=True) if el else None

        addr = price.find_next_sibling("div") if price is not None else None
        h = {
            "id": home_id,
            "address": addr.get_text(strip=True) if addr else None,
            "status": _card_status(card),
            # A card without a price is a listed home whose rent we do not know: available,
            # rent None -- never dropped (missing rent beats a missing home).
            "rent": _int(price.get_text()) if price is not None else None,
            "beds": _int(spec("listing-bed")),
            "baths": _bath(spec("listing-bath")),
            "sqft": _int(spec("listing-sqft")),
        }
        if h["rent"] or h["beds"]:
            homes.append(h)
    return homes


def _community_page(url: str) -> str | None:
    """The community page's HTML, or None when it could not be fetched."""
    try:
        from rentdb.fetchers import fetch_static
        page = fetch_static(url)
    except Exception:
        return None
    return page.html if page.status < 400 else None


def _advertised_models(url: str, page_html: str | None = None) -> list[tuple[int, int | None]]:
    """(beds, sqft) for every advertised model on the community page.

    AMH embeds schema.org SingleFamilyResidence objects (HTML-escaped) with
    'N-bed / N-bath / X sq ft' descriptions + floorSize. Returns [] on any
    failure — enumeration is an enhancement, never a blocker.
    """
    if page_html is None:
        return []
    u = _htmlmod.unescape(page_html)
    models: list[tuple[int, int | None]] = []
    seen: set[tuple[int, int | None]] = set()
    for m in re.finditer(r'\{\s*"@type"\s*:\s*"SingleFamilyResidence"', u):
        depth = 0
        obj_raw = None
        for j in range(m.start(), min(len(u), m.start() + 3000)):
            if u[j] == "{":
                depth += 1
            elif u[j] == "}":
                depth -= 1
                if depth == 0:
                    obj_raw = u[m.start():j + 1]
                    break
        if not obj_raw:
            continue
        try:
            d = json.loads(obj_raw)
        except json.JSONDecodeError:
            continue
        beds_m = _MODEL_BEDS_RE.search(d.get("description") or "")
        if not beds_m:
            continue
        beds = int(beds_m.group(1))
        sf = (d.get("floorSize") or {}).get("value")
        sf = int(sf) if isinstance(sf, (int, float)) else None
        key = (beds, sf)
        if key not in seen:
            seen.add(key)
            models.append(key)
    return models
