"""Bug — an offer that NAMES a bed type is not a property-wide concession.

THE DEFECT. Seven adapters (ark, avalon, entrata, knock, realpage, rentcafe,
sightmap) each carry their own copy of "find N weeks free / N months free". None has
the shared scanner's `_is_named_subset` guard, so a per-floorplan promo is stored as
a property-wide `concession_weeks` — and `concession_weeks` feeds net-effective rent
for EVERY unit. HOU, verbatim from `rents.weekly_snapshots`:

    "8 weeks FREE! On One bedroom Unit starting at $1099 and Two bedroom Unit
     starting at $13.."

stored `concession_weeks = 8`, discounting the three-bedrooms by an offer that was
never made on them. 247 of 24,780 stored rows carry that shape.

THE OTHER HALF OF THE SPEC — the row's own FAIL condition, "a community that
legitimately advertises a community-wide offer loses its concession because a guard
is too eager", and Codex finding 3 of the 2026-09-21 review, verbatim:

    "CONFIRMED - Delegation erases explicit community-wide offers near unrelated
     bedroom descriptions. For 'Enjoy 6 weeks free on all homes. Spacious 3 bedroom
     homes available.' all six old helpers return 6; all six new helpers return
     None. The shared `_is_named_subset` treats the nearby bedroom description as a
     restriction despite 'on all homes.' These adapters scan full page text, so
     adjacent inventory descriptions can trigger this regression."

That finding is a property of `_is_named_subset` itself, not of delegation: it fires
today on that string. So the guard these adapters get must honour an EXPLICIT
community-wide marker, or fixing the 247 rows buys them a new defect. Both halves are
pinned here because the regression risk runs in both directions.

Every case is derived from the spec — the row's Observed/Pass/Fail text and the
review's findings — never from what the code currently returns.
"""
from __future__ import annotations

import unittest


from rentdb.adapters import ark, avalon, entrata, knock, realpage, rentcafe, sightmap  # noqa: E402
from rentdb.adapters.base import ExtractResult, UnitGroup  # noqa: E402
from rentdb.concessions import merge_concession  # noqa: E402

# The seven site-level scanners, by the name each module exposes.
SCANNERS = {
    "ark": lambda t: ark._extract_concession(t),
    "avalon": lambda t: avalon._scan_concession_text(t),
    "entrata": lambda t: entrata._concession(t),
    "knock": lambda t: knock._concession(t),
    "realpage": lambda t: realpage._concession(t),
    "rentcafe": lambda t: rentcafe._extract_concession(t),
    "sightmap": lambda t: sightmap._extract_concession(t),
}

HOU_BANNER = ("8 weeks FREE! On One bedroom Unit starting at $1099 and "
              "Two bedroom Unit starting at $13..")


class BedScopedOffersAreNotPropertyWide(unittest.TestCase):
    """The 247-row class: a named bed type keeps the banner and drops the weeks."""

    def test_the_hou_banner_records_no_weeks_on_any_adapter(self):
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan(HOU_BANNER)
                self.assertIsNone(weeks, f"{name} priced a one-bedroom promo property-wide")
                self.assertTrue(raw, f"{name} dropped the banner text as well as the weeks")

    def test_a_single_named_bed_type_is_a_per_floorplan_offer(self):
        # HSV The Gabriel, the canonical case in concessions.py's own docstring.
        text = "Up to 8 weeks free on our premium 2-bedroom floorplans."
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan(text)
                self.assertIsNone(weeks, f"{name} recorded a 2BR-only offer site-wide")
                self.assertTrue(raw)

    def test_the_months_form_is_vetoed_the_same_way(self):
        text = "2 months free on 3 bedroom homes"
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan(text)
                self.assertIsNone(weeks, f"{name} vetoes weeks-free but not months-free")
                self.assertTrue(raw)

    def test_all_of_one_bed_type_is_still_a_subset(self):
        # "all" scopes nothing here — it quantifies the 2-bedrooms, not the property.
        text = "6 weeks free on all 2 bedroom homes"
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, _ = scan(text)
                self.assertIsNone(weeks, f"{name} read 'all 2 bedroom homes' as community-wide")


class CommunityWideOffersSurvive(unittest.TestCase):
    """Codex finding 3, and the row's stated FAIL condition."""

    def test_an_explicit_all_homes_offer_beats_adjacent_inventory_copy(self):
        text = "Enjoy 6 weeks free on all homes. Spacious 3 bedroom homes available."
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, _ = scan(text)
                self.assertEqual(6, weeks,
                                 f"{name} erased a community-wide offer over nearby bed copy")

    def test_a_bare_offer_is_unchanged(self):
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                self.assertEqual(8, scan("8 weeks free")[0])
                self.assertEqual(8, scan("2 months free")[0])

    def test_an_unspecified_subset_still_records_the_weeks(self):
        # SOP step 6: "select homes" is ordinary marketing copy and records N.
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                self.assertEqual(8, scan("8 weeks free on select homes")[0])

    def test_a_conjoined_list_of_bed_types_is_one_community_wide_offer(self):
        # Park at 33rd PHX/mf — pinned for the shared scanner by
        # rentdb/test_concession_bed_scope.py; the adapters must agree.
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                self.assertEqual(8, scan("up to 8 weeks FREE one and two bed")[0])

    def test_a_community_wide_marker_wins_from_either_side_of_the_offer(self):
        """Codex, round 2 of the 2026-09-21 review, P2 — verbatim:

            "For `All homes receive 6 weeks free! Studios available.` all seven adapters
             now return no concession instead of six weeks: `Studios` is closer to the
             offer than `All homes`, so the distance comparison overrides the explicit
             community-wide scope. ... Distinguish scope attached to the offer from
             adjacent inventory copy rather than relying solely on character distance."

        The marker and the offer share a clause; the bed word is a separate sentence.
        Distance alone cannot tell those apart, and nearest-wins gets it backwards here.
        """
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                got = scan("All homes receive 6 weeks free! Studios available.")[0]
                self.assertEqual(6, got, f"{name} erased an explicit all-homes offer")

    def test_a_marker_in_a_LATER_sentence_does_not_rescue_a_scoped_offer(self):
        # The other direction: "Every home" here describes an amenity in its own
        # sentence, and the offer really is 2BR-only. A rule that cancels on the mere
        # presence of a community-wide word would record 6 for every unit.
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                got = scan("6 weeks free on 2BR. Every home has a garage.")[0]
                self.assertIsNone(got, f"{name} took scope from an unrelated sentence")

    def test_a_bed_marker_further_away_than_the_offer_does_not_veto(self):
        # "Every home" is nearer the offer than "2BR", so the veto must not fire...
        self.assertEqual(6, SCANNERS["ark"]("6 weeks free, every home. 2BR from $1400")[0])
        # ...and when the bed marker is nearer, it must.
        self.assertIsNone(SCANNERS["ark"]("6 weeks free on 2BR. Every home has a garage.")[0])


class AvalonKeepsSearchingPastAnUncountableOffer(unittest.TestCase):
    """Codex finding 4's shape, which the veto reintroduces if the caller is left alone.

    `_concession` returns as soon as a promotion yields a non-empty `raw`. A vetoed
    per-floorplan promo now yields exactly that — raw with no weeks — so a real
    community-wide offer later in the same promotions array would be lost.
    """

    def test_a_vetoed_promo_does_not_shadow_a_later_community_wide_one(self):
        units_raw = [{"promotions": ["8 weeks free on our 2 bedroom homes",
                                     "6 weeks free on all homes"]}]
        weeks, raw, _scoped = avalon._concession(units_raw, "<html></html>")
        self.assertEqual(6, weeks)
        self.assertTrue(raw)

    def test_a_countable_offer_still_wins_immediately(self):
        units_raw = [{"promotions": ["6 weeks free on all homes",
                                     "8 weeks free on our 2 bedroom homes"]}]
        self.assertEqual(6, avalon._concession(units_raw, "<html></html>")[0])

    def test_an_uncountable_offer_is_kept_when_nothing_countable_follows(self):
        units_raw = [{"promotions": ["8 weeks free on our 2 bedroom homes"]}]
        weeks, raw, _scoped = avalon._concession(units_raw, "<html></html>")
        self.assertIsNone(weeks)
        self.assertTrue(raw, "the banner must survive even when the weeks are dropped")


class TheVetoSurvivesTheProductionRepairStep(unittest.TestCase):
    """Codex, 2026-09-21 review of this fix, P1 — the seam the unit tests missed.

        "For a bedroom-specific offer, this returns `(None, '8 weeks FREE')`,
         discarding the restriction. `the pipeline.finalize_result()` immediately passes
         that text to `merge_concession()`, which restores 8 weeks because it can no
         longer see the bedroom scope. This reproduces across all six modified
         adapters, so the new veto does not survive the production pipeline."

    `finalize_result` repairs in place whenever `concession_weeks is None and
    raw_concession_text` — which is EXACTLY the state a veto produces. So the raw text
    a veto returns has to carry the scope that drove it, or the fix is undone one
    function later and nothing downstream can tell.
    """

    def _repaired(self, adapter_raw: str) -> int | None:
        result = ExtractResult(units=[UnitGroup(beds=3, sf_min=1400, sf_max=1400,
                                                available=2, rent_min=2100, rent_max=2100)],
                               concession_weeks=None, raw_concession_text=adapter_raw)
        merge_concession(result, result.raw_concession_text, monthly_rent=2100)
        return result.concession_weeks

    def test_the_repair_step_cannot_resurrect_a_vetoed_offer(self):
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan(HOU_BANNER)
                self.assertIsNone(weeks)
                self.assertIsNone(self._repaired(raw),
                                  f"{name}: merge_concession restored the weeks from {raw!r}")

    def test_a_scope_marker_BEFORE_the_offer_is_kept_too(self):
        text = "Our spacious 3 bedroom homes are leasing now with 6 weeks free this month."
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan(text)
                self.assertIsNone(weeks, f"{name} recorded a 3BR-only offer site-wide")
                self.assertIsNone(self._repaired(raw),
                                  f"{name}: scope before the offer was cut from {raw!r}")

    def test_a_countable_offer_still_stores_the_bare_match(self):
        # Unchanged contract on the non-veto path: every adapter has always stored the
        # matched phrase, and merge_concession re-derives the same count from it.
        for name, scan in SCANNERS.items():
            with self.subTest(adapter=name):
                weeks, raw = scan("Enjoy 6 weeks free on all homes. Spacious 3 bedroom homes available.")
                self.assertEqual(6, weeks)
                self.assertEqual("6 weeks free", raw.lower())


if __name__ == "__main__":
    unittest.main()
