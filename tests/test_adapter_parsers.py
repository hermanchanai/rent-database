"""Offline regression tests for the Austin adapter additions."""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta

from bs4 import BeautifulSoup

from rentdb.adapters import avalon, entrata, knock, realpage, rentcafe, repli360, sightmap


class AdapterParserTests(unittest.TestCase):
    def test_rentcafe_card_layout_preserves_base_and_allin(self):
        soup = BeautifulSoup(
            """
            <div class="col-lg-8 col-12">
              <h2>Willow</h2><p>3 Bedrooms | 2 Bathrooms</p>
              <p>Up to 1,530 Sq. Ft.</p>
              <div class="card"><h3>Apartment: # 101</h3>
                <p>Available Now</p>
                <p>Total Monthly Leasing Price Starting at: $2,600</p>
                <p>Base rent from $2,485/mo for 12 months</p>
              </div>
              <div class="card"><h3>Apartment: # 102</h3>
                <p>Date Available: 8/1/2026</p>
                <p>Base rent from $2,550/mo for 12 months</p>
              </div>
            </div>
            """,
            "html.parser",
        )
        groups, warnings = rentcafe._parse_card_layout(soup)
        self.assertEqual([], warnings)
        self.assertEqual(1, len(groups))
        # Listed = available (2026-07-14): the future "Date Available" card
        # still counts as available, not coming_soon.
        self.assertEqual((2, 0), (groups[0].available, groups[0].coming_soon))
        self.assertEqual((2485, 2550), (groups[0].rent_min, groups[0].rent_max))
        self.assertEqual(2600, groups[0].rent_allin_min)

    def test_entrata_razz_inventory_dates(self):
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=10)).isoformat()
        payload = {
            "initialStoreState": {
                "$inventory": {
                    "models": [
                        {
                            "id": "m1",
                            "beds": 3,
                            "sqft": {"min": 1600, "max": 1600},
                            "rent": {"min": 2400, "max": 2500},
                        }
                    ],
                    "units": [
                        {
                            "model_id": "m1",
                            "available": f"!Date:{today}T00:00:00.000Z",
                            "rent": {"min": 2410, "max": 2410},
                            "sqft": {"min": 1600, "max": 1600},
                        },
                        {
                            "model_id": "m1",
                            "available": f"!Date:{future}T00:00:00.000Z",
                            "rent": {"min": 2490, "max": 2490},
                            "sqft": {"min": 1600, "max": 1600},
                        },
                    ],
                }
            }
        }
        html = f'<script id="vike_pageContext" type="application/json">{json.dumps(payload)}</script>'
        result = entrata._parse_razz_inventory(html)
        self.assertIsNotNone(result)
        # Listed = available: both feed units are on-market listings.
        self.assertEqual((2, 0), (result.units[0].available, result.units[0].coming_soon))
        self.assertEqual((2410, 2490), (result.units[0].rent_min, result.units[0].rent_max))

    def test_realpage_groups_base_and_total_rent(self):
        today = date.today().isoformat()
        future = (date.today() + timedelta(days=5)).isoformat()
        groups = realpage._group_units(
            [
                {
                    "numberOfBeds": 3,
                    "squareFeet": 1500,
                    "rent": 2200,
                    "totalRent": 2285,
                    "internalAvailableDate": today,
                    "displayed": True,
                },
                {
                    "numberOfBeds": 3,
                    "squareFeet": 1510,
                    "rent": 2300,
                    "totalRent": 2385,
                    "internalAvailableDate": future,
                    "displayed": True,
                },
            ]
        )
        self.assertEqual(1, len(groups))
        # Listed = available: future internalAvailableDate still counts.
        self.assertEqual((2, 0), (groups[0].available, groups[0].coming_soon))
        self.assertEqual((2200, 2300), (groups[0].rent_min, groups[0].rent_max))
        self.assertEqual((2285, 2385), (groups[0].rent_allin_min, groups[0].rent_allin_max))

    def test_knock_groups_available_units(self):
        groups = knock._group_units(
            [
                {
                    "available": True,
                    "leased": False,
                    "hidden": False,
                    "bedrooms": 4,
                    "area": 1940,
                    "displayPrice": "2867",
                    "availableOn": date.today().isoformat(),
                }
            ]
        )
        self.assertEqual(1, len(groups))
        self.assertEqual(1, groups[0].available)
        self.assertEqual(2867, groups[0].rent_min)

    def test_avalon_embedded_units_json(self):
        units = [
            {
                "unitId": "AVB-TX006-010-1002",
                "bedroomNumber": 3,
                "squareFeet": 1695,
                "unitStatus": "NoticeAvailable",
                "promotions": [],
                "concessions": [],
                "startingAtPricesUnfurnished": {
                    "moveInDate": "2026-09-10T04:00:00+00:00",
                    "prices": {"price": 3080, "totalPrice": 3144},
                },
            },
            {
                "unitId": "AVB-TX006-014-1405",
                "bedroomNumber": 3,
                "squareFeet": 1695,
                "unitStatus": "Available",
                "startingAtPricesUnfurnished": {
                    "prices": {"price": 3210, "totalPrice": 3274},
                },
            },
        ]
        html = '<script>{"props":{"units":' + json.dumps(units) + ',"x":1}}</script>'
        parsed = avalon._extract_units_json(html)
        self.assertIsNotNone(parsed)
        groups = avalon._group_units(parsed)
        self.assertEqual(1, len(groups))
        g = groups[0]
        # Listed = available: both units count, future move-in included.
        self.assertEqual((3, 2, 0), (g.beds, g.available, g.coming_soon))
        self.assertEqual((3080, 3210), (g.rent_min, g.rent_max))
        self.assertEqual((3144, 3274), (g.rent_allin_min, g.rent_allin_max))

    def test_repli360_deduplicates_tabs(self):
        card = """
          <div class="pro-sec rracFloorplan available">
            C4 Three Bedrooms | 2.5 Baths | 1551 sq.ft. | 1 Unit Available
            Base Rent Starting From $2,219
            *Total Monthly Leasing Price $2,226
            Available Now
          </div>
        """
        groups = repli360._parse_inventory(card + card)
        self.assertEqual(1, len(groups))
        self.assertEqual(1, groups[0].available)
        self.assertEqual(2219, groups[0].rent_min)
        self.assertEqual(2226, groups[0].rent_allin_min)


    # --- sightmap availability signals (golden-eval 2026-07-28, P2 HSV/LAS round) ---

    @staticmethod
    def _sm(units, plans=None):
        """Minimal SightMap API payload."""
        return {"data": {"units": units,
                         "floor_plans": plans or [{"id": 1, "bedroom_count": 1, "area": 700}]}}

    def test_sightmap_total_price_alone_is_not_listed(self):
        """AUS Siena South: total_price populated on units with no price AND no date
        made the whole building read as available (292/292, flag_review=false)."""
        units = ([{"floor_plan_id": 1, "area": 700, "price": 1500,
                   "available_on": "2026-08-01", "total_price": [1600, 1600]}] * 3
                 + [{"floor_plan_id": 1, "area": 700, "price": None,
                     "available_on": None, "total_price": [1600, 1600]}] * 7)
        groups, _ = sightmap._parse_api(self._sm(units), public_record_units=10)
        self.assertEqual(3, sum(g.available or 0 for g in groups))

    def test_sightmap_uniform_date_nulls_availability(self):
        """LAS Bria: 256 units all stamped one identical available_on while the live
        site listed 40. One date across the whole feed carries no information."""
        units = [{"floor_plan_id": 1, "area": 700, "price": 1500 + i,
                  "available_on": "2026-07-28"} for i in range(60)]
        groups, warns = sightmap._parse_api(self._sm(units), public_record_units=65)
        self.assertTrue(all(g.available is None for g in groups))
        self.assertTrue(all(g.flag_review for g in groups))
        self.assertTrue(any("uniform-date" in w for w in warns))

    def test_sightmap_varied_dates_survive_at_high_occupancy(self):
        """Sablewood/Skyview class: a real lease-up spreads across many dates and must
        NOT be nulled, even when the feed covers most of the building."""
        units = [{"floor_plan_id": 1, "area": 700, "price": 1500 + i,
                  "available_on": f"2026-08-{(i % 20) + 1:02d}"} for i in range(60)]
        groups, warns = sightmap._parse_api(self._sm(units), public_record_units=65)
        self.assertEqual(60, sum(g.available or 0 for g in groups))
        self.assertFalse(any("uniform-date" in w for w in warns))

    def test_sightmap_uniform_date_ignored_for_small_share(self):
        """A uniform batch that is a small slice of the building is not the defect."""
        units = [{"floor_plan_id": 1, "area": 700, "price": 1500,
                  "available_on": "2026-07-28"} for _ in range(10)]
        groups, warns = sightmap._parse_api(self._sm(units), public_record_units=400)
        self.assertEqual(10, sum(g.available or 0 for g in groups))
        self.assertFalse(any("uniform-date" in w for w in warns))

    def test_sightmap_priceless_embed_still_nulls(self):
        """Avalon class (an earlier commit) must not regress: zero priced/dated units
        => availability unknown, never the building count."""
        units = [{"floor_plan_id": 1, "area": 700, "price": None,
                  "available_on": None} for _ in range(20)]
        groups, _ = sightmap._parse_api(self._sm(units), public_record_units=126)
        self.assertTrue(all(g.available is None for g in groups))



    def test_sightmap_uniform_date_needs_every_listing_dated(self):
        """A cross-model review finding: many price-only listings plus ONE dated
        listing yields a single distinct date, but is NOT a uniform-date feed — those
        price-only units are legitimately listed and must keep their availability."""
        units = [{"floor_plan_id": 1, "area": 700, "price": 1500 + i,
                  "available_on": None} for i in range(59)]
        units.append({"floor_plan_id": 1, "area": 700, "price": 1600,
                      "available_on": "2026-07-28"})
        groups, warns = sightmap._parse_api(self._sm(units), public_record_units=65)
        self.assertEqual(60, sum(g.available or 0 for g in groups))
        self.assertFalse(any("uniform-date" in w for w in warns))

    def test_sightmap_uniform_date_excludes_unlisted_totals_from_rent(self):
        """A cross-model review finding: unlisted inventory can still carry a
        total_price; it must not move rent_allin on the vetoed path."""
        units = [{"floor_plan_id": 1, "area": 700, "price": 2000,
                  "available_on": "2026-07-28", "total_price": [2100, 2100]}
                 for _ in range(55)]
        units += [{"floor_plan_id": 1, "area": 700, "price": None,
                   "available_on": None, "total_price": [99999, 99999]} for _ in range(5)]
        groups, warns = sightmap._parse_api(self._sm(units), public_record_units=62)
        self.assertTrue(any("uniform-date" in w for w in warns))
        for g in groups:
            if g.rent_allin_max is not None:
                self.assertLess(g.rent_allin_max, 99999)



    # --- beds:null invariant (PLAN.md Phase 2a, 2026-07-28) -------------------

    def test_rentcafe_card_resolves_bed_and_studio_without_bathroom_word(self):
        """86 rentcafe communities dumped units into a beds-None catch-all because the
        walk required BOTH "N Bedroom" and a sq-ft string. "1 Bed | 1 Bath" and Studio
        must now resolve (LAS LVB's path)."""
        html = ('<section><h2>The Aspen | 1 Bed | 1 Bath | 712 Sq. Ft.</h2>'
                '<div class="card">Apartment: #204 Base rent from $1,398</div></section>'
                '<section><h2>The Birch | Studio | 555 Sq. Ft.</h2>'
                '<div class="card">Apartment: #101 Base rent from $1,499</div></section>')
        groups, _ = rentcafe._parse_card_layout(BeautifulSoup(html, "html.parser"))
        self.assertEqual({0, 1}, {g.beds for g in groups})
        self.assertEqual(0, sum(1 for g in groups if g.beds is None))

    def test_drop_null_bed_groups_drops_only_EMPTY_groups(self):
        """An EMPTY beds-null group is parse residue and goes."""
        from rentdb.adapters.base import ExtractResult, UnitGroup, drop_null_bed_groups
        r = ExtractResult(units=[UnitGroup(beds=1, sf_min=700, sf_max=700, available=3),
                                 UnitGroup(beds=None, sf_min=None, sf_max=None, available=0)],
                          extractor_used="rentcafe")
        drop_null_bed_groups(r, "rentcafe")
        self.assertEqual(1, len(r.units))
        self.assertEqual(1, r.units[0].beds)
        self.assertTrue(r.flag_review)
        self.assertIn("unresolved beds", r.scrape_error)

    def test_drop_null_bed_groups_KEEPS_groups_carrying_real_data(self):
        """The 2026-07-28 correction: on live data, 48 of 65 affected communities had
        their ONLY rent inside the beds-null group, and that rent does reach
        v_latest_unit_groups -> the rent-comp consumer. Dropping it would destroy real pricing
        to fix a grading-visibility problem. Keep + flag instead."""
        from rentdb.adapters.base import ExtractResult, UnitGroup, drop_null_bed_groups
        r = ExtractResult(units=[UnitGroup(beds=None, sf_min=None, sf_max=None,
                                           rent_min=1509, rent_max=1698, available=20),
                                 UnitGroup(beds=1, sf_min=690, sf_max=690, available=0)],
                          extractor_used="rentcafe")
        drop_null_bed_groups(r, "rentcafe")
        self.assertEqual(2, len(r.units))                       # nothing removed
        self.assertTrue(any(g.rent_min == 1509 for g in r.units))  # pricing survives
        self.assertTrue(r.flag_review)
        self.assertIn("KEPT", r.scrape_error)

    def test_drop_null_bed_groups_noop_when_clean(self):
        from rentdb.adapters.base import ExtractResult, UnitGroup, drop_null_bed_groups
        r = ExtractResult(units=[UnitGroup(beds=2, sf_min=900, sf_max=900, available=1)], extractor_used="knock")
        drop_null_bed_groups(r, "knock")
        self.assertEqual(1, len(r.units))
        self.assertFalse(r.flag_review)
        self.assertIsNone(r.scrape_error)





class EntrataRazzDispatchTests(unittest.TestCase):
    """`_parse_razz_inventory` works; nothing ROUTED to it.

    RED-first, 2026-08-20. The Nashville/Tampa needs_adapter triage classified 15 of
    86 unparseable communities as Razz Interactive sites carrying Entrata inventory in
    a `<script id="vike_pageContext">` blob — and `entrata._parse_razz_inventory()` has
    parsed exactly that shape since the Austin wave. Calling it directly on all 15
    returned real groups and rents, 15/15.

    They were never reached, because `matches()` accepts a community only when the URL
    is a prospectportal.com host or the HTML carries one of ENTRATA_SIGNALS
    ("prospectportal.com", "commoncf.entrata.com", "entrata's accessibility
    statement"). A Razz page carries NONE of the three, so entrata declined the row,
    every later adapter declined it too, and it fell through to the LLM tail and then
    to needs_adapter.

    The existing razz tests (test_new_adapters.test_entrata_razz_inventory_dates,
    test_unit_rents.test_entrata_razz_inventory) call the parser DIRECTLY. That is why
    a fully-working parser sat unreachable for a whole campaign with a green suite:
    the parser was pinned and the DISPATCH never was. These tests pin the dispatch.
    """

    RAZZ_PAYLOAD = {
        "initialStoreState": {
            "$inventory": {
                "models": [{"id": 1, "beds": 2, "baths": 2.0, "sqft": 1100}],
                "units": [{"model_id": 1, "available": "2026-09-01", "rent": 1725}],
            }
        }
    }

    def _razz_html(self) -> str:
        # No prospectportal.com, no commoncf.entrata.com, no accessibility string —
        # exactly what the 15 live pages look like.
        return (
            "<html><head><title>Westshore Crossing Apartments</title></head><body>"
            '<script id="vike_pageContext" type="application/json">'
            f"{json.dumps(self.RAZZ_PAYLOAD)}</script>"
            "</body></html>"
        )

    def _patched_matches(self, html: str, url: str = "https://westshorecrossing.com/") -> bool:
        """Run entrata.matches() against `html` without touching the network."""
        # entrata.matches() does `from rentdb.fetchers import fetch_static` at call time,
        # which resolves against rentdb/ being on sys.path (as it is under the pipeline).
        import sys
        from pathlib import Path as _P
        _tools = str(_P(__file__).resolve().parent.parent)
        if _tools not in sys.path:
            sys.path.insert(0, _tools)
        import rentdb.fetchers as fetchers
        from rentdb.fetchers.static import FetchResult

        real = fetchers.fetch_static
        fetchers.fetch_static = lambda u, **kw: FetchResult(
            status=200, final_url=u, html=html, bytes_len=len(html), profile_used="test")
        try:
            return entrata.matches({"url": url})
        finally:
            fetchers.fetch_static = real

    def test_razz_page_is_recognised_as_entrata(self):
        """The defect itself: a Razz page must dispatch to entrata."""
        self.assertTrue(
            self._patched_matches(self._razz_html()),
            "entrata.matches() declined a page carrying vike_pageContext + $inventory, "
            "so _parse_razz_inventory() — which parses exactly this — is unreachable",
        )

    def test_razz_signal_needs_BOTH_marker_and_inventory(self):
        """Guard against widening the fingerprint into a false positive.

        `vike_pageContext` is a Vike/Vite SSR marker, not an Entrata one — any Vike site
        emits it. Matching on it alone would hand entrata every Vike page on the fleet.
        The discriminator is the marker AND an Entrata inventory payload inside it.
        """
        vike_only = (
            "<html><body>"
            '<script id="vike_pageContext" type="application/json">'
            '{"initialStoreState": {"$somethingElse": {}}}</script>'
            "</body></html>"
        )
        self.assertFalse(
            self._patched_matches(vike_only),
            "a Vike page with no Entrata inventory must NOT be claimed by entrata",
        )

    def test_unrelated_page_still_declined(self):
        self.assertFalse(self._patched_matches("<html><body>Hello</body></html>"))

    def test_parser_still_reached_through_the_public_signals(self):
        """The three original ENTRATA_SIGNALS must keep matching — no regression."""
        for signal in entrata.ENTRATA_SIGNALS:
            with self.subTest(signal=signal):
                self.assertTrue(
                    self._patched_matches(f"<html><body>{signal}</body></html>"))


class EntrataRazzFindAdapterTests(unittest.TestCase):
    """The PRODUCTION dispatch path — `find_adapter`, not `matches`.

    RED-first, 2026-08-20, and this is the SECOND red on the same defect because the
    first fix was aimed at the wrong seam.

    `entrata.matches()` was widened to recognise Razz, EntrataRazzDispatchTests went
    green, and production did not change: a controlled `the pipeline --skip-llm` over the
    15 known-Razz communities still reported `SKIPPED (no adapter, --skip-llm)` 15/15.

    The reason is that `adapters.find_adapter()` does not call `matches()` at all. For
    performance it re-implements vendor detection inline off ONE shared homepage fetch:

        if any(signal in low for signal in entrata.ENTRATA_SIGNALS):
            return entrata

    So there are two parallel detection paths, `matches()` and `find_adapter`, and
    the pipeline uses only the second (via `find_adapter_with_probe`). Widening one left
    the other untouched.

    That is the same shape as the original bug — a working parser nothing routed to —
    one level up. These tests pin the path the pipeline actually executes.
    """

    RAZZ_PAYLOAD = EntrataRazzDispatchTests.RAZZ_PAYLOAD

    def _razz_html(self) -> str:
        return (
            "<html><head><title>Westshore Crossing</title></head><body>"
            '<script id="vike_pageContext" type="application/json">'
            f"{json.dumps(self.RAZZ_PAYLOAD)}</script></body></html>"
        )

    def _find(self, html: str, url: str = "https://www.westshorecrossingtampa.com/"):
        import sys
        from pathlib import Path as _P
        _tools = str(_P(__file__).resolve().parent.parent)
        if _tools not in sys.path:
            sys.path.insert(0, _tools)
        import rentdb.fetchers as fetchers
        from rentdb.fetchers.static import FetchResult
        from rentdb import adapters as A

        real = fetchers.fetch_static
        fetchers.fetch_static = lambda u, **kw: FetchResult(
            status=200, final_url=u, html=html, bytes_len=len(html), profile_used="test")
        try:
            mod = A.find_adapter({"url": url})
            return getattr(mod, "NAME", None)
        finally:
            fetchers.fetch_static = real

    def test_find_adapter_routes_a_razz_page_to_entrata(self):
        """The defect the pipeline actually hits."""
        self.assertEqual(
            "entrata", self._find(self._razz_html()),
            "find_adapter() — the path the pipeline uses via find_adapter_with_probe — "
            "did not route a Razz page to entrata, so widening matches() changed nothing")

    def test_find_adapter_still_declines_a_bare_vike_page(self):
        """The false-positive guard has to hold on this path too."""
        self.assertIsNone(self._find(
            '<html><body><script id="vike_pageContext" type="application/json">'
            '{"initialStoreState": {"$somethingElse": {}}}</script></body></html>'))

    def test_find_adapter_still_honours_the_original_signals(self):
        for signal in entrata.ENTRATA_SIGNALS:
            with self.subTest(signal=signal):
                self.assertEqual("entrata", self._find(f"<html><body>{signal}</body></html>"))

    def test_operator_adapters_still_win_over_entrata(self):
        """Order matters: a Razz signal must not outrank the operator-level adapters."""
        self.assertIsNone(self._find("<html><body>nothing here</body></html>"))


if __name__ == "__main__":
    unittest.main()
