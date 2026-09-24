"""SOL core signal — donchian20_atr_btcRegime__SOL__1d.

ATR = SMA(TrueRange, 14) — NOT Wilder (satellites use Wilder).
"""
from __future__ import annotations

import json
import math
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "crypto-dashboard-sol-core/1.0"}
VISION = "https://data-api.binance.vision"

STRATEGY_ID = "donchian20_atr_btcRegime__SOL__1d"
SYMBOL = "SOLUSDT"
BTC_SYMBOL = "BTCUSDT"
INTERVAL = "1d"
DONCH_N = 20
ATR_N = 14
ATR_STOP_MULT = 2.0
ATR_TRAIL_MULT = 3.0
USE_TRAIL = True
MAX_HOLD_BARS = 10_000
BTC_SMA_N = 200
ONE_WAY_BPS = 20.0
COST = ONE_WAY_BPS / 10_000.0
QUOTE_USDT = 1500.0
LOT_STEP = 0.001


def now_taipei() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def now_iso_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def in_daily_window(now: datetime | None = None) -> bool:
    """UTC 00:05–00:15 (Taipei 08:05–08:15)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.hour == 0 and 5 <= now.minute <= 15


def http_json(url: str, retries: int = 4, timeout: int = 60) -> Any:
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.4 * (i + 1))
    raise RuntimeError(f"HTTP failed {url}: {last}")


def fetch_klines(symbol: str, interval: str = "1d", limit: int = 500) -> pd.DataFrame:
    url = f"{VISION}/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    raw = http_json(url)
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("open_time").sort_index()


def split_closed_1d(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    now = now or datetime.now(timezone.utc)
    if df.empty:
        return df.copy()
    now_ts = pd.Timestamp(now)
    if now_ts.tzinfo is None:
        now_ts = now_ts.tz_localize("UTC")
    else:
        now_ts = now_ts.tz_convert("UTC")
    if now_ts < df.index[-1] + pd.Timedelta(days=1):
        return df.iloc[:-1].copy()
    return df.copy()


def sma_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilder_atr_ref(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    out = tr.copy().astype(float)
    out[:] = np.nan
    if len(tr) < n:
        return out
    out.iloc[n - 1] = float(tr.iloc[:n].mean())
    for i in range(n, len(tr)):
        out.iloc[i] = (float(out.iloc[i - 1]) * (n - 1) + float(tr.iloc[i])) / n
    return out


def add_core_indicators(df: pd.DataFrame) -> pd.DataFrame:
    o = df.copy()
    h, l, c = o["High"], o["Low"], o["Close"]
    o["donch20_hi"] = h.rolling(DONCH_N).max().shift(1)
    o["donch20_lo"] = l.rolling(DONCH_N).min().shift(1)
    o["atr"] = sma_atr(o, ATR_N)
    o["atr_wilder_ref"] = wilder_atr_ref(o, ATR_N)
    o["sma_200"] = c.rolling(BTC_SMA_N).mean()
    return o


def donchian_state(df: pd.DataFrame) -> pd.Series:
    hi = df["donch20_hi"].values
    lo = df["donch20_lo"].values
    closes = df["Close"].values
    hold = np.zeros(len(df), dtype=int)
    state = 0
    for i in range(len(df)):
        if np.isnan(hi[i]) or np.isnan(lo[i]):
            hold[i] = state
            continue
        if state == 0 and closes[i] > hi[i]:
            state = 1
        elif state == 1 and closes[i] < lo[i]:
            state = 0
        hold[i] = state
    return pd.Series(hold, index=df.index)


def floor_qty(qty: float, step: float = LOT_STEP) -> float:
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step))))
    floored = math.floor(qty / step) * step
    return float(f"{floored:.{precision}f}")


def compute_signal(
    sol_raw: pd.DataFrame | None = None,
    btc_raw: pd.DataFrame | None = None,
    quote_usdt: float = QUOTE_USDT,
) -> dict:
    if sol_raw is None:
        sol_raw = fetch_klines(SYMBOL, INTERVAL, 500)
    if btc_raw is None:
        btc_raw = fetch_klines(BTC_SYMBOL, INTERVAL, 500)

    sol = add_core_indicators(split_closed_1d(sol_raw))
    btc = add_core_indicators(split_closed_1d(btc_raw))
    if len(sol) < BTC_SMA_N + 5 or len(btc) < BTC_SMA_N + 5:
        raise RuntimeError("insufficient closed bars for SOL core")

    btc_a = btc.reindex(sol.index).ffill()
    raw = donchian_state(sol)
    btc_ok = (btc_a["Close"] > btc_a["sma_200"]).astype(int)
    sig = (raw.astype(int) & btc_ok.astype(int)).astype(int)

    i = -1
    row = sol.iloc[i]
    brow = btc_a.iloc[i]
    px = float(row["Close"])
    hi = float(row["donch20_hi"])
    lo = float(row["donch20_lo"])
    atr = float(row["atr"])
    atr_w = float(row["atr_wilder_ref"]) if not np.isnan(row["atr_wilder_ref"]) else None
    btc_px = float(brow["Close"])
    btc_sma = float(brow["sma_200"])
    regime_on = bool(btc_px > btc_sma)
    raw_now = int(raw.iloc[i])
    sig_now = int(sig.iloc[i])
    sig_prev = int(sig.iloc[i - 1])
    rising_edge = sig_now == 1 and sig_prev == 0
    above_hi = px > hi

    entry_px = px * (1.0 + COST)
    stop0 = entry_px - ATR_STOP_MULT * atr
    qty_raw = quote_usdt / entry_px
    qty = floor_qty(qty_raw, LOT_STEP)

    needs_reset = bool(sig_now == 1 and not rising_edge)
    would_order = bool(rising_edge and regime_on)

    if not regime_on:
        conclusion = "regime_closed"
        status = "WAIT_REGIME"
    elif rising_edge:
        conclusion = "enter_on_last_close_edge"
        status = "SIGNAL"
    elif needs_reset:
        conclusion = "wait_signal_reset_then_breakout"
        status = "WAIT_RESET"
    else:
        conclusion = "wait_breakout"
        status = "ARMED"

    dist_pct = (hi / px - 1.0) * 100.0
    dist_atr = (hi - px) / atr if atr > 0 else None

    return {
        "slot": "core_sol",
        "strategy_id": STRATEGY_ID,
        "status": status,
        "would_order": would_order,
        "generated_at_taipei": now_taipei(),
        "generated_at_iso": now_iso_taipei(),
        "in_daily_window_utc_0005_0015": in_daily_window(),
        "bar": {
            "interval": INTERVAL,
            "last_closed_open_time_utc": str(sol.index[i]),
            "last_closed_date_utc": str(sol.index[i].date()),
        },
        "sol": {
            "symbol": SYMBOL,
            "close": round(px, 6),
            "donch20_hi": round(hi, 6),
            "donch20_lo": round(lo, 6),
            "atr14_sma": round(atr, 6),
            "atr14_wilder_ref_only": None if atr_w is None else round(atr_w, 6),
            "atr_method_live": "SMA_TR_14_matches_backtest",
            "above_hi": above_hi,
            "dist_to_breakout_pct": round(dist_pct, 4),
            "dist_to_breakout_atr": None if dist_atr is None else round(dist_atr, 4),
        },
        "btc_regime": {
            "symbol": BTC_SYMBOL,
            "close": round(btc_px, 4),
            "ma_type": "SMA",
            "ma_n": BTC_SMA_N,
            "ma_value": round(btc_sma, 4),
            "regime_on": regime_on,
            "gap_pct": round((btc_px / btc_sma - 1.0) * 100.0, 4),
        },
        "signal": {
            "donchian_state": raw_now,
            "regime_filtered": sig_now,
            "prev_regime_filtered": sig_prev,
            "rising_edge_0_to_1": rising_edge,
            "needs_reset_before_entry": needs_reset,
        },
        "order_plan_if_enter_at_last_close": {
            "quote_usdt": quote_usdt,
            "entry_px_model_close_plus_20bps": round(entry_px, 6),
            "qty_raw": round(qty_raw, 8),
            "qty_floored_lot_step": qty,
            "lot_step": LOT_STEP,
            "initial_stop": round(stop0, 6),
            "stop_formula": "avgFillPrice - 2.0 * atr14_sma (live: from actual fill)",
            "trail_mult": ATR_TRAIL_MULT,
            "exchange_stop": "STOP_LOSS_LIMIT or OCO; replace daily when trail ratchets",
        },
        "conclusion": conclusion,
        "params_locked": {
            "donch_n": DONCH_N,
            "atr_n": ATR_N,
            "atr_method": "SMA_TR",
            "atr_stop_mult": ATR_STOP_MULT,
            "atr_trail_mult": ATR_TRAIL_MULT,
            "use_trail": USE_TRAIL,
            "max_hold_bars": MAX_HOLD_BARS,
            "btc_sma_n": BTC_SMA_N,
            "one_way_bps": ONE_WAY_BPS,
            "quote_usdt": QUOTE_USDT,
        },
        # satellite-compatible summary fields for dry-run feed
        "action": "enter" if would_order else ("wait_reset" if needs_reset else "armed"),
        "reason": conclusion,
        "symbol": SYMBOL,
        "tf": "1d",
        "quote_usdt": quote_usdt,
        "mark": round(px, 6),
        "donch_hi": round(hi, 6),
        "atr": round(atr, 6),
    }


def expectation_heartbeat(sig: dict) -> dict:
    return {
        "event_type": "HEARTBEAT",
        "bar_open_time_utc": sig["bar"]["last_closed_open_time_utc"],
        "decision_ts_taipei": sig["generated_at_taipei"],
        "sol_close": sig["sol"]["close"],
        "donch20_hi": sig["sol"]["donch20_hi"],
        "donch20_lo": sig["sol"]["donch20_lo"],
        "atr14_sma": sig["sol"]["atr14_sma"],
        "btc_close": sig["btc_regime"]["close"],
        "btc_sma200": sig["btc_regime"]["ma_value"],
        "regime_on": sig["btc_regime"]["regime_on"],
        "donchian_state": sig["signal"]["donchian_state"],
        "regime_filtered_signal": sig["signal"]["regime_filtered"],
        "rising_edge": sig["signal"]["rising_edge_0_to_1"],
        "needs_reset_before_entry": sig["signal"]["needs_reset_before_entry"],
        "status": sig["status"],
        "would_order": sig["would_order"],
        "expected_entry_px_model": None,
        "actual_fill_px": None,
        "slippage_bps_vs_close": None,
        "qty": None,
        "stop_before": None,
        "stop_after": None,
        "exit_reason": None,
        "backtest_replay_same_bar_action": (
            "HOLD_FLAT_NO_EDGE" if not sig["would_order"] else "ENTER_RISING_EDGE"
        ),
        "diff_vs_backtest_note": sig["conclusion"],
    }
