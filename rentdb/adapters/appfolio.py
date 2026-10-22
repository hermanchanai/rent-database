"""Adapter for AppFolio-embedded listings.

AppFolio sites do not render pricing into their own HTML. They ship AppFolio's
documented embed snippet, which injects an iframe at run time:

    Appfolio.Listing({
      hostUrl: 'metricpropertymanagement.appfolio.com',
      propertyGroup: 'ASNO',
      themeColor: '#676767', height: '500px', width: '100%',
      defaultOrder: 'date_posted'
    });

Because the iframe is JS-injected, a static fetch of the wrapper page contains no
prices at all — which is why Ascent at Northgate (AUS) was `needs_adapter` on every
snapshot it ever had, with the error "no pricing visible". That error was a true
statement about the page we fetched and a false statement about the property, which
publishes 59 priced units. The SNIPPET, though, is static, and it carries everything
needed to address the listings feed directly. No browser required.

🔴 `propertyGroup` is safety-critical, not optional. `hostUrl` alone addresses the
MANAGEMENT COMPANY: `metricpropertymanagement.appfolio.com/listings` with no filter
returns every property Metric runs. Writing that into one community's snapshot is
cross-property contamination of exactly the kind `test_resolve_cross_property.py`
exists to prevent. So a config without a propertyGroup is REFUSED, never guessed at,
and the refusal is what `parse_widget_config` returning None means.

The embed usually lives on an availability/floor-plans subpage rather than the tracked
homepage, so routing generally arrives through `probe.VENDOR_SUBPATHS`.
"""

from __future__ import annotations

import base64
import html as _html
import json
import re
from collections import defaultdict
from urllib.parse import quote

from bs4 import BeautifulSoup

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "appfolio"
SF_BAND = 50   # same bucketing as knock/normalizer

# `Appfolio.Listing({ ... })`. Tolerant of whitespace/newlines and of either quote
# style; the keys are read individually rather than by position, because the snippet's
# key order is author-controlled and does vary between sites.
_WIDGET_RE = re.compile(r"Appfolio\s*\.\s*Listing\s*\(\s*\{(?P<body>.*?)\}\s*\)",
                        re.I | re.S)
_HOST_RE = re.compile(r"""hostUrl\s*:\s*['"]\s*(?P<v>[A-Za-z0-9.-]+\.appfolio\.com)\s*['"]""",
                      re.I)
_GROUP_RE = re.compile(r"""propertyGroup\s*:\s*['"]\s*(?P<v>[^'"]+?)\s*['"]""", re.I)
_ORDER_RE = re.compile(r"""defaultOrder\s*:\s*['"]\s*(?P<v>[^'"]+?)\s*['"]""", re.I)

_RENT_RE = re.compile(r"\$\s*([\d,]+)")
_BEDBATH_RE = re.compile(r"(Studio|\d+)\s*(?:bd)?\s*/\s*([\d.]+)\s*ba", re.I)
_SF_RE = re.compile(r"Square\s*Feet:?\s*([\d,]+)", re.I)
_AVAIL_RE = re.compile(r"Available\s+(Now|\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def parse_widget_config(html: str) -> dict | None:
    """-> {"host", "property_group", "order"} or None when it is not usable.

    None means "do not use this adapter", and it covers two very different cases on
    purpose: no widget on the page at all, and a widget with no propertyGroup. Both
    end the same way — we must not fetch an unscoped portfolio feed.
    """
    if not html:
        return None
    m = _WIDGET_RE.search(html)
    if not m:
        return None
    body = m.group("body")
    host = _HOST_RE.search(body)
    group = _GROUP_RE.search(body)
    if not host or not group:
        # A widget without a property group addresses the whole management company.
        return None
    order = _ORDER_RE.search(body)
    return {"host": host.group("v"),
            "property_group": group.group("v"),
            "order": order.group("v") if order else "date_posted"}


# An AppFolio LISTING link — an application for, or the detail page of, one listed unit
# (PHX Villas at Vesta Views' "Apply Now", 2026-09-25). It names the manager's host but
# no property group, so it is only ever used with the street-address scope below.
_APPLY_RE = re.compile(
    r"https?://([A-Za-z0-9-]+\.appfolio\.com)/listings/"
    r"(?:rental_applications/new\?[^\"'\s<>]*listable_uid=|detail/)", re.I)
# NORMALIZED, never deleted: dropping directionals and
# street types made "100 N Main St, Phoenix" and "100 S Main Ave, Mesa" one key.
_STREET_ABBR = {"north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne",
                "northwest": "nw", "southeast": "se", "southwest": "sw", "road": "rd",
                "street": "st", "avenue": "ave", "drive": "dr", "lane": "ln", "boulevard": "blvd",
                "parkway": "pkwy", "court": "ct", "circle": "cir", "place": "pl",
                "highway": "hwy", "terrace": "ter", "trail": "trl"}
_ZIP5_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\s*$")


def parse_apply_host(html: str) -> str | None:
    m = _APPLY_RE.search(html or "")
    return m.group(1) if m else None


def _street_key(addr: str | None) -> tuple | None:
    """("15620", ("n", "el", "mirage", "rd"), "85378") from "15620 N El Mirage Rd Surprise, AZ 85378" — house number, the street with directionals and types
    NORMALIZED (North -> n, Road -> rd), and the 5-digit ZIP (None if absent, which then
    matches nothing that has one). None without a house number."""
    raw = (addr or "").strip()
    mz = _ZIP5_RE.search(raw)
    # " - Unit 1104" / " - 02107" is the UNIT, not the street ("2422 South Loop 336
    # West - Unit 1104" is Boardwalk Village's 2422 S Loop 336 W). Only a SPACED dash: an
    # unspaced one is a house-number range ("734-828 E Apollo Rd").
    line = re.split(r"[,#]|\s-\s", raw, maxsplit=1)[0].lower()
    toks = [_STREET_ABBR.get(t, t) for t in re.findall(r"[a-z0-9]+", line)]
    if len(toks) < 2 or not toks[0].isdigit():
        return None
    return toks[0], tuple(toks[1:]), (mz.group(1) if mz else None)


def has_signal(html: str) -> bool:
    """Detection for `find_adapters`/`probe`. Deliberately the SAME calls the extractor
    makes, so detection and extraction cannot disagree about what counts as AppFolio —
    a resident-login link to `*.appfolio.com` is not a pricing signal and must not
    route a community here. A LISTING link is: it names a listed unit."""
    return parse_widget_config(html) is not None or parse_apply_host(html) is not None


# The MANAGER's AppFolio database, named without any property scope (2026-10-05).
# Two spellings in the 62 communities of the diagnosis:
#   a resident/owner portal link   https://<pm>.appfolio.com/connect/... | /oportal/...
#   an AppFolio Websites (Duda) widget config, base64 JSON in `data-widget-config`,
#   carrying "database": "<pm>" (the portal link itself is built in JS from it, so a
#   static fetch never sees the link — HOU Boardwalk Village)
# `has_signal` stays strict on purpose: this names a PORTFOLIO, and `extract` only ever
# reads it through the street-address scope, refusing when nothing matches.
_PORTAL_RE = re.compile(r"(?:https?:)?//([a-z0-9-]+)\.appfolio\.com/(connect|oportal)\b", re.I)
_WIDGET_CFG_RE = re.compile(r'data-widget-config="([A-Za-z0-9+/=]{8,})"')
_DB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
# AppFolio's own hosts and its demo databases name no manager.
_NOT_A_MANAGER = re.compile(r"^(?:www|account|app|apm|listings|images|cdn|static|help|demo.*)$", re.I)
_MAX_PORTAL_HOSTS = 3


def parse_portal_hosts(html: str) -> list[str]:
    """Manager databases named on the page, most explicit first: the Websites widget's
    `database`, then tenant-portal (/connect) links, then owner-portal (/oportal) links.
    Each as `<pm>.appfolio.com`. Empty when there is none."""
    found: list[str] = []

    def add(name: str | None):
        name = re.sub(r"<[^>]+>", "", name or "").strip().lower()   # '<p class="rteBlock">x</p>'
        if _DB_NAME_RE.match(name) and not _NOT_A_MANAGER.match(name):
            host = f"{name}.appfolio.com"
            if host not in found:
                found.append(host)

    for m in _WIDGET_CFG_RE.finditer(html or ""):
        try:
            cfg = json.loads(base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
        except Exception:  # noqa: BLE001 — not every widget config is ours
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("database"), str):
            add(cfg["database"])
    links = [(m.group(2).lower(), m.group(1)) for m in _PORTAL_RE.finditer(_html.unescape(html or ""))]
    for kind in ("connect", "oportal"):
        for k, name in links:
            if k == kind:
                add(name)
    return found[:_MAX_PORTAL_HOSTS]


def has_portal_signal(html: str) -> bool:
    """Detection for `find_adapters`, LOW priority: the same call `_resolve_config` makes."""
    return bool(parse_portal_hosts(html))


def listings_url(cfg: dict | None) -> str | None:
    if not cfg:
        return None
    return (f"https://{cfg['host']}/listings"
            f"?filters%5Bproperty_list%5D={quote(cfg['property_group'])}"
            f"&filters%5Border_by%5D={quote(cfg.get('order') or 'date_posted')}")


def _int(value) -> int | None:
    try:
        return int(round(float(str(value).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None


def _card_facts(card) -> dict | None:
    text = card.get_text(" ", strip=True)
    bb = _BEDBATH_RE.search(text)
    if not bb:
        return None
    beds = 0 if bb.group(1).lower() == "studio" else _int(bb.group(1))
    if beds is None:
        return None
    rent = _RENT_RE.search(text)
    sf = _SF_RE.search(text)
    avail = _AVAIL_RE.search(text)
    try:
        baths = float(bb.group(2))
    except (TypeError, ValueError):
        baths = None
    return {
        "beds": beds,
        "baths": baths,
        "rent": _int(rent.group(1)) if rent else None,
        "sf": _int(sf.group(1)) if sf else None,
        # A future date is still a LISTED unit with a published price; AppFolio has no
        # separate coming-soon feed, so it counts as available like every other card.
        "available": bool(avail),
    }


def _cards(pages):
    for page in ([pages] if isinstance(pages, str) else pages):
        yield from BeautifulSoup(page, "html.parser").select(".listing-item")


def parse_listings(html, street: tuple | None = None) -> list[UnitGroup]:
    """The `.listing-item` cards -> UnitGroups, bucketed by beds + SF band.

    `html` is one page or a list of a paginated feed's pages.
    `street` (a `_street_key`) keeps only the cards at that street address."""
    buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for card in _cards(html):
        if street is not None:
            addr = card.select_one(".js-listing-address")
            if addr is None or _street_key(addr.get_text(" ", strip=True)) != street:
                continue
        facts = _card_facts(card)
        if not facts:
            continue
        center = round((facts["sf"] or 0) / SF_BAND) * SF_BAND
        buckets[(facts["beds"], center)].append(facts)

    groups: list[UnitGroup] = []
    for (beds, _), members in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        rents = [m["rent"] for m in members if m["rent"]]
        sfs = [m["sf"] for m in members if m["sf"]]
        groups.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=len(members),
            coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            unit_rents=observed_rents(rents),
            baths=observed_baths(m["baths"] for m in members),
            flag_review=not rents,
        ))
    return groups


def _resolve_config(community: dict):
    """-> (cfg, error). Looks at the community URL, then the availability subpaths."""
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return None, "appfolio: no usable community url"
    tried = [url]
    # A community whose seed URL already IS an AppFolio listings page needs no widget.
    if re.search(r"//[A-Za-z0-9.-]+\.appfolio\.com/listings", url, re.I):
        # No property group here either: a manager's listings URL is its whole portfolio
        # (all 4 of hometrendrentals' homes landed on one community before 2026-09-25).
        # A detail URL is re-read as the manager feed, since one card is not the property.
        host = re.search(r"//([A-Za-z0-9.-]+\.appfolio\.com)", url, re.I).group(1)
        return {"host": host, "property_group": None, "order": "date_posted",
                "direct_url": f"https://{host}/listings", "address_scope": True,
                "paginate": True}, None
    base = url.rstrip("/")
    from .probe import VENDOR_SUBPATHS
    portal_hosts: list[str] = []
    for cand in [url] + [base + sp for sp in VENDOR_SUBPATHS]:
        if cand not in tried:
            tried.append(cand)
        try:
            page = fetch_static(cand)
        except Exception:
            continue
        if page.status >= 400:
            continue
        for h in parse_portal_hosts(page.html):
            if h not in portal_hosts:
                portal_hosts.append(h)
        cfg = parse_widget_config(page.html)
        if cfg:
            return cfg, None
        host = parse_apply_host(page.html)
        if host:
            # No property group to filter on: the MANAGER's whole feed, scoped by street.
            return {"host": host, "property_group": None, "order": "date_posted",
                    "direct_url": f"https://{host}/listings", "address_scope": True,
                    "paginate": True}, None
    if portal_hosts:
        # Only the MANAGER is known: its feed(s), street-scoped, refused on no match.
        # Last, so a property-scoped widget or listing link anywhere above always wins.
        hosts = portal_hosts[:_MAX_PORTAL_HOSTS]
        return {"host": hosts[0], "hosts": hosts, "property_group": None,
                "order": "date_posted", "direct_url": f"https://{hosts[0]}/listings",
                "address_scope": True, "paginate": True}, None
    return None, ("appfolio: no Appfolio.Listing widget with a propertyGroup found on "
                  + ", ".join(tried[:4]))


def matches(community: dict) -> bool:
    cfg, _ = _resolve_config(community)
    return cfg is not None


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    cfg, err = _resolve_config(community)
    if not cfg:
        return ExtractResult(scrape_error=(err or "appfolio: no widget")[:200],
                             extractor_used=NAME, flag_review=True)
    street = None
    if cfg.get("address_scope"):
        street = _street_key(community.get("address"))
        if street is None:
            return ExtractResult(
                scrape_error="appfolio: manager-wide listings and no community street "
                             "address to scope them by — refused, not written",
                extractor_used=NAME, flag_review=True)

    if cfg.get("paginate"):
        return _extract_manager_feeds(community, cfg, street)

    target = cfg.get("direct_url") or listings_url(cfg)
    try:
        page = fetch_static(target)
    except Exception as exc:
        return ExtractResult(
            scrape_error=f"appfolio: listings fetch failed: {type(exc).__name__}: {exc}"[:200],
            extractor_used=NAME, flag_review=True)
    if page.status >= 400:
        return ExtractResult(scrape_error=f"appfolio: listings HTTP {page.status}",
                             extractor_used=NAME, flag_review=True)

    groups = parse_listings(page.html, street)
    if not groups and street is not None:
        return ExtractResult(
            scrape_error=(f"appfolio: {cfg['host']} lists no home at "
                          f"{community.get('name')}'s street address — refused, not written")[:200],
            extractor_used=NAME, flag_review=True)
    if not groups:
        return ExtractResult(
            scrape_error=f"appfolio: 0 .listing-item cards at {target}"[:200],
            extractor_used=NAME, flag_review=True)
    return ExtractResult(units=groups, extractor_used=NAME,
                         flag_review=any(g.flag_review for g in groups))


# A manager feed pages at /listings/listings?page=N (southloopbvpo: 2 pages, chamberlin: 3 on
# 2026-10-05); page 1 alone missed 6 of Boardwalk Village's 28 homes. Every address-scoped
# route reads every page. Capped so one huge portfolio cannot run away with the community's
# fetch budget; a capped or partly unreadable feed is REFUSED, never stored as a floor.
_PAGE_RE = re.compile(r"/listings/listings\?page=(\d+)")
_MAX_FEED_PAGES = 10


def _feed_pages(host: str):
    """-> (pages_html, complete, error). `complete` is False when a page was capped or failed;
    the caller then refuses the read rather than store a floor."""
    from rentdb.fetchers import fetch_static

    try:
        first = fetch_static(f"https://{host}/listings")
    except Exception:  # noqa: BLE001
        # Worded without a status/transport token on purpose: classify_status reads those as
        # url_dead, and the COMMUNITY's own site answered fine.
        return [], False, f"{host} feed unreadable"
    if first.status >= 400:
        return [], False, f"{host} feed unreadable"
    pages, complete = [first.html], True
    last = max((int(n) for n in _PAGE_RE.findall(first.html)), default=1)
    if last > _MAX_FEED_PAGES:
        complete = False
    for n in range(2, min(last, _MAX_FEED_PAGES) + 1):
        try:
            nxt = fetch_static(f"https://{host}/listings/listings?page={n}")
        except Exception:  # noqa: BLE001
            complete = False
            continue
        if nxt.status >= 400:
            complete = False
            continue
        pages.append(nxt.html)
    return pages, complete, None


def _extract_manager_feeds(community: dict, cfg: dict, street: tuple) -> ExtractResult:
    """The manager's feed(s), every page, keeping ONLY the cards at the community's street.

    Several manager databases can be named on one site (AUS Villas del Sol links two); the
    street scope is what makes trying each safe, and the first that has a card here wins.
    Nothing at the street on any of them -> refused: a portfolio is never this community."""
    errors = []
    for host in cfg.get("hosts") or [cfg["host"]]:
        pages, complete, err = _feed_pages(host)
        if err:
            errors.append(err)
            continue
        groups = parse_listings(pages, street)
        if groups and not complete:
            # A page was capped or unreadable: what we have is a FLOOR that would replace a
            # complete earlier read indistinguishably. The
            # whole read is refused; the community keeps its last good snapshot.
            return ExtractResult(
                scrape_error=(f"appfolio: {host} feed only partly read (a page was capped or "
                              f"unreadable) — refused, not written")[:200],
                extractor_used=NAME, flag_review=True)
        if groups:
            return ExtractResult(units=groups, extractor_used=NAME,
                                 flag_review=any(g.flag_review for g in groups))
    tried = ", ".join(cfg.get("hosts") or [cfg["host"]])
    return ExtractResult(
        scrape_error=(f"appfolio: {tried} list no home at {community.get('name')}'s street "
                      f"address — refused, not written"
                      + (f" ({'; '.join(errors)})" if errors else ""))[:200],
        extractor_used=NAME, flag_review=True)
