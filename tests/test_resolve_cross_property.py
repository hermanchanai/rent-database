"""resolve_data_url must never hand back a DIFFERENT PROPERTY's page.

THE DEFECT, measured 2026-08-12 and not hypothetical. Palm Court (PHX mf,
greystar.com/palm-court-apartments-phoenix-az/p_12501) resolved to
greystar.com/seven-luxury-apartments-phoenix-az/p_11833 — a different building —
and the rents stored under Palm Court's name were Seven's:

    stored (rents.weekly_snapshots, 2026-08-12 20:03 UTC)
      707sf 1552-2054 · 747sf 1374-1904 · 1152sf 1707-2489 · 1256sf 2085-3003
    rendered on SEVEN       1357 1374 1535 1552 1690 1707 1904 2054 2068 2085 2489 3003
    rendered on PALM COURT  1313 1332 1425 1444 1538 1557 3035 3814 4547

    8 of 8 stored values appear on SEVEN's page. 0 of 8 appear on Palm Court's.

WHY THE OLD GUARD MISSED IT. probe.py rejected off-DOMAIN links, which is the
right rule for ILS portals and the wrong rule for an operator that runs every
property as a path on one domain. On greystar.com a sibling property IS the same
netloc. `_DATA_HREF_RE` matches any href containing "apartments", so on a Greystar
page every neighbouring property qualifies as a "floor-plan-ish link", and the
first one wins.

BOTH exits were exposed, not just the reported one:
  * the home_score==0 fallback returns candidates[0] outright — the reported path;
  * the SCORING loop returns any candidate that out-scores the homepage, which on
    a statically-rendered operator domain is a sibling with real prices on it.
The fix therefore filters the CANDIDATE LIST, upstream of both.

STRUCTURAL EXPOSURE (rents.communities, url_status='confirmed'): 215 communities
sit on a host shared with another community AND route to the LLM tier, where
resolve_data_url runs. 452 more run the LLM tier but are alone on their host, so
no sibling is reachable for them.
"""

from __future__ import annotations

import unittest
from unittest import mock

# probe.py imports `fetchers` from rentdb/ at call time, so rentdb/ must be on the
# path for mock.patch("rentdb.fetchers.fetch_static") to resolve. Same shim convention as
# rentdb/test_rent_basis.py.

from rentdb.adapters import probe  # noqa: E402

# Greystar's real page shape: sibling properties are same-host paths, and every
# one of them contains "apartments" so _DATA_HREF_RE matches it.
PALM = "https://www.greystar.com/palm-court-apartments-phoenix-az/p_12501"
SEVEN_PATH = "/seven-luxury-apartments-phoenix-az/p_11833"

GREYSTAR_HOME = f"""
<html><body>
  <h1>Palm Court Apartments</h1>
  <nav>
    <a href="{SEVEN_PATH}">Seven Luxury Apartments</a>
    <a href="/the-heritage-apartments-phoenix-az/p_10022">The Heritage Apartments</a>
  </nav>
  <a href="/palm-court-apartments-phoenix-az/p_12501/floorplans">View floor plans</a>
</body></html>
"""

# A single-property marketing site: the community IS the domain, so every
# same-host link is its own. This must keep working exactly as before.
SOLO = "https://www.rentretreat.com"
SOLO_HOME = """
<html><body><a href="/floorplans">Floor Plans</a>
<a href="/availability">Availability</a></body></html>
"""


class _Resp:
    def __init__(self, html="", status=200):
        self.html, self.status = html, status


def _fetcher(pages: dict, default_status=200):
    """fetch_static stub. Any URL not in `pages` returns an empty 200 — i.e. a
    real page that simply carries no prices, which is what scores 0."""
    def _f(u):
        key = u.rstrip("/")
        if key in pages:
            return _Resp(pages[key], default_status)
        return _Resp("", 200)
    return _f


class CandidateFilterTests(unittest.TestCase):
    """The unit the defect lived in."""

    def test_a_sibling_property_is_not_a_candidate(self):
        cands = probe._candidate_links(GREYSTAR_HOME, PALM)
        siblings = [c for c in cands if "seven-luxury" in c or "the-heritage" in c]
        self.assertEqual([], siblings,
                         f"sibling properties reached the candidate list: {siblings}")

    def test_the_communitys_own_subpage_survives(self):
        """The filter must not throw away the link we actually want."""
        cands = probe._candidate_links(GREYSTAR_HOME, PALM)
        self.assertIn(PALM + "/floorplans", cands)

    def test_vendor_subpaths_are_still_appended(self):
        cands = probe._candidate_links(GREYSTAR_HOME, PALM)
        for sp in probe.VENDOR_SUBPATHS:
            self.assertIn(PALM + sp, cands)

    def test_a_single_property_site_is_unaffected(self):
        """Where the community IS the domain, no sibling can exist and every
        same-host link is its own. This is the majority of the fleet — 452 of the
        667 llm-tier communities are alone on their host."""
        cands = probe._candidate_links(SOLO_HOME, SOLO)
        self.assertIn(SOLO + "/floorplans", cands)
        self.assertIn(SOLO + "/availability", cands)


class ResolveDataUrlTests(unittest.TestCase):
    """Both exits from resolve_data_url, against the real Palm Court shape."""

    def test_the_zero_score_fallback_never_returns_a_sibling(self):
        """The reported path: every Greystar page is JS-rendered, so the homepage
        and all candidates score 0 statically and the fallback fires."""
        with mock.patch("rentdb.fetchers.fetch_static", _fetcher({PALM: GREYSTAR_HOME})):
            got = probe.resolve_data_url({"name": "Palm Court", "url": PALM})
        self.assertNotIn("seven-luxury", got)
        self.assertTrue(got.startswith(PALM),
                        f"resolved outside the community's own url: {got}")

    def test_the_scoring_loop_never_returns_a_sibling(self):
        """The unreported path, and the more dangerous one: a sibling that DOES
        render prices statically out-scores the subject's blank homepage and is
        returned with a confident log line."""
        rich_sibling = "<html>" + " ".join(
            f"<div>$1,{n:03d}/mo 900 sq ft</div>" for n in range(100, 140)) + "</html>"
        pages = {PALM: GREYSTAR_HOME,
                 "https://www.greystar.com" + SEVEN_PATH: rich_sibling}
        with mock.patch("rentdb.fetchers.fetch_static", _fetcher(pages)):
            got = probe.resolve_data_url({"name": "Palm Court", "url": PALM})
        self.assertNotIn("seven-luxury", got)

    def test_a_real_own_subpage_still_wins_on_score(self):
        """The feature this resolver exists for must survive the fix: a genuine
        floor-plan subpage of the SAME property still beats the homepage."""
        own = PALM + "/floorplans"
        rich_own = "<html>" + " ".join(
            f"<div>$1,{n:03d}/mo 900 sq ft</div>" for n in range(200, 240)) + "</html>"
        with mock.patch("rentdb.fetchers.fetch_static",
                        _fetcher({PALM: GREYSTAR_HOME, own: rich_own})):
            got = probe.resolve_data_url({"name": "Palm Court", "url": PALM})
        self.assertEqual(own, got)

    def test_a_single_property_site_still_resolves_to_its_subpage(self):
        own = SOLO + "/floorplans"
        rich = "<html>" + " ".join(
            f"<div>$2,{n:03d}/mo 1200 sq ft</div>" for n in range(100, 140)) + "</html>"
        with mock.patch("rentdb.fetchers.fetch_static",
                        _fetcher({SOLO: SOLO_HOME, own: rich})):
            got = probe.resolve_data_url({"name": "The Retreat", "url": SOLO})
        self.assertEqual(own, got)

    def test_falling_back_to_the_communitys_own_url_is_the_safe_answer(self):
        """When nothing survives the filter, returning the community's OWN url is
        correct — the LLM tier then reads the right property's page, which is
        strictly better than reading the wrong property's."""
        home_only = '<html><body><a href="/seven-luxury-apartments-phoenix-az/p_11833">Seven</a></body></html>'
        with mock.patch("rentdb.fetchers.fetch_static", _fetcher({PALM: home_only})):
            got = probe.resolve_data_url({"name": "Palm Court", "url": PALM})
        self.assertEqual(PALM, got)


if __name__ == "__main__":
    unittest.main()
