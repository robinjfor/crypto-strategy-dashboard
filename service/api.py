#!/usr/bin/env python3
"""Cloud Run HTTP API: session-gated /status + /control; POST /auth/login."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request

from binance_client import BinanceClient
from execution import market_close_slot
from slots import SLOTS, SOL_SLOT, DEFAULT_APPROVED, DEFAULT_SIGNAL_ONLY, MAX_NOTIONAL_USDT, slot_by_strategy_id, strategy_id_for_slot, SUPPORTED_FAMILIES, approval_mode, is_approved_live, LABEL_SIGNAL_ONLY, apply_satellite_slot_approval, ensure_approved_families, approve_family, revoke_family, DEFAULT_APPROVED_FAMILIES, is_family_approved
from state_store import StateStore
from allocation import (
    resolve_allocation, validate_allocation, live_slots as alloc_live_slots,
    normalize_family, RUNNER_FAMILIES, slot_to_runtime,
)
from strategy import now_iso_taipei
from strategy_codes import add_code, code_for, code_for_symbol



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
AUTH_HEADER = "Authorization"
SESSION_TTL_SEC = int(os.environ.get("SESSION_TTL_SEC") or str(12 * 3600))
# Prefer explicit secret; else derive stably from pin hash (never the raw PIN).
_SESSION_SECRET_ENV = (os.environ.get("SESSION_HMAC_SECRET") or "").strip()
SESSION_HMAC_SECRET = (
    _SESSION_SECRET_ENV.encode("utf-8")
    if _SESSION_SECRET_ENV
    else hashlib.sha256(("session-v1|" + PIN_SALT + "|" + PIN_HASH).encode("utf-8")).digest()
)

_fail_buckets: dict[str, list[float]] = {}
RATE_MAX = 5
RATE_WINDOW = 600.0
_bal_cache: dict[str, Any] = {"ts": 0.0, "data": None}
BAL_TTL = 15.0


def normalize_pin(raw: str | None) -> str:
    """Strip whitespace and map full-width digits (０-９) to ASCII."""
    if raw is None:
        return ""
    s = str(raw).strip()
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if 0xFF10 <= o <= 0xFF19:
            out.append(chr(o - 0xFF10 + ord("0")))
        else:
            out.append(ch)
    return "".join(out)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint_session_token(now: float | None = None) -> tuple[str, int]:
    """Return (token, exp_unix). Payload is compact JSON; HMAC-SHA256 over it."""
    ts = int(now if now is not None else time.time())
    exp = ts + SESSION_TTL_SEC
    payload = json.dumps({"v": 1, "exp": exp}, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(SESSION_HMAC_SECRET, payload, hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}", exp


def verify_session_token(token: str) -> bool:
    if not token or "." not in token:
        return False
    try:
        body_b64, sig_b64 = token.split(".", 1)
        payload = _b64url_decode(body_b64)
        expected = hmac.new(SESSION_HMAC_SECRET, payload, hashlib.sha256).digest()
        got = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected, got):
            return False
        data = json.loads(payload.decode("utf-8"))
        exp = int(data.get("exp") or 0)
        if exp < int(time.time()):
            return False
        return int(data.get("v") or 0) == 1
    except Exception:  # noqa: BLE001
        return False


def _pin_digest(pin: str) -> str:
    return hashlib.sha256((PIN_SALT + pin).encode("utf-8")).hexdigest()


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = CORS_ORIGIN
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = f"Content-Type, {PIN_HEADER}, {AUTH_HEADER}"
    resp.headers["Access-Control-Max-Age"] = "3600"
    return resp


@app.after_request
def after(resp):
    return _cors(resp)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "crypto-trader-api", "ok": True})


@app.route("/status", methods=["OPTIONS"])
@app.route("/auth/login", methods=["OPTIONS"])
@app.route("/control/pause", methods=["OPTIONS"])
@app.route("/control/resume", methods=["OPTIONS"])
@app.route("/control/close", methods=["OPTIONS"])
@app.route("/control/close_all", methods=["OPTIONS"])
@app.route("/control/approve", methods=["OPTIONS"])
@app.route("/control/revoke", methods=["OPTIONS"])
@app.route("/control/approve_family", methods=["OPTIONS"])
@app.route("/control/revoke_family", methods=["OPTIONS"])
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


def _clear_fails(ip: str) -> None:
    _fail_buckets.pop(ip, None)


def _require_https() -> tuple[bool, str, int] | None:
    proto = (request.headers.get("X-Forwarded-Proto") or request.scheme or "").lower()
    if proto and proto != "https" and os.environ.get("ALLOW_HTTP_PIN") != "1":
        return False, "僅接受 HTTPS", 403
    return None


def _check_pin_value(pin_raw: str) -> bool:
    pin = normalize_pin(pin_raw)
    if not pin or not PIN_HASH:
        return False
    return hmac.compare_digest(_pin_digest(pin), PIN_HASH)


def _check_auth() -> tuple[bool, str, int]:
    """Accept Authorization: Bearer <session> or legacy X-Trader-Pin (normalized)."""
    if not PIN_HASH:
        return False, "伺服器未設定控制 PIN 雜湊", 503
    ip = _client_ip()
    if _rate_limited(ip):
        return False, "嘗試次數過多，請稍後再試", 429
    https_err = _require_https()
    if https_err is not None:
        return https_err

    auth = request.headers.get(AUTH_HEADER) or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if verify_session_token(token):
            _clear_fails(ip)
            return True, "", 200
        _record_fail(ip)
        return False, "登入已過期或無效，請重新登入", 401

    pin = request.headers.get(PIN_HEADER) or ""
    if not pin:
        _record_fail(ip)
        return False, "未登入或缺少操作密碼", 401
    if not _check_pin_value(pin):
        _record_fail(ip)
        return False, "操作密碼錯誤", 401
    _clear_fails(ip)
    return True, "", 200


def _check_pin() -> tuple[bool, str, int]:
    """Back-compat alias — prefer session Bearer via _check_auth."""
    return _check_auth()


@app.route("/auth/login", methods=["POST"])
def auth_login():
    if not PIN_HASH:
        return jsonify({"ok": False, "error": "伺服器未設定控制 PIN 雜湊"}), 503
    ip = _client_ip()
    if _rate_limited(ip):
        return jsonify({"ok": False, "error": "嘗試次數過多，請稍後再試"}), 429
    https_err = _require_https()
    if https_err is not None:
        return jsonify({"ok": False, "error": https_err[1]}), https_err[2]

    body = request.get_json(silent=True) or {}
    pin = body.get("pin") or body.get("password") or request.headers.get(PIN_HEADER) or ""
    if not normalize_pin(pin):
        _record_fail(ip)
        return jsonify({"ok": False, "error": "缺少密碼"}), 401
    if not _check_pin_value(pin):
        _record_fail(ip)
        return jsonify({"ok": False, "error": "密碼錯誤"}), 401

    _clear_fails(ip)
    token, exp = mint_session_token()
    return jsonify({
        "ok": True,
        "token": token,
        "expires_at": exp,
        "expires_in": SESSION_TTL_SEC,
        "token_type": "Bearer",
    })


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
        venue = pos.get("venue") or "spot"
        side = str(pos.get("side") or "LONG").upper()
        lev = int(float(pos.get("leverage") or 1))
        try:
            if venue == "futures":
                from futures_client import FuturesDemoClient
                from futures_execution import mark_price as fut_mark
                mark = fut_mark(FuturesDemoClient(), symbol)
            else:
                mark = client.ticker_price(symbol)
        except Exception:  # noqa: BLE001
            pass
        if side == "SHORT":
            upnl = (entry - mark) * qty if entry and mark else None
        else:
            upnl = (mark - entry) * qty if entry and mark else None
        display = f"{symbol} {'多' if side == 'LONG' else '空'}×{lev}" if venue == "futures" else symbol
        out.append(
            {
                "slot": slot_id,
                "strategy_id": pos.get("strategy_id") or strategy_id_for_slot(slot_id),
                "code": pos.get("code") or code_for(pos.get("strategy_id") or strategy_id_for_slot(slot_id), pos.get("family")),
                "symbol": display,
                "raw_symbol": symbol,
                "asset": f"{symbol.replace('USDT', '')} {'多' if side == 'LONG' else '空'}×{lev}" if venue == "futures" else symbol.replace("USDT", ""),
                "qty": qty,
                "entry": entry,
                "mark": mark,
                "mark_price": mark,
                "stop": pos.get("stop"),
                "donch_lo": pos.get("donch_lo"),
                "unrealized_pnl": round(upnl, 4) if upnl is not None else None,
                "market_value": abs(qty) * mark if mark else None,  # full notional
                "status": pos.get("status") or "FILLED",
                "filled_at": pos.get("filled_at"),
                "tf": "1d" if slot_id == "core_sol" else pos.get("tf"),
                "side": side if venue == "futures" else None,
                "leverage": lev if venue == "futures" else None,
                "venue": venue,
            }
        )
    # Also surface any Demo futures positions not yet mirrored into state
    try:
        from futures_client import FuturesDemoClient
        from futures_execution import list_open_futures_positions
        known = {r.get("raw_symbol") for r in out if r.get("venue") == "futures"}
        for fr in list_open_futures_positions(FuturesDemoClient()):
            if fr.get("raw_symbol") in known:
                continue
            out.append(add_code(fr, fr.get("strategy_id"), fr.get("family")))
    except Exception as e:  # noqa: BLE001
        log.warning("futures_positions_merge_fail err=%s", e)
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
                "strategy_id": slot.get("strategy_id"),
                "code": code_for(slot.get("strategy_id"), slot.get("family")),
                "approved": is_approved_live(state, slot.get("strategy_id") or ""),
                "order_mode": approval_mode(state, slot.get("strategy_id") or ""),
                "label_zh": (
                    LABEL_SIGNAL_ONLY
                    if approval_mode(state, slot.get("strategy_id") or "") == "signal_only"
                    else ("已核准 · 上線待命" if is_approved_live(state, slot.get("strategy_id") or "") else "未核准")
                ),
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
            "strategy_id": SOL_SLOT.get("strategy_id"),
            "code": code_for(SOL_SLOT.get("strategy_id"), SOL_SLOT.get("family")),
            "approved": False,
            "order_mode": "signal_only",
            "mode": "signal_only",
            "label_zh": LABEL_SIGNAL_ONLY,
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
                "strategy_id": a.get("strategy_id"),
                "code": a.get("code") or code_for(a.get("strategy_id"), a.get("family")),
                "tf": a.get("tf"),
                "donch_n": 55 if "fet" in str(a.get("slot")) else 20,
                "target_notional_usdt": a.get("quote_usdt"),
                "ui_status": status,
                "status_label": (LABEL_SIGNAL_ONLY if a.get("order_mode") == "signal_only" or a.get("mode") == "signal_only" else status_zh(a.get("status") or a.get("reason") or status)),
                "approved": a.get("approved"),
                "order_mode": a.get("order_mode") or a.get("mode") or "live",
                "label_zh": a.get("label_zh"),
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
    """Seed DEFAULT_APPROVED; sync signal_only with family+allocation live gate.

    Strategies whose family is approved and allocation slot enabled are LIVE —
    do NOT demote them into signal_only just because they appear in
    DEFAULT_SIGNAL_ONLY (that list is only the pre-approval default).
    """
    approved = state.get("approved")
    if not isinstance(approved, dict):
        approved = {}
    dirty = False
    if not approved:
        for sid, meta in DEFAULT_APPROVED.items():
            approved[sid] = {**meta, "approved_at": now_iso_taipei(), "approved": True, "mode": "live"}
        dirty = True
    else:
        for sid, meta in DEFAULT_APPROVED.items():
            if sid not in approved:
                approved[sid] = {**meta, "approved_at": now_iso_taipei(), "approved": True, "mode": "live"}
                dirty = True
            else:
                cur = approved[sid]
                if cur.get("mode") == "signal_only" or cur.get("approved") is False:
                    approved[sid] = {**meta, **cur, "mode": "live", "approved": True, "label_zh": meta.get("label_zh")}
                    dirty = True
    monitored = state.get("signal_only") if isinstance(state.get("signal_only"), dict) else {}
    for sid, meta in DEFAULT_SIGNAL_ONLY.items():
        # Family+allocation live → promote out of signal_only
        if is_approved_live(state, sid):
            if sid in monitored:
                monitored.pop(sid, None)
                dirty = True
            if sid not in approved:
                slot = slot_by_strategy_id(sid) or {}
                approved[sid] = {
                    **meta,
                    "approved": True,
                    "mode": "live",
                    "approved_at": now_iso_taipei(),
                    "label_zh": "已核准 · 上線待命",
                    "slot": meta.get("slot") or slot.get("id"),
                    "family": slot.get("family") or meta.get("family"),
                    "notional_usdt": meta.get("notional_usdt") or slot.get("quote_usdt"),
                }
                dirty = True
            continue
        if sid in approved:
            approved.pop(sid, None)
            dirty = True
        if sid not in monitored:
            monitored[sid] = {**meta, "updated_at": now_iso_taipei()}
            dirty = True
        else:
            monitored[sid] = {**meta, **monitored[sid], "mode": "signal_only", "approved": False, "label_zh": LABEL_SIGNAL_ONLY}
    state["approved"] = approved
    state["signal_only"] = monitored
    if dirty:
        state["_seeded_approved_dirty"] = True
    return approved




def _approved_public(state: dict | None = None) -> dict:
    store = StateStore()
    st = state if state is not None else store.load()
    approved = _ensure_approved(st)
    if state is None and st.get("_seeded_approved_dirty"):
        def _seed(s):
            _ensure_approved(s)
            s.pop("_seeded_approved_dirty", None)
            return s
        try:
            store.mutate(_seed)
            st = store.load()
            approved = st.get("approved") or approved
        except Exception as e:  # noqa: BLE001
            log.warning("approved_seed_save_skip err=%s", e)

    out = []
    for sid, meta in (approved or {}).items():
        if not isinstance(meta, dict):
            continue
        if str(meta.get("mode") or "live") == "signal_only" or meta.get("approved") is False:
            continue
        slot = slot_by_strategy_id(sid) or {}
        out.append({
            "strategy_id": sid,
            "code": code_for(sid, slot.get("family") or meta.get("family")),
            "slot": meta.get("slot") or slot.get("id"),
            "symbol": slot.get("symbol"),
            "family": slot.get("family") or meta.get("family"),
            "notional_usdt": meta.get("notional_usdt") or slot.get("quote_usdt") or 1000,
            "approved_at": meta.get("approved_at"),
            "approved": True,
            "mode": "live",
            "order_mode": "live",
            "status": "live_standby",
            "label_zh": meta.get("label_zh") or "已核准 · 上線待命",
        })
    sig_only = []
    mon = st.get("signal_only") if isinstance(st.get("signal_only"), dict) else dict(DEFAULT_SIGNAL_ONLY)
    for sid, meta in mon.items():
        slot = slot_by_strategy_id(sid) or {}
        sig_only.append({
            "strategy_id": sid,
            "code": code_for(sid, slot.get("family") or meta.get("family")),
            "slot": meta.get("slot") or slot.get("id"),
            "symbol": slot.get("symbol"),
            "family": slot.get("family") or meta.get("family"),
            "approved": False,
            "mode": "signal_only",
            "order_mode": "signal_only",
            "label_zh": LABEL_SIGNAL_ONLY,
            "gate_pass": meta.get("gate_pass"),
            "gate_fail_reasons": meta.get("gate_fail_reasons") or [],
            "satellite_slot": meta.get("satellite_slot") or slot.get("satellite_slot"),
        })
    return {"ok": True, "approved": out, "signal_only": sig_only, "count": len(out)}



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
                        "code": code_for_symbol(sym, SLOTS + [SOL_SLOT]),
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
    live_allocation = (state.get("allocation_public") or _allocation_public(state)).get("live_slots") or []
    last_decisions = [add_code(d, d.get("strategy_id"), d.get("family")) for d in (meta.get("last_decisions") or []) if isinstance(d, dict)]
    closed_norm = [add_code(t, t.get("strategy_id"), t.get("family")) for t in closed_norm]
    for trade in closed_norm:
        if not trade.get("code"):
            trade["code"] = code_for_symbol(trade.get("symbol"), SLOTS + [SOL_SLOT])
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
        "futures_order_probe": (state.get("meta") or {}).get("futures_order_probe"),
        "last_job_ok": meta.get("last_ok"),
        "last_mode": meta.get("last_mode"),
        "last_decisions": last_decisions,
        "last_decisions_at": meta.get("last_decisions_at"),
        "sol_expectation_log": (state.get("expectation_log") or [])[-30:],
        "sol_expectation_latest": (state.get("expectation_log") or [None])[-1],
        "feed_updated_at": feed.get("updated_at"),
        "title": "Binance Demo 帳戶（真實成交）",
        "source_label": "Binance Demo · Cloud Run",
        "planned_positions": _planned_from_armed(armed),
        "approved": _approved_public(state).get("approved"),
        "signal_only": _approved_public(state).get("signal_only"),
        "approved_families": ensure_approved_families(state),
        "allocation": (state.get("allocation_public") or _allocation_public(state)),
        "allocation_alert": state.get("allocation_alert"),
        "live_slots": [add_code(x, x.get("strategy_id"), x.get("family")) for x in live_allocation if isinstance(x, dict)],
        "satellite_strategies": [],
        "strategies": [],
    }


@app.route("/status", methods=["GET"])
def status():
    ok, err, code = _check_auth()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
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




@app.route("/control/close_all", methods=["POST"])
def close_all():
    """Pause first, then market-close every open position under the GCS lock."""
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code

    store = StateStore()
    result_holder: dict = {}

    def mut(st):
        st["paused"] = True
        st.setdefault("meta", {})["paused_at"] = now_iso_taipei()
        st["meta"]["paused_by"] = "close_all"
        pos_map = dict(st.get("positions") or {})
        if not pos_map:
            result_holder["r"] = {
                "ok": True,
                "paused": True,
                "results": [],
                "message": "目前沒有持倉，已只暫停",
            }
            return st
        client = BinanceClient()
        results = []
        for slot_id in list(pos_map.keys()):
            snap = pos_map.get(slot_id) or {}
            r = market_close_slot(client, st, slot_id, reason="close_all")
            closed = r.get("closed") or {}
            results.append(
                {
                    "slot": slot_id,
                    "symbol": closed.get("symbol") or snap.get("symbol") or slot_id,
                    "ok": bool(r.get("ok")),
                    "error": r.get("error"),
                    "closed": closed if r.get("ok") else None,
                }
            )
        all_ok = all(x["ok"] for x in results)
        result_holder["r"] = {
            "ok": all_ok,
            "paused": True,
            "results": results,
            "message": ("已暫停並平倉全部持倉" if all_ok else "已暫停，部分平倉失敗"),
        }
        return st

    try:
        store.mutate(mut)
        r = result_holder.get("r") or {}
        status = 200 if r.get("ok") else 207
        return jsonify(r), status
    except Exception as e:  # noqa: BLE001
        log.exception("close_all_fail")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/approved", methods=["GET"])
def approved_list():
    ok, err, code = _check_auth()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    try:
        return jsonify(_approved_public())
    except Exception as e:  # noqa: BLE001
        log.exception("approved_list_fail")
        return jsonify({"ok": False, "error": str(e)}), 500




def _allocation_public(state: dict) -> dict:
    """Load allocation (GCS→repo), validate, expose live/signal views."""
    try:
        doc, source = resolve_allocation()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "source": None, "live_slots": [], "slots": []}
    # Prefer last-good cached on state if current invalid
    errs = validate_allocation(doc, check_binance=False)
    alert = None
    if errs:
        alert = {"ok": False, "errors": errs, "message": "配置檔驗證失敗，沿用上一份有效配置"}
        cached = state.get("allocation_last_good")
        if isinstance(cached, dict):
            doc = cached
            source = "last_good"
        else:
            # still expose invalid doc for debugging but no live slots
            return {
                "ok": False,
                "source": source,
                "updated_at": doc.get("updated_at"),
                "updated_by": doc.get("updated_by"),
                "slots": doc.get("slots") or [],
                "live_slots": [],
                "errors": errs,
            }
    else:
        # cache last good on state object (caller may persist)
        state["allocation_last_good"] = doc
        state["allocation_alert"] = None
    fams = ensure_approved_families(state)
    live = alloc_live_slots(doc, fams)
    return {
        "ok": True,
        "source": source,
        "updated_at": doc.get("updated_at"),
        "updated_by": doc.get("updated_by"),
        "book_usdt": doc.get("book_usdt"),
        "slots": doc.get("slots") or [],
        "live_slots": live,
        "approved_families": fams,
        "errors": None,
        "alert": alert,
    }


@app.route("/control/approve_family", methods=["POST"])
def approve_family_ep():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    body = request.get_json(silent=True) or {}
    family = normalize_family((body.get("family") or "").strip())
    if not family:
        return jsonify({"ok": False, "error": "缺少 family"}), 400
    if family not in RUNNER_FAMILIES and family not in SUPPORTED_FAMILIES:
        return jsonify({"ok": False, "error": f"雲端尚未支援此策略類型：{family}"}), 400

    holder: dict = {}

    def mut(st):
        holder["r"] = approve_family(st, family, at=now_iso_taipei(), by="api")
        # Keep derived per-strategy approved in sync for older clients
        _sync_approved_from_families(st)
        return st

    try:
        StateStore().mutate(mut)
        r = holder.get("r") or {}
        return jsonify({
            "ok": True,
            "message": f"已批准策略家族 {family}",
            "family": family,
            "approved_families": r.get("approved_families"),
        })
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/control/revoke_family", methods=["POST"])
def revoke_family_ep():
    ok, err, code = _check_pin()
    if not ok:
        return jsonify({"ok": False, "error": err}), code
    body = request.get_json(silent=True) or {}
    family = normalize_family((body.get("family") or "").strip())
    if not family:
        return jsonify({"ok": False, "error": "缺少 family"}), 400

    holder: dict = {}

    def mut(st):
        holder["r"] = revoke_family(st, family, at=now_iso_taipei(), by="api")
        _sync_approved_from_families(st)
        return st

    try:
        StateStore().mutate(mut)
        r = holder.get("r") or {}
        return jsonify({
            "ok": True,
            "message": f"已撤銷策略家族 {family}（停止開新倉；既有持倉續管）",
            "family": family,
            "approved_families": r.get("approved_families"),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


def _sync_approved_from_families(st: dict) -> None:
    """Derive legacy per-strategy approved dict from approved_families + allocation."""
    fams = ensure_approved_families(st)
    pub = _allocation_public(st)
    live_ids = {s.get("strategy_id") for s in (pub.get("live_slots") or [])}
    approved = {}
    for sid in live_ids:
        if not sid:
            continue
        slot = slot_by_strategy_id(sid) or {}
        approved[sid] = {
            "slot": slot.get("id"),
            "notional_usdt": slot.get("quote_usdt"),
            "approved": True,
            "mode": "live",
            "family": slot.get("family"),
            "label_zh": "已核准 · 上線待命",
            "approved_at": now_iso_taipei(),
        }
    # Also mark DEFAULT_APPROVED if family approved and no allocation hit
    for sid, meta in DEFAULT_APPROVED.items():
        slot = slot_by_strategy_id(sid) or {}
        fam = slot.get("family") or meta.get("family") or "donchian_atr"
        if fam in fams and sid not in approved:
            # only if allocation doesn't explicitly disable
            disabled = False
            for s in (pub.get("slots") or []):
                if s.get("strategy_id") == sid and not s.get("enabled"):
                    disabled = True
                    break
            if not disabled:
                approved[sid] = {**meta, "approved": True, "mode": "live", "family": fam}
    st["approved"] = approved

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
    # Emily 資金控管：僅 3 年窗 gate_pass_3y（全期僅參考）
    if body.get("gate_pass_3y") is False:
        return jsonify({"ok": False, "error": "未過 3 年門檻，無法核准"}), 400
    # Ignore legacy gate_pass_both from old clients — do not block
    supported = body.get("supported_by_runner")
    if supported is None:
        supported = family in SUPPORTED_FAMILIES
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

    summary_holder: dict = {}

    def mut(st):
        _ensure_approved(st)
        # Dynamic slot: if unknown, synthesize from body (ICP etc.)
        nonlocal_slot = slot
        if nonlocal_slot is None and body.get("symbol") and body.get("timeframe"):
            # Register ephemeral monitored config under signal_only until approved
            st.setdefault("dynamic_slots", {})[strategy_id] = {
                "id": f"dyn_{strategy_id[:24]}",
                "strategy_id": strategy_id,
                "family": family or "donchian",
                "symbol": body.get("symbol"),
                "tf": body.get("timeframe"),
                "donch_n": int((body.get("params") or {}).get("donch") or 55),
                "stop_atr_mult": float((body.get("params") or {}).get("stop_atr") or 2.0),
                "trail_atr_mult": float((body.get("params") or {}).get("trail_atr") or 3.0),
                "quote_usdt": notional,
                "require_reset_below_hi": True,
                "armed": True,
                "satellite_slot": body.get("slot") or body.get("satellite_slot"),
            }
        if (slot or {}).get("satellite_slot") or (body.get("slot") == "satellite_A"):
            # Ensure slot dict available
            use_slot = slot or st.get("dynamic_slots", {}).get(strategy_id)
            if use_slot and not slot:
                # temporarily inject into lookup via signal_only meta
                st.setdefault("signal_only", {})[strategy_id] = {
                    **(st.get("signal_only", {}).get(strategy_id) or {}),
                    "slot": use_slot["id"],
                    "satellite_slot": use_slot.get("satellite_slot") or "satellite_A",
                }
            summary_holder["r"] = apply_satellite_slot_approval(
                st, strategy_id, notional=notional, approved_at=now_iso_taipei()
            )
        else:
            approved = st.setdefault("approved", {})
            st.setdefault("signal_only", {}).pop(strategy_id, None)
            approved[strategy_id] = {
                "slot": (slot or {}).get("id"),
                "notional_usdt": notional,
                "approved_at": now_iso_taipei(),
                "approved": True,
                "mode": "live",
                "status": "live_standby",
                "family": family or (slot or {}).get("family"),
                "label_zh": "已核准 · 上線待命",
            }
            st["approved"] = approved
            summary_holder["r"] = {"approved_id": strategy_id, "demoted": []}
        return st

    try:
        StateStore().mutate(mut)
        r = summary_holder.get("r") or {}
        msg = f"已核准 {strategy_id}（上線待命）"
        if r.get("demoted"):
            msg += "；同槽下架：" + ", ".join(r["demoted"])
        return jsonify({
            "ok": True,
            "message": msg,
            "strategy_id": strategy_id,
            "notional_usdt": notional,
            "status": "live_standby",
            "satellite_slot": r.get("satellite_slot"),
            "demoted": r.get("demoted") or [],
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
