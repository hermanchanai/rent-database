"""Static HTTP fetch with TLS-fingerprint impersonation.

Uses curl_cffi to mimic real browser TLS + HTTP/2 behavior. This is what
clears Cloudflare "Just a moment..." challenges and similar bot walls that
plain httpx cannot pass.

Tries browser profiles in order — default: chrome131 then safari17_0. The
ResiHome / ResiBuilt platform specifically blocks chrome131 but accepts
safari17_0, so the fallback is load-bearing.

Example:
    from rentdb.fetchers.static import fetch_static
    result = fetch_static("https://example.com")
    if result.status < 400:
        parse(result.html)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from curl_cffi import requests as cffi_requests

from . import throttle

log = logging.getLogger(__name__)

DEFAULT_PROFILES: tuple[str, ...] = ("chrome131", "safari17_0")
DEFAULT_TIMEOUT: float = 25.0


@dataclass(frozen=True)
class FetchResult:
    """Result of a static fetch."""
    status: int
    final_url: str
    html: str
    bytes_len: int
    profile_used: str


def fetch(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    profiles: tuple[str, ...] = DEFAULT_PROFILES,
    allow_redirects: bool = True,
    verify: bool | str = True,
) -> FetchResult:
    """Fetch a URL, trying browser profiles until one returns status < 400.

    If every profile returns 4xx/5xx, returns the last attempt — callers can
    inspect `result.status` and `result.html` for the error body.

    Network-level errors (DNS, TLS, timeout) propagate as curl_cffi
    exceptions; callers decide whether to catch.

    Args:
        url: URL to fetch.
        timeout: Per-attempt timeout in seconds.
        profiles: Browser profiles to try, in order.
        allow_redirects: Follow HTTP redirects.
        verify: passed to curl_cffi. A CA-bundle PATH for a host whose root curl_cffi's
            built-in bundle lacks (*.myresman.com -> "SSL.com TLS RSA Root CA 2022",); True (its own bundle) everywhere else.

    Returns:
        FetchResult with status, final URL, HTML, bytes, and which profile won.
    """
    if not profiles:
        raise ValueError("profiles must contain at least one entry")

    last_resp = None
    last_profile = profiles[0]
    for profile in profiles:
        # Per-host politeness. A burst against one operator's host reads as a dead
        # site in the ledger: 15 Tricon communities across 7 markets sat at
        # `url_dead` from 2026-07-15 purely because a Wave-4 burst self-429'd.
        throttle.wait_for(url)
        resp = cffi_requests.get(
            url,
            impersonate=profile,
            timeout=timeout,
            allow_redirects=allow_redirects,
            verify=verify,
        )
        throttle.note_response(url, resp.status_code, resp.headers.get("Retry-After"))
        throttle.note_redirect(url, str(resp.url))
        last_resp = resp
        last_profile = profile
        if resp.status_code < 400:
            return FetchResult(
                status=resp.status_code,
                final_url=str(resp.url),
                html=resp.text,
                bytes_len=len(resp.content),
                profile_used=profile,
            )
        log.debug(
            "fetch_static: %s returned %d with profile %s, trying next",
            url,
            resp.status_code,
            profile,
        )

    # All profiles failed; return the last response for the caller to inspect.
    assert last_resp is not None
    return FetchResult(
        status=last_resp.status_code,
        final_url=str(last_resp.url),
        html=last_resp.text,
        bytes_len=len(last_resp.content),
        profile_used=last_profile,
    )
