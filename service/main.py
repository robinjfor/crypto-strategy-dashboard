#!/usr/bin/env python3
"""Binance Demo trader — Cloud Run Job entrypoint.

Modes:
  probe    — ping / time / signed account (nonzero balances only; never print keys)
  dry-run  — compute signals & intended orders; place nothing
  run      — reconcile + place when TRADER_MODE=live AND TRADER_ENABLED=true

Env (match service/GCP_SETUP_zh.md):
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
from slots import MAX_NOTIONAL_USDT, SLOTS
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
        except Exception as e:  # noqa: BLE001
            log.warning("feed_account_skip err=%s", e)
    return {
        "updated_at": now_iso_taipei(),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine": "binance_demo_cloudrun",
        "mode": mode,
        "trader_enabled": env_bool("TRADER_ENABLED", True),
        "region_hint": "asia-east1",
        "balances": balances,
        "myTrades": trades,
        "slots": signals,
        "positions": state.get("positions", {}),
        "meta": state.get("meta", {}),
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


def apply_signal(client: BinanceClient, state: dict, sig: dict, live: bool) -> dict:
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
        quote = min(float(sig.get("quote_usdt") or 0), MAX_NOTIONAL_USDT)
        coid = sig.get("client_order_id") or f"{slot_id}-enter"
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
                "entry_bar_ts": sig["bar_ts"],
                "stop": stop,
                "client_order_id": coid,
                "order_id": order.get("orderId"),
                "filled_at": now_iso_taipei(),
            }
            meta["last_acted_bar_ts"] = sig["bar_ts"]
            out["executed"] = True
            out["fill"] = {"qty": qty, "price": px, "orderId": order.get("orderId")}
            log.info("filled_enter symbol=%s qty=%s px=%s", sig["symbol"], qty, px)
        except Exception as e:  # noqa: BLE001
            log.error("enter_fail symbol=%s err=%s", sig["symbol"], e)
            out["error"] = str(e)
        return out

    if action == "exit":
        pos = positions.get(slot_id) or {}
        qty = float(pos.get("qty") or 0)
        coid = f"{slot_id}-x-{str(sig.get('exit_bar_ts') or '')[:13]}".replace(":", "")[:36]
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
            order = client.market_sell(sig["symbol"], qty, coid)
            positions.pop(slot_id, None)
            meta["last_acted_bar_ts"] = sig.get("exit_bar_ts") or sig.get("bar_ts")
            meta["last_exit_bar_ts"] = sig.get("exit_bar_ts") or sig.get("bar_ts")
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
            pos["stop"] = sig["stop"]
            pos["mark"] = sig.get("mark")
            pos["last_manage_bar_ts"] = sig.get("bar_ts")
            pos["updated_at"] = now_iso_taipei()
        return out

    return out


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

    # --- satellites (1h/4h Donchian + Wilder ATR) ---
    signals = evaluate_all(client, state)
    applied = [apply_signal(client, state, sig, live=live) for sig in signals]

    # --- SOL core (1d Donchian + SMA ATR + BTC regime); separate module ---
    sol_meta = state.setdefault("slots", {}).setdefault("core_sol", {})
    # Persist needs_reset: once True while sig=1 mid-channel, clear only when sig returns to 0
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
        # Live SOL entries only in daily window (UTC 00:05-00:15); dry-run always evaluates
        if live and sol_sig["would_order"] and not sol_sig["in_daily_window"]:
            sol_sig["would_order"] = False
            sol_sig["action"] = "defer_until_daily_window"
            sol_sig["reason"] = "outside_utc_0005_0015"
        applied.append(sol_sig)
        # Expectation log heartbeat (append to state + feed)
        hb = expectation_heartbeat(sol_sig)
        state.setdefault("expectation_log", [])
        state["expectation_log"].append(hb)
        state["expectation_log"] = state["expectation_log"][-500:]
    except Exception as e:  # noqa: BLE001
        log.exception("sol_core_fail err=%s", e)
        applied.append({"slot": "core_sol", "symbol": "SOLUSDT", "error": str(e)})

    state.setdefault("meta", {})["last_run_at"] = now_iso_taipei()
    state["meta"]["last_mode"] = "dry-run" if (dry_run or not live) else "live"
    store.save(state)

    feed = build_feed(
        client if client.api_key else None, state, applied, state["meta"]["last_mode"]
    )
    # Attach latest SOL expectation heartbeat for dashboard
    if state.get("expectation_log"):
        feed["sol_expectation_latest"] = state["expectation_log"][-1]
        feed["sol_core"] = next((x for x in applied if x.get("slot") == "core_sol"), None)
    url = store.save_feed(feed)
    # Also persist expectation log object for 14-day review
    try:
        store.save_feed(
            {"updated_at": now_iso_taipei(), "events": state.get("expectation_log", [])},
            object_name="trader/sol_expectation_log.json",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("expectation_log_save_skip err=%s", e)

    log.info("run_done signals=%s feed=%s", len(applied), url)
    print(
        json.dumps(
            {"ok": True, "live": live, "signals": applied, "feed": url},
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
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
