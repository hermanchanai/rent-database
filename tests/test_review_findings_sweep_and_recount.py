"""Three defects Codex found in the 2026-08-26 review..an earlier change.

All three were shipped and MERGED before they were caught. My own tests passed on every one,
which is the point of an outside reviewer: a test written by the author of a defect tends to
encode the defect. Each is derived from the SPEC — the contract the code's own docstring or
commit message states — never from what the code currently does.

  1. `sweep_concession` drops a dollar-off banner entirely. an earlier change said "the banner is
     still captured"; that is true of `merge_concession`, which sets raw even when weeks is
     None, and FALSE of `sweep_concession`, which bails on `weeks is None` before touching
     raw. So on the landing-page sweep path a "$500 off" offer loses the weeks (intended)
     AND the raw text (not intended).

  2. `_refuse_misattributed_rents` records `condemned.add(g.beds)` — a BED COUNT — but
     `_recount_condemned_from_grid` rewrites every priced group of that bed count that has a
     matching card. A same-bed sibling group at a different SF was never condemned and must
     not be rewritten.

  3. `_recount_condemned_from_grid`'s docstring promises it "can raise a plan toward what the
     site publishes and can never erase inventory", but `if cards.get(key): g.available =
     cards[key]` assigns any positive card count, including one LOWER than the observed count.
     This is not hypothetical: the HOU/mf round proved rentcafe cards can publish a CAPPED
     count (Smart Living's Monterey card reads 10 against a true 14), so a capped card can
     silently delete observed units.
"""

from __future__ import annotations

import unittest


from rentdb.concessions import sweep_concession, merge_concession
from rentdb.adapters import rentcafe
from rentdb.adapters.base import UnitGroup


class _Result:
    """Minimal stand-in for ExtractResult — the two helpers only touch these fields."""
    def __init__(self, weeks=None, raw=None):
        self.concession_weeks = weeks
        self.raw_concession_text = raw


DOLLAR_PAGE = "Fall Special: Get $500 base rent OFF your first month! See office for details."


class SweepMustKeepAnUncountableBannerTests(unittest.TestCase):
    """FINDING 1. The dollar-off rule refuses the WEEKS, never the banner."""

    def test_sweep_captures_a_dollar_only_banner(self):
        r = _Result()
        sweep_concession(r, DOLLAR_PAGE)
        self.assertIsNone(r.concession_weeks, "a dollar credit is not ongoing rent relief")
        self.assertIsNotNone(r.raw_concession_text,
                             "the earlier change's contract: the banner is still captured")
        self.assertIn("500", r.raw_concession_text)

    def test_merge_concession_already_did_this(self):
        """The sibling helper is the reference behaviour — it sets raw with weeks None."""
        r = _Result()
        merge_concession(r, DOLLAR_PAGE)
        self.assertIsNone(r.concession_weeks)
        self.assertIn("500", r.raw_concession_text or "")

    def test_sweep_still_refuses_to_overwrite_an_existing_count(self):
        r = _Result(weeks=6, raw="6 Weeks Free")
        self.assertFalse(sweep_concession(r, DOLLAR_PAGE))
        self.assertEqual(6, r.concession_weeks)
        self.assertEqual("6 Weeks Free", r.raw_concession_text)

    def test_sweep_does_not_clobber_a_better_existing_banner_with_a_dollar_one(self):
        """An uncountable banner already captured must not be swapped for another
        uncountable one — the existing text is at least as good."""
        r = _Result(weeks=None, raw="Ask about our move-in specials")
        sweep_concession(r, DOLLAR_PAGE)
        self.assertEqual("Ask about our move-in specials", r.raw_concession_text)

    def test_a_countable_page_still_wins(self):
        r = _Result()
        self.assertTrue(sweep_concession(r, "Enjoy 8 Weeks Free on select homes!"))
        self.assertEqual(8, r.concession_weeks)


class RecountTouchesOnlyTheCondemnedGroupTests(unittest.TestCase):
    """FINDING 2. Condemnation is per GROUP; it must not travel to same-bed siblings."""

    def _advertised(self):
        return [UnitGroup(beds=1, sf_min=700, sf_max=700, available=6),
                UnitGroup(beds=1, sf_min=900, sf_max=900, available=2)]

    def test_a_same_bed_sibling_is_not_rewritten(self):
        condemned = UnitGroup(beds=1, sf_min=700, sf_max=700, available=2, flag_review=True)
        sibling = UnitGroup(beds=1, sf_min=900, sf_max=900, available=5)
        out = rentcafe._recount_condemned_from_grid(
            [condemned, sibling], self._advertised(), condemned_keys={(1, 700)})
        self.assertEqual(6, out[0].available, "the condemned group is recounted")
        self.assertEqual(5, out[1].available,
                         "the sibling was never condemned and must keep its own count")


class RecountNeverLowersTests(unittest.TestCase):
    """FINDING 3. The docstring promises raise-only. A CAPPED card must not delete units."""

    def test_a_lower_card_count_does_not_replace_a_higher_observed_count(self):
        """Smart Living's Monterey card reads 10 against a true 14 — cards cap."""
        priced = [UnitGroup(beds=1, sf_min=768, sf_max=768, available=14, flag_review=True)]
        advertised = [UnitGroup(beds=1, sf_min=768, sf_max=768, available=10)]
        out = rentcafe._recount_condemned_from_grid(priced, advertised,
                                                    condemned_keys={(1, 768)})
        self.assertEqual(14, out[0].available, "a capped card must not erase observed units")

    def test_a_higher_card_count_still_raises(self):
        priced = [UnitGroup(beds=1, sf_min=700, sf_max=700, available=2, flag_review=True)]
        advertised = [UnitGroup(beds=1, sf_min=700, sf_max=700, available=6)]
        out = rentcafe._recount_condemned_from_grid(priced, advertised,
                                                    condemned_keys={(1, 700)})
        self.assertEqual(6, out[0].available)

    def test_a_silent_card_still_never_zeroes(self):
        priced = [UnitGroup(beds=1, sf_min=784, sf_max=784, available=5, flag_review=True)]
        advertised = [UnitGroup(beds=1, sf_min=784, sf_max=784, available=0)]
        out = rentcafe._recount_condemned_from_grid(priced, advertised,
                                                    condemned_keys={(1, 784)})
        self.assertEqual(5, out[0].available)


if __name__ == "__main__":
    unittest.main()
