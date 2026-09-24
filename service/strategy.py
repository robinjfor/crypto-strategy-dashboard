"""Donchian breakout + Wilder ATR trail."""
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


def now_iso_taipei() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def evaluate_slot(
    slot: dict,
    klines: pd.DataFrame,
    position: dict | None,
    slot_meta: dict,
) -> dict[str, Any]:
    tf = slot["tf"]
    closed, forming = split_closed(klines, tf)
    ind = add_donch_atr(closed, int(slot["donch_n"])).dropna(
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
        "variant": slot["variant"],
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


def evaluate_all(client, state: dict) -> list[dict]:
    results: list[dict] = []
    positions = state.setdefault("positions", {})
    slots_meta = state.setdefault("slots", {})
    for slot in SLOTS:
        meta = slots_meta.setdefault(slot["id"], {})
        pos = positions.get(slot["id"])
        try:
            limit = max(250, int(slot["donch_n"]) + 80)
            kl = client.fetch_klines(slot["symbol"], slot["tf"], limit=limit, use_vision=True)
            results.append(evaluate_slot(slot, kl, pos, meta))
        except Exception as e:  # noqa: BLE001
            log.exception("evaluate_fail slot=%s", slot["id"])
            results.append({"slot": slot["id"], "symbol": slot["symbol"], "error": str(e)})
    return results


