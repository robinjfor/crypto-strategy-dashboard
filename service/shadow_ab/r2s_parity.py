"""Box-side: build the ledger and run demo_r2s.parity(); writes shadow_ab/parity_R2S.json and r2s_ledger_orders.json."""
import json, os, pickle, subprocess, sys
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE.parent))
import demo_r2s as D
out = os.environ.get("R2S_OUT", "/workspace/r2s")
if "--reuse" not in sys.argv:
    subprocess.run([sys.executable, str(HERE / "r2s_ledger.py")], check=True, env={**os.environ, "R2S_OUT": out})
led = pickle.load(open(out + "/r2s_ledger.pkl", "rb")); P = pd.read_pickle(out + "/r2s_panel.pkl")
res = D.parity(led, P["C"], P["H"], P["L"], P["QV"])
res["min_divergence_fallback_mins"] = D.min_divergence(led["orders"], D.EXCHANGE_MIN_DEFAULT)
json.dump(res, open(HERE / "parity_R2S.json", "w"), indent=1, default=str)
json.dump({"capital": 1000.0, "orders": led["orders"]}, open(HERE / "r2s_ledger_orders.json", "w"))
print(json.dumps({k: res[k] for k in ("days", "ledger_orders", "orders", "mismatches", "signal_day_mismatches", "skipped_lt5", "pass")}))
