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
    lev = float(slot.get("leverage") or 1.0)
    venue = "futures" if lev > 1.0 + 1e-9 else "spot"
    result = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf,
        "family": "donchian_fear_greed", "atr_mode": atr_mode, "fg_mode": fg_mode,
        "fg_available": fg_ok, "bar_ts": bar_ts, "mark": mark, "close": float(bar["Close"]),
        "donch_hi": donch_hi, "donch_lo": donch_lo, "atr": round(atr, 8),
        "signal": int(sig.iloc[-1]), "raw_signal": int(raw.iloc[-1]),
        "leverage": lev, "venue": venue, "side": "LONG",
        "checked_at": now_iso_taipei(), "action": "hold", "reason": None,
        "require_reset_below_hi": bool(slot.get("require_reset_below_hi")),
    }

    # In-position: same stop/trail/donch_lo/max_hold as donchian (FG does not force exit)
    if position and position.get("status") == "FILLED":
        # Delegate to donchian manage path
        return evaluate_donchian_slot(slot, klines, position, slot_meta) | {
            "family": "donchian_fear_greed", "fg_mode": fg_mode, "fg_available": fg_ok,
            "leverage": lev, "venue": venue, "side": position.get("side") or "LONG",
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
        side="LONG", leverage=lev, venue=venue,
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
        # Spot when lev<=1; futures venue when lev>1 (same signal path)
        return evaluate_fear_greed_slot(slot, klines, position, slot_meta)
    if fam == "donchian_lev_vol":
        return evaluate_lev_vol_slot(slot, klines, position, slot_meta)
    if fam in ("donchian_long_short_btc_regime", "ls_donch_btc_regime_perp"):
        return evaluate_long_short_slot(slot, klines, position, slot_meta)
    if fam == "ls_univ_portfolio_perp":
        return evaluate_portfolio_slot(slot, klines, position, slot_meta)
    if fam == "donchian_lev":
        return {
            "slot": slot["id"], "symbol": slot["symbol"], "family": fam,
            "action": "skip", "reason": "family_has_no_gate_pass",
            "checked_at": now_iso_taipei(),
        }
    return {
        "slot": slot.get("id"), "symbol": slot.get("symbol"), "family": fam,
        "action": "skip", "reason": "unsupported_family",
        "checked_at": now_iso_taipei(),
    }


# Back-compat alias


def evaluate_lev_vol_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    """Long Donchian on futures venue; leverage metadata only (sizing at full notional)."""
    out = evaluate_donchian_slot(slot, klines, position, slot_meta)
    out["family"] = "donchian_lev_vol"
    out["venue"] = "futures"
    out["leverage"] = float(slot.get("leverage") or 1.0)
    out["side"] = "LONG"
    out["vol_target"] = slot.get("vol_target")
    return out


def evaluate_long_short_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    """Donchian long/short with BTC regime gate — matches ext_engine.signal_donchian_ls + apply_btc_regime_ls."""
    from indicators import (
        add_donch_atr, signal_donchian_ls, split_closed, bar_ts_iso, replay_stop_path, resolve_atr,
    )

    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    atr_mode = str(slot.get("atr_mode") or "wilder").strip().lower()
    donch_n = int(slot.get("donch_n") or 20)
    ind = add_donch_atr(closed, donch_n, atr_mode=atr_mode).dropna(subset=["atr", "donch_hi", "donch_lo"])
    if ind.empty:
        return {"slot": slot["id"], "symbol": slot["symbol"], "error": "no_closed_bars",
                "family": slot.get("family") or "donchian_long_short_btc_regime", "venue": "futures"}

    sig = signal_donchian_ls(ind)
    # BTC regime: long only when bull, short only when bear
    regime_on = slot_meta.get("_btc_regime_on")
    if regime_on is True:
        sig = sig.where(sig > 0, 0)  # block shorts in bull? wait: long only in bull
        # long kept, short zeroed
        sig = sig.clip(lower=0)
    elif regime_on is False:
        sig = sig.where(sig < 0, 0)  # short only in bear
        sig = sig.clip(upper=0)
    # if regime_on is None, leave both (caller should set)

    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(bar.name)
    atr = float(bar["atr"])
    mark = float(forming["Close"]) if forming is not None else float(bar["Close"])
    cur = int(sig.iloc[-1])
    prev = int(sig.iloc[-2]) if len(sig) > 1 else 0
    result = {
        "slot": slot["id"], "symbol": slot["symbol"], "tf": tf,
        "family": slot.get("family") or "donchian_long_short_btc_regime", "venue": "futures",
        "atr_mode": atr_mode, "bar_ts": bar_ts, "mark": mark, "close": float(bar["Close"]),
        "atr": round(atr, 8), "signal": cur, "leverage": float(slot.get("leverage") or 1.0),
        "btc_regime_on": regime_on, "checked_at": now_iso_taipei(),
        "action": "hold", "reason": None,
    }

    pos = position if position and position.get("status") == "FILLED" else None
    if pos:
        is_long = (pos.get("side") or "LONG").upper() == "LONG"
        # exit when signal flips away from position side
        want = 1 if is_long else -1
        if cur != want:
            result.update(action="exit", reason="signal_flip", exit_ref=float(bar["Close"]),
                          exit_bar_ts=bar_ts, side=pos.get("side"))
            return result
        # stop/trail (long: below; short: above) — reuse replay for long; short mirrored lightly
        stop_m = float(slot.get("stop_atr_mult") or slot.get("stop_m") or 1.5)
        trail_m = float(slot.get("trail_atr_mult") or slot.get("trail_m") or 1.5)
        if is_long:
            ind2 = ind.copy()
            replay = replay_stop_path(
                ind2, entry=float(pos["entry"]), entry_bar_ts=pos.get("entry_bar_ts"),
                stop_atr_mult=stop_m, trail_atr_mult=trail_m,
            )
            if replay.get("exit"):
                result.update(action="exit", reason="stop", exit_ref=replay["exit"]["exit_ref"],
                              exit_bar_ts=replay["exit"]["bar_ts"], side="LONG")
                return result
            result.update(action="manage", reason="trail_update", stop=round(float(replay["stop"]), 8),
                          side="LONG", qty=pos.get("qty"), entry=pos.get("entry"))
        else:
            # short stop above entry
            entry = float(pos["entry"])
            stop = entry + stop_m * atr
            if float(bar["High"]) >= stop:
                result.update(action="exit", reason="stop", exit_ref=stop, exit_bar_ts=bar_ts, side="SHORT")
                return result
            trail = float(bar["Close"]) + trail_m * atr
            if trail < float(pos.get("stop") or stop):
                stop = trail
            result.update(action="manage", reason="trail_update", stop=round(stop, 8),
                          side="SHORT", qty=pos.get("qty"), entry=pos.get("entry"),
                          unrealized_pct=round((entry / mark - 1.0) * 100.0, 4))
        return result

    if not slot.get("armed", True):
        result.update(action="skip", reason="not_armed")
        return result
    if slot_meta.get("last_acted_bar_ts") == bar_ts:
        result.update(action="skip", reason="idempotent_same_bar")
        return result
    # entry on edge into +/-1
    if cur == prev or cur == 0:
        result.update(action="armed", reason="waiting_ls_breakout")
        return result
    from slots import MAX_NOTIONAL_USDT
    quote = min(float(slot["quote_usdt"]), MAX_NOTIONAL_USDT)
    # Vol-target scale (ATR% → risk_frac), matching cagr70 vol_risk_frac
    vt = slot.get("vol_target")
    if vt:
        try:
            atr_pct = atr / float(bar["Close"]) if float(bar["Close"]) else 0.0
            if atr_pct > 1e-9:
                rf = max(0.05, min(1.0, float(vt) / atr_pct))
                quote = max(10.0, quote * rf)
                result["vol_risk_frac"] = round(rf, 4)
        except Exception:
            pass
    if cur > 0:
        suggested_stop = float(bar["Close"]) - float(slot.get("stop_atr_mult") or 1.5) * atr
        result.update(action="enter", reason="donchian_long", side="LONG",
                      quote_usdt=quote, suggested_stop=round(suggested_stop, 8),
                      client_order_id=f"{slot['id']}-L-{bar_ts[:16].replace(':','').replace('+','')}"[:36])
    else:
        suggested_stop = float(bar["Close"]) + float(slot.get("stop_atr_mult") or 1.5) * atr
        result.update(action="enter", reason="donchian_short", side="SHORT",
                      quote_usdt=quote, suggested_stop=round(suggested_stop, 8),
                      client_order_id=f"{slot['id']}-S-{bar_ts[:16].replace(':','').replace('+','')}"[:36])
    return result


evaluate_slot = evaluate_slot_dispatch


def _enrich_signal(res: dict, slot: dict) -> dict:
    """Stamp slot identity (from allocation runtime slot) onto the signal so
    the runner never needs a hardcoded lookup for strategy/family/venue."""
    from slots import venue_for_family
    if not isinstance(res, dict):
        return res
    fam = slot.get("family")
    res.setdefault("strategy_id", slot.get("strategy_id"))
    if fam:
        res.setdefault("family", fam)
    res.setdefault("venue", slot.get("venue") or venue_for_family(fam))
    if res.get("venue") == "futures":
        res.setdefault("leverage", slot.get("leverage") or 1)
    if res.get("action") == "enter" and not res.get("quote_usdt") and slot.get("quote_usdt"):
        res["quote_usdt"] = slot.get("quote_usdt")
    return res


def evaluate_all(client, state: dict) -> list[dict]:
    results: list[dict] = []
    positions = state.setdefault("positions", {})
    slots_meta = state.setdefault("slots", {})
    slots = state.get("runtime_eval_slots")
    if not slots:
        try:
            from slots import allocation_runtime_slots
            slots = [s for s in allocation_runtime_slots() if s.get("enabled")]
        except Exception:  # noqa: BLE001
            slots = []
    slots = slots or SLOTS
    need_btc = any(
        s.get("btc_regime")
        or s.get("family") in (
            "donchian_long_short_btc_regime",
            "ls_donch_btc_regime_perp",
            "ls_univ_portfolio_perp",
        )
        for s in slots
    )
    btc_on = btc_daily_regime_on(client) if need_btc else None
    for slot in slots:
        meta = slots_meta.setdefault(slot["id"], {})
        if slot.get("btc_regime") or slot.get("family") in (
            "donchian_long_short_btc_regime",
            "ls_donch_btc_regime_perp",
            "ls_univ_portfolio_perp",
        ):
            meta["_btc_regime_on"] = btc_on
        pos = positions.get(slot["id"])
        try:
            donch_n = int(slot.get("donch_n") or 20)
            ema_slow = int(slot.get("ema_slow") or slot.get("slow") or 0)
            limit = max(250, donch_n + 80, ema_slow + 80)
            kl = client.fetch_klines(slot["symbol"], slot["tf"], limit=limit, use_vision=True)
            res = evaluate_slot_dispatch(slot, kl, pos, meta)
            _enrich_signal(res, slot)
            results.append(res)
        except Exception as e:  # noqa: BLE001
            log.exception("evaluate_fail slot=%s", slot["id"])
            results.append({"slot": slot["id"], "symbol": slot["symbol"], "error": str(e)})
    return results




def evaluate_portfolio_slot(slot: dict, klines: pd.DataFrame, position: dict | None, slot_meta: dict) -> dict:
    """Meta-slot for ls_univ_portfolio_perp: emit rebalance plan; per-coin LS filled by caller.

    `klines` here is unused for multi-symbol; caller passes slot_meta["_port_marks"] /
    `_port_atr_pct` / `_port_signals` gathered from per-symbol evaluates.
    """
    from portfolio import (
        DEFAULT_UNIVERSE, MAX_BOOK_USDT, clamp_book, clamp_lev,
        target_weights, target_notionals, rebalance_orders, liquidation_risk,
    )
    univ = slot.get("universe") or DEFAULT_UNIVERSE
    univ = [str(s).upper().replace("USDT", "") for s in univ]
    lev = clamp_lev(slot.get("leverage") or 1.5)
    book = clamp_book(float(slot.get("quote_usdt") or MAX_BOOK_USDT))
    mode = str(slot.get("port_mode") or slot.get("mode") or "ew")
    atr_pcts = slot_meta.get("_port_atr_pct") or {}
    marks = slot_meta.get("_port_marks") or {}
    current = slot_meta.get("_port_positions") or {}
    weights = target_weights(mode, atr_pcts, univ)
    targets = target_notionals(book, lev, weights)
    intents = rebalance_orders(current, targets, marks)
    liq_alerts = []
    for sym, pos in current.items():
        mk = float(marks.get(sym) or pos.get("mark") or 0)
        if mk and pos.get("qty"):
            risk = liquidation_risk({**pos, "leverage": lev}, mk)
            if risk.get("at_risk"):
                liq_alerts.append({"symbol": sym, **risk})
                intents.append({
                    "symbol": sym, "action": "exit", "reason": "liquidation_risk",
                    "side": pos.get("side"), "qty": pos.get("qty"),
                })
    return {
        "slot": slot.get("id"),
        "symbol": "PORTFOLIO",
        "family": "ls_univ_portfolio_perp",
        "venue": "futures",
        "action": "rebalance" if intents else "hold",
        "reason": "portfolio_plan",
        "leverage": lev,
        "book_usdt": book,
        "weights": weights,
        "targets": targets,
        "intents": intents,
        "liq_alerts": liq_alerts,
        "universe": univ,
        "checked_at": now_iso_taipei(),
    }


def check_position_liquidation(position: dict, mark: float) -> dict:
    from portfolio import liquidation_risk
    return liquidation_risk(position, mark)
