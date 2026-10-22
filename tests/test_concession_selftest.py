"""The concession scanner's own table of banner texts and the week counts they must yield."""
from __future__ import annotations

import pytest

from rentdb.concessions import _SELFTEST, scan_concession


@pytest.mark.parametrize("text,want", _SELFTEST, ids=[t[:50] for t, _ in _SELFTEST])
def test_banner(text, want):
    assert scan_concession(text)[0] == want


@pytest.mark.parametrize("rent", [None, 2300])
def test_dollars_off_never_become_weeks_but_the_banner_is_kept(rent):
    weeks, raw = scan_concession("Up to $500 First Month's Rent on Select Homes*", monthly_rent=rent)
    assert weeks is None and raw
