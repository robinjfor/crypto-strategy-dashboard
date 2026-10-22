#!/usr/bin/env bash
# Copy the analyst's shadow ledgers (box-only research files) into the repo so the site can show them.
# Dependency: runs on the box; A/B books are produced there daily 08:20 (Taipei) from 10/23. Run after that.
set -euo pipefail
SRC=/workspace/strategy-unified-3y/spec_2026-11
REPO=/workspace/crypto-dashboard-pages
cd "$REPO"
cp -f "$SRC/u1_shorts/shadow_tv040/ledger.csv" data/shadow/tv040_analyst.csv
if [ -d "$SRC/research_1022/shadow_AB" ]; then
  mkdir -p data/shadow/AB
  find "$SRC/research_1022/shadow_AB" -maxdepth 1 -type f \( -name '*.csv' -o -name '*.json' -o -name '*.md' \) -exec cp -f {} data/shadow/AB/ \;
fi
python3 - <<'PY'
import json, os, datetime
fs = []
for root, _, names in os.walk("data/shadow"):
    for n in names:
        if n != "index.json":
            fs.append(os.path.relpath(os.path.join(root, n), "data/shadow"))
json.dump({"files": sorted(fs), "synced_at": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")},
          open("data/shadow/index.json", "w"), ensure_ascii=False, indent=1)
PY
gh auth setup-git >/dev/null 2>&1 || true
git add data/shadow
if ! git diff --cached --quiet; then
  git -c user.email=robinjfor@users.noreply.github.com -c user.name=robinjfor commit -qm "sync analyst shadow ledgers"
  git stash -q || true; git pull -q --rebase; git push -q; git stash pop -q 2>/dev/null || true
fi
