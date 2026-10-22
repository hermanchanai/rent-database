"""The ONE concession scanner survives the production repair step (M4,).

`the pipeline.finalize_result` repairs any result whose weeks are None but whose raw banner
is set, by re-scanning the raw text alone (`merge_concession`). So whatever raw a VETO
returns must still be vetoed when re-read. Found while unifying the seven adapters onto
`scan_concession` (M4 C1; that switch is HELD -- see
_review/m4_rent_contract_2026-09-25/PLAN.md): the scanner's veto returned a 15-char-lead
snippet, which cuts a bed-scope marker sitting BEFORE the offer (the adapters' old
`local_concession` returned `_scope_window` for exactly this,), and Codex's review
of the first fix showed a 60-char tail cuts the SECOND tier of a tiered banner that the
old 130-char snippet kept. Separately, the scanner's free-amenity veto searched the whole
nearby window, so "6 weeks free rent + free parking" lost its rent offer.

These affect the LLM tier (reconcile_concession is authoritative over the model's count)
and the landing-page sweep today. Every case below is from a row or a review finding.
"""
from __future__ import annotations

import unittest


from rentdb.concessions import scan_concession  # noqa: E402


class VetoSurvivesRepair(unittest.TestCase):
    def test_a_scope_marker_before_the_offer(self):
        #'s pinned sentence (rentdb/adapters/test_concession_bed_scope_adapters.py)
        text = "Our spacious 3 bedroom homes are leasing now with 6 weeks free this month."
        weeks, raw = scan_concession(text, None)
        self.assertIsNone(weeks)
        self.assertIsNone(scan_concession(raw, None)[0], f"repair re-reads {raw!r}")

    def test_the_hou_banner(self):
        # a past bug's verbatim HOU banner
        text = "8 weeks FREE! On One bedroom Unit starting at $1099 and Two bedroom Unit starting at $13.."
        weeks, raw = scan_concession(text, None)
        self.assertIsNone(weeks)
        self.assertIsNone(scan_concession(raw, None)[0], f"repair re-reads {raw!r}")

    def test_a_tiered_banner_keeps_its_second_tier(self):
        # Codex review of M4 C1, round 1, P2
        text = ("8 weeks free on 1 & 2 bedroom homes. Limited time offer, contact our "
                "leasing office for details. 4 weeks free on 3 bedroom homes.")
        weeks, raw = scan_concession(text, None)
        self.assertIsNone(weeks)
        self.assertIsNone(scan_concession(raw, None)[0], f"repair re-reads {raw!r}")


class AmenityVetoBelongsToItsOwnOffer(unittest.TestCase):
    # Codex review of M4 C1, round 1, P2; the vetoed forms are the scanner's own pins.
    CASES = {"6 weeks free rent + free parking": 6,
             "2 Months Free + Free Parking for a year": 8,
             "1 month free parking": None,
             "Free parking for 2 months": None,
             "8-weeks free + 1 month free parking + $500 off your first month": 8}  #

    def test_cases(self):
        for text, want in self.CASES.items():
            with self.subTest(text=text):
                self.assertEqual(want, scan_concession(text, None)[0])


if __name__ == "__main__":
    unittest.main()
