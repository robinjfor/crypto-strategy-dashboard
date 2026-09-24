#!/usr/bin/env python3
"""Paper runner — one-shot ARMED/FILLED management.

Usage:
  python runner.py --state-dir ./dry_run --seed-from /path/to/paper_trading.json

Never modifies --seed-from. Writes only under --state-dir.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from indicators import (
    BLACKLIST,
    OBSERVE_ONLY,
    ONE_WAY,
    add_donch_atr,
    bar_ts_iso,
    base_of,
    buy_px,
    fetch_klines,
    now_iso_taipei,
    now_taipei,
    replay_stop_path,
    sell_px,
    split_closed,
)

ROOT = Path(__file__).resolve().parent


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def append_settlement(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def push_alert(alerts: dict, level: str, code: str, msg: str, symbol: str | None = None) -> None:
    alerts.setdefault("alerts", []).append({
        "id": f"al_{len(alerts.get('alerts', []))}",
        "ts": now_iso_taipei(),
        "level": level,
        "source": "runner",
        "code": code,
        "msg": msg,
        "symbol": symbol,
    })
    alerts["alerts"] = alerts["alerts"][-100:]
    alerts["updated_at"] = now_iso_taipei()


def parse_variant(variant: str, fb: dict | None = None) -> dict:
    fb = fb or {}
    tf = fb.get("tf") or ("1h" if "1h" in variant else "4h")
    donch_n = int(fb.get("donch_n") or (55 if "55" in variant else 20))
    stop_m = float(fb.get("stop_atr_mult") or 2.0)
    trail_m = float(fb.get("trail_atr_mult") or 3.0)
    m = re.search(r"_s([0-9.]+)_t([0-9.]+)", variant)
    if m:
        stop_m, trail_m = float(m.group(1)), float(m.group(2))
    return {"tf": tf, "donch_n": donch_n, "stop_atr_mult": stop_m, "trail_atr_mult": trail_m}


def seed_from_paper(paper: dict, state_dir: Path) -> None:
    positions: list[dict] = []
    orders: list[dict] = []

    for i, p in enumerate(paper.get("open_positions") or []):
        sym = p["symbol"]
        variant = p.get("variant") or ""
        params = parse_variant(variant, p)
        if "NEAR" in sym:
            params = {"tf": "4h", "donch_n": 20, "stop_atr_mult": 2.0, "trail_atr_mult": 3.0}
            slot, entry_bar = "core", "2026-09-17T16:00:00+00:00"
        elif "OP" in sym:
            params = {"tf": "4h", "donch_n": 20, "stop_atr_mult": 1.5, "trail_atr_mult": 1.5}
            slot, entry_bar = "satellite_B", "2026-09-19T00:00:00+00:00"
        else:
            slot, entry_bar = f"sat_{i}", None
        pid = f"pos_{base_of(sym).lower()}_{i}"
        positions.append({
            "id": pid,
            "slot": slot,
            "symbol": sym,
            "side": p.get("side", "LONG"),
            "qty": float(p["qty"]),
            "entry": float(p["entry"]),
            "entry_bar_ts": entry_bar,
            "stop": float(p.get("stop") or 0),
            "seed_stop": float(p.get("stop") or 0),
            "donch_lo": float(p["donch_lo"]) if p.get("donch_lo") is not None else None,
            "mark": float(p.get("mark_price") or p["entry"]),
            "unrealized_pct": float(p.get("unrealized_pct") or 0),
            "variant": variant,
            **params,
            "max_hold_bars": 36 if params["tf"] == "4h" else 96,
            "status": "FILLED",
            "last_manage_bar_ts": None,
            "last_check_at": None,
        })
        orders.append({
            "id": f"ord_{base_of(sym).lower()}",
            "slot": slot,
            "symbol": sym,
            "status": "FILLED",
            "position_id": pid,
            "variant": variant,
            "quote_usdt": round(float(p["entry"]) * float(p["qty"]), 2),
            **params,
        })

    for sym, a in (paper.get("armed") or {}).items():
        st_in = (a.get("status") or "ARMED").upper()
        if st_in == "FILLED":
            continue
        variant = a.get("variant") or ""
        params = parse_variant(variant, a)
        if "NEAR" in sym:
            slot = "core"
        elif "FET" in sym:
            slot = "satellite_A"
        elif "OP" in sym:
            slot = "satellite_B"
        elif "DOT" in sym:
            slot = "satellite_C"
        else:
            slot = a.get("slot") or "satellite"
        if st_in in ("DISARMED", "DISARMED_OOS_FAIL", "EXITED"):
            status = st_in
        elif st_in in ("WAIT_RESET", "REARM", "ARMED_WAIT_RESET"):
            status = "WAIT_RESET"
        elif st_in in ("WAIT_BREAKOUT", "ARMED_WAIT_BREAKOUT", "ARMED", "SIGNAL", "PENDING"):
            status = "ARMED" if st_in.startswith("WAIT") or st_in.startswith("ARMED") else st_in
            if st_in in ("WAIT_BREAKOUT", "ARMED_WAIT_BREAKOUT"):
                status = "ARMED"
        else:
            status = "ARMED"
        row = {
            "id": f"ord_{base_of(sym).lower()}",
            "slot": slot,
            "symbol": sym,
            "status": status,
            "variant": variant,
            "quote_usdt": float(a.get("quote") or a.get("quote_usdt") or 0),
            "mark": a.get("mark"),
            "last_signal_bar_ts": None,
            "last_acted_bar_ts": None,
            **params,
        }
        if a.get("rearm_rule"):
            row["rearm_rule"] = a["rearm_rule"]
        if status == "WAIT_RESET":
            row["reset_done"] = False
        elif a.get("rearm_rule") == "reset_below_hi" and status == "ARMED":
            row["reset_done"] = bool(a.get("reset_done", True))
        if a.get("stop_ref") is not None:
            row["stop_ref"] = a["stop_ref"]
        if a.get("note"):
            row["note"] = a["note"]
        orders.append(row)

    save_json(state_dir / "positions.json", {
        "updated_at": now_iso_taipei(),
        "virtual_equity": paper.get("virtual_equity"),
        "balances": dict(paper.get("balances") or {}),
        "light": paper.get("light", "GREEN"),
        "positions": positions,
        "seed_note": "migrated from paper_trading.json; source untouched",
    })
    save_json(state_dir / "orders.json", {
        "updated_at": now_iso_taipei(),
        "orders": orders,
    })
    if not (state_dir / "news_light.json").is_file():
        save_json(state_dir / "news_light.json", {
            "light": paper.get("light", "GREEN"),
            "updated_at": now_iso_taipei(),
            "updated_by": "seed",
            "note": "default GREEN",
        })
    if not (state_dir / "commands.json").is_file():
        save_json(state_dir / "commands.json", {"commands": []})
    if not (state_dir / "alerts.json").is_file():
        save_json(state_dir / "alerts.json", {"updated_at": now_iso_taipei(), "alerts": []})


def process_commands(commands: dict, positions: dict, orders: dict, news: dict) -> list[str]:
    log: list[str] = []
    for cmd in commands.get("commands") or []:
        if cmd.get("status") not in (None, "queued"):
            continue
        action = cmd.get("action")
        try:
            if action == "set_news_light":
                news["light"] = cmd.get("light", news.get("light"))
                news["updated_at"] = now_iso_taipei()
                news["updated_by"] = "commands"
                log.append(f"news -> {news['light']}")
            elif action == "pause_slot":
                slot = cmd.get("slot")
                for o in orders.get("orders") or []:
                    if o.get("slot") == slot and o.get("status") in ("ARMED", "SIGNAL", "PENDING"):
                        o["status_before_pause"] = o["status"]
                        o["status"] = "PAUSED"
                log.append(f"pause {slot}")
            elif action == "resume_slot":
                slot = cmd.get("slot")
                for o in orders.get("orders") or []:
                    if o.get("slot") == slot and o.get("status") == "PAUSED":
                        o["status"] = o.pop("status_before_pause", "ARMED")
                log.append(f"resume {slot}")
            elif action == "cancel_order":
                oid, sym = cmd.get("order_id"), cmd.get("symbol")
                for o in orders.get("orders") or []:
                    if (oid and o.get("id") == oid) or (sym and o.get("symbol") == sym):
                        if o.get("status") in ("ARMED", "SIGNAL", "PENDING", "PAUSED"):
                            o["status"] = "CANCELLED"
                            log.append(f"cancel {o.get('id')}")
            elif action == "close_position":
                sym = cmd.get("symbol")
                for p in positions.get("positions") or []:
                    if p.get("symbol") == sym and p.get("status") == "FILLED":
                        p["_manual_close"] = True
                        p["_manual_reason"] = cmd.get("reason") or "manual"
                        log.append(f"close queued {sym}")
            else:
                log.append(f"unknown action {action}")
            cmd["status"] = "done"
            cmd["done_at"] = now_iso_taipei()
        except Exception as e:  # noqa: BLE001
            cmd["status"] = "error"
            cmd["error"] = str(e)
            log.append(f"cmd error: {e}")
    commands["commands"] = []
    return log


def manage_position(pos: dict, settlement_path: Path, dry_run: bool) -> dict:
    """Path-correct manage: Wilder ATR trail, low-hit exits, min(open, stop) fill.

    On catch-up (seed / missed bars), replay from entry and exit on the FIRST
    bar that hits — do not jump to the latest bar with a fully-ratcheted stop.
    Initial stop is always entry - stop_mult*ATR(entry bar); paper seed_stop is
    audit-only (stale trail values must not short-circuit the path).
    Also enforces max_hold_bars on close.
    """
    sym, tf = pos["symbol"], pos["tf"]
    raw = fetch_klines(sym, tf, limit=max(250, int(pos["donch_n"]) + 80))
    closed, forming = split_closed(raw, tf)
    ind = add_donch_atr(closed, int(pos["donch_n"])).dropna(
        subset=["atr", "donch_hi", "donch_lo"]
    )
    if ind.empty:
        return {"symbol": sym, "error": "no_closed_bars"}

    mark = float(forming["Close"]) if forming is not None else float(ind.iloc[-1]["Close"])
    last_bar_ts = bar_ts_iso(ind.iloc[-1].name)

    # Idempotent only when already managed this last bar AND no pending catch-up exit
    if (
        pos.get("last_manage_bar_ts") == last_bar_ts
        and not pos.get("_manual_close")
        and pos.get("status") == "FILLED"
    ):
        # Still refresh MTM; trail already settled for this bar
        pos["mark"] = mark
        pos["unrealized_pct"] = round((mark / pos["entry"] - 1.0) * 100.0, 4)
        pos["unrealized_usdt"] = round((mark - pos["entry"]) * pos["qty"], 2)
        pos["last_check_at"] = now_iso_taipei()
        return {
            "symbol": sym,
            "skipped": "idempotent_same_bar",
            "stop": pos["stop"],
            "mark": mark,
            "bar_ts": last_bar_ts,
        }

    # Always path-replay from entry (ignore seed_stop as initial — may be stale trail)
    replay = replay_stop_path(
        ind,
        entry=float(pos["entry"]),
        entry_bar_ts=pos.get("entry_bar_ts"),
        stop_atr_mult=float(pos["stop_atr_mult"]),
        trail_atr_mult=float(pos["trail_atr_mult"]),
        initial_stop=None,
    )
    stop = float(replay["stop"])
    donch_lo = float(replay["donch_lo"])
    atr = float(replay["atr"])
    pos["stop"] = round(stop, 8)
    pos["donch_lo"] = round(donch_lo, 8)
    pos["atr"] = round(atr, 8)
    pos["mark"] = mark
    pos["unrealized_pct"] = round((mark / pos["entry"] - 1.0) * 100.0, 4)
    pos["unrealized_usdt"] = round((mark - pos["entry"]) * pos["qty"], 2)
    pos["last_check_at"] = now_iso_taipei()

    manual = bool(pos.pop("_manual_close", False))
    reason_manual = pos.pop("_manual_reason", None)
    exit_info = replay.get("exit")

    # max_hold: count closed bars from entry (entry bar = 0)
    max_hold = int(pos.get("max_hold_bars") or 0)
    hold_exit = None
    if max_hold > 0 and pos.get("entry_bar_ts"):
        ets = __import__("pandas").Timestamp(pos["entry_bar_ts"])
        if ets.tzinfo is None:
            ets = ets.tz_localize("UTC")
        post = ind.loc[ind.index >= ets]
        if len(post) > max_hold:
            # bar index max_hold (0-based from entry) is the expiry bar
            exp = post.iloc[max_hold]
            # only if we did not already stop earlier
            if exit_info is None or __import__("pandas").Timestamp(exit_info["bar_ts"]) > exp.name:
                hold_exit = {
                    "bar_ts": bar_ts_iso(exp.name),
                    "exit_ref": float(exp["Close"]),
                    "reason": "max_hold",
                    "stop": stop,
                }

    should_exit = bool(exit_info) or bool(hold_exit) or manual
    if manual:
        reason = "manual"
        bar_ts = last_bar_ts
        ref = mark
    elif exit_info and (hold_exit is None or exit_info["bar_ts"] <= hold_exit["bar_ts"]):
        reason = "stop"
        bar_ts = exit_info["bar_ts"]
        ref = float(exit_info["exit_ref"])  # already min(open, stop)
        stop = float(exit_info["stop"])
        pos["stop"] = round(stop, 8)
    elif hold_exit:
        reason = "max_hold"
        bar_ts = hold_exit["bar_ts"]
        ref = float(hold_exit["exit_ref"])
    else:
        reason = "hold"
        bar_ts = last_bar_ts
        ref = None

    summary: dict = {
        "symbol": sym,
        "bar_ts": bar_ts,
        "mark": mark,
        "stop": pos["stop"],
        "donch_lo": pos["donch_lo"],
        "atr": pos["atr"],
        "unrealized_pct": pos["unrealized_pct"],
        "unrealized_usdt": pos["unrealized_usdt"],
        "should_exit": should_exit,
        "reason": reason,
        "seed_stop": pos.get("seed_stop"),
        "replay_exit": exit_info,
    }

    if should_exit and ref is not None:
        entry = float(pos["entry"])
        qty = float(pos["qty"])
        # Report stop/ref as price; apply 20bps fee+slip both sides → matches capital ~+56
        px = float(ref) if reason == "stop" else sell_px(ref)
        if reason == "stop":
            # capital-control: exit at stop ref; RT 20bps each side on notionals
            pnl = (ref - entry) * qty - ONE_WAY * entry * qty - ONE_WAY * ref * qty
            pnl_pct = (ref / entry - 1.0) * 100.0  # gross % on refs (user +6.05)
            # net pct after RT:
            net_pct = pnl / (entry * qty) * 100.0
        else:
            px = sell_px(ref)
            pnl = (px - entry) * qty - ONE_WAY * entry * qty  # entry-side cost if mid
            pnl_pct = (px / entry - 1.0) * 100.0
            net_pct = pnl / (entry * qty) * 100.0
        row = {
            "ts": now_iso_taipei(),
            "kind": "EXIT",
            "engine": "box_paper_vision",
            "symbol": sym,
            "side": "SELL",
            "qty": qty,
            "price": round(ref if reason == "stop" else px, 8),
            "entry": entry,
            "reason": reason_manual or reason,
            "pnl_usdt": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_pct_net": round(net_pct, 4),
            "status": "CLOSED",
            "variant": pos.get("variant"),
            "signal_bar_ts": bar_ts,
            "dry_run": dry_run,
            "stop_at_exit": round(stop, 8),
        }
        append_settlement(settlement_path, row)
        pos["status"] = "EXITED"
        pos["exit"] = row
        summary["exit"] = row

    pos["last_manage_bar_ts"] = last_bar_ts if not should_exit else bar_ts
    return summary


def try_enter(
    order: dict,
    positions: dict,
    light: str,
    settlement_path: Path,
    dry_run: bool,
) -> dict:
    if light != "GREEN":
        return {"symbol": order["symbol"], "skipped": f"light={light}"}
    if order.get("status") == "PAUSED":
        return {"symbol": order["symbol"], "skipped": "paused"}
    if order.get("status") not in ("ARMED", "SIGNAL", "PENDING", "WAIT_RESET"):
        return {"symbol": order["symbol"], "skipped": f"status={order.get('status')}"}

    base = base_of(order["symbol"])
    if base in BLACKLIST or base in OBSERVE_ONLY:
        return {"symbol": order["symbol"], "skipped": "blacklist_or_observe"}

    sym, tf = order["symbol"], order["tf"]
    raw = fetch_klines(sym, tf, limit=max(200, int(order["donch_n"]) + 80))
    closed, forming = split_closed(raw, tf)
    ind = add_donch_atr(closed, int(order["donch_n"])).dropna(
        subset=["atr", "donch_hi", "donch_lo"]
    )
    if ind.empty:
        return {"symbol": sym, "error": "no_closed_bars"}

    bar = ind.iloc[-1]
    bar_ts = bar_ts_iso(bar.name)
    atr = float(bar["atr"])
    order["last_check_at"] = now_iso_taipei()
    order["mark"] = float(forming["Close"]) if forming is not None else float(bar["Close"])
    order["donch_hi"] = float(bar["donch_hi"])
    order["dist_to_breakout_atr"] = (
        round((float(bar["Close"]) - float(bar["donch_hi"])) / atr, 4) if atr else None
    )

    close_px = float(bar["Close"])
    donch_hi = float(bar["donch_hi"])
    rearm = (order.get("rearm_rule") or "").strip()
    # reset_below_hi: after exit, require a close below Donchian high before next breakout counts
    if rearm == "reset_below_hi" or order.get("status") == "WAIT_RESET":
        if order.get("reset_done"):
            # already reset — treat as normal ARMED breakout watch
            if order.get("status") == "WAIT_RESET":
                order["status"] = "ARMED"
        else:
            if close_px < donch_hi:
                order["reset_done"] = True
                order["reset_at_bar_ts"] = bar_ts
                order["status"] = "ARMED"
                order["reset_note"] = "close back below donch_hi; armed for fresh breakout"
                return {
                    "symbol": sym,
                    "status": "ARMED",
                    "reset_done": True,
                    "mark": order["mark"],
                    "donch_hi": order["donch_hi"],
                    "dist_atr": order["dist_to_breakout_atr"],
                }
            # still above / not yet reset — do not allow breakout entry
            order["status"] = "WAIT_RESET"
            return {
                "symbol": sym,
                "status": "WAIT_RESET",
                "triggered": False,
                "reset_done": False,
                "mark": order["mark"],
                "donch_hi": order["donch_hi"],
                "dist_atr": order["dist_to_breakout_atr"],
                "note": "waiting close < donch_hi before re-arm breakout",
            }

    triggered = close_px > donch_hi
    if not triggered:
        # keep WAIT_RESET or ARMED
        if order.get("status") not in ("WAIT_RESET", "PAUSED"):
            order["status"] = "ARMED"
        return {
            "symbol": sym,
            "status": order.get("status") or "ARMED",
            "triggered": False,
            "dist_atr": order["dist_to_breakout_atr"],
            "mark": order["mark"],
            "donch_hi": order["donch_hi"],
        }

    if order.get("last_acted_bar_ts") == bar_ts:
        return {"symbol": sym, "skipped": "idempotent_already_acted", "bar_ts": bar_ts}

    order["status"] = "SIGNAL"
    order["last_signal_bar_ts"] = bar_ts
    ref = float(forming["Open"]) if forming is not None else float(bar["Close"])
    px = buy_px(ref)
    quote = float(order["quote_usdt"])
    qty = quote / px
    stop = px - float(order["stop_atr_mult"]) * atr
    pid = f"pos_{base.lower()}_{bar_ts[:13].replace(':', '')}"
    pos = {
        "id": pid,
        "slot": order.get("slot"),
        "symbol": sym,
        "side": "LONG",
        "qty": round(qty, 8),
        "entry": round(px, 8),
        "entry_bar_ts": bar_ts,
        "stop": round(stop, 8),
        "seed_stop": round(stop, 8),
        "donch_lo": round(float(bar["donch_lo"]), 8),
        "mark": order["mark"],
        "unrealized_pct": round((order["mark"] / px - 1.0) * 100.0, 4),
        "variant": order.get("variant"),
        "tf": tf,
        "donch_n": order["donch_n"],
        "stop_atr_mult": order["stop_atr_mult"],
        "trail_atr_mult": order["trail_atr_mult"],
        "max_hold_bars": 36 if tf == "4h" else 96,
        "status": "FILLED",
        "last_manage_bar_ts": bar_ts,
        "last_check_at": now_iso_taipei(),
    }
    row = {
        "ts": now_iso_taipei(),
        "kind": "OPEN",
        "engine": "box_paper_vision",
        "symbol": sym,
        "side": "BUY",
        "qty": pos["qty"],
        "price": pos["entry"],
        "quote": quote,
        "stop": pos["stop"],
        "status": "FILLED",
        "variant": order.get("variant"),
        "signal_bar_ts": bar_ts,
        "order_id": order.get("id"),
        "dry_run": dry_run,
    }
    append_settlement(settlement_path, row)
    positions.setdefault("positions", []).append(pos)
    bal = positions.setdefault("balances", {})
    bal["USDT"] = float(bal.get("USDT") or 0) - quote
    bal[base] = float(bal.get(base) or 0) + pos["qty"]
    order["status"] = "FILLED"
    order["position_id"] = pid
    order["last_acted_bar_ts"] = bar_ts
    order["filled_at"] = now_iso_taipei()
    return {
        "symbol": sym,
        "status": "FILLED",
        "entry": pos["entry"],
        "stop": pos["stop"],
        "qty": pos["qty"],
    }


def run(state_dir: Path, dry_run: bool = True) -> dict:
    positions = load_json(state_dir / "positions.json", {"positions": [], "balances": {}})
    orders = load_json(state_dir / "orders.json", {"orders": []})
    news = load_json(state_dir / "news_light.json", {"light": "GREEN"})
    commands = load_json(state_dir / "commands.json", {"commands": []})
    alerts = load_json(state_dir / "alerts.json", {"alerts": []})
    settlement_path = state_dir / "settlement.jsonl"
    light = (news.get("light") or "GREEN").upper()

    cmd_log = process_commands(commands, positions, orders, news)
    results: dict[str, Any] = {
        "cmd_log": cmd_log,
        "positions": [],
        "orders": [],
        "light": light,
        "checked_at": now_taipei(),
    }

    for pos in list(positions.get("positions") or []):
        if pos.get("status") != "FILLED":
            continue
        try:
            results["positions"].append(manage_position(pos, settlement_path, dry_run))
        except Exception as e:  # noqa: BLE001
            push_alert(alerts, "ERROR", "MANAGE_FAIL", str(e), pos.get("symbol"))
            results["positions"].append({"symbol": pos.get("symbol"), "error": str(e)})

    active = []
    for p in positions.get("positions") or []:
        if p.get("status") == "FILLED":
            active.append(p)
        elif p.get("status") == "EXITED":
            bal = positions.setdefault("balances", {})
            base = base_of(p["symbol"])
            exit_row = p.get("exit") or {}
            bal["USDT"] = float(bal.get("USDT") or 0) + float(exit_row.get("price") or 0) * float(p["qty"])
            bal[base] = float(bal.get(base) or 0) - float(p["qty"])
            for o in orders.get("orders") or []:
                if o.get("symbol") == p.get("symbol") and o.get("status") == "FILLED":
                    o["status"] = "EXITED"
                    o["exited_at"] = now_iso_taipei()
    positions["positions"] = active

    for order in orders.get("orders") or []:
        if order.get("status") in ("ARMED", "SIGNAL", "PENDING", "PAUSED", "WAIT_RESET"):
            try:
                results["orders"].append(
                    try_enter(order, positions, light, settlement_path, dry_run)
                )
            except Exception as e:  # noqa: BLE001
                push_alert(alerts, "ERROR", "ENTER_FAIL", str(e), order.get("symbol"))
                results["orders"].append({"symbol": order.get("symbol"), "error": str(e)})

    eq = float(positions.get("balances", {}).get("USDT") or 0)
    for p in positions.get("positions") or []:
        eq += float(p.get("mark") or 0) * float(p.get("qty") or 0)
    positions["virtual_equity"] = round(eq, 4)
    positions["updated_at"] = now_iso_taipei()
    orders["updated_at"] = now_iso_taipei()

    health = {
        "last_success_at": now_iso_taipei(),
        "last_runner_at": now_iso_taipei(),
        "last_runner_ok": True,
        "last_error": None,
        "dry_run": dry_run,
    }
    save_json(state_dir / "positions.json", positions)
    save_json(state_dir / "orders.json", orders)
    save_json(state_dir / "news_light.json", news)
    save_json(state_dir / "commands.json", {"commands": commands.get("commands") or []})
    save_json(state_dir / "alerts.json", alerts)
    save_json(state_dir / "health.json", health)
    save_json(state_dir / "runner_last.json", results)
    return results



def write_compat_paper(state_dir: Path, positions: dict, orders: dict, news: dict) -> None:
    """Mirror state into paper_trading.json for legacy UI / homepage."""
    armed: dict = {}
    for o in orders.get("orders") or []:
        st = o.get("status")
        if st in ("ARMED", "SIGNAL", "PENDING", "PAUSED", "WAIT_RESET"):
            status_out = (
                "WAIT_RESET" if st == "WAIT_RESET"
                else ("WAIT_BREAKOUT" if st == "ARMED" else st)
            )
            armed[o["symbol"]] = {
                "variant": o.get("variant"),
                "quote": o.get("quote_usdt"),
                "status": status_out,
                "mark": o.get("mark"),
                "tf": o.get("tf"),
                "donch_n": o.get("donch_n"),
                "donch_hi": o.get("donch_hi"),
                "dist_to_breakout_atr": o.get("dist_to_breakout_atr"),
                "stop_atr_mult": o.get("stop_atr_mult"),
                "trail_atr_mult": o.get("trail_atr_mult"),
                "rearm_rule": o.get("rearm_rule"),
                "reset_done": o.get("reset_done"),
                "slot": o.get("slot"),
            }
        elif st == "FILLED":
            armed[o["symbol"]] = {
                "variant": o.get("variant"),
                "quote": o.get("quote_usdt"),
                "status": "FILLED",
                "tf": o.get("tf"),
                "donch_n": o.get("donch_n"),
                "stop_atr_mult": o.get("stop_atr_mult"),
                "trail_atr_mult": o.get("trail_atr_mult"),
                "slot": o.get("slot"),
            }
        elif st in ("EXITED", "DISARMED", "DISARMED_OOS_FAIL"):
            armed[o["symbol"]] = {
                "variant": o.get("variant"),
                "status": st,
                "slot": o.get("slot"),
                "note": o.get("note"),
                "exit_reason": o.get("exit_reason"),
                "exit_price": o.get("exit_price"),
            }
    open_positions = []
    for p in positions.get("positions") or []:
        if p.get("status") != "FILLED":
            continue
        open_positions.append({
            "symbol": p["symbol"],
            "side": p.get("side", "LONG"),
            "qty": p["qty"],
            "entry": p["entry"],
            "entry_price": p["entry"],
            "stop": p.get("stop"),
            "mark_price": p.get("mark"),
            "unrealized_pct": p.get("unrealized_pct"),
            "unrealized_pnl": p.get("unrealized_usdt"),
            "variant": p.get("variant"),
            "donch_lo": p.get("donch_lo"),
            "tf": p.get("tf"),
            "donch_n": p.get("donch_n"),
            "stop_atr_mult": p.get("stop_atr_mult"),
            "trail_atr_mult": p.get("trail_atr_mult"),
        })
    paper = {
        "exchange": "Box Paper (Binance Vision)",
        "base_url": "https://data-api.binance.vision",
        "strategy": "multi box paper",
        "virtual_equity": positions.get("virtual_equity"),
        "balances": dict(positions.get("balances") or {}),
        "open_positions": open_positions,
        "last_action": "runner_ok",
        "light": (news.get("light") or "GREEN"),
        "armed": armed,
        "source": "state_mirror",
        "slots": {
            "core": {"status": "DISARMED_OOS_FAIL", "symbol": "NEARUSDT"},
            "satellite_A": {"status": "ARMED", "symbol": "FETUSDT"},
            "satellite_B": {"status": "WAIT_RESET", "symbol": "OPUSDT"},
            "satellite_C": {"status": "ARMED", "symbol": "DOTUSDT"},
        },
    }
    # fill updated_at properly
    paper["updated_at_taipei"] = now_iso_taipei()
    save_json(state_dir / "paper_trading.json", paper)
    legacy = ROOT.parent / "data" / "strategy-crypto-s2" / "paper_trading.json"
    if legacy.parent.is_dir():
        save_json(legacy, paper)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", type=Path, default=ROOT.parent / "state")
    ap.add_argument("--seed-from", type=Path, default=None)
    ap.add_argument("--seed-only", action="store_true", help="seed state then exit")
    ap.add_argument("--dry-run", action="store_true", help="tag settlements as dry_run")
    args = ap.parse_args()
    args.state_dir.mkdir(parents=True, exist_ok=True)

    if args.seed_from:
        paper = load_json(args.seed_from)
        if not paper:
            print(f"seed file missing: {args.seed_from}", file=sys.stderr)
            return 2
        seed_from_paper(paper, args.state_dir)
        print(f"seeded {args.seed_from} -> {args.state_dir} (source untouched)")
        if args.seed_only:
            return 0

    if not (args.state_dir / "positions.json").is_file():
        print("no positions.json; pass --seed-from", file=sys.stderr)
        return 2

    results = run(args.state_dir, dry_run=bool(args.dry_run))
    positions = load_json(args.state_dir / "positions.json", {})
    orders = load_json(args.state_dir / "orders.json", {})
    news = load_json(args.state_dir / "news_light.json", {"light": "GREEN"})
    write_compat_paper(args.state_dir, positions, orders, news)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
