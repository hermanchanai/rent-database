"""Dated net-effective rent records. Expected values are worked by hand from the rules in
rentdb/normalize.py's docstring, not taken from the code's output."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from rentdb.adapters.base import STATUS_LIVE, STATUS_NOAVAIL, ExtractResult, UnitGroup
from rentdb.normalize import Snapshot, market_rent, net_effective, records, round_half_up


def test_net_effective_spreads_free_weeks_over_a_year():
    assert net_effective(1500, 6) == 1327        # 1500 * 46/52 = 1326.92
    assert net_effective(1500, 4) == 1385        # one month free = 4 weeks: 1500 * 48/52 = 1384.6
    assert net_effective(1500, None) == 1500
    assert net_effective(1500, 0) == 1500
    assert net_effective(None, 6) is None


def test_rounding_is_half_up_like_the_database_not_bankers():
    assert net_effective(1001, 26) == 501         # 500.5 -> 501 (Python's round() gives 500)
    assert round_half_up(2.5) == 3 and round_half_up(3.5) == 4


def test_market_rent_is_the_mean_of_the_lowest_five_observed():
    assert market_rent([1340] * 7 + [1904], 1340) == 1340
    assert market_rent([1904, 1500, 1400, 1450, 1600, 1700], 1400) == 1530   # 7650 / 5
    assert market_rent([1395, 1415, 1450], 1395) == 1420                      # fewer than five
    assert market_rent(None, 1340) == 1340                                    # range only -> low end
    assert market_rent([], 1340) == 1340


def _snap(units, weeks=None, status=STATUS_LIVE):
    return Snapshot(community_id="c1", scraped_at="2026-09-21T16:00:00+00:00", status=status,
                    extractor="test", concession_weeks=weeks, raw_concession_text="6 weeks free",
                    units=units)


def test_records_carry_date_concession_and_both_rents():
    [r] = records(_snap([{"beds": 2, "sf_min": 1000, "sf_max": 1000, "available": 3,
                          "rent_min": 1500, "rent_max": 1700, "unit_rents": [1500, 1600, 1700]}],
                         weeks=6))
    assert (r.observed_on, r.beds, r.rent_min, r.rent_max) == ("2026-09-21", 2, 1500, 1700)
    assert (r.ner_min, r.ner_max) == (1327, 1504)                             # 1700*46/52 = 1503.8
    assert r.market_rent == 1600
    assert r.concession_weeks == 6 and r.concession_text == "6 weeks free"


def test_a_sentinel_endpoint_is_recovered_from_members_or_dropped():
    recovered, unpriced = records(_snap([
        {"beds": 1, "rent_min": -1, "rent_max": -1, "unit_rents": [0, 1450, 1500]},
        {"beds": 2, "rent_min": 0, "rent_max": 0, "unit_rents": None}]))
    assert (recovered.rent_min, recovered.rent_max, recovered.unit_rents) == (1450, 1500, (1450, 1500))
    assert (unpriced.rent_min, unpriced.ner_min, unpriced.market_rent) == (None, None, None)


def test_only_a_live_read_carries_rents():
    assert records(_snap([{"beds": 1, "rent_min": 1500}], status=STATUS_NOAVAIL)) == []


def test_snapshot_from_result_requires_an_aware_timestamp():
    result = ExtractResult(units=[UnitGroup(beds=1, sf_min=700, sf_max=700, available=1,
                                            rent_min=1400, rent_max=1400)],
                           extractor_used="x", status=STATUS_LIVE, concession_weeks=4)
    snap = Snapshot.from_result("c1", result, datetime(2026, 9, 21, 12, tzinfo=timezone.utc))
    assert snap.scraped_at == "2026-09-21T12:00:00+00:00"
    assert records(snap)[0].ner_min == 1292                                     # 1400*48/52 = 1292.3
    with pytest.raises(ValueError):
        Snapshot.from_result("c1", result, datetime(2026, 9, 21, 12))
