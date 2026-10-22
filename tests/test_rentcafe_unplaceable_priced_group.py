"""A priced group with NO square footage must not let the grid count its units again.

BUG found by Codex reviewing an earlier change for a past bug on 2026-09-20.

`_merge_advertised` builds its SF bands with `if g.beds is None or g.sf_min is None: continue`,
so a PRICED group carrying a real bed count but no SF contributes no band at all. Every
advertised card for that bed type then fails `_already_priced` and is appended WITH ITS OWN
AVAILABILITY, on top of the priced group that already counted those same listings.

THE NO-SF PRICED GROUP IS REACHABLE, not hypothetical: `_parse` builds bedded groups with
`sf_min=min(sfs) if sfs else None`, so a layout whose unit rows carry a bed count but no
square footage yields exactly `(beds=1, sf_min=None)`.

🔴 THIS ONE INFLATES, which is the dangerous direction and the opposite of the bug family
around it (under-reported; an earlier change existed to stop bed types reading 0).
`_refuse_impossible_availability` is blind to it whenever the doubled count stays under
`public_record_units`, which for a handful of units is almost always.

THE FIX IS NOT TO DROP THE PLAN. The SF-band convention
(`workflows/golden_eval_austin_bfr_mf_sop.md`) says a bed group's SF must span every plan the
site advertises for that bed count, and an earlier change exists so a live bed type never reads 0. So
the advertised card is still emitted -- keeping its SF, its structure and its `flag_review` --
but at `available: 0`, because its units are already inside the unplaceable priced group. The
bed type stays live on the priced group's own count, the SF band still widens, and no unit is
counted twice.

WHAT THIS DELIBERATELY DOES NOT TOUCH: a priced group with `beds=None` (the `unknown` bucket).
Its units belong to no stated bed type, so it cannot claim any bed type's cards; suppressing on
it would zero every advertised plan on any site that has one.
"""

from __future__ import annotations

import unittest


from rentdb.adapters import rentcafe
from rentdb.adapters.base import UnitGroup


def _g(beds, sf, avail, **kw):
    return UnitGroup(beds=beds, sf_min=sf, sf_max=sf, available=avail, **kw)


def _merge(priced, advertised):
    out = rentcafe._merge_advertised(priced, advertised)
    return out, sum(g.available for g in out)


class UnplaceablePricedGroupTest(unittest.TestCase):

    def test_codex_reproduction_does_not_double_count(self):
        """priced (1bd, sf=None, 2) + grid (1bd, 700sf, 2) was 4 available. It is 2."""
        priced = [_g(1, None, 2)]
        advertised = [_g(1, 700, 2)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 2, [(g.beds, g.sf_min, g.available) for g in out])

    def test_the_advertised_plan_is_still_emitted_with_its_sf(self):
        """The SOP requires the bed group's SF to span every advertised plan, so the card
        survives — it just brings no units with it."""
        priced = [_g(1, None, 2)]
        advertised = [_g(1, 700, 2)]
        out, _ = _merge(priced, advertised)
        self.assertEqual(sorted((g.sf_min or -1) for g in out), [-1, 700])
        card = next(g for g in out if g.sf_min == 700)
        self.assertEqual(card.available, 0)

    def test_a_bed_type_with_no_priced_group_keeps_its_full_grid_count(self):
        """an earlier change's whole purpose. GREEN before and after — this is the control."""
        priced = [_g(2, 900, 3)]
        advertised = [_g(1, 700, 6), _g(2, 900, 3)]
        out, total = _merge(priced, advertised)
        one_bed = [g for g in out if g.beds == 1]
        self.assertEqual([g.available for g in one_bed], [6])
        self.assertEqual(total, 9)

    def test_a_placed_band_still_suppresses_a_contained_card(self):
        """Also a control: the ordinary path is untouched."""
        priced = [_g(1, 700, 4)]
        advertised = [_g(1, 700, 4)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 4)
        self.assertEqual(len(out), 1)

    def test_an_unplaceable_sibling_spends_its_budget_on_what_no_band_covers(self):
        """A bed type can have BOTH a placed band and an unplaceable group.

        The 700 card is covered by the band and suppressed outright. The 1,200 card is not,
        and the unplaceable group's 2 units are presumably part of its 5 — so 2 are deducted
        and 3 remain. Total 4 + 2 + 3 = 9, which is what the grid states for this bed type
        (4 + 5).

        This asserted 6 when first written on 2026-09-21, from the zero-the-whole-card rule.
        That was the undercount: it threw away three units the site lists and nothing else
        counts.
        """
        priced = [_g(1, 700, 4), _g(1, None, 2)]
        advertised = [_g(1, 700, 4), _g(1, 1200, 5)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 9)
        far = next(g for g in out if g.sf_min == 1200)
        self.assertEqual(far.available, 3)

    def test_the_group_suppresses_only_as_many_units_as_it_HOLDS(self):
        """🔴 ZEROING EVERY CARD TRADES OVERCOUNTING FOR UNDERCOUNTING.

        A no-SF 1bd group holding 2 units cannot be every advertised plan at once. Against
        cards of 2 and 3 units, zeroing both stored 2 — worse than the 7 the bug produced,
        in the opposite direction. The group can only account for as many units as it
        HOLDS, so 2 is the suppression budget and the rest of the grid still counts.

        The TOTAL is invariant to which card absorbs the budget: priced + sum(cards) - budget
        = 2 + 5 - 2 = 5 either way, which is the right answer whichever plan the group
        turns out to be. Only the per-card split is a guess, and it is documented as one.
        """
        priced = [_g(1, None, 2)]
        advertised = [_g(1, 700, 2), _g(1, 900, 3)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 5, [(g.sf_min, g.available) for g in out])

    def test_a_budget_larger_than_the_grid_cannot_go_negative(self):
        priced = [_g(1, None, 9)]
        advertised = [_g(1, 700, 2)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 9)
        self.assertEqual(next(g for g in out if g.sf_min == 700).available, 0)

    def test_reconciliation_happens_at_the_BED_TYPE_TOTAL(self):
        """🔴 A COVERED CARD IS NOT A PLACE TO CHARGE THE BUDGET.

        Priced (700sf, 2) and (None, 2) against grid cards (700sf, 4) and (1200sf, 5). The
        700 card is band-covered and suppressed, so the grid's 4 is already discarded in
        favour of the priced 2 — and then deducting the unplaceable 2 from the 1,200 card as
        well charged the same missing units twice, storing 7 where the site publishes 9.

        Missing SF gives no basis for saying WHICH plan the no-SF units belong to, so the
        reconciliation is done at the bed-type total and not per card: the bed type lands at
        max(what the priced feed observed, what the grid publishes). That is never below
        either source, so it can neither invent units nor discard observed ones.
        """
        priced = [_g(1, 700, 2), _g(1, None, 2)]
        advertised = [_g(1, 700, 4), _g(1, 1200, 5)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 9, [(g.sf_min, g.available) for g in out])

    def test_an_unknown_beds_priced_group_claims_nothing(self):
        """A beds=None bucket belongs to no bed type; suppressing on it would zero every
        advertised plan on any site that has one."""
        priced = [UnitGroup(beds=None, sf_min=None, sf_max=None, available=3)]
        advertised = [_g(1, 700, 6)]
        out, total = _merge(priced, advertised)
        self.assertEqual(total, 9)
        self.assertEqual(next(g for g in out if g.beds == 1).available, 6)


if __name__ == "__main__":
    unittest.main()
