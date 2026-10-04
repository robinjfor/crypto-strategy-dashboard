#!/usr/bin/env python3
"""One-off: move only the USDT shortfall (+ small buffer) from Demo spot to USD-M futures.

Runs inside the Cloud Run job (asia-east1). Never places orders. Never prints keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
import urllib.parse

import requests

# ls_op_4h failed enter at 00:01 TPT 2026-10-04: quote 633.9436 USDT, leverage 1x isolated.
NOTIONAL = 633.94362565327
LEVERAGE = 1.0
# 0.1% fee cushion + 15 USDT so the same-size retry is not short by a rounding tick.
REQUIRED = NOTIONAL / LEVERAGE * 1.001 + 15.0
# Hard cap: never move the whole spot book.
MAX_TRANSFER = 800.0
SPOT_FLOOR = 100.0  # leave at least this much free USDT on spot


def _signed(base: str, method: str, path: str, params: dict | None = None) -> dict | list:
    key = (os.environ.get("BINANCE_DEMO_API_KEY") or "").strip()
    sec = (os.environ.get("BINANCE_DEMO_API_SECRET") or "").strip()
    if not key or not sec:
        raise RuntimeError("missing demo api key/secret")
    params = dict(params or {})
    params["timestamp"] = int(time.time() * 1000)
    params["recvWindow"] = 5000
    qs = urllib.parse.urlencode(params)
    sig = hmac.new(sec.encode(), qs.encode(), hashlib.sha256).hexdigest()
    url = f"{base.rstrip('/')}{path}?{qs}&signature={sig}"
    r = requests.request(method, url, headers={"X-MBX-APIKEY": key}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def _usdt_spot(spot_base: str) -> float:
    acct = _signed(spot_base, "GET", "/api/v3/account")
    for b in acct.get("balances") or []:
        if b.get("asset") == "USDT":
            return float(b.get("free") or 0)
    return 0.0


def _usdt_futures(fut_base: str) -> dict:
    acct = _signed(fut_base, "GET", "/fapi/v2/account")
    out = {
        "wallet": 0.0,
        "available": 0.0,
        "margin_balance": 0.0,
        "total_wallet": float(acct.get("totalWalletBalance") or 0),
        "available_account": float(acct.get("availableBalance") or 0),
    }
    for a in acct.get("assets") or []:
        if a.get("asset") == "USDT":
            out["wallet"] = float(a.get("walletBalance") or 0)
            out["available"] = float(a.get("availableBalance") or 0)
            out["margin_balance"] = float(a.get("marginBalance") or 0)
            break
    return out


def _record(payload: dict) -> None:
    """Stash result on state.meta.futures_order_probe.fund_umfuture (visible in /status)."""
    try:
        from state_store import StateStore
        store = StateStore()
        st = store.load()
        meta = st.setdefault("meta", {})
        probe = meta.get("futures_order_probe")
        if not isinstance(probe, dict):
            probe = {}
        probe["fund_umfuture"] = payload
        meta["futures_order_probe"] = probe
        store.save(st)
        payload["recorded"] = True
    except Exception as e:  # noqa: BLE001
        payload["recorded"] = False
        payload["record_error"] = str(e)[:300]


def main() -> int:
    spot_base = os.environ.get("BINANCE_BASE_URL", "https://demo-api.binance.com")
    fut_base = os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com")
    spot = _usdt_spot(spot_base)
    fut = _usdt_futures(fut_base)
    wallet, avail = fut["wallet"], fut["available"]
    short = max(0.0, REQUIRED - avail)
    amt = math.ceil(short * 100) / 100.0
    amt = min(amt, MAX_TRANSFER)
    cap = max(0.0, spot - SPOT_FLOOR)
    if amt > cap:
        amt = math.floor(cap * 100) / 100.0
    before = {
        "spot_usdt_free": round(spot, 4),
        "futures_wallet": round(wallet, 4),
        "futures_available": round(avail, 4),
        "futures_margin_balance": round(fut["margin_balance"], 4),
        "futures_total_wallet": round(fut["total_wallet"], 4),
        "required_available": round(REQUIRED, 4),
        "transfer_usdt": amt,
        "slot": "ls_op_4h",
        "notional": NOTIONAL,
        "leverage": LEVERAGE,
    }
    if os.environ.get("FUND_READONLY", "").strip().lower() in ("1", "true", "yes"):
        done = {"phase": "readonly", "transferred": 0, "reason": "read_only", **before}
        _record(done)
        print(json.dumps(done), flush=True)
        return 0
    _record({"phase": "before", **before})
    print(json.dumps({"phase": "before", **before}), flush=True)
    if amt < 1.0:
        done = {"phase": "done", "transferred": 0, "reason": "futures_margin_sufficient_or_spot_floor", **before}
        _record(done)
        print(json.dumps(done), flush=True)
        return 0
    amount = f"{amt:.2f}"
    candidates = [
        (spot_base, "/sapi/v1/asset/transfer", {"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": amount}, "MAIN_UMFUTURE"),
        (spot_base, "/sapi/v1/futures/transfer", {"asset": "USDT", "amount": amount, "type": 1}, "futures_transfer_type1"),
        (fut_base, "/fapi/v1/transfer", {"asset": "USDT", "amount": amount, "type": 1}, "fapi_transfer_type1"),
        (fut_base, "/sapi/v1/asset/transfer", {"type": "MAIN_UMFUTURE", "asset": "USDT", "amount": amount}, "fapi_host_MAIN_UMFUTURE"),
    ]
    attempts = []
    resp = None
    via = None
    for base, path, params, name in candidates:
        try:
            resp = _signed(base, "POST", path, params)
            via = name
            attempts.append({"via": name, "ok": True})
            break
        except Exception as e:  # noqa: BLE001
            attempts.append({"via": name, "ok": False, "error": str(e)[:220]})
            resp = None
    if resp is None:
        fail = {"phase": "error", "transferred": 0, "attempts": attempts, "before": before}
        _record(fail)
        print(json.dumps(fail), flush=True)
        return 0
    time.sleep(1.5)
    spot2 = _usdt_spot(spot_base)
    fut2 = _usdt_futures(fut_base)
    wallet2, avail2 = fut2["wallet"], fut2["available"]
    after = {
        "phase": "after",
        "via": via,
        "attempts": attempts,
        "tranId": (resp.get("tranId") if isinstance(resp, dict) else None),
        "spot_usdt_free": round(spot2, 4),
        "futures_wallet": round(wallet2, 4),
        "futures_available": round(avail2, 4),
        "transferred": amt,
        "before": before,
    }
    _record(after)
    print(json.dumps(after), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        err = {"phase": "error", "error": str(e)[:500]}
        try:
            _record(err)
        except Exception:
            pass
        print(json.dumps(err), flush=True)
        raise SystemExit(0)
