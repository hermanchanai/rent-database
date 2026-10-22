"""Platform parsers over single saved pages (trimmed to what each parser reads)."""
from __future__ import annotations

from bs4 import BeautifulSoup

from rentdb.adapters import appfolio, entrata, knock, razz, realpage, repli360
from rentdb.adapters.base import (STATUS_LIVE, STATUS_NOAVAIL, ExtractResult, classify_status,
                                  result_is_usable)

from .conftest import page, page_json

# --- Razz: an SSR payload that mixes leasable units with leased ones and plan rows ------------
# The six cards the models page rendered, plus the two hidden behind "1 Other Units".
RAZZ_AVAILABLE = {
    "A-103": (1, 650, 1445), "C-113": (0, 350, 1095), "F-139": (2, 825, 1940),
    "I-156": (2, 900, 1755), "I-162": (2, 900, 1755), "J-169": (0, 500, 1170),
    "J-179": (0, 500, 975), "L-302": (0, 405, 1160),
}


def test_razz_counts_only_units_with_an_availability_date():
    units = razz.available_units(page("razz-models-page", "page1.html"))
    assert {u["id"]: (u["beds"], u["sqft"]["min"], u["rent"]["min"]) for u in units} == RAZZ_AVAILABLE


def test_razz_groups_carry_every_member_rent():
    groups = {(g.beds, g.sf_min): g for g in razz.parse_models_page(page("razz-models-page", "page1.html"))}
    assert sorted(groups) == [(0, 350), (0, 405), (0, 500), (1, 650), (2, 825), (2, 900)]
    studio = groups[(0, 500)]
    assert (studio.available, studio.rent_min, studio.rent_max) == (2, 975, 1170)
    assert sorted(studio.unit_rents) == [975, 1170]


# --- Knock: a feed with layouts but no units is a catalog, not live data ----------------------
def test_knock_layouts_without_units_are_a_dry_catalog():
    data = page_json("knock-dry-catalog", "page1.json")["units_data"]
    assert not data.get("units")
    groups = knock._add_advertised_layouts(knock._group_units([]), data.get("layouts") or [])
    assert groups
    assert not any(g.rent_min or g.rent_max or g.available or g.coming_soon for g in groups)
    result = ExtractResult(units=groups, extractor_used="knock")
    assert not result_is_usable(result)          # the chain keeps looking
    assert classify_status(result) == STATUS_NOAVAIL


# --- Entrata: base rent vs the fee-inclusive total --------------------------------------------
def _entrata(slug):
    cards = BeautifulSoup(page(slug, "page1.html"), "html.parser").select(".fp-card")
    return {(g.beds, g.sf_min): g for g in entrata._parse_fp_cards(cards)}


def test_entrata_fee_transparency_card_stores_base_as_rent_and_total_as_all_in():
    g = _entrata("entrata-fee-transparency-cards")
    got = {k: (v.available, v.rent_min, v.rent_max) for k, v in g.items()}
    assert got[(3, 2245)] == (2, 2802, 2917)
    assert got[(4, 2118)] == (1, 2571, 2571)
    assert got[(4, 2522)] == (5, 2840, 2840)
    assert got[(5, 2510)] == (2, 2581, 2841)
    assert got[(5, 2904)] == (2, 2934, 2934)
    three = g[(3, 2245)]
    assert (three.rent_allin_min, three.rent_allin_max) == (2987, 3102)
    assert three.unit_rents == [2802, 2917]


def test_entrata_single_price_card_has_no_all_in():
    g = _entrata("entrata-single-price-cards")
    assert g[(3, 1381)].rent_min == 2270
    assert g[(3, 1381)].rent_allin_min is None


# --- RealPage: a plan the units feed omits is added back, unpriced ----------------------------
def test_realpage_unlisted_plan_is_merged_without_a_price():
    plans = page_json("realpage-unlisted-plan", "page1.json")["response"]["floorplans"]
    units = page_json("realpage-unlisted-plan", "page2.json")["response"]["units"]
    merged = realpage._merge_unlisted_floorplans(realpage._group_units(units), plans)
    by_beds = {g.beds: g for g in merged}
    assert sorted(by_beds) == [1, 2, 3]
    one = by_beds[1]
    assert (one.sf_min, one.available, one.coming_soon, one.rent_min, one.unit_rents) == (841, 0, 0, None, None)
    assert one.flag_review
    assert (by_beds[2].available, by_beds[2].rent_min, by_beds[2].sf_min) == (9, 1451, 1068)
    assert (by_beds[3].available, by_beds[3].rent_min, by_beds[3].sf_min) == (9, 1657, 1240)


# --- AppFolio: one card per listed unit -------------------------------------------------------
def test_appfolio_listing_cards():
    groups = {g.beds: g for g in appfolio.parse_listings(page("appfolio-listings", "page1.html"))}
    assert sorted(groups) == [0, 1, 2]
    assert [(groups[b].rent_min, groups[b].rent_max, groups[b].available) for b in (0, 1, 2)] == [
        (699, 839, 3), (799, 1024, 7), (1099, 1099, 2)]
    assert groups[1].unit_rents == [799, 799, 828, 839, 879, 879, 1024]
    assert groups[0].baths == 1
    assert classify_status(ExtractResult(units=list(groups.values()), extractor_used="appfolio")) == STATUS_LIVE


# --- Repli360: plan cards price a floor, not a unit list --------------------------------------
def test_repli360_attribute_cards():
    groups = repli360._parse_inventory(page("repli360-attribute-cards", "page1.html"))

    def bed(b):
        gs = [g for g in groups if g.beds == b]
        return sum(g.available for g in gs), min(g.rent_min for g in gs), max(g.rent_max for g in gs)

    assert [bed(b) for b in (0, 1, 2, 3)] == [(2, 1223, 1223), (10, 1325, 1639), (5, 1750, 1989), (2, 2100, 2100)]
    assert all(g.unit_rents is None for g in groups)
