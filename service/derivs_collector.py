"""Daily Binance USD-M derivatives snapshot (public mainnet fapi, no keys, no LLM).
Top 30 crypto USDT perps by 24h quote volume (TradFi/stock perps + PAXG/XAUT excluded via tradfi.py)
-> gs://$GCS_BUCKET/research/derivs/YYYY-MM-DD.json.gz (UTC date of the run). Runs as Cloud Run Job crypto-derivs-daily."""
from __future__ import annotations

import gzip
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

import requests

import tradfi

BASE = os.environ.get("FAPI_BASE_URL", "https://fapi.binance.com")
WS = os.environ.get("FSTREAM_WS_URL", "wss://fstream.binance.com/ws/!forceOrder@arr")
TOP_N = int(os.environ.get("DERIVS_TOP_N", "30"))
LIQ_SECONDS = int(os.environ.get("DERIVS_LIQ_SECONDS", "60"))
FIELDS = ["symbol", "rank", "quote_volume_24h", "last_price", "price_change_pct_24h", "mark_price", "index_price",
          "funding_rate_last", "next_funding_time", "funding_rate_sum_24h", "funding_events_24h", "open_interest",
          "open_interest_usd", "oi_usd_change_pct_1d", "basis", "basis_rate", "global_ls_account_ratio",
          "global_long_account", "top_ls_position_ratio", "top_long_position", "taker_buy_sell_ratio",
          "taker_buy_vol_1d", "taker_sell_vol_1d", "liq_sample_long_usd", "liq_sample_short_usd", "liq_sample_count"]
S = requests.Session()


def get(path: str, **params):
    for i in range(4):
        r = S.get(BASE + path, params=params, timeout=15)
        if r.status_code in (418, 429) or r.status_code >= 500:
            time.sleep(2 * (i + 1)); continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def select_universe(info: dict, tickers: list[dict], n: int = TOP_N) -> list[dict]:
    excl = tradfi.tradfi_map(info)
    ok = {s["symbol"] for s in info.get("symbols", []) if tradfi.is_crypto_perp(s) and s.get("quoteAsset") == "USDT"
          and s.get("status") == "TRADING" and s["symbol"] not in excl}
    rows = [t for t in tickers if t["symbol"] in ok]
    return sorted(rows, key=lambda t: float(t.get("quoteVolume") or 0), reverse=True)[:n]


def _last(x):
    return x[-1] if isinstance(x, list) and x else {}


def collect_liqs(seconds: int) -> tuple[dict, str]:
    """!forceOrder@arr only pushes the latest liquidation per symbol per 1000ms -> a sample, not a full total."""
    agg: dict[str, dict] = {}
    try:
        import websocket  # websocket-client
    except Exception as e:  # noqa: BLE001
        return agg, f"websocket-client missing: {e}"
    stop = time.time() + seconds
    try:
        ws = websocket.create_connection(WS, timeout=5)
        while time.time() < stop:
            try:
                o = json.loads(ws.recv()).get("o") or {}
            except websocket.WebSocketTimeoutException:
                continue
            a = agg.setdefault(o.get("s"), {"long": 0.0, "short": 0.0, "n": 0})
            usd = float(o.get("ap") or o.get("p") or 0) * float(o.get("z") or o.get("q") or 0)
            a["long" if o.get("S") == "SELL" else "short"] += usd   # SELL order = long liquidated
            a["n"] += 1
        ws.close()
        return agg, "ok"
    except Exception as e:  # noqa: BLE001
        return agg, f"ws error: {e}"


def row_for(t: dict, rank: int, prem: dict, now_ms: int) -> dict:
    s = t["symbol"]
    p = prem.get(s, {})
    fr = get("/fapi/v1/fundingRate", symbol=s, startTime=now_ms - 86_400_000, limit=100) or []
    oi = get("/fapi/v1/openInterest", symbol=s)
    oih = get("/futures/data/openInterestHist", symbol=s, period="1d", limit=2) or []
    basis = _last(get("/futures/data/basis", pair=s, contractType="PERPETUAL", period="1d", limit=1))
    gls = _last(get("/futures/data/globalLongShortAccountRatio", symbol=s, period="1d", limit=1))
    tls = _last(get("/futures/data/topLongShortPositionRatio", symbol=s, period="1d", limit=1))
    tk = _last(get("/futures/data/takerlongshortRatio", symbol=s, period="1d", limit=1))
    f = lambda d, k: float(d[k]) if d.get(k) not in (None, "") else None  # noqa: E731
    oi_chg = None
    if len(oih) == 2 and float(oih[0]["sumOpenInterestValue"] or 0):
        oi_chg = (float(oih[1]["sumOpenInterestValue"]) / float(oih[0]["sumOpenInterestValue"]) - 1) * 100
    mark = f(p, "markPrice")
    return {"symbol": s, "rank": rank, "quote_volume_24h": f(t, "quoteVolume"), "last_price": f(t, "lastPrice"),
            "price_change_pct_24h": f(t, "priceChangePercent"), "mark_price": mark, "index_price": f(p, "indexPrice"),
            "funding_rate_last": f(p, "lastFundingRate"), "next_funding_time": p.get("nextFundingTime"),
            "funding_rate_sum_24h": sum(float(x["fundingRate"]) for x in fr), "funding_events_24h": len(fr),
            "open_interest": f(oi, "openInterest"),
            "open_interest_usd": float(oi["openInterest"]) * mark if mark else None,
            "oi_usd_change_pct_1d": oi_chg, "basis": f(basis, "basis"), "basis_rate": f(basis, "basisRate"),
            "global_ls_account_ratio": f(gls, "longShortRatio"), "global_long_account": f(gls, "longAccount"),
            "top_ls_position_ratio": f(tls, "longShortRatio"), "top_long_position": f(tls, "longAccount"),
            "taker_buy_sell_ratio": f(tk, "buySellRatio"), "taker_buy_vol_1d": f(tk, "buyVol"),
            "taker_sell_vol_1d": f(tk, "sellVol")}


def main() -> int:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    liq_box: dict = {}
    th = threading.Thread(target=lambda: liq_box.update(zip(("agg", "status"), collect_liqs(LIQ_SECONDS))))
    th.start()
    info = get("/fapi/v1/exchangeInfo")
    uni = select_universe(info, get("/fapi/v1/ticker/24hr"))
    prem = {x["symbol"]: x for x in get("/fapi/v1/premiumIndex")}
    rows, errors = [], {}
    for i, t in enumerate(uni, 1):
        try:
            rows.append(row_for(t, i, prem, now_ms))
        except Exception as e:  # noqa: BLE001
            errors[t["symbol"]] = str(e)[:300]
    th.join(LIQ_SECONDS + 15)
    agg = liq_box.get("agg") or {}
    for r in rows:
        a = agg.get(r["symbol"], {})
        r.update(liq_sample_long_usd=a.get("long", 0.0), liq_sample_short_usd=a.get("short", 0.0), liq_sample_count=a.get("n", 0))
    doc = {"schema": "derivs_daily_v1", "generated_at": now.isoformat(), "source": BASE, "n_symbols": len(rows),
           "fields": FIELDS, "errors": errors,
           "notes": {"liquidations": f"no REST history (allForceOrders retired); {LIQ_SECONDS}s sample of websocket "
                                     f"!forceOrder@arr (max 1 event/symbol/sec) -> liq_sample_* are a proxy, not daily totals; "
                                     f"status={liq_box.get('status')}",
                     "periods": "basis/LS/taker/OI-hist = latest 1d bucket from futures/data"},
           "rows": rows}
    name = f"research/derivs/{now:%Y-%m-%d}.json.gz"
    payload = gzip.compress(json.dumps(doc, separators=(",", ":")).encode())
    bucket = os.environ.get("GCS_BUCKET")
    if bucket:
        from google.cloud import storage
        b = storage.Client().bucket(bucket).blob(name)
        b.content_encoding = None
        b.upload_from_string(payload, content_type="application/gzip")
        print(f"wrote gs://{bucket}/{name} symbols={len(rows)} errors={len(errors)} liq={liq_box.get('status')}")
    else:
        open(os.path.basename(name), "wb").write(payload); print("wrote local", name)
    return 0 if len(rows) >= TOP_N and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
