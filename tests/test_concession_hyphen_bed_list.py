"""A bed enumeration whose hyphens carry SPACES is still one community-wide offer (leg 3).

The house rule (the project rules "Concessions"): hyphen/comma bed enumerations ("1- and 2-Bedroom")
are community-wide. taught `_MULTI_BED_SCOPE` the dangling hyphen written directly after
a list number ("1- and 2-"); a hyphen with a space before it ("1 -and 2-Bedroom",
"1 - & 2 - Bedrooms") still broke the list, `_BED_SCOPE`'s single-type veto fired, and the
offer lost its weeks. Stored production banner, verbatim: "get 2 Months FREE off your base
rent on a 1 -and 2-Bedroom Apartment Homes!".

The guard that must hold: a genuinely TIERED banner -- two different amounts of free rent,
each on its own bed types -- is per-floorplan and stays null (SOP step 6), hyphens or not.
"""

from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession


class HyphenBedListTests(unittest.TestCase):
    COMMUNITY_WIDE = [
        ("today and get 2 Months FREE off your base rent on a 1 -and 2-Bedroom Apartment Homes!", 8),
        ("Enjoy 4 Weeks FREE on 1 - & 2 - Bedrooms", 4),
        ("6 Weeks Free on 1 - and 2 - Bedroom Units", 6),
        # the row's own examples (fixed by) must keep working
        ("Enjoy 4 Weeks FREE on 1- & 2-Bedrooms", 4),
        ("6 Weeks Free on 1- and 2-Bedroom Units", 6),
    ]
    TIERED = [
        "Eight Weeks Free On 1 & 3 Bedrooms, Six Weeks Free On 2 & 4 Bedrooms",  # HOU Veranda Dellbrook
        "8 Weeks Free On 1 - & 3 - Bedrooms, 6 Weeks Free On 2 - & 4 - Bedrooms",
        "Receive 2 months free on a 1-bedroom or 1 month free on a 2-bedroom.",
    ]
    NAMED_SUBSET = [
        "6 weeks free on 2 - Bedroom homes",
        "1 MONTH FREE ON 4-BEDROOM HOMES!",
    ]

    def test_spaced_hyphen_enumeration_is_community_wide(self):
        for text, want in self.COMMUNITY_WIDE:
            with self.subTest(text=text):
                self.assertEqual(scan_concession(text)[0], want)

    def test_tiered_banner_still_vetoed(self):
        for text in self.TIERED:
            with self.subTest(text=text):
                weeks, raw = scan_concession(text)
                self.assertIsNone(weeks)
                self.assertTrue(raw)

    def test_single_named_type_still_vetoed(self):
        for text in self.NAMED_SUBSET:
            with self.subTest(text=text):
                weeks, raw = scan_concession(text)
                self.assertIsNone(weeks)
                self.assertTrue(raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
