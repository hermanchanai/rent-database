"""A fetch failure during dispatch must not look like "no vendor matched".

THE DEFECT. `find_adapter()` does ONE shared homepage fetch and decides the vendor from
that HTML. Both failure exits are silent:

    try:
        page = fetch_static(url)
    except Exception:
        return None            # <- transport died
    if page.status >= 400:
        return None            # <- WAF, 5xx, rate limit

`None` is also what it returns when the fetch SUCCEEDED and the page simply is not a
vendor it knows. Those are completely different facts and the caller cannot tell them
apart: the pipeline falls to the LLM tier, or under --skip-llm records `needs_adapter`,
and nothing anywhere says the community was never actually looked at.

This is not hypothetical. On 2026-08-20 a controlled `the pipeline --skip-llm` over 15
known-Razz communities reported "no adapter" 15/15 while every row took ~40s instead of
the usual ~3-7s — the shape of a transport problem — and the run was believed. It cost
two wrong diagnoses before the real cause was found. The same blindness is a candidate
explanation for part of the 144 identical `realpage: property id or public API key not
found` lines the rotation logs carry in three days.

The fix does NOT change the return contract — callers still get a module or None — it
makes the transport failure OBSERVABLE, so a silent demotion leaves a trace.
"""

from __future__ import annotations

import unittest


from rentdb import adapters as A


class _Boom(Exception):
    pass


class DispatchFetchFailureTests(unittest.TestCase):
    URL = "https://www.example-community.com/"

    def _run(self, fetch_impl):
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        fetchers.fetch_static = fetch_impl
        seen = []
        try:
            mod = A.find_adapter({"url": self.URL, "name": "Example"}, log=seen.append)
            return mod, seen
        finally:
            fetchers.fetch_static = real

    def _ok_page(self, html, status=200):
        from rentdb.fetchers.static import FetchResult
        return lambda u, **kw: FetchResult(status=status, final_url=u, html=html,
                                           bytes_len=len(html), profile_used="test")

    def test_transport_failure_is_logged(self):
        """A raised fetch must leave a trace naming the url."""
        def boom(u, **kw):
            raise _Boom("connection reset")
        mod, seen = self._run(boom)
        self.assertIsNone(mod)
        self.assertTrue(seen, "a transport failure during dispatch was completely silent")
        self.assertTrue(any("example-community.com" in m for m in seen), seen)

    def test_http_error_status_is_logged(self):
        """A 403/5xx is a WAF or an outage, not evidence the vendor is unknown."""
        mod, seen = self._run(self._ok_page("<html></html>", status=403))
        self.assertIsNone(mod)
        self.assertTrue(seen, "a 403 during dispatch was completely silent")
        self.assertTrue(any("403" in m for m in seen), seen)

    def test_a_clean_no_match_stays_quiet(self):
        """The common case must NOT become noise: fetch worked, no vendor signal."""
        mod, seen = self._run(self._ok_page("<html><body>a marketing page</body></html>"))
        self.assertIsNone(mod)
        self.assertEqual([], seen,
                         "an ordinary no-vendor-match logged something; that would put a "
                         "line on every LLM-tier community in the fleet")

    def test_a_real_match_stays_quiet_and_still_matches(self):
        mod, seen = self._run(self._ok_page(
            "<html><body>entrata's accessibility statement</body></html>"))
        self.assertEqual("entrata", getattr(mod, "NAME", None))
        self.assertEqual([], seen)

    def test_log_is_optional(self):
        """Every existing caller passes no log; that must keep working."""
        import rentdb.fetchers as fetchers
        real = fetchers.fetch_static
        fetchers.fetch_static = self._ok_page("<html></html>", status=500)
        try:
            self.assertIsNone(A.find_adapter({"url": self.URL}))
        finally:
            fetchers.fetch_static = real


if __name__ == "__main__":
    unittest.main()
