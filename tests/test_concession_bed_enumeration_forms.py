"""A hyphenated or comma-ampersand bed ENUMERATION is one community-wide offer.

    PYTHONPATH=. ~/.venvs/bfr-rent-tracker/bin/python rentdb/test_concession_bed_enumeration_forms.py

The rule: an offer that ENUMERATES bed types ("one and two bed", "1 & 2 BR") is a
community-wide offer written the way marketing writes it, not a carve-out. Two spellings
slipped past `_MULTI_BED_SCOPE`: "1- and 2-Bedroom" (the trailing hyphen of "1-" was
consumed as the list connector, so the list never matched and `_BED_SCOPE`'s single-type
veto fired) and "1, 2, & 3 Bedroom" (a comma directly before "&"). Found 2026-09-21 while
measuring the scanner-override: six live LLM rows with a correct count would have
been nulled by it for no reason but these two spellings.
"""

from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession


class EnumerationSpellingsTests(unittest.TestCase):
    COMMUNITY_WIDE = [
        ("4 Weeks Free on 1, 2, & 3 Bedroom Units.", 4),
        ("Enjoy One Month Free on 1- and 2-Bedroom Apartment Homes.", 4),
        ("SIX WEEKS FREE (base rent) on one- and two-bedroom floor plans!", 6),
        ("Enjoy 4 Weeks FREE on 1- & 2-Bedrooms*", 4),
        ("Up to 10 weeks FREE RENT on brand new one-, two- and three-bedroom townhomes!", 10),
        # the spellings that already worked must keep working
        ("8 weeks free 1 & 2 bed", 8),
        ("6 weeks free on one, two and three bedrooms", 6),
    ]
    NAMED_SUBSET = [
        "Offering 6 weeks FREE on two bedroom homes.",
        "1 MONTH FREE ON 4-BEDROOM HOMES!",
        "All one bedrooms 8 weeks free!",
    ]

    def test_enumerations_are_community_wide(self):
        for text, want in self.COMMUNITY_WIDE:
            with self.subTest(text=text):
                self.assertEqual(scan_concession(text)[0], want)

    def test_explicit_community_wide_marker_beats_adjacent_inventory_copy(self):
        # Codex r2 (2026-09-21): scan_concession used _is_named_subset directly and never
        # consulted the explicit-marker protection that offer_is_bed_scoped gives adapters.
        self.assertEqual(scan_concession("All homes receive 6 weeks free! Studios available.")[0], 6)
        self.assertEqual(scan_concession("Every home gets 6 weeks free. Studios available.")[0], 6)
        # the veto stays honest when the marker is a separate sentence about an amenity
        self.assertIsNone(scan_concession("6 weeks free on 2BR. Every home has a garage.")[0])

    def test_single_named_type_still_vetoed(self):
        for text in self.NAMED_SUBSET:
            with self.subTest(text=text):
                weeks, raw = scan_concession(text)
                self.assertIsNone(weeks)
                self.assertIsNotNone(raw)


if __name__ == "__main__":
    unittest.main()
