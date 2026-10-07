"""Tests use a frozen fixture allocation, never the live config/allocation.json
(which changes as Emily enables/disables slots and would make tests go stale)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
FIXTURE_ALLOC = Path(__file__).resolve().parent / "fixtures" / "allocation.json"

import allocation  # noqa: E402

# Patch at import time so module-level reads (e.g. parametrize lists) see the fixture too.
allocation.REPO_ALLOC_PATH = FIXTURE_ALLOC


@pytest.fixture(autouse=True)
def _fixture_allocation(monkeypatch):
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    monkeypatch.setattr(allocation, "REPO_ALLOC_PATH", FIXTURE_ALLOC)
    import slots
    slots.invalidate_allocation_cache()
    yield
    slots.invalidate_allocation_cache()
