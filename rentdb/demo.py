"""Offline end-to-end run over the bundled saved pages.

    python -m rentdb.demo [--pages sample/pages] [--out out]

For every saved community under `--pages`:
  kind "chain"  replay its saved pages through the full read chain (adapters, probe, guards)
  kind "llm"    no adapter matches; the rendered page text goes to the LLM tier, whose reply
                is replayed from the saved model output (no key, no network)
Each read becomes a dated snapshot; the snapshots and the community registry then go through
the rollup builder. Writes `<out>/communities.json`, `<out>/snapshots.jsonl` and
`<out>/rent-database.json`, and prints one line per community.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from rentdb.llm_fallback import LLMExtractor, RecordedProvider
from rentdb.normalize import Snapshot, records
from rentdb.pipeline import read_community
from rentdb.replay import SavedPages
from rentdb.snapshot import build

ROOT = Path(__file__).resolve().parent.parent


def read_case(case_dir: Path) -> tuple[dict, Snapshot] | None:
    case = json.loads((case_dir / "case.json").read_text())
    community = dict(case["community"])
    when = datetime.fromisoformat(case["scraped_at"])
    quiet = lambda _m: None  # noqa: E731
    if case.get("kind") == "chain":
        saved = SavedPages(case_dir)
        with saved.installed():
            result = read_community(community, logf=quiet)
    elif case.get("kind") == "llm":
        page = (case_dir / case["rendered_text"]).read_text(encoding="utf-8")
        reply = (case_dir / case["model_reply"]).read_text(encoding="utf-8")
        llm = LLMExtractor(RecordedProvider(reply, name="recorded"))
        saved = SavedPages.empty()
        with saved.installed():
            result = read_community(community, llm=llm, render=lambda _url: page, logf=quiet)
    else:
        return None                     # parser-level cases are test fixtures, not communities
    return community, Snapshot.from_result(community["id"], result, when)


def run(pages: Path, out: Path) -> dict:
    communities, snaps = [], []
    for case_dir in sorted(p for p in pages.iterdir() if (p / "case.json").exists()):
        got = read_case(case_dir)
        if got is None:
            continue
        community, snap = got
        communities.append({
            "id": community["id"], "market": community["market"], "name": community["name"],
            "product": community.get("product"), "url_status": "confirmed",
            # public-record facts only; none are bundled with the saved pages
            "units": None, "units_source": None, "year_built": None, "year_source": None,
        })
        snaps.append(snap)
        recs = records(snap)
        ner = [r.ner_min for r in recs if r.ner_min is not None]
        print(f"{community['market']}  {community['name']:<28} {snap.extractor:<22} {snap.status:<15} "
              f"{len(recs):>2} plan(s)  concession={snap.concession_weeks or 0}w  "
              f"net-effective from ${min(ner):,}" if ner else
              f"{community['market']}  {community['name']:<28} {snap.extractor:<22} {snap.status:<15} "
              f"{len(recs):>2} plan(s)")
    payload = build(communities, snaps, sample_per_market=20)
    out.mkdir(parents=True, exist_ok=True)
    (out / "communities.json").write_text(json.dumps(communities, indent=2) + "\n")
    (out / "snapshots.jsonl").write_text("".join(json.dumps(s.to_json()) + "\n" for s in snaps))
    (out / "rent-database.json").write_text(json.dumps(payload, indent=2) + "\n")
    h = payload["headline"]
    print(f"\nwrote {out / 'rent-database.json'}: {h['communities']} communities, "
          f"{h['floor_plans']} floor plans, {h['under_concession']} under concession")
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay the bundled saved pages and build the rollup.")
    ap.add_argument("--pages", type=Path, default=ROOT / "sample" / "pages")
    ap.add_argument("--out", type=Path, default=ROOT / "out")
    a = ap.parse_args(argv)
    run(a.pages, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
