"""Archive / restore family review state in GCS trader state."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from slots import (  # noqa: E402
    archive_family, unarchive_family, approve_family,
    archived_families_map, restored_families_map, ensure_approved_families,
)


def test_archive_revokes_and_unarchive_restores_flag():
    st = {"approved_families": ["donchian_atr"], "archived_families": {}, "restored_families": {}}
    ensure_approved_families(st)
    r = archive_family(st, "donchian_atr", at="2026-10-01T12:00:00+08:00", by="test")
    assert "donchian_atr" not in st["approved_families"]
    assert "donchian_atr" in archived_families_map(st)
    u = unarchive_family(st, "donchian_atr", at="2026-10-01T12:01:00+08:00", by="test")
    assert "donchian_atr" not in archived_families_map(st)
    assert "donchian_atr" in restored_families_map(st)


def test_approve_clears_archive_flags():
    st = {"approved_families": [], "archived_families": {"ema_cross_atr": {"reason": "manual"}},
          "restored_families": {"ema_cross_atr": {}}}
    approve_family(st, "ema_cross_atr", at="t", by="test")
    assert "ema_cross_atr" in st["approved_families"]
    assert "ema_cross_atr" not in archived_families_map(st)
    assert "ema_cross_atr" not in restored_families_map(st)
