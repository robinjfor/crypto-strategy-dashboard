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
print("done")
PY
