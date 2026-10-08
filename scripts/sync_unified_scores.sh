#!/usr/bin/env bash
set -euo pipefail
SRC_DIR=""
for d in /workspace/strategy-unified-3y /workspace/strategy-unified-3y; do
  if [[ -f "$d/scores.json" || -f "$d/catalog.json" || -f "$d/catalog/catalog.json" ]]; then
    SRC_DIR="$d"; break
  fi
done
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="$ROOT/data/unified-3y"
mkdir -p "$DST_DIR/equity"
if [[ -z "$SRC_DIR" ]]; then
  echo "scores/catalog not ready"
  exit 0
fi
python3 - "$SRC_DIR" "$DST_DIR" <<'PY'
import json, sys, shutil
from pathlib import Path
src_dir, dst_dir = map(Path, sys.argv[1:])
dst_dir.mkdir(parents=True, exist_ok=True)
(eq := dst_dir / "equity").mkdir(exist_ok=True)

scores = src_dir / "scores.json"
if scores.exists():
    raw = json.loads(scores.read_text())
    if isinstance(raw, list):
        raw = {"meta": {}, "strategies": raw}
    if "strategies" not in raw and "rows" in raw:
        raw["strategies"] = raw.pop("rows")
    raw.setdefault("meta", {})
    raw.setdefault("strategies", [])
    for r in raw["strategies"]:
        kind = str(r.get("kind") or r.get("family") or "")
        sid = str(r.get("strategy_id") or "")
        if r.get("supported_by_runner") is None:
            low = sid.lower()
            fam = kind or (
                "donchian_btc_regime" if ("btcregime" in low or "btc_regime" in low) else
                ("donchian" if ("donchian" in low or "donchian" in low) else "other")
            )
            r["supported_by_runner"] = str(fam).startswith("donchian")
            r.setdefault("family", fam if fam != "other" else kind)
    (dst_dir / "scores.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    print(f"synced scores {len(raw['strategies'])} -> {dst_dir/'scores.json'}")

catalog_src = None
for cand in (src_dir / "catalog.json", src_dir / "catalog" / "catalog.json"):
    if cand.exists():
        shutil.copy2(cand, dst_dir / "catalog.json")
        catalog_src = cand
        print(f"copied catalog from {cand}")
        break
if catalog_src is not None:
    # Emily 2026-09-25: news families retired — drop on every sync
    cat_path = dst_dir / "catalog.json"
    cat = json.loads(cat_path.read_text())
    hidden = {"news_burst_confirm", "news_filter_donchian"}
    if isinstance(cat, dict) and isinstance(cat.get("strategies"), list):
        cat["strategies"] = [s for s in cat["strategies"] if s.get("strategy_family_id") not in hidden]
        cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
        print(f"filtered hidden families {sorted(hidden)} -> {len(cat['strategies'])} remain")
    import subprocess
    subprocess.run([
        sys.executable, str(dst_dir.parent.parent / "scripts" / "assign_codes.py"),
        "--catalog", str(dst_dir / "catalog.json"),
        "--mirror", str(catalog_src),
        "--codes", str(dst_dir.parent.parent / "config" / "strategy_codes.json"),
    ], check=True)
for name in ("CATALOG.md",):
    for cand in (src_dir / name, src_dir / "catalog" / name):
        if cand.exists():
            shutil.copy2(cand, dst_dir / name)
            break
for name in ("scores.csv", "REPORT.md", "equity_3y.png"):
    p = src_dir / name
    if p.exists():
        shutil.copy2(p, dst_dir / name)
eq_src = src_dir / "equity"
if eq_src.is_dir():
    for f in eq_src.glob("*.csv"):
        shutil.copy2(f, eq / f.name)
# Lookahead-corrected curves live at lookahead_fix/equity/<CODE>.csv (catalog
# row.lookahead_fix.strict_equity_csv). The page fetches equity/<strategy_id>.csv,
# so copy them under the strategy_id name (overrides the pre-fix curve).
try:
    _cat = json.loads((dst_dir / "catalog.json").read_text(encoding="utf-8"))
    _n = 0
    for _f in _cat.get("strategies") or []:
        for _r in _f.get("rows") or []:
            _sid = _r.get("strategy_id")
            _src = ((_r.get("lookahead_fix") or {}).get("strict_equity_csv")
                    or _r.get("equity_csv"))
            if not _sid or not _src:
                continue
            _sp = Path(_src)
            if not _sp.is_absolute():
                _sp = src_dir / _sp
            if _sp.exists():
                shutil.copy2(_sp, eq / f"{_sid}.csv")
                _n += 1
    print(f"copied {_n} row equity curves (lookahead_fix / equity_csv)")
except Exception as _e:  # noqa: BLE001
    print("row equity copy skipped:", _e)
# --- site overrides (config/catalog_overrides.json) + scores.json from current catalog ---
try:
    import datetime as _dt
    _root = dst_dir.parent.parent
    _ov_p = _root / "config" / "catalog_overrides.json"
    _ov = json.loads(_ov_p.read_text(encoding="utf-8")) if _ov_p.exists() else {}
    _cat_p = dst_dir / "catalog.json"
    _cat = json.loads(_cat_p.read_text(encoding="utf-8"))
    _n_ov = 0
    for _f in _cat.get("strategies") or []:
        for _a, _b in (_ov.get("family_text_replace") or {}).get(_f.get("strategy_family_id"), []):
            for _k, _v in list(_f.items()):
                if isinstance(_v, str) and _a in _v:
                    _f[_k] = _v.replace(_a, _b); _n_ov += 1
        for _r in _f.get("rows") or []:
            _sid = _r.get("strategy_id")
            if _sid in (_ov.get("row_status") or {}):
                _r["status_catalog"] = _r.get("status"); _r["status"] = _ov["row_status"][_sid]; _n_ov += 1
            if _sid in (_ov.get("row_notes") or {}):
                _r["site_note_zh"] = _ov["row_notes"][_sid]; _n_ov += 1
    _cat_p.write_text(json.dumps(_cat, ensure_ascii=False, indent=2) + "\n")
    # scores.json was a stale 9/24 export; rebuild it from the current catalog.
    _old = json.loads((dst_dir / "scores.json").read_text()) if (dst_dir / "scores.json").exists() else {}
    _meta = dict(_old.get("meta") or {})
    _now_tpe = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
    # REAUDIT2 E3: generated_at is the time of THIS sync (not the stale 9/24 export).
    _meta.update({"generated_from": "catalog.json (sync_unified_scores.sh)",
                  "generated_at": _now_tpe.strftime("%Y-%m-%d %H:%M:%S CST"),
                  "generated_at_taipei": _now_tpe.isoformat(timespec="seconds"),
                  "catalog_meta": _cat.get("meta")})
    _rows = []
    for _f in _cat.get("strategies") or []:
        for _r in _f.get("rows") or []:
            _p = _r.get("params") or {}
            _rows.append({
                "strategy_id": _r.get("strategy_id"), "code": _r.get("code"),
                "family": _f.get("strategy_family_id"), "symbol": _r.get("symbol"), "timeframe": _r.get("timeframe"),
                "status": _r.get("status"), "params": _p, "kind": _f.get("strategy_family_id"),
                "initial": _r.get("initial"), "final": _r.get("final"),
                "ret_3y": _r.get("ret_3y"), "total_return_3y": _r.get("total_return_3y"), "cagr_3y": _r.get("cagr_3y"),
                "ret_1y": _r.get("ret_1y"), "bh_ret_3y": _r.get("bh_ret_3y"),
                "maxdd": _r.get("maxdd_3y", _r.get("maxdd")), "oos_pass": _r.get("oos_pass_3y", _r.get("oos_pass")),
                "n_trades": _r.get("n_trades_3y", _r.get("n_trades")),
                "gate_pass": _r.get("gate_pass_3y"), "gate_pass_3y": _r.get("gate_pass_3y"),
                "gate_pass_both": _r.get("gate_pass_both"), "gate_fail_reasons": _r.get("gate_fail_reasons"),
                "score": _r.get("score") if _r.get("gate_pass_3y") else 0.0,
                "full_period": _r.get("full_period"),
                "robust_neighbor_pct": _r.get("robust_neighbor_pct") if _r.get("robust_neighbor_pct") is not None
                    else _r.get("neighbor_pct_close_fill", _r.get("neighbor_pct_live_reval")),
                "maxdd_next_open": (_r.get("next_open_reval") or {}).get("maxdd"),
                "neighbor_pct_next_open": (_r.get("next_open_reval") or {}).get("neighbor_pct"),
                "site_note_zh": _r.get("site_note_zh"),
                "supported_by_runner": _f.get("cloud_supported", str(_f.get("strategy_family_id") or "").startswith("donchian")),
            })
    (dst_dir / "scores.json").write_text(json.dumps({"meta": _meta, "strategies": _rows}, ensure_ascii=False, indent=2) + "\n")
    print(f"overrides applied {_n_ov}; scores.json rebuilt from catalog: {len(_rows)} rows")
except Exception as _e:  # noqa: BLE001
    print("overrides/scores rebuild FAILED:", _e); raise
print("done")
PY
