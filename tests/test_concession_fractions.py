"""scan_concession reads FRACTIONAL months as fractions, not as their last digit.

    PYTHONPATH=. ~/.venvs/bfr-rent-tracker/bin/python rentdb/test_concession_fractions.py

Found 2026-09-21 while simulating the scanner-override across the 2,323 latest
llm_haiku snapshots that carry a model week count: `_NUM` had no fraction form, so
"1/2 Month Free" matched as "2 Month" (8 weeks — 15 live rows), "Two and a half Months
Free" matched on the "half" token (2 weeks — 5 rows), "Half a Month FREE" matched on
"a Month" (4 weeks — 4 rows). Every adapter goes through this scanner, so adapter rows
already carried the wrong numbers; the override would have copied them onto LLM rows
that were right. The rule lives in ONE place, so the fix does too.
"""

from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession


class FractionalMonthsTests(unittest.TestCase):
    CASES = [
        ("1/2 Month Free!", 2),
        ("Receive 1/2 month free rent on select homes!", 2),
        ("1/2 Month Rent Free + 50% Off Security Deposit (Ends: Sep 30)", 2),
        ("Enjoy ½ month RENT FREE on select apartment homes", 2),
        ("Half a Month FREE", 2),
        ("One Half a Month FREE!", 2),
        ("Two and a half Months Free Rent + Up to a $1,000 Gift Card!", 10),
        ("2 1/2 Months free", 10),
        ("Up to 1 1/2 Months Free base rent!", 6),
        ("up to one and a half months free", 6),
        ("A month and a half FREE on select all available suites!", 6),
        ("Free rent for a month and a half", 6),          # reverse form
        ("free rent for two and a half months", 10),
        ("Get a 1/2 month free!", 2),                      # article is not a whole number
        # unchanged neighbours — the integer and decimal forms must still read as before
        ("2.5 Months FREE", 10),
        ("2 Months Free", 8),
        ("Half Month Free Rent", 2),
    ]

    def test_zero_denominator_never_raises(self):
        # Codex r2 (2026-09-21): a malformed fraction must be refused, not raised — in the
        # batch path one bad banner would abort the whole collection.
        weeks, raw = scan_concession("Free rent for 1/0 months")
        self.assertIsNone(weeks)

    def test_tiered_detection_sees_the_half(self):
        # Codex r2: _is_tiered compared 6 against 4 here and refused the enumeration.
        from rentdb.concessions import _is_tiered
        text = "1 1/2 months free on 1 & 2 bedrooms; 1 month and a half free on 3 & 4 bedrooms"
        self.assertFalse(_is_tiered(text))
        self.assertEqual(scan_concession(text)[0], 6)

    def test_fractions_parse_as_fractions(self):
        for text, want in self.CASES:
            with self.subTest(text=text):
                got, raw = scan_concession(text)
                self.assertEqual(got, want, f"{text!r}: want {want}, got {got} (raw={raw!r})")
                self.assertIsNotNone(raw)


if __name__ == "__main__":
    unittest.main()
