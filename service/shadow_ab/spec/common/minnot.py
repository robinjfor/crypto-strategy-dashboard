"""Load per-symbol min notional / stepSize from min_notional.json (override with env MIN_NOTIONAL_JSON)."""
import os, json, numpy as np
from pathlib import Path
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "min_notional.json"
def load(cols, path=None):
    p = Path(path or os.environ.get("MIN_NOTIONAL_JSON") or DEFAULT_PATH); cfg = json.load(open(p))
    d = cfg.get("default", {}); s = cfg.get("symbols", {})
    mn = np.array([float(s.get(c, {}).get("min_notional", d.get("min_notional", 5.0))) for c in cols])
    st = np.array([float(s.get(c, {}).get("step_size", d.get("step_size", 0.0))) for c in cols])
    return mn, st, str(p)
