"""A dollar-off move-in credit is NOT a rent concession. the comp rule, 2026-08-26.

WHY THIS IS A RULE AND NOT A PARSER BUG. `concession_weeks` is site-level and feeds
net-effective rent in `rents.v_latest_unit_groups` as `asking x (1 - weeks/52)`, i.e. it models
ONGOING rent relief spread across the lease. "$500 off your first month" is a one-time move-in
credit; converting it into 2 weeks of free rent tells a comp set the asking rent is permanently
lower than it is. The SOP's step-6 labelling rule already said dollar-off is not a concession,
and every golden label follows it — the scanner did not.

WHERE IT ACTUALLY LIVED. Found by the HOU/mf golden round via Huntcliff's "Get $500 base rent
OFF your first month!", which stored `concession_weeks = 2`. My first diagnosis blamed
`realpage._concession` and was WRONG: that function only matches weeks-free and months-free and
returns None here, as do the local functions in ark, knock, rentcafe and sightmap. The
conversion is step 3 of the SHARED scanner, `concessions.scan_concession`, and it fires only
when a caller supplies `monthly_rent` — which the fill-if-missing helpers in this same module do.

SCOPE, measured before the change: 437 live communities, 10.1% of all rows carrying a
concession, across every adapter (llm_haiku 155, rentcafe 94, knock 88, sightmap 48, entrata 36,
realpage 11, repli360 5) and every market. Their `concession_weeks` goes NULL; the raw banner is
still captured, which is what the audit tab and NER surface.
"""

from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession


class DollarOffNeverConvertsTests(unittest.TestCase):
    """The banner is kept; the weeks are not invented — with or without a rent."""

    CASES = [
        "Get $500 base rent OFF your first month!",                     # HOU Huntcliff
        "Up to $500 First Month's Rent on Select Homes*",               # the old self-test case
        "$1,000 OFF your first full month on select apartment homes!",  # TPA Westbury
        "Receive up to $1000 off your rent!",                           # HSV The Davis
        "$1,000 off",                                                   # HOU Cortland River Oaks
    ]

    def test_no_weeks_even_when_a_rent_is_supplied(self):
        for text in self.CASES:
            with self.subTest(text=text):
                weeks, raw = scan_concession(text, monthly_rent=2300)
                self.assertIsNone(weeks, "a one-time dollar credit is not ongoing rent relief")
                self.assertIsNotNone(raw, "the banner itself must still be captured")

    def test_no_weeks_without_a_rent_either(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertIsNone(scan_concession(text)[0])

    def test_the_raw_banner_survives(self):
        _, raw = scan_concession("Get $500 base rent OFF your first month!", monthly_rent=1299)
        self.assertIn("500", raw)


class FreeRentStillConvertsTests(unittest.TestCase):
    """The change must be surgical: only the dollar branch stops producing weeks."""

    def test_weeks_free(self):
        self.assertEqual(6, scan_concession("Your Key to 6 Weeks FREE")[0])

    def test_months_free(self):
        self.assertEqual(8, scan_concession("2 Months Free on all new leases")[0])

    def test_fractional_months(self):
        self.assertEqual(6, scan_concession("Up to 1.5 Months Free on Select Apartments!")[0])

    def test_first_month_free_is_still_four_weeks(self):
        """An ordinal FREE month is free rent. Only a DOLLAR amount is excluded."""
        self.assertEqual(4, scan_concession("receive ONE MONTH FREE on select homes")[0])

    def test_a_rent_argument_no_longer_changes_any_answer(self):
        """monthly_rent existed only to convert dollars. Nothing else should depend on it."""
        for text in ["Your Key to 6 Weeks FREE", "2 Months Free", "Get $500 off your first month",
                     "Up to 1.5 Months Free", "Sunshine, Savings & Home Sweet Home"]:
            with self.subTest(text=text):
                self.assertEqual(scan_concession(text)[0],
                                 scan_concession(text, monthly_rent=1850)[0])

    def test_a_waived_fee_is_still_not_a_concession(self):
        self.assertIsNone(scan_concession("$55 application fee waived", monthly_rent=2300)[0])


if __name__ == "__main__":
    unittest.main()
