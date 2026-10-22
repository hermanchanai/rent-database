"""Adapter for Razz/Zeki-built leasing sites (SSR unit payload on the models page).

Razz sites server-render their floor plans AND their individual units into the HTML of
the models/floor-plans page as JSON. Echelon (PHX) is the case that surfaced it: its
Knock Doorway feed returns `units: 0, layouts: 35` — genuinely empty, nothing for the
`knock` adapter to drop — while `/models` carries six priced, leasable units.

🔴 The payload is a RATE TABLE, not an availability list. Echelon's carried 136 objects:
116 units and 20 plan rows, nearly all with a rent, including units nobody can lease.
Grouping it wholesale reports a 124-unit property as ~116 units available — inventory
-as-availability, the error the `avalon`-before-`sightmap` ordering already exists to
prevent. So this adapter filters on the payload's own per-unit flag and nothing else:

    "available": false                              -> excluded
    "available": "!Date:2026-09-07T00:00:00.000Z"   -> included

Verified by reconciliation, not by assumption: the filter yields 8 units and the page
renders 6 cards, two of them reading "1 Other Units" (J-179 hides J-169, I-162 hides
I-156). 8 = 6 + 2.

Plan/model rows (`0Jx1`, `2x2`, …) have no `model_id` and no `available`; their
`rent.min/max` spans every unit of the plan, leased or not. They are the catalog, not
the offer, and are excluded.

Objects are located by walking outward from their own keys rather than by finding a
named container: the container is part of a bundler-generated screen blob whose shape is
not ours and will churn. The keys are the contract.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .base import ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "razz"
SF_BAND = 50

# The models/floor-plans page is where the payload lives; the tracked URL is usually the
# homepage. Ordered by hit-likelihood on Razz builds.
MODELS_SUBPATHS = ("/models", "/floorplans", "/floor-plans", "/availability")

# Signals that the page carries a Razz/Zeki SSR payload at all.
_SIGNAL_RE = re.compile(r"razzapp\.co|zeki-ssr|Razz|requestedScreen", re.I)
_UNIT_KEY_RE = re.compile(r'"model_id"\s*:')
_AVAIL_DATE_RE = re.compile(r"^!Date:", re.I)

_UNIT_KEYS = ("beds", "baths", "sqft", "rent", "available")
_MAX_OBJ = 24_000     # a plan object with images runs ~1-4 KB; this is a safety stop


def _enclosing_object(text: str, at: int) -> dict | None:
    """The smallest balanced {...} containing `at`, parsed, or None."""
    depth, start = 0, None
    for j in range(at, max(0, at - _MAX_OBJ), -1):
        c = text[j]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                start = j
                break
            depth -= 1
    if start is None:
        return None
    depth, end = 0, None
    for j in range(start, min(len(text), start + _MAX_OBJ)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return None
    try:
        return json.loads(text[start:end])
    except (ValueError, TypeError):
        return None


def has_signal(html: str) -> bool:
    """Razz payload present AND at least one leasable unit in it. Both halves matter: a
    Razz marketing page with no availability payload must fall through to whatever else
    can read the site rather than being claimed and returning nothing."""
    if not html or not _SIGNAL_RE.search(html):
        return False
    return bool(available_units(html))


def available_units(html: str) -> list[dict]:
    """Every unit object the payload marks leasable. Deduped by `id`."""
    if not html:
        return []
    out: dict[str, dict] = {}
    for m in _UNIT_KEY_RE.finditer(html):
        obj = _enclosing_object(html, m.start())
        if not obj or not all(k in obj for k in _UNIT_KEYS):
            continue
        uid = obj.get("id")
        if not uid or uid in out:
            continue
        avail = obj.get("available")
        # A date string means leasable-from; False/None/"" means not. Anything else is
        # unrecognised and is treated as NOT available — this flag is the only thing
        # standing between a rate table and a rent comp.
        if not isinstance(avail, str) or not _AVAIL_DATE_RE.match(avail):
            continue
        out[uid] = obj
    return list(out.values())


def _int(value) -> int | None:
    try:
        return int(round(float(str(value).replace(",", "").strip())))
    except (TypeError, ValueError):
        return None


def parse_models_page(html: str) -> list[UnitGroup]:
    buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for u in available_units(html):
        beds = _int(u.get("beds"))
        if beds is None:
            continue
        sf = _int((u.get("sqft") or {}).get("min"))
        buckets[(beds, round((sf or 0) / SF_BAND) * SF_BAND)].append(u)

    groups: list[UnitGroup] = []
    for (beds, _), members in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        # `if r` alone drops 0 but KEEPS -1, which is exactly what this payload sends
        # on a unit whose lease-from date is set and whose price is not ("rent":
        # {"min": -1, "max": -1}). That -1 became the group's rent_min and reached
        # the comp view as a -$1 rent (a past bug). It is not a rent; the unit still
        # counts as listed, which is the documented shorter-than-`available` case.
        rents = [r for r in (_int((m.get("rent") or {}).get("min")) for m in members)
                 if r is not None and r > 0]
        sfs = [s for s in (_int((m.get("sqft") or {}).get("min")) for m in members) if s]
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
            baths=observed_baths(m.get("baths") for m in members),
            flag_review=not rents,
        ))
    return groups


def _resolve_page(community: dict):
    """-> (html, url, error) for the page carrying the payload."""
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return None, None, "razz: no usable community url"
    base = url.rstrip("/")
    tried = []
    for cand in [url] + [base + sp for sp in MODELS_SUBPATHS]:
        if cand in tried:
            continue
        tried.append(cand)
        try:
            page = fetch_static(cand)
        except Exception:
            continue
        if page.status >= 400:
            continue
        if has_signal(page.html):
            return page.html, cand, None
    return None, None, ("razz: no Razz payload with an available unit on "
                        + ", ".join(tried[:5]))


def matches(community: dict) -> bool:
    html, _, _ = _resolve_page(community)
    return html is not None


def extract(community: dict) -> ExtractResult:
    html, url, err = _resolve_page(community)
    if html is None:
        return ExtractResult(scrape_error=(err or "razz: no payload")[:200],
                             extractor_used=NAME, flag_review=True)
    groups = parse_models_page(html)
    if not groups:
        return ExtractResult(scrape_error=f"razz: payload at {url} had no leasable unit"[:200],
                             extractor_used=NAME, flag_review=True)
    return ExtractResult(units=groups, extractor_used=NAME,
                         flag_review=any(g.flag_review for g in groups))
