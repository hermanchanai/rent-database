"""plural "BRs" is a bed scope, a total stated AFTER the number is the offer, and a
community-wide offer beside a bed-type add-on is the site-level count.

The house rule (the project rules "Concessions"): concession_weeks is SITE-LEVEL -- NER applies it to
every unit -- so an offer scoped to a named bed type is not recorded, and a banner quoting
different amounts per bed type ("tiered") is ambiguous -> null. "X months free" = X x 4 weeks.

Expected values are derived from that rule, never from scanner output. The three banners are
verbatim from production rents.weekly_snapshots (read 2026-10-08); made their hyphenated
forms readable, so each is pinned in BOTH the hyphenated (production) and the spaced spelling.
  1. 1 month on 1 & 2 BRs, 10 weeks on 3BRs: two amounts by bed type -> None (tiered).
  2. 6 weeks + an additional 2 weeks "(8 Weeks Total)": the banner states its own total -> 8.
  3. first full month free on ALL homes + an additional half month on select 3-bedroom homes:
     the site-level offer is the month (4 weeks); the half month is a 3BR-only add-on.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace


from rentdb.concessions import merge_concession, reconcile_concession, scan_concession

# --- verbatim production banners (rents.weekly_snapshots.raw_concession_text) ---------------
P1_TIERED_BRS = ("1-month free on 1 and 2 BRs with 12-15-month lease terms, 10-weeks free on 3BRs "
                 "with 12-15-month terms.")
P2_TOTAL_AFTER = ("6-weeks FREE on all apartments! Lock in extra savings with our Look and Lease "
                  "Special - Receive an additional 2-weeks FREE (8 Weeks Total) when you apply "
                  "within 48-hours of touring.")
P3_ALL_HOMES_PLUS_3BR = ("o main content FIRST FULL MONTH FREE on all apartment homes! Take an "
                         "additional HALF-MONTH FREE on select 3-bedroom apartment homes! Must "
                         "apply by August 26th! *Speci")

# the spaced twins -- the same offers, the spelling main read before
S1_TIERED_BRS = ("1 month free on 1 and 2 BRs with 12-15-month lease terms, 10 weeks free on 3BRs "
                 "with 12-15-month terms.")
S2_TOTAL_AFTER = P2_TOTAL_AFTER.replace("6-weeks", "6 weeks").replace("2-weeks", "2 weeks")
S3_ALL_HOMES_PLUS_3BR = P3_ALL_HOMES_PLUS_3BR.replace("HALF-MONTH", "HALF MONTH")


class ProductionBanners3603(unittest.TestCase):
    CASES = [
        ("P1 tiered by bed (hyphen)", P1_TIERED_BRS, None),
        ("P1 tiered by bed (spaced)", S1_TIERED_BRS, None),
        ("P2 total after (hyphen)", P2_TOTAL_AFTER, 8),
        ("P2 total after (spaced)", S2_TOTAL_AFTER, 8),
        ("P3 all homes + 3BR add-on (hyphen)", P3_ALL_HOMES_PLUS_3BR, 4),
        ("P3 all homes + 3BR add-on (spaced)", S3_ALL_HOMES_PLUS_3BR, 4),
    ]

    def test_reads_the_house_rule(self):
        for name, text, want in self.CASES:
            with self.subTest(name):
                got, raw = scan_concession(text)
                self.assertEqual(got, want, f"{name}: {text!r}")
                self.assertTrue(raw, f"{name}: the banner itself must be kept")

    def test_marker_before_the_offer(self):
        # Codex design finding 3: the community-wide marker may PRECEDE the offer; the stored
        # raw must keep it, or a re-read picks the 3BR add-on
        text = ("All apartment homes can receive our limited-time offer of a FIRST FULL MONTH "
                "FREE! Take an additional HALF-MONTH FREE on select 3-bedroom homes.")
        got, raw = scan_concession(text)
        self.assertEqual(got, 4)
        self.assertEqual(scan_concession(raw)[0], 4, raw)

    def test_raw_rereads_to_the_same_answer(self):
        # finalize_result re-scans a weeks-None raw (merge_concession); reconcile re-reads raw.
        # Whatever raw the scanner stores must carry its own verdict.
        for name, text, want in self.CASES:
            with self.subTest(name):
                _w, raw = scan_concession(text)
                self.assertEqual(scan_concession(raw)[0], want, f"{name}: raw {raw!r}")

    def test_repair_step_does_not_restore_a_tier(self):
        for text in (P1_TIERED_BRS, S1_TIERED_BRS):
            with self.subTest(text[:30]):
                w, raw = scan_concession(text)
                r = SimpleNamespace(concession_weeks=w, raw_concession_text=raw)
                merge_concession(r, raw)
                self.assertIsNone(r.concession_weeks)

    def test_reconcile_over_a_model_count(self):
        # the LLM path: the scanner is authoritative when it reads the model's own banner
        for text, model, want in ((P2_TOTAL_AFTER, 8, 8), (S2_TOTAL_AFTER, 8, 8),
                                  (P2_TOTAL_AFTER, 6, 8),
                                  (P1_TIERED_BRS, 10, None), (S1_TIERED_BRS, 10, None),
                                  (P3_ALL_HOMES_PLUS_3BR, 4, 4), (S3_ALL_HOMES_PLUS_3BR, 2, 4)):
            with self.subTest((text[:30], model)):
                r = SimpleNamespace(concession_weeks=model, raw_concession_text=text,
                                    flag_review=False)
                reconcile_concession(r)
                self.assertEqual(r.concession_weeks, want)


# Design review finding 1: the seven adapters run their
# OWN private regexes through `local_concession`, which returned the first match ("6 weeks
# FREE") -- and finalize_result never repairs or sweeps a non-null count, so the spaced P2
# banner stored 6 on every adapter even with the shared scanner fixed. These run each adapter's
# real scanner and then finalize_result's own order: repair a weeks-None raw
# (merge_concession), then the landing sweep over the page (sweep_concession).
from rentdb.adapters import ark, avalon, entrata, knock, realpage, rentcafe, sightmap  # noqa: E402
from rentdb.concessions import sweep_concession  # noqa: E402

ADAPTER_SCANNERS = {
    "ark": lambda t: ark._extract_concession(t),
    "avalon": lambda t: avalon._scan_concession_text(t),
    "entrata": lambda t: entrata._concession(t),
    "knock": lambda t: knock._concession(t),
    "realpage": lambda t: realpage._concession(t),
    "rentcafe": lambda t: rentcafe._extract_concession(t),
    "sightmap": lambda t: sightmap._extract_concession(t),
}


def adapter_then_finalize(scan, page_text):
    w, raw = scan(page_text)
    r = SimpleNamespace(concession_weeks=w, raw_concession_text=raw)
    if r.concession_weeks is None and r.raw_concession_text:
        merge_concession(r, r.raw_concession_text)
    if r.concession_weeks is None:
        sweep_concession(r, page_text)
    return r.concession_weeks


class AdapterPath3603(unittest.TestCase):
    def test_every_adapter_stores_the_house_answer(self):
        for name, text, want in ProductionBanners3603.CASES:
            for adapter, scan in ADAPTER_SCANNERS.items():
                with self.subTest(adapter=adapter, case=name):
                    self.assertEqual(adapter_then_finalize(scan, text), want,
                                     f"{adapter} / {name}: {text!r}")


class LeadingMarker3603(unittest.TestCase):
    """Codex r4 (codex_review_3603_r4_20261008-113357.md) P1: a community-wide marker binds to
    the offer it introduces, never to a bed-scoped add-on later in the same clause. House rule:
    the site-level (all-homes) offer is the count; a bed-type add-on is per-floorplan."""
    CASES = [
        # marker BEFORE the base offer, add-on scoped three ways
        ("All homes get 6 weeks free and select 3BRs get an additional 2 weeks free", 6),
        ("All homes get 6 weeks free and select 3-bedroom homes get an additional 2 weeks free", 6),
        ("All homes get 6 weeks free and select 3BR homes get an additional 2 weeks free", 6),
        ("All apartment homes receive 1 month free, and 3BRs receive an additional 2 weeks free", 4),
        ("All homes: first month free, plus an additional half month free on 3BRs", 4),
        # marker AFTER the base offer
        ("6 weeks free on all homes and an additional 2 weeks free for select 3BRs", 6),
        ("6 weeks free for all homes, plus 2 weeks free on 3-bedroom homes", 6),
        ("1 month free on all apartment homes and an additional 2 weeks free on select 3BR homes", 4),
        # no community-wide offer at all: tiers stay null
        ("Select 3BRs get 6 weeks free and 2BRs get 4 weeks free", None),
    ]

    def test_site_level_offer_wins(self):
        for text, want in self.CASES:
            with self.subTest(text):
                got, raw = scan_concession(text)
                self.assertEqual(got, want, text)
                self.assertEqual(scan_concession(raw)[0], want, f"raw {raw!r}")


class Guards3603(unittest.TestCase):
    """What the fix must NOT change."""
    CASES = [
        # plural BRs as ONE community-wide enumeration keeps its count (_MULTI_BED_SCOPE)
        ("8 weeks free on 1 & 2 BRs!", 8),
        ("8-weeks free on 1 and 2 BRs", 8),
        # a single named plural bed type is a per-floorplan special -> vetoed
        ("6 weeks free on our 2BRs!", None),
        ("Get 1 month free on 3 BRs only", None),
        # Codex diff review (codex_review_3603_20261008-104152.md) P2 1: a plural bed count in a
        # DIFFERENT claim is inventory / price copy, not the offer's scope
        ("2BRs available. Receive 6 weeks free!", 6),
        ("Enjoy up to 10 weeks free base rent. 1BRs starting at $1,649 ( $1,525 base rent).", 10),
        # Codex diff review P2 2: a "N weeks total" that does not directly follow an offer is not
        # that offer's total
        ("6 weeks free on all homes! Tour the pool today. Average stay: 8 weeks total.", 6),
        ("6 weeks free on all homes! Ask about our 8 weeks total relocation window", 6),
        # Codex diff re-review (codex_review_3603_r2_20261008-111627.md): the add-on may share the
        # community-wide offer's clause with no punctuation between them
        ("6 weeks free on all homes plus an additional 2 weeks free on 3BRs", 6),
        ("6 weeks free on all homes plus an additional 2 weeks free on 3 bedroom homes", 6),
        ("FIRST FULL MONTH FREE on all apartment homes and an additional HALF-MONTH FREE on "
         "select 3-bedroom apartment homes", 4),
        # Codex r3 (codex_review_3603_r3_20261008-112434.md) P2 1: a bed type in an EARLIER
        # sentence does not head the community-wide offer's banner
        ("Explore our 2 bedroom homes. FIRST FULL MONTH FREE on all apartment homes! Take an "
         "additional HALF-MONTH FREE on select 3-bedroom apartment homes!", 4),
        # Codex r3 P2 2: a stated total scoped to a bed type is not the site-level offer
        ("6 weeks FREE on all apartments! An additional 2 weeks FREE (8 Weeks Total) applies to "
         "3-bedroom homes", 6),
        # Codex r5 (codex_review_3603_r5_20261008-114404.md) P1: a duration followed by lease /
        # term wording is the LEASE, never the offer's total
        ("Get 2 weeks free (6 months total lease)", 2),
        ("Get 1 month free (12-month lease total)", 4),
        ("Get 6 weeks free (13 months total)", 6),
        ("2 weeks free on all homes (6 months total term)", 2),
        # ...while a real stated total still counts
        ("4 weeks free in October plus 2 weeks free in November, a total of 6 weeks free!", 6),
        ("2 weeks free + 4 weeks free (6 weeks total)", 6),
        ("1 month free + 2 weeks free (6 weeks total)", 6),
        # Codex r7 (codex_review_3603_r7_20261018-014253.md) P2: a stated total SUMS the offers
        # it follows; a stay / any other duration after "total" that is not that sum is not one
        ("Get 2 weeks free (6 months total stay)", 2),
        ("Get 2 weeks free (6 months total)", 2),
        ("6 weeks free on all homes (6 months total residency required)", 6),
        # Codex r5 P2: a comma or a line break ends the plural bed count's claim too
        ("2BRs available, Receive 6 weeks free!", 6),
        ("2BRs available\nReceive 6 weeks free!", 6),
        # ...but a plural inside the offer's own clause still scopes it
        ("Receive 6 weeks free on 2BRs, restrictions apply", None),
        # a stated total no larger than the headline offer does not displace it
        ("Up to 8 weeks free! 2 weeks free in October (2 weeks total).", 8),
        # a lone "(N weeks total)" beside a reversed offer reads as before
        ("Additional Weeks Free Base Rent (up to 10 weeks total) *Other costs & fees excluded.", 10),
        # tiers with NO community-wide offer stay null
        ("6 weeks free on 2BR, 8 weeks free on 3BR", None),
        ("Eight Weeks Free On 1 & 3 Bedrooms, Six Weeks Free On 2 & 4 Bedrooms", None),
        # a community-wide offer beside a named-bed add-on is the site-level count
        ("6 weeks free on all homes! Plus an additional 2 weeks free on 3 bedroom homes.", 6),
        # ...but not when the "all" offer is itself scoped to a bed type
        ("6 weeks free on all 2 bedroom homes! 4 weeks free on 3 bedroom homes.", None),
    ]

    def test_guards(self):
        for text, want in self.CASES:
            with self.subTest(text):
                self.assertEqual(scan_concession(text)[0], want, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
