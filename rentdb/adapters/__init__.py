"""Operator adapters (Tier 1 extraction).

Each adapter module exposes:
    matches(community: dict) -> bool
    extract(community: dict) -> ExtractResult

The orchestrator iterates through ADAPTERS in priority order; first match wins.
Fallback to Tier 2 (LLM extractor) if no adapter matches or extract() returns
an error.
"""

from . import ark, amh, avalon, firstkey, rentcafe_marketplace, air_communities, apartments247, appfolio, knock, razz, entrata, realpage, repli360, sightmap, rentcafe, rently
from . import resman, rise48

# Order matters: operator-level adapters first, then vendor platforms.
# Avalon MUST precede sightmap: AvalonBay pages embed SightMap as a price-less
# map, so the vendor adapter would match and report inventory-as-availability.
# SightMap stays ahead of RentCafe because some RentCafe pages embed SightMap;
# the public SightMap inventory API is the better source when both match.
# AppFolio leads the vendor block: its signal is the operator's own
# `Appfolio.Listing({...propertyGroup...})` embed — an explicit, unambiguous
# declaration, unlike a CDN asset reference — and no other adapter claims it.
# Razz sits directly AFTER knock: when a Knock Doorway feed HAS units it is the
# operator's own live feed and wins. Razz is the fallback for the case that
# surfaced it — a Knock site whose feed is empty while the page carries the units.
# rentcafe_marketplace is URL-matched like the operator adapters (www.rentcafe.com/apartments/
# .../default.aspx is RentCafe's own listing site, never a property site), so it sits in that
# exclusive block: the vendor `rentcafe` adapter matches the same page and reads nothing.
ADAPTERS = [ark, amh, avalon, firstkey, rentcafe_marketplace, rise48, air_communities, apartments247, appfolio, knock, razz, entrata, realpage, repli360, sightmap, rentcafe, resman, rently]


def find_adapter(community: dict, log=None):
    """Return the best adapter after one shared homepage fetch, or None.

    Thin wrapper over `find_adapters` — the winner is its first element. Kept because
    every pre-2026-08-21 caller wants exactly one module and the contract is unchanged.
    """
    mods = find_adapters(community, log=log)
    return mods[0] if mods else None


def find_adapters(community: dict, log=None):
    """EVERY adapter matching this community, in priority order (possibly empty).

    Added 2026-08-21. `find_adapter` answered "who wins", which is the wrong question
    once the winner can fail: the pipeline set `result = None` on an extract() error and
    fell straight to the Tier-2 LLM without ever asking whether a LATER adapter also
    matched the same page.

    Measured over rotation logs 08-18..08-21: 581 realpage extract failures across 183
    distinct communities. Re-fetching all 183 and running the rest of the chain, 92 of
    them — 55 rentcafe, 34 sightmap, 3 entrata — had a working adapter that was never
    tried, because realpage sits ahead of all three and claims the page on a signal as
    weak as a cs-cdn.realpage.com asset reference. (SightMap is a RealPage product, so
    its pages reference realpage hosts as a matter of course.)

    Reordering is NOT the fix: realpage genuinely serves 122 communities in those same
    logs, and demoting it would hand those to an adapter that cannot read them.

    Returns [] rather than None, so "nothing matched" and "the chain is exhausted" are
    the same shape. `log` semantics are unchanged — called only when dispatch could not
    look at the page at all.

    Operator domains are checked first without network I/O. Vendor detection then reuses
    one HTML response instead of refetching the homepage once per adapter.

    `log` is optional and, when given, is called ONLY when dispatch could not look at
    the page at all — a raised fetch or an HTTP error status. An empty list already
    means "no vendor matched", and a transport failure returns the same empty list, so
    without this the two are indistinguishable to every caller: the pipeline falls to the
    LLM tier, or records `needs_adapter` under --skip-llm, and nothing says the
    community was never actually examined.

    That blindness cost two wrong diagnoses on 2026-08-20, when a controlled
    --skip-llm run reported "no adapter" 15/15 on communities that do match, every row
    taking ~40s instead of ~3-7s, and the result was believed twice before the real
    cause surfaced.

    A CLEAN no-match stays silent on purpose. Logging that would put a line on every
    LLM-tier community in the fleet — hundreds a night — and a log everything writes is
    one nobody reads.
    """
    for mod in (ark, amh, avalon, firstkey, rentcafe_marketplace):
        if mod.matches(community):
            # An operator match is exclusive: these are per-landlord sites, and the
            # match is on the URL alone, so there is no shared page to chain over.
            return [mod]

    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return []
    if resman.has_signal(url):
        # The seed URL IS a ResMan portal: it links nothing to itself, and its TLS
        # chain needs the CA bundle resman.extract passes -- routed on the URL, unfetched.
        return [resman]
    from rentdb.fetchers import fetch_static, throttle
    try:
        page = fetch_static(url)
    except throttle.DeadlineExceeded:
        # The community's rate-limit budget is spent, not a transport failure: the pipeline's
        # dispatch handler records it as `throttled`. Swallowed here, the community fell to
        # the LLM tier and a partial browser read was stored as live_data.
        raise
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"dispatch: could not fetch {url} ({type(e).__name__}: {e}); "
                f"NO ADAPTER WAS TRIED — this is a transport failure, not a vendor miss")
        return []
    if page.status >= 400:
        if log:
            log(f"dispatch: {url} returned HTTP {page.status}; NO ADAPTER WAS TRIED — "
                f"a WAF or outage, not evidence the vendor is unknown")
        return []
    return detect_vendors(page.html)


def detect_vendors(html: str) -> list:
    """Vendor adapters whose signal is in `html`, in chain priority order (possibly empty).

    THE one vendor-detection path (split out of find_adapters 2026-10-05,). Two
    callers: find_adapters, on the static homepage, and adapters.rendered, on the page the
    browser RENDERED for the LLM tier -- where a SightMap embed or a Repli360 widget that
    JavaScript injected finally shows up. A second, hand-copied detector there would drift
    from this one exactly as the matches()/find_adapters pair did (see the warning below).
    """
    low = html.lower()
    out = []
    # Rise48's own platform: ~42 vanity domains 301 to rise48communities.com, a Next.js
    # site with no vendor signal at all -- before this they fell to the LLM tier. The signal is
    # the page's canonical on that host; static fetches follow the 301, so the vanity URL the
    # manifest stores lands here. An operator declaration, so it leads every vendor.
    if rise48.has_signal(html):
        out.append(rise48)
    # AIR Communities' own site platform: an operator declaration ahead of every vendor. Its
    # pages reference Entrata, which matched and read nothing; the LLM then saw one plan page
    # (beds_null_groups, 2026-09-30). Same has_signal the adapter's matches() uses.
    if air_communities.has_signal(html):
        out.append(air_communities)
    # Apartments247's own asset host: an explicit platform declaration, same-origin API.
    if apartments247.has_signal(html):
        out.append(apartments247)
    # `has_signal` is the SAME call appfolio.extract makes, per the warning below: a
    # vendor signal that lands only in matches() leaves production unchanged.
    if appfolio.has_signal(html):
        out.append(appfolio)
    if knock._INIT_RE.search(html):
        out.append(knock)
    # Same has_signal contract as appfolio: detection is the extractor's own check.
    if razz.has_signal(html):
        out.append(razz)
    # `_is_razz_entrata` is shared with entrata.matches() DELIBERATELY. This function
    # re-implements vendor detection inline (one shared fetch instead of one per
    # adapter), so it is a second, parallel detection path — and the pipeline uses only
    # this one, via find_adapter_with_probe. Widening matches() alone on 2026-08-20
    # left production completely unchanged: a controlled --skip-llm run over the 15
    # known-Razz communities still reported "no adapter" 15/15. Any future vendor
    # signal has to land in BOTH places, or call the same helper as here.
    if (any(signal in low for signal in entrata.ENTRATA_SIGNALS)
            or entrata._is_razz_entrata(low)):
        out.append(entrata)
    if any(signal in low for signal in realpage.SIGNALS):
        out.append(realpage)
    if repli360._SCRIPT_RE.search(html):
        out.append(repli360)
    if sightmap.EMBED_RE.search(html):
        out.append(sightmap)
    strong_rentcafe_signals = tuple(
        signal for signal in rentcafe.RENTCAFE_SIGNALS if signal != "securecafe.com/"
    )
    if any(signal in html for signal in strong_rentcafe_signals):
        out.append(rentcafe)
    # ResMan's applicant portal, scoped by p= to ONE property. Same has_signal the
    # extractor resolves with. After the vendor feeds: a fallback when none of them priced.
    if resman.has_signal(html):
        out.append(resman)
    # AppFolio known only by its MANAGER's database (portal link / Websites widget,):
    # a portfolio feed, read street-scoped and refused on no match, so it sits low.
    if appfolio not in out and appfolio.has_portal_signal(html):
        out.append(appfolio)
    # LAST: a Rently link is often just a self-tour button beside a real vendor's
    # inventory. It is the reading only when nothing ahead of it can price the page.
    if rently.has_signal(html):
        out.append(rently)
    return out
