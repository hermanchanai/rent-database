"""The chain's control flow, with stand-in adapters so each rule is tested on its own."""
from __future__ import annotations

import pytest

import rentdb.pipeline as P
from rentdb.adapters.base import (STATUS_LIVE, STATUS_NEEDS_ADAPTER, STATUS_NOAVAIL, ExtractResult,
                                  UnitGroup)
from rentdb.fetchers import throttle
from rentdb.replay import SavedPages


class Fake:
    def __init__(self, name, result=None, exc=None):
        self.NAME, self._result, self._exc, self.calls = name, result, exc, 0

    def extract(self, community):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._result


def priced(name, rent=1500, available=2):
    return ExtractResult(units=[UnitGroup(beds=2, sf_min=1000, sf_max=1000, available=available,
                                          rent_min=rent, rent_max=rent)], extractor_used=name)


def catalog(name):
    return ExtractResult(units=[UnitGroup(beds=2, sf_min=1000, sf_max=1000, available=0)],
                         extractor_used=name)


class FakeLLM:
    def __init__(self, result):
        self.result, self.seen = result, []

    def extract(self, community, html):
        self.seen.append(html)
        return self.result


@pytest.fixture
def chain(monkeypatch):
    def install(candidates, fallback=()):
        monkeypatch.setattr(P, "find_adapters_with_probe", lambda c, log=None: [(a, c) for a in candidates])
        monkeypatch.setattr(P, "fallback_candidates", lambda c, tried, log=None: [(a, c) for a in fallback])
    with SavedPages.empty().installed():
        yield install


C = {"name": "Example", "url": "https://example.com/", "market": "TST"}


def test_a_catalog_does_not_stop_the_chain(chain):
    a, b = Fake("a", catalog("a")), Fake("b", priced("b"))
    chain([a, b])
    result = P.read_community(C, sweep=False)
    assert (result.extractor_used, result.status) == ("b", STATUS_LIVE)


def test_an_adapter_that_raises_is_a_miss_not_a_crash(chain):
    chain([Fake("a", exc=RuntimeError("layout changed")), Fake("b", priced("b"))])
    assert P.read_community(C, sweep=False).extractor_used == "b"


def test_the_second_look_runs_only_after_every_direct_candidate_failed(chain):
    late = Fake("late", priced("late"))
    chain([Fake("a", catalog("a"))], fallback=[late])
    assert P.read_community(C, sweep=False).extractor_used == "late"
    assert late.calls == 1


def test_rendered_page_goes_to_the_llm_when_no_reader_can_price_it(chain):
    chain([Fake("a", catalog("a"))])
    llm = FakeLLM(priced("llm_test"))
    result = P.read_community(C, llm=llm, render=lambda url: "<p>2 bed $1,500</p>", sweep=False)
    assert result.extractor_used == "llm_test"
    assert llm.seen == ["<p>2 bed $1,500</p>"]


def test_without_a_reader_or_an_llm_the_community_needs_an_adapter(chain):
    chain([])
    assert P.read_community(C, sweep=False).status == STATUS_NEEDS_ADAPTER


def test_a_spent_rate_limit_budget_is_recorded_as_throttled(chain):
    chain([Fake("a", exc=throttle.DeadlineExceeded("budget spent"))])
    result = P.read_community(C, llm=FakeLLM(priced("llm_test")), render=lambda u: "<p></p>", sweep=False)
    assert result.status == "throttled" and result.units == []


def test_an_affirmed_sell_out_stays_no_availability(chain):
    sold = ExtractResult(units=[], extractor_used="a", status=STATUS_NOAVAIL)
    chain([Fake("a", sold)])
    assert P.read_community(C, sweep=False).status == STATUS_NOAVAIL


def test_finalize_nulls_a_number_that_cannot_be_a_monthly_rent():
    result = P.finalize(priced("x", rent=257_000), C, sweep=False)
    [g] = result.units
    assert (g.rent_min, g.rent_max, g.flag_review) == (None, None, True)


def test_finalize_nulls_llm_availability_above_the_public_record_count_only():
    over = {"public_record_units": 100, **C}
    llm = P.finalize(priced("llm_x", available=999), over, sweep=False)
    assert llm.units[0].available is None and llm.units[0].rent_min == 1500
    adapter = P.finalize(priced("rentcafe", available=999), over, sweep=False)
    assert adapter.units[0].available == 999      # an adapter counts records; it is not nulled


def test_finalize_counts_a_banner_the_extractor_captured_but_did_not_count():
    result = priced("x")
    result.raw_concession_text = "Move in by October 31 and get 6 weeks free!"
    assert P.finalize(result, C, sweep=False).concession_weeks == 6


def test_a_bed_scoped_offer_is_not_counted_community_wide():
    result = priced("x")
    result.raw_concession_text = "Get 6 weeks free on all 2 bedroom homes!"
    finalized = P.finalize(result, C, sweep=False)
    assert finalized.concession_weeks is None and finalized.raw_concession_text
