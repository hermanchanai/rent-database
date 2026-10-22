"""The full read chain over saved operator pages, replayed offline.

Each expected value was read off the page by hand when the page was saved (the source
project's labelled truth), not taken from the adapter's output. The saved pages are trimmed
to the elements the read needs; the trim was accepted only when the read stayed identical.
"""
from __future__ import annotations

import pytest

from rentdb.adapters.base import STATUS_LIVE, STATUS_URL_DEAD
from rentdb.pipeline import read_community
from rentdb.replay import SavedPages

from .conftest import case_dir


def read(slug: str):
    saved = SavedPages(case_dir(slug))
    with saved.installed():
        result = read_community(saved.community, logf=lambda _m: None)
    return result, saved


def by_plan(result):
    return {(g.beds, g.sf_min): (g.available, g.coming_soon, g.rent_min, g.rent_max, g.unit_rents)
            for g in result.units}


def test_apartments247_plans_and_a_count_that_is_only_a_floor():
    result, _ = read("apartments247-belvedere")
    assert (result.extractor_used, result.status) == ("apartments247", STATUS_LIVE)
    g = {(u.beds, u.sf_min): u for u in result.units}
    a = g[(3, 1330)]
    assert (a.available, a.rent_min, a.rent_max, a.unit_rents) == (1, 3225, 3225, [3225])
    p = g[(3, 1400)]
    assert (p.available, p.rent_min, p.rent_max, p.unit_rents) == (3, 3000, 3275, [3000, 3250, 3275])
    assert p.flag_review                 # 3 shown == the widget's display cap: a floor, not a count
    one_bed = [(u.available, u.rent_min) for u in result.units if u.beds == 1]
    assert one_bed == [(0, None)]        # "call for details": listed plan, no price


def test_firstkey_homes_grouped_by_plan():
    result, _ = read("firstkey-meadow-springs")
    assert result.extractor_used == "firstkey"
    assert by_plan(result) == {(3, 1593): (0, 1, 2045, 2045, [2045]),
                               (3, 2250): (1, 0, 2000, 2000, [2000]),
                               (4, 2448): (1, 1, 2305, 2575, [2305, 2575])}


def test_sightmap_reads_the_communitys_own_feed_not_a_neighbours_embed():
    result, saved = read("sightmap-channel-point")
    assert result.scrape_error is None
    assert {g.beds: (g.available, g.rent_min, g.rent_max) for g in result.units} == {
        1: (11, 3020, 3235), 2: (6, 3675, 3835)}
    assert "https://sightmap.com/embed/8ywkl5q9vlx" in saved.requested


def test_rently_feed_found_through_a_wrapped_link():
    result, _ = read("rently-lakeside")
    assert result.extractor_used == "rently"
    assert by_plan(result)[(3, 1580)] == (1, 0, 1850, 1850, [1850])


def test_rently_a_sizeless_typo_price_is_dropped_and_the_home_kept():
    result, _ = read("rently-1801-townhomes")
    g = by_plan(result)
    assert g[(3, None)] == (1, 0, None, None, None)      # feed said $23,058 with size 0
    assert g[(3, 1738)] == (1, 0, 2529, 2529, [2529])
    assert result.flag_review


SPUR_PLANS = {  # plan -> (beds, baths, sf, available, unit_rents)
    "1X1A": (1, 1.0, 836, 2, [1373, 1433]),
    "Carriage": (1, 1.0, 897, 1, [1675]),
    "2X2C": (2, 2.0, 1123, 2, [1581, 1596]),
    "3X2E": (3, 2.0, 1390, 2, [1920, 1990]),
    "1X1B": (1, 1.0, 898, 1, [1550]),
    "2X2D": (2, 2.0, 1219, 1, [1687]),
}


@pytest.mark.parametrize("plan", sorted(SPUR_PLANS))
def test_marketplace_listing_keeps_the_low_end_of_each_units_lease_term_range(plan):
    result, _ = read("rentcafe-marketplace-spur")
    got = {g.plan_name: g for g in result.units}
    assert set(got) == set(SPUR_PLANS)
    beds, baths, sf, avail, rents = SPUR_PLANS[plan]
    g = got[plan]
    assert (g.beds, g.baths, g.sf_min, g.sf_max, g.available, g.unit_rents,
            g.rent_min, g.rent_max, g.rent_type, g.rent_basis) == (
        beds, baths, sf, sf, avail, rents, min(rents), max(rents), "base", "actual")


def test_a_removed_listing_that_redirects_to_a_city_search_is_url_dead():
    result, _ = read("rentcafe-marketplace-removed-listing")
    assert (result.status, result.units) == (STATUS_URL_DEAD, [])


def test_replay_answers_404_for_anything_not_saved():
    saved = SavedPages(case_dir("firstkey-meadow-springs"))
    assert saved.fetch("https://example.com/not-saved").status == 404
