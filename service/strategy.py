"""Donchian breakout + ATR trail (Wilder or SMA of TR via atr_mode)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from indicators import (
    add_donch_atr,
    bar_ts_iso,
    needs_reset_below_hi,
    replay_stop_path,
    split_closed,
)
from slots import MAX_NOTIONAL_USDT, SLOTS

log = logging.getLogger("trader.strategy")
TZ = ZoneInfo("Asia/Taipei")

def btc_daily_regime_on(client) -> bool:
    """BTC 1d close > SMA200 (reuse SOL regime definition)."""
    try:
        import numpy as np
        kl = client.fetch_klines("BTCUSDT", "1d", limit=250, use_vision=True)
        closes = kl["Close"].astype(float)
        if len(closes) < 200:
            return False
        sma = closes.rolling(200).mean().iloc[-1]
        return bool(float(closes.iloc[-1]) > float(sma))
    except Exception as e:  # noqa: BLE001
        log.warning("btc_regime_check_fail err=%s", e)
        return False



def now_iso_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def evaluate_donchian_slot(
    slot: dict,
    klines: pd.DataFrame,
    position: dict | None,
    slot_meta: dict,
) -> dict[str, Any]:
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    atr_mode = str(slot.get("atr_mode") or "wilder").strip().lower()
    if atr_mode not in ("sma", "wilder"):
        raise ValueError(f"atr_mode 必須是 sma 或 wilder，收到：{atr_mode!r}")
    donch_n = int(slot.get("donch_n") or 20)
    ind = add_donch_atr(closed, donch_n, atr_mode=atr_mode).dropna(
        subset=["atr", "donch_hi", "donch_lo"]
    )
    if ind.empty:
        return {"slot": slot["id"], "symbol": slot["symbol"], "error": "no_closed_bars"}

    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(bar.name)
    atr = float(bar["atr"])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    donch_hi = float(bar["donch_hi"])
    donch_lo = float(bar["donch_lo"])
    dist = round((float(bar["Close"]) - donch_hi) / atr, 4) if atr else None

    result: dict[str, Any] = {
        "slot": slot["id"],
        "symbol": slot["symbol"],
        "tf": tf,
        "variant": slot.get("variant"),
        "atr_mode": atr_mode,
        "require_reset_below_hi": bool(slot.get("require_reset_below_hi")),
        "bar_ts": bar_ts,
        "mark": mark,
        "close": float(bar["Close"]),
        "donch_hi": donch_hi,
        "donch_lo": donch_lo,
        "atr": round(atr, 8),
        "dist_to_breakout_atr": dist,
        "checked_at": now_iso_taipei(),
        "action": "hold",
        "reason": None,
        "stop_reference": slot.get("stop_reference"),
    }

    if position and position.get("status") == "FILLED":
        replay = replay_stop_path(
            ind,
            entry=float(position["entry"]),
            entry_bar_ts=position.get("entry_bar_ts"),
            stop_atr_mult=float(slot["stop_atr_mult"]),
            trail_atr_mult=float(slot["trail_atr_mult"]),
            initial_stop=None,
        )
        stop = float(replay["stop"])
        result["stop"] = round(stop, 8)
        result["unrealized_pct"] = round((mark / float(position["entry"]) - 1.0) * 100.0, 4)
        result["qty"] = position.get("qty")
        result["entry"] = position.get("entry")

        exit_info = replay.get("exit")
        max_hold = int(slot.get("max_hold_bars") or 0)
        hold_exit = None
        if max_hold > 0 and position.get("entry_bar_ts"):
            ets = pd.Timestamp(position["entry_bar_ts"])
            if ets.tzinfo is None:
                ets = ets.tz_localize("UTC")
            post = ind.loc[ind.index >= ets]
            if len(post) > max_hold:
                exp = post.iloc[max_hold]
                if exit_info is None or pd.Timestamp(exit_info["bar_ts"]) > exp.name:
                    hold_exit = {
                        "bar_ts": bar_ts_iso(exp.name),
                        "exit_ref": float(exp["Close"]),
                        "reason": "max_hold",
                    }

        lower_exit = None
        if float(bar["Close"]) < donch_lo and exit_info is None:
            lower_exit = {"bar_ts": bar_ts, "exit_ref": float(bar["Close"]), "reason": "donch_lo"}

        if exit_info and (hold_exit is None or exit_info["bar_ts"] <= hold_exit["bar_ts"]):
            result.update(
                action="exit",
                reason="stop",
                exit_ref=exit_info["exit_ref"],
                exit_bar_ts=exit_info["bar_ts"],
            )
        elif hold_exit:
            result.update(
                action="exit",
                reason="max_hold",
                exit_ref=hold_exit["exit_ref"],
                exit_bar_ts=hold_exit["bar_ts"],
            )
        elif lower_exit:
            result.update(
                action="exit",
                reason="donch_lo",
                exit_ref=lower_exit["exit_ref"],
                exit_bar_ts=lower_exit["bar_ts"],
            )
        else:
            result.update(action="manage", reason="trail_update")
        return result

    if not slot.get("armed", True):
        result.update(action="skip", reason="not_armed")
        return result
    if slot_meta.get("last_acted_bar_ts") == bar_ts:
        result.update(action="skip", reason="idempotent_same_bar")
        return result
    if slot.get("require_reset_below_hi") and slot_meta.get("last_exit_bar_ts"):
        if needs_reset_below_hi(ind, slot_meta.get("last_exit_bar_ts")):
            result.update(action="wait_reset", reason="need_close_below_donch_hi")
            return result
    if not (float(bar["Close"]) > donch_hi):
        result.update(action="armed", reason="waiting_breakout")
        return result

    if slot.get("btc_regime"):
        # Caller may pass _btc_regime_on in slot_meta to avoid refetch
        regime_on = slot_meta.get("_btc_regime_on")
        if regime_on is None:
            result.update(action="armed", reason="btc_regime_unchecked")
            result["btc_regime_required"] = True
            return result
        if not regime_on:
            result.update(action="armed", reason="btc_regime_off")
            result["btc_regime_on"] = False
            return result
        result["btc_regime_on"] = True

    quote = min(float(slot["quote_usdt"]), MAX_NOTIONAL_USDT)
    suggested_stop = float(bar["Close"]) - float(slot["stop_atr_mult"]) * atr
    result.update(
        action="enter",
        reason="donchian_breakout",
        quote_usdt=quote,
        suggested_stop=round(suggested_stop, 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return result




def _rising_edge(sig: pd.Series) -> bool:
    if len(sig) < 2:
        return False
    return int(sig.iloc[-1]) == 1 and int(sig.iloc[-2]) == 0


def _apply_btc_regime_to_sig(sig: pd.Series, regime_on: bool | None) -> pd.Series:
    if regime_on is None:
        return sig
    if not regime_on:
        return pd.Series(0, index=sig.index, dtype=int)
    return sig.astype(int)


def evaluate_ema_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    """EMA cross + ATR stop/trail (+ optional BTC regime). Matches engine.signal_ema + run_donchian_engine."""
    from indicators import add_ema, resolve_atr, split_closed, bar_ts_iso, replay_stop_path

    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    atr_mode = str(slot.get("atr_mode") or "sma").strip().lower()
    fast = int(slot.get("ema_fast") or slot.get("fast") or 12)
    slow = int(slot.get("ema_slow") or slot.get("slow") or 26)
    ind = add_ema(closed, (fast, slow))
    ind["atr"] = resolve_atr(ind, 14, atr_mode)
    ind = ind.dropna(subset=["atr", f"ema_{fast}", f"ema_{slow}"])
    if ind.empty:
        return {"slot": slot["id"], "symbol": slot["symbol"], "error": "no_closed_bars", "family": "ema_cross_atr"}

    from indicators import signal_ema
    sig = signal_ema(ind, fast, slow)
    if slot.get("btc_regime"):
        regime_on = slot_meta.get("_btc_regime_on")
        if regime_on is False:
            sig = pd.Series(0, index=sig.index, dtype=int)
        # if None, leave signal but gate entry later

    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(bar.name)
    atr = float(bar["atr"])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    result = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf,
        "family": "ema_cross_atr", "atr_mode": atr_mode,
        "bar_ts": bar_ts, "mark": mark, "close": float(bar["Close"]),
        "atr": round(atr, 8), "ema_fast": float(bar[f"ema_{fast}"]),
        "ema_slow": float(bar[f"ema_{slow}"]), "signal": int(sig.iloc[-1]),
        "checked_at": now_iso_taipei(), "action": "hold", "reason": None,
    }

    if position and position.get("status") == "FILLED":
        # reuse ATR trail; donch columns unused — synthesize flat donch for replay helper
        ind2 = ind.copy()
        ind2["donch_hi"] = ind2["High"]
        ind2["donch_lo"] = ind2["Low"]
        replay = replay_stop_path(
            ind2, entry=float(position["entry"]), entry_bar_ts=position.get("entry_bar_ts"),
            stop_atr_mult=float(slot.get("stop_atr_mult") or slot.get("stop_m") or 2.0),
            trail_atr_mult=float(slot.get("trail_atr_mult") or slot.get("trail_m") or 3.0),
        )
        stop = float(replay["stop"])
        result["stop"] = round(stop, 8)
        result["unrealized_pct"] = round((mark / float(position["entry"]) - 1.0) * 100.0, 4)
        result["qty"] = position.get("qty")
        result["entry"] = position.get("entry")
        exit_info = replay.get("exit")
        # signal exit when ema cross down
        if int(sig.iloc[-1]) == 0:
            result.update(action="exit", reason="signal_exit", exit_ref=float(bar["Close"]), exit_bar_ts=bar_ts)
            return result
        max_hold = int(slot.get("max_hold_bars") or slot.get("max_hold") or 0)
        if max_hold > 0 and position.get("entry_bar_ts"):
            ets = pd.Timestamp(position["entry_bar_ts"])
            if ets.tzinfo is None:
                ets = ets.tz_localize("UTC")
            post = ind.loc[ind.index >= ets]
            if len(post) > max_hold:
                result.update(action="exit", reason="max_hold", exit_ref=float(bar["Close"]), exit_bar_ts=bar_ts)
                return result
        if exit_info:
            result.update(action="exit", reason="stop", exit_ref=exit_info["exit_ref"], exit_bar_ts=exit_info["bar_ts"])
        else:
            result.update(action="manage", reason="trail_update")
        return result

    if not slot.get("armed", True):
        result.update(action="skip", reason="not_armed")
        return result
    if slot_meta.get("last_acted_bar_ts") == bar_ts:
        result.update(action="skip", reason="idempotent_same_bar")
        return result
    if slot.get("btc_regime") and slot_meta.get("_btc_regime_on") is False:
        result.update(action="armed", reason="btc_regime_off", btc_regime_on=False)
        return result
    if not _rising_edge(sig):
        result.update(action="armed", reason="waiting_ema_cross")
        return result
    quote = min(float(slot["quote_usdt"]), MAX_NOTIONAL_USDT)
    suggested_stop = float(bar["Close"]) - float(slot.get("stop_atr_mult") or 2.0) * atr
    result.update(
        action="enter", reason="ema_cross",
        quote_usdt=quote, suggested_stop=round(suggested_stop, 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return result


def evaluate_fear_greed_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    """Donchian ATR long + fear/greed entry filter. FG unavailable → no new entries."""
    from indicators import add_donch_atr, signal_donchian_state, split_closed, bar_ts_iso, needs_reset_below_hi, replay_stop_path
    from fear_greed import fetch_fear_greed, apply_fg_filter

    # Start from donchian evaluation on filtered signal by temporarily checking rising edge of FG-filtered optimal
    # Re-implement entry gating with FG
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    atr_mode = str(slot.get("atr_mode") or "wilder").strip().lower()
    donch_n = int(slot.get("donch_n") or 20)
    ind = add_donch_atr(closed, donch_n, atr_mode=atr_mode).dropna(subset=["atr", "donch_hi", "donch_lo"])
    if ind.empty:
        return {"slot": slot["id"], "symbol": slot["symbol"], "error": "no_closed_bars", "family": "donchian_fear_greed"}

    raw = signal_donchian_state(ind)
    fg_mode = str(slot.get("fg_mode") or "none")
    fg = fetch_fear_greed()
    sig = apply_fg_filter(raw, fg, fg_mode)
    fg_ok = fg is not None

    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(bar.name)
    atr = float(bar["atr"])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    donch_hi = float(bar["donch_hi"])
    donch_lo = float(bar["donch_lo"])
    result = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf,
        "family": "donchian_fear_greed", "atr_mode": atr_mode, "fg_mode": fg_mode,
        "fg_available": fg_ok, "bar_ts": bar_ts, "mark": mark, "close": float(bar["Close"]),
        "donch_hi": donch_hi, "donch_lo": donch_lo, "atr": round(atr, 8),
        "signal": int(sig.iloc[-1]), "raw_signal": int(raw.iloc[-1]),
        "checked_at": now_iso_taipei(), "action": "hold", "reason": None,
        "require_reset_below_hi": bool(slot.get("require_reset_below_hi")),
    }

    # In-position: same stop/trail/donch_lo/max_hold as donchian (FG does not force exit)
    if position and position.get("status") == "FILLED":
        # Delegate to donchian manage path
        return evaluate_donchian_slot(slot, klines, position, slot_meta) | {
            "family": "donchian_fear_greed", "fg_mode": fg_mode, "fg_available": fg_ok
        }

    if not slot.get("armed", True):
        result.update(action="skip", reason="not_armed")
        return result
    if slot_meta.get("last_acted_bar_ts") == bar_ts:
        result.update(action="skip", reason="idempotent_same_bar")
        return result
    if not fg_ok:
        result.update(action="armed", reason="fg_unavailable_no_entry")
        return result
    if slot.get("require_reset_below_hi") and slot_meta.get("last_exit_bar_ts"):
        if needs_reset_below_hi(ind, slot_meta.get("last_exit_bar_ts")):
            result.update(action="wait_reset", reason="need_close_below_donch_hi")
            return result
    if not _rising_edge(sig):
        result.update(action="armed", reason="waiting_breakout_or_fg")
        return result
    quote = min(float(slot["quote_usdt"]), MAX_NOTIONAL_USDT)
    suggested_stop = float(bar["Close"]) - float(slot.get("stop_atr_mult") or 2.0) * atr
    result.update(
        action="enter", reason="donchian_breakout_fg",
        quote_usdt=quote, suggested_stop=round(suggested_stop, 8),
        client_order_id=f"{slot['id']}-{bar_ts[:16].replace(':', '').replace('+', '')}"[:36],
    )
    return result


def evaluate_slot_dispatch(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    fam = str(slot.get("family") or "donchian_atr").strip().lower()
    if fam in ("donchian", "donchian_atr", "donchian_btc_regime"):
        return evaluate_donchian_slot(slot, klines, position, slot_meta)
    if fam == "ema_cross_atr":
        return evaluate_ema_slot(slot, klines, position, slot_meta)
    if fam == "donchian_fear_greed":
        # Spot path only for leverage<=1; futures path handled separately
        lev = float(slot.get("leverage") or 1.0)
        if lev > 1.0:
            return {
                "slot": slot["id"], "symbol": slot["symbol"], "family": fam,
                "action": "skip", "reason": "futures_required", "leverage": lev,
                "checked_at": now_iso_taipei(),
            }
        return evaluate_fear_greed_slot(slot, klines, position, slot_meta)
    if fam in ("donchian_lev_vol", "donchian_long_short_btc_regime", "donchian_lev"):
        return {
            "slot": slot["id"], "symbol": slot["symbol"], "family": fam,
            "action": "skip", "reason": "futures_required",
            "checked_at": now_iso_taipei(),
        }
    return {
        "slot": slot.get("id"), "symbol": slot.get("symbol"), "family": fam,
        "action": "skip", "reason": "unsupported_family",
        "checked_at": now_iso_taipei(),
    }


# Back-compat alias
evaluate_slot = evaluate_slot_dispatch


def evaluate_all(client, state: dict) -> list[dict]:
    results: list[dict] = []
    positions = state.setdefault("positions", {})
    slots_meta = state.setdefault("slots", {})
    need_btc = any(s.get("btc_regime") for s in SLOTS)
    btc_on = btc_daily_regime_on(client) if need_btc else None
    for slot in SLOTS:
        meta = slots_meta.setdefault(slot["id"], {})
        if slot.get("btc_regime"):
            meta["_btc_regime_on"] = btc_on
        pos = positions.get(slot["id"])
        try:
            donch_n = int(slot.get("donch_n") or slot.get("donch_n") or 20)
            limit = max(250, donch_n + 80)
            kl = client.fetch_klines(slot["symbol"], slot["tf"], limit=limit, use_vision=True)
            results.append(evaluate_slot_dispatch(slot, kl, pos, meta))
        except Exception as e:  # noqa: BLE001
            log.exception("evaluate_fail slot=%s", slot["id"])
            results.append({"slot": slot["id"], "symbol": slot["symbol"], "error": str(e)})
    return results


