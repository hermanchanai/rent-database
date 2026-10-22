"""Observed-only text cues: structure type · stories · garage · year built · amenities.

Shared by the adapters (which run these over plan headings and card text they
already parse) and by the LLM tier (which uses AMENITY_VOCAB to fence what Haiku
is allowed to report). One home for the rules so the two tiers cannot drift.

THE INVARIANT, inherited from `unit_rents` (2026-08-12): every function here is
OBSERVED-ONLY. It reads the page's WORDS and returns None when they do not say.
Nothing infers from square footage, bed count, rent level, unit count, or the
community's name. The predecessor these replace — `detect_type`'s blanket "SFR"
default — is exactly the failure mode: it filled an underwriting cell with a
guess that no reader could distinguish from a measurement.

Every ambiguity resolves to None on purpose. A blank cell sends a reader to look;
a confident wrong value does not.
"""

from __future__ import annotations

import re

# --- structure type --------------------------------------------------------
# One pattern per class. A page is classified by which class it MENTIONS, never
# by what its numbers imply.
_STRUCTURE_PATTERNS = {
    "th": re.compile(r"\btown\s?(?:home|house)s?\b", re.I),
    "sfd": re.compile(r"\bsingle[-\s]family(?:\s(?:home|house|rental))?s?\b"
                      r"|\bdetached\s(?:home|house)s?\b", re.I),
    "mf": re.compile(r"\bapartments?\b|\bcondo(?:minium)?s?\b|\bflats\b", re.I),
}

# --- stories ---------------------------------------------------------------
_STORY_WORDS = {"single": 1, "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3}
_STORIES_RE = re.compile(r"\b(one|two|three|single|[123])[-\s]stor(?:y|ey|ies)\b", re.I)
# "ranch" is industry page-language for single-storey, but ONLY in its explicit
# forms. Codex caught the optional `-style` group letting a proper noun fabricate a
# reading: "The Ranch at Prescott" and a floor plan named "Ranch" both resolved to
# 1 storey. A community's NAME is not a statement about its storeys — the same
# class of error HARD RULE 8 names when it forbids inferring from square footage.
_RANCH_RE = re.compile(r"\branch[-\s]style\b|\branch\s+homes?\b", re.I)

# --- garage ----------------------------------------------------------------
_GARAGE_WORDS = {"no": 0, "one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3}
# The lookbehind is what makes "1-2 car garage" ambiguous rather than 2: the "2"
# is preceded by "-", so it never anchors a match, and the "1" is not followed by
# "car". A community's RANGE is not this plan's garage.
_GARAGE_RE = re.compile(r"(?<![\d-])\b(no|one|two|three|[123])[-\s]car\s+garages?\b", re.I)
_NO_GARAGE_RE = re.compile(r"\bno\s+garage\b", re.I)

# --- year built ------------------------------------------------------------
# WHY THIS EXISTS (2026-08-14). `year_built` on the comp tab comes from ONE source:
# the "compl: M/YYYY" fragment the vendor seed writes into
# rents.communities.notes. Measured that day: 2,804 of 4,840 communities carry it
# (57.9%) and EVERY other origin is zero — including all five web-discovered
# communities. A comp that comp-discovery found and comp-live-rents scraped has no
# vintage and, under that design, never will. This cue is the only source such a
# community can ever have, and it costs nothing: the text is already in hand for the
# concession sweep.
#
# A YEAR NEEDS A VERB. Never match a bare four-digit number — every leasing page in
# the fleet carries "© 2026" in its footer, and a bare-year rule would stamp the
# current year onto every comp as its vintage. That is the `detect_type` "SFR"
# failure in a new column: a guess indistinguishable from a measurement.
#
# RENOVATION IS NOT CONSTRUCTION. "Renovated in 2019" is excluded by construction
# rather than by subtraction — 'renovated' is simply not in the verb list, so
# "Built in 1998, renovated in 2019" reads 1998 and a renovation-only page reads
# None. Same for "Since 1998", deliberately OUT: "serving residents since" and "built
# since" are the same words, and an ambiguity resolves to None here.
_YEAR = r"((?:1[89]|20)\d{2})"
_BUILT_RE = re.compile(
    r"\b(?:built|constructed|completed|established|est\.|opened|delivered|circa)\s*"
    r"(?:in|on)?\s*[:\-]?\s*" + _YEAR + r"\b", re.I)
_YEAR_LABEL_RE = re.compile(r"\byear\s+built\b\s*[:\-]?\s*" + _YEAR, re.I)
# Wide enough for a lease-up that advertises its delivery year, tight enough that a
# phone number, a unit count or a street number cannot pass as a vintage.
_YEAR_MIN, _YEAR_MAX = 1900, 2035

# --- amenities -------------------------------------------------------------
# CLOSED vocabulary. A closed list is the only thing that stops an LLM writing
# marketing adjectives ("resort-style", "luxury") into a deal document as if they
# were facts about the property. Anything outside it is dropped, never kept.
AMENITY_VOCAB = (
    "pool", "spa", "gym", "clubhouse", "dog park", "pet friendly", "playground",
    "gated", "tennis", "pickleball", "walking trails", "smart home", "ev charging",
)


def structure_type_from_text(text: str | None) -> str | None:
    """'th' | 'sfd' | 'mf' from what the page CALLS this product, else None.

    A tie returns None: mixed-product pages are real, and picking one of two
    equally-supported readings is a coin flip rendered as a fact. A class wins
    only when it is mentioned at least twice as often as the runner-up, which
    lets a townhome page survive one stray "apartment finder" link in a footer.
    """
    if not text:
        return None
    hits = {k: len(p.findall(text)) for k, p in _STRUCTURE_PATTERNS.items()}
    ranked = sorted(hits.items(), key=lambda kv: -kv[1])
    top, top_n = ranked[0]
    if top_n == 0:
        return None
    runner_n = ranked[1][1] if len(ranked) > 1 else 0
    if runner_n and top_n < 2 * runner_n:
        return None
    return top


def stories_from_text(text: str | None) -> int | None:
    """Storey count the page STATES for this plan, else None.

    Distinct-values rule: a page advertising "1-story and 2-story options" is
    describing the community's range, not this floor plan, so it resolves to None
    rather than to whichever appeared first.
    """
    if not text:
        return None
    found = {_STORY_WORDS[m.group(1).lower()] for m in _STORIES_RE.finditer(text)}
    if _RANCH_RE.search(text):
        found.add(1)
    return found.pop() if len(found) == 1 else None


def garage_from_text(text: str | None) -> int | None:
    """Garage CAR COUNT the page states, else None. Zero is an observation.

    A bare "attached garage" returns None on purpose — it does not say one or two,
    and this cell feeds an underwriting model.
    """
    if not text:
        return None
    found = {_GARAGE_WORDS[m.group(1).lower()] for m in _GARAGE_RE.finditer(text)}
    if _NO_GARAGE_RE.search(text):
        found.add(0)
    return found.pop() if len(found) == 1 else None


def year_built_from_text(text: str | None) -> int | None:
    """Year built the page STATES, else None. Never a bare four-digit number.

    Two shapes are accepted and nothing else: a construction verb followed by a year
    ("built in 1998", "completed 2015", "circa 1985", "opened 2021"), and the explicit
    label ("Year Built: 1998"). A renovation year is not construction and does not
    match; a copyright footer has no verb and does not match.

    Distinct-values rule, same as `stories_from_text`: "Phase 1 built 2019, Phase 2
    built 2021" describes a community's range, not one vintage, so it returns None
    rather than whichever appeared first. A range written as "built between 1998 and
    2002" matches nothing at all — 'between' is not one of the connectors — which is
    the failure direction we want.
    """
    if not text:
        return None
    found = {int(m.group(1))
             for rx in (_BUILT_RE, _YEAR_LABEL_RE)
             for m in rx.finditer(text)}
    found = {y for y in found if _YEAR_MIN <= y <= _YEAR_MAX}
    return found.pop() if len(found) == 1 else None


def normalize_amenities(values) -> list[str] | None:
    """Fence a reported amenity list to AMENITY_VOCAB: lowercased, deduped, sorted.

    None (not []) when nothing survives — the same load-bearing-None discipline as
    unit_rents, so a consumer can tell "the page named none" from "we did not ask".
    """
    if not values:
        return None
    keep = {str(v).strip().lower() for v in values if v}
    return sorted(keep & set(AMENITY_VOCAB)) or None
