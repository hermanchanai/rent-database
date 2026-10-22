"""When the winning adapter's extract() fails, the NEXT matching adapter must be tried.

THE DEFECT, measured 2026-08-21 over the four rotation logs 08-18..08-21.

`find_adapter()` returns the FIRST module whose vendor signal appears in one shared
homepage fetch, and `the pipeline` sets `result = None` when that module's extract()
errors — falling straight through to the Tier-2 LLM. Nothing ever asks whether a LATER
adapter also matched the same page.

realpage sits ahead of repli360, sightmap and rentcafe in ADAPTERS, and it fails a lot:

    581 `adapter realpage failed: realpage: property id or public API key not found`
    122 realpage successes in the same logs (119 live_data + 3 no_availability)

Re-fetching all 183 distinct communities behind those 581 failures and asking what the
REST of the chain would have said:

    84  genuine RealPage OnlineLeasing — inventory is behind a JS shell, no static read
    55  SHADOWED -> rentcafe
    34  SHADOWED -> sightmap
     3  SHADOWED -> entrata
     6  no vendor at all
     1  fetch failed

**Half of them — 92 of 183 — have a working adapter that is never tried.** They reach
the LLM tail instead: 520 of the 573 resolved outcomes came back `llm:haiku [live_data]`,
so the cost is Haiku spend and ~60-95s per community instead of ~8-20s, not silent data
loss. 52 ended `needs_adapter`, which IS loss.

WHY NOT JUST REORDER. realpage genuinely serves 122 communities; demoting it below
rentcafe/sightmap would hand those to an adapter that does not understand them. The
chain is what fixes the shadowing without trading one misroute for another.
"""

from __future__ import annotations

import unittest


from rentdb import adapters as A


def _page(html, status=200):
    from rentdb.fetchers.static import FetchResult
    return lambda u, **kw: FetchResult(status=status, final_url=u, html=html,
                                       bytes_len=len(html), profile_used="test")


# A page carrying BOTH a realpage signal and a sightmap embed. This is not a contrived
# pairing: SightMap is a RealPage product, so its pages routinely reference a
# realpage.com host, and 34 live communities are in exactly this state.
BOTH = ('<html><body>'
        '<script src="https://cs-cdn.realpage.com/x.js"></script>'
        '<iframe src="https://sightmap.com/embed/abc123"></iframe>'
        '</body></html>')


class FindAdaptersTests(unittest.TestCase):
    URL = "https://www.example-community.com/"

    def _run(self, html, status=200):
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        fetchers.fetch_static = _page(html, status)
        seen = []
        try:
            return A.find_adapters({"url": self.URL, "name": "Ex"}, log=seen.append), seen
        finally:
            fetchers.fetch_static = real

    def test_every_matching_adapter_is_returned_in_priority_order(self):
        mods, _ = self._run(BOTH)
        names = [m.NAME for m in mods]
        self.assertIn("realpage", names)
        self.assertIn("sightmap", names)
        self.assertLess(names.index("realpage"), names.index("sightmap"),
                        "priority order must be preserved")

    def test_find_adapter_still_returns_only_the_winner(self):
        """The old single-module contract is unchanged for every existing caller."""
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        fetchers.fetch_static = _page(BOTH)
        try:
            mod = A.find_adapter({"url": self.URL, "name": "Ex"})
        finally:
            fetchers.fetch_static = real
        self.assertEqual("realpage", mod.NAME)

    def test_a_single_vendor_page_yields_exactly_one_candidate(self):
        mods, _ = self._run('<html><body><iframe src="https://sightmap.com/embed/zq7x4m"></iframe></body></html>')
        self.assertEqual(["sightmap"], [m.NAME for m in mods])

    def test_no_vendor_yields_an_empty_list_not_none(self):
        mods, seen = self._run("<html><body>a marketing page</body></html>")
        self.assertEqual([], mods)
        self.assertEqual([], seen, "a clean no-match must stay quiet")

    def test_transport_failure_yields_empty_and_is_logged(self):
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        def boom(u, **kw):
            raise RuntimeError("connection reset")
        fetchers.fetch_static = boom
        seen = []
        try:
            mods = A.find_adapters({"url": self.URL}, log=seen.append)
        finally:
            fetchers.fetch_static = real
        self.assertEqual([], mods)
        self.assertTrue(any("NO ADAPTER WAS TRIED" in m for m in seen), seen)

    def test_operator_adapters_short_circuit_without_a_fetch(self):
        """ark/amh/avalon match on the URL alone; they must not trigger network I/O."""
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        def boom(u, **kw):
            raise AssertionError("operator match must not fetch")
        fetchers.fetch_static = boom
        try:
            mods = A.find_adapters({"url": "https://communities.amh.com/communities/x"})
        finally:
            fetchers.fetch_static = real
        self.assertEqual(["amh"], [m.NAME for m in mods])


class ProbeChainTests(unittest.TestCase):
    def test_probe_exposes_the_whole_chain_with_its_community(self):
        from rentdb.adapters.probe import find_adapters_with_probe
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        fetchers.fetch_static = _page(BOTH)
        try:
            pairs = find_adapters_with_probe({"url": "https://x.com/", "name": "Ex"})
        finally:
            fetchers.fetch_static = real
        self.assertEqual(["realpage", "sightmap"], [m.NAME for m, _c in pairs])
        for _m, c in pairs:
            self.assertEqual("https://x.com/", c["url"])


if __name__ == "__main__":
    unittest.main()
