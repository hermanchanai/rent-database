"""Shared concession scanner — market-agnostic, no network, no LLM.

WHY THIS EXISTS: concession banners ("8 Weeks Free", "Two Months Free") live on
a community's MARKETING page (hero/header), but both extraction paths miss them:
  - vendor adapters (sightmap/rentcafe/amh/entrata) hit an availability API/embed
    that never includes the marketing banner;
  - the LLM path targets the availability page AND _trim_html() decomposes
    <header>, dropping the banner before the model sees it.
The golden-set eval (2026-07-04) caught 4 live Atlanta concessions missed this way
(Harmon Cedar Run 8wk, Summerwell Legacy 8wk, Parkwood 6wk, MacLand $500).

This module is the ONE place concession text is turned into weeks-free. It's
called as a fill-if-missing step in every path (LLM extractor + weekly landing
sweep), so a NEW MARKET gets concession recall for free — nothing per-adapter.

Convention (matches the project rules): "X weeks free"=X; "X months free"=X*4;
"$Y off"=weeks null unless a monthly rent is supplied to convert; rate-locks /
waived-fees / gift-cards / free-amenities are NOT free rent → weeks null.

Public API:
    scan_concession(text, monthly_rent=None) -> (weeks: int|None, raw: str|None)
    merge_concession(result, text, monthly_rent=None) -> None   # fill-if-missing
"""

from __future__ import annotations

import re
import threading
from typing import Optional, Tuple

_WORD_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "half": 0.5,  # "Half Month Free Rent" — real Tricon banner class
}
# Decimal support matters: "2.5 Months FREE" must parse as 2.5, not the bare "5"
# after the dot (golden-eval catch 2026-07-14: Broadstone read as 5 mo -> 20 wk).
# Fractions (2026-09-21): "1/2 Month Free" used to read as "2 Month" (8 weeks) because
# the grammar had no fraction form and `\b` sits between "/" and "2"; "Two and a half Months"
# read on its "half" token (2 weeks); "Half a Month" on its "a Month" (4 weeks). Fraction
# forms come FIRST in the alternation and `_NUM_START` refuses to begin a number right after
# a "/" or ".", so the last digit of a fraction or a decimal can never match alone.
_INT_WORD = r"(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
# The whole part of a mixed fraction is a NUMBER word: "a 1/2 month" is half a month, not
# one and a half.
_WHOLE = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_FRAC = r"(?:\u00bd|\d\s*/\s*\d)"
_NUM = (rf"(?:{_WHOLE}\s*(?:and|&)\s*{_FRAC}"               # "1 and 1/2", "1 & 1/2" (G5)
        rf"|{_WHOLE}?\s*{_FRAC}"                       # "1/2", "2 1/2", "\u00bd", "one \u00bd"
        rf"|{_WHOLE}\s+and\s+a\s+half"                  # "two and a half"
        rf"|half(?:\s+an?)?"                              # "half", "half a"
        rf"|\d{{1,2}}(?:\.\d{{1,2}})?"
        rf"|{_INT_WORD})")
# Where a number may START. `\b` would accept the "2" of "1/2", and a non-word "\u00bd" has
# no `\b` before it at all. "." is deliberately NOT refused: "2.5 Months" is consumed from
# its "2" by leftmost matching, while "PLUS...ONE MONTH FREE" needs a number after a dot.
_NUM_START = r"(?<![\w/])"
# "a month and a half free" / "free rent for a month and a half" — the half comes AFTER
# the unit; both month forms carry it.
_PLUS_HALF = r"(?P<plushalf>\s+and\s+a\s+half)?"

# num → unit → (short gap) → free.  "8 Weeks FREE", "3 MONTHS FREE", "Six Weeks Free"
# _HY: a hyphen between number and unit is a space ("2-Months FREE", "8-weeks free",
# "1-Month Free") -- but "6-month" is the ADJECTIVE of a lease term ("a 6-month lease"), so a
# hyphen before a SINGULAR unit counts only after one/1/half or a fraction. _*_OFF use it too;
# _DURATION and _UNIT_AT take any hyphen (they judge a match, never make one). The reversed
# forms are left alone: _is_tiered(main_tiers) reads them unvetoed (leg 2).
_HY = (r"(?:\s*-\s*(?=(?:weeks|wks|months|mos)\b)"
       r"|(?:(?<=\b1)|(?<=\bone)|(?<=half)|(?<=/\d)|(?<=/\s\d)|(?<=\u00bd))\s*-\s*|\s*)")
_WEEKS_RE = re.compile(rf"{_NUM_START}({_NUM}){_HY}(?:weeks?|wks?)\b[^.\n]{{0,12}}?\bfree\b", re.I)
_MONTHS_RE = re.compile(rf"{_NUM_START}({_NUM}){_HY}(?:months?|mos?){_PLUS_HALF}\b[^.\n]{{0,12}}?\bfree\b", re.I)
# reverse: "free ... N weeks/months" (e.g. "free rent for 2 months")
_WEEKS_REV = re.compile(rf"\bfree\b[^.\n]{{0,20}}?{_NUM_START}({_NUM})\s*(?:weeks?|wks?)\b", re.I)
_MONTHS_REV = re.compile(rf"\bfree\b[^.\n]{{0,20}}?{_NUM_START}({_NUM})\s*(?:months?|mos?){_PLUS_HALF}\b", re.I)
# "N months/weeks OFF" = free rent (golden-eval catch 2026-07-15: Avalon
# "2 months off"). The number must DIRECTLY precede the unit so "$110 per month
# off" (a $-amount, handled below) doesn't match — there the number binds to $,
# not to "month".
_WEEKS_OFF = re.compile(rf"{_NUM_START}({_NUM}){_HY}(?:weeks?|wks?)\s+off\b", re.I)
_MONTHS_OFF = re.compile(rf"{_NUM_START}({_NUM}){_HY}(?:months?|mos?)\s+off\b", re.I)
# ORDINAL month-free: "1st Full Month- FREE RENT", "First Full Month Free",
# "1st month free". An ordinal names WHICH month is free, not HOW MANY — it is
# always exactly ONE month, so this yields 4 weeks and captures no number.
# The cardinal spellings ("1 Month Free", "One Month Free!") already scored 4;
# every ordinal spelling scored None, which is how PHX's The Galleria read
# pipe=None against a site banner worth 4 weeks (golden-eval 2026-08-09).
# 🔴 The trailing \bfree\b is load-bearing: without it this would swallow
# "$1,000 OFF your first month's rent" (Celia, PHX mf) and silently promote every
# dollars-off-first-month banner into a 4-week concession. A $-amount off the
# first month is a DOLLAR offer and must keep falling through to step 3.
_ORDINAL_MONTH_FREE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)|first|second|third)\s+(?:full\s+)?month(?:['’]?s)?\b"
    r"[^.\n]{0,12}?\bfree\b", re.I)
# dollars-off-rent: "$500 off", "$500 First Month", "$1,000 off your first month's rent"
_DOLLAR_RE = re.compile(
    r"\$\s?([\d,]{2,7})\b[^.\n]{0,30}?(?:off|first\s+(?:full\s+)?month|credit\s+toward)", re.I)

# percent-off-rent (2026-09-21): "50% off first month rent", "Half Off Your First 3
# Months", "1/2 off September rent". The same rule as dollars-off — a discount on named
# months is a move-in credit, not ongoing relief — and the same outcome: banner kept,
# weeks refused. Recognized so the scanner can say "seen, uncountable" rather than
# "nothing here", which is what lets it overrule a model that counted it.
_PERCENT_RE = re.compile(
    r"(?<![\w/.])(?:\d{1,2}\s?(?:%|percent)|\u00bd|1/2|half)\s*(?:off|discount)\b"
    r"[^.\n]{0,40}?\b(?:rent|month)", re.I)

# "free" that is NOT free rent — don't let these anchor a match by accident.
_FREE_AMENITY = re.compile(
    r"\bfree\b\s+(?:wi-?fi|internet|parking|water|cable|gym|nights?|trial|"
    r"gift|application|app\b|move|consultation|tour|estimate)", re.I)
# a month free on a NON-RENT product named earlier in the offer's own sentence. Avalon
# Monrovia's site alert "Tour Second Space Monrovia — now offering 1 month free" is AvalonBay's
# coworking membership, and was stored as 4 weeks of free rent. The NAMED product only: a generic
# amenity ("Enjoy our coworking lounge and receive 1 month free on all apartments") describes the
# community the offer is for.
_NON_RENT_PRODUCT = re.compile(r"\bsecond\s+space\b", re.I)


def _non_rent_product(text: str, at: int) -> bool:
    """Does a non-rent product precede the offer at `at` inside its own sentence?"""
    head = text[max(0, at - 160):at]
    cut = max(head.rfind(ch) for ch in ".!?\n")
    return bool(_NON_RENT_PRODUCT.search(head[cut + 1:]))


# rate-lock language ("20-month lease at the 12-month rate") is not free rent.
_RATE_LOCK = re.compile(r"\b(?:lease\s+rate|rate\s+for\s+the\s+duration|rate\s+lock)\b", re.I)

# --- named-subset veto (SOP step 6, "What select homes means") --------------
# concession_weeks is SITE-LEVEL: NER applies it to EVERY unit. An offer scoped to
# a NAMED bed type is a per-floorplan special and must NOT be recorded as weeks
# (HSV The Gabriel: "up to 8 weeks free" on "our premium 2-bedroom floorplans" —
# booking 8 there discounts all 51 units and understates rent community-wide).
# An UNSPECIFIED subset ("select homes") is ordinary marketing copy and DOES
# record N — that is what _SELFTEST below has encoded since 2026-07-04.
_BED_SCOPE = re.compile(
    r"(?:\bstudios?\b"
    r"|\b\d{1,2}\s*-\s*bed(?:room)?s?\b"
    r"|\b\d{1,2}\s+bed(?:room)?s?\b"
    # abbreviations are how tiered per-bed discounts are actually written:
    # "1BR: $200 Off * 2BR: $250 Off" (HSV The Collier). Without these the veto
    # never fires on the most common per-floorplan dollar banner.
    r"|\b\d{1,2}\s*(?:br|bd|bhk)\b"
    r"|\b(?:one|two|three|four|five)[\s-]+bed(?:room)?s?\b)", re.I)
# The PLURAL abbreviation is a scope too -- "10 weeks free on 3BRs" -- but only inside
# the offer's OWN clause: across a sentence it is inventory or price copy ("2BRs available.
# Receive 6 weeks free!", "10 weeks free base rent. 1BRs starting at $1,649"; Codex diff review
# of). `_nearest_bed` applies the clause bound; _BED_SCOPE keeps main's reach unchanged.
_BED_SCOPE_PLURAL = re.compile(r"\b\d{1,2}\s*(?:brs|bds)\b", re.I)
# The qualifier must ATTACH to a dwelling noun. A bare "qualifying"/"select" is
# boilerplate: Gabriel ends "subject to approval and qualifying lease terms",
# which qualifies the LEASE — matching it there vetoed a real named subset.
_UNSPEC_SUBSET = re.compile(
    r"\b(?:select|selected|certain|participating|qualifying)\b"
    r"(?:\W+\w+){0,4}?\W+(?:homes?|apartments?|units?|residences?|floor\s*plans?|plans?)\b", re.I)

# ONE offer that ENUMERATES bed types — "one and two bed", "1 & 2 BR", "1-2 bedroom",
# "one, two and three bedrooms". This is a community-wide offer written the way
# leasing pages write them, NOT the per-floorplan carve-out _BED_SCOPE is looking
# for, so it cancels that veto (2026-08-13).
#
# The bug it fixes: parkat33rd.com advertises "up to 8 weeks FREE one and two bed"
# and the scrape stored concession_weeks = NULL, because _BED_SCOPE saw "two bed"
# beside the offer and _is_named_subset dropped the 8. Net-effective rent then reads
# as if the property gave nothing away. "N weeks free on 1 & 2 bed" is one of the
# most common banner shapes there is, so this was silently under-reporting fleet-wide.
#
# The items must be ADJACENT, separated only by commas and a conjunction. That is
# what keeps a TIERED banner vetoed: "1BR: $200 Off • 2BR: $250 Off" (HSV The
# Collier) names two bed types too, but each carries its own price, so no
# conjunction sits between them and this pattern cannot match.
#
# Residual, accepted: at a property with 1/2/3BR, "1 & 2 bed only" really is a
# subset and will now be recorded community-wide. That errs toward recording a
# concession that exists rather than erasing one — and the text alone cannot tell
# the two apart; only the property's bed mix could, which scan_concession never sees.
_BED_LIST_NUM = r"(?:\d{1,2}|one|two|three|four|five)"
_BED_LIST_NOUN = r"(?:bed(?:room)?s?|brs?|bds?)"   # "1 & 2 BRs" is a list too
# `-?` after each list number: "1- and 2-Bedroom" writes the shared noun once
# and leaves a dangling hyphen on every earlier item; without it that hyphen was read as
# the list connector and the list never matched. `,?` before the connector: "1, 2, & 3".
# The dangling hyphen may carry a space before it: "1 -and 2-Bedroom", "1 - & 2 - Bedrooms"
# (leg 3; a stored prod banner lost its 8 weeks to it).
_MULTI_BED_SCOPE = re.compile(
    rf"\b{_BED_LIST_NUM}(?:\s*-)?\s*(?:{_BED_LIST_NOUN})?"
    rf"(?:\s*,\s*{_BED_LIST_NUM}(?:\s*-)?\s*(?:{_BED_LIST_NOUN})?)*"
    rf"\s*,?\s*(?:&|and|or|/|-|–|to)\s*"
    rf"{_BED_LIST_NUM}(?:\s*-)?\s*{_BED_LIST_NOUN}\b", re.I)
_SCOPE_BEFORE, _SCOPE_AFTER = 95, 60

# --- explicit community-wide marker (a past bug) ------------------------------
# An offer can say outright that it applies to the whole property, and then a bed
# type that appears NEARBY is describing inventory, not scoping the offer. Codex,
# 2026-09-21, reviewing the reverted delegation attempt: for "Enjoy 6 weeks free on
# all homes. Spacious 3 bedroom homes available." `_is_named_subset` returns True
# and the 6 is erased. That is the row's own FAIL condition — "a community that
# legitimately advertises a community-wide offer loses its concession because a
# guard is too eager" — and it matters most for the seven ADAPTERS, which scan full
# page text where adjacent inventory copy is the norm rather than the exception.
#
# NEAREST MARKER WINS, the same doctrine `_is_named_subset` already uses for
# unspecified subsets. "6 weeks free on 2BR. Every home has a garage." keeps its
# veto because the bed marker is the nearer of the two.
_COMMUNITY_WIDE = re.compile(
    r"\b(?:all|any|every|each)\b(?:\W+\w+){0,2}?\W+"
    r"(?:homes?|apartments?|units?|residences?|floor\s*plans?|plans?|leases?)\b"
    r"|\b(?:community|property)[\s-]?wide\b", re.I)


def _nearest(rx: "re.Pattern[str]", text: str, start: int, end: int) -> Optional[int]:
    """Character distance from the matched offer to the nearest hit of `rx`, or None."""
    lo = max(0, start - _SCOPE_BEFORE)
    best: Optional[int] = None
    for m in rx.finditer(text[lo:start]):          # before: measure from its END
        d = start - (lo + m.end())
        best = d if best is None else min(best, d)
    for m in rx.finditer(text[end:end + _SCOPE_AFTER]):   # after: measure from its START
        best = m.start() if best is None else min(best, m.start())
    return best


# A banner is a run of separate claims. Only a marker in the OFFER'S OWN clause is
# describing the offer; anything past a sentence end or a bullet is a different claim.
_CLAUSE_BREAK = re.compile(r"[.!?;|\u2022\u00b7]")


# A plural bed count's claim also ends at a comma or a line break: "2BRs available, Receive 6
# weeks free!" is inventory copy.
_PLURAL_BREAK = re.compile(r"[.!?;|\u2022\u00b7,\r\n]")


def _nearest_bed(text: str, start: int, end: int) -> Optional[int]:
    """`_nearest(_BED_SCOPE)`, plus a plural abbreviation inside the offer's own clause."""
    best = _nearest(_BED_SCOPE, text, start, end)
    lo = max(0, start - _SCOPE_BEFORE)
    breaks = list(_PLURAL_BREAK.finditer(text, lo, start))
    if breaks:
        lo = breaks[-1].end()
    tail = text[end:end + _SCOPE_AFTER]
    b = _PLURAL_BREAK.search(tail)
    tail = tail[:b.start()] if b else tail
    for m in _BED_SCOPE_PLURAL.finditer(text, lo, start):
        d = start - m.end()
        best = d if best is None else min(best, d)
    for m in _BED_SCOPE_PLURAL.finditer(tail):
        best = m.start() if best is None else min(best, m.start())
    return best


def _is_tiered(text: str, main_tiers: bool = False) -> bool:
    """True when the text offers TWO DIFFERENT amounts of free rent.

    That is the signature of a per-floorplan deal however the bed types are
    written, and it is what stops the multi-bed relaxation below from swallowing a
    genuinely tiered banner. HOU Veranda Dellbrook, verbatim from the DB: "Eight
    Weeks Free On 1 & 3 Bedrooms, Six Weeks Free On 2 & 4 Bedrooms" — every tier
    enumerates several bed types, so a conjunction test alone reads it as
    community-wide and records the better of the two offers for all units.

    Found by running the fixed parser against all 1,187 distinct banners stored in
    rents.weekly_snapshots: this was the only unintended behaviour change in the
    set, which is also the reason the check is one line rather than a heuristic.
    """
    vals: set[float] = set()
    if main_tiers:
        # local_concession (the seven adapters' private regexes, until M4 C1 retires them)
        # keeps main's tier reading exactly: its own regex can hold an amenity month ("1 month
        # free parking"), and main's veto is what kept that month out of storage
        for rx, mult in ((_WEEKS_RE, 1), (_WEEKS_REV, 1), (_WEEKS_OFF, 1),
                         (_MONTHS_RE, 4), (_MONTHS_REV, 4), (_MONTHS_OFF, 4)):
            for m in rx.finditer(text):
                n = _num_from_match(m)
                if n:
                    vals.add(round(n * mult))
        return len(vals) > 1
    for rx, form, mult, rate_lock in _COUNTED_FORMS:
        # a tier is an offer the scanner would ACCEPT -- an amenity month ("1 month
        # free parking") or a move-in deadline ("within 2 weeks") is not a second amount of
        # free rent, and counting it vetoed the real offer beside a bed enumeration
        for m in _accepted(rx, text, form, rate_lock):
            n = _num_from_match(m)          # the trailing "and a half" counts
            if n:
                vals.add(round(n * mult))
    return len(vals) > 1


def _is_named_subset(text: str, start: int, end: int, main_tiers: bool = False) -> bool:
    """True when the matched offer is scoped to a NAMED bed type rather than an
    unspecified 'select homes' subset.

    NEAREST MARKER WINS. A banner routinely carries both — HSV The Collier reads
    "Save Up to $350/Month on Select Apartments | 1BR: $200 Off • 2BR: $250 Off",
    where the per-bed-type discount sits adjacent to the match and the "Select
    Apartments" copy is further away. Letting "unspecified" win globally scored
    that as a community-wide 1-week concession. Conversely HSV The Kelvin says
    "Luxury 1 Bedrooms Starting at $999! ... 1 Month FREE on Select Apartment
    Homes" — there the unspecified marker is adjacent and the bed count belongs
    to a price promo, so it correctly records 4."""
    nb = _nearest_bed(text, start, end)
    if nb is None:
        return False
    # An ENUMERATION of bed types is one community-wide offer, not a carve-out —
    # and it always starts at or before the single-type marker inside it, so a
    # nearer-or-equal multi-bed hit means the bed marker that triggered the veto is
    # part of the list. "<=" and not "<": for "8 weeks free 1 & 2 bed" the list and
    # its own last item can tie once the offer sits to the left of both.
    nm = _nearest(_MULTI_BED_SCOPE, text, start, end)
    if nm is not None and nm <= nb and not _is_tiered(text, main_tiers):
        return False
    nu = _nearest(_UNSPEC_SUBSET, text, start, end)
    return nu is None or nb < nu


def _offer_is_community_wide(text: str, start: int, end: int) -> bool:
    """True when an explicit community-wide marker sits in the offer's OWN clause.

    Codex, round 2 of the 2026-09-21 review: "All homes receive 6 weeks free! Studios
    available." lost its 6, because the nearest-marker rule this started as measured
    "Studios" (2 chars after) against "All homes" (11 before) and let the nearer one
    win. Distance cannot tell an offer's own scope from adjacent inventory copy —
    ATTACHMENT can, and a clause boundary is where attachment ends. The reverse case
    is what keeps the veto honest: in "6 weeks free on 2BR. Every home has a garage."
    the marker is a separate sentence about an amenity, so the 2BR scope stands.

    A marker whose own span names a bed type is excluded: "all 2 bedroom homes"
    quantifies that bed type, not the property, and must not cancel its own veto.
    """
    lo = max(0, start - _SCOPE_BEFORE)
    for m in _COMMUNITY_WIDE.finditer(text[lo:start]):
        if _BED_SCOPE.search(m.group(0)) or _BED_SCOPE_PLURAL.search(m.group(0)):
            continue
        if not _CLAUSE_BREAK.search(text[lo + m.end():start]):
            return True
    for m in _COMMUNITY_WIDE.finditer(text[end:end + _SCOPE_AFTER]):
        if _BED_SCOPE.search(m.group(0)) or _BED_SCOPE_PLURAL.search(m.group(0)):
            continue
        if not _CLAUSE_BREAK.search(text[end:end + m.start()]):
            return True
    return False


def _scope_window(text: str, start: int, end: int) -> str:
    """The text `offer_is_bed_scoped` actually looks at, whitespace-normalized.

    Every marker that can veto an offer — `_BED_SCOPE`, `_MULTI_BED_SCOPE`,
    `_UNSPEC_SUBSET`, `_COMMUNITY_WIDE` — is searched within
    [start - _SCOPE_BEFORE, end + _SCOPE_AFTER], so returning that whole span makes the
    stored banner self-describing: anyone (or anything) re-reading it sees why the week
    count was dropped. Wider than `_snippet`'s 15-char lookbehind on purpose.
    """
    lo = max(0, start - _SCOPE_BEFORE)
    hi = min(len(text), end + _SCOPE_AFTER)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def offer_is_bed_scoped(text: str, start: int, end: int, main_tiers: bool = False) -> bool:
    """True when the offer at [start, end) is scoped to a NAMED bed type.

    The adapter-facing form of `_is_named_subset`, and the ONLY piece of the shared
    rule the seven local scanners borrow (a past bug). It adds the community-wide
    override. The 2026-09-21 review found four regressions in making these adapters
    delegate wholesale, so they keep their own patterns and take this one check.
    Since the round `scan_concession` calls it too: "All homes receive 6 weeks
    free! Studios available." was vetoed on the shared path and kept on the adapter
    path, and the LLM override made that split visible.
    """
    if _nearest_bed(text, start, end) is None:
        return False
    # `_is_tiered` still wins: a banner quoting two different amounts of free rent is
    # per-floorplan however community-wide its wording sounds.
    if _offer_is_community_wide(text, start, end) and not _is_tiered(text, main_tiers):
        return False
    return _is_named_subset(text, start, end, main_tiers)


def local_concession(text: str, weeks_re: "re.Pattern[str]", months_re: "re.Pattern[str]",
                     months_first: bool = False) -> Tuple[Optional[int], Optional[str]]:
    """Run an ADAPTER'S OWN weeks/months patterns, vetoing a named-bed-type offer.

    One implementation of the guard rather than seven, while each adapter keeps
    the patterns and the match ORDER it already had (entrata reads months first; the
    rest read weeks first). On a veto this returns `(None, raw)` — the banner is kept
    and only the week count is dropped, matching `scan_concession`'s contract.
    """
    order = ((months_re, 4), (weeks_re, 1)) if months_first else ((weeks_re, 1), (months_re, 4))
    for rx, mult in order:
        m = rx.search(text)
        while m and _non_rent_product(text, m.start()):     # not a rent offer; read on
            m = rx.search(text, m.end())
        if not m:
            continue
        try:
            n = float(m.group(1))
        except ValueError:
            continue
        if offer_is_bed_scoped(text, m.start(), m.end(), main_tiers=True):
            # 🔴 THE VETO'S RAW TEXT MUST CARRY THE SCOPE THAT DROVE IT.
            #
            # `the pipeline.finalize_result` repairs in place whenever
            # `concession_weeks is None and raw_concession_text` — precisely the state a
            # veto produces — by calling `merge_concession(result, raw)`, which re-runs
            # `scan_concession` on that text alone. Returning the bare match
            # ("8 weeks FREE") strips the bedroom scope, so the repair step reads a
            # community-wide offer and puts the 8 straight back. Codex found this in the
            # 2026-09-21 review of this fix; every adapter reproduced it.
            #
            # `_scope_window` is the exact span `offer_is_bed_scoped` consulted, so
            # re-scanning the returned text reaches the same verdict by construction.
            raw = _scope_window(text, m.start(), m.end())
            # r5: a comma now ends a plural bed count's claim, so a window cut before a
            # later tier's own scope ("..., 10 weeks free" without "on 3BRs") can re-read as a
            # site-level count. Only then widen to the shared veto raw, which re-reads to None.
            if scan_concession(raw)[0] is not None:
                raw = _veto_raw(text, m.start(), m.end())
            return None, raw
        # a banner that states its own TOTAL ("6 weeks FREE ...
        # an additional 2 weeks FREE (8 Weeks Total)") means the total, and finalize_result
        # never repairs or sweeps a non-null count -- so the adapter path must read it here.
        tot = _stated_total(text, _counted_offers(text))
        if (tot and tot[0] > round(n * mult)
                and not offer_is_bed_scoped(text, tot[1].start(), tot[1].end(), main_tiers=True)):
            return tot[0], _snippet(text, tot[2], tot[1].end())
        return int(round(n * mult)), m.group(0)
    return None, None


def _to_num(token: str) -> Optional[float]:
    token = re.sub(r"\s+", " ", token.strip().lower()).replace("\u00bd", " 1/2").strip()
    token = re.sub(r"\s+", " ", token)                            # "1& \u00bd" -> "1&  1/2"
    token = re.sub(r"^(\S+?) ?(?:and|&) ?(?=\d ?/)", r"\1 ", token)  # "1 and 1/2" = "1 1/2" (G5)
    m = re.fullmatch(r"(?:(\S+) )?(\d) ?/ ?(\d)", token)          # "1/2", "2 1/2"
    if m:
        whole = _to_num(m.group(1)) if m.group(1) else 0
        if whole is None or int(m.group(3)) == 0:      # "1/0" is refused, not raised
            return None
        return whole + int(m.group(2)) / int(m.group(3))
    m = re.fullmatch(r"(\S+) and a half", token)                  # "two and a half"
    if m:
        whole = _to_num(m.group(1))
        return None if whole is None else whole + 0.5
    if token.startswith("half"):                                  # "half", "half a"
        return 0.5
    try:
        return float(token)
    except ValueError:
        return _WORD_NUM.get(token)


def _num_from_match(m: "re.Match[str]") -> Optional[float]:
    """The matched number, plus the "and a half" that may trail the unit."""
    n = _to_num(m.group(1))
    if n is not None and m.groupdict().get("plushalf"):
        n += 0.5
    return n


def _snippet(text: str, start: int, end: int, width: int = 130) -> str:
    lo = max(0, start - 15)
    hi = min(len(text), end + width)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def _veto_raw(text: str, start: int, end: int) -> str:
    """The raw a VETOED offer returns. `finalize_result` re-scans any weeks-None banner
    (`merge_concession`), so the raw must carry everything that drove the veto: the bed
    scope `offer_is_bed_scoped` reads (up to _SCOPE_BEFORE chars before the offer,)
    AND the tier evidence `_is_tiered` reads, which HEAD's 130-char snippet kept after
    the offer. The union of the two windows."""
    lo = max(0, start - max(_SCOPE_BEFORE, 15))
    hi = min(len(text), end + max(_SCOPE_AFTER, 130))
    raw = re.sub(r"\s+", " ", text[lo:hi]).strip()
    if getattr(_VETO_CHECK, "active", False):
        return raw
    # 🔴 THE RAW MUST RE-READ AS A VETO (design review r5, finding 2). `_is_tiered`
    # reads the WHOLE text, so a second tier further away than the window was cut off and
    # finalize_result's merge_concession(raw) put the vetoed count back ("8 weeks free on 1 & 2
    # bedrooms. <130+ chars of amenities> 6 weeks free on 3 & 4 bedrooms." -> None -> 8).
    # Widen to every offer the tier check read; the last resort is the text itself, which
    # re-reads to this very veto.
    spans = [(start, end)] + [m.span() for rx, form, _mult, rate_lock in _COUNTED_FORMS
                              for m in _accepted(rx, text, form, rate_lock)]
    wide_lo = max(0, min(a for a, _ in spans) - _SCOPE_BEFORE)
    wide_hi = min(len(text), max(b for _, b in spans) + max(_SCOPE_AFTER, 130))
    for cand in (raw, re.sub(r"\s+", " ", text[wide_lo:wide_hi]).strip(),
                 re.sub(r"\s+", " ", text).strip()):
        if _rescans_to_none(cand):
            return cand
    return text


_VETO_CHECK = threading.local()


def _rescans_to_none(raw: str) -> bool:
    _VETO_CHECK.active = True
    try:
        return scan_concession(raw)[0] is None
    finally:
        _VETO_CHECK.active = False


_FREE_WORD = re.compile(r"\bfree\b", re.I)


def _amenity_offer(text: str, m: "re.Match[str]") -> bool:
    """Is THIS match a free-AMENITY offer ("1 month free parking")?

    A FORWARD offer ends in its own 'free' ("6 weeks free", "1 month free"), so the
    amenity must be that free's object: a separate promo nearby ("6 weeks free rent +
    free parking") does not cancel a rent offer. Every other form
    -- reversed ("free + 1 month" can borrow ANOTHER offer's 'free'), "off", ordinal,
    dollar -- keeps the window test it always had."""
    frees = list(_FREE_WORD.finditer(m.group(0)))
    if frees and frees[-1].end() == len(m.group(0).rstrip()):
        at = m.start() + frees[-1].start()
        # G1/G2: a duration cannot be the length of a free move-in or a free
        # application, so after a FORWARD offer's own free those words start the next claim:
        # "4 WEEKS FREE MOVE-IN BY 9/30", "1 MONTH FREE APP & ADMIN PAID AT MOVE-IN"
        return bool(_FREE_AMENITY.match(text, at)) and not _FREE_NOT_A_TERM.match(text, at)
    return bool(_FREE_AMENITY.search(text[max(0, m.start() - 5):m.end() + 25]))


# Only where the words after "move-in" / "app" END the claim (a deadline, "special", "or",
# punctuation): "2 weeks free move-in assistance" and "1 month free application processing"
# name a free SERVICE and stay amenities.
_MONTH_NAME = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
_FREE_NOT_A_TERM = re.compile(
    r"\bfree\b\s+(?:move[\s-]*in\b(?=\s*(?:$|[!.,;:*|\u2022]|\d|(?:specials?|ready|by|before|"
    rf"until|or|on|in|this|today|now|{_MONTH_NAME})\b))"
    r"|app(?:lication)?\s*(?:&|and)\s*admin\s+(?:fees?\s+)?(?:paid|waived)\b)", re.I)


# --------------------------------------------------------------------------- #
# / deltas (2026-09-26).
#
# Two earlier redesigns REPLACED main's reading (a wider regex window; a tokenizer that
# assigned every free to one duration) and each lost 4-5 Codex rounds to a new text one
# gap-shape over (fix/1502-scanner-tokenizer, LOOP_STOP.md x2). This rebuild keeps main's
# patterns and its weeks-then-months order as the baseline. Each delta below fires on ONE
# named shape; where none fires the reading is main's by construction, and every change it
# makes is measured over the production texts before review
# (_review/scanner_rebuild_2026-09-26/scanner_differential.py).
#
#   D1 a newline between a unit and its "free" is whitespace ........ _join_split_offers
#   D2 a duration right after within/by/before/until/after is a
#      deadline, never an offer, in every form ........................ _is_deadline
#   D3 forward offer + "free move-in" / "free application" ........... _amenity_offer
#   D4 "1 and 1/2" = 1.5 ............................................... _NUM, _to_num
#   D5 an offer introduced as a stated total, larger than every other
#      offer (it sums them), wins ....................................... _preferred_offer
#   D6 a forward gap never spans another duration ...................... _gap_holds_duration
#   D7 beside named tiers, the "all other plans/homes" offer wins, unless
#      a plain "select homes" tier is present (SOP step 6 keeps its N) .... _preferred_offer
#   D8 a REJECTED match no longer hides one that starts inside it ...... _accepted
#   D9 _is_tiered counts only accepted offers ......................... _is_tiered
#   D10 a duration marked as a LEASE TERM is never an offer ........... _is_term
#   D12 a plain "on select <homes>" offer beats a tier whose own clause
#       names a bed type or a beds x baths config ("our 1x1 floorplans") . _preferred_offer
#   D13 an offer in an explicitly community-wide clause ("on all apartment homes"),
#       beside offers that each name a bed type, is the site-level count . _community_wide_offer
#   D5 also reads a total stated AFTER the number: "(8 Weeks Total)" ...... _stated_total
_FWD, _REV, _OFF, _ORD = "forward", "reversed", "off", "ordinal"
_WEEK_FORMS = ((_WEEKS_RE, _FWD), (_WEEKS_REV, _REV), (_WEEKS_OFF, _OFF))
_MONTH_FORMS = ((_MONTHS_RE, _FWD), (_MONTHS_REV, _REV), (_MONTHS_OFF, _OFF))
#: (pattern, form, weeks multiplier, rate-lock veto) -- main applies the rate-lock veto to
#: the month forms only, and so does every reader here
_COUNTED_FORMS = tuple((rx, f, 1, False) for rx, f in _WEEK_FORMS) + \
    tuple((rx, f, 4, True) for rx, f in _MONTH_FORMS)

_UNIT = r"(?:weeks?|wks?|months?|mos?)"
# D2. "Move in within 2 weeks", "if leased within 2 weeks", "Apply within 2 weeks! Free rent":
# the number is WHEN, not how much. Only these words, immediately before the number: "for",
# "in", "the first/next" all introduce the length of real offers ("free rent for 2 months",
# "Free rent in the first 2 months"), and so does "after" ("After 1 month free, standard rent
# applies." is a 1-month offer -- Codex design review r5).
_DEADLINE_BEFORE = re.compile(r"\b(?:within|by|before|until|till)(?:\s+the\s+next)?\s+$", re.I)
# D6. "50% off for 3 months OR 1 month free": the forward gap reached across "1 month" and
# counted the %-off duration as 12 free weeks (G3).
_DURATION = re.compile(rf"{_NUM_START}{_NUM}\s*-?\s*{_UNIT}\b", re.I)
_UNIT_AT = re.compile(rf"\s*-?\s*{_UNIT}(?:\s+and\s+a\s+half)?\b", re.I)
# D1. A text node's own newline ("<h2>6 weeks\nFREE</h2>", get_text keeps it) between the
# unit and the offer's free. Refused when the free is an amenity's ("3 months\nFREE parking")
# or heads its own reversed offer ("Lease for 6 months\nFree rent for 2 months!").
_SPLIT_OFFER = re.compile(rf"{_UNIT}(?:['’]?s)?\b([ \t]*[\r\n][ \t\r\n]*)(?=free\b)", re.I)
# D5. "... PLUS TWO WEEKS FREE in November! That's 4 WEEKS FREE across two months!" (G4)
_TOTAL_BEFORE = re.compile(
    r"\b(?:that['’]?s|that\s+is|a\s+total\s+of|totaling|totalling)\s+(?:up\s+to\s+)?$", re.I)
# D5, stated AFTER the number: "... an additional 2 weeks FREE (8 Weeks Total)".
_TOTAL_AFTER = re.compile(
    rf"{_NUM_START}({_NUM})\s*-?\s*(weeks?|wks?|months?|mos?)\s+(?:free\s+)?total\b", re.I)
_TOTAL_JOIN = re.compile(r"[\s(\[,:=\u2013\u2014-]*")
# D7. "6 weeks free on all vacant A1 floor plans and 1 month free on all other vacant floor
# plans!" / "... All other floor plans: 1 MONTH FREE!" (G7). The marker must not name a
# bed type: "4 weeks free on all other 1 bedroom floor plans" is itself a bed tier.
_DWELLING = r"(?:apartment\s+homes?|homes?|apartments?|units?|residences?|townhomes?|floor\s*plans?|plans?)"
_ALL_OTHER_AFTER = re.compile(
    rf"[^.!?;|\u2022\n]{{0,20}}?\bon\s+all\s+other\b((?:\W+\w+){{0,3}}?\W+{_DWELLING})\b", re.I)
_ALL_OTHER_LABEL = re.compile(
    rf"\ball\s+other\b((?:\W+\w+){{0,3}}?\W+{_DWELLING})\s*:\s*$", re.I)


# D10. D2 removes the deadline main used to (wrongly) count in "Lease for 6 months, free! Move
# in within 2 weeks.", which exposes the lease term to the forward form (-> 24). A duration
# introduced by LEASE wording ("Lease for", "Lease:", "Lease terms:") or followed by lease/term
# is the length of the lease, not of the offer. Bare "with a" / "minimum" / "sign a" are not
# lease evidence: "Move in with a 1 month free special!" and "Minimum 6 weeks free on all
# homes!" are offers.
_TERM_BEFORE = re.compile(r"\b(?:lease\s+for|leases?(?:\s+(?:terms?|length))?\s*:|terms?\s*:)\s*$", re.I)
_TERM_AFTER = re.compile(r"[\s-]*(?:lease|leases|term|terms)\b", re.I)


def _is_term(text: str, m: "re.Match[str]") -> bool:
    u = _UNIT_AT.match(text, m.end(1))
    return bool(_TERM_BEFORE.search(text[max(0, m.start(1) - 24):m.start(1)])
                or (u and _TERM_AFTER.match(text, u.end())))


def _is_deadline(text: str, num_start: int) -> bool:
    return bool(_DEADLINE_BEFORE.search(text[max(0, num_start - 24):num_start]))


def _gap_holds_duration(text: str, m: "re.Match[str]") -> bool:
    """FORWARD form only: does the gap between the unit and the free hold another duration?"""
    u = _UNIT_AT.match(text, m.end(1))
    frees = list(_FREE_WORD.finditer(m.group(0)))
    if not u or not frees:
        return False
    return bool(_DURATION.search(text[u.end():m.start() + frees[-1].start()]))


def _accepts(text: str, m: "re.Match[str]", form: str, rate_lock: bool) -> bool:
    """Main's vetoes (amenity; rate-lock on the month forms) plus D2, D6 and D10."""
    if _amenity_offer(text, m) or _non_rent_product(text, m.start()):
        return False
    if rate_lock and _RATE_LOCK.search(text[max(0, m.start() - 5):m.end() + 25]):
        return False
    if form != _ORD and (_is_deadline(text, m.start(1)) or _is_term(text, m)):
        return False
    return not (form == _FWD and _gap_holds_duration(text, m))


def _accepted(rx: "re.Pattern[str]", text: str, form: str, rate_lock: bool):
    """Every match of `rx` the scanner accepts, in text order.

    D8. `finditer` resumes after a match's END, so a rejected match hid any offer that
    starts inside it: "2 months free parking + free 6 weeks rent" (the reversed match from
    the parking free is rejected, and the rent's own "free 6 weeks" was never tried), "50% off
    for 3 months OR 1 month free" once D6 rejects the first reading. After a REJECTION the
    search resumes just past the rejected match's number (forward/off forms) or its first
    free (reversed/ordinal); after an ACCEPTED match it resumes at the end, as main does.
    """
    pos = 0
    while pos <= len(text):
        m = rx.search(text, pos)
        if not m:
            return
        if _accepts(text, m, form, rate_lock):
            yield m
            pos = max(m.end(), pos + 1)
        elif form in (_FWD, _OFF):
            pos = max(m.end(1), pos + 1)
        else:
            pos = m.start() + 1


def _count(n: Optional[float], mult: int) -> Optional[int]:
    """Main's accepted ranges: weeks 1..26, months 0.4..6 (-> weeks, capped 1..26)."""
    if not n:
        return None
    if mult == 1:
        return round(n) if 1 <= n <= 26 else None
    return min(max(round(n * 4), 1), 26) if 0.4 <= n <= 6 else None


def _heads_reversed_offer(text: str, at: int) -> bool:
    """Does the free at `at` start its OWN reversed offer ("Free rent for 2 months")?"""
    for rx in (_WEEKS_REV, _MONTHS_REV):
        m = rx.match(text, at)
        if m and not _is_deadline(text, m.start(1)):
            return True
    return False


def _join_split_offers(text: str) -> str:
    """D1: the same text, with a newline between a unit and its offer's free read as a space.
    Same length, so every offset (snippets, scope windows) still points at the original."""
    spans = []
    for m in _SPLIT_OFFER.finditer(text):
        at = m.end()
        # the same amenity test a one-line forward offer gets in _amenity_offer, including
        # its G1/G2 exemptions ("4 weeks\nFREE MOVE-IN BY 9/30"): without them a
        # kept newline cost an offer the flattened text read (Codex diff review r2)
        amenity = _FREE_AMENITY.match(text, at) and not _FREE_NOT_A_TERM.match(text, at)
        if amenity or _heads_reversed_offer(text, at):
            continue
        spans.append(m.span(1))
    if not spans:
        return text
    out, last = [], 0
    for a, b in spans:
        out += [text[last:a], " " * (b - a)]
        last = b
    out.append(text[last:])
    return "".join(out)


# D12 / D7. A beds x baths configuration names a floor plan exactly as a bed type does
# ("2 week free on our 1x1 floorplans", "select 1x1 floor plans"). Used ONLY to judge the two
# preference rules below, never as a veto of its own.
_CONFIG = re.compile(r"\b\d\s*x\s*\d(?:s)?\b", re.I)
_PLAIN_SELECT_CLAUSE = re.compile(
    rf"\W*(?:(?:base\s+)?rent\s+)?on\s+(?:select|selected)\s+((?:\w+\s+){{0,2}}?{_DWELLING})\b", re.I)


def _names_a_plan(s: str) -> bool:
    return bool(_BED_SCOPE.search(s) or _BED_SCOPE_PLURAL.search(s) or _CONFIG.search(s))


_ATTACHED_BREAK = re.compile(r"[.!?;|\u2022\u00b7,]")


def _own_clause(hay: str, m: "re.Match[str]", attached: bool = False) -> str:
    """The text after an offer up to its clause break (at most _SCOPE_AFTER chars); with
    `attached`, up to the first comma too -- the scope attached to the offer itself."""
    tail = hay[m.end():m.end() + _SCOPE_AFTER]
    b = (_ATTACHED_BREAK if attached else _CLAUSE_BREAK).search(tail)
    return tail[:b.start()] if b else tail


def _plain_select_anywhere(hay: str) -> bool:
    """Does the banner scope ANY offer to an unspecified subset ("select homes")?"""
    return any(not _names_a_plan(u.group(0)) for u in _UNSPEC_SUBSET.finditer(hay))


def _preceding_scope_names_a_plan(hay: str, m: "re.Match[str]", offers) -> bool:
    """Does a bed type / config PRECEDE this offer inside its own clause?

    "6 weeks free on studios; For 2 bedroom homes, get 4 weeks free on all other floor plans."
    -- the scope before the offer is as much its own as the one after it. The segment runs back to the clause break; if another offer starts inside
    it, the bed type there is THAT offer's trailing scope ("6 weeks FREE on select 2-bedroom
    apartments or 1 month FREE on all other apartment homes"), not this one's."""
    lo = max(0, m.start() - _SCOPE_BEFORE)
    breaks = list(_CLAUSE_BREAK.finditer(hay, lo, m.start()))
    if breaks:
        lo = breaks[-1].end()
    if any(o is not m and lo <= o.start() < m.start() for _, o in offers):
        return False
    return _names_a_plan(hay[lo:m.start()])


def _counted_offers(hay: str):
    """Every accepted, countable free-rent offer except the "off" forms: (weeks, match), in
    text order."""
    offers = []
    for rx, form, mult, rate_lock in _COUNTED_FORMS:
        if form == _OFF:
            continue
        for m in _accepted(rx, hay, form, rate_lock):
            w = _count(_num_from_match(m), mult)
            if w is not None:
                offers.append((w, m))
    offers.sort(key=lambda o: o[1].start())
    return offers


def _stated_total(hay: str, offers):
    """D5: (weeks, match, raw_start) of an offer the banner states as its TOTAL, or None. A stated total
    sums OTHER offers, so it must be larger than each of them: "Get 8 weeks free: that's 4
    weeks free in October plus 4 weeks free in November!" is a breakdown of the headline 8,
    not a total. An offer OVERLAPPING the total
    shares its words ("4 WEEKS FREE across two months" also reads reversed as "FREE across two
    months"; "FREE (8 Weeks Total)" reads reversed as "FREE (8 Weeks") and is not a part."""
    def _parts(a, b):
        return [w2 for w2, m2 in offers if m2.end() <= a or m2.start() >= b]
    for w, m in offers:                                       # "that's 8 weeks free"
        others = _parts(m.start(), m.end())
        if (_TOTAL_BEFORE.search(hay[max(0, m.start() - 24):m.start()])
                and others and w > max(others)):
            return w, m, m.start()
    for t in _TOTAL_AFTER.finditer(hay):                      # "(8 Weeks Total)"
        # "(6 months total lease)" is the LEASE, not the offer's total
        if _is_deadline(hay, t.start(1)) or _is_term(hay, t) or _TERM_AFTER.match(hay, t.end()):
            continue
        # the total must directly FOLLOW an offer ("2 weeks FREE (8 Weeks Total)"): an "8 weeks
        # total" elsewhere on the page is not this banner's total
        if not any(m2.end() <= t.start() and _TOTAL_JOIN.fullmatch(hay, m2.end(), t.start())
                   for _w2, m2 in offers):
            continue
        w = _count(_to_num(t.group(1)), 1 if t.group(2)[0].lower() == "w" else 4)
        # a TOTAL is the SUM of the offers it totals: "6 weeks + 2 weeks (8 Weeks Total)".
        # "Get 2 weeks free (6 months total stay)" is a stay, not a 24-week offer.
        # Parts are the offers before it that do not overlap one another: "2 weeks free + 4
        # weeks free" also reads reversed as "free + 4 weeks", the same offer twice.
        parts, last_end = [], -1
        for w2, m2 in sorted(offers, key=lambda o: o[1].start()):
            if m2.end() <= t.start() and m2.start() >= last_end:
                parts.append((w2, m2))
                last_end = m2.end()
        if w is not None and len(parts) >= 2 and w == sum(w2 for w2, _m in parts):
            return w, t, parts[0][1].start()
    return None


# D13. "FIRST FULL MONTH FREE on all apartment homes! Take an additional HALF-MONTH
# FREE on select 3-bedroom apartment homes!" -- an offer the banner itself says covers the whole
# community, beside offers that each name a bed type / config, is the site-level count; the
# others are per-floorplan add-ons. Ordinal months ("first full month free") count here as 4.
def _community_wide_offer(hay: str, offers):
    cands = list(offers)
    for m in _ORDINAL_MONTH_FREE.finditer(hay):
        if not (_amenity_offer(hay, m) or _non_rent_product(hay, m.start())
                or _RATE_LOCK.search(hay[max(0, m.start() - 5):m.end() + 25])):
            cands.append((4, m))
    cands.sort(key=lambda o: o[1].start())
    # headed: a bed type ahead of the FIRST offer, in that offer's own clause, scopes every tier
    # after it ("Studio specials: 6 weeks free on A1; ..."). Clause-bound here: "Explore our 2
    # bedroom homes. FIRST FULL MONTH FREE on all apartment homes! ..." is not headed
    if cands:
        first = cands[0][1].start()
        lo = max(0, first - _SCOPE_BEFORE)
        breaks = list(_CLAUSE_BREAK.finditer(hay, lo, first))
        if _names_a_plan(hay[breaks[-1].end() if breaks else lo:first]):
            return None
    def _before(m):
        # back to the clause break, and never past the previous offer: text between two offers
        # leads into the LATER one ("All homes get 6 weeks free and select 3BRs get an
        # additional 2 weeks free" -- Codex r4 of)
        lo = max(0, m.start() - _SCOPE_BEFORE)
        breaks = list(_CLAUSE_BREAK.finditer(hay, lo, m.start()))
        if breaks:
            lo = breaks[-1].end()
        prev = [m2.end() for _w2, m2 in cands if m2.end() <= m.start()]
        return hay[max([lo] + prev):m.start()]

    def _after(m, attached):
        # forward to the clause break, and never into the next offer: "6 weeks free on all homes
        # plus an additional 2 weeks free on 3BRs" has no punctuation between them.
        # The base offer's own scope also ends at a conjunction that starts the next claim.
        own = _own_clause(hay, m, attached=attached)
        nxt = [m2.start() - m.end() for _w2, m2 in cands if m2.start() >= m.end()]
        if nxt and min(nxt) < len(own):
            own = own[:min(nxt)]
        if attached:
            c = _NEXT_CLAIM.search(own)
            own = own[:c.start()] if c else own
        return own

    def _wide(seg):
        return any(not _names_a_plan(c.group(0)) for c in _COMMUNITY_WIDE.finditer(seg))

    for w, m in cands:
        before, after = _before(m), _after(m, True)
        if not (_wide(before) or _wide(after)) or _names_a_plan(before) or _names_a_plan(after):
            continue
        rest = [(w2, m2) for w2, m2 in cands if m2.end() <= m.start() or m2.start() >= m.end()]
        if rest and all(_names_a_plan(_before(m2)) or _names_a_plan(_after(m2, False))
                        for _w2, m2 in rest):
            return w, m
    return None


# D13: a conjunction that starts the NEXT claim ends an offer's own scope.
_NEXT_CLAIM = re.compile(r"\b(?:and|plus)\b|\+|&", re.I)


def _preferred_offer(hay: str):
    """D5 / D7: an offer the banner itself marks as THE offer. Returns (weeks, match,
    scope_checked, raw_start) or None -- weeks, the match, and whether the bed-scope veto still applies. A stated total keeps the veto; an "all other"
    offer is by its own words the rest of the community, so a named tier beside it does not
    veto it."""
    offers = _counted_offers(hay)               # (weeks, match) in text order
    # "Studio specials: 6 weeks free on A1; 4 weeks free on all other floor plans." -- a bed
    # type ahead of the FIRST offer scopes every tier after it, so neither preference applies
    #
    headed = bool(offers) and bool(_BED_SCOPE.search(hay[max(0, offers[0][1].start() - _SCOPE_BEFORE):
                                                           offers[0][1].start()]))
    tot = _stated_total(hay, offers)                                       # D5
    # a total scoped to a bed type is not THE offer: fall through to the other preferences
    # rather than veto the banner
    if tot and not offer_is_bed_scoped(hay, tot[1].start(), tot[1].end()):
        # the raw starts at the first offer the total sums, so it re-reads to the total
        return tot[0], tot[1], True, max(0, tot[2] - 15)
    cw = _community_wide_offer(hay, offers)                                # D13
    if cw:
        # the raw keeps the marker that made it THE offer, which may sit up to _SCOPE_BEFORE
        # before it -- else a re-read picks the add-on
        return cw[0], cw[1], False, max(0, cw[1].start() - _SCOPE_BEFORE)
    for w, m in ([] if headed else offers):                                # D12
        own = _own_clause(hay, m, attached=True)
        sel = _PLAIN_SELECT_CLAUSE.match(own)
        if sel and not _names_a_plan(own) and not _preceding_scope_names_a_plan(hay, m, offers) and any(
                w2 != w and _names_a_plan(_own_clause(hay, m2)) for w2, m2 in offers):
            return w, m, False, max(0, m.start() - 15)
    if headed or _plain_select_anywhere(hay):
        return None
    for w, m in offers:                                                    # D7
        # the WHOLE attached scope must name no bed type: "4 weeks free on all other floor
        # plans with 2 bedrooms" is itself a bed tier
        if (_names_a_plan(_own_clause(hay, m, attached=True))
                or _preceding_scope_names_a_plan(hay, m, offers)):
            continue
        after = _ALL_OTHER_AFTER.match(hay, m.end())
        label = _ALL_OTHER_LABEL.search(hay[max(0, m.start() - 60):m.start()])
        for hit in (after, label):
            if hit and not _names_a_plan(hit.group(1)):
                return w, m, False, max(0, m.start() - 15)
    return None


def scan_concession(text: str, monthly_rent: Optional[float] = None
                    ) -> Tuple[Optional[int], Optional[str]]:
    """Extract (weeks_free, raw_concession_text) from free-form marketing text.

    Returns (None, None) when there is no concession. Returns (None, raw) when a
    concession exists but can't be normalized to weeks (e.g. a $-off with no rent
    supplied) — the raw banner is still captured, which is what NER and the audit
    tab surface. weeks is ROUNDED and capped at 26 (a half-year of free rent is a
    parse error, not a real offer)."""
    if not text:
        return None, None
    # D1: same length as `text`, so offsets stay meaningful for snippets.
    hay = _join_split_offers(text)

    # 0) D5 / D7 -- an offer the banner itself marks as THE offer.
    pref = _preferred_offer(hay)
    if pref:
        w, m, scope_checked, lo = pref
        if scope_checked and offer_is_bed_scoped(hay, m.start(), m.end()):
            return None, _veto_raw(hay, m.start(), m.end())
        return w, re.sub(r"\s+", " ", hay[lo:min(len(hay), m.end() + 130)]).strip()

    # 1) weeks free / weeks off (both directions) — most specific, no rent needed.
    # 2) months free / months off → weeks = months * 4 (>6 months free is a parse error;
    #    0.5 = "half month"). Main's order and ranges; _accepted adds D2 D6 D8.
    for rx, form, mult, rate_lock in _COUNTED_FORMS:
        for m in _accepted(rx, hay, form, rate_lock):
            w = _count(_num_from_match(m) if mult == 4 else _to_num(m.group(1)), mult)
            if w is None:
                continue
            raw = _snippet(hay, m.start(), m.end())
            if offer_is_bed_scoped(hay, m.start(), m.end()):
                # the raw must carry what drove the veto (_veto_raw; M4)
                return None, _veto_raw(hay, m.start(), m.end())
            return w, raw

    # 2b) ordinal first-month free → exactly one month, i.e. 4 weeks. Runs AFTER
    # the cardinal month forms (so "2 Months Free" still wins on its own number)
    # and BEFORE dollars (so a banner carrying both a free month and a $-amount
    # resolves to the free month, which is the normalizable signal).
    for m in _ORDINAL_MONTH_FREE.finditer(hay):
        ctx = hay[max(0, m.start() - 5):m.end() + 25]
        if _amenity_offer(hay, m) or _non_rent_product(hay, m.start()) or _RATE_LOCK.search(ctx):
            continue
        raw = _snippet(hay, m.start(), m.end())
        if offer_is_bed_scoped(hay, m.start(), m.end()):
            # the raw must carry what drove the veto (_veto_raw; M4)
            return None, _veto_raw(hay, m.start(), m.end())
        return 4, raw

    # 3) dollars off — capture the banner; convert to weeks only if a rent is known.
    for m in _DOLLAR_RE.finditer(hay):
        if _amenity_offer(hay, m):
            continue
        dollars = int(m.group(1).replace(",", ""))
        if dollars < 100 or dollars > 20_000:  # $55 waived fee / absurd → not a rent concession
            continue
        raw = _snippet(hay, m.start(), m.end())
        # 🔴 A DOLLAR AMOUNT NEVER BECOMES WEEKS. the comp rule, 2026-08-26.
        #
        # This used to convert with `weeks = round(dollars / (monthly_rent / (52/12)))`
        # whenever a caller supplied a rent, and the fill-if-missing helpers below do. But
        # `concession_weeks` is site-level and feeds net-effective rent as
        # `asking x (1 - weeks/52)` — a model of ONGOING rent relief spread over the lease.
        # "$500 off your first month" is a one-time move-in credit; turning it into 2 weeks
        # tells a comp set the asking rent is permanently lower than it is. The SOP's step-6
        # labelling rule already said dollar-off is not a concession and every golden label
        # follows it; the scanner was the one disagreeing, which is why HOU Huntcliff graded
        # as the only concession miss in a round that otherwise scored 5/6.
        #
        # Measured before the change: 437 live communities, 10.1% of all rows carrying a
        # concession, spread across every adapter and every market.
        #
        # The banner is still captured. That is the honest half — NER and the audit tab
        # surface the raw text, and a reader can see the offer without the pipeline having
        # priced it. `monthly_rent` is now unused by every branch; it is kept in the
        # signature because callers pass it positionally and it costs nothing to ignore.
        return None, raw

    # 3b) percent off — same rule as dollars: banner kept, weeks refused.
    m = _PERCENT_RE.search(hay)
    if m:
        return None, _snippet(hay, m.start(), m.end())

    return None, None


def _saw_countable_offer(text: str) -> bool:
    """Did the scanner READ a free-rent offer it could count — accepted by the same
    amenity / rate-lock veto scan_concession applies? "1 month free parking" is a
    rejected match and so is not evidence the scanner saw anything."""
    hay = _join_split_offers(text)                    # D1, as scan_concession reads it
    for rx, form in _WEEK_FORMS + _MONTH_FORMS + ((_ORDINAL_MONTH_FREE, _ORD),):
        for _m in _accepted(rx, hay, form, True):
            return True
    return False


# Free-rent language in either direction, however it is spelled — the scanner's recall
# gaps ("free rent for 8-wks", "complimentary rent", "no rent until") all still
# say this. The duration abbreviations match the ones the counting patterns accept.
_FREE_RENT_HINT = re.compile(
    r"\b(?:free|complimentary|no)\b[^.\n]{0,25}?\b(?:rent|months?|mos?|weeks?|wks?)\b"
    r"|\b(?:rent|months?|mos?|weeks?|wks?)\b[^.\n]{0,25}?\bfree\b", re.I)


def reconcile_concession(result) -> bool:
    """The scanner is authoritative over a week count the primary extraction supplied,
    WHEN it can read that extraction's own banner (2026-09-21).

    The LLM path took `concession_weeks` from the model's JSON verbatim, and
    merge_concession never touches a count that is already set — so the house rule
    (dollar-off / percent-off is a move-in credit, weeks NULL; a named-bed-type offer
    is not site-level) was never applied to a number the model chose. PHX San Giovanni
    stored 4 weeks for "50% off first month rent" and NER read ~7.7% low.

    Three outcomes, decided on the model's OWN raw_concession_text so weeks and raw
    keep describing the same offer:
      - the scanner recognizes the banner and disagrees -> the scanner's value wins,
        including None; two DIFFERENT counts additionally raise flag_review;
      - the scanner sees nothing in the banner -> the model's count STANDS. Measured
        over 2,323 live LLM rows, the scanner is blind to ~125 legitimate offers
        ("1 Month Complimentary Rent", "pay no rent until 11/1"; "8-weeks free" until);
        nulling those would cost far more than the ~40 wrong counts it fixes;
      - no raw text -> nothing to read, the count stands.
    A None from the scanner is only trusted when the scanner actually READ the offer the
    model counted: either a countable free-rent form is present (so the None is a scope
    veto of that very offer), or the banner carries no free-rent language at all (so the
    None means the whole banner is a credit). "8 weeks of complimentary rent + $500 off your
    first month" is neither — the scanner misses the weeks and recognizes only the credit —
    and the model's 8 stands.

    Mutates `result`; returns True if it changed the count."""
    weeks = getattr(result, "concession_weeks", None)
    raw = getattr(result, "raw_concession_text", None)
    if weeks is None or not isinstance(raw, str) or not raw:
        return False
    scanned, seen = scan_concession(raw)
    if seen is None or scanned == weeks:
        return False
    if scanned is None and not _saw_countable_offer(raw) and _FREE_RENT_HINT.search(raw):
        return False           # credit recognized, free-rent language unread: not evidence
    result.concession_weeks = scanned
    if scanned is not None:
        result.flag_review = True
    return True


def veto_non_rent_quote(result, page_text: str) -> bool:
    """ on the LLM tier. The model quotes only
    the offer ("1 month free"), which reads as rent on its own, so reconcile_concession and
    fill_concession_from_banner keep or fill the 4 -- while the page says "Tour Second Space
    Monrovia — now offering 1 month free" (coworking). Find the quote on the page: when EVERY
    place it appears is a non-rent product's offer (or the quote itself names one), drop the
    weeks and keep the quote WITH that context, so merge_concession's re-scan of the raw in
    the pipeline's repair step reaches the same verdict. Mutates `result`; True if it vetoed."""
    raw = getattr(result, "raw_concession_text", None)
    if getattr(result, "concession_weeks", None) is None or not isinstance(raw, str) or not raw.strip():
        return False
    quote = re.sub(r"\s+", " ", raw).strip()
    page = re.sub(r"\s+", " ", page_text or "")
    free = _FREE_WORD.search(quote)
    off = free.start() if free else 0
    spots = [m.start() for m in re.finditer(re.escape(quote), page, re.I)]
    if spots:
        if not all(_non_rent_product(page, at + off) for at in spots):
            return False
        at = spots[0]
        head = page[max(0, at + off - 160):at + off]
        cut = max(head.rfind(ch) for ch in ".!?\n")
        result.raw_concession_text = (head[cut + 1:] + page[at + off:at + len(quote)]).strip()
    elif not _non_rent_product(quote, off):
        return False
    result.concession_weeks = None
    return True


def fill_concession_from_banner(result) -> bool:
    """The other half of reconcile_concession (2026-09-22): the model QUOTED a banner but
    supplied NO count. SYSTEM_PROMPT says '"up to X" or vague = null' while this scanner —
    the house SOP — counts "Up to 8 Weeks FREE" as 8; Haiku ignores the prompt and returns N
    (which reconcile then leaves alone), gpt-5.6-luna obeys it and returns null with the text,
    and that row fell through both hooks: reconcile needs a count, merge_concession needs NO
    raw text. 67 of 270 real pages on 2026-09-21. Read the model's own banner and fill.

    Fills only when the scanner reads a countable offer; a credit ("50% off", "$500 off") or a
    banner the scanner cannot read stays null. No flag_review: the count and the raw text
    describe the same offer, nothing disagreed. A supplied count is never touched here.
    Mutates `result`; returns True if it filled the count."""
    weeks = getattr(result, "concession_weeks", None)
    raw = getattr(result, "raw_concession_text", None)
    if weeks is not None or not isinstance(raw, str) or not raw:
        return False
    scanned, _seen = scan_concession(raw)
    if scanned is None:
        return False
    result.concession_weeks = scanned
    return True


def merge_concession(result, text: str, monthly_rent: Optional[float] = None) -> bool:
    """Fill-if-missing: only populate concession fields the primary extraction
    left empty. Never overwrites a concession the adapter/LLM already found.
    Mutates `result` (an ExtractResult); returns True if it filled anything."""
    if getattr(result, "concession_weeks", None) is not None:
        return False   # never overwrite a week count the adapter/LLM already found
    if getattr(result, "concession_unit_scoped", False):
        return False   # the adapter refused this unit-level raw as site-level weeks
    existing_raw = getattr(result, "raw_concession_text", None)
    if existing_raw:
        # The primary extraction FOUND a banner but could not normalize it (the
        # LLM prompt returns null for "up to X"). Normalize its OWN text rather
        # than the whole page, so weeks and raw always describe the same offer —
        # scanning the page here could pair a week count from one banner with raw
        # text from another. Previously this branch just bailed, silently losing
        # every concession the LLM captured-but-didn't-count.
        weeks, _ = scan_concession(existing_raw, monthly_rent)
        if weeks is None:
            return False
        result.concession_weeks = weeks
        return True
    weeks, raw = scan_concession(text, monthly_rent)
    if raw is None:
        return False
    result.concession_weeks = weeks
    result.raw_concession_text = raw
    return True


def sweep_concession(result, page_text: str, monthly_rent: Optional[float] = None) -> bool:
    """Scan a MARKETING page for a countable offer, even when a banner was captured.

    The difference from merge_concession is the precondition. That one asks "is
    raw_concession_text empty?", which lets an UNCOUNTABLE banner suppress the
    search: Park at 33rd (PHX/mf) advertises "up to 8 weeks FREE one and two bed"
    on its homepage, the LLM captured its other promo — "WAIVED Administrative
    fees" — and the sweep in the pipeline.py was gated off by the presence of that
    text, so the free-rent banner was never fetched. A fee waiver can never
    normalize to weeks, so the gate belonged on the WEEK COUNT all along.

    Replaces raw_concession_text alongside the week count when it finds one, which
    keeps merge_concession's invariant intact rather than breaking it: weeks and raw
    always describe the SAME offer. A page with nothing countable on it leaves the
    captured banner exactly as it was — an uncountable banner is still a real
    promotion and still belongs in the Notes cell.
    """
    if getattr(result, "concession_weeks", None) is not None:
        return False        # never overwrite a count the adapter/LLM already made
    weeks, raw = scan_concession(page_text, monthly_rent)
    if weeks is None:
        # 🔴 An UNCOUNTABLE banner is still a real promotion.
        # Bailing here used to be harmless because the only way to reach it was a page
        # with no offer at all. Since 2026-08-26 a dollar-off page lands here too — the
        # scanner refuses its WEEKS by rule — and an earlier change claimed "the banner is still
        # captured", which was true of merge_concession and false of this function. Keep
        # the text when we have nothing better; never overwrite a banner already captured,
        # because the existing one is at least as good and may be the countable offer's.
        if raw and not getattr(result, "raw_concession_text", None):
            result.raw_concession_text = raw
            return True
        return False        # nothing countable and nothing better to say
    result.concession_weeks = weeks
    result.raw_concession_text = raw
    return True


# --------------------------------------------------------------------------- #
# Self-test: the exact 18 golden banner texts (verbatim, read from live sites
# 2026-07-04). This is the proof the scanner extracts what the eval captured.
_SELFTEST = [
    # (text, expected_weeks)
    ("8 Weeks FREE Rent + $500 Gift Card!", 8),
    ("Up to 8 Weeks FREE Base Rent! Special may change based on home type, lease term and other factors.", 8),
    ("UP TO 3 MONTHS FREE AND A $1000 CREDIT - ADDITIONAL SPECIALS FOR SELECT HOMES", 12),
    ("Limited Time Offer: Enjoy Up To 8 Weeks Free. Call For Details.", 8),
    ("LEASE NOW AND GET TWO MONTHS FREE + $1,000 GIFT CARD* - Receive two months free base rent plus a $1,000 gift card.", 8),
    ("Summer Specials Are Heating Up - Move in by 07/03/2026 and receive Up To Six Weeks FREE on select homes!", 6),
    ("1 MONTH FREE + EXCLUSIVE PRICING ON SELECT HOMES - Apply by July 15, 2026, and receive 1 MONTH FREE", 4),
    ("Summer Specials Are Heating Up - Move in by 07/03/2026 and receive Up To 8 Weeks FREE on select homes!", 8),
    # decimal months (Broadstone North ATX, golden-eval 2026-07-14 — was misread as 5mo/20wk)
    ("Receive Up to 2.5 Months FREE! AND a $1,500 Gift Card*", 10),
    # half month (Tricon Bryson, golden-eval 2026-07-14 — was missed entirely)
    ("Up to Half Month Free Rent + 50% off Security Deposit*", 2),
    # rate-lock — NOT free rent
    ("Now offering 20-month leases. Base rent will remain at the 12-month lease rate for the duration of the 20-month term.", None),
    # waived fee — NOT free rent
    ("One Waived Application Fee ($55 Value)", None),
    # --- named-subset veto (golden-eval 2026-07-28, HSV/LAS P2 round) ---------
    # Scoped to a NAMED bed type => per-floorplan special, weeks must be null.
    # HSV The Gabriel, verbatim. Note the trailing "qualifying lease terms":
    # that qualifies the LEASE, not the homes, and must NOT read as "select homes".
    ("Your Choice, Your Home — Move in to one of our premium 2-bedroom floorplans, before July ends, "
     "to get up to 8 weeks FREE off base rent! Restrictions apply. Subject to approval and "
     "qualifying lease terms.", None),
    # ...but an UNSPECIFIED subset still records N, even with a bed count elsewhere
    # in the banner. HSV The Kelvin, verbatim — "Luxury 1 Bedrooms" belongs to the
    # price promo; the OFFER is scoped to "Select Apartment Homes".
    ("Your New Home Starts Here!! Luxury 1 Bedrooms Starting at Just $999! Move Now & Receive "
     "1 Month FREE on Select Apartment Homes!", 4),
    # per-bed-type dollar discount is not a community-wide weeks-free offer (HSV The Collier)
    ("New Lower Rates! Save Up to $350/Month on Select Apartments | 1BR: $200 Off • 2BR: $250 Off", None),
    # --- ORDINAL first-month free (golden-eval 2026-08-09, PHX mf round) -------
    # A first/1st FULL MONTH free IS one month free -> 4 weeks. The cardinal forms
    # ("1 Month Free", "One Month Free!") already scored 4; every ordinal form
    # scored None, so PHX's The Galleria read pipe=None vs site=4.
    # The Galleria Apartments (PHX mf, rentcafe), verbatim:
    ("MOVE IN SPECIAL - 1st Full Month- FREE RENT with 12 month lease on approved credit", 4),
    ("1st Full Month FREE RENT", 4),
    ("First Full Month Free", 4),
    ("1st month free", 4),
    ("First Month Free!", 4),
    ("2nd Month Free", 4),
    # GUARD — a DOLLAR amount off the first month is NOT a free month. Celia (PHX
    # mf) verbatim: this must stay null, or the ordinal rule would silently
    # convert every "$X off first month" banner into a 4-week concession.
    ("Reserve an apartment home by August 16 and receive $1,000 OFF your first month's rent!", None),
    # GUARD — the named-subset veto still governs ordinals. Scoping a first-month
    # offer to a NAMED bed type is a per-floorplan special, exactly as for "8 weeks
    # free" (HSV The Gabriel class). The ordinal rule must not widen past the veto.
    ("Move in and get your first full month free on our premium 2-bedroom floorplans!", None),
    # ...but an UNSPECIFIED subset still records N, same as the cardinal forms.
    ("Limited time: First Full Month FREE on select homes!", 4),
    # no concession
    ("", None),
    ("Sunshine, Savings & Home Sweet Home", None),
    # fractions — the last digit of a fraction is not the number
    ("1/2 Month Free!", 2),
    ("Two and a half Months Free Rent + Up to a $1,000 Gift Card!", 10),
    ("Half a Month FREE", 2),
    ("A month and a half FREE on select all available suites!", 6),
    # percent-off is a move-in credit: banner kept (checked below), weeks refused
    ("50% off first month rent", None),
]


def _run_selftest() -> int:
    fails = 0
    for text, want in _SELFTEST:
        got, raw = scan_concession(text)
        ok = got == want
        if not ok:
            fails += 1
        print(f"  {'OK ' if ok else 'XX '} want={want!s:<5} got={got!s:<5} raw={raw!r}")
    # $-off NEVER converts, with or without a rent (the comp rule 2026-08-26). The banner
    # is still captured; only the weeks are refused.
    got, raw = scan_concession("Up to $500 First Month's Rent on Select Homes*", monthly_rent=2300)
    ok = got is None and raw is not None
    fails += 0 if ok else 1
    print(f"  {'OK ' if ok else 'XX '} $-off w/rent -> want=(None, raw) got=({got}, {raw!r})")
    # and the same without a rent
    got, raw = scan_concession("Up to $500 First Month's Rent on Select Homes*")
    ok = got is None and raw is not None
    fails += 0 if ok else 1
    print(f"  {'OK ' if ok else 'XX '} $-off no-rent -> want=(None, raw) got=({got}, {raw!r})")
    print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAILED'}")
    return fails


if __name__ == "__main__":
    import sys
    sys.exit(1 if _run_selftest() else 0)
