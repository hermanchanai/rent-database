"""Shared types for operator adapters."""

from __future__ import annotations

import json
import re

from dataclasses import dataclass, field

# The shared concession rule lives in rentdb/concessions.py and is re-exported here so
# every adapter reaches it the same way.
from rentdb.concessions import local_concession  # noqa: F401


@dataclass
class UnitGroup:
    """One group in the weekly_snapshots.units JSONB array.

    Per planning doc §6, groups are formed by bedroom count + SF band
    (±50 sf tolerance — see normalizer.py).

    Rent fields:
      rent_min / rent_max          : the primary rent as displayed
      rent_type                    : "base" | "allin" | "unspecified"
                                     - base   = pre-fees rent (most common convention)
                                     - allin  = base + mandatory fees rolled in
                                     - unspecified = site shows a number without labeling
      rent_allin_min / rent_allin_max
                                   : populated ONLY when a site shows BOTH base and
                                     all-in side by side (e.g., Quinn, Invitation Homes).
                                     Null otherwise.
      rent_basis                   : "actual" | "starting_at" | "unspecified"
                                     - actual      = the numbers are rents a renter can pay
                                     - starting_at = they are FLOOR prices ("from $X", "$X+").
                                                     rent_max is then the highest floor, NOT a
                                                     maximum rent, so the group is a lower bound
                                                     and not a range. Consumers must render it
                                                     "$X+" rather than "$X-$Y".
                                     - unspecified = not determined (the safe default)

    A note on rent_basis vs rent_type: they are ORTHOGONAL. rent_type says what the
    number INCLUDES (fees or not); rent_basis says whether the number is the real
    thing or a floor. San Palacio (PHX) is base rent AND starting-at.

    unit_rents (added 2026-08-12)
    ----------------------------
    The individual per-unit rents this group was built from, sorted ascending — the
    inputs to rent_min/rent_max rather than the two order statistics alone.

    WHY: rent_min/rent_max collapse a group to its endpoints, so the only central
    estimate a consumer can form is the midpoint (min+max)/2. That midpoint is
    unweighted: one premium unit moves it as much as seven standard ones. Measured
    on Palm Court (PHX) 2026-08-08, a 747sf 1BR group of 8 listed units spanning
    $1,340-$1,904 produced a $1,622 comp; if seven of those eight sat at $1,340 the
    true mean is ~$1,411. Nothing stored could tell the difference. the comp rule
    is the mean of the LOWEST FIVE observed rents, which needs the observations.

    HARD RULE: this list holds only rents we ACTUALLY OBSERVED, one entry per unit.
    Never synthesize it from rent_min/rent_max, never pad it out to `available`,
    never round-trip a range through it. `None` is the honest answer when the page
    published a range but not its members, and consumers key on that distinction:
    None means "range only — use the low end", a populated list means "real
    observations — average the lowest five". A fabricated list silently converts
    the first case into the second and there is no way to detect it downstream.

    It may legitimately be SHORTER than `available`: a listed unit with a move-in
    date and no price contributes to the count but has no rent to record.

    structure_type / stories / garage (added 2026-08-12)
    ---------------------------------------------------
    What the PAGE says this plan is — "th"/"sfd"/"mf", its storey count, its garage
    car count. They fill the rent-comp tab's Type (col M), Stories and Garage cells,
    which until now were either blank or, worse, filled by `detect_type`'s blanket
    "SFR" default: a guess rendered in a cell an underwriter reads as a measurement.

    SAME HARD RULE AS unit_rents: OBSERVED-ONLY. Read from the page's words via
    `cues.py`, never inferred from square footage, bed count, rent level, unit count
    or the community's name. None means "the page did not say" and is load-bearing —
    a blank cell sends a reader to look; a confident wrong one does not. A 1,900sf
    3BR is not evidence of a detached home.

    `garage = 0` is an OBSERVATION ("no garage"), not a blank. It must survive
    serialization as 0 or the reading is silently converted into "we never checked".

    baths (added 2026-08-13)
    -----------------------
    Bathroom count for the plan, as a float because 1.5 and 2.5 are real. Fills the
    rent-comp tab's Bath cell (col V), which until now was hand-typed or left empty.

    The adapters have been READING this all along — rentcafe parses "N Bedrooms |
    M Bathrooms" off the plan heading and entrata's card line is literally
    "N Bed / M Bath — X,XXX sq. ft" — and then dropped it on the floor here,
    because groups are keyed on (beds, SF band) and nothing carried it through.

    SAME OBSERVED-ONLY RULE as unit_rents and structure_type: what the page SAYS.
    Never inferred from bed count, SF or rent (a 2BR is not evidence of 2 baths;
    plenty of 1980s Phoenix 2BRs are 1-bath — Cactus Trail's is, on this very comp
    set). Set it through `observed_baths()`, which returns None when the units in
    a group disagree: one group is one plan, so two bath counts under one SF band
    means the grouping is coarser than the page's own plans, and a blank cell is
    the honest rendering of that.
    """
    beds: int | None
    sf_min: int | None
    sf_max: int | None
    available: int              # LISTED homes in this group (priced or move-in-dated — site UI convention; a future date still counts). 2026-07-14 semantics decision.
    coming_soon: int = 0        # advertised but NOT yet listed (no price/listing, or explicit "coming soon" site label)
    rent_min: int | None = None # whole dollars; computed across available + coming_soon
    rent_max: int | None = None
    rent_type: str = "unspecified"      # "base" | "allin" | "unspecified"
    rent_allin_min: int | None = None   # only set when site shows base + allin both
    rent_allin_max: int | None = None
    rent_basis: str = "unspecified"     # "actual" | "starting_at" | "unspecified"
    unit_rents: list[int] | None = None  # observed per-unit rents, ascending; None = range only
    structure_type: str | None = None    # "sfd" | "th" | "mf" — what the PAGE calls it
    stories: int | None = None           # storeys the page STATES for this plan
    garage: int | None = None            # garage car count the page STATES; 0 is real
    baths: float | None = None           # bathrooms the page STATES; 1.5/2.5 are real
    plan_name: str | None = None         # the site's OWN name for this plan ("The Oak")
    flag_review: bool = False

    def to_json(self) -> dict:
        return {
            "beds": self.beds,
            "sf_min": self.sf_min,
            "sf_max": self.sf_max,
            "available": self.available,
            "coming_soon": self.coming_soon,
            "rent_min": self.rent_min,
            "rent_max": self.rent_max,
            "rent_type": self.rent_type,
            "rent_allin_min": self.rent_allin_min,
            "rent_allin_max": self.rent_allin_max,
            "rent_basis": self.rent_basis,
            "unit_rents": self.unit_rents,
            "structure_type": self.structure_type,
            "stories": self.stories,
            "garage": self.garage,
            "baths": self.baths,
            "plan_name": self.plan_name,
            "flag_review": self.flag_review,
        }


#: A DESCRIPTION, not a name: "2BR/2BA", "Two Bedroom", "3 x 2", "1,100 sqft".
#: Shared by BOTH write paths — the adapters and the LLM extractor — because a
#: catalog can hand over a description just as easily as a model can invent one.
#:
#: FULLY ANCHORED, and that is the whole subtlety. "1x1" alone is bed-by-bath, a
#: description of every one-bed one-bath plan a property has; "1x1 A" is ONE of
#: them, and leasing sites really do name plans that way (LAS, 2026-08-21). The
#: distinguishing suffix is the difference between a category and a name, so the
#: pattern must match the ENTIRE string or not at all.
_NOT_A_PLAN_NAME = re.compile(
    r"^(\d+(\.\d+)?\s*[x×]\s*\d+(\.\d+)?"
    r"|(studio|\d+|one|two|three|four|five)\s*[-\s]*(br|bd|bed|bedroom)s?"
    r"(\s*[/x-]\s*\d+(\.\d+)?\s*(ba|bath|bathroom)s?)?"
    r"|[\d,]+\s*(sq\.?\s*ft|sf|sqft)"
    r"|studio)$", re.I)


def clean_plan_name(v) -> str | None:
    """The ONE rule for what counts as a floor-plan name, and what the name IS.

    Shared by `observed_plan_name` (every adapter) and `extractor._plan_name` (the
    LLM tail), because the consumer cannot tell which path a name arrived by and
    both can produce the same junk.

    UNWRAPS a nested plan object. Some SightMap feeds put the whole plan in the
    name field — `{"name":"Milan","provider_id":"2400427"}` — and the LAS rotation
    of 2026-08-21 stored 13 of those verbatim within an hour of `plan_name`
    shipping. The real name is inside, so the rule is unwrap, not reject: dropping
    "Milan" would lose a true name. Both encodings are handled, JSON and Python
    repr, because `str(dict)` is the other way this arrives.

    REJECTS what is not a name at all: a bare number ("32" — a unit count that
    wandered into the field), a bed/SF DESCRIPTION, and anything still object-shaped
    after unwrapping. This matters more than it looks: the financial model's
    live-rent matcher treats a name as STRONG evidence and skips its beds/SF check
    on a name match, so a junk name binds a scraped group to a mix row on nothing.

    Verbatim otherwise, whitespace collapsed, capped at 120 chars — a longer "name"
    is a marketing paragraph, not a label.
    """
    if isinstance(v, dict):
        v = v.get("name")
    t = " ".join(str(v or "").split())
    if t.startswith("{") or t.startswith("["):
        inner = None
        for parse in (json.loads, _literal_eval):
            try:
                obj = parse(t)
            except Exception:            # noqa: BLE001 — a malformed blob is just not a name
                continue
            if isinstance(obj, dict) and obj.get("name"):
                inner = obj["name"]
            break
        if inner is None:
            return None
        t = " ".join(str(inner).split())
    if not t or t.startswith("{") or t.startswith("["):
        return None
    if _BARE_NUMBER.match(t) or _NOT_A_PLAN_NAME.match(t):
        return None
    return t[:120]


_BARE_NUMBER = re.compile(r"^[\d,]+(\.\d+)?$")


def _literal_eval(s):
    from ast import literal_eval  # noqa: PLC0415 — only reached on a repr'd dict
    return literal_eval(s)


def observed_plan_name(values) -> str | None:
    """The site's OWN name for this floor plan, or None when it did not say one.

    Same shape rule as observed_baths, same reason: a group is one floor plan, so
    two names inside it means (beds, SF+/-50) bucketed two plans together and
    printing either would assert something we did not observe.

      every unit that named a plan agrees (case/space-insensitively) -> that name
      units disagree ("The Oak" and "The Elm" in one group)          -> None
      nobody named one                                              -> None

    WHY THIS FIELD EXISTS (2026-08-20). The financial model's subject live-rent
    column matches a scraped group to the rent roll's floor plan in column D. Beds
    and SF are weak evidence for that — two plans routinely share both — and the
    name is strong evidence where a site publishes one. It is stored, never
    inferred: a synthesised label like "2BR/2BA" would let the model claim a NAME
    match and skip the beds/SF check, which is the only real evidence there is.

    The FIRST spelling wins the casing, so the stored value reads the way the page
    writes it; comparison is what is normalised, not the value.
    """
    seen: dict[str, str] = {}
    for v in values or []:
        t = clean_plan_name(v)
        if not t:
            continue
        seen.setdefault(t.lower(), t)
    return next(iter(seen.values())) if len(seen) == 1 else None


def observed_rents(values) -> list[int] | None:
    """Normalize a per-unit rent list into UnitGroup.unit_rents shape.

    Every adapter already builds the list this reads — it is the same sequence
    min()/max() are taken from — so this exists to make the *shape* uniform in one
    place rather than nine: whole dollars, ascending, junk dropped, and None (not
    []) when nothing survives, since None is what the market-rent rule keys on.

    Deliberately does NOT apply the plausibility ceiling: clamp_implausible_rents
    owns that judgement, needs the group's SF to make it, and clears this list
    alongside rent_min/rent_max when it fires. Two places deciding what counts as
    a real rent is how the group and its members drift apart.
    """
    if not values:
        return None
    out: list[int] = []
    for v in values:
        try:
            n = int(round(float(v)))
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return sorted(out) or None


def observed_baths(values) -> float | None:
    """The bath count for a group, or None when the page did not unambiguously say.

    Mirrors observed_rents' job — one place decides the SHAPE, not nine adapters —
    but the rule is different because baths is a single fact about a plan, not a
    distribution over units:

      every unit that stated a count agrees   -> that count
      units disagree (1.0 and 2.0 in one group) -> None
      nobody stated one                        -> None

    Disagreement returns None DELIBERATELY. A group is one floor plan; two bath
    counts inside it means (beds, SF±50) bucketed two different plans together, and
    picking either one would print a measurement we did not make. The blank sends a
    reader to the leasing page, which is the correct outcome.

    Whole numbers come back as floats (2 -> 2.0) so the JSONB type is stable; halves
    survive because bath counts genuinely go .5.
    """
    seen: set[float] = set()
    for v in values or []:
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if n > 0:
            seen.add(n)
    return seen.pop() if len(seen) == 1 else None


# ---------------------------------------------------------------------------
# Weekly ledger states (accounting-completeness — see build plan §1)
# Every community resolves to exactly one of these each run. "covered" =
# live_data + no_availability (a known, correct state). The other three are
# gaps the coverage work (and the self-heal agent) drain.
# ---------------------------------------------------------------------------
STATUS_LIVE = "live_data"              # current unit/rent data captured
STATUS_NOAVAIL = "no_availability"     # fetched OK, operator shows nothing leasable
STATUS_NEEDS_ADAPTER = "needs_adapter" # fetch OK but neither adapter nor LLM could parse
STATUS_URL_DEAD = "url_dead"           # fetch failed / 404 / wrong URL
STATUS_BROKEN = "broken"               # unexpected error (adapter raised, shape violation)

LEDGER_STATES = (STATUS_LIVE, STATUS_NOAVAIL, STATUS_NEEDS_ADAPTER, STATUS_URL_DEAD, STATUS_BROKEN)
COVERED_STATES = (STATUS_LIVE, STATUS_NOAVAIL)


@dataclass
class ExtractResult:
    """What an adapter returns for one community's weekly snapshot."""
    units: list[UnitGroup] = field(default_factory=list)
    concession_weeks: int | None = None
    raw_concession_text: str | None = None
    flag_review: bool = False
    scrape_error: str | None = None
    extractor_used: str = ""    # "ark" | "amh" | "llm_haiku" | etc.
    status: str = ""            # one of LEDGER_STATES; set by classify_status() or the orchestrator
    # Community-level, not per-group: the amenities the page NAMES, fenced to
    # cues.AMENITY_VOCAB. None = the page named none. Feeds the rent-comp Notes
    # cell after the concession. LLM-tier only for now — adapters read pricing
    # feeds that carry no amenity language at all.
    amenities: list[str] | None = None
    # True only when the extractor kept the units to the manifest row's `plan_scope`.
    # the pipeline.finalize_result discards any result for a scoped community without it.
    # Never persisted.
    plan_scope_applied: bool = False
    # True when an adapter REFUSED a unit-level offer as site-level weeks: weeks None,
    # raw = the unit offers. merge_concession must not re-derive a count from that raw; the
    # landing-page sweep still runs (a page banner is site-level). Never persisted.
    concession_unit_scoped: bool = False

    def to_snapshot_row(self, community_id: str) -> dict:
        """Shape matching the weekly_snapshots table columns (minus id)."""
        return {
            "community_id": community_id,
            "concession_weeks": self.concession_weeks,
            "raw_concession_text": self.raw_concession_text,
            "flag_review": self.flag_review,
            "units": [u.to_json() for u in self.units],
            "extractor_model": self.extractor_used,
            "scrape_error": self.scrape_error,
            "status": self.status,
            "amenities": self.amenities,
        }


def group_carries_data(g) -> bool:
    """True when a unit group actually carries something we came here for.

    A rent (either bound), a listed unit, or a coming-soon unit. Everything else —
    beds/SF/plan name — is CATALOG: it describes what the property offers, not what it
    is asking or showing today. Hoisted to module level 2026-09-01 so `classify_status`
    and `drop_null_bed_groups` share ONE definition of "carries data"; they had drifted
    apart, and the ledger consequence is below.
    """
    return (getattr(g, "rent_min", None) is not None
            or getattr(g, "rent_max", None) is not None
            or bool(getattr(g, "available", None))
            or bool(getattr(g, "coming_soon", None)))


def result_is_usable(result: "ExtractResult") -> bool:
    """Should the adapter CHAIN stop here, or try the next candidate?

    the pipeline used to fall through only on `scrape_error and not units`, which misses
    the shape that matters most: an adapter that succeeds, returns groups, sets no
    error — and every group is empty. Echelon (PHX) is the case. Its Knock Doorway feed
    returns `units: 0, layouts: 35`, so `knock` emits 9 catalog groups with no rent and
    no availability, no error, and WINS the chain. The `razz` adapter, which can read
    the 8 leasable units server-rendered into the very same page, was never reached.

    A result nobody can price a deal from is not a reason to stop looking.

    EXCEPT when the adapter AFFIRMS the zero -- or affirms the URL is dead: it read the site's own complete
    inventory and it lists nothing, and it said so by setting status=no_availability. Then
    there is nothing left to find, and the LLM can only invent units (Hoke O' Kelly: llm_luna
    stored 5 + 3 available off an AMH page listing none). Only an adapter whose source IS the
    listing sets this; a dry vendor catalog (Echelon's knock feed) never does.
    """
    if result.status == STATUS_NOAVAIL and not result.scrape_error:
        return True
    # An adapter that PROVES the URL no longer serves this community stops the chain too
    #: a removed RentCafe listing redirects to a city search page carrying OTHER
    # communities' prices, and the LLM tier, handed that same page, would read one of them
    # as this community. No adapter sets url_dead otherwise; the LLM tier's own url_dead
    # never passes through here.
    if result.status == STATUS_URL_DEAD:
        return True
    if not result.units:
        return False
    return any(group_carries_data(g) for g in result.units)


def classify_status(result: "ExtractResult") -> str:
    """Derive the ledger state from an extract result's *content*.

    Control-flow states the orchestrator knows directly (a hard skip -> needs_adapter,
    an orchestrator exception -> broken) are set by the pipeline; this handles the
    normal adapter/LLM outcomes. The one fuzzy boundary is no_availability vs
    needs_adapter when 0 units come back with an error string: a fetch-failure error
    => url_dead, a parse-failure error => needs_adapter, a clean fetch that
    affirmatively found nothing => no_availability. Step D tightens the parse-vs-leased
    calls case by case.
    """
    err = (result.scrape_error or "").lower()
    if result.units:
        # At least one unit group is data, even if rent is null on SOME groups.
        if any(group_carries_data(g) for g in result.units):
            return STATUS_LIVE
        # ...but groups that ALL carry nothing are a plan CATALOG, not live data, and
        # calling them live_data is the worst available answer (2026-09-01). Measured
        # on the fleet that day: 360 latest snapshots were in this state (llm_haiku 289,
        # rentcafe 31, knock 29, amh 6, entrata 5), and because
        # `rents.v_latest_unit_groups` selects the newest snapshot WHERE
        # status = 'live_data', 50 of those communities had REAL rents in an older
        # live_data snapshot that the empty newer one was hiding from every comp pull.
        #
        # Not no_availability: that asserts the operator shows nothing leasable. Echelon
        # (PHX), the community that surfaced this, was advertising six priced units on
        # its own site while reporting live_data with 9 empty groups — the Knock Doorway
        # feed returns `units: 0, layouts: 35` and the prices live in the site's own
        # SSR payload. We did not look in the right place; the property is not dry.
        # needs_adapter is the literal truth and is the state coverage work drains.
        #
        # REVISED 2026-09-25 (the analyst): "if nothing priced, then NA — next time something
        # might be priced." The site's plans WERE read and none is priced or listed, so the
        # honest ledger word is no_availability. The Echelon protection moved upstream:
        # `result_is_usable` still refuses to stop the adapter chain on a catalog, so a later
        # adapter or the LLM still gets its chance at the real units and only the chain's
        # LAST word reaches here. It is still never live_data. A result that read nothing at
        # all (no groups + an error) stays needs_adapter below — that one is our miss.
        # Measured that day: 416 latest snapshots were dry catalogs, 1,972 unreadable.
        note = (f"{result.extractor_used or 'extractor'}: "
                f"{len(result.units)} plan group(s) parsed but NO rent, availability or "
                f"coming-soon on any of them — a plan catalog, recorded no_availability")
        result.scrape_error = f"{result.scrape_error}; {note}" if result.scrape_error else note
        result.flag_review = True
        return STATUS_NOAVAIL
    if err:
        broken_markers = ("orchestrator error", "raised", "shape", "unexpected", "traceback")
        if any(m in err for m in broken_markers):
            return STATUS_BROKEN
        fetch_fail_markers = (
            "fetch failed", "status=4", "status=5", "http 4", "http 5", "http4", "http5",
            "timeout", "connection", "dns", "ssl", "navigation", "404", "403", "500", "502", "503",
        )
        if any(m in err for m in fetch_fail_markers):
            return STATUS_URL_DEAD
        # Reached the page but couldn't parse it ("no td-card-rent cells found", ...).
        return STATUS_NEEDS_ADAPTER
    # 0 units, no error => extractor affirmatively found nothing leasable.
    return STATUS_NOAVAIL


# Absolute ceiling: no Sunbelt apartment rents monthly anywhere near this, so a
# value this high is definitionally a non-rent number (condo sale price, lease total).
_MAX_PLAUSIBLE_MONTHLY_RENT = 50_000
# Rent per SF is the real discriminator (the project rules): a $30,250 4,267sf penthouse at
# $7.09/sf is a real rent; a $45,263 1,784sf 2BR at $25.37/sf is a lease-term or
# aggregate figure parsed as monthly. Measured against the whole fleet 2026-08-10:
# of 7,734 groups carrying rent_max AND a plausible SF, the highest VERIFIED-real
# reads are AUS ultra-luxury (The Travis 9.07, Residences at 6G 9.91) and there is a
# clean empty band between 9.91 and 12.36 — above which every single group was a
# confirmed parse error (Galleria Palms 18.87-33.70, Paseo 12.36-25.37). $10 sits in
# that gap. It is deliberately NOT tuned tight: this nulls only the indefensible.
_MAX_PLAUSIBLE_RENT_PER_SF = 10.0
# Below this, SF is itself junk (a placeholder plan), so a psf ratio means nothing.
_MIN_SF_FOR_PSF_TEST = 100


# A rent is a number a renter can pay, so a non-positive one is definitionally not
# a rent — it is the feed's "no price available" placeholder sitting in the price
# field. Measured on the two feeds that emit it (2026-09-18):
#   razz     "rent": {"min": -1, "max": -1}   on a unit whose lease-from date is set
#            but whose price is not (Discovery at Mandolin HOU, units dated
#            2026-12-13 and 2027-02-08; 12 of 260 unit objects on that page)
#   rentcafe "Rent: $0.00"                    on an unpriced unit (Willow Branch ATL,
#            50 of 76 rows; `_parse_currency` reads it as the integer 0)
# `observed_rents` has ruled `n > 0` since 2026-08-12, which is why `unit_rents` is
# clean on these groups while the endpoints are not: every adapter takes min()/max()
# over its RAW member list, and the three idioms in use leak differently —
# `if r` (amh/appfolio/entrata/knock/realpage/razz) drops 0 but KEEPS -1,
# `if r is not None` (ark/rentcafe) keeps both, and repli360 filters nothing.
# Nine leak paths, so this is one guard at the convergence point rather than nine.


def drop_sentinel_rents(result: "ExtractResult", source: str) -> "ExtractResult":
    """Refuse a non-positive "no price available" sentinel as a rent.

    WHY HERE and not only per-adapter: exactly the argument `clamp_implausible_rents`
    below already makes. That clamp's ceiling lived in extractor.py and so only ever
    covered the LLM tier while the fleet's worst value came from an adapter. The floor
    has the same shape — the two adapters emitting today are razz and rentcafe, but
    eight of the ten member filters are permeable to at least one sentinel, so a fix
    that lands only where it currently bites is a fix that waits for the next feed.

    RECOVERS RATHER THAN NULLS wherever it can. `unit_rents` is the same member list
    min()/max() were taken from, with the junk already dropped, so `min(unit_rents)`
    IS the group's lowest real rent — not a guess, and the one case where the "null,
    never guess" rule does not apply because we are not guessing. Nulling instead
    would throw away a price the page published: on Discovery at Mandolin alone that
    is 11 observed rents across 13 listed units.

    NEVER DROPS THE GROUP. A `rent_min > 0` filter satisfies a count check and
    silently deletes inventory. An unpriced group still honestly carries "37 listed
    homes, none priced" — `group_carries_data` keeps it alive on `available`, and a
    comp set that quietly gets smaller is worse than the -$1 that started this,
    because the -$1 is visible and a missing comp is not.
    """
    hits: list[str] = []
    for g in result.units or []:
        # the member list itself, on EVERY group. Recovering the endpoints from
        # the positive members and then storing the raw list let `rents.market_rent()`
        # average [0, 1500] to $750. None, not [], when nothing survives (observed_rents).
        members_in = getattr(g, "unit_rents", None)
        if members_in:
            g.unit_rents = [v for v in members_in if v is not None and v > 0] or None
        pairs = (("rent_min", "rent_max", True), ("rent_allin_min", "rent_allin_max", False))
        touched: list[str] = []
        for lo_name, hi_name, use_members in pairs:
            lo, hi = getattr(g, lo_name, None), getattr(g, hi_name, None)
            if (lo is None or lo > 0) and (hi is None or hi > 0):
                continue
            # Members are positive-only by observed_rents' contract; re-filter anyway
            # so a hand-built group cannot smuggle a sentinel in through the list.
            members = sorted(v for v in (getattr(g, "unit_rents", None) or [])
                             if v is not None and v > 0) if use_members else []
            if lo is not None and lo <= 0:
                setattr(g, lo_name, members[0] if members else None)
                touched.append(f"{lo_name}={lo}")
            if hi is not None and hi <= 0:
                setattr(g, hi_name, members[-1] if members else None)
                touched.append(f"{hi_name}={hi}")
        if not touched:
            continue
        recovered = g.rent_min is not None or g.rent_max is not None
        if not recovered:
            # No price survived. Say so on the row itself — the ledger complaint in
            # a past bug was that 18 of 22 out-of-range groups read flag_review=false,
            # so nothing on the row admitted it was unpriced.
            g.unit_rents = None
            g.flag_review = True
        hits.append(f"{g.beds}bd " + " ".join(touched)
                    + (f" -> {g.rent_min}-{g.rent_max}" if recovered else " -> unpriced"))

    if hits:
        result.flag_review = True
        note = f"{source}: refused {len(hits)} sentinel rent(s) [" + "; ".join(hits[:4]) + "]"
        result.scrape_error = f"{result.scrape_error}; {note}" if result.scrape_error else note
    return result


def clamp_implausible_rents(result: "ExtractResult", source: str) -> "ExtractResult":
    """Null a group's rent when the number cannot be a monthly rent — never guess one.

    WHY here and not per-adapter: the >$50k clamp lived in extractor.py and so only
    ever covered the LLM tier, while the worst single value in the fleet
    (Paseo AUS, 2BR 1,784sf at $45,263 = $25.37/sf) comes from the ENTRATA adapter,
    on a bookable card, and sailed straight past it. Every extractor converges here,
    the same place drop_null_bed_groups already guards.

    Per the project rules the rule is null + flag_review, never a corrected guess: we know
    the number is not a rent, we do not know what the rent is.
    """
    hits: list[str] = []
    for g in result.units or []:
        rents = [v for v in (getattr(g, "rent_min", None), getattr(g, "rent_max", None))
                 if v is not None]
        if not rents:
            continue
        sf = max((v for v in (getattr(g, "sf_min", None), getattr(g, "sf_max", None))
                  if v is not None), default=None)
        why = None
        if max(rents) > _MAX_PLAUSIBLE_MONTHLY_RENT:
            why = f"${max(rents):,} > ${_MAX_PLAUSIBLE_MONTHLY_RENT:,}/mo"
        elif sf and sf >= _MIN_SF_FOR_PSF_TEST and max(rents) / sf > _MAX_PLAUSIBLE_RENT_PER_SF:
            why = f"${max(rents):,} on {sf}sf = ${max(rents)/sf:.2f}/sf"
        if why is None:
            continue
        hits.append(f"{g.beds}bd {why}")
        g.rent_min = None
        g.rent_max = None
        # The members are the same numbers the endpoints came from — leaving them
        # behind would let a consumer average per-unit values off a group whose
        # rent we just declared unreadable, which is the exact wrong-number-with-
        # more-confidence outcome this clamp exists to prevent.
        g.unit_rents = None
        g.flag_review = True

    if hits:
        result.flag_review = True
        note = f"{source}: nulled {len(hits)} implausible rent(s) [" + "; ".join(hits[:4]) + "]"
        result.scrape_error = f"{result.scrape_error}; {note}" if result.scrape_error else note
    return result


def null_impossible_llm_availability(result: "ExtractResult", source: str,
                                     public_record_units) -> "ExtractResult":
    """Null every count in an LLM read whose total exceeds the building.

    The LLM tier reads counts off prose, so a number that is not a count reaches
    `available`: Creekstone Apartments NSH stored 999 on three groups (3000 of 316
    units), The Cottages at Villa Rica ATL 101 on four (404 of 192). A total above
    public_record_units is impossible, and nothing says WHICH group is wrong, so every count in
    that read becomes None + flag_review. Rents are kept; no group is dropped.
    `available` and `coming_soon` are summed and nulled together, so a coming_soon-only
    overcount is caught too.

    LLM ONLY, BY DESIGN. An adapter counts member records, so its overcount indicts the
    seed's public_record_units rather than the read (Rasha at Audubon HOU enumerates 313 priced,
    dated units against a stale seed of 312; the developer's total is 326) — that stays
    visible on rents_dq.unit_reconcile instead of being nulled here. `len(unit_rents)` was
    considered as the discriminator and rejected: realpage
    records only priced members, the LLM caps observed rents at ten, and
    clamp_implausible_rents clears the list just before this runs.
    """
    if not (source or "").startswith("llm_"):
        return result
    try:
        cap = int(public_record_units)
    except (TypeError, ValueError):
        return result
    if cap <= 0:
        return result
    groups = result.units or []
    claimed = sum((g.available or 0) + (g.coming_soon or 0) for g in groups)
    if claimed <= cap:
        return result
    for g in groups:
        g.available = None
        g.coming_soon = None
        g.flag_review = True
    result.flag_review = True
    note = (f"{source}: nulled availability on {len(groups)} group(s) "
            f"[claimed {claimed} > public_record_units {cap}]")
    result.scrape_error = f"{result.scrape_error}; {note}" if result.scrape_error else note
    return result


def drop_null_bed_groups(result: "ExtractResult", source: str) -> "ExtractResult":
    """Remove unit groups whose `beds` could not be resolved, loudly.

    WHY: the project rules defines the weekly_snapshots.units shape as requiring `beds`, and
    every downstream consumer keys on it — eval_golden's bed_rollup SKIPS beds-null
    groups outright, so their rents and unit counts vanish from grading and from any
    bed-level consumer with no warning at all. Measured 2026-07-28: 104 communities
    carried one such group (86 rentcafe / 14 entrata / 2 knock / 2 llm_haiku),
    hiding 87 rents and 564 units; 8 communities were 100% invisible while still
    reporting `live_data`.

    The outcome is recorded in scrape_error + flag_review so it shows up in the run log
    and in flag_review_ceiling rather than passing quietly. Knock-on: if this empties
    `units` while scrape_error is set, the pipeline routes the community to the LLM tail
    (a second chance) or to `needs_adapter`.

    CORRECTION 2026-07-28, before this ever ran on real data: only groups carrying NO
    data are dropped. Measured on the live set, 65 communities had a beds-null group and
    for **48 of them it held the ONLY rent they have** — those rents do reach
    rents.v_latest_unit_groups and therefore the rent-comp consumer today, as an unattributed
    row. Dropping them would have destroyed real pricing for 48 communities in order to
    fix a GRADING-visibility problem. That trade is backwards: invisibility costs a
    golden grade, lost pricing costs a comp, and comps price deals.

    So: a beds-null group with a rent or availability is KEPT and flagged (the data
    survives, the defect is visible, and the beds_null_groups dq assertion tracks the
    remediation backlog). Only an empty beds-null group — no rent, no availability, pure
    parse residue — is removed.
    """
    groups = result.units or []
    bad = [g for g in groups if getattr(g, "beds", None) is None]
    if not bad:
        return result

    _carries_data = group_carries_data   # one definition, module level (2026-09-01)

    droppable = [g for g in bad if not _carries_data(g)]
    kept = [g for g in bad if _carries_data(g)]
    if droppable:
        drop_ids = {id(g) for g in droppable}
        result.units = [g for g in groups if id(g) not in drop_ids]
    # Flag the KEPT groups themselves, not just the snapshot. rents.v_latest_unit_groups
    # (migration 0022) exposes the GROUP flag; `result.flag_review` below is the SNAPSHOT
    # flag and never reaches that view. Without this a kept beds-null group arrives as
    # group_flag_review=false — a NULL-bed row carrying real money that reads as a clean,
    # attributed comp, which is the opposite of what the note two lines down claims about
    # it. Same pattern the implausible-rent guard already uses above (`g.flag_review = True`
    # before `result.flag_review = True`). Found 2026-08-18 by a Codex cross-review.
    for g in kept:
        g.flag_review = True
    result.flag_review = True
    bits = []
    if droppable:
        bits.append(f"dropped {len(droppable)} empty group(s) with unresolved beds")
    if kept:
        n_units = sum((g.available or 0) for g in kept)
        bits.append(f"KEPT {len(kept)} group(s) ({n_units} unit(s)) with unresolved beds "
                    f"but real data — invisible to bed-level consumers, needs a parser fix")
    note = f"{source}: " + "; ".join(bits)
    result.scrape_error = f"{result.scrape_error}; {note}" if result.scrape_error else note
    return result
