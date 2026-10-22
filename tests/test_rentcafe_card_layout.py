"""Offline regression tests for the RentCafe card-layout defects behind the bulk
of the `beds_null_groups` dq failure.

Measured 2026-08-10: `rents_dq.beds_null_groups` failed at 188 communities, and the
breakdown by extractor is rentcafe 151 / entrata 24 / knock 13 — so this is NOT the
entrata "Studio" bug, it is the majority of the assertion on its own.

GROUND TRUTH is https://www.avillaholloway.com/availableunits read live 2026-08-10.
One floor plan ("The Alcove", 1 Bedroom | 1 Bathroom, approximately 690.00 square
feet) with 20 priced unit cards. Every one of those 20 landed in the beds=None
catch-all, so the whole community was invisible to every bed-level consumer while
still reporting `live_data`. The fixture below reproduces the page's nesting
(plan section > row > col > card) and its exact wording.

Three defects, all in `_parse_card_layout`:

  1. `SF_RE` needed 4+ digits (`\\d{1,2}(?:,?\\d{3})`), so "690.00 square feet" —
     any sub-1,000 sf plan — did not match.
  2. Beds were only accepted when a SF match came with them (`bed_match and
     sf_match`). `BEDS_RE` matched "1 Bedroom" fine; defect 1 threw that away.
     The 2026-07-28 fallback could not rescue it because it used the narrower
     `_PLAN_BEDS_RE` (`\\b(\\d)\\s*Beds?\\b`), which cannot match "Bedroom".
  3. The base rent was only read from "Base rent FROM $X". On the "Base rent $X"
     wording the adapter fell through to the all-in "Total Monthly Leasing Price"
     and stored it as `rent_type="base"` — a ~9% overstatement on this page
     ($2,039 all-in recorded instead of $1,864 base) reaching the rent-comp consumer.
"""

from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from rentdb.adapters import rentcafe

# Faithful trim of the live page: the plan heading sits three ancestors above each
# unit card, exactly as RentCafe nests it.
AVILLA_HOLLOWAY = """
<div class="availability-widget-optimized w-100">
  <div class="row no-gutters"><div class="col-12 mt-3"><div class="row">
    <div class="col-lg-8 col-12">
      <h2>The Alcove</h2>
      <p>1 Bedroom | 1 Bathroom</p>
      <p>One bedroom apartment with 1 bathroom. Approximately 690.00 square feet.
         Floor plans are artist's rendering. All dimensions are approximate.</p>
      <div class="row my-3">
        <div class="col-sm-6"><div class="card">
          <h3>Apartment: # 104</h3><p>Available Now</p>
          <p>Total Monthly Leasing Price Starting at: $2,039.00</p>
          <p>Base rent $1,864.00 &middot; 18-month term</p>
        </div></div>
        <div class="col-sm-6"><div class="card">
          <h3>Apartment: # 216</h3><p>Available Now</p>
          <p>Total Monthly Leasing Price Starting at: $2,009.00</p>
          <p>Base rent $1,834.00 &middot; 18-month term</p>
        </div></div>
        <div class="col-sm-6"><div class="card">
          <h3>Apartment: # 409</h3><p>Date Available: 8/14/2026</p>
          <p>Total Monthly Leasing Price Starting at: $1,929.00</p>
          <p>Base rent $1,754.00 &middot; 18-month term</p>
        </div></div>
      </div>
    </div>
  </div></div></div>
</div>
"""


class RentCafeCardLayoutBedAttributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        soup = BeautifulSoup(AVILLA_HOLLOWAY, "html.parser")
        cls.groups, cls.warnings = rentcafe._parse_card_layout(soup)

    def test_no_beds_null_catch_all_group(self):
        """The whole defect: 20 priced units in a beds=None group are invisible to
        every bed-level consumer (eval_golden's bed_rollup drops them outright)."""
        self.assertEqual([], [g for g in self.groups if g.beds is None])
        self.assertEqual([], self.warnings)

    def test_units_attribute_to_the_plans_bed_count(self):
        self.assertEqual(1, len(self.groups))
        self.assertEqual(1, self.groups[0].beds)
        self.assertEqual(3, self.groups[0].available)

    def test_three_digit_square_footage_is_read(self):
        """SF_RE required 4+ digits, so every sub-1,000 sf plan lost its SF."""
        self.assertEqual((690, 690), (self.groups[0].sf_min, self.groups[0].sf_max))

    def test_base_rent_is_the_base_rent_not_the_all_in_price(self):
        """rent_type is "base", so rent_min/max must be the base rent. The all-in
        belongs in rent_allin_min/max — the schema has a field for it."""
        self.assertEqual((1754, 1864), (self.groups[0].rent_min, self.groups[0].rent_max))
        self.assertEqual((1929, 2039),
                         (self.groups[0].rent_allin_min, self.groups[0].rent_allin_max))
        self.assertEqual("base", self.groups[0].rent_type)


class RentCafeFloorplanRegexTests(unittest.TestCase):
    """Pin the exact strings that broke, and the ones that already worked."""

    def test_sf_re_matches_three_and_four_digit_forms(self):
        for text, expected in [
            ("Approximately 690.00 square feet.", "690"),
            ("Up to 1,530 Sq. Ft.", "1,530"),
            ("1530 sq ft", "1530"),
            ("Approximately 1,092.00 square feet.", "1,092"),
        ]:
            with self.subTest(text=text):
                m = rentcafe.SF_RE.search(text)
                self.assertIsNotNone(m, f"SF_RE must match {text!r}")
                self.assertEqual(expected, m.group(1))

    def test_bedroom_wording_resolves_without_a_sf_match(self):
        """"1 Bedroom" must resolve on its own — the old fallback regex needed a
        word boundary right after "Bed", so "Bedroom" never matched it."""
        soup = BeautifulSoup(
            """
            <div class="col-lg-8 col-12">
              <h2>Plan A</h2><p>2 Bedrooms | 2 Bathrooms</p>
              <p>Size not published on this page.</p>
              <div class="card"><h3>Apartment: # 7</h3><p>Available Now</p>
                <p>Base rent $2,100.00</p></div>
            </div>
            """,
            "html.parser",
        )
        groups, _ = rentcafe._parse_card_layout(soup)
        self.assertEqual([2], [g.beds for g in groups])
        self.assertIsNone(groups[0].sf_min)

    def test_table_layout_resolves_bedroom_wording_without_bathroom_text(self):
        """`_find_floorplan_beds_baths` (the td-card-rent path) carried the same
        `_PLAN_BEDS_RE` fallback, so "1 Bedroom" with no parseable bathroom text
        fell through to beds=None there too."""
        soup = BeautifulSoup(
            """
            <div class="fp-section">
              <h2>The Alcove</h2><p>1 Bedroom</p>
              <table><tr>
                <td class="td-card-name">104</td>
                <td class="td-card-rent">$1,864</td>
                <td class="td-card-sqft">690</td>
              </tr></table>
            </div>
            """,
            "html.parser",
        )
        rent_td = soup.find("td", class_="td-card-rent")
        self.assertEqual(1, rentcafe._find_floorplan_beds_baths(rent_td)[0])

    def test_studio_still_resolves_to_zero_beds(self):
        soup = BeautifulSoup(
            """
            <div class="col-lg-8 col-12">
              <h2>S1</h2><p>Studio | 1 Bathroom</p>
              <p>Approximately 505.00 square feet.</p>
              <div class="card"><h3>Apartment: # 12</h3><p>Available Now</p>
                <p>Base rent from $1,295/mo for 12 months</p></div>
            </div>
            """,
            "html.parser",
        )
        groups, _ = rentcafe._parse_card_layout(soup)
        self.assertEqual([0], [g.beds for g in groups])
        self.assertEqual(505, groups[0].sf_min)
        self.assertEqual(1295, groups[0].rent_min)


if __name__ == "__main__":
    unittest.main()
