"""Shared helpers: saved pages live under sample/pages/<case>/ (see rentdb/replay.py)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PAGES = ROOT / "sample" / "pages"


def case_dir(slug: str) -> Path:
    return PAGES / slug


def page(slug: str, name: str) -> str:
    return (PAGES / slug / name).read_text(encoding="utf-8")


def page_json(slug: str, name: str):
    return json.loads(page(slug, name))


@pytest.fixture(scope="session")
def demo_output(tmp_path_factory):
    """One offline run of the whole demo (replay -> snapshots -> rollup), shared by tests."""
    from rentdb.demo import run
    out = tmp_path_factory.mktemp("demo")
    payload = run(PAGES, out)
    return out, payload
