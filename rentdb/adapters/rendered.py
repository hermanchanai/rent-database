"""Tier 1.5: read the browser-RENDERED page with deterministic readers before the LLM.

the pipeline's Tier-2 path renders a community's data page in Chromium and, until 2026-10-05,
handed that HTML straight to the LLM (sync) or queued it for a Message Batch. Adapter dispatch
(`find_adapters`) only ever looked at the STATIC homepage, so a vendor widget that JavaScript
injects -- or that sits on the subpage resolve_data_url picked -- never reached its adapter,
and the LLM, which cannot read an iframe, answered "no pricing visible" (~44 SightMap
embeds and ~14 Repli360 widgets in the 2026-10-04 diagnosis of 731 such pages).

`dispatch_rendered` runs on that rendered HTML, in this order:
  1. vendor detection -- `adapters.detect_vendors`, the SAME function find_adapters uses, so
     there is one detection path -- dispatched to each detected adapter that exposes
     `extract_rendered(community, html)` and was not already tried this community;
  2. GENERIC_READERS, html-only parsers of structured data the page itself carries:
     `jsonld_offers` (per-unit schema.org makesOffer,), `gounion` (the Union CRM's
     PRELOADED_STATE "allPlans" floor-plan payload,).
Every result goes through the same beds-null cleanup and `result_is_usable` test as the
adapter chain in the pipeline; the first usable one wins. None means "hand the page to the LLM",
which is exactly the old behaviour.
"""

from __future__ import annotations

from . import detect_vendors, gounion, jsonld_offers
from .base import ExtractResult, drop_null_bed_groups, result_is_usable

# html-only readers (module with NAME + extract_html(community, html)), tried AFTER every vendor.
GENERIC_READERS: tuple = (jsonld_offers, gounion)


def _readers(html: str, tried: set[str]):
    for mod in detect_vendors(html):
        fn = getattr(mod, "extract_rendered", None)
        if fn is not None and mod.NAME not in tried:
            yield mod.NAME, fn
    for mod in GENERIC_READERS:
        if mod.NAME not in tried:
            yield mod.NAME, mod.extract_html


def dispatch_rendered(community: dict, html: str, tried=frozenset(), log=None) -> ExtractResult | None:
    """The first usable deterministic read of the rendered page, or None (-> LLM tier).

    `tried`: adapter NAMEs the static chain already ran for this community; re-running one on
    the same site costs fetches and reads what it read before. Exceptions other than the
    throttle's DeadlineExceeded are a miss for that reader, never a crash of the community --
    the LLM tier is still behind this.
    """
    from rentdb.fetchers import throttle

    if not html:
        return None
    for name, fn in _readers(html, set(tried)):
        try:
            result = fn(community, html)
        except throttle.DeadlineExceeded:
            raise
        except Exception as e:  # noqa: BLE001 -- a reader failure is "no read", the LLM is next
            if log:
                log(f"rendered: {name} raised {type(e).__name__}: {e}")
            continue
        if result is None:
            continue
        if any(u.beds is None for u in (result.units or [])):
            drop_null_bed_groups(result, name)
        if result_is_usable(result):
            if log:
                log(f"rendered: {name} read the rendered page "
                    f"({len(result.units)} group(s)) -- LLM tier skipped")
            return result
        if log:
            why = (result.scrape_error or f"{len(result.units)} group(s), none priced or listed")[:80]
            log(f"rendered: {name} unusable: {why}")
    return None
