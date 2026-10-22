"""The read chain for one community: adapters, a second look, the rendered page, then the LLM.

    candidates = every adapter whose signal matches (operator URL match first, then one shared
                 homepage fetch checked against every vendor signal, then vendor subpaths)
    for each candidate, in priority order:
        extract -> drop empty beds-null groups -> usable?  stop : next candidate
    nothing usable -> probe the vendor subpaths once more for an adapter not yet tried
    still nothing  -> render the page (injected) and run the deterministic readers on it
    still nothing  -> the LLM tier (injected provider)
    finalize       -> concession repair and sweep, sentinel / implausible-rent guards,
                      ledger status

"Usable" is `base.result_is_usable`: a result that parsed plan groups but carries no rent,
no listed unit and no coming-soon unit is a catalog, and a catalog does not stop the chain.
A later adapter, or the LLM, still gets its chance at the real units.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from bs4 import BeautifulSoup

from rentdb.adapters.base import (ExtractResult, clamp_implausible_rents,
                                  classify_status, drop_null_bed_groups, drop_sentinel_rents,
                                  null_impossible_llm_availability, result_is_usable)
from rentdb.adapters.probe import fallback_candidates, find_adapters_with_probe
from rentdb.adapters.rendered import dispatch_rendered
from rentdb.concessions import merge_concession, sweep_concession
from rentdb.fetchers import throttle

log = logging.getLogger(__name__)

Renderer = Callable[[str], Optional[str]]


def _try(adapter, community: dict, logf) -> ExtractResult | None:
    try:
        result = adapter.extract(community)
    except throttle.DeadlineExceeded:
        raise
    except Exception as e:  # noqa: BLE001 -- one adapter raising is a miss, never a crash
        logf(f"adapter {adapter.NAME} raised {type(e).__name__}: {e}")
        return None
    if any(u.beds is None for u in (result.units or [])):
        drop_null_bed_groups(result, adapter.NAME)
    if result_is_usable(result):
        return result
    logf(f"adapter {adapter.NAME} unusable: "
         f"{(result.scrape_error or f'{len(result.units)} group(s), none priced or listed')[:80]}")
    return None


def read_community(community: dict, *, llm=None, render: Renderer | None = None,
                   sweep: bool = True, logf: Callable[[str], None] | None = None) -> ExtractResult:
    """One community through the whole chain. Never raises for a page problem."""
    logf = logf or log.info
    result: ExtractResult | None = None
    tried: set[str] = set()
    try:
        candidates = find_adapters_with_probe(community, log=logf)
        for adapter, c_ad in candidates:
            tried.add(adapter.NAME)
            result = _try(adapter, c_ad, logf)
            if result is not None:
                break
        if result is None and candidates:
            for adapter, c_ad in fallback_candidates(community, tried, log=logf):
                tried.add(adapter.NAME)
                result = _try(adapter, c_ad, logf)
                if result is not None:
                    logf(f"fallback probe: {adapter.NAME} at {c_ad.get('url')}")
                    break
    except throttle.DeadlineExceeded as e:
        # A throttled host is not a dead URL and not an adapter miss: record it as such and do
        # not fall through to a tier whose first fetch is the same rate-limited host.
        return ExtractResult(scrape_error=f"rate-limit budget exhausted: {e}"[:200],
                             extractor_used="none", flag_review=True, status="throttled")

    html = None
    if result is None and render is not None:
        html = render(community.get("url") or "")
        if html:
            result = dispatch_rendered(community, html, tried=frozenset(tried), log=logf)
    if result is None and llm is not None and html:
        result = llm.extract(community, html)
    if result is None:
        result = ExtractResult(scrape_error="no deterministic reader matched and no LLM tier ran",
                               extractor_used="none")
    return finalize(result, community, sweep=sweep, logf=logf)


def _marketing_page_text(url: str) -> str | None:
    from rentdb.fetchers import fetch_static
    try:
        page = fetch_static(url)
    except Exception:  # noqa: BLE001 -- no sweep is the safe outcome
        return None
    if page.status >= 400 or not page.html:
        return None
    soup = BeautifulSoup(page.html, "html.parser")
    for t in soup.find_all(("script", "style", "noscript")):
        t.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def finalize(result: ExtractResult, community: dict, *, sweep: bool = True,
             logf: Callable[[str], None] | None = None) -> ExtractResult:
    """Everything that happens to a result after extraction, the same for every tier."""
    logf = logf or log.info
    if result.status == "throttled":
        return result
    source = result.extractor_used or "extractor"
    rent_hint = min((u.rent_min for u in result.units if u.rent_min), default=None)
    # A banner the extractor captured but could not count is normalised from its own text.
    if result.units and result.concession_weeks is None and result.raw_concession_text:
        merge_concession(result, result.raw_concession_text, monthly_rent=rent_hint)
    # Banners live on the marketing page; adapters read an availability feed and miss them.
    # Never for a plan-scoped community: a shared portal's banner speaks for every product on it.
    if (sweep and result.units and result.concession_weeks is None
            and not community.get("plan_scope") and community.get("url")):
        text = _marketing_page_text(community["url"])
        if text and sweep_concession(result, text, monthly_rent=rent_hint):
            logf(f"concession swept from the marketing page: {result.concession_weeks}w")
    drop_sentinel_rents(result, source)
    clamp_implausible_rents(result, source)
    null_impossible_llm_availability(result, source, community.get("public_record_units"))
    drop_null_bed_groups(result, source)
    # Stamp the ledger state from content unless an adapter affirmed one (sold out, URL dead).
    if not result.status:
        result.status = classify_status(result)
    return result
