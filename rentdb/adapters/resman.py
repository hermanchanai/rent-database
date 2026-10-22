"""Adapter for ResMan's public applicant availability portal (2026-10-05).

    https://<pm>.myresman.com/Portal/Applicants/Availability?a=<account>&p=<property uuid>

26 communities in the diagnosis link this page from their marketing site and nothing
read it, so every snapshot was the LLM tier's "no pricing visible" off the homepage. The
page is SERVER-RENDERED: each listed unit is

    div.unit.available-unit[data-unit-type="A1 Classic"]
        h4                          the unit number
        .fw > .fl label + .fv       Sq Ft / Bedrooms / Bathrooms / Building / Floor / ...
        table.unit-rent-table       header "Lease term | Rent": one row per term,
                                    td.unit-lease-term-col ("12 Months") + span.rent-value

and the floor-plan cards (`[data-unit-type-id]`) render even when no unit is listed.

Read rules:
  * `p=` scopes the page to ONE property. A marketing page linking two different `p=`
    values is a manager's portfolio page (TPA Windtree Villas' manager page links two), so
    detection refuses it rather than pick one.
  * A unit's rent is its BEST lease-term price — min over the "N Months" rows — the fleet
    convention (realpage `bestprice=true`, entrata "starting at the best lease term",
    rentcafe/rentcafe_marketplace low end). It is a price a renter can pay for THAT unit, so
    it is observed and goes into `unit_rents`; rent_basis "actual", rent_type "base" (the
    page keeps fees in a separate "Other Charges" table).
  * Only rows whose term reads "<n> month(s)" are lease terms. HOU The Redford's units carry
    an "Other Charges" table — Pest Control 8.00, Trash 20.00 — in the SAME
    `unit-rent-table` class; read as rents those would be the unit's minimum.
  * `span.special-rent-value` is the page's slot for a discounted price beside a struck-out
    regular one. It was EMPTY on all 21 public pages (2026-10-05), so its semantics are
    unverified: when one is ever non-empty the regular price is still the one stored and
    the group is flagged for review, never guessed at.
  * listed = available: every `available-unit` counts, whatever its "Available on" date.
  * Plans rendered and NO unit card at all -> status no_availability, affirmed: the portal
    IS the property's listing. A listed card the parser cannot read is an error,
    never a zero and never silently dropped. "Page Not Enabled" is refused with an error.

TLS: `*.myresman.com` chains to "SSL.com TLS RSA Root CA 2022", which curl_cffi's built-in
CA bundle lacks (`curl: (60) unable to get local issuer certificate` on every live GET), so
the portal fetch passes certifi's bundle.
"""

from __future__ import annotations

import html as _html
import re
from collections import defaultdict

from bs4 import BeautifulSoup

from .base import STATUS_NOAVAIL, ExtractResult, UnitGroup, observed_baths, observed_rents

NAME = "resman"
SF_BAND = 50

_LINK_RE = re.compile(
    r"https?://([a-z0-9-]+)\.myresman\.com/Portal/Applicants/Availability"
    r"\?a=(\d+)(?:&amp;|&)p=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I)
_TERM_RE = re.compile(r"^\s*(\d{1,2})\s*months?\s*$", re.I)
_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")


def portal_url(html: str) -> str | None:
    """The ONE property availability URL this page links, else None (none, or several)."""
    props = {(m.group(1).lower(), m.group(2), m.group(3).lower())
             for m in _LINK_RE.finditer(_html.unescape(html or ""))}
    if len(props) != 1:
        return None
    host, acct, prop = props.pop()
    return f"https://{host}.myresman.com/Portal/Applicants/Availability?a={acct}&p={prop}"


def has_signal(html: str) -> bool:
    """Detection for `find_adapters` — the same call `extract` makes."""
    return portal_url(html) is not None


def matches(community: dict) -> bool:
    url, _ = _resolve(community)
    return url is not None


def _resolve(community: dict):
    from rentdb.fetchers import fetch_static

    url = (community.get("url") or "").strip()
    if not url.startswith("http"):
        return None, "resman: no usable community url"
    if _LINK_RE.search(url):
        return portal_url(url), None
    try:
        page = fetch_static(url)
    except Exception as exc:  # noqa: BLE001
        return None, f"resman: homepage fetch failed: {type(exc).__name__}: {exc}"
    if page.status >= 400:
        return None, f"resman: homepage HTTP {page.status}"
    target = portal_url(page.html)
    if not target:
        return None, "resman: no single-property availability link on the homepage"
    return target, None


def _num(text: str | None) -> float | None:
    m = _NUM_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _field(unit, label: str) -> str | None:
    for fw in unit.select(".fw"):
        fl, fv = fw.select_one(".fl"), fw.select_one(".fv")
        if fl and fv and fl.get_text(" ", strip=True).lower() == label.lower():
            return fv.get_text(" ", strip=True)
    return None


def _unit_facts(unit) -> dict | None:
    beds = _num(_field(unit, "Bedrooms"))
    if beds is None:
        return None
    rents, special = [], False
    for row in unit.select("table.unit-rent-table tr"):
        term = row.select_one("td.unit-lease-term-col")
        if term is None or not _TERM_RE.match(term.get_text(" ", strip=True)):
            continue   # an "Other Charges" row (Pest Control, Trash, ...) — a fee, not a rent
        rv = row.select_one(".rent-value")
        val = _num(rv.get_text(" ", strip=True)) if rv else None
        if val is not None:
            rents.append(int(round(val)))
        sp = row.select_one(".special-rent-value")
        if sp is not None and sp.get_text(strip=True):
            special = True
    positive = [r for r in rents if r > 0]
    sf = _num(_field(unit, "Sq Ft"))
    return {"beds": int(beds), "baths": _num(_field(unit, "Bathrooms")),
            "sf": int(sf) if sf else None,
            "rent": min(positive) if positive else None,
            "special": special}


def parse_availability(html: str) -> tuple[list[UnitGroup], int, bool, int]:
    """-> (groups, plan_count, page_enabled, unreadable_units).

    `unreadable_units` counts listed unit cards whose bed count could not be read: they are
    LISTED, so a zero built by skipping them is not a zero."""
    soup = BeautifulSoup(html or "", "html.parser")
    enabled = "page is not enabled" not in soup.get_text(" ", strip=True).lower()
    plans = len(soup.select("[data-unit-type-id]"))
    unreadable = 0
    buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for unit in soup.select("div.unit.available-unit"):
        facts = _unit_facts(unit)
        if not facts:
            unreadable += 1
            continue
        center = round((facts["sf"] or 0) / SF_BAND) * SF_BAND
        buckets[(facts["beds"], center)].append(facts)
    groups = []
    for (beds, _), members in sorted(buckets.items()):
        rents = [m["rent"] for m in members if m["rent"]]
        sfs = [m["sf"] for m in members if m["sf"]]
        groups.append(UnitGroup(
            beds=beds,
            sf_min=min(sfs) if sfs else None,
            sf_max=max(sfs) if sfs else None,
            available=len(members),
            coming_soon=0,
            rent_min=min(rents) if rents else None,
            rent_max=max(rents) if rents else None,
            rent_type="base",
            rent_basis="actual" if rents else "unspecified",
            unit_rents=observed_rents(rents),
            baths=observed_baths(m["baths"] for m in members),
            flag_review=(not rents) or any(m["special"] for m in members),
        ))
    return groups, plans, enabled, unreadable


def extract(community: dict) -> ExtractResult:
    import certifi
    from rentdb.fetchers import fetch_static

    target, err = _resolve(community)
    if not target:
        return ExtractResult(scrape_error=(err or "resman: no portal link")[:200],
                             extractor_used=NAME, flag_review=True)
    try:
        page = fetch_static(target, verify=certifi.where())
    except Exception as exc:  # noqa: BLE001
        return ExtractResult(
            scrape_error=f"resman: availability fetch failed: {type(exc).__name__}: {exc}"[:200],
            extractor_used=NAME, flag_review=True)
    if page.status >= 400:
        return ExtractResult(scrape_error=f"resman: availability HTTP {page.status}",
                             extractor_used=NAME, flag_review=True)
    groups, plans, enabled, unreadable = parse_availability(page.html)
    if not enabled:
        return ExtractResult(scrape_error="resman: availability page is not enabled for "
                                          "this property — refused, not written",
                             extractor_used=NAME, flag_review=True)
    if unreadable:
        # Listed cards the parser could not read: the page changed under us. Never a zero,
        # never a partial count passed off as the listing.
        return ExtractResult(scrape_error=(f"resman: {unreadable} listed unit card(s) "
                                           f"unreadable (no bed count) at {target}")[:200],
                             extractor_used=NAME, flag_review=True)
    if not groups:
        if plans:
            # The portal rendered the property's plans and lists no unit: an affirmed zero.
            return ExtractResult(units=[], extractor_used=NAME, status=STATUS_NOAVAIL)
        return ExtractResult(scrape_error=f"resman: no plans or units parsed at {target}"[:200],
                             extractor_used=NAME, flag_review=True)
    return ExtractResult(units=groups, extractor_used=NAME,
                         flag_review=any(g.flag_review for g in groups))
