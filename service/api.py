#!/usr/bin/env python3
"""Cloud Run HTTP API: public GET /status + PIN-gated control writes."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request

from binance_client import BinanceClient
from execution import market_close_slot
from slots import SLOTS, SOL_SLOT, DEFAULT_APPROVED, MAX_NOTIONAL_USDT, slot_by_strategy_id, strategy_id_for_slot, SUPPORTED_FAMILIES, approval_mode
from state_store import StateStore
from strategy import now_iso_taipei



STATUS_ZH = {
    "waiting_breakout": "等訊號",
    "WAIT_BREAKOUT": "等訊號",
    "wait_breakout": "等訊號",
    "armed": "已武裝",
    "ARMED": "已武裝",
    "WAIT_RESET": "等回落重置（需先收回上軌下方）",
    "wait_reset": "等回落重置（需先收回上軌下方）",
    "wait_signal_reset_then_breakout": "等回落重置（需先收回上軌下方）",
    "WAIT_SIGNAL_RESET_THEN_BREAKOUT": "等回落重置（需先收回上軌下方）",
    "PENDING_FILL": "已掛單，等成交",
    "pending_fill": "已掛單，等成交",
}


def status_zh(code) -> str:
    """Map slot/strategy status codes to Traditional Chinese labels."""
    if code is None or code == "":
        return "—"
    s = str(code).strip()
    if s in STATUS_ZH:
        return STATUS_ZH[s]
    low = s.lower()
    for k, v in STATUS_ZH.items():
        if k.lower() == low:
            return v
    # collapse separators
    compact = low.replace("-", "_")
    for k, v in STATUS_ZH.items():
        if k.lower().replace("-", "_") == compact:
            return v
    return f"{s}（未對照）"



logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("trader.api")

app = Flask(__name__)

CORS_ORIGIN = os.environ.get("CORS_ORIGIN") or "https://robinjfor.github.io"
PIN_SALT = os.environ.get("CONTROL_PIN_SALT") or "crypto-trader-v1"
PIN_HASH = (os.environ.get("CONTROL_PIN_HASH") or "").strip().lower()
PIN_HEADER = "X-Trader-Pin"

_fail_buckets: dict[str, list[float]] = {}
RATE_MAX = 5
RATE_WINDOW = 600.0
_bal_cache: dict[str, Any] = {"ts": 0.0, "data": None}
BAL_TTL = 15.0


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = f"Content-Type, {PIN_HEADER}"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


@app.after_request
def after(resp):
    return _cors(resp)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "crypto-trader-api", "ok": True})


@app.route("/status", methods=["OPTIONS"])
@app.route("/control/pause", methods=["OPTIONS"])
@app.route("/control/resume", methods=["OPTIONS"])
@app.route("/control/close", methods=["OPTIONS"])
@app.route("/control/approve", methods=["OPTIONS"])
@app.route("/control/revoke", methods=["OPTIONS"])
@app.route("/approved", methods=["OPTIONS"])
def options_ok():
    return _cors(app.make_response(("", 204)))


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For") or ""
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    bucket = [t for t in _fail_buckets.get(ip, []) if now - t < RATE_WINDOW]
    _fail_buckets[ip] = bucket
    return len(bucket) >= RATE_MAX


def _record_fail(ip: str) -> None:
    _fail_buckets.setdefault(ip, []).append(time.time())


def _check_pin() -> tuple[bool, str, int]:
    if not PIN_HASH:
        return False, "伺服器未設定控制 PIN 雜湊", 503
    ip = _client_ip()
    if _rate_limited(ip):
        return False, "嘗試次數過多，請稍後再試", 429
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "").lower()
    if proto and proto != "https" and os.environ.get("ALLOW_HTTP_PIN") != "1":
        return False, "僅接受 HTTPS", 403
    pin = request.headers.get(PIN_HEADER) or ""
    if not pin:
        _record_fail(ip)
        return False, "缺少操作密碼", 401
    digest = hashlib.sha256((PIN_SALT + pin).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(digest, PIN_HASH):
        _record_fail(ip)
        return False, "操作密碼錯誤", 401
    return True, "", 200


def _balances(client: BinanceClient) -> list:
    now = time.time()
    if _bal_cache["data"] is not None and now - float(_bal_cache["ts"]) < BAL_TTL:
        return _bal_cache["data"]
    try:
        data = client.nonzero_balances()
        _bal_cache["ts"] = now
        _bal_cache["data"] = data
        return data
    except Exception as e:  # noqa: BLE001
        log.warning("balances_fail err=%s", e)
        return _bal_cache["data"] or []


def _health(meta: dict, paused: bool) -> dict:
    last_ok = meta.get("last_ok")
    last_utc = meta.get("last_run_at_utc") or ""
    age_min = None
    stale = False
    if last_utc:
        try:
            ts = datetime.fromisoformat(last_utc.replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds() / 60.0
            stale = age_min > 90
        except Exception:  # noqa: BLE001
            stale = True
    else:
        stale = True
    if stale or last_ok is False:
        color = "red"
        label = "警告：超過 90 分鐘無成功執行" if stale else "警告：上次執行失敗"
    elif paused:
        color = "yellow"
        label = "已暫停（不開新倉）"
    else:
        color = "green"
        label = "自動交易中"
    return {
        "color": color,
        "label": label,
        "stale": stale,
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "paused": paused,
    }


def _open_positions(client: BinanceClient, state: dict) -> list:
    out = []
    for slot_id, pos in (state.get("positions") or {}).items():
        if not pos or float(pos.get("qty") or 0) <= 0:
            continue
        symbol = pos.get("symbol") or ""
        entry = float(pos.get("entry") or 0)
        qty = float(pos.get("qty") or 0)
        mark = float(pos.get("mark") or 0)
        try:
            mark = client.ticker_price(symbol)
        except Exception:  # noqa: BLE001
            pass
        upnl = (mark - entry) * qty if entry and mark else None
        out.append(
            {
                "slot": slot_id,
                "symbol": symbol,
                "qty": qty,
                "entry": entry,
                "mark": mark,
                "mark_price": mark,
                "stop": pos.get("stop"),
                "donch_lo": pos.get("donch_lo"),
                "unrealized_pnl": round(upnl, 4) if upnl is not None else None,
                "status": pos.get("status") or "FILLED",
                "filled_at": pos.get("filled_at"),
                "tf": "1d" if slot_id == "core_sol" else None,
            }
        )
    return out


def _slot_status(state: dict, feed_slots: list | None) -> list:
    by_id = {s.get("slot") or s.get("id"): s for s in (feed_slots or []) if isinstance(s, dict)}
    rows = []
    for slot in SLOTS:
        sid = slot["id"]
        sig = by_id.get(sid) or {}
        meta = (state.get("slots") or {}).get(sid) or {}
        trigger = sig.get("donch_hi") or sig.get("trigger")
        mark = sig.get("mark") or sig.get("close")
        dist = None
        try:
            if trigger is not None and mark is not None:
                dist = float(trigger) - float(mark)
        except Exception:  # noqa: BLE001
            pass
        rows.append(
            {
                "slot": sid,
                "symbol": slot["symbol"],
                "tf": slot["tf"],
                "armed": slot.get("armed", True),
                "variant": slot.get("variant"),
                "quote_usdt": slot.get("quote_usdt"),
                "target_pct": slot.get("target_pct") or slot.get("target_pct"),
                "require_reset_below_hi": slot.get("require_reset_below_hi"),
                "action": sig.get("action"),
                "reason": sig.get("reason"),
                "status": sig.get("status") or (meta.get("last_signal") or {}).get("status"),
                "entry_condition": (
                    f"Donchian{slot['donch_n']} 突破上軌"
                    + ("（需先回落重置）" if slot.get("require_reset_below_hi") else "")
                ),
                "trigger": trigger,
                "mark": mark,
                "distance": dist,
                "suggested_stop": sig.get("suggested_stop") or sig.get("stop"),
                "status_zh": status_zh(sig.get("status") or sig.get("reason") or sig.get("action") or (meta.get("last_signal") or {}).get("status")),
            }
        )
    sol = by_id.get("core_sol") or {}
    sol_meta = (state.get("slots") or {}).get("core_sol") or {}
    sol_sig = sol.get("signal") or {}
    # Prefer live feed fields; fall back to expectation log / compute_signal
    latest_exp = None
    for key in ("expectation_log", "sol_expectation_log"):
        evs = state.get(key) or []
        if evs:
            latest_exp = evs[-1]
            break
    trigger = (
        sol_sig.get("donch20_hi")
        or sol.get("donch20_hi")
        or (sol.get("order_plan_if_enter_at_last_close") or {}).get("donch_hi")
        or (latest_exp or {}).get("donch20_hi")
        or (latest_exp or {}).get("donch20_hi")
    )
    mark = (
        sol.get("sol_close")
        or sol_sig.get("sol_close")
        or (sol_sig.get("sol") or {}).get("close")
        or (latest_exp or {}).get("sol_close")
    )
    if mark is None or trigger is None:
        try:
            from sol_core.signal import compute_signal as sol_compute_signal
            fresh = sol_compute_signal()
            mark = mark if mark is not None else fresh.get("sol_close") or (fresh.get("sol") or {}).get("close")
            trigger = trigger if trigger is not None else (
                (fresh.get("signal") or {}).get("donch20_hi")
                or fresh.get("donch20_hi")
            )
            if not sol.get("status"):
                sol = {**sol, **{k: fresh.get(k) for k in ("status", "action", "reason", "conclusion", "in_daily_window") if fresh.get(k) is not None}}
                sol_sig = fresh.get("signal") or sol_sig
        except Exception as e:  # noqa: BLE001
            log.warning("sol_fresh_skip err=%s", e)
    if mark is None:
        try:
            mark = BinanceClient().ticker_price("SOLUSDT")
        except Exception:  # noqa: BLE001
            pass
    dist = None
    try:
        if trigger is not None and mark is not None:
            dist = float(trigger) - float(mark)
    except Exception:  # noqa: BLE001
        pass
    status = sol.get("status") or (sol_meta.get("last_signal") or {}).get("status")
    reason = sol.get("reason") or sol.get("conclusion") or (sol_meta.get("last_signal") or {}).get("reason")
    rows.append(
        {
            "slot": "core_sol",
            "symbol": "SOLUSDT",
            "mode": approval_mode(state, SOL_SLOT.get("strategy_id") or ""),
            "label_zh": "訊號監看（未核准下單）" if approval_mode(state, SOL_SLOT.get("strategy_id") or "") == "signal_only" else "已核准",
            "tf": "1d",
            "armed": True,
            "variant": SOL_SLOT.get("variant") or "donchian20_atr_btcRegime",
            "quote_usdt": SOL_SLOT.get("quote_usdt") or 1500,
            "target_pct": SOL_SLOT.get("target_pct") or SOL_SLOT.get("target_pct") or 30.0,
            "require_reset_below_hi": True,
            "action": sol.get("action"),
            "reason": reason,
            "status": status,
            "status_zh": status_zh(status or reason),
            "entry_condition": "BTC>SMA200 且 Donchian20 上升沿 0→1（需先重置）· UTC 00:05–00:15",
            "trigger": trigger,
            "mark": mark,
            "distance": dist,
            "needs_reset": sol_sig.get("needs_reset_before_entry")
            or (latest_exp or {}).get("needs_reset_before_entry")
            or (sol_meta.get("last_signal") or {}).get("needs_reset"),
            "in_daily_window": sol.get("in_daily_window"),
        }
    )
    return rows


def _planned_from_armed(armed: list) -> list:
    out = []
    for a in armed:
        status = str(a.get("status") or a.get("action") or "ARMED").upper()
        if "DISARM" in status:
            continue
        asset = (a.get("symbol") or "").replace("USDT", "")
        out.append(
            {
                "asset": asset,
                "symbol": a.get("symbol"),
                "slot": a.get("slot"),
                "strategy_name": a.get("variant"),
                "tf": a.get("tf"),
                "donch_n": 55 if "fet" in str(a.get("slot")) else 20,
                "target_notional_usdt": a.get("quote_usdt"),
                "ui_status": status,
                "status_label": status_zh(a.get("status") or a.get("reason") or status),
                "status_code": a.get("status") or a.get("reason") or status,
                "target_pct": a.get("target_pct"),
                "entry_rule": a.get("entry_condition"),
                "mark": a.get("mark"),
                "trigger": a.get("trigger"),
                "stop_note": f"建議止損 {a.get('suggested_stop')}" if a.get("suggested_stop") else "—",
                "stop_mode": "pending",
            }
        )
    return out




def _ensure_approved(state: dict) -> dict:
    """Seed approved map; always enforce SOL signal_only until 資金控管 lifts it."""
    approved = state.get("approved")
    if not isinstance(approved, dict) or not approved:
        approved = {}
        for sid, meta in DEFAULT_APPROVED.items():
            approved[sid] = {**meta, "approved_at": now_iso_taipei()}
        state["approved"] = approved
        state["_seeded_approved_dirty"] = True
    # Enforce SOL signal_only on every load (idempotent migration)
    sol_id = SOL_SLOT.get("strategy_id") or "donchian20_atr_btcRegime__SOL__1d"
    seed = DEFAULT_APPROVED.get(sol_id) or {}
    cur = approved.get(sol_id) or {}
    if cur.get("mode") != "signal_only" or not cur:
        approved[sol_id] = {
            **seed,
            **cur,
            "mode": "signal_only",
            "label_zh": "訊號監看（未核准下單）",
            "slot": "core_sol",
            "notional_usdt": cur.get("notional_usdt") or seed.get("notional_usdt") or 1500,
            "approved_at": cur.get("approved_at") or now_iso_taipei(),
        }
        state["approved"] = approved
        state["_seeded_approved_dirty"] = True
    # Ensure FET gate_fail flags present (still live)
    fet_id = next((k for k in DEFAULT_APPROVED if "FET" in k), "donchian55_s2.0_t3.0__FET__1h")
    if fet_id in approved:
        fet_seed = DEFAULT_APPROVED.get(fet_id) or {}
        if approved[fet_id].get("gate_pass") is None:
            approved[fet_id] = {**fet_seed, **approved[fet_id], "mode": approved[fet_id].get("mode") or "live"}
            state["approved"] = approved
            state["_seeded_approved_dirty"] = True
    return state["approved"]


def _approved_public(state: dict | None = None) -> dict:
    store = StateStore()
    st = state if state is not None else store.load()
    approved = _ensure_approved(st)
    # Persist seed if we just created it
    if state is None and st.get("_seeded_approved_dirty"):
        pass
    # Save seed if newly created (generation-safe mutate)
    if not state:
        def mut(s):
            _ensure_approved(s)
            return s
        try:
            # Only write if missing
            cur = store.load()
            if not isinstance(cur.get("approved"), dict) or not cur.get("approved"):
                store.mutate(mut)
                st = store.load()
                approved = st.get("approved") or approved
        except Exception as e:  # noqa: BLE001
            log.warning("approved_seed_save_skip err=%s", e)
    out = []
    for sid, meta in (approved or {}).items():
        slot = slot_by_strategy_id(sid) or {}
        out.append({
            "strategy_id": sid,
            "slot": meta.get("slot") or slot.get("id"),
            "symbol": slot.get("symbol"),
            "family": slot.get("family"),
            "notional_usdt": meta.get("notional_usdt") or slot.get("quote_usdt") or 1000,
            "approved_at": meta.get("approved_at"),
            "mode": meta.get("mode") or "live",
            "gate_pass": meta.get("gate_pass"),
            "gate_fail_reasons": meta.get("gate_fail_reasons") or [],
            "label_zh": meta.get("label_zh") or ("訊號監看（未核准下單）" if meta.get("mode") == "signal_only" else "已核准 · 上線待命"),
            "status": meta.get("status") or "live_standby",
            "label_zh": "已核准 · 上線待命",
        })
    return {"ok": True, "approved": out, "count": len(out)}


def build_status() -> dict:
    store = StateStore()
    state = store.load()
    feed = store.load_feed() or {}
    client = BinanceClient()
    bals = _balances(client) if client.api_key else []
    bals_map = {b["asset"]: float(b["free"]) + float(b.get("locked") or 0) for b in bals}
    positions = _open_positions(client, state)
    recent_fills: list[dict] = []
    for sym in ["FETUSDT", "OPUSDT", "DOTUSDT", "SOLUSDT"]:
        try:
            for t in client.my_trades(sym, limit=10):
                recent_fills.append(
                    {
                        "symbol": sym,
                        "id": t.get("id"),
                        "time": datetime.fromtimestamp(int(t["time"]) / 1000, tz=timezone.utc)
                        .astimezone()
                        .isoformat(timespec="seconds"),
                        "side": "BUY" if t.get("isBuyer") else "SELL",
                        "qty": float(t.get("qty") or 0),
                        "price": float(t.get("price") or 0),
                        "quoteQty": float(t.get("quoteQty") or 0),
                        "commission": t.get("commission"),
                    }
                )
        except Exception as e:  # noqa: BLE001
            log.warning("mytrades_skip %s err=%s", sym, e)
    recent_fills.sort(key=lambda x: x.get("time") or "", reverse=True)
    closed_norm = []
    for trow in (state.get("closed_trades") or [])[-50:]:
        closed_norm.append(
            {
                **trow,
                "closed_at": trow.get("closed_at") or trow.get("time"),
                "exit": trow.get("exit") or trow.get("price"),
                "pnl_usdt": trow.get("pnl_usdt"),
            }
        )
    paused = bool(state.get("paused"))
    meta = state.get("meta") or {}
    mode = os.environ.get("TRADER_MODE") or meta.get("last_mode") or "dry-run"
    armed = _slot_status(state, feed.get("slots"))
    return {
        "ok": True,
        "updated_at": now_iso_taipei(),
        "source": "cloud",
        "mode": mode,
        "paused": paused,
        "health": _health(meta, paused),
        "balances": bals,
        "balances_map": bals_map,
        "equity_usdt": bals_map.get("USDT"),
        "positions": positions,
        "open_positions": positions,
        "armed_slots": armed,
        "recent_fills": recent_fills[:30],
        "closed_trades": closed_norm,
        "last_job_run_at": meta.get("last_run_at"),
        "last_job_run_at_utc": meta.get("last_run_at_utc"),
        "last_job_ok": meta.get("last_ok"),
        "last_mode": meta.get("last_mode"),
        "sol_expectation_log": (state.get("expectation_log") or [])[-30:],
        "sol_expectation_latest": (state.get("expectation_log") or [None])[-1],
        "feed_updated_at": feed.get("updated_at"),
        "title": "Binance Demo 帳戶（真實成交）",
        "source_label": "Binance Demo · Cloud Run",
        "planned_positions": _planned_from_armed(armed),
        "approved": _approved_public(state).get("approved"),
        "satellite_strategies": [],
        "strategies": [],
    }


@app.route("/status", methods=["GET"])
def status():
    try:
        return jsonify(build_status())
    except Exception as e:  # noqa: BLE001
        log.exception("status_fail")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/pause", methods=["POST"])
def pause():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code

    def mut(st):
        st["paused"] = True
        st.setdefault("meta", {})["paused_at"] = now_iso_taipei()
        st["meta"]["paused_by"] = "api"
        return st

    try:
        StateStore().mutate(mut)
        return jsonify({"ok": True, "paused": True, "message": "已暫停自動開倉（既有持倉續管止損／出場）"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/resume", methods=["POST"])
def resume():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code

    def mut(st):
        st["paused"] = False
        st.setdefault("meta", {})["resumed_at"] = now_iso_taipei()
        return st

    try:
        StateStore().mutate(mut)
        return jsonify({"ok": True, "paused": False, "message": "已恢復自動交易"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/close", methods=["POST"])
def close_slot():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    body = request.get_json(silent=True) or {}
    slot = (body.get("slot") or "").strip()
    if not slot:
        return jsonify({"ok": False, "error": "缺少 slot"}), 400

    store = StateStore()
    result_holder: dict = {}

    def mut(st):
        client = BinanceClient()
        result_holder["r"] = market_close_slot(client, st, slot, reason="manual")
        return st

    try:
        store.mutate(mut)
        r = result_holder.get("r") or {}
        if not r.get("ok"):
            return jsonify({"ok": False, "error": r.get("error") or "平倉失敗", "slot": slot}), 400
        return jsonify({"ok": True, "message": f"已手動市價平倉 {slot}", "closed": r.get("closed")})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/approved", methods=["GET"])
def approved_list():
    try:
        return jsonify(_approved_public())
    except Exception as e:  # noqa: BLE001
        log.exception("approved_list_fail")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/approve", methods=["POST"])
def approve():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    body = request.get_json(silent=True) or {}
    strategy_id = (body.get("strategy_id") or "").strip()
    if not strategy_id:
        return jsonify({"ok": False, "error": "缺少 strategy_id"}), 400
    slot = slot_by_strategy_id(strategy_id)
    family = (body.get("family") or (slot or {}).get("family") or "").lower()
    if body.get("passed_threshold") is False:
        return jsonify({"ok": False, "error": "未過門檻，無法核准"}), 400
    supported = body.get("supported_by_runner")
    if supported is None:
        supported = family in SUPPORTED_FAMILIES or family.startswith("donchian")
    if supported is False:
        return jsonify({"ok": False, "error": "雲端尚未支援此策略類型"}), 400
    try:
        notional = float(
            body.get("notional")
            or body.get("notional_usdt")
            or (slot or {}).get("quote_usdt")
            or 1000
        )
    except Exception:  # noqa: BLE001
        notional = 1000.0
    notional = min(max(notional, 10.0), float(MAX_NOTIONAL_USDT))

    def mut(st):
        approved = _ensure_approved(st)
        approved[strategy_id] = {
            "slot": (slot or {}).get("id"),
            "notional_usdt": notional,
            "approved_at": now_iso_taipei(),
            "status": "live_standby",
            "family": family or (slot or {}).get("family"),
            "label_zh": "已核准 · 上線待命",
        }
        st["approved"] = approved
        return st

    try:
        StateStore().mutate(mut)
        return jsonify({
            "ok": True,
            "message": f"已核准 {strategy_id}（上線待命）",
            "strategy_id": strategy_id,
            "notional_usdt": notional,
            "status": "live_standby",
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/revoke", methods=["POST"])
def revoke():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    body = request.get_json(silent=True) or {}
    strategy_id = (body.get("strategy_id") or "").strip()
    if not strategy_id:
        return jsonify({"ok": False, "error": "缺少 strategy_id"}), 400
    result = {"had_position": False}

    def mut(st):
        approved = _ensure_approved(st)
        meta = approved.pop(strategy_id, None)
        st["approved"] = approved
        slot_id = (meta or {}).get("slot") or (slot_by_strategy_id(strategy_id) or {}).get("id")
        pos = (st.get("positions") or {}).get(slot_id) if slot_id else None
        if pos and float(pos.get("qty") or 0) > 0:
            result["had_position"] = True
            pos["revoked_no_new_entries"] = True
            pos["revoked_at"] = now_iso_taipei()
            st.setdefault("positions", {})[slot_id] = pos
        st.setdefault("meta", {})["last_revoke"] = {
            "strategy_id": strategy_id,
            "at": now_iso_taipei(),
            "had_position": result["had_position"],
        }
        return st

    try:
        StateStore().mutate(mut)
        msg = f"已撤銷 {strategy_id}"
        if result["had_position"]:
            msg += "（仍有持倉：續管止損／出場，不再新開倉）"
        return jsonify({
            "ok": True,
            "message": msg,
            "strategy_id": strategy_id,
            "had_position": result["had_position"],
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500



def main():
    port = int(os.environ.get("PORT") or 8080)
    log.info("api_listen port=%s cors=%s pin_hash_set=%s", port, CORS_ORIGIN, bool(PIN_HASH))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
