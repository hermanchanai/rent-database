""" — two REAL plans at the same (beds, SF) must each keep their own count.

`_parse_advertised_plans` dedups marketing cards on (beds, sf) and keeps max(). That is
deliberate and it is right for a NESTED card — NSH Newport, LAX La Serena — where the
section card and the plan card inside it are the same inventory read twice. It is wrong
for a genuine sibling pair: ATL The Crest at Acworth publishes A1 (5 available) and A2
(8 available), both 1 Bed / 777 Sq. Ft., and the community stored 8.

THE MARKUP IN EVERY FIXTURE HERE IS LIVE, taken from the 60-community sweep cache of
2026-09-21, not invented. `data-floorplan-id` on `span.fp-availability` is the
discriminator; the sweep measured what it can and cannot resolve.
"""
from __future__ import annotations

import unittest


from bs4 import BeautifulSoup  # noqa: E402

from rentdb.adapters.rentcafe import _parse_advertised_plans  # noqa: E402


def _soup(cards: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{cards}</body></html>", "html.parser")


def _card(name: str, beds_txt: str, sf: str, span: str = "", extra: str = "") -> str:
    return (f'<div class="card"><h3>{name}</h3>'
            f'<span>{beds_txt}</span><span>{sf} Sq. Ft.</span>{span}{extra}</div>')


def _span(fp_id: str | None, text: str, data_max: str = "0") -> str:
    attr = f' data-floorplan-id="{fp_id}"' if fp_id is not None else ""
    return f'<span class="fp-availability"{attr} data-max="{data_max}">{text}</span>'


# ATL The Crest at Acworth /floorplans, 2026-09-21. Two 1bd/777sf plans, own ids.
CREST = (_card("A1", "1 Bed", "777", _span("4561572", "5 Available"))
         + _card("A2", "1 Bed", "777", _span("4561573", "8 Available")))

# LAX La Serena /floorplans, 2026-09-21. The "1 Bedroom" SECTION card wraps the plan
# card; both render the SAME span, id 1978987, "6 Available". One plan, read twice.
SERENA_NESTED = (
    '<div class="card"><h3>1 Bedroom</h3><span>1 Bed</span><span>725 Sq. Ft.</span>'
    + _span("1978987", "6 Available")
    + _card("1 Bedroom/1Bath", "1 Bed", "725", _span("1978987", "6 Available"))
    + '</div>')

# LAX La Serena 2bd/965sf. Both cards carry spans naming TWO plans (1978989),
# so neither card's own identity is readable — `_card_span_counts` already returns []
# for exactly this shape.
SERENA_MULTI = (
    '<div class="card"><h3>2 Bedrooms</h3><span>2 Beds</span><span>965 Sq. Ft.</span>'
    + _span("2075087", "11 Available") + _span("1978989", "7 Available")
    + _card("2 Bedroom/1 Bath", "2 Beds", "965",
            _span("2075087", "11 Available") + _span("1978989", "7 Available"))
    + '</div>')

# CLT The Wrenley /floorplans, 2026-09-21. A2 and A2A are genuinely different plans at
# 1bd/713sf, but NEITHER card carries a `span.fp-availability` at all.
WRENLEY = (_card("A2", "1 Bed", "713", "", "<p>Available Now</p>")
           + _card("A2A", "1 Bed", "713", "", "<p>Available Now</p>"))


class SiblingPlansKeepTheirOwnCount(unittest.TestCase):

    def test_two_plans_with_distinct_ids_each_keep_their_count(self):
        """THE BUG. A1 (5) and A2 (8) at 1bd/777sf are two plans, not one of 8."""
        plans = _parse_advertised_plans(_soup(CREST))
        self.assertEqual(sorted(p.available for p in plans), [5, 8],
                         [(p.beds, p.sf_min, p.available) for p in plans])
        self.assertEqual(sum(p.available for p in plans), 13)

    def test_a_nested_card_sharing_one_id_still_collapses(self):
        """The FAIL condition: one plan read twice must not become twelve units."""
        plans = _parse_advertised_plans(_soup(SERENA_NESTED))
        self.assertEqual([p.available for p in plans], [6],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_cards_whose_spans_name_two_plans_still_collapse(self):
        """A card naming two plans identifies neither, so it can claim no sibling."""
        plans = _parse_advertised_plans(_soup(SERENA_MULTI))
        self.assertEqual([p.available for p in plans], [11],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_siblings_with_no_id_at_all_now_split_on_name_and_structure(self):
        """🔴 CORRECTED 2026-09-21 by a past bug, not accommodated — this test used to
        assert `[1]` and call it an "honest under-count".

        It was honest only while `data-floorplan-id` was the sole discriminator. A2 and
        A2A are two real plans and the grid publishes one available on each; the markup
        does separate them, through `.card-title` plus DOM ancestry, and that is 42 of the
        sweep's 52 disjoint collisions. The old expectation encoded the very defect
        was filed to remove, so it moves to `[1, 1]`. See
        SiblingsWithNoPlanIdSplitOnNameAndStructure for the full rule."""
        plans = _parse_advertised_plans(_soup(WRENLEY))
        self.assertEqual([p.available for p in plans], [1, 1],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_a_card_with_no_id_falls_through_to_name_and_structure(self):
        """🔴 CORRECTED 2026-09-21 by a past bug, not accommodated — this test used to
        assert `[8]`, on the reasoning that "an id-less card could BE the section wrapper
        for the very plans beside it".

        That reasoning was right about the RISK and wrong about the only way to answer it.
        A wrapper can now be identified structurally — it literally contains the plan card
        — so an id-less card that is DISJOINT from its neighbours and carries its own name
        is not a wrapper, and A1/A2/A? are three plans. What still poisons a key is a card
        that names NOTHING, which is pinned by
        `SiblingsWithNoPlanIdSplitOnNameAndStructure.test_an_unnamed_card_poisons_the_key`.
        The id limb is unchanged: where EVERY card publishes an id, the id still decides."""
        markup = CREST + _card("A?", "1 Bed", "777", "", "<p>3 Available</p>")
        plans = _parse_advertised_plans(_soup(markup))
        self.assertEqual(sorted(p.available for p in plans), [3, 5, 8],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_each_sibling_carries_its_own_cap_delta(self):
        """The second half of. An uncapped 5-unit plan beside a capped plan whose
        own spans disagree (10 · 15 under data-max=10) must not share one delta."""
        markup = (_card("A1", "1 Bed", "777", _span("4561572", "5 Available"))
                  + _card("A2", "1 Bed", "777",
                          _span("4561573", "10 Available", data_max="10")
                          + _span("4561573", "15 Available", data_max="10")))
        deltas: dict = {}
        plans = _parse_advertised_plans(_soup(markup), deltas)
        by_avail = {p.available: deltas.get(id(p), 0) for p in plans}
        self.assertEqual(by_avail, {5: 0, 15: 5}, by_avail)


class SplitSiblingsMustNotCondemnAGoodRent(unittest.TestCase):
    """🔴 THE SPLIT CHANGED WHO SPEAKS FOR A BED TYPE.

    `_refuse_misattributed_rents` built its floors as
    `{g.beds: g.rent_min for g in advertised}` — a dict comprehension, so the LAST
    advertised plan of a bed type won and the others were not consulted at all. That was
    already arbitrary across two SF plans of one bed type; splitting a (beds, SF) key put
    a second entry on the SAME key and made it bite. Siblings advertising $1,500 and
    $1,400 at 1bd/777sf, beside a 2bd plan advertising $1,500: floors[1] becomes 1400, so
    a one-bedroom honestly priced at its own plan's $1,500 no longer matches its own
    floor, matches the 2bd floor exactly, and is condemned — rent AND `unit_rents` nulled.

    A BED TYPE HAS AS MANY FLOORS AS THE GRID PRINTS PLANS, so all of them are kept. The
    exemption is "this price is one of MY OWN plans' floors" and the accusation is "it is
    one of ANOTHER bed type's floors and none of mine", which is what the docstring
    always claimed and what the last-wins dict could not express.
    """

    def _adv(self, beds, sf, rent):
        from rentdb.adapters.base import UnitGroup
        return UnitGroup(beds=beds, sf_min=sf, sf_max=sf, available=0, rent_min=rent,
                         rent_basis="starting_at")

    def _priced(self, beds, sf, rent):
        from rentdb.adapters.base import UnitGroup
        return UnitGroup(beds=beds, sf_min=sf, sf_max=sf, available=2, rent_min=rent,
                         rent_max=rent, unit_rents=[rent, rent])

    def test_a_rent_matching_one_of_its_own_siblings_floors_survives(self):
        from rentdb.adapters.rentcafe import _refuse_misattributed_rents
        advertised = [self._adv(1, 777, 1500), self._adv(1, 777, 1400),
                      self._adv(2, 1050, 1500)]
        priced = [self._priced(1, 777, 1500)]
        reasons: list[str] = []
        out = _refuse_misattributed_rents(priced, advertised, reasons)
        self.assertEqual(out[0].rent_min, 1500, reasons)
        self.assertEqual(out[0].unit_rents, [1500, 1500], reasons)
        self.assertEqual(reasons, [])

    def test_the_hadley_shape_is_still_condemned(self):
        """The defect this guard exists for. A 1bd priced to the dollar at the 3bd floor,
        matching NO 1bd plan, is still nulled."""
        from rentdb.adapters.rentcafe import _refuse_misattributed_rents
        advertised = [self._adv(1, 739, 1295), self._adv(1, 760, 1350),
                      self._adv(3, 1400, 2295)]
        priced = [self._priced(1, 739, 2295)]
        reasons: list[str] = []
        out = _refuse_misattributed_rents(priced, advertised, reasons)
        self.assertIsNone(out[0].rent_min, reasons)
        self.assertIsNone(out[0].unit_rents, reasons)
        self.assertTrue(out[0].flag_review)
        self.assertEqual(len(reasons), 1, reasons)

    def test_the_seam_production_uses(self):
        """Through the real parser, not hand-built groups: the grid below is the shape
        `_parse_advertised_plans` now splits, and the rent must survive the round trip."""
        from rentdb.adapters.rentcafe import _parse_advertised_plans, _refuse_misattributed_rents
        markup = (_card("A1", "1 Bed", "777", _span("4561572", "5 Available"),
                        "<p>Starting at $1,500.00</p>")
                  + _card("A2", "1 Bed", "777", _span("4561573", "8 Available"),
                          "<p>Starting at $1,400.00</p>")
                  + _card("B1", "2 Beds", "1050", _span("4561574", "2 Available"),
                          "<p>Starting at $1,500.00</p>"))
        advertised = _parse_advertised_plans(_soup(markup))
        self.assertEqual(len(advertised), 3, [(a.beds, a.sf_min, a.rent_min) for a in advertised])
        priced = [self._priced(1, 777, 1500)]
        reasons: list[str] = []
        out = _refuse_misattributed_rents(priced, advertised, reasons)
        self.assertEqual(out[0].rent_min, 1500, reasons)

    def test_a_split_sibling_must_not_condemn_another_bed_types_rent(self):
        """🔴 THE MIRROR OF THE ABOVE, and `own_floors` does not cover it.

        The accusation still reads the last-wins `floors` dict, and splitting a 2bd key
        changes WHICH 2bd plan that is. A 1bd floor of $1,000 beside two 2bd siblings
        advertising $1,600 then $1,500: before the split the grid contributed one 2bd
        entry at $1,600, so a premium 1bd priced at $1,500 was untouched; after it,
        floors[2] is 1500 and that same correct rent is condemned.

        The accusation basis is therefore built from the PRE-SPLIT view of the grid -- one
        entry per (beds, SF), the first card's price, exactly what HEAD saw -- so how
        sibling cards are emitted cannot change whose rent gets condemned.
        """
        from rentdb.adapters.rentcafe import _refuse_misattributed_rents
        advertised = [self._adv(1, 700, 1000),
                      self._adv(2, 900, 1600), self._adv(2, 900, 1500)]
        priced = [self._priced(1, 700, 1500)]
        reasons: list[str] = []
        out = _refuse_misattributed_rents(priced, advertised, reasons)
        self.assertEqual(out[0].rent_min, 1500, reasons)
        self.assertEqual(out[0].unit_rents, [1500, 1500], reasons)
        self.assertEqual(reasons, [])

    def test_a_priceless_first_sibling_still_claims_its_key(self):
        """🔴 THE PRE-SPLIT VIEW MUST BE BUILT ON THE KEY, NOT ON THE PRICE.

        The grid prints "Starting at $X" per card and some cards carry none. Skipping a
        priceless first sibling BEFORE recording its (beds, SF) key let the SECOND
        sibling's price into the supposedly pre-split basis -- the very leak the basis
        exists to stop. Two 2bd/900sf siblings priced null then $1,500, beside a 1bd floor
        of $1,000: the pre-split grid contributed NO 2bd floor at all, so a premium 1bd at
        $1,500 has nothing to collide with.
        """
        from rentdb.adapters.rentcafe import _refuse_misattributed_rents
        advertised = [self._adv(1, 700, 1000),
                      self._adv(2, 900, None), self._adv(2, 900, 1500)]
        priced = [self._priced(1, 700, 1500)]
        reasons: list[str] = []
        out = _refuse_misattributed_rents(priced, advertised, reasons)
        self.assertEqual(out[0].rent_min, 1500, reasons)
        self.assertEqual(out[0].unit_rents, [1500, 1500], reasons)
        self.assertEqual(reasons, [])


class SiblingsWithNoPlanIdSplitOnNameAndStructure(unittest.TestCase):
    """ — the 42 pairs `data-floorplan-id` could not separate.

    an earlier change split a (beds, SF) key only when EVERY card published a distinct
    `data-floorplan-id`, which resolved 10 of 52 structurally-disjoint collisions and left
    **42 real sibling pairs collapsed on max()**. The row also recorded the measurement
    that killed the obvious follow-on: taking each card's leading TEXT as its name would
    have split all 42 correctly AND double-counted all 10 nested pairs, because a wrapper
    card's text opens with the section heading.

    THE MARKUP ANSWERS BOTH QUESTIONS, and the earlier measurement was reading the wrong
    part of it. Two facts, taken together:
      * **DOM ancestry** settles nesting outright — a wrapper card literally CONTAINS the
        plan card. That is structure, not a heuristic, and it is what the sweep
        graded against rather than trusting the id to mark its own homework.
      * **`.card-title`** carries the plan name in the flat layout (CLT The Wrenley: `A2`
        vs `A2A`). In the nested layout there is no `.card-title` at all: the wrapper's
        `h2` is the SECTION ("1 Bedroom") and both cards' `h3` is the same plan ("A1"), so
        resolving `.card-title` -> `h3` -> `h2` gives the wrapper and its child the SAME
        name and they collapse on the name limb too.

    So a key with no usable ids splits only when its cards are pairwise DOM-disjoint AND
    their plan names are distinct and non-empty. Measured over the same 60-community cache
    as **52 of 52 disjoint collisions split, 11 of 11 nested still collapse, zero
    nested false splits.**

    Markup below is LIVE, from that cache.
    """

    # CLT The Wrenley /floorplans — two real 1bd/713sf plans, no fp-availability span on
    # either card, distinct `.card-title`.
    WRENLEY = (
        '<div class="card text-center h-100"><h2 class="card-title">A2</h2>'
        '<h3>Self-Guided Tour for A2</h3>'
        '<span>1 Bed</span><span>1 Bath</span><span>713 Sq. Ft.</span>'
        '<p>Available Now</p></div>'
        '<div class="card text-center h-100"><h2 class="card-title">A2A</h2>'
        '<h3>Guided Tour for A2A</h3>'
        '<span>1 Bed</span><span>1 Bath</span><span>713 Sq. Ft.</span>'
        '<p>Available Now</p></div>')

    # HOU Crenshaw Grand /floorplans — the SECTION card wraps the plan card. No
    # `.card-title` on either; the wrapper's h2 is "1 Bedroom" and both h3 are "A1".
    CRENSHAW = (
        '<div class="fp-card card mb-2"><h2>1 Bedroom</h2><h3>A1</h3>'
        '<span>1 Bed</span><span>1 Bath</span><span>611 Sq. Ft.</span>'
        '<p>Only 2 Left!</p>'
        '<div class="card"><h3>A1</h3>'
        '<span>1 Bed</span><span>1 Bath</span><span>611 Sq. Ft.</span>'
        '<p>Only 2 Left!</p></div>'
        '</div>')

    def test_two_real_plans_with_no_id_split_on_title(self):
        """THE BUG. A2 and A2A are two plans; the grid said 1 available on each."""
        plans = _parse_advertised_plans(_soup(self.WRENLEY))
        self.assertEqual(len(plans), 2, [(p.beds, p.sf_min, p.available) for p in plans])
        self.assertEqual(sum(p.available for p in plans), 2)

    def test_a_nested_card_with_no_id_still_collapses(self):
        """The FAIL condition, and the reason a text-prefix rule was rejected."""
        plans = _parse_advertised_plans(_soup(self.CRENSHAW))
        self.assertEqual(len(plans), 1, [(p.beds, p.sf_min, p.available) for p in plans])

    def test_two_cards_naming_the_SAME_plan_collapse(self):
        """Disjoint but identical `.card-title` is one plan rendered twice — a mobile and
        a desktop block — not a sibling pair. Structure alone cannot see this; the name
        limb is what catches it."""
        markup = (
            '<div class="card"><h2 class="card-title">A1</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span><p>3 Available</p></div>'
            '<div class="card"><h2 class="card-title">A1</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span><p>3 Available</p></div>')
        plans = _parse_advertised_plans(_soup(markup))
        self.assertEqual([p.available for p in plans], [3],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_an_unnamed_card_poisons_the_key(self):
        """Same conservatism as the id rule: a card that names nothing cannot be anybody's
        distinct sibling, so the whole key collapses rather than risk a double count."""
        markup = (self.WRENLEY
                  + '<div class="card"><span>1 Bed</span><span>713 Sq. Ft.</span>'
                    '<p>5 Available</p></div>')
        plans = _parse_advertised_plans(_soup(markup))
        self.assertEqual([p.available for p in plans], [5],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_a_shared_plan_id_still_beats_distinct_names(self):
        """When EVERY card publishes an id, the id decides — it is the site naming its own
        plans. Two cards on one id are one plan however their headings read."""
        markup = (
            '<div class="card"><h2 class="card-title">A1</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span>'
            + _span("77", "3 Available") + '</div>'
            '<div class="card"><h2 class="card-title">A1 - Renovated</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span>'
            + _span("77", "3 Available") + '</div>')
        plans = _parse_advertised_plans(_soup(markup))
        self.assertEqual([p.available for p in plans], [3],
                         [(p.beds, p.sf_min, p.available) for p in plans])

    def test_a_shared_id_still_binds_when_another_card_has_none(self):
        """🔴 THE FALLBACK MUST NOT THROW AWAY THE IDS IT DOES HAVE.

        Two cards on id `77` are one plan however their titles read — that is the rule the
        id limb already enforces. But the fallback fires whenever ANY card lacks an id, and
        it was deciding purely on names and structure, so a third id-less sibling beside
        them made all three "distinct" and emitted 3 + 3 + 1 for a plan the site publishes
        three of. INFLATION, the one direction this family of bugs must never go, and
        `_merge_advertised` carries it straight through.

        A key is therefore never split while two of its cards share an id. Collapsing to
        max() under-counts here (3 where the site implies 4) and that is the same safe
        direction as `test_an_unnamed_card_poisons_the_key` — the boolean is per KEY, so
        partial grouping is not expressible, and an under-count is not an invented unit.
        """
        markup = (
            '<div class="card"><h2 class="card-title">A1</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span>'
            + _span("77", "3 Available") + '</div>'
            '<div class="card"><h2 class="card-title">A1 - Renovated</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span>'
            + _span("77", "3 Available") + '</div>'
            '<div class="card"><h2 class="card-title">A1 - Corner</h2>'
            '<span>1 Bed</span><span>700 Sq. Ft.</span><p>1 Available</p></div>')
        plans = _parse_advertised_plans(_soup(markup))
        self.assertEqual([p.available for p in plans], [3],
                         [(p.beds, p.sf_min, p.available) for p in plans])
        self.assertLessEqual(sum(p.available for p in plans), 4,
                             "a plan the site publishes 3 of must never be counted twice")



if __name__ == "__main__":
    unittest.main(verbosity=2)
