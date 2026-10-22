"""Per-host politeness for the fetch layer.

WHY THIS EXISTS: on 2026-07-15/16 the Wave-4 scrape hit triconresidential.com ~16
times in quick succession and got HTTP 429 on every one. Those 15 communities across
7 markets have been sitting at `url_dead` ever since — not blocked, rate-limited, and
nobody re-ran them. The loss was self-inflicted and silent: a burst against one
operator's host looks exactly like a dead site in the ledger, and a seed list sorted
by market groups an operator's communities together, which is exactly that shape.

Recovering them on 2026-07-28 worked (13 of 15 back to live_data) but also corrected
the diagnosis: Tricon is not merely "throttled that one day". It rate-limits under
SUSTAINED access — that recovery run still took 80 × HTTP 429 at a flat 3s interval,
because a fixed cooldown expires and the very next request re-trips the limit. One
community burned 30 minutes that way. Hence the escalation below: a host that keeps
refusing gets a longer steady-state interval, not just another identical nap.

Design notes:
  - Keyed on registrable HOST, not URL. triconresidential.com 301s to triconhomes.com,
    so the redirect target must be stamped too or the throttle only paces half the
    requests (call `note_redirect` with the final URL).
  - Process-local and thread-safe. There is no cross-process coordination; the weekly
    run is a single process per manifest, which is the unit that bursts.
  - A 429/503 arms a cooldown for that host, honouring Retry-After when the server
    sends one. Cooldowns are capped so a hostile header cannot stall a run.
  - 🔴 That per-cooldown cap is NOT a per-caller cap, and conflating the two cost the
    fleet four days of scrapes (2026-08-04 → 08-07). `MAX_COOLDOWN` bounds ONE wait;
    nothing bounded how MANY. A community makes ~27 fetches (vendor probing, adapter,
    resolve_data_url, browser render, concession sweep) and a host that 429s every one
    of them re-arms a fresh 300s cooldown each time — 27 × 300s = the 8,234s that
    triconresidential.com burned per community, eating whole manifests alive. Use
    `deadline()` around a unit of work to bound the TOTAL. Pinned by
    tools/test_throttle_deadline.py.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_MIN_INTERVAL = 3.0     # seconds between requests to the same host
DEFAULT_COOLDOWN = 60.0        # applied on a 429/503 with no usable Retry-After
MAX_COOLDOWN = 300.0           # ceiling: never let one host stall a run indefinitely

_lock = threading.Lock()
_last_request: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}
_consecutive_429: dict[str, int] = {}
_host_interval: dict[str, float] = {}   # adaptive, per host

# A host that keeps 429-ing needs a LONGER steady-state interval, not just another
# fixed cooldown. Measured 2026-07-28 on the Tricon recovery run: at a flat 3s
# interval with a flat 60s cooldown, triconresidential.com returned 429 EIGHTY
# times and one community took 30 minutes — the cooldown expired and the very next
# request tripped it again. Escalating the host's own interval converges instead.
_INTERVAL_ESCALATION = 2.0
MAX_HOST_INTERVAL = 30.0


def host_of(url: str) -> str:
    """Registrable-ish host key: lowercased, www-stripped. '' for unparseable input."""
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


class DeadlineExceeded(RuntimeError):
    """A `deadline()` budget ran out while waiting on a throttled host.

    Raised, not returned: the caller's remaining work for this unit is all against
    the same rate-limited host, so continuing only burns more of the manifest.
    """


# Thread-local on purpose. the pipeline is single-threaded per manifest, but scout.py
# fans out over a ThreadPoolExecutor — a budget armed on one thread must not bound
# another's unrelated fetch.
_scope = threading.local()


def exhausted(message: str) -> DeadlineExceeded:
    """Latch this thread's spent budget and return the exception to raise.

    Adapters catch broad Exception inside extract(), so a raised DeadlineExceeded can be
    swallowed into an ordinary scrape_error -- or into a partial read that looks complete.
    The latch survives that: the pipeline asks `spent()` after the adapter chain, whatever
    the adapter did with the exception. Every raise of DeadlineExceeded goes through here.
    """
    _scope.spent = message
    return DeadlineExceeded(message)


def spent() -> str | None:
    """Why this thread's current budget ran out, or None while it has not."""
    return getattr(_scope, "spent", None)


def _remaining() -> float | None:
    """Seconds left in the enclosing deadline, or None when there is no deadline."""
    end = getattr(_scope, "end", None)
    return None if end is None else end - time.monotonic()


def arm_deadline(seconds: float | None) -> None:
    """Start a fresh budget on this thread, replacing any previous one.

    For a long loop body that would be painful to indent under `deadline()`.
    Replacement is the safety property: an iteration that escapes without calling
    `clear_deadline` cannot leave the NEXT iteration with an exhausted budget,
    because arming overwrites rather than nests.
    """
    _scope.end = None if seconds is None else time.monotonic() + max(0.0, seconds)
    _scope.spent = None


def clear_deadline() -> None:
    """Drop any armed budget on this thread."""
    _scope.end = None
    _scope.spent = None


@contextmanager
def deadline(seconds: float | None):
    """Bound the TOTAL time `wait_for` may sleep inside this scope.

    Wrap one unit of work whose starvation you care about — for the weekly scrape
    that is one community. Pass None to opt out (restored on exit either way, so a
    fired deadline never leaks into the next community).
    """
    prev = getattr(_scope, "end", None)
    prev_spent = spent()
    _scope.end = None if seconds is None else time.monotonic() + max(0.0, seconds)
    _scope.spent = None
    try:
        yield
    finally:
        _scope.end = prev
        # A budget spent INSIDE still means the enclosing read came up short.
        _scope.spent = prev_spent or spent()


def wait_for(url: str, min_interval: float = DEFAULT_MIN_INTERVAL) -> float:
    """Block until it is polite to hit this URL's host. Returns seconds slept.

    Raises DeadlineExceeded if an enclosing `deadline()` runs out. Without one the
    behaviour is exactly as before — other callers are unaffected.
    """
    host = host_of(url)
    if not host:
        return 0.0
    slept = 0.0
    while True:
        with _lock:
            now = time.monotonic()
            cool = _cooldown_until.get(host, 0.0)
            last = _last_request.get(host, 0.0)
            effective = max(min_interval, _host_interval.get(host, 0.0))
            wait = max(cool - now, (last + effective) - now, 0.0)
            if wait <= 0:
                _last_request[host] = now
                return slept
        remaining = _remaining()
        if remaining is not None:
            if remaining <= 0:
                raise exhausted(
                    f"wait budget exhausted on host {host!r} after {slept:.0f}s "
                    f"(it still wanted {wait:.0f}s more)")
            # Clamp so the sleep cannot overshoot the budget it is being measured against.
            wait = min(wait, remaining)
        # sleep OUTSIDE the lock so other hosts are not serialised behind this one
        chunk = min(wait, MAX_COOLDOWN)
        time.sleep(chunk)
        slept += chunk


def note_response(url: str, status: int, retry_after: str | None = None) -> None:
    """Record the outcome; arm a cooldown on a rate-limit response."""
    host = host_of(url)
    # Coerce: a caller passing "429" as a string would otherwise silently skip the
    # cooldown, which is the exact failure this module exists to prevent.
    try:
        code = int(status)
    except (TypeError, ValueError):
        return
    if not host:
        return
    if code < 400:
        # a clean response means the current pace is tolerated; stop escalating
        with _lock:
            _consecutive_429.pop(host, None)
        return
    if code not in (429, 503):
        return
    delay = DEFAULT_COOLDOWN
    if retry_after:
        delay = _parse_retry_after(str(retry_after), delay)
    with _lock:
        n = _consecutive_429.get(host, 0) + 1
        _consecutive_429[host] = n
        # escalate the cooldown AND the host's steady-state interval, so the next
        # request after the cooldown does not immediately re-trip the limit
        delay = max(0.0, min(delay * (_INTERVAL_ESCALATION ** (n - 1)), MAX_COOLDOWN))
        _cooldown_until[host] = time.monotonic() + delay
        _host_interval[host] = min(
            max(_host_interval.get(host, DEFAULT_MIN_INTERVAL) * _INTERVAL_ESCALATION,
                DEFAULT_MIN_INTERVAL * _INTERVAL_ESCALATION),
            MAX_HOST_INTERVAL)
        new_iv = _host_interval[host]
    log.warning("throttle: %s returned %s (#%d) — backing off %.0fs, "
                "host interval now %.0fs", host, code, n, delay, new_iv)


def _parse_retry_after(value: str, default: float) -> float:
    """Retry-After is delta-seconds OR an HTTP-date (RFC 9110). Ignoring the date
    form made us retry too early and risk extending the rate limit."""
    v = value.strip()
    m = re.match(r"^(\d+)$", v)
    if m:
        return float(m.group(1))
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        if dt is None:
            return default
        from datetime import datetime, timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001
        return default


def note_redirect(original_url: str, final_url: str) -> None:
    """Link a redirect alias to its target and SHARE all adaptive state both ways.

    triconresidential.com 301s to triconhomes.com, and a real manifest carries both
    forms (10 direct + 5 redirecting, measured 2026-07-28). Copying only the cooldown
    let each hostname learn its own interval and consecutive-429 count, so the two
    URL forms rate-limited the same physical server independently and the escalation
    never converged. A cross-model review finding.
    """
    a, b = host_of(original_url), host_of(final_url)
    if not b or a == b:
        return
    with _lock:
        now = time.monotonic()
        _last_request[b] = now
        _last_request[a] = now
        worst_cool = max(_cooldown_until.get(a, 0.0), _cooldown_until.get(b, 0.0))
        if worst_cool:
            _cooldown_until[a] = _cooldown_until[b] = worst_cool
        worst_iv = max(_host_interval.get(a, 0.0), _host_interval.get(b, 0.0))
        if worst_iv:
            _host_interval[a] = _host_interval[b] = worst_iv
        worst_n = max(_consecutive_429.get(a, 0), _consecutive_429.get(b, 0))
        if worst_n:
            _consecutive_429[a] = _consecutive_429[b] = worst_n


def reset() -> None:
    """Clear all state (tests only)."""
    with _lock:
        _last_request.clear()
        _cooldown_until.clear()
        _consecutive_429.clear()
        _host_interval.clear()
