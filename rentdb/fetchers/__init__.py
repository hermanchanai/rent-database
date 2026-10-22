"""Fetch layer: a static fetch with a browser-like TLS fingerprint, plus per-host throttling.

Production also rendered JavaScript-only pages in a headless browser. That fetcher is not in
this repo; `rentdb.pipeline` takes the rendered HTML as an injected callable instead.
"""

from . import throttle
from .static import FetchResult, fetch as fetch_static

__all__ = ["FetchResult", "fetch_static", "throttle"]
