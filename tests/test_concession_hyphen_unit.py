"""A hyphen between the number and its unit is the same offer as a space.

The house rule (the project rules "Concessions"): weeks free are stored directly and "X months free"
is X x 4. "2-Months FREE" says exactly what "2 Months FREE" says. BFR golden 2026-09-27: ATL
Hayloft Cottages at Big Creek's banner "2-Months Base Rent FREE" stored no concession while
the site gave 8 weeks, and on adapter sites the landing sweep is the only reader, so the
offer was lost outright.

The guard that must hold: a hyphenated LEASE TERM ("12-month lease", "6-month lease") is the
length of the lease, never an offer -- the hyphen must not turn it into weeks. "N-month" with a
SINGULAR unit is that adjective unless N is one ("1-Month Free", "HALF-MONTH FREE").
"""

from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession


class HyphenUnitTests(unittest.TestCase):
    OFFERS = [
        ("2-Months FREE", 8),
        ("2-Months Base Rent FREE", 8),                      # ATL Hayloft Cottages at Big Creek
        ("8-weeks free", 8),
        ("Get 6-Weeks FREE on all homes!", 6),
        ("1-month free rent", 4),
        ("2-months off", 8),
        ("Six-Weeks Free", 6),
        ("Two-Weeks FREE in the month of August 2026!", 2),
        ("Up to 1.5-Months Free on Select Apartments", 6),
        ("Take a HALF-MONTH FREE", 2),
        ("8-wks free", 8),
        # fractions stay fractions
        ("1/2-Month Free", 2),
        ("\u00bd-Month Free", 2),
        ("1 1/2-Month Free", 6),
    ]
    # each spaced twin reads the same -- the spec is "parse like the spaced form"
    SPACED_TWINS = [
        ("2-Months FREE", "2 Months FREE"),
        ("8-weeks free", "8 weeks free"),
        ("2-months off", "2 months off"),
    ]
    # (text, expected weeks): the lease term must never become (or add) an offer
    LEASE_TERMS = [
        ("Ask about our 12-month lease", None),
        ("6-month lease available", None),
        ("12-month lease free of hidden fees", None),
        ("6-month lease, free parking", None),
        ("Sign a 12-month lease and get 1 month free", 4),
        ("1 month free with a 12-month lease", 4),
        ("6 weeks free on a 12-month lease", 6),
        # "N-month" with N > 1 is the adjective of a term: it is never read as N months free
        # (the adversarial sweep's house rule)
        ("Get 6-month free! Move in within 2 weeks.", None),
        ("Sign a 6-month, free! Move in within 2 weeks.", None),
        ("12-month free parking", None),
    ]

    def test_hyphenated_offers_count(self):
        for text, want in self.OFFERS:
            with self.subTest(text=text):
                self.assertEqual(scan_concession(text)[0], want)

    def test_hyphen_reads_like_space(self):
        for hyph, spaced in self.SPACED_TWINS:
            with self.subTest(text=hyph):
                self.assertEqual(scan_concession(hyph)[0], scan_concession(spaced)[0])

    def test_hyphenated_lease_term_is_not_an_offer(self):
        for text, want in self.LEASE_TERMS:
            with self.subTest(text=text):
                self.assertEqual(scan_concession(text)[0], want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
