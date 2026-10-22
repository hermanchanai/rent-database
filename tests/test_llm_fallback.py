"""The LLM tier, with the model call replaced by a saved reply.

The three pages and replies were saved from one production comparison run. Expected values
are read off the page text (the base-rent and all-in figures are printed side by side), never
off the model's output: on two of these pages the saved reply put the all-in figure in the
primary rent field, and the deterministic backstop is what moves it back.
"""
from __future__ import annotations

import json

from rentdb.llm_fallback import SYSTEM_PROMPT, LLMExtractor, RecordedProvider

from .conftest import page


def replay(slug, community=None):
    provider = RecordedProvider(page(slug, "model_reply.txt"))
    result = LLMExtractor(provider).extract(community or {"name": slug}, page(slug, "rendered.txt"))
    return result, provider


def rows(result):
    return [(g.beds, g.rent_min, g.rent_max, g.rent_type, g.rent_allin_min, g.rent_allin_max)
            for g in result.units]


def test_base_rent_is_primary_when_the_page_prints_both():
    result, _ = replay("llm-base-and-allin")
    assert rows(result) == [(1, 1475, 1650, "base", 1653, 1828),
                            (2, 1595, 1950, "base", 1773, 2128),
                            (3, 2300, 2450, "base", 2478, 2628)]


def test_floor_prices_keep_their_basis_after_the_base_rent_swap():
    result, _ = replay("llm-floor-prices")
    got = rows(result)
    assert (1, 1248, 1401, "base", 1383, 1536) in got
    assert (2, 1506, 1652, "base", 1641, 1787) in got
    assert (3, 2478, 2478, "base", 2613, 2613) in got
    assert (1, None, None, "allin", None, None) in got           # an unpriced plan is untouched
    assert all(g.rent_basis == "starting_at" for g in result.units if g.rent_min is not None)
    assert result.concession_weeks is None                       # "$2,000 off" is not weeks


def test_an_all_in_only_page_stays_all_in():
    result, _ = replay("llm-allin-only")
    assert {g.rent_type for g in result.units} == {"allin"}
    assert all(g.rent_allin_min is None for g in result.units)
    assert result.concession_weeks == 8                          # "Up to TWO MONTHS of FREE RENT"


def test_the_model_sees_name_and_address_only():
    _, provider = replay("llm-allin-only", {"name": "Example", "address": "1 Main St",
                                            "public_record_units": 300, "owner": "Someone"})
    system, user = provider.calls[0]
    assert system == SYSTEM_PROMPT
    head = user.split("PAGE TEXT", 1)[0]
    assert "name: Example" in head and "address: 1 Main St" in head
    assert "300" not in head and "owner" not in head


def test_code_fences_are_stripped_and_truncated_json_is_salvaged_but_flagged():
    reply = json.loads(page("llm-allin-only", "model_reply.txt").strip().strip("`").removeprefix("json"))
    full = json.dumps(reply)
    ex = LLMExtractor(RecordedProvider(""))
    ok = ex.parse_reply("", "```json\n" + full + "\n```")
    assert len(ok.units) == 3 and not ok.scrape_error
    cut = full[: full.rfind('{"beds"')]                           # reply cut off mid-array
    partial = ex.parse_reply("", cut)
    assert len(partial.units) == 2
    assert partial.flag_review and "truncated" in partial.scrape_error


def test_a_provider_error_is_a_scrape_error_not_a_crash():
    class Broken:
        name = "broken"

        def complete(self, system, user):
            raise TimeoutError("upstream timeout")

    result = LLMExtractor(Broken()).extract({"name": "x"}, "<p>$1,500</p>")
    assert result.units == [] and result.flag_review
    assert "TimeoutError" in result.scrape_error
