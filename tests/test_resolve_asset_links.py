"""resolve_data_url must not hand the LLM a stylesheet or a non-data page.

THE DEFECT, measured 2026-08-20 during the Tampa/Nashville needs_adapter triage:
22 of the 86 unparseable communities were misrouted by the resolver — 7 to a `.css`
file and 15 to `/apply`, `/feed`, `/gallery` or `/amenities`. 19 of them show real
pricing on the page the resolver SHOULD have picked.

MECHANISM. `_DATA_HREF_RE` matches any `href="…"` containing one of
floor-plans/availab/pricing/apartments/residences. Two things make that far broader
than intended:

  1. It matches `<link rel="stylesheet" href="…">`, not just anchors — and <head>
     comes before <body>, so a stylesheet is frequently `candidates[0]`.
  2. The token can live in the community's OWN BASE PATH. On
     robbinspropertyllc.com/apartments/fl/tampa/4800-westshore/ every internal link
     on the entire site contains "apartments", so `/apply` and `/gallery` qualify
     as "floor-plan-ish" exactly as much as `/floorplans` does.

Both then reach `resolve_data_url` branch 3:

    if home_score == 0 and first_link:
        return first_link          # candidates[0], document order

which is the JS-rendered-site path — precisely the sites this class lands on. The
homepage scores 0 because it is a JS shell, so the resolver returns the FIRST
candidate, and on these sites that is an asset or a nav link.

This is a sibling of the cross-property defect pinned in
test_resolve_cross_property.py: same regex, same over-matching, different victim.
That one asked "is this the same PROPERTY"; this one asks "is this a PAGE THAT
COULD CARRY UNIT DATA AT ALL".
"""

from __future__ import annotations

import unittest

from rentdb.adapters import probe


BASE = "https://www.robbinspropertyllc.com/apartments/fl/tampa/4800-westshore"


def _page(*hrefs: str) -> str:
    head = "".join(
        f'<link rel="stylesheet" href="{h}">' for h in hrefs if h.endswith(".css"))
    body = "".join(
        f'<a href="{h}">x</a>' for h in hrefs if not h.endswith(".css"))
    return f"<html><head>{head}</head><body>{body}</body></html>"


class AssetAndNonDataLinksAreNotCandidates(unittest.TestCase):

    def test_stylesheet_is_not_a_candidate(self):
        """The 7-row `.css` class. A stylesheet can never carry unit data."""
        html = _page("/apartments/fl/tampa/4800-westshore/css/main.css",
                     "/apartments/fl/tampa/4800-westshore/floorplans")
        cands = probe._candidate_links(html, BASE)
        self.assertFalse(
            [c for c in cands if c.endswith(".css")],
            f"a .css file was offered to the LLM tier as a data page: {cands[:3]}")

    def test_stylesheet_in_head_does_not_become_first_link(self):
        """`<head>` precedes `<body>`, so document order alone puts the asset first.

        This is the exact shape branch 3 returns on a JS-rendered site.
        """
        html = _page("/apartments/fl/tampa/4800-westshore/css/main.css",
                     "/apartments/fl/tampa/4800-westshore/floorplans")
        cands = probe._candidate_links(html, BASE)
        self.assertTrue(cands, "expected at least the floorplans link")
        self.assertNotIn(".css", cands[0],
                         f"candidates[0] is an asset, and branch 3 returns it verbatim: {cands[0]}")

    def test_non_data_pages_are_not_candidates(self):
        """The 15-row `/apply` `/gallery` `/amenities` `/feed` class.

        These match only because the base path contains 'apartments'.
        """
        html = _page(*(f"/apartments/fl/tampa/4800-westshore/{p}"
                       for p in ("apply", "gallery", "amenities", "feed", "contact")))
        cands = probe._candidate_links(html, BASE)
        offending = [c for c in cands
                     if any(c.rstrip("/").endswith("/" + p)
                            for p in ("apply", "gallery", "amenities", "feed", "contact"))]
        self.assertEqual([], offending,
                         f"non-data pages offered as unit-data pages: {offending}")

    def test_the_real_floorplan_link_still_survives(self):
        """The guard must not cost us the page we actually want."""
        html = _page("/apartments/fl/tampa/4800-westshore/css/main.css",
                     "/apartments/fl/tampa/4800-westshore/apply",
                     "/apartments/fl/tampa/4800-westshore/floorplans")
        cands = probe._candidate_links(html, BASE)
        self.assertTrue(
            any(c.endswith("/floorplans") for c in cands),
            f"the floor-plan link was filtered away with the noise: {cands[:5]}")

    def test_availability_and_pricing_links_still_survive(self):
        for good in ("availability", "pricing", "floor-plans", "residences"):
            with self.subTest(page=good):
                html = _page(f"/apartments/fl/tampa/4800-westshore/{good}")
                cands = probe._candidate_links(html, BASE)
                self.assertTrue(any(c.endswith("/" + good) for c in cands),
                                f"{good!r} must remain a candidate; got {cands[:5]}")

    def test_entrata_application_module_is_not_a_candidate(self):
        """Found by END-TO-END probing, not by unit test.

        After the first fix shipped, all 86 needs_adapter communities were run through
        the live resolver: 0 assets and 0 listed non-data segments, but Legacy Hill and
        Parc Fairview both resolved to `/Apartments/module/application_authentication...`
        — an Entrata login module. Whole-segment equality could never catch it; the
        segment name varies per install. Substring matching does.
        """
        for path in ("/Apartments/module/application_authentication",
                     "/Apartments/module/application_authentication/?prop=1"):
            with self.subTest(path=path):
                base = "https://www.livelegacyhill.com"
                cands = probe._candidate_links(_page(path), base)
                self.assertEqual(
                    [], [c for c in cands if "application_authentication" in c.lower()],
                    f"an Entrata application/login module was offered as a data page: {cands[:3]}")

    def test_cross_property_guard_is_unaffected(self):
        """Belt and braces: the sibling-building rule still rejects a parallel path."""
        gbase = "https://www.greystar.com/palm-court-apartments-phoenix-az/p_12501"
        html = _page("/seven-luxury-apartments-phoenix-az/p_11833")
        self.assertEqual([], [c for c in probe._candidate_links(html, gbase)
                              if "seven-luxury" in c])


if __name__ == "__main__":
    unittest.main()
