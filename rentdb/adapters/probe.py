"""Vendor-path probing (build plan Step A).

Many long-tail communities are actually RentCafe or SightMap (Engrain)
under the hood, but their *seed URL is a bare marketing homepage* that carries no
vendor signal — so `find_adapter()`'s homepage detection misses them and they fall
to the (slower, paid, less reliable) LLM tier. This prober appends the common
vendor subpaths and re-checks the vendor signals on each, using ONE static fetch
per candidate (not one per adapter), and routes a hit back to the right adapter.

Only the two *vendor* adapters benefit (rentcafe, sightmap). Operator adapters
(ark) are name/domain-specific and gain nothing from generic subpath probing.
"""

from __future__ import annotations

import re

from . import find_adapter, find_adapters

# Ordered by hit-likelihood. `/availableunits` is the RentCafe convention;
# `/floorplans` + variants are where SightMap embeds usually live.
VENDOR_SUBPATHS = (
    "/availableunits",
    "/floorplans",
    "/floor-plans",
    "/availability",
    "/pricing",
    "/homes",
    # SightMap's own page on many templates (JAX Soluna: /site-plan/; RentCafe sites:
    # /interactivepropertymap). Added 2026-09-25.
    "/site-plan",
    "/interactivepropertymap",
)


def find_adapters_with_probe(community: dict, log=None):
    """Every (adapter_module, community_to_extract) candidate, in priority order.

    Added 2026-08-21 alongside `adapters.find_adapters`. `find_adapter_with_probe`
    returns only the winner, which is all a caller needs until the winner's extract()
    fails — at which point the pipeline fell straight to the Tier-2 LLM without asking
    whether a later adapter also matched. 92 of the 183 communities behind the 581
    realpage failures in rotation logs 08-18..08-21 had exactly such an adapter.

    The PROBE half is deliberately NOT chained. It exists for pages whose homepage
    carries no vendor signal at all, and it returns at the first signal it finds on a
    subpath; there is no second candidate to offer, and re-probing every subpath for
    each adapter would multiply the fetch count against a host we are already rate
    -limiting. So: chain the direct matches, single-shot the probe.
    """
    mods = find_adapters(community, log=log)
    # A Rently link ALONE is not a vendor match. `find_adapters` puts rently last
    # because it is usually a self-tour button beside the real inventory, but that order
    # only helps when the real vendor is ALSO on the homepage. One Atlanta townhome community
    # links Rently from its homepage and embeds SightMap on /floor-plans: the
    # probe never looked, and Rently's self-tour subset (2 homes, $2,664-2,684) stood in for
    # the site's 9 ($2,449-2,599). So when rently is the only direct match, probe the
    # subpaths too and put a hit AHEAD of rently, which stays in the chain as the fallback.
    # Only rently-only communities pay these fetches (3 of 10 rerouted on 2026-09-27).
    if [m.NAME for m in mods] == ["rently"]:
        ad, c = _probe(community, log=log, exclude={"rently"})
        if ad is not None:
            return [(ad, c), (mods[0], community)]
    if mods:
        return [(m, community) for m in mods]
    ad, c = find_adapter_with_probe(community, log=log)
    return [(ad, c)] if ad is not None else []


def find_adapter_with_probe(community: dict, log=None):
    """Return (adapter_module, community_to_extract).

    1. Direct match on the seed URL (unchanged behavior).
    2. Else probe vendor subpaths; on the first RentCafe/SightMap signal, route to
       that adapter. For RentCafe we hand back the *base* URL so its own two-hop
       (/availableunits) logic runs; for SightMap we hand back the candidate URL
       where the embed was found.
    Returns (None, community) if nothing matches — the caller falls to the LLM tier.
    """
    # Pass `log` through: find_adapter is where the one shared homepage fetch happens,
    # so a transport failure or a WAF 403 is invisible unless it is reported from there.
    ad = find_adapter(community, log=log)
    if ad is not None:
        return ad, community
    return _probe(community, log=log)


def fallback_candidates(community: dict, tried: set[str], log=None):
    """The subpath probe, as a SECOND look after every directly matched adapter failed.

    `find_adapters_with_probe` probes only when nothing matched on the homepage. JAX
    Soluna matches `realpage` (an online-leasing link) which cannot read it, while its
    rents sit in a SightMap embed on /site-plan/ (2026-09-25). the pipeline calls this
    only once its direct chain is exhausted, so a healthy community never pays these
    fetches; it replaces a paid LLM call, not a free adapter.
    Returns [] or [(adapter, community_to_extract)], never an adapter in `tried`.
    """
    ad, c = _probe(community, log=log, exclude=set(tried))
    return [(ad, c)] if ad is not None else []


def _probe(community: dict, log=None, exclude: set[str] = frozenset()):
    """Subpaths, then the homepage fallbacks (Maymont settings, SecureCafe).

    `exclude`: adapter NAMEs already tried. A hit for one of them does not END the scan —
    a RentCafe signal on /availableunits must not hide a SightMap embed on /site-plan."""
    base = (community.get("url") or "").strip().rstrip("/")
    if not base.startswith("http"):
        return None, community

    # Local imports to avoid circulars + only pay the cost when probing.
    from rentdb.fetchers import fetch_static, throttle
    from .rentcafe import RENTCAFE_SIGNALS
    from .sightmap import EMBED_RE
    from . import appfolio, rentcafe, sightmap

    tried: set[str] = set()
    for sp in VENDOR_SUBPATHS:
        cand = base + sp
        if cand in tried:
            continue
        tried.add(cand)
        try:
            fr = fetch_static(cand)
        except throttle.DeadlineExceeded:
            raise    # a spent budget is the pipeline's `throttled`, not a probe miss
        except Exception:
            continue
        if fr.status >= 400:
            continue
        html = fr.html
        # AppFolio first: its embed is an explicit operator declaration carrying the
        # property scope, and it is checked here because the widget lives on an
        # availability/floor-plans subpage, never the tracked homepage — homepage
        # detection alone would never route these communities anywhere but the LLM
        # tier, which is where Ascent at Northgate (AUS) sat on every snapshot it had.
        if appfolio.has_signal(html):
            if log:
                log(f"probe: AppFolio listings widget found at {sp} -> "
                    f"{community.get('name')}")
            if appfolio.NAME not in exclude:
                return appfolio, {**community, "url": cand}
        if EMBED_RE.search(html):
            if log:
                log(f"probe: SightMap embed found at {sp} -> {community.get('name')}")
            if sightmap.NAME not in exclude:
                return sightmap, {**community, "url": cand}
        if any(sig in html for sig in RENTCAFE_SIGNALS):
            if log:
                log(f"probe: RentCafe signal found at {sp} -> {community.get('name')}")
            if rentcafe.NAME not in exclude:
                return rentcafe, {**community, "url": base}
    # A SecureCafe resident/applicant link alone is not enough to route before
    # probing: some such sites use SightMap for public inventory. After all
    # inventory paths miss, it is still a useful RentCafe-family fallback.
    try:
        homepage = fetch_static(base)
        # Maymont Homes sites (Next.js + Builder.io) carry NO sightmap reference in any
        # page: the browser reads `engrain.sightmapUrl` from /api/global-settings and
        # injects the map. Cannondale Court (CHA) was needs_adapter on every snapshot
        # while that feed listed 11 priced homes (2026-09-25). Only a Maymont page pays
        # this fetch.
        if homepage.status < 400 and "maymonthomes" in homepage.html.lower():
            settings = base + "/api/global-settings"
            fr = fetch_static(settings)
            if fr.status < 400 and EMBED_RE.search(fr.html):
                if log:
                    log(f"probe: Maymont global-settings SightMap -> {community.get('name')}")
                if sightmap.NAME not in exclude:
                    return sightmap, {**community, "url": settings}
        if homepage.status < 400 and "securecafe.com/" in homepage.html.lower():
            if log:
                log(f"probe: SecureCafe fallback -> {community.get('name')}")
            if rentcafe.NAME not in exclude:
                return rentcafe, {**community, "url": base}
    except throttle.DeadlineExceeded:
        raise    # same as the subpath loop above
    except Exception:
        pass
    return None, community


# Entrata prospect-portal availability URL (e.g.
# https://mysummerwellcedarcrest.prospectportal.com/acworth/summerwell-cedarcrest/…)
_PP_LINK_RE = re.compile(r"https?://[a-z0-9-]+\.prospectportal\.com/[a-z0-9-]+/[a-z0-9-]+/", re.I)


# Anchor hrefs that smell like the floor-plan / availability page.
_DATA_HREF_RE = re.compile(
    r"href=[\"']([^\"']*(?:floor-?plans?|availab|pricing|apartments|residences)[^\"']*)[\"']",
    re.I,
)
# Signals that a page actually carries unit data (rent prices / square footage).
_PRICE_HINT_RE = re.compile(r"\$\s?[1-9],?\d{3}(?!\d)")
_SF_HINT_RE = re.compile(r"\b\d{3,4}\s*(?:sq\.?\s?ft|sf\b|square\s+f)", re.I)

# --- what a candidate can never be -----------------------------------------
# `_DATA_HREF_RE` above matches ANY href carrying one of its tokens, and two things
# make that far wider than intended:
#   1. it matches <link rel="stylesheet" href="..."> as readily as an <a>, and <head>
#      precedes <body>, so a stylesheet is often literally candidates[0];
#   2. the token can sit in the community's OWN BASE PATH — on
#      robbinspropertyllc.com/apartments/fl/tampa/<name>/ every internal link on the
#      whole site contains "apartments", so /apply and /gallery qualify exactly as
#      much as /floorplans does.
# Both land in resolve_data_url branch 3 (`home_score == 0 -> return first_link`),
# which is the JS-rendered-site path these communities live on. Measured on the
# 2026-08-20 Tampa/Nashville needs_adapter triage: 22 of 86 misrouted — 7 to a .css
# file, 15 to /apply, /feed, /gallery or /amenities.
_ASSET_EXT_RE = re.compile(
    r"\.(?:css|js|mjs|json|xml|rss|png|jpe?g|gif|webp|avif|svg|ico|bmp|"
    r"pdf|docx?|xlsx?|zip|mp4|webm|mov|woff2?|ttf|otf|eot)(?:$|[?#])", re.I)

# Pages that exist on every leasing site and can never carry unit inventory.
# Deliberately conservative — a page wrongly excluded here costs extraction quality
# on that site, but a page wrongly INCLUDED is handed to the LLM as if it were the
# floor-plan page, which is how a community ends up with no rents at all.
_NON_DATA_SEGMENTS = frozenset({
    "apply", "application", "applynow", "apply-now",
    "gallery", "photos", "photo-gallery", "media", "videos", "virtual-tour", "tour",
    "amenities", "feed", "feeds",
    "contact", "contactus", "contact-us", "directions", "map", "maps", "location",
    "neighborhood", "neighbourhood", "reviews", "blog", "news",
    "privacy", "privacy-policy", "terms", "accessibility", "sitemap",
    "login", "signin", "sign-in", "portal", "resident", "residents", "resident-portal",
})

# Whole-segment equality above misses COMPOUND names. Found 2026-08-20 by probing all 86
# needs_adapter communities through the live resolver: Legacy Hill and Parc Fairview both
# resolved to `/Apartments/module/application_authentication...`, an Entrata login module.
# It is not in _NON_DATA_SEGMENTS and never would be — the segment varies per install — so
# match on a substring for the handful of module names that can never carry inventory.
_NON_DATA_SUBSTRINGS = ("application_authentication", "applicationauthentication",
                        "/module/application", "onlineleasing/application",
                        # RealPage CMS asset handler. Its stylesheet URL carries
                        # "...FloorplanV3..." in the query, matched _DATA_HREF_RE, and was
                        # handed to Chromium as the floor-plan page ("Download is starting").
                        "getresource.ashx")


def _is_data_capable(cand: str) -> bool:
    """Could this URL conceivably be a floor-plan / availability page?"""
    from urllib.parse import urlparse

    if _ASSET_EXT_RE.search(cand):
        return False
    low = cand.lower()
    if any(s in low for s in _NON_DATA_SUBSTRINGS):
        return False
    seg = urlparse(cand).path.rstrip("/").rsplit("/", 1)[-1].lower()
    return seg not in _NON_DATA_SEGMENTS



def _data_score(html: str) -> int:
    """How much unit data does this page visibly carry? (static-fetch heuristic)"""
    return len(_PRICE_HINT_RE.findall(html)) + len(_SF_HINT_RE.findall(html))


def _same_property(base: str, cand: str) -> bool:
    """Is `cand` a page of the SAME property as `base`, not a neighbouring one?

    Same-DOMAIN was the old test and it is only half the rule. An operator that
    runs every property as a path on one domain (greystar.com, and it is not alone)
    makes a sibling building same-netloc, while `_DATA_HREF_RE` matches any href
    containing "apartments" — so on a Greystar page every neighbouring property
    looks like a floor-plan link. Palm Court resolved to Seven Luxury Apartments
    and stored Seven's rents under Palm Court's name (2026-08-12).

    Two shapes, and the distinction is whether the community's own URL has a path:

      bare host  (https://www.rentretreat.com)  -> the community IS the domain, no
        sibling can exist, every same-host link is its own. Unchanged behaviour,
        and this is the majority: 452 of the 667 llm-tier communities are alone on
        their host.
      path-scoped (https://www.greystar.com/palm-court-…/p_12501) -> the property
        is a subtree, so only that subtree is the same property.

    Deliberately strict for the second shape. A same-property page reachable only
    by a PARALLEL path is rejected here and the caller falls back to the
    community's own URL — costing some extraction quality on that one site. The
    other error costs a comp priced off the wrong building, silently. Those are
    not comparable, so the tie goes to strict.
    """
    from urllib.parse import urlparse

    b, c = urlparse(base), urlparse(cand)
    if c.netloc.lower() not in ("", b.netloc.lower()):
        return False                     # off-domain: ILS portal, as before
    b_path = b.path.rstrip("/")
    if not b_path:
        return True                      # community is the whole domain
    c_path = c.path.rstrip("/")
    return c_path == b_path or c_path.startswith(b_path + "/")


def _candidate_links(html: str, base: str) -> list[str]:
    """Floor-plan-ish links on the page, plus the conventional vendor subpaths.

    Extracted from resolve_data_url so the candidate set is testable without a
    network round-trip — it is where the cross-property defect lived, and filtering
    HERE covers both exits (the score loop and the zero-score fallback) at once.
    """
    from urllib.parse import urljoin

    candidates: list[str] = []
    for m in _DATA_HREF_RE.finditer(html):
        href = m.group(1).strip()
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        cand = urljoin(base + "/", href)
        if not _same_property(base, cand):
            continue
        # Filtered HERE, alongside the cross-property guard, for the reason that
        # docstring gives: this is the single choke point both exits share — the
        # score loop AND branch 3's `first_link`. Branch 3 is the one that was
        # actually shipping assets to the LLM, because it fires precisely when the
        # homepage scores 0, which is what a JS-rendered site always does.
        if not _is_data_capable(cand):
            continue
        cand = cand.split("#")[0].rstrip("/")
        if cand and cand != base and cand not in candidates:
            candidates.append(cand)
    for sp in VENDOR_SUBPATHS:
        cand = base + sp
        if cand not in candidates:
            candidates.append(cand)
    return candidates


def resolve_data_url(community: dict, log=None) -> str:
    """Return the best data-bearing URL to hand the LLM tier (Step A, fuller form).

    Many long-tail sites serve a marketing homepage with NO rent — the actual
    floorplan/availability data lives on a subpage (golden-eval catch 2026-07-14:
    Marq/Windsor/Santal/St Elmo homepages had ZERO prices and near-zero SF, so
    the LLM dropped beds and nulled SF). Resolution order:

      1. Entrata prospectportal → its /availability/ page (original behavior).
      2. Candidate subpages = floor-plan-ish links found ON the homepage +
         the conventional vendor subpaths. Static-fetch each (cheap), score by
         visible price+SF density, return the best scorer that beats the
         homepage.
      3. No scorer but a floor-plan link exists → return that link anyway
         (JS-rendered plan pages score 0 on a static fetch but almost always
         beat the homepage once browser-rendered).
      4. Otherwise the original URL.

    Called only for communities with no adapter match, right before the LLM fetch.
    """
    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return url
    from rentdb.fetchers import fetch_static, throttle
    try:
        fr = fetch_static(url)
    except throttle.DeadlineExceeded:
        raise    # the pipeline's Tier-2 handler records `throttled`
    except Exception:
        return url
    h = fr.html
    low = h.lower()

    # 1. Entrata prospect portal (original behavior)
    if "entrata" in low and "prospectportal" in low:
        m = _PP_LINK_RE.search(h)
        if m:
            avail = m.group(0).rstrip("/") + "/availability/"
            if log:
                log(f"resolve: Entrata availability page -> {community.get('name')}: {avail}")
            return avail

    # 2. Generic: find the floor-plans / availability page and score it.
    base = url.rstrip("/")
    candidates = _candidate_links(h, base)

    home_score = _data_score(h)
    best_url, best_score = None, home_score
    first_link = candidates[0] if candidates and not candidates[0].startswith(base + "/availableunits") else None
    for cand in candidates[:8]:  # bound the probe cost
        try:
            cf = fetch_static(cand)
        except throttle.DeadlineExceeded:
            raise    # same as the homepage fetch above
        except Exception:
            continue
        if cf.status >= 400:
            # branch 3 below used to return this very
            # link after its probe had just answered 404 -- the browser then read a
            # dead page while the homepage was fine. ONLY a missing page: 403/429/5xx
            # is a blocked fetch, and the browser may still render it (diff review, P2).
            if cand == first_link and cf.status in (404, 410):
                first_link = None
            continue
        score = _data_score(cf.html)
        if score > best_score:
            best_url, best_score = cand, score

    if best_url:
        if log:
            log(f"resolve: data page ({best_score} hits vs homepage {home_score}) -> "
                f"{community.get('name')}: {best_url}")
        return best_url

    # 3. JS-rendered plan pages score 0 statically — still better than a
    #    homepage that ITSELF shows no data.
    if home_score == 0 and first_link:
        if log:
            log(f"resolve: no static scorer; trying homepage's plan link -> "
                f"{community.get('name')}: {first_link}")
        return first_link
    return url
