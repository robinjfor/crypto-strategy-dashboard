"""READ-ONLY: from the Cloud Run job (asia-east1), fetch public USDT-M perp klines + funding via the
deployed perp_market module (no API keys, GET only, no state writes). Prints one PERP_PROBE line."""
import json
import os
import time

import perp_market as pm

out = {"base": pm.PERP_MARKET_BASE, "runner_version": os.environ.get("RUNNER_VERSION"), "at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "rows": []}
for sym, tf in (("ARBUSDT", "4h"), ("DOTUSDT", "4h"), ("APTUSDT", "4h"), ("GALAUSDT", "6h")):
    row = {"symbol": sym, "tf": tf}
    try:
        kl = pm.fetch_perp_klines(sym, tf, limit=pm.KLINES_MAX)
        row.update(klines_n=len(kl), first=str(kl.index[0]), last=str(kl.index[-1]), last_close=float(kl["Close"].iloc[-1]))
        d = pm.fetch_perp_klines(sym, "1d", limit=200)
        row.update(daily_n=len(d), daily_last=str(d.index[-1]))
        f = pm.fetch_funding(sym, limit=100)
        row.update(funding_n=len(f), funding_last=str(f.index[-1]), funding_last9_mean=float(f.iloc[-9:].mean()))
        row["ok"] = True
    except Exception as e:  # noqa: BLE001
        row.update(ok=False, error=str(e)[:300])
    out["rows"].append(row)
print("PERP_PROBE " + json.dumps(out), flush=True)
