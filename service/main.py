#!/usr/bin/env python3
"""Binance Demo trader — Cloud Run Job entrypoint.

Modes:
  probe    — ping / time / signed account (nonzero balances only; never print keys)
  dry-run  — compute signals & intended orders; place nothing
  run      — reconcile + place when TRADER_MODE=live AND TRADER_ENABLED=true

Env:
  BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET
  BINANCE_BASE_URL   default https://demo-api.binance.com
  VISION_BASE_URL    default https://data-api.binance.vision
  GCS_BUCKET
  TRADER_MODE        dry-run | live   (default dry-run)
  TRADER_ENABLED     true | false
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from binance_client import BinanceClient
from execution import client_order_id, market_close_slot, place_hard_stop, replace_trail_stop
from slots import MAX_NOTIONAL_USDT, SLOTS, SOL_SLOT, strategy_id_for_slot, DEFAULT_APPROVED, DEFAULT_SIGNAL_ONLY, approval_mode, is_approved_live, ensure_approved_families
from allocation import resolve_allocation, live_slots as alloc_live_slots, slot_to_runtime, validate_allocation
from slots import ensure_approved_families
from state_store import StateStore
from strategy import evaluate_all, now_iso_taipei
from sol_core.signal import compute_signal as sol_compute_signal, expectation_heartbeat, in_daily_window

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trader")


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def trader_mode() -> str:
    return (os.environ.get("TRADER_MODE") or "dry-run").strip().lower()


def build_feed(client: BinanceClient | None, state: dict, signals: list[dict], mode: str) -> dict:
    balances: list = []
    trades: dict[str, list] = {}
    if client and client.api_key:
        try:
            balances = client.nonzero_balances()
            for slot in SLOTS:
                sym = slot["symbol"]
                try:
                    trades[sym] = client.my_trades(sym, limit=20)
                except Exception as e:  # noqa: BLE001
                    trades[sym] = [{"error": str(e)}]
            try:
                trades["SOLUSDT"] = client.my_trades("SOLUSDT", limit=20)
            except Exception as e:  # noqa: BLE001
                trades["SOLUSDT"] = [{"error": str(e)}]
        except Exception as e:  # noqa: BLE001
            log.warning("feed_account_skip err=%s", e)
    return {
        "updated_at": now_iso_taipei(),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": "binance_demo_cloudrun",
        "mode": mode,
        "paused": bool(state.get("paused")),
        "trader_enabled": env_bool("TRADER_ENABLED", True),
        "region_hint": "asia-east1",
        "balances": balances,
        "myTrades": trades,
        "slots": signals,
        "positions": state.get("positions", {}),
        "closed_trades": state.get("closed_trades", [])[-50:],
        "meta": state.get("meta", {}),
        "sol_expectation_log": state.get("expectation_log", [])[-20:],
    }


def cmd_probe(client: BinanceClient) -> int:
    log.info("probe_start base=%s", client.base_url)
    ping = client.ping()
    t = client.server_time()
    server_ms = int(t.get("serverTime") or 0)
    local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    log.info("probe_ping ok=%s", ping == {})
    log.info("probe_time serverTime=%s skew_ms=%s", server_ms, local_ms - server_ms)
    acct = client.account()
    bals = client.nonzero_balances(acct)
    log.info("probe_account nonzero_balances=%s", json.dumps(bals))
    log.info("probe_ok canTrade=%s accountType=%s", acct.get("canTrade"), acct.get("accountType"))
    return 0




def _iter_trade_slots(state: dict) -> list[dict]:
    """Yield runtime slots allowed to open new entries (allocation ∩ approved_families)."""
    fams = ensure_approved_families(state)
    try:
        doc, src = resolve_allocation()
        errs = validate_allocation(doc, check_binance=False)
        if errs:
            log.warning("allocation_invalid_using_last_good errs=%s src=%s", errs, src)
            doc = state.get("allocation_last_good") or doc
            if errs and not state.get("allocation_last_good"):
                # no last good — trade nothing new
                state["allocation_alert"] = {"errors": errs}
                return []
        else:
            state["allocation_last_good"] = doc
            state["allocation_alert"] = None
        live = alloc_live_slots(doc, fams)
        return [slot_to_runtime(s) for s in live]
    except Exception as e:  # noqa: BLE001
        log.warning("allocation_resolve_fail fallback_defaults err=%s", e)
        # Fallback: legacy DEFAULT_APPROVED live slots
        out = []
        for s in SLOTS:
            if approval_mode(state, s.get("strategy_id") or "") == "live":
                out.append(s)
        return out


def _approved_ids(state: dict) -> set[str]:
    """Strategy IDs allowed to place Demo orders (family approved ∩ allocation enabled)."""
    out = set()
    try:
        for slot in _iter_trade_slots(state):
            sid = slot.get("strategy_id")
            if sid:
                out.add(sid)
        if out:
            return out
    except Exception as e:  # noqa: BLE001
        log.warning("approved_ids_alloc_fail err=%s", e)
    # Fallback: approval_mode live
    for s in SLOTS + ([SOL_SLOT] if SOL_SLOT else []):
        sid = s.get("strategy_id")
        if sid and approval_mode(state, sid) == "live":
            out.add(sid)
    if not out:
        # last resort seed
        out = set(DEFAULT_APPROVED.keys())
    return out


def _allow_entry_for_slot(state: dict, slot_id: str, *, paused: bool) -> tuple[bool, str]:
    if paused:
        return False, "paused_no_new_entries"
    sid = strategy_id_for_slot(slot_id)
    if not sid:
        return False, "unknown_slot_strategy"
    if sid not in _approved_ids(state):
        return False, "not_approved"
    mode = approval_mode(state, sid)
    if mode == "signal_only":
        return False, "signal_only"
    return True, ""


def apply_signal(client: BinanceClient, state: dict, sig: dict, live: bool, *, allow_entries: bool) -> dict:
    """Apply one satellite/SOL-shaped signal. Spot only. When paused, skip enters only."""
    slot_id = sig.get("slot")
    action = sig.get("action")
    out = dict(sig)
    out["executed"] = False
    if sig.get("error"):
        return out

    positions = state.setdefault("positions", {})
    slots_meta = state.setdefault("slots", {})
    meta = slots_meta.setdefault(slot_id, {})

    if action == "enter":
        ok_entry, why = _allow_entry_for_slot(state, slot_id, paused=not allow_entries)
        # allow_entries already False when globally paused; also block unapproved
        if not allow_entries or not ok_entry:
            out["reason"] = why if not ok_entry else "paused_no_new_entries"
            log.info("skip_enter slot=%s reason=%s", slot_id, out["reason"])
            return out
        quote = min(float(sig.get("quote_usdt") or 0), MAX_NOTIONAL_USDT)
        bar_ts = str(sig.get("bar_ts") or "")
        coid = sig.get("client_order_id") or client_order_id(slot_id, "enter", bar_ts)
        log.info(
            "intent_enter symbol=%s quote=%s coid=%s live=%s stop=%s",
            sig["symbol"], quote, coid, live, sig.get("suggested_stop"),
        )
        if not live:
            out["intended_order"] = {
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": quote,
                "clientOrderId": coid,
            }
            return out
        if positions.get(slot_id, {}).get("status") == "FILLED":
            out["reason"] = "already_filled"
            return out
        try:
            order = client.market_buy(sig["symbol"], quote, coid)
            fills = order.get("fills") or []
            qty = float(order.get("executedQty") or 0)
            if fills:
                notional = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                qty2 = sum(float(f["qty"]) for f in fills)
                px = notional / qty2 if qty2 else float(sig["close"])
            else:
                px = float(order.get("price") or sig["close"])
                qty = qty or (quote / px if px else 0)
            atr = float(sig.get("atr") or 0)
            stop = float(sig.get("suggested_stop") or (px - 2 * atr))
            positions[slot_id] = {
                "status": "FILLED",
                "symbol": sig["symbol"],
                "qty": qty,
                "entry": px,
                "entry_bar_ts": sig.get("bar_ts"),
                "stop": stop,
                "donch_lo": sig.get("donch_lo"),
                "client_order_id": coid,
                "order_id": order.get("orderId"),
                "filled_at": now_iso_taipei(),
            }
            # Hard exchange stop immediately after fill (spot STOP_LOSS_LIMIT)
            try:
                stop_ord = place_hard_stop(client, sig["symbol"], qty, stop, slot_id)
                positions[slot_id]["stop_order_id"] = stop_ord.get("orderId")
                positions[slot_id]["stop_client_order_id"] = stop_ord.get("clientOrderId")
            except Exception as e:  # noqa: BLE001
                log.error("hard_stop_fail_after_fill slot=%s err=%s", slot_id, e)
                positions[slot_id]["stop_error"] = str(e)
            meta["last_acted_bar_ts"] = sig.get("bar_ts")
            out["executed"] = True
            out["fill"] = {"qty": qty, "price": px, "orderId": order.get("orderId")}
            log.info("filled_enter symbol=%s qty=%s px=%s stop=%s", sig["symbol"], qty, px, stop)
        except Exception as e:  # noqa: BLE001
            log.error("enter_fail symbol=%s err=%s", sig["symbol"], e)
            out["error"] = str(e)
        return out

    if action == "exit":
        pos = positions.get(slot_id) or {}
        qty = float(pos.get("qty") or 0)
        coid = client_order_id(slot_id, "exit", str(sig.get("exit_bar_ts") or sig.get("bar_ts") or ""))
        log.info(
            "intent_exit symbol=%s qty=%s reason=%s ref=%s live=%s",
            sig["symbol"], qty, sig.get("reason"), sig.get("exit_ref"), live,
        )
        if not live:
            out["intended_order"] = {
                "side": "SELL",
                "type": "MARKET",
                "quantity": qty,
                "clientOrderId": coid,
            }
            return out
        if qty <= 0:
            out["reason"] = "no_qty"
            return out
        try:
            try:
                client.cancel_open_orders(sig["symbol"])
            except Exception as e:  # noqa: BLE001
                log.warning("cancel_before_exit_skip err=%s", e)
            order = client.market_sell(sig["symbol"], qty, coid)
            entry = float(pos.get("entry") or 0)
            fill_px = float(sig.get("mark") or sig.get("close") or entry)
            fills = order.get("fills") or []
            if fills:
                notional = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                qty2 = sum(float(f["qty"]) for f in fills)
                fill_px = notional / qty2 if qty2 else fill_px
            closed = {
                "slot": slot_id,
                "symbol": sig["symbol"],
                "qty": qty,
                "entry": entry,
                "exit": fill_px,
                "pnl_usdt": round((fill_px - entry) * qty, 4) if entry else None,
                "reason": sig.get("reason") or "exit",
                "closed_at": now_iso_taipei(),
                "order_id": order.get("orderId"),
            }
            state.setdefault("closed_trades", []).append(closed)
            state["closed_trades"] = state["closed_trades"][-200:]
            positions.pop(slot_id, None)
            meta["last_acted_bar_ts"] = sig.get("exit_bar_ts") or sig.get("bar_ts")
            meta["last_exit_bar_ts"] = sig.get("exit_bar_ts") or sig.get("bar_ts")
            meta["last_exit_reason"] = closed["reason"]
            if slot_id == "core_sol":
                meta["needs_reset_latch"] = True
            out["executed"] = True
            out["fill"] = {"orderId": order.get("orderId"), "executedQty": order.get("executedQty")}
            log.info("filled_exit symbol=%s", sig["symbol"])
        except Exception as e:  # noqa: BLE001
            log.error("exit_fail symbol=%s err=%s", sig["symbol"], e)
            out["error"] = str(e)
        return out

    if action == "manage":
        pos = positions.get(slot_id)
        if pos and sig.get("stop") is not None:
            new_stop = float(sig["stop"])
            pos["mark"] = sig.get("mark")
            pos["donch_lo"] = sig.get("donch_lo")
            pos["last_manage_bar_ts"] = sig.get("bar_ts")
            pos["updated_at"] = now_iso_taipei()
            if live and new_stop > float(pos.get("stop") or 0):
                try:
                    replace_trail_stop(client, pos, new_stop, slot_id)
                except Exception as e:  # noqa: BLE001
                    log.error("trail_replace_fail slot=%s err=%s", slot_id, e)
                    pos["stop"] = new_stop  # still update state stop target
            else:
                pos["stop"] = new_stop
        return out

    return out


def apply_sol_entry(client: BinanceClient, state: dict, sol_sig: dict, live: bool, allow_entries: bool) -> dict:
    """Map SOL core would_order into apply_signal enter shape."""
    if not sol_sig.get("would_order"):
        return sol_sig
    if not allow_entries:
        sol_sig = dict(sol_sig)
        sol_sig["reason"] = "paused_no_new_entries"
        sol_sig["would_order"] = False
        return sol_sig
    plan = sol_sig.get("order_plan_if_enter_at_last_close") or {}
    atr = float((sol_sig.get("signal") or {}).get("atr14_sma") or plan.get("atr") or 0)
    close = float(sol_sig.get("sol_close") or plan.get("entry_px") or 0)
    stop_mult = float(plan.get("stop_atr_mult") or 2.0)
    stop = float(plan.get("stop_px") or (close - stop_mult * atr if close and atr else 0))
    enter_sig = {
        "slot": "core_sol",
        "symbol": "SOLUSDT",
        "action": "enter",
        "quote_usdt": min(float(plan.get("quote_usdt") or sol_sig.get("quote_usdt") or 1500), MAX_NOTIONAL_USDT),
        "bar_ts": sol_sig.get("bar") or (sol_sig.get("signal") or {}).get("bar_open_time_utc"),
        "close": close,
        "atr": atr,
        "suggested_stop": stop,
        "donch_lo": (sol_sig.get("signal") or {}).get("donch20_lo"),
        "client_order_id": client_order_id("core_sol", "enter", str(sol_sig.get("bar") or "")),
    }
    result = apply_signal(client, state, enter_sig, live=live, allow_entries=allow_entries)
    merged = dict(sol_sig)
    merged["executed"] = result.get("executed")
    merged["fill"] = result.get("fill")
    merged["error"] = result.get("error")
    merged["enter_result"] = {k: result.get(k) for k in ("executed", "fill", "error", "reason", "intended_order")}
    return merged


def cmd_run(client: BinanceClient, dry_run: bool) -> int:
    enabled = env_bool("TRADER_ENABLED", True)
    mode = "dry-run" if dry_run else trader_mode()
    live = (not dry_run) and mode == "live" and enabled

    log.info(
        "run_start dry_run=%s mode=%s enabled=%s live=%s bucket=%s",
        dry_run, mode, enabled, live, os.environ.get("GCS_BUCKET"),
    )
    if not enabled:
        log.warning("TRADER_ENABLED=false — kill switch; signals only")
        live = False

    store = StateStore()
    state = store.load()
    paused = bool(state.get("paused"))
    allow_entries = not paused
    if paused:
        log.info("paused=True — managing exits/stops only; no new entries")
    log.info("approved_ids=%s", sorted(_approved_ids(state)))
    log.info("signal_only_ids=%s", sorted(DEFAULT_SIGNAL_ONLY.keys()))

    # --- satellites (1h/4h Donchian + Wilder ATR) ---
    signals = evaluate_all(client, state)
    applied = [
        apply_signal(client, state, sig, live=live, allow_entries=allow_entries)
        for sig in signals
    ]

    # --- SOL core ---
    sol_meta = state.setdefault("slots", {}).setdefault("core_sol", {})
    try:
        sol_sig = sol_compute_signal()
        if sol_meta.get("needs_reset_latch"):
            if sol_sig["signal"]["regime_filtered"] == 0:
                sol_meta["needs_reset_latch"] = False
                sol_sig["signal"]["needs_reset_before_entry"] = False
                if sol_sig["status"] == "WAIT_RESET":
                    sol_sig["status"] = "ARMED"
                    sol_sig["conclusion"] = "wait_breakout"
                    sol_sig["would_order"] = False
            else:
                sol_sig["signal"]["needs_reset_before_entry"] = True
                sol_sig["status"] = "WAIT_RESET"
                sol_sig["would_order"] = False
                sol_sig["conclusion"] = "wait_signal_reset_then_breakout"
        elif sol_sig["signal"]["needs_reset_before_entry"]:
            sol_meta["needs_reset_latch"] = True
        sol_meta["last_signal"] = {
            "status": sol_sig["status"],
            "bar": sol_sig["bar"],
            "needs_reset": sol_sig["signal"]["needs_reset_before_entry"],
            "regime_on": sol_sig["btc_regime"]["regime_on"],
        }
        sol_sig["in_daily_window"] = in_daily_window()
        if live and sol_sig.get("would_order") and not sol_sig["in_daily_window"]:
            sol_sig["would_order"] = False
            sol_sig["action"] = "defer_until_daily_window"
            sol_sig["reason"] = "outside_utc_0005_0015"
        # Manage existing SOL position trail if any
        sol_pos = state.get("positions", {}).get("core_sol")
        if sol_pos and sol_sig.get("signal"):
            atr = float(sol_sig["signal"].get("atr14_sma") or 0)
            mark = float(sol_sig.get("sol_close") or 0)
            trail_mult = 2.0
            if atr and mark:
                new_stop = mark - trail_mult * atr
                manage_sig = {
                    "slot": "core_sol",
                    "symbol": "SOLUSDT",
                    "action": "manage",
                    "stop": new_stop,
                    "mark": mark,
                    "donch_lo": sol_sig["signal"].get("donch20_lo"),
                    "bar_ts": sol_sig.get("bar"),
                }
                apply_signal(client, state, manage_sig, live=live, allow_entries=False)
        sol_mode = approval_mode(state, strategy_id_for_slot("core_sol") or "")
        if sol_mode == "signal_only":
            # Compute + log only; never place Demo orders
            sol_sig["mode"] = "signal_only"
            sol_sig["label_zh"] = "只算訊號（未核准）"
            if sol_sig.get("would_order"):
                log.info("sol_signal_only_no_order would_order=True status=%s", sol_sig.get("status"))
            sol_sig["would_order"] = False
            sol_sig = apply_sol_entry(client, state, sol_sig, live=live, allow_entries=False)
            sol_sig["mode"] = "signal_only"
        else:
            sol_sig = apply_sol_entry(client, state, sol_sig, live=live, allow_entries=allow_entries)

        applied.append(sol_sig)
        hb = expectation_heartbeat(sol_sig)
        state.setdefault("expectation_log", [])
        state["expectation_log"].append(hb)
        state["expectation_log"] = state["expectation_log"][-500:]
    except Exception as e:  # noqa: BLE001
        log.exception("sol_core_fail err=%s", e)
        applied.append({"slot": "core_sol", "symbol": "SOLUSDT", "error": str(e)})

    state.setdefault("meta", {})["last_run_at"] = now_iso_taipei()
    state["meta"]["last_run_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["meta"]["last_mode"] = "dry-run" if (dry_run or not live) else "live"
    state["meta"]["last_live"] = live
    state["meta"]["last_ok"] = True
    state["meta"]["last_paused"] = paused
    # Use generation lock so API pause/close doesn't race
    try:
        store.save(state, if_generation_match=store._generation)
    except Exception as e:  # noqa: BLE001
        log.warning("state_save_gen_conflict retry_plain err=%s", e)
        store.save(state)

    feed = build_feed(
        client if client.api_key else None, state, applied, state["meta"]["last_mode"]
    )
    if state.get("expectation_log"):
        feed["sol_expectation_latest"] = state["expectation_log"][-1]
        feed["sol_core"] = next((x for x in applied if x.get("slot") == "core_sol"), None)
    url = store.save_feed(feed)
    try:
        store.save_feed(
            {"updated_at": now_iso_taipei(), "events": state.get("expectation_log", [])},
            object_name="trader/sol_expectation_log.json",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("expectation_log_save_skip err=%s", e)

    log.info("run_done signals=%s live=%s paused=%s feed=%s", len(applied), live, paused, url)
    print(
        json.dumps(
            {"ok": True, "live": live, "paused": paused, "signals": applied, "feed": url},
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0



def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Binance Demo Cloud Run trader")
    ap.add_argument(
        "mode",
        nargs="?",
        default=os.environ.get("JOB_MODE") or "run",
        choices=["probe", "run", "dry-run"],
    )
    args = ap.parse_args(argv)
    client = BinanceClient()
    try:
        if args.mode == "probe":
            return cmd_probe(client)
        if args.mode == "dry-run":
            return cmd_run(client, dry_run=True)
        return cmd_run(client, dry_run=(trader_mode() != "live"))
    except Exception as e:  # noqa: BLE001
        log.exception("fatal err=%s", e)
        try:
            store = StateStore()
            st = store.load()
            st.setdefault("meta", {})["last_ok"] = False
            st["meta"]["last_error"] = str(e)
            st["meta"]["last_run_at"] = now_iso_taipei()
            store.save(st)
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
