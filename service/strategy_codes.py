"""Read the repository's append-only strategy code mapping."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_PATHS = (
    Path(__file__).with_name("strategy_codes.json"),
    Path(__file__).resolve().parents[1] / "config" / "strategy_codes.json",
)
_CACHE: dict[str, Any] | None = None


def mapping() -> dict[str, Any]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    for path in _PATHS:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _CACHE = raw
                return raw
        except (OSError, json.JSONDecodeError):
            continue
    _CACHE = {"families": {}}
    return _CACHE


def code_for(strategy_id: str | None = None, family: str | None = None) -> str | None:
    fams = mapping().get("families") or {}
    if family and isinstance(fams.get(family), dict):
        entry = fams[family]
        rows = entry.get("rows") or {}
        if strategy_id and isinstance(rows.get(strategy_id), dict):
            return rows[strategy_id].get("code")
        # Row lives under another family (caller passed a stale family):
        # the row-level code wins over the caller's family letter.
        if strategy_id and family_for_strategy_id(strategy_id):
            return code_for(strategy_id, None)
        return entry.get("code")
    if strategy_id:
        for entry in fams.values():
            if not isinstance(entry, dict):
                continue
            row = (entry.get("rows") or {}).get(strategy_id)
            if isinstance(row, dict):
                return row.get("code")
    return None


def family_for_strategy_id(strategy_id: str | None) -> str | None:
    """Family whose rows contain strategy_id (authoritative family for a variant)."""
    if not strategy_id:
        return None
    for fam, entry in (mapping().get("families") or {}).items():
        if isinstance(entry, dict) and isinstance((entry.get("rows") or {}).get(strategy_id), dict):
            return fam
    return None


def family_code(family: str | None) -> str | None:
    entry = (mapping().get("families") or {}).get(family or "")
    return entry.get("code") if isinstance(entry, dict) else None


def code_for_symbol(symbol: str | None, slots: list[dict[str, Any]]) -> str | None:
    sym = str(symbol or "").upper().replace("USDT", "")
    for slot in slots:
        ss = str(slot.get("symbol") or "").upper().replace("USDT", "")
        if ss == sym:
            return code_for(slot.get("strategy_id"), slot.get("family"))
    # Closed trades may predate strategy_id; use the unique symbol token in
    # the variant id as a deterministic fallback (e.g. ls__NEAR__...).
    token = f"__{sym}__"
    for entry in (mapping().get("families") or {}).values():
        if not isinstance(entry, dict):
            continue
        for rid, row in (entry.get("rows") or {}).items():
            if token in str(rid).upper() and isinstance(row, dict):
                return row.get("code")
    return None


def add_code(item: dict[str, Any], strategy_id: str | None = None, family: str | None = None) -> dict[str, Any]:
    out = dict(item)
    sid = strategy_id or out.get("strategy_id") or out.get("variant_id")
    fam = family or out.get("family")
    if not out.get("code"):
        code = code_for(sid, fam)
        if code:
            out["code"] = code
    return out
