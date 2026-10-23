"""Box-side: build the r2s_minnot ledger (3,000U, mode 2) and run demo_r2s.parity_minnot(); writes shadow_ab/parity_R2S.json."""
import json, os, pickle, subprocess, sys
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE.parent))
import demo_r2s as D
out = os.environ.get("R2S_OUT", "/workspace/r2s")
if "--reuse" not in sys.argv:
    subprocess.run([sys.executable, str(HERE / "r2s_minnot_ledger.py")], check=True, env={**os.environ, "R2S_OUT": out, "R2S_CAPITAL": str(D.CAPITAL)})
led = pickle.load(open(out + "/r2s_mn_ledger.pkl", "rb")); P = pd.read_pickle(out + "/r2s_panel.pkl")
res = D.parity_minnot(led, P["C"], P["H"], P["L"], P["QV"])
used = sorted({led["cols"][c] for _, c, *_ in led["rec"]})
res["mins_used"] = {s: [float(led["mn"][led["cols"].index(s)]), float(led["step"][led["cols"].index(s)])] for s in used}
json.dump(res, open(HERE / "parity_R2S.json", "w"), indent=1, default=str)
print(json.dumps({k: res[k] for k in ("decisions", "ledger_orders", "orders", "mismatches", "signal_day_mismatches", "kinds", "pass")}, default=str))
