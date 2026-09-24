#!/usr/bin/env python3
"""Assign stable family/variant codes and inject them into catalog files.

The mapping is append-only: active and retired ids remain recorded forever so
family letters and row numbers are never reused.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def alpha(n: int) -> str:
    """1-based spreadsheet-like letters: A..Z, AA.."""
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def alpha_num(code: str) -> int:
    n = 0
    for ch in code:
        if not ("A" <= ch <= "Z"):
            break
        n = n * 26 + ord(ch) - 64
    return n


def row_id(row: dict[str, Any]) -> str:
    return str(row.get("variant_id") or row.get("strategy_id") or row.get("id") or "").strip()


def family_id(group: dict[str, Any]) -> str:
    return str(group.get("strategy_family_id") or group.get("family_id") or group.get("id") or group.get("key") or "").strip()


def score(group: dict[str, Any]) -> float:
    vals = [r.get("score") for r in group.get("rows", []) if isinstance(r, dict) and isinstance(r.get("score"), (int, float))]
    return max(vals) if vals else 0.0


def ensure_mapping(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("version", 1)
    raw.setdefault("families", {})
    if not isinstance(raw["families"], dict):
        raw["families"] = {}
    return raw


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True, type=Path, help="catalog to inject")
    ap.add_argument("--mirror", type=Path, action="append", default=[], help="additional catalog copies to inject")
    ap.add_argument("--codes", type=Path, default=Path("config/strategy_codes.json"))
    args = ap.parse_args()

    catalog = load(args.catalog)
    groups = catalog.get("strategies") if isinstance(catalog, dict) else None
    if not isinstance(groups, list):
        raise SystemExit(f"catalog has no strategies array: {args.catalog}")
    mapping = ensure_mapping(load(args.codes) if args.codes.exists() else {})
    fmap: dict[str, Any] = mapping["families"]

    # The backtest page sorts families and rows by score descending, stably.
    ordered_groups = sorted(enumerate(groups), key=lambda x: (-score(x[1]), x[0]))
    used_family_codes = [alpha_num(str(v.get("code", ""))) for v in fmap.values() if isinstance(v, dict) and v.get("code")]
    next_family = max(used_family_codes or [0]) + 1
    active_families: set[str] = set()
    for _, group in ordered_groups:
        if not isinstance(group, dict):
            continue
        fid = family_id(group)
        if not fid:
            continue
        active_families.add(fid)
        entry = fmap.get(fid)
        if entry is None:
            entry = {"code": alpha(next_family), "rows": {}, "active": True}
            fmap[fid] = entry
            next_family += 1
        if not entry.get("code"):
            entry["code"] = alpha(next_family)
            next_family += 1
        entry.setdefault("rows", {})
        entry["active"] = True
        rows = group.get("rows") or []
        row_entries = entry["rows"]
        used_numbers = []
        for rv in row_entries.values():
            if isinstance(rv, dict) and str(rv.get("code", "")).startswith(str(entry["code"])):
                tail = str(rv["code"])[len(str(entry["code"])):]
                if tail.isdigit():
                    used_numbers.append(int(tail))
        next_number = max(used_numbers or [0]) + 1
        for _, row in sorted(enumerate(rows), key=lambda x: (-(float(x[1].get("score") or 0) if isinstance(x[1], dict) else 0), x[0])):
            if not isinstance(row, dict):
                continue
            rid = row_id(row)
            if not rid:
                continue
            rentry = row_entries.get(rid)
            if rentry is None:
                rentry = {"code": f"{entry['code']}{next_number}", "active": True}
                row_entries[rid] = rentry
                next_number += 1
            elif not rentry.get("code"):
                rentry["code"] = f"{entry['code']}{next_number}"
                next_number += 1
            rentry["active"] = True

    # Retain deleted ids and mark them inactive; their codes remain reserved.
    for fid, entry in fmap.items():
        if not isinstance(entry, dict):
            continue
        entry["active"] = fid in active_families
        current = next((g for g in groups if isinstance(g, dict) and family_id(g) == fid), None)
        current_ids = {row_id(r) for r in (current or {}).get("rows", []) if isinstance(r, dict)}
        for rid, rv in (entry.get("rows") or {}).items():
            if isinstance(rv, dict):
                rv["active"] = rid in current_ids

    # Inject family/row codes while preserving every other field.
    for group in groups:
        if not isinstance(group, dict):
            continue
        fid = family_id(group)
        entry = fmap.get(fid) or {}
        if entry.get("code"):
            group["code"] = entry["code"]
        for row in group.get("rows") or []:
            if not isinstance(row, dict):
                continue
            rid = row_id(row)
            rv = (entry.get("rows") or {}).get(rid) or {}
            if rv.get("code"):
                row["code"] = rv["code"]

    dump(args.codes, mapping)
    dump(args.catalog, catalog)
    for mirror in args.mirror:
        if mirror.resolve() != args.catalog.resolve():
            dump(mirror, catalog)
    print(f"assigned codes: {len(active_families)} families, {sum(len((fmap[f].get('rows') or {})) for f in active_families)} active rows")


if __name__ == "__main__":
    main()
