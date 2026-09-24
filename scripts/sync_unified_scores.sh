#!/usr/bin/env bash
set -euo pipefail
SRC_DIR="${SCORES_SRC_DIR:-/workspace/strategy-unified-3y}"
SRC="${SCORES_SRC:-$SRC_DIR/scores.json}"
CATALOG_SRC="$SRC_DIR/catalog.json"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST_DIR="$ROOT/data/unified-3y"
DST="$DST_DIR/scores.json"
mkdir -p "$DST_DIR/equity"
if [[ ! -f "$SRC" ]]; then
  echo "scores not ready: missing $SRC"
  exit 0
fi
python3 - <<'PY' "$SRC" "$DST" "$DST_DIR" "$CATALOG_SRC"
import json, sys, shutil
from pathlib import Path
src, dst, dst_dir, catalog_src = map(Path, sys.argv[1:])
raw = json.loads(src.read_text())
if isinstance(raw, list):
    raw = {"meta": {}, "strategies": raw}
if "strategies" not in raw and "rows" in raw:
    raw["strategies"] = raw.pop("rows")
raw.setdefault("meta", {})
raw.setdefault("strategies", [])
# Normalize supported_by_runner hint
for r in raw["strategies"]:
    kind = str(r.get("kind") or r.get("family") or "")
    sid = str(r.get("strategy_id") or "")
    if r.get("supported_by_runner") is None:
        fam = kind or ("donchian_btc_regime" if "btcRegime" in sid else ("donchian" if sid.startswith("donchian") else "other"))
        r["supported_by_runner"] = fam.startswith("donchian")
        r.setdefault("family", fam if fam != "other" else kind)
dst.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
sib = src.parent
eq = sib / "equity"
if eq.is_dir():
    for f in eq.glob("*.csv"):
        shutil.copy2(f, dst_dir / "equity" / f.name)
for name in ("scores.csv", "REPORT.md", "equity_3y.png"):
    p = sib / name
    if p.exists():
        shutil.copy2(p, dst_dir / name)
# Prefer catalog.json when analyst publishes it
if catalog_src.exists():
    shutil.copy2(catalog_src, dst_dir / "catalog.json")
    print(f"copied catalog.json")
print(f"synced {len(raw['strategies'])} strategies -> {dst}")
PY
