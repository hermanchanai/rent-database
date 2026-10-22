"""Offline regression tests for the two value-level guards behind the
`rent_absurd_bounds` dq failure (83 violations, 2026-08-10).

Both are "null + flag_review, never a guessed value" guards, per the project rules: we can
prove the number is not a monthly rent, we cannot know what the rent actually is.

  1. `base.clamp_implausible_rents` — rent per SF as the discriminator. The old
     >$50k/mo clamp lived in extractor.py and so only ever covered the LLM tier,
     while the fleet's single worst value (Paseo AUS, 2BR 1,784sf at $45,263 =
     $25.37/sf) comes from the ENTRATA adapter on a bookable card and sailed past it.
  2. `sightmap._drop_placeholder_plans` — Aventerra at Dobson Ranch's `TEMP` plan
     (`bedroom_count=0`, `area=1`, one unit at $1,209), which every run surfaced as
     a real 0-bed group. 22 groups across 15 communities carry SF <= 5 fleet-wide.

Every number below is a real measured value, not an invented one.
"""

from __future__ import annotations

import unittest

from rentdb.adapters import sightmap
from rentdb.adapters.base import ExtractResult, UnitGroup, clamp_implausible_rents


def _g(beds, sf, rent, **kw) -> UnitGroup:
    return UnitGroup(beds=beds, sf_min=sf, sf_max=sf, available=1,
                     rent_min=rent, rent_max=rent, **kw)


class ClampImplausibleRentTests(unittest.TestCase):
    def test_nulls_the_lease_aggregate_reads_and_keeps_real_luxury(self):
        """The boundary this guard has to get right. Above $10/sf every fleet group
        was a confirmed parse error; the highest verified-real reads sit below it."""
        result = ExtractResult(units=[
            _g(2, 1784, 45263),   # Paseo AUS      $25.37/sf — aggregate, entrata
            _g(2, 1841, 22763),   # Paseo AUS      $12.36/sf — aggregate
            _g(1, 614, 17785),    # Galleria Palms $28.97/sf — llm_haiku
            _g(3, 2607, 25842),   # Residences at 6G $9.91/sf — REAL AUS luxury
            _g(1, 1510, 13700),   # The Travis     $9.07/sf — REAL AUS luxury
            _g(3, 2716, 19718),   # 700 River      $7.26/sf — REAL, sightmap
            _g(3, 2018, 9950),    # Optima Kierland $4.93/sf — REAL PHX luxury
            _g(1, 690, 1864),     # Avilla Holloway $2.70/sf — ordinary
        ])
        clamp_implausible_rents(result, "test")
        self.assertEqual([None, None, None, 25842, 13700, 19718, 9950, 1864],
                         [g.rent_max for g in result.units])
        self.assertEqual([True, True, True, False, False, False, False, False],
                         [g.flag_review for g in result.units])

    def test_nulls_both_bounds_together(self):
        """rent_min must go too — half a nulled range is worse than none."""
        result = ExtractResult(units=[UnitGroup(beds=2, sf_min=773, sf_max=773,
                                                available=9, rent_min=1300, rent_max=16800)])
        clamp_implausible_rents(result, "llm_haiku")
        self.assertEqual((None, None), (result.units[0].rent_min, result.units[0].rent_max))

    def test_absolute_ceiling_still_applies_without_sf(self):
        """A condo sale price read as monthly rent, on a group with no SF at all."""
        result = ExtractResult(units=[UnitGroup(beds=3, sf_min=None, sf_max=None,
                                                available=1, rent_min=525000, rent_max=525000)])
        clamp_implausible_rents(result, "llm_haiku")
        self.assertIsNone(result.units[0].rent_max)

    def test_junk_sf_does_not_drive_the_psf_test(self):
        """A 1-sf placeholder makes every psf ratio astronomical. That group is the
        sightmap placeholder class and is dropped there; this guard must not claim
        it, or the log would blame the rent for a bad SF."""
        result = ExtractResult(units=[_g(0, 1, 1209)])
        clamp_implausible_rents(result, "sightmap")
        self.assertEqual(1209, result.units[0].rent_max)

    def test_clean_result_is_untouched(self):
        result = ExtractResult(units=[_g(1, 690, 1864), _g(2, 1052, 2268)])
        clamp_implausible_rents(result, "test")
        self.assertIsNone(result.scrape_error)
        self.assertFalse(result.flag_review)

    def test_the_defect_is_recorded_not_silent(self):
        result = ExtractResult(units=[_g(2, 1784, 45263)])
        clamp_implausible_rents(result, "entrata")
        self.assertTrue(result.flag_review)
        self.assertIn("entrata", result.scrape_error)
        self.assertIn("25.37/sf", result.scrape_error)


class SightmapPlaceholderPlanTests(unittest.TestCase):
    # Aventerra at Dobson Ranch's real feed shape (verified byte-identical across
    # 5 consecutive fetches, 2026-08-09).
    PLANS = [
        {"id": 1, "name": "A1", "bedroom_count": 1, "area": 680},
        {"id": 2, "name": "B1", "bedroom_count": 2, "area": 860},
        {"id": 3, "name": "TEMP", "bedroom_count": 0, "area": 1},
    ]
    UNITS = [
        {"floor_plan_id": 1, "area": 680, "price": 1057},
        {"floor_plan_id": 2, "area": 860, "price": 1421},
        {"floor_plan_id": 3, "area": 1, "price": 1209},
    ]

    def test_temp_plan_and_its_unit_are_dropped(self):
        plans, units, dropped = sightmap._drop_placeholder_plans(self.PLANS, self.UNITS)
        self.assertEqual(["A1", "B1"], [p["name"] for p in plans])
        self.assertEqual([680, 860], [u["area"] for u in units])
        self.assertEqual(["TEMP"], dropped)

    def test_real_plans_whose_names_merely_contain_a_placeholder_word_survive(self):
        """Exact match only — substring matching would eat "Temple" and "Modelo"."""
        plans = [{"id": 1, "name": "Temple", "bedroom_count": 1, "area": 700},
                 {"id": 2, "name": "Modelo", "bedroom_count": 2, "area": 950},
                 {"id": 3, "name": "The Testament", "bedroom_count": 3, "area": 1400}]
        kept, _units, dropped = sightmap._drop_placeholder_plans(plans, [])
        self.assertEqual([], dropped)
        self.assertEqual(3, len(kept))

    def test_a_junk_area_is_enough_on_its_own(self):
        """The name is not always the tell — an area below any leasable size is."""
        plans = [{"id": 9, "name": "Penthouse", "bedroom_count": 0, "area": 3}]
        kept, _units, dropped = sightmap._drop_placeholder_plans(plans, [])
        self.assertEqual([], kept)
        self.assertEqual(["Penthouse"], dropped)

    def test_a_plan_with_no_area_is_kept(self):
        """SightMap floor_plans often carry no size field at all — absent is not junk."""
        plans = [{"id": 4, "name": "C2", "bedroom_count": 2, "area": None}]
        kept, _units, dropped = sightmap._drop_placeholder_plans(plans, [])
        self.assertEqual(1, len(kept))
        self.assertEqual([], dropped)

    def test_no_zero_bed_group_survives_the_full_parse(self):
        """End to end: the whole point is that `TEMP` stops being a 0-bed group."""
        groups, _warnings = sightmap._parse_api({"data": {
            "floor_plans": self.PLANS,
            "units": [dict(u, available_on="2026-08-15") for u in self.UNITS],
        }})
        self.assertNotIn(0, [g.beds for g in groups])
        self.assertEqual([1, 2], sorted(g.beds for g in groups))


if __name__ == "__main__":
    unittest.main()
