"""Turn one community read into dated, concession-adjusted rent records.

A read is stored as a SNAPSHOT: one row per community per scrape, carrying the ledger status,
the extractor that produced it, the community-level concession and the floor-plan groups.
`records()` expands a snapshot into one `RentRecord` per group, with the two derived numbers
every consumer needs computed in exactly one place:

  net-effective rent   ner = round(rent * (1 - concession_weeks / 52))
                       A "6 weeks free" special on a 12-month lease is 6 of 52 weeks of rent
                       given back, so the asking rent is discounted by 6/52. No concession,
                       or one that could not be counted in weeks ("$500 off"), leaves ner equal
                       to the asking rent.
  market rent          the mean of the LOWEST FIVE observed per-unit rents when the page priced
                       units one by one; rent_min when it published only a range. A list is
                       never reconstructed from the range, so `unit_rents is None` is the
                       signal that selects the second branch.

Rents a feed uses as "no price" placeholders (0, -1) are recovered from the observed member
list where possible and otherwise dropped to None, matching `base.drop_sentinel_rents`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from rentdb.adapters.base import STATUS_LIVE, ExtractResult

WEEKS_PER_YEAR = 52
MARKET_RENT_LOWEST_N = 5


def round_half_up(x: float | Decimal | None) -> int | None:
    """Postgres numeric round(): halves go away from zero. Python's round() is banker's."""
    if x is None:
        return None
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def net_effective(rent: int | float | None, concession_weeks: int | None) -> int | None:
    if rent is None:
        return None
    weeks = concession_weeks or 0
    return round_half_up(Decimal(str(rent)) * (1 - Decimal(weeks) / Decimal(WEEKS_PER_YEAR)))


def market_rent(unit_rents: list[int] | None, rent_min: int | None,
                n: int = MARKET_RENT_LOWEST_N) -> int | None:
    members = sorted(v for v in (unit_rents or []) if v is not None and v > 0)
    if not members:
        return rent_min
    lowest = members[:max(n, 1)]
    return round_half_up(Decimal(sum(lowest)) / Decimal(len(lowest)))


@dataclass
class Snapshot:
    """One community read, as stored."""
    community_id: str
    scraped_at: str                 # ISO-8601 with offset
    status: str
    extractor: str
    concession_weeks: int | None = None
    raw_concession_text: str | None = None
    flag_review: bool = False
    scrape_error: str | None = None
    units: list[dict] = field(default_factory=list)

    @classmethod
    def from_result(cls, community_id: str, result: ExtractResult, scraped_at: datetime) -> "Snapshot":
        if scraped_at.tzinfo is None:
            raise ValueError("scraped_at must be timezone-aware")
        return cls(
            community_id=community_id,
            scraped_at=scraped_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            status=result.status,
            extractor=result.extractor_used or "none",
            concession_weeks=result.concession_weeks,
            raw_concession_text=result.raw_concession_text,
            flag_review=result.flag_review,
            scrape_error=result.scrape_error,
            units=[u.to_json() for u in result.units],
        )

    @classmethod
    def from_json(cls, d: dict) -> "Snapshot":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__ if k in d})

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RentRecord:
    """One floor-plan group on one date, with its net-effective and market rent."""
    community_id: str
    observed_on: str                # YYYY-MM-DD (UTC)
    scraped_at: str
    extractor: str
    beds: int | None
    sf_min: int | None
    sf_max: int | None
    plan_name: str | None
    baths: float | None
    structure_type: str | None
    available: int | None
    coming_soon: int | None
    rent_type: str
    rent_basis: str
    rent_min: int | None
    rent_max: int | None
    rent_allin_min: int | None
    rent_allin_max: int | None
    unit_rents: tuple[int, ...] | None
    market_rent: int | None
    concession_weeks: int | None
    concession_text: str | None
    ner_min: int | None
    ner_max: int | None
    flag_review: bool

    def to_json(self) -> dict:
        d = asdict(self)
        d["unit_rents"] = list(self.unit_rents) if self.unit_rents is not None else None
        return d


def _positive(v) -> int | None:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def records(snap: Snapshot) -> list[RentRecord]:
    """The snapshot's groups as dated records. Only a live_data read carries rents."""
    if snap.status != STATUS_LIVE:
        return []
    weeks = snap.concession_weeks if (snap.concession_weeks or 0) > 0 else None
    out: list[RentRecord] = []
    for g in snap.units or []:
        members = sorted(v for v in (_positive(x) for x in (g.get("unit_rents") or [])) if v)
        lo, hi = _positive(g.get("rent_min")), _positive(g.get("rent_max"))
        # A sentinel endpoint is recovered from the observed members, never guessed.
        if lo is None and g.get("rent_min") is not None and members:
            lo = members[0]
        if hi is None and g.get("rent_max") is not None and members:
            hi = members[-1]
        out.append(RentRecord(
            community_id=snap.community_id,
            observed_on=snap.scraped_at[:10],
            scraped_at=snap.scraped_at,
            extractor=snap.extractor,
            beds=g.get("beds"),
            sf_min=g.get("sf_min"),
            sf_max=g.get("sf_max"),
            plan_name=g.get("plan_name"),
            baths=g.get("baths"),
            structure_type=g.get("structure_type"),
            available=g.get("available"),
            coming_soon=g.get("coming_soon"),
            rent_type=g.get("rent_type") or "unspecified",
            rent_basis=g.get("rent_basis") or "unspecified",
            rent_min=lo,
            rent_max=hi,
            rent_allin_min=_positive(g.get("rent_allin_min")),
            rent_allin_max=_positive(g.get("rent_allin_max")),
            unit_rents=tuple(members) if members else None,
            market_rent=market_rent(members, lo),
            concession_weeks=weeks,
            concession_text=snap.raw_concession_text,
            ner_min=net_effective(lo, weeks),
            ner_max=net_effective(hi, weeks),
            flag_review=bool(g.get("flag_review")),
        ))
    return out
