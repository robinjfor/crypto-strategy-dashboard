"""USDT-M perpetual public market data for perp-calibrated families (S short_trend_perp).

The S family is calibrated on Binance USDT-M PERP klines (data.binance.vision futures/um), not spot,
so signals/stops must use /fapi/v1/klines OHLC. Funding comes from /fapi/v1/fundingRate (event time
floored to the minute). Public endpoints only (no keys); base overridable via PERP_MARKET_BASE_URL.
Funding income per trade (FUNDING_FEE) is read from the Demo account's /fapi/v1/income."""
from __future__ import annotations

import os
import time

import pandas as pd
import requests

PERP_MARKET_BASE = os.environ.get("PERP_MARKET_BASE_URL", "https://fapi.binance.com").rstrip("/")
KLINES_MAX = 1500  # /fapi/v1/klines max limit
UA = {"User-Agent": "crypto-trader/1.0"}


def _get(path: str, params: dict, base: str | None = None):
    url = f"{(base or PERP_MARKET_BASE)}{path}"
    last = None
    for attempt in range(3):
        r = requests.get(url, params=params, headers=UA, timeout=30)
        if r.status_code == 451:
            raise RuntimeError(f"HTTP 451 geo-blocked perp market data {path}")
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r.json()
        last = r.status_code
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"perp market data {path} failed status={last}")


def klines_frame(raw: list) -> pd.DataFrame:
    cols = ["open_time", "Open", "High", "Low", "Close", "Volume", "close_time", "quote_vol",
            "trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("open_time").sort_index()


def fetch_perp_klines(symbol: str, interval: str, limit: int = KLINES_MAX, base: str | None = None) -> pd.DataFrame:
    raw = _get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": min(int(limit), KLINES_MAX)}, base)
    return klines_frame(raw)


def funding_series(rows: list) -> pd.Series:
    """fundingTime (ms) floored to the minute -> rate, sorted, de-duplicated."""
    if not rows:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([int(r["fundingTime"]) for r in rows], unit="ms", utc=True).floor("min")
    s = pd.Series([float(r["fundingRate"]) for r in rows], index=idx).sort_index()
    return s[~s.index.duplicated(keep="last")]


def fetch_funding(symbol: str, limit: int = 100, base: str | None = None) -> pd.Series:
    """Recent funding settlements (>= 9 needed for the fpos filter on the last two bars)."""
    return funding_series(_get("/fapi/v1/fundingRate", {"symbol": symbol, "limit": min(int(limit), 1000)}, base))


def funding_income_usdt(fc, symbol: str, start_ms: int, end_ms: int | None = None) -> float | None:
    """Sum of FUNDING_FEE income (USDT, + = received) for symbol in [start_ms, end_ms] on the Demo account."""
    params = {"symbol": symbol, "incomeType": "FUNDING_FEE", "startTime": int(start_ms), "limit": 1000}
    if end_ms:
        params["endTime"] = int(end_ms)
    try:
        rows = fc._signed("GET", "/fapi/v1/income", params) or []
    except Exception:  # noqa: BLE001
        return None
    return round(sum(float(r.get("income") or 0) for r in rows if r.get("incomeType") == "FUNDING_FEE"), 8)
