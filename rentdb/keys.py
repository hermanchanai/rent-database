"""Named join keys, one definition each, imported by every side of the join."""
from __future__ import annotations


def community_key(market: str, name: str) -> tuple[str, str]:
    """A community's identity: `(market, lower(name))`.

    Resolving on the raw `(market, name)` pair misses case-only differences
    ('WOODBRIDGE APARTMENTS' vs 'Woodbridge Apartments').
    """
    return (market, (name or "").lower())


def advertised_plan_key(beds, sf) -> tuple:
    """The exact (beds, SF) join between a grid plan card, the unit cards priced under it
    and the groups built from them. NOT a plan identity: two sibling plans can share it,
    so per-plan state stays keyed on the plan object. Exact equality only."""
    return (beds, sf)
