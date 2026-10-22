"""Build the public rent-database rollup from a community registry and stored snapshots.

The output has the same shape as the portfolio site's `rent-database.json`: a headline,
per-market coverage and pricing, vintage / bedroom / asset-class rollups, extraction engines,
weekly activity, re-observation depth, direction of rent moves, and a small per-market sample.
Everything is an aggregate except the sample, which is bounded on purpose (see `samples`).

Production computed these in SQL over its snapshot table and a latest-groups view. This module
computes the same rollups in plain Python so they can be run and tested offline:

  latest groups   the groups of each community's newest live_data snapshot
  floor plan      one latest group
  median          linear-interpolated percentile (Postgres percentile_cont(0.5))
  psf             rent_min / sf_min
  under concession  concession_weeks > 0 on the snapshot the group came from
  discount        100 * (1 - ner_min / rent_min), averaged where ner_min < rent_min

Two production view rules are simplified here and noted so nobody reads more into the demo
output than it supports: the "sold out" carry-forward of the last priced read, and the
freshness / dead-URL gates. Both only remove rows; neither changes how a row is computed.

Unit counts come only from public records (a county property record, a state apartment
licence, a city permit), each with its source named. A community with no public-record count
contributes nothing to `units`, and `units` is null when no community has one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from rentdb.adapters.base import STATUS_LIVE
from rentdb.normalize import RentRecord, Snapshot, records, round_half_up

METROS = {
    "ATL": "Atlanta, GA", "AUS": "Austin, TX", "CHA": "Chattanooga, TN", "CHI": "Chicago, IL",
    "CLT": "Charlotte, NC", "DEN": "Denver, CO", "DFW": "Dallas–Fort Worth, TX",
    "GSP": "Greenville, SC", "HOU": "Houston, TX", "HSV": "Huntsville, AL",
    "JAX": "Jacksonville, FL", "LAS": "Las Vegas, NV", "LAX": "Los Angeles / Orange County, CA",
    "MCO": "Orlando, FL", "NSH": "Nashville, TN", "PHX": "Phoenix, AZ",
    "RDU": "Raleigh–Durham, NC", "SAT": "San Antonio, TX", "SAV": "Savannah, GA",
    "SRQ": "Sarasota, FL", "TPA": "Tampa, FL", "TYS": "Knoxville, TN",
}


# --- small numeric helpers -------------------------------------------------------------
def median(values) -> float | None:
    """percentile_cont(0.5): the mean of the two middle values for an even count."""
    vals = [v for v in values if v is not None]
    return float(statistics.median(vals)) if vals else None


def rnd(x, places: int):
    if x is None:
        return None
    return round(float(x) + 0.0, places)


def as_int(x):
    return round_half_up(x) if x is not None else None


def pct(part: int, whole: int):
    return round(100.0 * part / whole, 1) if whole else None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _week(d: datetime) -> str:
    day = d.astimezone(timezone.utc).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _psf(r: RentRecord) -> float | None:
    if r.rent_min and r.rent_min > 0 and r.sf_min and r.sf_min > 0:
        return r.rent_min / r.sf_min
    return None


def _priced(r: RentRecord) -> bool:
    return r.rent_min is not None and r.rent_min > 0


def fold_llm_engines(extraction: list[dict]) -> list[dict]:
    """Collapse every llm_<model> engine into one row labelled with the models seen."""
    llm = [e for e in extraction if str(e["engine"]).startswith("llm")]
    rest = [e for e in extraction if not str(e["engine"]).startswith("llm")]
    if not llm:
        return extraction
    llm.sort(key=lambda e: -e["observations"])
    names = [e["engine"].removeprefix("llm_").removeprefix("llm").strip("_").capitalize() or "?"
             for e in llm]
    folded = {"engine": f"LLM ({' / '.join(dict.fromkeys(names))})",
              "observations": sum(e["observations"] for e in llm)}
    return sorted(rest + [folded], key=lambda e: -e["observations"])


# --- the build -------------------------------------------------------------------------
def build(communities: list[dict], snapshots: list[Snapshot], *, sample_per_market: int = 20,
          as_of: datetime | None = None, metros: dict | None = None) -> dict:
    metros = metros or METROS
    by_id = {c["id"]: c for c in communities}
    snaps = [s for s in snapshots if s.community_id in by_id]
    as_of = as_of or max((_ts(s.scraped_at) for s in snaps), default=datetime.now(timezone.utc))
    confirmed = [c for c in communities if c.get("url_status", "confirmed") == "confirmed"]

    latest: dict[str, Snapshot] = {}
    for s in sorted(snaps, key=lambda s: s.scraped_at):
        if s.status == STATUS_LIVE:
            latest[s.community_id] = s
    groups = [r for s in latest.values() for r in records(s)]
    groups_by_c: dict[str, list[RentRecord]] = defaultdict(list)
    for r in groups:
        groups_by_c[r.community_id].append(r)
    market_of = {cid: by_id[cid]["market"] for cid in by_id}

    out: dict = {}
    live = [s for s in snaps if s.status == STATUS_LIVE]
    out["headline"] = {
        "communities": len(confirmed),
        "markets": len({c["market"] for c in communities}),
        "floor_plans": len(groups),
        "communities_with_rents": len(groups_by_c),
        "with_net_effective": sum(1 for r in groups if r.ner_min is not None),
        "under_concession": sum(1 for r in groups if (r.concession_weeks or 0) > 0),
        "observations_all": len(snaps),
        "observations_30d": sum(1 for s in snaps if _ts(s.scraped_at) > as_of - timedelta(days=30)),
        "last_observation": max((s.scraped_at for s in snaps), default=None),
        "first_observation": min((s.scraped_at for s in snaps), default=None),
    }

    # Coverage is two numbers: communities tracked, and how many carry live rent detail now.
    markets = []
    by_market: dict[str, list[dict]] = defaultdict(list)
    for c in confirmed:
        by_market[c["market"]].append(c)
    for m, cs in by_market.items():
        g = [r for r in groups if market_of[r.community_id] == m]
        priced = [r for r in g if _priced(r)]
        disc = [100.0 * (1 - r.ner_min / r.rent_min) for r in priced
                if r.ner_min is not None and r.ner_min < r.rent_min]
        last = [latest[cid].scraped_at for cid in {r.community_id for r in g}]
        markets.append({
            "market": m,
            "communities": len(cs),
            "bfr": sum(1 for c in cs if c.get("product") == "bfr"),
            "mf": sum(1 for c in cs if c.get("product") == "mf"),
            "with_rents": len({r.community_id for r in g}),
            "floor_plans": len(g),
            "median_rent": median(r.rent_min for r in priced),
            "median_psf": rnd(median(_psf(r) for r in g), 2),
            "pct_concession": pct(sum(1 for r in g if (r.concession_weeks or 0) > 0), len(g)),
            "avg_discount_pct": rnd(sum(disc) / len(disc), 1) if disc else None,
            "last_scraped": max(last) if last else None,
        })
    markets.sort(key=lambda m: -m["communities"])
    out["markets"] = markets

    # Vintage: one value per community first, or a 40-plan community counts 40 times.
    buckets: dict[str, list] = defaultdict(list)
    for cid, g in groups_by_c.items():
        c = by_id[cid]
        yb = c.get("year_built")
        decade = ("Unknown" if yb is None else "Pre-1970" if yb < 1970
                  else "2020s" if yb >= 2020 else f"{yb // 10 * 10}s")
        buckets[decade].append((c.get("product"), median(_psf(r) for r in g), yb))
    order = lambda kv: min((yb for _p, _x, yb in kv[1] if yb is not None), default=9999)  # noqa: E731
    out["vintage"] = [{
        "decade": d,
        "communities": len(rows),
        "bfr": sum(1 for p, _x, _y in rows if p == "bfr"),
        "mf": sum(1 for p, _x, _y in rows if p == "mf"),
        "median_psf": rnd(median(x for _p, x, _y in rows), 2),
    } for d, rows in sorted(buckets.items(), key=order)]

    beds: dict[int, list[RentRecord]] = defaultdict(list)
    for r in groups:
        if r.beds is not None:
            beds[r.beds].append(r)
    out["beds"] = [{
        "beds": b,
        "floor_plans": len(g),
        "communities": len({r.community_id for r in g}),
        "median_rent": as_int(median(r.rent_min for r in g if _priced(r))),
        "median_sf": as_int(median(r.sf_min for r in g if r.sf_min and r.sf_min > 0)),
        "median_psf": rnd(median(_psf(r) for r in g), 2),
        "pct_concession": pct(sum(1 for r in g if (r.concession_weeks or 0) > 0), len(g)),
    } for b, g in sorted(beds.items())]

    # Asset class comes from the registry's product; the page's own structure word refines it.
    ac: dict[tuple, list[RentRecord]] = defaultdict(list)
    for r in groups:
        ac[(by_id[r.community_id].get("product") or "unclassified",
            r.structure_type or "unspecified")].append(r)
    out["asset_class"] = sorted(({
        "product": p, "structure": st,
        "floor_plans": len(g),
        "communities": len({r.community_id for r in g}),
        "median_rent": as_int(median(r.rent_min for r in g if _priced(r))),
        "median_sf": as_int(median(r.sf_min for r in g if r.sf_min and r.sf_min > 0)),
    } for (p, st), g in ac.items()), key=lambda d: -d["floor_plans"])

    recent = [s for s in live if _ts(s.scraped_at) > as_of - timedelta(days=60)]
    engines: dict[str, int] = defaultdict(int)
    for s in recent:
        engines[s.extractor or "none"] += 1
    out["extraction"] = fold_llm_engines(sorted(
        ({"engine": e, "observations": n} for e, n in engines.items()),
        key=lambda d: -d["observations"]))

    weeks: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snaps:
        if _ts(s.scraped_at) > as_of - timedelta(weeks=16):
            weeks[_week(_ts(s.scraped_at))].append(s)
    out["activity"] = [{
        "week": w,
        "observations": sum(1 for s in ss if s.status == STATUS_LIVE),
        "communities": len({s.community_id for s in ss if s.status == STATUS_LIVE}),
        "markets": len({market_of[s.community_id] for s in ss if s.status == STATUS_LIVE}),
    } for w, ss in sorted(weeks.items())]

    # Re-observation depth: the honest limit on any trend claim.
    n90: dict[str, int] = defaultdict(int)
    for s in live:
        if _ts(s.scraped_at) > as_of - timedelta(days=90):
            n90[s.community_id] += 1
    out["revisit"] = {
        "observed": len(n90),
        "twice_plus": sum(1 for n in n90.values() if n >= 2),
        "thrice_plus": sum(1 for n in n90.values() if n >= 3),
        "avg_observations": rnd(sum(n90.values()) / len(n90), 1) if n90 else None,
    }

    # Direction of travel for communities seen twice or more, at least 14 days apart. Not an
    # index: a community's median listed rent moves when its listed mix moves.
    obs: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for s in live:
        t = _ts(s.scraped_at)
        if t <= as_of - timedelta(days=75) or not s.units:
            continue
        med = median(v for v in (g.get("rent_min") for g in s.units) if v is not None and v > 0)
        if med is not None:
            obs[s.community_id].append((t, med))
    moves = []
    for seq in obs.values():
        seq.sort()
        span = (seq[-1][0].date() - seq[0][0].date()).days
        if len(seq) >= 2 and span >= 14 and seq[0][1] > 0:
            moves.append((seq[0][1], seq[-1][1], span))
    out["rent_moves"] = {
        "communities": len(moves),
        "moved_up": sum(1 for a, b, _ in moves if b > a),
        "moved_down": sum(1 for a, b, _ in moves if b < a),
        "unchanged": sum(1 for a, b, _ in moves if b == a),
        "avg_span_days": as_int(sum(s for *_x, s in moves) / len(moves)) if moves else None,
    }

    out["samples"] = {m["market"]: samples(m["market"], confirmed, groups_by_c, latest,
                                           sample_per_market)
                      for m in markets}
    unnamed = [m["market"] for m in markets if m["market"] not in metros]
    if unnamed:
        raise ValueError(f"no display name for {', '.join(unnamed)}; add them to METROS")
    out["metros"] = {k: metros[k] for k in sorted({m["market"] for m in markets})}
    out["generated_at"] = as_of.astimezone(timezone.utc).isoformat(timespec="seconds")
    out["sample_per_market"] = sample_per_market
    return out


def _md5(cid: str) -> str:
    return hashlib.md5(cid.encode()).hexdigest()  # noqa: S324 -- a stable shuffle, not security


def samples(market: str, confirmed: list[dict], groups_by_c: dict, latest: dict, n: int) -> list[dict]:
    """A bounded, stratified, stably pseudo-random sample of READ communities.

    Only communities with a priced latest group qualify: an unread row shows a reader nothing.
    Ordered by a hash of the id, not by size, so the sample is not all 1,000-unit towers;
    up to five build-for-rent communities lead so the BFR tail is visible.
    """
    pool = []
    for c in confirmed:
        if c["market"] != market or not c.get("name"):
            continue
        priced = [r.rent_min for r in groups_by_c.get(c["id"], []) if r.rent_min and r.rent_min > 0]
        if not priced or c["id"] not in latest:
            continue
        pool.append((c, round_half_up(sum(priced) / len(priced))))
    rank: dict[str, int] = {}
    for product in {c.get("product") for c, _ in pool}:
        ids = sorted((c["id"] for c, _ in pool if c.get("product") == product), key=_md5)
        rank.update({cid: i + 1 for i, cid in enumerate(ids)})
    picked = sorted(pool, key=lambda t: (0 if t[0].get("product") == "bfr" and rank[t[0]["id"]] <= 5 else 1,
                                         _md5(t[0]["id"])))[:n]
    rows = []
    for c, avg in sorted(picked, key=lambda t: (t[0].get("product") or "", t[0]["name"])):
        has_units = c.get("units") is not None
        rows.append({
            "name": c["name"],
            "product": (c.get("product") or "—").upper(),
            "units": c.get("units"),
            "units_source": c.get("units_source") if has_units else None,
            "year_built": c.get("year_built"),
            "year_source": c.get("year_source") if c.get("year_built") is not None else None,
            "last_seen": latest[c["id"]].scraped_at[:10],
            "avg_rent": avg,
        })
    return rows


def load_snapshots(path: Path) -> list[Snapshot]:
    return [Snapshot.from_json(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--communities", type=Path, required=True)
    ap.add_argument("--snapshots", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sample-per-market", type=int, default=20)
    ap.add_argument("--as-of", type=date.fromisoformat, default=None,
                    help="evaluate the 30/60/75/90-day windows at this date (default: newest read)")
    a = ap.parse_args(argv)
    as_of = (datetime.combine(a.as_of, datetime.min.time(), tzinfo=timezone.utc)
             if a.as_of else None)
    payload = build(json.loads(a.communities.read_text()), load_snapshots(a.snapshots),
                    sample_per_market=a.sample_per_market, as_of=as_of)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(payload, indent=2) + "\n")
    h = payload["headline"]
    print(f"wrote {a.out}: {h['communities']} communities, {h['floor_plans']} floor plans, "
          f"{h['under_concession']} under concession, {h['observations_all']} observations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
