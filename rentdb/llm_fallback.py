"""Tier 2: the LLM fallback for pages no deterministic reader could price.

The model sees the page's VISIBLE TEXT (not markup), returns one JSON object per the
schema in SYSTEM_PROMPT, and everything after that is deterministic: the reply is parsed
defensively, coerced into the same UnitGroup shape every adapter emits, and then re-checked
against the page by backstops that do not trust the model (floor prices, base-vs-all-in
rent, SF bands, self-contradicting groups, the concession count).

The model call itself is injected. `LLMExtractor(provider)` takes any object with
`complete(system: str, user: str) -> str`; production wrapped a hosted model behind a
spend-tracking client. Tests and the offline demo use `RecordedProvider`, which replays a
saved reply, so nothing here needs a key or the network.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional, Protocol

from bs4 import BeautifulSoup

from rentdb.adapters.base import ExtractResult, UnitGroup, clean_plan_name, observed_rents
from rentdb.adapters.cues import normalize_amenities
from rentdb.concessions import (_SPLIT_OFFER, fill_concession_from_banner, merge_concession,
                                reconcile_concession, veto_non_rent_quote)

log = logging.getLogger(__name__)

MAX_HTML_CHARS = 60_000  # cap after trimming + price-window slide
MAX_TOKENS = 4_000       # output cap the production caller set
# Heuristic regex for "rent-like" prices used in price-window trimming.
# Matches $1,XXX through $9,XXX with optional comma, not followed by another digit.
PRICE_HINT_RE = re.compile(r"\$\s?[1-9],?\d{3}(?!\d)")

# Strip these tags entirely from input HTML.
# Structural chrome (nav/footer/aside) goes; <header> is KEPT since v2 text
# mode — hero concession banners live there and text-mode header noise is tiny.
DROP_TAGS = {
    "script", "style", "noscript", "svg", "iframe", "img", "video", "audio",
    "canvas", "meta", "link", "nav", "footer", "aside",
}
# Class/id exact-match drops (only for genuinely safe banner-style elements).
# Use exact id match or class equality — NOT substring.
DROP_BY_EXACT_ID = ("cookie-banner", "newsletter-modal", "chat-widget")
DROP_BY_EXACT_CLASS = ("cookie-banner", "newsletter-modal", "chat-widget", "cookie-consent")

SYSTEM_PROMPT = """You are a real-estate data extractor. Your job: given the rendered visible text of one rental community's pricing/availability page, extract structured rent and concession data.

Return ONLY a JSON object matching this exact schema (no prose, no code fences):

{
  "concession_weeks": <integer | null>,        // weeks of free rent the community offers. "X weeks free"=X. "X months free"=X*4. "$Y off"=null (can't normalize without rent). "up to X" or vague=null.
  "raw_concession_text": <string | null>,      // verbatim concession text from the page, or null if none mentioned.
  "rent_type": "base" | "allin" | "unspecified",  // does the site display BASE rent (pre-fees), ALL-IN (incl mandatory fees), or unlabeled?
  "units": [
    {
      "beds": <integer | null>,                // bedroom count
      "sf_min": <integer | null>,              // smallest square footage in this floor plan group
      "sf_max": <integer | null>,              // largest square footage
      "available": <integer>,                  // count of units LISTED for lease (priced or with a move-in date) — count a future move-in date as available; the site's own "N available" is the ground truth
      "coming_soon": <integer>,                // count of units/plans advertised but NOT yet listed (no price, no listing row, or explicitly labeled "coming soon")
      "rent_min": <integer | null>,            // min rent in whole dollars (no $, no decimal)
      "rent_max": <integer | null>,            // max rent in whole dollars
      "rent_allin_min": <integer | null>,      // populate ONLY if site shows both base AND all-in side-by-side
      "rent_allin_max": <integer | null>,
      "rent_basis": "actual" | "starting_at",  // are these rents a renter can pay ("actual"), or FLOOR prices the page renders "from $X" / "$X+" ("starting_at")? See HARD RULE 3.
      "unit_rents": <[integer] | null>,        // the individual rents of the listed units in this group, ASCENDING, LOWEST 10 ONLY. null unless the page prices units one by one. See HARD RULE 4.
      "structure_type": "sfd" | "th" | "mf" | null,  // what the PAGE CALLS this product: "townhome(s)"/"townhouse" => "th"; "single-family home"/"detached home" => "sfd"; "apartment(s)"/"condo(s)"/"flats" => "mf". null when the page never says. See HARD RULE 8.
      "stories": <integer | null>,             // storeys, ONLY when the page states it for this plan ("two-story" => 2, "single-story"/"ranch-style" => 1). null otherwise.
      "garage": <integer | null>,              // garage CAR COUNT, ONLY when stated: "2-car garage" => 2, "no garage" => 0. A bare "garage"/"attached garage" with no number => null. See HARD RULE 8.
      "baths": <number | null>,                // BATHROOM COUNT this plan states: "1 Bath"/"1 BA" => 1, "2.5 Baths" => 2.5. Halves are real, keep them. null when the page never says for this plan, and NEVER inferred from beds or SF. See HARD RULE 8.
      "plan_name": <string | null>,            // the page's OWN name for this floor plan: "The Sonoran", "A1", "Oak Two-Story". Copy it verbatim. null when the page names no plan. See HARD RULE 9.
      "flag_review": <boolean>                 // true if you're uncertain about any field in this group
    }
  ],
  "amenities": <[string] | null>,              // community amenities the page NAMES, lowercase, ONLY from this list: pool, spa, gym, clubhouse, dog park, pet friendly, playground, gated, tennis, pickleball, walking trails, smart home, ev charging. null when the page names none of them.
  "flag_review": <boolean>,                    // page-level flag: site was unreadable, anti-bot, or you couldn't extract confidently
  "scrape_error": <string | null>              // brief error description if you couldn't extract (e.g., "no pricing visible", "page requires login")
}

GROUPING RULE: Group units by (bedroom_count, SF band ±50sqft). Two units with the same beds and SF within 50sqft of each other are the same group. Different group otherwise.

COVERAGE RULE: Enumerate EVERY floor plan / bed type the page advertises — including plans with zero available units, no price, or a "join waitlist" state (emit them with available=0 and null rents rather than skipping them). A missing bed group is a worse error than an empty one. Always capture square footage when it appears anywhere near the plan (e.g. "748 Sq. Ft.", "1,205 SF").

COVERAGE RULE 2 — AN ADVERTISED PLAN YOU CANNOT RENT TODAY STILL SETS THE SF BAND. Many pages segregate those plans into their own block: "Unavailable Floor Plans", "Notify Me", "Join Waitlist", "Contact for Availability", "Call for Pricing", "Please Call", "0 available units". The block still publishes a PLAN with a SQUARE FOOTAGE, and that geometry is real. Its bed group's sf_min/sf_max must reach it — and nothing else about that group may move, because such a plan has no rent a renter can pay and no inventory. The signature of getting this wrong: a bed type whose sf_max stops at the largest plan you can rent TODAY while the page advertises a bigger one.

`coming_soon` is NOT the place for them. Reserve it for units the page explicitly calls "coming soon" or gives a future delivery date. A plan that merely has nothing listed is available=0, not coming_soon.

A COLLAPSED LIST IS NOT A TRUNCATED ONE. "See 3 more available 1 Bed apartments", "Show 4 more", "Show less" and the like are DISPLAY toggles over rows that are already on the page: the N is a delta against what is currently visible, not extra inventory, and the rows it hides carry their own prices. Count every row the page contains, including those, and do not add N to your total. A count you can compare against is a COUNT — a filter tab reading "1 Bed (8)", or rentcafe's "14 available" over a list capped at 10. Measured on equityapartments.com 2026-09-18: 8 one-bed rows with a footer reading "See 3 more" (8 minus a display limit of 5), and the filter tab agreeing at 8.

HARD RULES:
1. If rent is not visible on the page, set rent_min/rent_max to null and flag_review=true on that group. Never guess.
2. If a value is gated behind a "Check Availability" form, login, phone, or email — DON'T extract a number, set flag_review=true.
3. FLOOR PRICES. If the price a plan shows is a lower bound — "Starting at $X", "from $X", "$X+" — that plan's rents are `rent_basis="starting_at"`, and so is every group any such plan lands in. Judge this from HOW THE PAGE PRICES, not from the numbers you end up with: after the GROUPING RULE merges two floor-priced plans, the group holds rent_min=<lowest floor>, rent_max=<highest floor> and looks like an ordinary range, but it is not one — rent_max is the highest FLOOR, never a maximum rent. Set flag_review=true on those groups too. Only use "actual" when the page prices real units a renter can lease at that number.
4. Per-unit listings: roll them up into floor plan groups by (beds, SF±50). Set `available` = count of LISTED units (a future move-in date still counts as available), `coming_soon` = only units explicitly labeled "coming soon" or advertised without a listing. ALSO copy each listed unit's own rent into `unit_rents`, ascending, the LOWEST 10 only (`rent_max` still carries the top of the range, so nothing is lost by stopping at 10). This is the ONLY place the individual numbers survive grouping.
4a. `unit_rents` IS COPIED, NEVER DERIVED. If the page prices the plan as a whole and not its units — "8 available, from $1,340", "$1,340 - $1,904", one price above a list of unpriced unit numbers — then there ARE no per-unit rents on that page: set `unit_rents` to null. Do NOT emit [rent_min, rent_max], do not interpolate, do not repeat one price N times. A downstream rule reads null as "the page published a range, use its low end" and a list as "these are measured rents, average the lowest five", so a reconstructed list turns a range into a fabricated average. Null is always the safe answer; a wrong list is not recoverable.
5. If the page shows NO pricing at all (e.g. "Join waitlist" only) → return {"units":[], "flag_review":true, "scrape_error":"no pricing visible"}.
6. "Plus fees" disclaimers without quantified fee amounts → keep rent_type="base", leave rent_allin null.
6a. TWO PRICES, BASE IS THE PRIMARY ONE. When a plan shows an all-in price AND a labelled base figure — "$1,653 /mo* ... $1,475 Base Rent", "Total Monthly Leasing Price $1,680-$2,580 / Monthly Base Rent $1,495-$2,395", "Starting at $1,383 incl. fees / Base Rent $1,248", "$1,103 - $3,253 / Base rent from $1,067" — the BASE figure goes in rent_min/rent_max and the all-in goes in rent_allin_min/rent_allin_max, with rent_type="base". The all-in is the larger number and the one the page leads with; leading with it is exactly why this is got wrong. rent_type="allin" is ONLY for a page that publishes no base figure anywhere.
7. Fee schedules are NOT concessions: application/admin fees, deposits, "first month's rent due at move-in", pet fees, package-locker fees → concession_weeks=null, raw_concession_text=null. A concession is a DISCOUNT ("X weeks/months free", "$Y off rent").
8. structure_type / stories / garage / baths / amenities are OBSERVED-ONLY — the same standard as rule 4a. Copy what the page's WORDS say and nothing else. NEVER infer them from square footage, bed count, bathroom count, rent level, unit count, or the community's name. No words => null. A 1,900 sqft 3BR is NOT evidence of "sfd"; the phrase "single-family homes" IS. "Attached garage" with no number is NOT evidence of 1 or 2. A community called "The Townes" is NOT evidence of "th" — its floor plans saying "townhome" is. If a page advertises BOTH ("1-story and 2-story available", "townhomes and apartments"), that is the community's range and not this plan's fact: return null.

9. plan_name is COPIED, NEVER SYNTHESISED — the same standard as rules 4a and 8. It is the page's own LABEL for the plan. "2BR/2BA", "2 Bed", "Two Bedroom", "1,100 sqft" are DESCRIPTIONS of a plan, not names of one: return null for those. If the GROUPING RULE merged units from two differently-named plans into one group, return null rather than picking one — a downstream matcher treats a name as strong evidence, so a wrong name is worse than no name.


WORKED EXAMPLES. Each shows a page's rendered text (abridged) and the ONLY acceptable output. Read the WHY lines: they are the rules above applied, and the mistakes named there are the ones you make most often.

--- Example 1: per-unit listings, named plans, a countable banner, a waitlisted plan ---
PAGE TEXT:
Move in by 9/30 and get 6 weeks free on select homes!
The Sonoran | 1 Bed | 1 Bath | 748 Sq. Ft. | 3 Available Available Now $1,395 Available Oct 4 $1,415 Available Now $1,450
The Mesa | 2 Bed | 2 Bath | 1,105 Sq. Ft. | 1 Available Available Now $1,890
The Verde | 3 Bed | 2 Bath | 1,340 Sq. Ft. | Join Waitlist
Amenities: Resort-style pool, 24-hour fitness center, Dog park, Gated community, Package lockers
OUTPUT:
{"concession_weeks":6,"raw_concession_text":"Move in by 9/30 and get 6 weeks free on select homes!","rent_type":"unspecified","units":[{"beds":1,"sf_min":748,"sf_max":748,"available":3,"coming_soon":0,"rent_min":1395,"rent_max":1450,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":[1395,1415,1450],"structure_type":null,"stories":null,"garage":null,"baths":1,"plan_name":"The Sonoran","flag_review":false},{"beds":2,"sf_min":1105,"sf_max":1105,"available":1,"coming_soon":0,"rent_min":1890,"rent_max":1890,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":[1890],"structure_type":null,"stories":null,"garage":null,"baths":2,"plan_name":"The Mesa","flag_review":false},{"beds":3,"sf_min":1340,"sf_max":1340,"available":0,"coming_soon":0,"rent_min":null,"rent_max":null,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":null,"structure_type":null,"stories":null,"garage":null,"baths":2,"plan_name":"The Verde","flag_review":true}],"amenities":["pool","gym","dog park","gated"],"flag_review":false,"scrape_error":null}
WHY: The page prices units one by one, so unit_rents is COPIED (rule 4) — three listed units, three rents, ascending; a future move-in date (Oct 4) still counts as available. "6 weeks free" is countable, so concession_weeks=6 and the banner is quoted verbatim. The Verde is on the page with no price, so it is emitted with available=0, null rents and flag_review=true (COVERAGE RULE + rule 1) — it is NOT skipped. structure_type stays null: "select homes" in a banner is not the page calling the product "single-family homes" (rule 8). "Package lockers" is not in the amenity list, so it is dropped; the four that are map to their list words.

--- Example 2: floor prices, a dollar-off promo, a fee schedule, townhomes with a garage ---
PAGE TEXT:
Townhomes for rent in Katy
2 Bedroom Townhome | 2.5 Bath | 1,250-1,310 sq ft | 2-car garage | Starting at $2,095 | 4 available
3 Bedroom Townhome | 2.5 Bath | 1,480 sq ft | 2-car garage | Starting at $2,395 | 2 available
$500 off your first month! Application fee $75. Admin fee $250. Pet fee $300.
Pet friendly. Community playground and walking trails.
OUTPUT:
{"concession_weeks":null,"raw_concession_text":"$500 off your first month!","rent_type":"unspecified","units":[{"beds":2,"sf_min":1250,"sf_max":1310,"available":4,"coming_soon":0,"rent_min":2095,"rent_max":2095,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"starting_at","unit_rents":null,"structure_type":"th","stories":null,"garage":2,"baths":2.5,"plan_name":null,"flag_review":true},{"beds":3,"sf_min":1480,"sf_max":1480,"available":2,"coming_soon":0,"rent_min":2395,"rent_max":2395,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"starting_at","unit_rents":null,"structure_type":"th","stories":null,"garage":2,"baths":2.5,"plan_name":null,"flag_review":true}],"amenities":["pet friendly","playground","walking trails"],"flag_review":false,"scrape_error":null}
WHY: "Starting at $X" is a FLOOR (rule 3): rent_basis="starting_at", rent_min=rent_max=the floor, flag_review=true on the group. Four units are available but the page never prices them individually, so unit_rents is null (rule 4a) — never [2095,2095,2095,2095] and never [rent_min,rent_max]. "$500 off" is a discount, so it is captured verbatim, but dollars cannot be normalized to weeks, so concession_weeks=null; the application, admin and pet fees are fees, not concessions (rule 7), and are left out of raw_concession_text. "2 Bedroom Townhome" DESCRIBES the plan, so plan_name is null (rule 9). The page's own word "Townhome" makes structure_type "th", and "2-car garage" makes garage=2; nothing says how many stories, so stories is null (rule 8).

--- Example 3: a page with no pricing at all ---
PAGE TEXT:
Now Leasing — Join our waitlist to be the first to know when pricing is released.
1, 2 & 3 bedroom apartment homes coming Spring 2027.
Call (555) 010-2000 or complete the form below for availability.
OUTPUT:
{"concession_weeks":null,"raw_concession_text":null,"rent_type":"unspecified","units":[],"flag_review":true,"scrape_error":"no pricing visible"}
WHY: Rule 5 — when NO price appears anywhere, return empty units, flag_review=true and the fixed error string. Do not invent bed groups with null rents here: rule 5 is the one case where an empty list is the correct, complete answer. Do not call the phone number, do not guess from "coming Spring 2027" that units are coming_soon.

--- Example 4: base and all-in shown side by side, plans priced as ranges, a vague banner ---
PAGE TEXT:
A1 | Studio | 1 BA | 520 sq ft | Base rent $1,240 - $1,310 | All-in $1,395 - $1,465 | 5 Available
B2 | 2 BR | 2 BA | 980 - 1,020 sq ft | Base rent $1,780 - $1,940 | All-in $1,935 - $2,095 | 2 Available
C1 | 3 BR | 2 BA | 1,260 sq ft | Coming Soon
Up to 8 weeks free on select apartments. Restrictions apply.
EV charging stations. Smart home technology in every apartment. Pickleball courts.
OUTPUT:
{"concession_weeks":null,"raw_concession_text":"Up to 8 weeks free on select apartments. Restrictions apply.","rent_type":"base","units":[{"beds":0,"sf_min":520,"sf_max":520,"available":5,"coming_soon":0,"rent_min":1240,"rent_max":1310,"rent_allin_min":1395,"rent_allin_max":1465,"rent_basis":"actual","unit_rents":null,"structure_type":"mf","stories":null,"garage":null,"baths":1,"plan_name":"A1","flag_review":false},{"beds":2,"sf_min":980,"sf_max":1020,"available":2,"coming_soon":0,"rent_min":1780,"rent_max":1940,"rent_allin_min":1935,"rent_allin_max":2095,"rent_basis":"actual","unit_rents":null,"structure_type":"mf","stories":null,"garage":null,"baths":2,"plan_name":"B2","flag_review":false},{"beds":3,"sf_min":1260,"sf_max":1260,"available":0,"coming_soon":1,"rent_min":null,"rent_max":null,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":null,"structure_type":"mf","stories":null,"garage":null,"baths":2,"plan_name":"C1","flag_review":true}],"amenities":["ev charging","smart home","pickleball"],"flag_review":false,"scrape_error":null}
WHY: Both a base and an all-in figure are displayed, so rent_type="base" and rent_allin_min/max are populated — the ONLY situation they are. A studio is beds=0. Each plan is priced as a RANGE with several units, so unit_rents is null (rule 4a) even though five units are available. "Up to 8 weeks" is vague, so the text is captured and concession_weeks is null. C1 is explicitly "Coming Soon": available=0, coming_soon=1, null rents, flagged. "A1"/"B2"/"C1" are the page's own labels, so they are plan names; "apartment" in the page's words makes structure_type "mf".

--- Example 5: more than ten listed units, and a plan whose units come from two named plans ---
PAGE TEXT:
1 Bedroom Apartments
Aspen A1 | 1 BR | 1 BA | 690 sq ft: $1,310, $1,325, $1,325, $1,340, $1,355, $1,360
Birch A2 | 1 BR | 1 BA | 715 sq ft: $1,380, $1,395, $1,395, $1,410, $1,425, $1,450, $1,475
2 Bedroom Apartments
Cedar B1 | 2 BR | 2 BA | 1,050 sq ft: $1,790, $1,820
Waived application fee this weekend only.
OUTPUT:
{"concession_weeks":null,"raw_concession_text":null,"rent_type":"unspecified","units":[{"beds":1,"sf_min":690,"sf_max":715,"available":13,"coming_soon":0,"rent_min":1310,"rent_max":1475,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":[1310,1325,1325,1340,1355,1360,1380,1395,1395,1410],"structure_type":"mf","stories":null,"garage":null,"baths":1,"plan_name":null,"flag_review":false},{"beds":2,"sf_min":1050,"sf_max":1050,"available":2,"coming_soon":0,"rent_min":1790,"rent_max":1820,"rent_allin_min":null,"rent_allin_max":null,"rent_basis":"actual","unit_rents":[1790,1820],"structure_type":"mf","stories":null,"garage":null,"baths":2,"plan_name":"Cedar B1","flag_review":false}],"amenities":null,"flag_review":false,"scrape_error":null}
WHY: Aspen A1 (690 sq ft) and Birch A2 (715 sq ft) are both 1BR within 50 sq ft of each other, so the GROUPING RULE merges them into one group: available=13, sf_min/sf_max span both plans, rent_min/rent_max span all thirteen rents, and unit_rents holds the LOWEST TEN ONLY, ascending, duplicates kept (rule 4) — rent_max still carries the top of the range, so nothing is lost. The merged group came from two differently named plans, so plan_name is null rather than a guess (rule 9); Cedar B1 stands alone and keeps its name. "Waived application fee" is a fee, not a discount, so both concession fields are null (rule 7).

Be concise. Return MINIFIED JSON only (single line, no indentation, no spaces after separators) — output length is capped and pretty-printing wastes it."""




def _visible_text(html: str) -> str:
    """Full visible text of the page INCLUDING header/hero (unlike _trim_html,
    which drops <header>) — so the concession scanner can see banner text."""
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup.find_all(("script", "style", "noscript")):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


def _concession_text(html: str) -> str:
    """The page text the concession scanner reads: `_visible_text`, except that a newline
    the scanner has a RULE for is kept.

    `concessions.scan_concession`'s D1 rule (`_SPLIT_OFFER`) decides a text node's own
    newline between a duration and "free": "<h2>6 weeks\\nFREE</h2>" joins to 6, while
    "Stay 6 months\\nFree rent for 2 months ... on our 1BR" is refused -- that "Free" heads
    its own, per-1BR offer. `_visible_text` collapses every whitespace run to a space, so
    both page-level sweeps never let D1 see the newline and stored 24 weeks community-wide.

    ONLY those newlines survive. Every other one is flattened exactly as before, because
    the rest of the scanner is written against one-line text: the reversed patterns will
    not cross a newline, so keeping them all lost "Free rent\\nfor 2 months", and falling back only when the kept-lines read found nothing
    still lost it beside a second offer ("... plus $500 off", Codex diff review P2).
    Strings are joined with a space, so no block boundary becomes a newline.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for t in soup.find_all(("script", "style", "noscript")):
        t.decompose()
    joined = soup.get_text(" ", strip=True)
    keep = [m.span(1) for m in _SPLIT_OFFER.finditer(joined)]
    out, at = [], 0
    for a, b in keep:
        out.append(re.sub(r"\s+", " ", joined[at:a]))
        out.append("\n")
        at = b
    out.append(re.sub(r"\s+", " ", joined[at:]))
    return "".join(out)

def _trim_html(html: str) -> str:
    """Reduce rendered HTML to the page's VISIBLE TEXT, one line per block.

    v2 (2026-07-15, golden-eval fix): the old version kept raw markup, which on
    React-heavy floor-plan pages runs 600-860K chars — the 60K price window then
    cut whole bed sections (Windsor lost studio+2BR+3BR; Berkshire lost 3BR →
    the MF llm-tail's 7/12 bed recall / 0/7 SF grade). The rendered visible text
    of those same pages is 5-14K chars: everything fits, nothing is windowed,
    and it's exactly what a renter (and the golden labeler) reads.

    Unlike v1 this KEEPS <header> (hero concession banners live there), and
    drops nav/footer/aside menus, scripts, and hidden machinery.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in DROP_TAGS:
        for t in soup.find_all(tag_name):
            t.decompose()

    # Drop only genuinely safe banner-style elements by EXACT id/class match.
    # Substring matching is dangerous — "card-header" / "section-nav" etc. live
    # inside main content on many sites.
    for el in list(soup.find_all(True)):
        if el.attrs is None:
            continue
        el_id = (el.get("id") or "").lower()
        el_classes = {c.lower() for c in (el.get("class") or [])}
        if el_id in DROP_BY_EXACT_ID or el_classes & set(DROP_BY_EXACT_CLASS):
            el.decompose()

    target = soup.find("body") or soup
    # One line per block element keeps row structure ("Plan A2 | 1 Bed | 748
    # Sq.Ft. | $1,395") readable without paying the markup tax.
    text = target.get_text(separator="\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    if len(text) <= MAX_HTML_CHARS:
        return text

    # If text exceeds budget, find the densest cluster of rent-price patterns
    # and window around it. Sliding-window — picks the MAX_HTML_CHARS-wide
    # window containing the most price hits. Handles spread-out price layouts
    # (Divi/WordPress sites where 50K+ of markup separates the first and last
    # price on the page).
    price_positions = [m.start() for m in PRICE_HINT_RE.finditer(text)]
    if price_positions:
        # Find the window of width MAX_HTML_CHARS containing the most price hits.
        # Walk through each price as a candidate window start; count how many
        # other prices fall within [start, start + MAX_HTML_CHARS].
        best_start = price_positions[0]
        best_count = 0
        for i, anchor in enumerate(price_positions):
            window_end = anchor + MAX_HTML_CHARS
            count = sum(1 for p in price_positions[i:] if p < window_end)
            if count > best_count:
                best_count = count
                best_start = anchor
        # Center the window around the matched prices: pad to the left so the
        # first matched price isn't right at offset 0 (LLM needs context).
        pad_left = min(2_000, best_start)
        start = max(0, best_start - pad_left)
        end = min(len(text), start + MAX_HTML_CHARS)
        prefix = "[...TRUNCATED_PRE...] " if start > 0 else ""
        suffix = " [...TRUNCATED_POST...]" if end < len(text) else ""
        return prefix + text[start:end] + suffix

    # No prices found at all — return head + tail to give LLM both nav context and footer disclaimers
    head = text[: MAX_HTML_CHARS - 5_000]
    tail = text[-5_000:]
    return head + " [...TRUNCATED_MIDDLE...] " + tail


# The LLM tier occasionally reads a SALE price (condo listings) or another non-rent
# number as MONTHLY rent — e.g. Mirador (AUS) came back $257k-$427k, the condo asking
# prices. No Sunbelt apartment rents monthly anywhere near this; the real downtown-luxury
# $10k-$37k reads come from the sightmap ADAPTER, which runs before this fallback tier and
# never reaches _coerce_result. So a value this high from the LLM is definitionally a
# non-rent number: null it + flag at the source. (dq rent_absurd_bounds is the backstop.)
_MAX_PLAUSIBLE_MONTHLY_RENT = 50_000

_RENT_BASES = ("actual", "starting_at", "unspecified")

# Any price on the page, and the subset of them rendered as a FLOOR.
_PRICE_RE = re.compile(r"\$\s?\d[\d,]{2,}")
_FLOOR_PRICE_RE = re.compile(
    r"""(?:
          \$\s?\d[\d,]{2,}(?:\.\d\d)?\s*\+          # "$1,474+"
        | \$\s?\d[\d,]{2,}(?:\.\d\d)?\s+and\s+up    # "$1,474 and up"
        | \b(?:from|starting\s+(?:at|from))\s+\$\s?\d[\d,]{2,}   # "from $1,474"
    )""",
    re.IGNORECASE | re.VERBOSE,
)


def mark_starting_at_basis(result: ExtractResult, visible_text: str) -> None:
    """Promote groups to rent_basis='starting_at' when the PAGE prices in floors.

    The model already had HARD RULE 3 and dropped it on San Palacio (PHX mf
    golden), so this does not depend on the model alone — the same
    fill-if-missing shape as merge_concession above.

    Deliberately conservative in two ways, because a false 'starting_at' is a
    rent range thrown away:
      - only groups the model left "unspecified" are touched (an explicit
        "actual" wins — a page can carry one marketing "from $X" above real
        per-unit prices);
      - floor-marked prices must be the DOMINANT pricing form on the page
        (a simple majority), not merely present.
    """
    prices = _PRICE_RE.findall(visible_text or "")
    floors = _FLOOR_PRICE_RE.findall(visible_text or "")
    if not floors or len(floors) * 2 < len(prices):
        return
    for g in result.units:
        if g.rent_basis != "unspecified":
            continue
        if g.rent_min is None and g.rent_max is None:
            continue           # nothing priced to mis-describe
        g.rent_basis = "starting_at"
        g.flag_review = True
        result.flag_review = True


# A plan the page advertises but cannot let today. Four things must sit close together for a
# match: a bed count, a "/ N Bath" (what marks a PLAN row rather than a section header or a
# filter tab), a square footage, and an explicit no-availability marker.
#
# "Contact Us" is deliberately absent from the markers. Osprey prints it on every AVAILABLE
# unit row ("Schedule Tour  Contact Us  Reserve Now"), so treating it as one would fold live
# inventory into the advertised set.
_NO_AVAIL_MARKER = (r"(?:notify\s*me|join\s+waitlist|waitlist|contact\s+for\s+availability"
                    r"|call\s+for\s+pricing|please\s+call)")
_ADVERTISED_PLAN_RE = re.compile(
    r"\b(?:(studio)|(\d+)\s*bed)\s*/\s*[\d.]+\s*bath\b"
    r".{0,40}?\b([\d,]{3,6})\s*sq\.?\s*ft\b"
    r".{0,40}?" + _NO_AVAIL_MARKER,
    re.I | re.S)


def widen_band_to_advertised_plans(result: ExtractResult, visible_text: str) -> None:
    """Stretch each bed group's SF band over the plans the page advertises but cannot let.

    `workflows/golden_eval_austin_bfr_mf_sop.md` § "SF-band convention" (2026-09-18): a bed
    group's sf_min/sf_max span EVERY floor plan the site advertises for that bed count,
    whether or not it is priced or bookable, and such a plan widens SF and moves nothing
    else. The COVERAGE RULE above says as much to the model, and the model half-obeys:
    ATL/mf Osprey came back with 2bd sf_min 866 (which IS B1PH, a Notify-Me plan, so it read
    the block) and sf_max 1188, the largest AVAILABLE unit, against a page advertising B8PH
    at 1,426 — one graded miss against `data/golden/atl_osprey_golden_2026-09-18.json`.

    A prompt line was tried first and was not enough: COVERAGE RULE 2 moved one live run to
    1328, further into the block and still short. Nor is the block being trimmed away —
    `_trim_html` keeps it, B8PH lands at char 8,159 of 15,067. So the page is re-read rather
    than the model trusted, the same shape and the same reason as `mark_starting_at_basis`.

    Only ever WIDENS, and only sf_min/sf_max. Never rent, `available` or `coming_soon`:
    those plans have no rent a renter can pay and no inventory, which is the convention
    itself. A group whose SF the model could not read at all stays null — that is "could not
    read it", not a band to fill in from one side. A bed group the model never emitted is not
    invented here: emitting advertised-only plans as their own `available: 0` groups is what
    the SOP ultimately wants, but that reshapes the group set across every LLM-tail community
    and needs its own measured change, the way `rentcafe._merge_advertised` got one.
    """
    if not visible_text:
        return
    by_beds: dict[int, list[int]] = defaultdict(list)
    for m in _ADVERTISED_PLAN_RE.finditer(visible_text):
        beds = 0 if m.group(1) else int(m.group(2))
        by_beds[beds].append(int(m.group(3).replace(",", "")))
    if not by_beds:
        return
    for g in result.units:
        sfs = by_beds.get(g.beds)
        if not sfs or g.sf_min is None or g.sf_max is None:
            continue
        g.sf_min = min(g.sf_min, min(sfs))
        g.sf_max = max(g.sf_max, max(sfs))


# ONE PLAN'S TWO PRICES. A page that publishes both an all-in figure and a labelled base
# figure prints them next to each other, in either order and in either direction:
#   "$1,653 /mo* Only 1 left! $1,475 Base Rent"       (Foxtail — all-in, then labelled base)
#   "Starting at $1,383 incl. fees Base Rent $1,248"  (Cortland Paseo — label, then figure)
#   "$1,680 - $2,580/MO* Monthly Base Rent: $1,495 - $2,395"   (YardHomes — both as RANGES)
#   "Base Rent $1,500 Total Monthly Price $1,700"     (base FIRST — scanned in reverse)
# The label may reach its figure through a word: "Base Rent Starting From $968" (Canyon
# Vistas), "Base rent from $1,067" (Greystar).
#
# CENTS ARE NOT OPTIONAL TO READ. altawildhorseranch.com prices "$1,832.16 - $2,072.16
# /mo* 12 months $1,785 - $2,025 Base Rent". A pattern that stops at the comma group reads
# "$1,832", fails to carry the range across ".16 - $2,072.16", resumes at $2,072 and pairs
# it with $1,785 — the base of $1,832. Cents are captured and ROUNDED, the same
# normalisation `adapters/base.py observed_rents` applies (`int(round(float(v)))`), so a
# key built here matches the integer the model stored instead of sitting a dollar below it.
# A price is properly comma-grouped or plainly digits — "$1,5" is a TRUNCATED "$1,500",
# not fifteen dollars. `_CARD_TAIL` cuts the last region at a fixed width and used to hand
# exactly that to a pattern of `\d[\d,]{2,6}`, which accepted it; `_dollars` then stripped
# the comma and returned 15, and a group was silently rewritten to a $15 rent.
_MONEY = r"\$\s?(?P<%s>\d{1,3}(?:,\d{3})+|\d{3,6})(?:\.(?P<%s_c>\d{1,2}))?"
_DASH = r"\s*(?:[-–—]|to)\s*"
_BASE_RENT_LABEL = r"(?:total\s+)?(?:monthly\s+)?base\s+rent"
_ALLIN_LABEL = r"total\s+monthly\s+(?:leasing\s+)?price"
_LABEL_TO_FIGURE = r"\b[:\s]*(?:starting\s+)?(?:from\s+)?"

# A PAIR MUST DESCRIBE ONE PLAN, and after `_visible_text` there is no newline left to
# prove it — every `\s+` is collapsed to a single space (extractor.py:717), so a `[^\n]`
# gap constrains NOTHING and "no dollar sign in between" only says no OTHER price
# intervened. It does not say the two prices belong to the same unit.
#
# rent-beckwellsbranch.com publishes one labelled base price per plan and no all-in at all:
#   "A1 1 Bed | 1 Bath 698 SQ FT Base Rent $1,043 Schedule a Tour 10 Available
#    A2 1 Bed | 1 Bath 799 SQ FT Base Rent $1,047"
# and the old pattern paired A1's $1,043 with A2's $1,047 straight across the row boundary.
# That page survived only because $1,047 > $1,043 tripped the base-below-all-in guard —
# arithmetic luck, not a guard. Reverse the listing order and the false pair stands.
#
# So the gap is bounded twice, both sized from the corpus rather than guessed. Over the
# 270-page replay run every one of the 883 accepted pairs has a gap of at most 36
# characters and NOT ONE contains a plan-row token; the only gaps that do are Beck's three
# cross-row matches. "available" is deliberately NOT a row token — Canyon Vistas prints
# "Starting $1,206 * Available Now Base Rent Starting From $968" inside a legitimate pair.
_MAX_PAIR_GAP = 40
_ROW_BOUNDARY_RE = re.compile(
    r"\b(?:bed|beds|bath|baths|ba|br)\b|\bsq\.?\s*ft\b|\bsqft\b|\bunit\b"
    r"|\bschedule\s+a\s+tour\b|\bview\s+(?:unit|details|floor)|\bapply\b|\bfloor\s*plan",
    re.IGNORECASE)
_GAP = r"(?P<gap>(?:(?!\$).){0,%d}?)" % _MAX_PAIR_GAP

# Direction 1 — the all-in figure first, the labelled base second.
_ALLIN_THEN_BASE_RE = re.compile(
    (_MONEY % ("a_lo", "a_lo")) + r"(?:" + _DASH + (_MONEY % ("a_hi", "a_hi")) + r")?"
    + _GAP +
    r"(?:"
    + _BASE_RENT_LABEL + _LABEL_TO_FIGURE
    + (_MONEY % ("b1_lo", "b1_lo")) + r"(?:" + _DASH + (_MONEY % ("b1_hi", "b1_hi")) + r")?"
    r"|"
    + (_MONEY % ("b2_lo", "b2_lo")) + r"(?:" + _DASH + (_MONEY % ("b2_hi", "b2_hi")) + r")?"
    r"\s*" + _BASE_RENT_LABEL + r"\b"
    r")",
    re.IGNORECASE)

# Direction 2 — the labelled base FIRST, the all-in second, which is how some cards read:
#   "A Base Rent $1,500 Total Monthly Price $1,700"
# The all-in must carry its own label here: without one there is nothing to say the price
# after a base figure is this plan's total rather than the next thing on the page.
#
# THIS PATTERN IS ONLY EVER RUN INSIDE ONE PLAN CARD. Run page-wide (as it briefly was, in
# an earlier change) it invents "A Base Rent $1,500 B Total Monthly Price $1,700" -> {1700: 1500}
# across a card boundary and manufactures false contradictions that SUPPRESS valid
# corrections. Confined to a card, neither is reachable — a card has one plan in it.
_BASE_THEN_ALLIN_RE = re.compile(
    _BASE_RENT_LABEL + _LABEL_TO_FIGURE
    + (_MONEY % ("b_lo", "b_lo")) + r"(?:" + _DASH + (_MONEY % ("b_hi", "b_hi")) + r")?"
    + _GAP + _ALLIN_LABEL + _LABEL_TO_FIGURE
    + (_MONEY % ("a_lo", "a_lo")) + r"(?:" + _DASH + (_MONEY % ("a_hi", "a_hi")) + r")?",
    re.IGNORECASE)

_BASE_RENT_PAIR_RE = _ALLIN_THEN_BASE_RE


def _dollars(whole: Optional[str], cents: Optional[str] = None) -> Optional[int]:
    """Whole dollars, ROUNDED — the same normalisation `observed_rents` applies."""
    if not whole:
        return None
    n = float(whole.replace(",", ""))
    if cents:
        n += int(cents.ljust(2, "0")) / 100.0
    return int(round(n))


# No rent on any page in the corpus is under $930, and the lowest all-in is $1,015. A
# figure below this is a deposit, a fee, an amenity price or a truncation — never the rent
# this hook is correcting. It is the same judgement `_MAX_PLAUSIBLE_MONTHLY_RENT` makes at
# the other end, and it is what stops "$1,700 Base Rent $100 Deposit" reading $100 as base.
_MIN_PLAUSIBLE_MONTHLY_RENT = 500


def _dollar_keys(whole: Optional[str], cents: Optional[str] = None) -> tuple[int, ...]:
    """Every integer a reader might have recorded this price as — rounded AND truncated.

    A lookup key here has to match the integer the MODEL already stored, and the model
    truncates: The Chloe Leander prints "$1,095.56 /mo* 20 months $1,044 Base Rent" and
    comes back with rent_min 1095, while Alta Wildhorse's "$2,072.16" comes back 2072.
    Normalising the page to 1096 on the grounds that `observed_rents` rounds would be
    internally tidy and would simply stop matching — it silently dropped the correction on
    both Chloe pages when tried. So the map is keyed BOTH ways and the VALUE it yields is
    the rounded one, which is the figure this hook is writing into the row.
    """
    r = _dollars(whole, cents)
    if r is None or r < _MIN_PLAUSIBLE_MONTHLY_RENT:
        return ()
    f = int(float(whole.replace(",", "")))
    return (r,) if f == r else (r, f)


def _pair_observations(visible_text: str) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    """Every (all-in, base) the page states, from BOTH scan directions, ungrouped.

    A pair priced as two RANGES contributes two observations (low->low, high->high); a
    pair of single prices contributes one. Nothing is deduplicated or resolved here — the
    caller needs the raw multiset, because an ambiguity is only visible before collapsing.
    """
    out: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for rx in (_ALLIN_THEN_BASE_RE, _BASE_THEN_ALLIN_RE):
        for m in rx.finditer(visible_text or ""):
            gd = m.groupdict()
            if _ROW_BOUNDARY_RE.search(gd.get("gap") or ""):
                continue        # the gap crossed into another plan's row
            a_lo = _dollar_keys(gd.get("a_lo"), gd.get("a_lo_c"))
            a_hi = _dollar_keys(gd.get("a_hi"), gd.get("a_hi_c"))
            if rx is _BASE_THEN_ALLIN_RE:
                b_lo = _dollar_keys(gd.get("b_lo"), gd.get("b_lo_c"))
                b_hi = _dollar_keys(gd.get("b_hi"), gd.get("b_hi_c"))
            else:
                b_lo = _dollar_keys(gd.get("b1_lo") or gd.get("b2_lo"),
                                    gd.get("b1_lo_c") if gd.get("b1_lo") else gd.get("b2_lo_c"))
                b_hi = _dollar_keys(gd.get("b1_hi") or gd.get("b2_hi"),
                                    gd.get("b1_hi_c") if gd.get("b1_hi") else gd.get("b2_hi_c"))
            for a, b in ((a_lo, b_lo), (a_hi, b_hi)):
                # A "base" at or above the all-in price is not this layout — it is a
                # misread (a deposit, a fee table, a neighbouring plan), and swapping it
                # would INVENT a rent increase. Only a strictly cheaper base is a base.
                if a and b and b[0] < a[0]:
                    out.append((a, b))
    return out


def _resolve_pairs(visible_text: str) -> tuple[dict[int, int], dict[int, int]]:
    """Both lookup directions for ONE SCOPE (a plan card), ambiguity resolved against the
    RAW observations of that scope.

    Returns `(base_by_allin, allin_by_base)`.

    Three ways a number is refused, and all three are read off the same multiset — which
    is the point. Filtering one direction first and deriving the other from the survivors
    destroys the evidence the second direction needs: a page stating 1653->1475,
    1700->1475 and 1700->1500 drops 1700 as an ambiguous ALL-IN, and a reverse map built
    from what is left then says 1475's all-in is 1653, although the page also paired 1475
    with 1700.

      - an all-in the page pairs with two different base figures is not a key;
      - a base the page pairs with two different all-in figures is not a key;
      - a number the page uses BOTH as an all-in and as a base — one plan's total is
        another plan's base — is refused in both directions. Membership in one map would
        otherwise decide which branch runs on nothing but a numeric coincidence.
    """
    return _resolve_observations(_pair_observations(visible_text))


def _observation_keys(obs):
    """Every (all-in, base) canonical pair an observation set states, before resolution."""
    for a_keys, b_keys in obs:
        yield a_keys[0], b_keys[0]


def _resolve_observations(obs) -> tuple[dict[int, int], dict[int, int]]:
    """The ambiguity rules, applied to whatever scope's observations are handed in."""
    fwd: dict[int, set[int]] = defaultdict(set)
    rev: dict[int, set[int]] = defaultdict(set)
    for a_keys, b_keys in obs:
        # Every key variant of one side maps to the CANONICAL (rounded) other side, so a
        # price written to the cent never contradicts itself across its own two spellings.
        for a in a_keys:
            fwd[a].add(b_keys[0])
        for b in b_keys:
            rev[b].add(a_keys[0])
    both = set(fwd) & set(rev)
    base_by_allin = {a: next(iter(bs)) for a, bs in fwd.items()
                     if len(bs) == 1 and a not in both}
    allin_by_base = {b: next(iter(as_)) for b, as_ in rev.items()
                     if len(as_) == 1 and b not in both}
    return base_by_allin, allin_by_base


_LABELLED_ALLIN_RE = re.compile(
    _ALLIN_LABEL + _LABEL_TO_FIGURE + (_MONEY % ("p", "p"))
    + r"(?:" + _DASH + (_MONEY % ("q", "q")) + r")?",
    re.IGNORECASE)


def _labelled_allin_figures(visible_text: str) -> set[int]:
    """Every figure the page explicitly LABELS as an all-in price, paired or not.

    Absence from `base_by_allin` means "no pairing was ACCEPTED for this number", NOT
    "this number is never an all-in price". A plan that
    advertises a total with no base beside it contributes no pair, so without this the
    RELABEL branch would read another plan's base figure of the same value as proof that
    this group's genuine all-in rent is really a base rent, and certify it.
    """
    out: set[int] = set()
    for m in _LABELLED_ALLIN_RE.finditer(visible_text or ""):
        gd = m.groupdict()
        for whole, cents in (("p", "p_c"), ("q", "q_c")):
            out.update(_dollar_keys(gd.get(whole), gd.get(cents)))
    return out


# ------------------------------------------------------------------ plan cards
#
# THE UNIT OF EVIDENCE IS A PLAN CARD, NOT THE PAGE. Every defect this hook has had was a
# variant of one mistake: treating a number that appears somewhere on the page as evidence
# about a particular bed group. `_visible_text` collapses the markup to one line, so the
# card boundaries a reader sees are gone and a pattern can pair one plan's total with the
# next plan's base — silently, and with a number nobody advertised.
#
# What survives the collapse is the SQUARE FOOTAGE. Every page in the corpus prints one per
# plan, and prints it before that plan's prices: "A1 1 Bed | 1 Bath 698 SQ FT Base Rent
# $1,043", "Barton Creek 1 Bed | 1 Bath 575 sq. ft. Starting at $1,383 incl. fees Base Rent
# $1,248", "Style A 1-Bed 1-Bath 635 SQ FT 12 Homes Available $1,680 - $2,580/MO*". So the
# page is cut at each square-footage token and everything up to the NEXT one is that plan's
# card. Prices are then paired only within a card, and a card is matched to a bed group by
# its own geometry rather than by its numbers.
#
# This is what makes the two pathological layouts safe rather than merely unlikely:
#   - Beck at Wells Branch prints a labelled base per plan and no all-in at all; each card
#     holds ONE base figure and no total, so no pair can form;
#   - a card that prints its base BEFORE its total is read correctly, because the reverse
#     pattern can no longer reach out of the card to grab a neighbour's price.
_SQFT_ANCHOR_RE = re.compile(
    r"([\d,]{3,6})\s*(?:sq\.?\s*ft|sqft|square\s+feet)\b", re.IGNORECASE)
# "1 Bed", "1-Bed", "Studio", "2 BR" — read from the text just BEFORE the square footage,
# which is where every layout in the corpus puts it...
_CARD_BEDS_RE = re.compile(r"(studio)|(\d+)\s*[-\s]?\s*(?:bedrooms?|beds?|br|bd)\b", re.IGNORECASE)
_CARD_WORD_BEDS_RE = re.compile(r"\b(studio|one|two|three|four|five)\s+bed", re.IGNORECASE)
_WORD_BEDS = {"studio": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
# ...except greystar, which puts a bare "<beds> <baths>" immediately AFTER it:
#   "A1 528 sq. ft. 1 1 $1,103 - $3,253 Base rent from $1,067"
_CARD_BEDS_AFTER_RE = re.compile(r"^[\s.,|-]*(\d)\s+(\d)(?:\s|$)")
_CARD_LOOKBACK = 60
# A card with no following anchor is bounded rather than run to the end of the document,
# so a page's footer cannot join the last plan.
_CARD_TAIL = 400


@dataclass
class _PlanCard:
    """One plan anchor: its geometry, and the two text regions it borders.

    A page puts a plan's descriptor on ONE side of its prices, and which side is the
    template's choice, not a convention:

      Foxtail      "#2708RR-3A Available Now 1 bed 1 bath 659 sq. ft. Floorplan layout:
                    Elm - 80 $1,653 /mo* Only 1 left! $1,475 Base Rent"   -> prices AFTER
      Windsor      "$2,523 /mo $2,433 Base Rent 11 Mo. Avail. Sep 27 View Details Apply
                    1 Bed 1 Bath 821 Sq. Ft. A4 Windsor on the Lake"      -> prices BEFORE

    Reading only forwards attributes every Windsor unit's prices to the PREVIOUS plan and
    stops correcting the whole Windsor family plus Cortland Bluff Springs — 16 real groups.
    Reading BOTH sides fixes that and re-opens the hole this redesign exists to close: the
    region before anchor i is the region after anchor i-1, so every plan also inherits its
    neighbour's prices. `_page_orientation` therefore picks ONE side for the whole page.
    """
    sqft: int
    beds: Optional[int]
    before: str
    after: str

    @property
    def body(self) -> str:
        """Both bordered regions, for callers that only want this anchor's evidence."""
        return self.before + " " + self.after


def _plan_cards(visible_text: str) -> list[_PlanCard]:
    """Cut the page at each square-footage token and hand each anchor its two neighbours."""
    if not visible_text:
        return []
    anchors = list(_SQFT_ANCHOR_RE.finditer(visible_text))
    if not anchors:
        return []
    # region i is the text between anchor i-1 and anchor i; region len(anchors) is the tail.
    regions: list[str] = []
    for i, m in enumerate(anchors):
        prev_end = anchors[i - 1].end() if i else max(0, m.start() - _CARD_TAIL)
        regions.append(visible_text[prev_end:m.start()])
    last = anchors[-1]
    regions.append(visible_text[last.end():min(len(visible_text), last.end() + _CARD_TAIL)])

    cards: list[_PlanCard] = []
    for i, m in enumerate(anchors):
        look = visible_text[max(0, m.start() - _CARD_LOOKBACK):m.start()]
        beds = None
        wm = list(_CARD_WORD_BEDS_RE.finditer(look))
        if wm:
            beds = _WORD_BEDS[wm[-1].group(1).lower()]
        else:
            bm = list(_CARD_BEDS_RE.finditer(look))
            if bm:
                beds = 0 if bm[-1].group(1) else int(bm[-1].group(2))
            else:
                am = _CARD_BEDS_AFTER_RE.match(regions[i + 1])
                if am:
                    beds = int(am.group(1))
        try:
            sqft = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        cards.append(_PlanCard(sqft, beds, regions[i], regions[i + 1]))
    return cards


def _cards_for_group(cards: list[_PlanCard], g: UnitGroup) -> list[_PlanCard]:
    """The anchors whose OWN geometry places them in this bed group.

    Matched on square footage against the group's band, widened by the same +/-50 the
    GROUPING RULE uses to build the band in the first place, and on bed count when both
    sides state one. A group the model gave no SF cannot be matched to an anchor and is
    left alone: that is "no evidence", which is the answer this hook owes it.
    """
    if g.sf_min is None or g.sf_max is None:
        return []
    lo, hi = g.sf_min - 50, g.sf_max + 50
    return [c for c in cards
            if lo <= c.sqft <= hi
            and (c.beds is None or g.beds is None or c.beds == g.beds)]


_LABELLED_BASE_RE = re.compile(
    r"(?:" + _BASE_RENT_LABEL + _LABEL_TO_FIGURE + (_MONEY % ("p", "p"))
    + r"(?:" + _DASH + (_MONEY % ("q", "q")) + r")?"
    # "$1,475 Base Rent" — but NOT "$1,103 - $3,253 Base rent from $1,067", where the
    # label points FORWARD at its own figure and the prices before it are the all-in.
    r"|" + (_MONEY % ("r", "r")) + r"(?:" + _DASH + (_MONEY % ("t", "t")) + r")?"
    r"\s*" + _BASE_RENT_LABEL + r"\b(?!" + _LABEL_TO_FIGURE + r"\$))",
    re.IGNORECASE)


def _labelled_base_figures(text: str) -> set[int]:
    """Every figure this text explicitly LABELS as a base rent, paired or not."""
    out: set[int] = set()
    for m in _LABELLED_BASE_RE.finditer(text or ""):
        gd = m.groupdict()
        for whole in ("p", "q", "r", "t"):
            out.update(_dollar_keys(gd.get(whole), gd.get(whole + "_c")))
    return out


def _pairs_in_card(card_body: str) -> dict[int, int]:
    """The all-in -> labelled-base lookup for ONE card (the swap direction).

    There is deliberately no page-wide version of this any more. A page-wide map was the
    root cause of every wrong rent this hook produced, and leaving one importable is
    leaving the footgun loaded.
    """
    return _resolve_pairs(card_body)[0]


def prefer_labelled_base_rent(result: ExtractResult, visible_text: str) -> None:
    """Put BASE rent in the primary fields when the page shows base AND all-in together.

    `adapters/base.py` UnitGroup is explicit: rent_min/rent_max are "the primary rent as
    displayed" and rent_allin_min/max are "populated ONLY when a site shows BOTH base and
    all-in side by side". The prompt tells the model the same thing three times — the
    schema comment on rent_allin_min, HARD RULE 6, and WORKED EXAMPLE 4, whose page text
    is this exact layout.

    claude-haiku-4-5 does it backwards anyway. Replay `20260922T025925Z-ed7ab6` ran
    production Haiku and gpt-5.6-luna over the SAME 270 real pages, same prompt, same
    parser: Haiku stored the all-in figure as the primary rent on 35 of them
    (rent_type='allin', rent_allin_* left null), median overstatement $146/mo, range
    $51-$204; Luna recorded base as primary on every one. So the prompt is not the layer
    that can fix this — the page is re-read instead, the same shape and the same reason as
    `mark_starting_at_basis` (HARD RULE 3) and `widen_band_to_advertised_plans`
    (COVERAGE RULE 2) beside it.

    Runs first among the post-hooks so the primary rent is right before anything else
    reads it. That is ordering hygiene, NOT a dependency: `scan_concession` accepts a
    `monthly_rent` hint and ignores it in every branch (concessions.py:456), and the other
    three hooks read markers, square footage and banner text rather than rent values.

    Conservative by construction — a wrong swap invents a discount that is not on the page:
      - only groups the model itself marked rent_type='allin' are touched; 'base' and
        'unspecified' are left exactly as they are;
      - a group that already carries rent_allin_min/max is the model getting it right in
        its own way, and is not overwritten;
      - BOTH endpoints must resolve to a base figure the page pairs with that exact all-in
        number. One unmatched endpoint means no swap at all, never a half-swap;
      - the base figure must be strictly below the all-in one, and the swapped range must
        stay ordered.

    `unit_rents` are per-unit ALL-IN numbers on such a page, and HARD RULE 4a's downstream
    contract reads a list as "measured rents, average the lowest five". Leaving them under
    a base rent_min/max is worse than the defect, so they are translated through the same
    map; if even one cannot be, the list goes null and the group is flagged — rule 4a's own
    answer, "Null is always the safe answer; a wrong list is not recoverable".

    No flag on a clean swap. The correction is an exact-value match read off the page, and
    flagging 35 pages a week would spend `rents_dq.flag_review_ceiling` on rows that are
    now RIGHT — the same argument test_extractor_flags.py makes for not flagging everything.
    """
    if not visible_text:
        return
    cards = _plan_cards(visible_text)
    if not cards:
        # No square-footage anchor anywhere means no card boundaries can be established,
        # and without them a price is just a number that appears on the page. Decline.
        return
    for g in result.units:
        if g.rent_type != "allin":
            continue
        if g.rent_min is None or g.rent_max is None:
            continue

        # EVIDENCE IS SCOPED TO THIS GROUP'S OWN CARDS. Pairs are read inside each card, so
        # one plan's total can never be paired with another plan's base; the cards are then
        # matched to the group by geometry, so a numeric coincidence elsewhere on the page
        # cannot reach it. A group with no matching card gets no correction.
        mine = _cards_for_group(cards, g)
        if not mine:
            continue
        obs = []
        labelled_allin: set[int] = set()
        labelled_base: set[int] = set()
        unpriced_allin = False
        for c in mine:
            for region in (c.before, c.after):
                obs.extend(_pair_observations(region))
                region_allin = _labelled_allin_figures(region)
                region_base = _labelled_base_figures(region)
                labelled_allin |= region_allin
                # A figure this group's own card labels as base rent is the strongest
                # evidence on the page about that number, and it outranks any pair a
                # neighbouring region offers for the same value.
                labelled_base |= region_base
                # A region that labels THIS group's rent an all-in and shows no base figure
                # at all is the page saying this plan publishes no base rent. A pair for the
                # same number from the plan next door is a coincidence, not a correction.
                if (g.rent_min in region_allin or g.rent_max in region_allin) and not region_base:
                    unpriced_allin = True
        base_by_allin, allin_by_base = _resolve_observations(obs)
        dropped_allin = {a for a, _ in ((k, v) for k, v in _observation_keys(obs))
                         if a not in base_by_allin}
        if not base_by_allin and not allin_by_base:
            continue

        # (a) THE LABEL IS THE ONLY THING WRONG. Both endpoints are figures the page
        # LABELS as base rent and neither is an all-in figure anywhere on it, so the
        # stored rents are already right — Whitley Austin reads "$2,938 /mo* 15 months
        # $2,800 Base Rent" and returns 2800 under rent_type='allin'. Move the label and
        # fill the empty all-in columns; never the rents, and never unit_rents, which are
        # base too. `_resolve_pairs` has already refused any number the page uses in BOTH
        # roles, so reaching this branch is evidence, not a numeric coincidence.
        if (g.rent_min not in base_by_allin and g.rent_max not in base_by_allin
                and g.rent_min not in labelled_allin and g.rent_max not in labelled_allin):
            up_min, up_max = allin_by_base.get(g.rent_min), allin_by_base.get(g.rent_max)
            if up_min is not None and up_max is not None and up_min <= up_max:
                log.info(f"    base rent stored under rent_type='allin'; relabelling "
                         f"beds={g.beds} {g.rent_min}-{g.rent_max} (all-in {up_min}-{up_max})")
                if g.rent_allin_min is None or g.rent_allin_min == g.rent_min:
                    g.rent_allin_min = up_min
                if g.rent_allin_max is None or g.rent_allin_max == g.rent_max:
                    g.rent_allin_max = up_max
                g.rent_type = "base"
            continue

        # (b) THE RENT ITSELF IS THE ALL-IN FIGURE — the swap.
        # Unless this group's OWN card labels that very number a base rent. A neighbouring
        # plan's total can coincide with this plan's base, and a page-wide key would then
        # "correct" a correct rent down to the neighbour's base. The
        # group's own card is the better witness, so the swap yields to it.
        if g.rent_min in labelled_base or g.rent_max in labelled_base or unpriced_allin:
            continue
        # An all-in column equal to the primary is the model echoing itself rather than
        # reading a second price off the page (Alta Wildhorse returns that on every
        # priced plan), so it carries no information and must not block the correction.
        # An all-in that genuinely DIFFERS is the model's own reading, and it wins.
        if ((g.rent_allin_min is not None and g.rent_allin_min != g.rent_min)
                or (g.rent_allin_max is not None and g.rent_allin_max != g.rent_max)):
            continue
        base_min = base_by_allin.get(g.rent_min)
        base_max = base_by_allin.get(g.rent_max)
        if base_min is None or base_max is None or base_min > base_max:
            continue

        # THE RANGE IS THE EXTREMA OF EVERY PAIR IN THE BAND, not of its two endpoints.
        # Fees differ between units, so the plan whose all-in sits in the middle of the
        # group's range can carry the CHEAPEST base on the page: three cards at
        # 1600->1100, 1650->1000, 1700->1150 translate to a 1100-1150 range that excludes
        # the advertised 1000. Every pair whose all-in falls inside the group's own
        # all-in range describes a unit of this group, so all of them set the bounds.
        in_band = {a: b for a, b in base_by_allin.items() if g.rent_min <= a <= g.rent_max}
        if in_band:
            base_min = min(base_min, min(in_band.values()))
            base_max = max(base_max, max(in_band.values()))

        # A fee that is not constant across the band means the ORDER of units by all-in is
        # not their order by base. Read with a $1 tolerance, because a cent price is keyed
        # under two spellings and a constant $500.56 fee would otherwise read as 500 and 501.
        band = sorted({a - b for a, b in in_band.items()})
        varies = bool(band) and (band[-1] - band[0]) > 1
        # An all-in inside the band that was DROPPED as ambiguous is itself evidence the
        # fee varies — the resolver removed exactly the disagreement this check looks for,
        # so treating its absence as "constant" reads a filtered map as a complete one.
        if any(g.rent_min <= a <= g.rent_max for a in dropped_allin):
            varies = True

        if g.unit_rents:
            translated = [base_by_allin.get(r) for r in g.unit_rents]
            # THE LOWEST TEN BY ALL-IN ARE NOT THE LOWEST TEN BY BASE when the fee load
            # differs between units. HARD RULE 4 caps the list at ten members, so a group
            # whose 11th unit carries a bigger fee has its true cheapest base rent missing
            # from the list entirely — and `rents.market_rent()` averages the LOWEST FIVE
            # of it. Re-sorting cannot recover a member that was never there.
            #
            # This is checked BEFORE the untranslatable-member branch: if the fee varies
            # across a capped list, nulling the members and keeping a range derived from
            # the same map writes bounds that are wrong in the same way.
            if len(g.unit_rents) >= 10 and varies:
                g.flag_review = True
                result.flag_review = True
                log.info(f"    declining base-rent swap: capped unit_rents over a varying "
                         f"fee, beds={g.beds} {g.rent_min}-{g.rent_max}")
                continue
            if any(t is None for t in translated):
                # HARD RULE 4a's own answer: null is always the safe answer, a wrong list
                # is not recoverable. The bounds stand because they come from the band.
                g.unit_rents = None
                g.flag_review = True
                result.flag_review = True
            else:
                g.unit_rents = sorted(translated)
                # `the project rules:62` defines unit_rents as the inputs to these order
                # statistics, so a member outside the range is incoherent — Windsor Ridge
                # shipped 1404-1464 holding [1404, 1464, 1605].
                base_min = min(base_min, g.unit_rents[0])
                base_max = max(base_max, g.unit_rents[-1])

        log.info(f"    all-in stored as primary; page labels base rent -> "
                 f"beds={g.beds} {g.rent_min}-{g.rent_max} becomes {base_min}-{base_max}")
        g.rent_allin_min, g.rent_allin_max = g.rent_min, g.rent_max
        g.rent_min, g.rent_max = base_min, base_max
        g.rent_type = "base"


def _looks_reconstructed(unit_rents: list[int], rent_min, rent_max, available: int) -> bool:
    """True when `unit_rents` is the group's own range echoed back as two members.

    HARD RULE 4a tells the model not to do this, and prompt rules are the layer
    this repo has repeatedly had to backstop in code — rule 3 (floor prices) needed
    `mark_starting_at_basis` for exactly this reason. The failure here is worse than
    rule 3's: a fabricated [rent_min, rent_max] pair does not merely mislabel a
    group, it moves it from the "range only, use the low end" branch of the
    market-rent rule to the "measured rents, average the lowest five" branch. On
    Palm Court's 747sf 1BR that is the difference between $1,340 and $1,622 — the
    precise error the field was added to remove.

    Deliberately narrow: two members, exactly equal to the two endpoints, on a group
    claiming MORE than two listed units. A genuine two-priced-unit group is
    indistinguishable from this and is the false positive we accept — its cost is
    falling back to rent_min, which is the answer a range-only group already gets.
    """
    return (len(unit_rents) == 2
            and available > 2
            and rent_min is not None and rent_max is not None
            and rent_min != rent_max
            and unit_rents == [rent_min, rent_max])


def _contradicts_itself(g: UnitGroup) -> bool:
    """True when a group's own numbers cannot all be true (leg 1).

    `unit_rents` is listed units' rents, lowest 10 (HARD RULE 4), and `available` counts
    every listed unit, so more members than listed units is impossible (fewer is legal: a
    listed unit with no price). Below the cap every priced listed unit is a member, so
    rent_max above the largest one is a contradiction. A coming-soon count does not exempt
    it: rent_min/max may span a priced coming-soon unit, but the count does not say the
    unit is priced. Luna,
    Grand at Stonecreek 2026-09-24: studio rent_max 1245 over members 1030-1075; 1-bed
    available 4 over five members.
    """
    if not g.unit_rents:
        return False
    # `is not None`, never truthiness: an unknown count cannot be exceeded, but an
    # explicit 0 with members is still a contradiction.
    if g.available is not None and len(g.unit_rents) > g.available:
        return True
    return len(g.unit_rents) < 10 and g.rent_max is not None and g.rent_max > max(g.unit_rents)


def flag_self_contradictions(result: ExtractResult) -> None:
    """Flag, never rewrite, every group that contradicts itself. Which figure is wrong is not
    knowable here. Runs on the model's rows and again after `prefer_labelled_base_rent`,
    whose band-extrema rule can set a base rent_max above every translated member."""
    for g in result.units:
        if _contradicts_itself(g):
            log.warning(f"    group contradicts its own unit_rents, flagged: beds={g.beds} "
                        f"available={g.available} {g.rent_min}-{g.rent_max} {g.unit_rents}")
            g.flag_review = True
            result.flag_review = True


_FLOOR_TALLY_RE = re.compile(r"\bFloor\s+\d+\s+(\d+)\s+avail\b")
_LIST_VIEW_RE = re.compile(r"\bList View\s+(\d+)\s+available\b")
_UNIT_CARD_RE = re.compile(r"\bFloor ?plan layout: ", re.IGNORECASE)


def distrust_floor_filtered_unit_list(result: ExtractResult, visible_text: str,
                                      prompt_text: Optional[str] = None) -> None:
    """A unit list the page filtered to one floor is not the community's units (leg 2).

    The jd-fp floor-plan widget lists only the ACTIVE floor's units while printing every
    floor's tally ("Floor 1 12 avail Floor 2 10 avail ..."). Chapel Hill 2026-09-21: 12 cards
    against 41 tallied, plan C1 "Only 2 left!" with no C1 card. The model copies that subset
    into `unit_rents`, which `rents.market_rent` averages as the lowest five measured rents,
    and counts it as `available`. Its "Outside price filter range" badge is NOT the signal:
    it is on every card of the widget, complete lists included.

    The listed count is read from `prompt_text` — what the model SAW (`_trim_html`'s 60k
    window) — and is the larger of the widget's header and the cards: the browser fetcher
    merges every floor's cards (leg 2a) but the header keeps the first floor's count,
    and a merged list too long for the trim window (Triton Uptown, 282) is partial again.
    When it is below the tally sum (tallies from the full page):
    `unit_rents` -> None (the documented safe value; market rent falls back to rent_min) and
    every group flagged, since every group's `available` came from one floor. Rents and
    counts are not rewritten — the hidden floors' split by plan is not on the page.
    """
    if not visible_text:
        return
    tallies = [int(n) for n in _FLOOR_TALLY_RE.findall(visible_text)]
    if not tallies:
        return
    seen = visible_text if prompt_text is None else prompt_text
    header = _LIST_VIEW_RE.search(seen)
    listed = max(int(header.group(1)) if header else 0, len(_UNIT_CARD_RE.findall(seen)))
    if listed >= sum(tallies):
        return
    log.warning(f"    unit list shows {listed} of {sum(tallies)} tallied units (one floor): "
                f"unit_rents dropped, groups flagged")
    for g in result.units:
        g.unit_rents = None
        g.flag_review = True
    result.flag_review = True


def _coerce_result(data: dict) -> ExtractResult:
    """Translate the JSON dict from the LLM into ExtractResult / UnitGroup."""
    units: list[UnitGroup] = []
    for u in data.get("units") or []:
        try:
            rent_min = _safe_int(u.get("rent_min"))
            rent_max = _safe_int(u.get("rent_max"))
            grp_flag = bool(u.get("flag_review"))
            rent_basis = u.get("rent_basis")
            if rent_basis not in _RENT_BASES:
                rent_basis = "unspecified"
            # Rule 3 asks the model to flag floor-priced groups and it does not
            # reliably do so — enforce it here, where grouping can't lose it.
            if rent_basis == "starting_at":
                grp_flag = True
            # A null count is UNKNOWN, never an observed zero: stored null and
            # flagged ("unknown fields are null + flag_review=true"). An explicit 0 stays 0.
            available = _safe_int(u.get("available"))
            coming_soon = _safe_int(u.get("coming_soon"))
            if available is None or coming_soon is None:
                grp_flag = True
            unit_rents = observed_rents(u.get("unit_rents"))
            if unit_rents is not None and _looks_reconstructed(unit_rents, rent_min, rent_max,
                                                              available or 0):
                log.warning(f"dropping unit_rents that merely restate the range: {u}")
                unit_rents = None
                grp_flag = True
            if ((rent_min is not None and rent_min > _MAX_PLAUSIBLE_MONTHLY_RENT)
                    or (rent_max is not None and rent_max > _MAX_PLAUSIBLE_MONTHLY_RENT)):
                log.warning(f"clamping non-rent value (>{_MAX_PLAUSIBLE_MONTHLY_RENT}) to null: {u}")
                rent_min = rent_max = None
                unit_rents = None
                grp_flag = True
            units.append(UnitGroup(
                beds=_safe_int(u.get("beds")),
                sf_min=_safe_int(u.get("sf_min")),
                sf_max=_safe_int(u.get("sf_max")),
                available=available,
                coming_soon=coming_soon,
                rent_min=rent_min,
                rent_max=rent_max,
                rent_type=data.get("rent_type", "unspecified") if data.get("rent_type") in ("base", "allin", "unspecified") else "unspecified",
                rent_allin_min=_safe_int(u.get("rent_allin_min")),
                rent_allin_max=_safe_int(u.get("rent_allin_max")),
                rent_basis=rent_basis,
                unit_rents=unit_rents,
                structure_type=_one_of(u.get("structure_type"), _STRUCTURE_TYPES),
                stories=_positive_int(u.get("stories"), minimum=1),
                garage=_positive_int(u.get("garage"), minimum=0),
                baths=_bath_count(u.get("baths")),
                plan_name=_plan_name(u.get("plan_name")),
                flag_review=grp_flag,
            ))
        except Exception as e:
            log.warning(f"could not coerce unit {u}: {e}")

    result = ExtractResult(
        units=units,
        concession_weeks=_safe_int(data.get("concession_weeks")),
        # A string or nothing: a list/dict where the banner belongs used to bypass the
        # scanner only because a count was set; sent into re.finditer it raised, and in
        # the batch path one bad row aborted the collection.
        raw_concession_text=(data.get("raw_concession_text")
                             if isinstance(data.get("raw_concession_text"), str) else None),
        # `or any(...)` — NOT data['flag_review'] alone. The page-level flag is the
        # LLM's own verdict on the page, and it is false precisely when the model was
        # CONFIDENT about a value it got wrong. Reading it alone discarded every
        # group-level flag on the way to the snapshot, which meant:
        #   - the >$50k/mo non-rent clamp (condo sale prices read as monthly rent,
        #     e.g. Mirador AUS) nulled the rent and raised the GROUP flag, but the
        #     persisted row stayed unflagged — a confident misparse looked clean;
        #   - prompt rules 1-3 (rent not visible / gated behind a form / "starting at
        #     $X") all tell the model to flag "that group", so the three commonest
        #     uncertainty signals this extractor explicitly asks for were thrown away;
        #   - rents_dq.flag_review_ceiling grades snapshot-level flag_review and could
        #     therefore never see any of it.
        # Every adapter already does this (entrata.py:305, rentcafe.py:161, knock.py:91,
        # realpage.py:131, avalon.py:89, repli360.py:91) and adapters/base.py:171 raises
        # the result flag directly. Only the LLM tier dropped it.
        flag_review=bool(data.get("flag_review")) or any(u.flag_review for u in units),
        extractor_used="llm_haiku",
        scrape_error=data.get("scrape_error"),
        # Fenced to the closed vocabulary, so a marketing adjective the model liked
        # the sound of ("resort-style") cannot reach a deal document as a fact.
        amenities=normalize_amenities(data.get("amenities")),
    )
    flag_self_contradictions(result)
    return result


def _repair_truncated_json(raw: str) -> Optional[dict]:
    """Recover a dict from JSON cut off mid-output (max_tokens truncation).

    Strategy: walk back to the last complete `}` (end of a unit object), then
    try closing the structure with the plausible bracket suffixes. Returns None
    if nothing parses — caller falls back to the parse-error path.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return None
    for cut in range(len(raw), 0, -1):
        if raw[cut - 1] != "}":
            continue
        head = raw[:cut].rstrip().rstrip(",")
        for suffix in ("", "]}", "]}}", "}", "}}"):
            try:
                d = json.loads(head + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                return d
        break  # only try the LAST complete '}' — earlier cuts change meaning
    return None


_UNIT_RENTS_LIST = re.compile(r'"unit_rents"\s*:\s*\[([^\[\]]*)\]')
def _parses_as_list(body: str) -> bool:
    """Only a list that is not legal JSON is replaced: a legal sibling such as
    [1000,null,1100] keeps whatever observed_rents() would keep from it."""
    try:
        return isinstance(json.loads("[" + body + "]"), list)
    except json.JSONDecodeError:
        return False


def _null_malformed_unit_rents(raw: str) -> Optional[dict]:
    """Null every unit_rents list that is not legal JSON, then parse.

    Every mid-reply failure in the 2026-10-02 A/B (4 of 4,200 replies) was inside one:
    `[2527,293?]`, `[...,1701,?]`, `[...,995,1165,1165,]`. unit_rents is observed-only and null
    is its documented safe value (the market rent falls back to rent_min), so the corrupt
    list is dropped and every unit is kept, rents included. Returns None if that is not
    the only thing wrong.
    """
    # The marker key rides inside the repaired group so THAT group is flagged after parsing
    #. Not a second "flag_review" key: the group's own one,
    # later in the object, would win.
    fixed = _UNIT_RENTS_LIST.sub(
        lambda m: m.group(0) if _parses_as_list(m.group(1))
        else '"unit_rents":null,"_unit_rents_repaired":true', raw)
    if fixed == raw:
        return None
    try:
        d = json.loads(fixed)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    for u in d.get("units") or []:
        if isinstance(u, dict) and u.pop("_unit_rents_repaired", False):
            u["flag_review"] = True
    return d


def _salvage_before(raw: str, pos: int) -> Optional[dict]:
    """Keep the complete unit objects BEFORE a malformed token at `pos` (leg 3).

    `_repair_truncated_json` handles a reply cut off at the END: it tries the last `}`. A stray
    token in the middle — gpt-5.6-luna on Manchester State Thomas, twice on identical bytes:
    `"unit_rents":[2527,293?]` — leaves a valid tail after the break, so that repair fails and
    the whole community was lost (three priced bed groups). Unit objects hold no nested
    objects (unit_rents is an array), so a `}` before the break closes one unit: cut there
    and close the structure. Returns None unless at least one unit survives.
    """
    if not raw or pos is None:
        return None
    end = raw.rfind("}", 0, pos)
    while end > 0:
        head = raw[:end + 1].rstrip().rstrip(",")
        for suffix in ("]}", "]}}"):
            try:
                d = json.loads(head + suffix)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict) and d.get("units"):
                return d
        end = raw.rfind("}", 0, end)
    return None


_STRUCTURE_TYPES = ("sfd", "th", "mf")


def _one_of(v, allowed: tuple[str, ...]) -> Optional[str]:
    """Keep a model-supplied enum only if it IS one of the values we defined.

    HARD RULE 8 names three; a model that answers "duplex" or "garden" has invented
    a fourth, and a value nothing downstream maps is worse than a null — the null
    at least reads as "unknown" instead of falling through some future else-branch.
    """
    s = str(v).strip().lower() if v is not None else ""
    return s if s in allowed else None


def _positive_int(v, minimum: int) -> Optional[int]:
    """Whole storeys / garage bays, or None.

    `minimum=0` for garage because ZERO IS AN OBSERVATION ("no garage") and must
    survive; `minimum=1` for stories because a zero-storey home is a parse error,
    not a reading. Anything non-numeric ("two") is the model answering in prose —
    dropped rather than guessed at, same as every other coercion here.
    """
    # Reject a FRACTIONAL value rather than truncating it. Codex caught
    # `_safe_int(1.5)` -> 1 on "1.5-car garage", which is real page language for a
    # tandem/half bay: truncation converts a value the schema cannot express into a
    # false OBSERVATION, and 1 is plausible enough that nothing downstream can tell.
    # A whole-number float still counts — JSON has no int/float distinction, so 2.0
    # from the model is a 2.
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != int(f):
        return None
    n = int(f)
    return n if n >= minimum else None


def _bath_count(v) -> Optional[float]:
    """Bathrooms, or None. The deliberate opposite of `_positive_int`'s fraction rule.

    A .5 bath IS the observation here ("2.5 Baths" is a half bath, not a parse
    error), so halves are kept where garage bays reject them. Anything else that is
    not a plain number, or a count outside 0.5-10, is the model answering in prose or
    hallucinating a mansion — dropped, not guessed.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f % 0.5 != 0:            # 1.75 baths is not a thing any leasing page states
        return None
    return f if 0.5 <= f <= 10 else None


#: The one rule for what counts as a plan name lives in `adapters/base.py`, shared
#: with every adapter — a catalog can hand over a nested plan object or a bare number
#: just as easily as this model can invent a description, and the consumer cannot tell
#: which path a name arrived by. HARD RULE 9 tells the model; this enforces it.
def _plan_name(v) -> Optional[str]:
    return clean_plan_name(v)


def _safe_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The model call, injected.
# ---------------------------------------------------------------------------
class Provider(Protocol):
    """Anything that turns (system prompt, user message) into the model's raw reply text."""

    name: str

    def complete(self, system: str, user: str) -> str: ...


class RecordedProvider:
    """Replays a saved model reply. Used by the tests and the offline demo; never calls out."""

    def __init__(self, reply: str, name: str = "recorded"):
        self.reply = reply
        self.name = name
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        return self.reply


def _user_message(community: dict, page_text: str) -> str:
    """Community context is the name and street address only. Inventory counts and
    ownership fields are deliberately not sent: the model must read the page, not a seed."""
    ctx = [f"{k}: {community[k]}" for k in ("name", "address") if community.get(k)]
    ctx_str = "\n".join(ctx) if ctx else "(no community context)"
    return f"COMMUNITY CONTEXT:\n{ctx_str}\n\nPAGE TEXT (rendered, trimmed):\n\n{page_text}"


def _strip_fences(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip())
    return re.sub(r"\s*```$", "", raw)


class LLMExtractor:
    """Tier 2. `extract(community, rendered_html)` -> ExtractResult, via an injected Provider."""

    def __init__(self, provider: Provider):
        self.provider = provider
        self.NAME = f"llm_{getattr(provider, 'name', 'model')}"

    def extract(self, community: dict, rendered_html: str) -> ExtractResult:
        user = _user_message(community, _trim_html(rendered_html))
        try:
            raw = self.provider.complete(SYSTEM_PROMPT, user)
        except Exception as e:  # noqa: BLE001 -- a provider failure is a scrape error, not a crash
            return ExtractResult(scrape_error=f"LLM call failed: {type(e).__name__}: {e}"[:200],
                                 extractor_used=self.NAME, flag_review=True)
        return self.parse_reply(rendered_html, raw)

    def parse_reply(self, rendered_html: str, raw: str) -> ExtractResult:
        raw = _strip_fences(raw)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            data = _repair_truncated_json(raw)
            note = "output truncated; repaired partial JSON"
            if data is None:
                data = _null_malformed_unit_rents(raw)
                note = f"invalid JSON at char {e.pos}; malformed unit_rents set to null"
            if data is None:
                data = _salvage_before(raw, e.pos)
                note = f"invalid JSON at char {e.pos}; salvaged the units before it"
            if data is None:
                return ExtractResult(scrape_error=f"LLM JSON parse: {e}; raw_head={raw[:150]!r}",
                                     extractor_used=self.NAME, flag_review=True)
            # Partial units are better than none, but never trusted silently.
            data["flag_review"] = True
            if not data.get("scrape_error"):
                data["scrape_error"] = note
        if not isinstance(data, dict):
            return ExtractResult(scrape_error=f"LLM JSON is not an object: {raw[:120]!r}",
                                 extractor_used=self.NAME, flag_review=True)

        result = _coerce_result(data)
        result.extractor_used = self.NAME
        visible = _visible_text(rendered_html)
        # Backstops: the page is re-read rather than the model trusted.
        prefer_labelled_base_rent(result, visible)
        flag_self_contradictions(result)
        distrust_floor_filtered_unit_list(result, visible, _trim_html(rendered_html))
        mark_starting_at_basis(result, visible)
        widen_band_to_advertised_plans(result, visible)
        # The scanner checks the model's week count against the model's own banner quote.
        if not reconcile_concession(result):
            fill_concession_from_banner(result)
        veto_non_rent_quote(result, visible)
        # Hero banners can sit outside the trimmed text the model saw: fill only if it found none.
        if result.raw_concession_text is None:
            rent_hint = min((u.rent_min for u in result.units if u.rent_min), default=None)
            merge_concession(result, _concession_text(rendered_html), monthly_rent=rent_hint)
        return result
