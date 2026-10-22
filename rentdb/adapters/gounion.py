"""Union (gounion.com) CRM sites' floor-plan payload -- a generic, html-only reader.

Mark Taylor's PHX sites (The Halsten 4abc9473, Spectra on 7th South 4a1ecb29, Tavalo at Cadence,
The Palms on Scottsdale, Elux at McQueen, EVR Spur Cross, EVR Bella Vista) are a React app on the
Union CRM (`my.gounion.com` dwell.js, `liftlytics.imgix.net` assets). Every page server-renders
`window.PRELOADED_STATE`, whose `pageData` section "FLOOR_PLANS" carries

    "allPlans": [{description, bedrooms, bathrooms, squareFootage, available,
                  minRent, maxRent, isVisible, propertyId, ...}, ...]

-- per plan: the listed count and the rent range over those units. The visible text of the page
the LLM tier got (often the home page) shows no price, so it answered "no pricing visible" on
all seven (2026-09-29..30 dumps). The section is re-synced by the CRM (its `updated` stamp was
2026-10-02 on all four sites fetched 2026-10-05; Elux's count moved 35 -> 36 between reads).

Reading rules:
  * one group per VISIBLE plan; available = the plan's own count (listed = available); a
    payload with any count missing is refused (unknown is not 0); every count 0 = the CRM
    affirms nothing listed -> status no_availability;
  * rent only for a plan with available > 0: a plan nothing is listed in has no rent a renter
    can pay today (available=0, rent null -- the prompt's COVERAGE RULE shape);
  * unit_rents is None: the payload is a per-plan RANGE, never per-unit rents;
  * a payload naming more than one CRM propertyId is refused (not one community's plans);
  * beds are the payload's own `bedrooms` (0 = studio); a plan without it is skipped.

One CRM propertyId is one community's inventory. EVR Bella Vista's payload is the same
as Elux at McQueen's (propertyId 376840 on both sites, 2026-10-06; EVR's site is the clone) and
the page carries no field that binds a propertyId to a site. So the first community in this
PROCESS to read an id claims it, and a different community (COMMUNITY_KEY) presenting it later
is refused with status url_dead (the terminal state: the page carries another
community's prices, so the LLM tier must not get it either). Scope is one the pipeline process =
one manifest: run_rotation runs each manifest and retry as its own subprocess, so a collision
ACROSS manifests is invisible here. The
guard cannot tell which site owns the id -- only the second claimer is refused, and the log
names both. A payload without one integer propertyId claims nothing. Pinned by
tools/test_gounion_property_claims_3458.py.
"""

from __future__ import annotations

import json
import re

from .base import STATUS_NOAVAIL, STATUS_URL_DEAD, ExtractResult, UnitGroup, observed_baths

NAME = "gounion"
_SIGNALS = ("my.gounion.com", "liftlytics.imgix.net")
_STATE = "window.PRELOADED_STATE"
_STATE_RE = re.compile(r"window\.PRELOADED_STATE\s*=\s*")

#: propertyId -> (COMMUNITY_KEY, display name) of the community that read it first in this process.
_CLAIMS: dict[int, tuple[tuple[str, str], str]] = {}


def reset_claims() -> None:
    """Forget every claim (tests; one the pipeline process is one run)."""
    _CLAIMS.clear()


def _key(community: dict, pid):
    """(COMMUNITY_KEY, name) for a usable integer `pid`, else None (None is not an identity)."""
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    from rentdb.keys import community_key
    return community_key(community.get("market") or "", community.get("name") or ""), community.get("name") or ""


def _taken_by(community: dict, pid) -> str | None:
    """The OTHER community's name when `pid` was already read for it this process."""
    me, owner = _key(community, pid), _CLAIMS.get(pid)
    return owner[1] if me is not None and owner is not None and owner[0] != me[0] else None


def _claim(community: dict, pid) -> None:
    """Record `pid` for `community` -- only once a usable read exists."""
    me = _key(community, pid)
    if me is not None:
        _CLAIMS.setdefault(pid, me)


def has_signal(html: str) -> bool:
    return bool(html) and _STATE in html and any(s in html for s in _SIGNALS)


def _num(v) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n else None


def _plans(html: str) -> list[dict] | None:
    m = _STATE_RE.search(html)
    if not m:
        return None
    try:
        state, _ = json.JSONDecoder().raw_decode(html, m.end())
    except ValueError:
        return None
    pages = ((state or {}).get("pageData") or {}).get("pageData") or []
    sections = [p for p in pages if isinstance(p, dict) and p.get("section") == "FLOOR_PLANS"]
    if len(sections) != 1:
        return None
    plans = (sections[0].get("values") or {}).get("allPlans")
    return plans if isinstance(plans, list) else None


def extract_html(community: dict, html: str) -> ExtractResult | None:
    if not has_signal(html):
        return None
    plans = [p for p in (_plans(html) or []) if isinstance(p, dict) and p.get("isVisible") is not False]
    if not plans:
        return None
    # A plan whose count is missing or not a whole number is availability UNKNOWN; the payload
    # then cannot say what is listed, and reading it as 0 would be a guess. The LLM tier gets the page, as before this reader existed.
    counts = [p.get("available") for p in plans]
    if any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in counts):
        return None
    if len({p.get("propertyId") for p in plans}) > 1:
        return ExtractResult(scrape_error=f"{NAME}: allPlans spans several CRM properties, refused",
                             extractor_used=NAME, flag_review=True)
    pid = plans[0].get("propertyId")
    other = _taken_by(community, pid)
    if other is not None:
        return ExtractResult(
            scrape_error=(f"{NAME}: identity conflict -- CRM propertyId {pid} was already read for "
                          f"{other!r} this run; refused")[:200],
            extractor_used=NAME, flag_review=True, status=STATUS_URL_DEAD)
    units, skipped = [], 0
    for p in plans:
        beds = _num(p.get("bedrooms"))
        if beds is None or beds < 0:
            skipped += 1
            continue
        avail = p["available"]
        lo, hi = _num(p.get("minRent")), _num(p.get("maxRent"))
        lo = lo if lo and lo > 0 else None
        hi = hi if hi and hi > 0 else None
        if avail and (lo or hi):
            lo, hi = lo or hi, hi or lo
        else:
            lo = hi = None
        sf = _num(p.get("squareFootage"))
        sf = int(round(sf)) if sf and sf > 0 else None
        units.append(UnitGroup(
            beds=int(beds), sf_min=sf, sf_max=sf, available=avail,
            rent_min=int(round(lo)) if lo else None, rent_max=int(round(hi)) if hi else None,
            rent_type="unspecified", rent_basis="unspecified", unit_rents=None,
            baths=observed_baths([_num(p.get("bathrooms"))]),
            plan_name=(str(p.get("description") or "").strip() or None),
            flag_review=bool(avail) and lo is None,
        ))
    if not units:
        return None
    _claim(community, pid)
    result = ExtractResult(
        units=units, extractor_used=NAME, flag_review=bool(skipped),
        scrape_error=f"{NAME}: skipped {skipped} plan(s) with no bed count" if skipped else None)
    # Every visible plan's own count is 0: the CRM affirms nothing is listed. That is the site's
    # answer, not a reason to let the LLM invent units (Codex design review, finding 4).
    if not skipped and all(g.available == 0 for g in units):
        result.status = STATUS_NOAVAIL
    return result
