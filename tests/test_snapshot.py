"""The rollup builder: worked on a hand-built dataset, then checked against the public shape.

The expected numbers in HandBuilt were computed by hand from the definitions in
rentdb/snapshot.py's docstring; the arithmetic is written out next to each one.
"""
from __future__ import annotations

import json

import pytest

from rentdb.normalize import Snapshot
from rentdb.snapshot import build

from .conftest import ROOT

PHX_RECORD = "Maricopa County, AZ property record"
COMMUNITIES = [
    {"id": "a", "market": "PHX", "name": "Alpha", "product": "bfr", "units": 120,
     "units_source": PHX_RECORD, "year_built": 2023, "year_source": PHX_RECORD},
    {"id": "b", "market": "PHX", "name": "Bravo", "product": "mf", "units": None,
     "units_source": None, "year_built": 1985, "year_source": PHX_RECORD},
    {"id": "c", "market": "ATL", "name": "Charlie", "product": "mf"},          # never read
]


def g(beds, sf, lo, hi=None, rents=None):
    return {"beds": beds, "sf_min": sf, "sf_max": sf, "available": 1, "rent_min": lo,
            "rent_max": hi or lo, "unit_rents": rents}


SNAPS = [
    Snapshot("a", "2026-09-01T12:00:00+00:00", "live_data", "knock", 0, None,
             units=[g(3, 1500, 2000, 2200, [2000, 2100, 2200])]),
    Snapshot("a", "2026-09-22T12:00:00+00:00", "live_data", "knock", 4, "4 weeks free",
             units=[g(3, 1500, 2100, 2300), g(4, 2000, 2600)]),
    Snapshot("b", "2026-09-20T12:00:00+00:00", "live_data", "llm_luna", None, None,
             units=[g(1, 700, 1400), g(2, 1000, 1800)]),
    Snapshot("b", "2026-09-23T12:00:00+00:00", "no_availability", "llm_luna", None, None, units=[]),
]


@pytest.fixture(scope="module")
def out():
    return build(COMMUNITIES, SNAPS)


class TestHandBuilt:
    def test_headline(self, out):
        assert out["headline"] == {
            "communities": 3, "markets": 2,
            "floor_plans": 4,                    # a's newest live read (2) + b's (2)
            "communities_with_rents": 2,
            "with_net_effective": 4, "under_concession": 2,   # a's 4 weeks free
            "observations_all": 4, "observations_30d": 4,
            "last_observation": "2026-09-23T12:00:00+00:00",
            "first_observation": "2026-09-01T12:00:00+00:00",
        }

    def test_markets(self, out):
        phx, atl = out["markets"]
        assert phx == {
            "market": "PHX", "communities": 2, "bfr": 1, "mf": 1,
            "with_rents": 2, "floor_plans": 4,
            "median_rent": 1950.0,               # 1400 1800 | 2100 2600
            "median_psf": 1.6,                   # 1.3 1.4 | 1.8 2.0
            "pct_concession": 50.0,
            "avg_discount_pct": 7.7,             # 2100 -> 1938 (7.71%), 2600 -> 2400 (7.69%)
            "last_scraped": "2026-09-22T12:00:00+00:00",
        }
        assert (atl["market"], atl["communities"], atl["with_rents"], atl["median_rent"]) == ("ATL", 1, 0, None)

    def test_vintage_counts_each_community_once(self, out):
        assert out["vintage"] == [
            {"decade": "1980s", "communities": 1, "bfr": 0, "mf": 1, "median_psf": 1.9},
            {"decade": "2020s", "communities": 1, "bfr": 1, "mf": 0, "median_psf": 1.35},
        ]

    def test_beds(self, out):
        assert [(b["beds"], b["median_rent"], b["median_sf"], b["median_psf"], b["pct_concession"])
                for b in out["beds"]] == [(1, 1400, 700, 2.0, 0.0), (2, 1800, 1000, 1.8, 0.0),
                                          (3, 2100, 1500, 1.4, 100.0), (4, 2600, 2000, 1.3, 100.0)]

    def test_asset_class(self, out):
        assert {(a["product"], a["floor_plans"], a["median_rent"], a["median_sf"]) for a in out["asset_class"]} == {
            ("bfr", 2, 2350, 1750), ("mf", 2, 1600, 850)}

    def test_extraction_folds_every_model_into_one_llm_row(self, out):
        assert out["extraction"] == [{"engine": "knock", "observations": 2},
                                     {"engine": "LLM (Luna)", "observations": 1}]

    def test_activity_weeks_start_on_monday(self, out):
        assert [(a["week"], a["observations"]) for a in out["activity"]] == [
            ("2026-08-31", 1), ("2026-09-14", 1), ("2026-09-21", 1)]   # 09-23 was not live

    def test_revisit_and_rent_moves(self, out):
        assert out["revisit"] == {"observed": 2, "twice_plus": 1, "thrice_plus": 0, "avg_observations": 1.5}
        # a: median 2000 on 09-01 -> 2350 on 09-22, 21 days apart; b was read live once
        assert out["rent_moves"] == {"communities": 1, "moved_up": 1, "moved_down": 0,
                                     "unchanged": 0, "avg_span_days": 21}

    def test_sample_shows_read_communities_with_public_record_facts_only(self, out):
        assert out["samples"]["ATL"] == []                        # Charlie was never read
        assert out["samples"]["PHX"] == [
            {"name": "Alpha", "product": "BFR", "units": 120, "units_source": PHX_RECORD,
             "year_built": 2023, "year_source": PHX_RECORD, "last_seen": "2026-09-22", "avg_rent": 2350},
            {"name": "Bravo", "product": "MF", "units": None, "units_source": None,
             "year_built": 1985, "year_source": PHX_RECORD, "last_seen": "2026-09-20", "avg_rent": 1600},
        ]

    def test_build_for_rent_communities_lead_a_short_sample(self):
        assert [r["name"] for r in build(COMMUNITIES, SNAPS, sample_per_market=1)["samples"]["PHX"]] == ["Alpha"]

    def test_a_market_without_a_display_name_fails_loudly(self):
        with pytest.raises(ValueError, match="ZZZ"):
            build([{"id": "z", "market": "ZZZ", "name": "Z"}], [])


# --- shape parity with the published file -----------------------------------------------------
def shape(x):
    if isinstance(x, dict):
        return {k: shape(v) for k, v in x.items()}
    if isinstance(x, list):
        return [shape(x[0])] if x else []
    if isinstance(x, bool):
        return "bool"
    if isinstance(x, (int, float)):
        return "number"
    return "null" if x is None else type(x).__name__


def assert_compatible(a, b, path="$"):
    if "null" in (a, b) or a == [] or b == []:
        return
    assert type(a) is type(b), f"{path}: {a!r} vs {b!r}"
    if isinstance(a, dict):
        if path in ("$.samples", "$.metros"):           # keyed by market: compare the values
            for va in a.values():
                for vb in b.values():
                    assert_compatible(va, vb, path + ".*")
            return
        assert set(a) == set(b), f"{path}: keys differ {set(a) ^ set(b)}"
        for k in a:
            assert_compatible(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        assert_compatible(a[0], b[0], path + "[0]")
    else:
        assert a == b, f"{path}: {a} vs {b}"


def test_demo_output_has_the_published_shape(demo_output):
    _out_dir, payload = demo_output
    published = json.loads((ROOT / "sample" / "rent-database.json").read_text())
    assert_compatible(shape(payload), shape(published))
    assert payload["headline"]["floor_plans"] > 0


SAMPLE_ROW_FIELDS = {"name", "product", "units", "units_source", "year_built", "year_source",
                     "last_seen", "avg_rent"}


def test_sample_rows_carry_only_the_public_fields(demo_output):
    """The only per-community rows are the bounded sample, and each carries exactly these
    eight fields: unit count and year with their public-record source, and our own read."""
    _out_dir, payload = demo_output
    published = json.loads((ROOT / "sample" / "rent-database.json").read_text())
    for doc in (published, payload):
        for rows in doc["samples"].values():
            assert all(set(r) == SAMPLE_ROW_FIELDS for r in rows)


def test_no_unit_totals_anywhere(demo_output):
    """Unit totals would have to sum a vendor-seeded count, so neither file carries one;
    per-community unit counts appear only in the sample, from public records."""
    _out_dir, payload = demo_output
    published = json.loads((ROOT / "sample" / "rent-database.json").read_text())
    for doc in (published, payload):
        assert "units" not in doc["headline"]
        assert all("units" not in m for m in doc["markets"])


def test_build_years_from_the_vendor_registry_are_withheld():
    published = json.loads((ROOT / "sample" / "rent-database.json").read_text())
    rows = [r for rs in published["samples"].values() for r in rs]
    assert not any("third-party" in (r["year_source"] or "") for r in rows)
    assert all((r["year_built"] is None) == (r["year_source"] is None) for r in rows)
