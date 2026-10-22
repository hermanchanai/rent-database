"""Replay saved operator pages through the read chain, offline.

A saved case is a directory holding `case.json` plus the page files it names:

    {
      "community": {"id": "...", "market": "PHX", "name": "...", "url": "https://..."},
      "scraped_at": "2026-09-01T18:00:00+00:00",
      "pages": [
        {"url": "https://example.com/floorplans", "file": "floorplans.html"},
        {"contains": "/api/v1/units", "file": "units.json"},
        {"url": "https://example.com/gone", "status": 404}
      ]
    }

`SavedPages.installed()` swaps the static fetcher, and the handful of adapters that call
curl_cffi directly, for a lookup in that list. Any URL not in the list answers HTTP 404, so a
replay can never reach the network; the test suite also runs with sockets disabled.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import rentdb.fetchers as fetchers
from rentdb.fetchers.static import FetchResult

# Adapters that call curl_cffi directly (a JSON API that needs a specific request shape).
_DIRECT_CURL_MODULES = ("amh", "knock", "realpage", "repli360")


@dataclass
class _Response:
    status_code: int
    text: str
    url: str
    headers: dict

    def json(self):
        return json.loads(self.text)


class SavedPages:
    def __init__(self, case_dir: Path | None, case: dict | None = None):
        self.dir = Path(case_dir) if case_dir is not None else None
        self.case = case if case is not None else json.loads((self.dir / "case.json").read_text())
        self.requested: list[str] = []

    @classmethod
    def empty(cls) -> "SavedPages":
        """Every URL answers 404: an offline run where only injected tiers can read anything."""
        return cls(None, {"community": {}, "pages": []})

    @property
    def community(self) -> dict:
        return dict(self.case["community"])

    def _lookup(self, url: str) -> tuple[int, str, str]:
        self.requested.append(url)
        for p in self.case["pages"]:
            hit = (p.get("url") == url) or (p.get("contains") and p["contains"] in url)
            if not hit:
                continue
            body = (self.dir / p["file"]).read_text(encoding="utf-8") if p.get("file") else ""
            return p.get("status", 200), body, p.get("final_url", url)
        return 404, "", url

    def fetch(self, url: str, **_kw) -> FetchResult:
        status, body, final = self._lookup(url)
        return FetchResult(status=status, final_url=final, html=body,
                           bytes_len=len(body.encode()), profile_used="saved")

    def _curl(self, url: str, *_a, **_kw) -> _Response:
        status, body, final = self._lookup(url)
        return _Response(status_code=status, text=body, url=final, headers={})

    @contextmanager
    def installed(self):
        import importlib

        class _FakeCurl:
            get = staticmethod(self._curl)
            post = staticmethod(self._curl)

        mods = [importlib.import_module(f"rentdb.adapters.{m}") for m in _DIRECT_CURL_MODULES]
        saved_fetch = fetchers.fetch_static
        saved_cr = [(m, m.cr) for m in mods]
        fetchers.fetch_static = self.fetch
        for m in mods:
            m.cr = _FakeCurl
        try:
            yield self
        finally:
            fetchers.fetch_static = saved_fetch
            for m, cr in saved_cr:
                m.cr = cr
