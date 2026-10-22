"""Adapter for Repli360's public floor-plan inventory widget."""

from __future__ import annotations

import re
from collections import defaultdict
from bs4 import BeautifulSoup
from curl_cffi import requests as cr

from . import cues
from .base import ExtractResult, UnitGroup, observed_baths

NAME = "repli360"
SF_BAND = 50
_SCRIPT_RE = re.compile(r"""https?://app\.repli360\.com/admin/rrac-website-script/[^"' ]+""", re.I)
_SITE_ID_RE = re.compile(r"""site_id\s*=\s*['"](\d+)""", re.I)
_NUMBER_WORDS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
}


def matches(community: dict) -> bool:
    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return False
    try:
        from rentdb.fetchers import fetch_static
        page = fetch_static(url)
    except Exception:
        return False
    return page.status < 400 and _SCRIPT_RE.search(page.html) is not None


def extract(community: dict) -> ExtractResult:
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    try:
        homepage = fetch_static(url)
    except Exception as exc:
        return ExtractResult(
            scrape_error=f"repli360: site fetch failed: {type(exc).__name__}: {exc}"[:200],
            extractor_used=NAME,
            flag_review=True,
        )
    script_match = _SCRIPT_RE.search(homepage.html)
    if not script_match:
        return ExtractResult(
            scrape_error="repli360: widget script not found",
            extractor_used=NAME,
            flag_review=True,
        )
    return _extract_script(url, script_match.group(0))


def extract_rendered(community: dict, html: str) -> ExtractResult:
    """Read the Repli360 widget whose script tag is in the browser-RENDERED page.

    On ~14 communities the widget script is injected by JavaScript, so the static homepage
    `extract` reads has no script tag and the LLM tier got the page (Monterra Village
    52400d99: script only after render, static homepage none)."""
    script_match = _SCRIPT_RE.search(html or "")
    if not script_match:
        return ExtractResult(scrape_error="repli360: widget script not in the rendered page",
                             extractor_used=NAME, flag_review=True)
    return _extract_script((community.get("url") or "").strip(), script_match.group(0))


def _extract_script(url: str, script_url: str) -> ExtractResult:
    """The widget script -> its site id -> the inventory render. `url` is the referer."""
    script = cr.get(script_url, impersonate="chrome131", timeout=25)
    site_match = _SITE_ID_RE.search(script.text)
    if script.status_code >= 400 or not site_match:
        return ExtractResult(
            scrape_error="repli360: site id not found",
            extractor_used=NAME,
            flag_review=True,
        )

    response = cr.post(
        "https://app.repli360.com/admin/template-render",
        data={
            "site_id": site_match.group(1),
            "action": "",
            "ready_script": "dom_load",
            "template_type": "",
            "source": "",
            "property_id": "",
            "zpopUp": "",
        },
        headers={"referer": url},
        impersonate="chrome131",
        timeout=30,
    )
    if response.status_code >= 400:
        return ExtractResult(
            scrape_error=f"repli360: inventory HTTP {response.status_code}",
            extractor_used=NAME,
            flag_review=True,
        )
    groups = _parse_inventory(response.text)
    return ExtractResult(
        units=groups,
        extractor_used=NAME,
        flag_review=any(group.flag_review for group in groups),
        scrape_error=None if groups else "repli360: no priced available floor plans",
    )


def _cue(members: list[dict]) -> str:
    """Card text of a group's members — the only descriptive text this feed has."""
    return " ".join(m.get("cue_text") or "" for m in members)


def _attribute_row(card, text: str) -> dict | None:
    """A card whose TEXT layout the regexes above do not know, read off its attributes.

    CLT 1001 Tyvola (2026-09-25): "Starting $1,223", "Studio", and "1 Unit Available" on
    every card because the site caps the displayed count (`rrac_display_max_available_unit`
    = '1'). The card carries data-bed / data-size / data-min-price, and one date per
    listed unit in data-available-date — that list's length is the true count.
    """
    import json
    try:
        beds = int(card.get("data-bed"))
        sqft = int(float(card.get("data-size")))
        rent = int(round(float(card.get("data-min-price"))))
    except (TypeError, ValueError):
        return None
    try:
        count = len(json.loads(card.get("data-available-date") or "[]"))
    except ValueError:
        count = 0
    if not count:
        m = re.search(r"(\d+)\s+Units?\s+Available", text, re.I)
        count = int(m.group(1)) if m else 0
    if rent <= 0 or count <= 0:
        return None
    bath_match = re.search(r"(\d+(?:\.\d+)?)\s*Baths?\b", text, re.I)
    return {"id": card.get("data-id") or f"{beds}/{sqft}/{rent}", "beds": beds,
            "baths": float(bath_match.group(1)) if bath_match else None, "sqft": sqft,
            "rent": rent, "allin": None, "available": count, "soon": 0, "cue_text": text}


def _parse_inventory(html: str) -> list[UnitGroup]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[tuple] = set()
    rows = []
    for card in soup.select(".rracFloorplan.available"):
        text = card.get_text(" ", strip=True)
        bed_match = re.search(r"(\d+|One|Two|Three|Four|Five)\s+Bedrooms?", text, re.I)
        sf_match = re.search(r"([\d,]+)\s*sq\.?ft", text, re.I)
        # "C4 Three Bedrooms | 2.5 Baths | 1551 sq.ft." — the bath count sits in
        # the same card line beds and SF are already read from. Halves are real.
        bath_match = re.search(r"(\d+(?:\.\d+)?)\s*Baths?\b", text, re.I)
        count_match = re.search(r"(\d+)\s+Units?\s+Available", text, re.I)
        base_match = re.search(r"Base\s+Rent\s+Starting\s+From\s+\$([\d,]+)", text, re.I)
        allin_match = re.search(r"Total\s+Monthly\s+Leasing\s+Price\s+\$([\d,.]+)", text, re.I)
        if not bed_match or not sf_match or not count_match or not base_match:
            row = _attribute_row(card, text)
            if row is not None and ("id", row["id"]) not in seen:
                seen.add(("id", row["id"]))
                rows.append(row)
            continue
        bed_token = bed_match.group(1).lower()
        beds = int(bed_token) if bed_token.isdigit() else _NUMBER_WORDS.get(bed_token)
        sqft = int(sf_match.group(1).replace(",", ""))
        count = int(count_match.group(1))
        rent = int(base_match.group(1).replace(",", ""))
        allin = int(round(float(allin_match.group(1).replace(",", "")))) if allin_match else None
        # Listed = available (2026-07-14): the card advertises "N Units
        # Available" with a price — that IS the site's availability count,
        # whether or not it also says "Available Now".
        key = (beds, sqft, rent, allin, count)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "beds": beds,
                "baths": float(bath_match.group(1)) if bath_match else None,
                "sqft": sqft,
                "rent": rent,
                "allin": allin,
                "available": count,
                "soon": 0,
                "cue_text": text,
            }
        )

    buckets: dict[tuple[int | None, int], list[dict]] = defaultdict(list)
    for row in rows:
        center = round(row["sqft"] / SF_BAND) * SF_BAND
        buckets[(row["beds"], center)].append(row)
    groups = []
    for (beds, _), members in sorted(buckets.items(), key=lambda item: (item[0][0] or 0, item[0][1])):
        groups.append(
            UnitGroup(
                beds=beds,
                sf_min=min(m["sqft"] for m in members),
                sf_max=max(m["sqft"] for m in members),
                available=sum(m["available"] for m in members),
                coming_soon=sum(m["soon"] for m in members),
                rent_min=min(m["rent"] for m in members),
                rent_max=max(m["rent"] for m in members),
                rent_type="base",
                baths=observed_baths(m["baths"] for m in members),
                rent_allin_min=min((m["allin"] for m in members if m["allin"]), default=None),
                rent_allin_max=max((m["allin"] for m in members if m["allin"]), default=None),
                structure_type=cues.structure_type_from_text(_cue(members)),
                stories=cues.stories_from_text(_cue(members)),
                garage=cues.garage_from_text(_cue(members)),
                # unit_rents stays None ON PURPOSE. A repli360 member is a PLAN, not
                # a unit: the card reads "Base Rent Starting From $2,219" next to
                # "3 Units Available", so that one number is a FLOOR covering three
                # unpriced units. Recording it as three observations — or as one —
                # would launder a floor into a measured rent. None routes these to
                # the rent_min branch of the market-rent rule, which is what a
                # "starting from" price honestly supports.
            )
        )
    return groups
