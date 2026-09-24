"""Binance Demo USDⓈ-M futures client (demo-fapi). Never logs secrets."""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
import urllib.parse
from typing import Any

import requests

log = logging.getLogger("trader.futures")

# Binance Demo Trading futures base (USD-M). Override via env if docs change.
FUTURES_DEMO_BASE = os.environ.get(
    "BINANCE_FUTURES_DEMO_BASE_URL", "https://demo-fapi.binance.com"
)
UA = {"User-Agent": "crypto-strategy-dashboard-trader/1.0"}
LEVERAGE_HARD_CAP = 3


class FuturesDemoClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = FUTURES_DEMO_BASE,
    ):
        self.api_key = (api_key or os.environ.get("BINANCE_DEMO_API_KEY") or "").strip()
        self.api_secret = (api_secret or os.environ.get("BINANCE_DEMO_API_SECRET") or "").strip()
        self.base_url = base_url.rstrip("/")

    def _public(self, path: str, params: dict | None = None) -> Any:
        r = requests.get(
            f"{self.base_url}{path}", params=params or {}, headers=UA, timeout=30
        )
        if r.status_code == 451:
            raise RuntimeError(f"HTTP 451 geo-blocked from futures demo {path}")
        r.raise_for_status()
        return r.json()

    def _signed(self, method: str, path: str, params: dict | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Missing BINANCE_DEMO_API_KEY/SECRET for futures")
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{qs}&signature={sig}"
        headers = {**UA, "X-MBX-APIKEY": self.api_key}
        r = requests.request(method, url, headers=headers, timeout=30)
        if r.status_code == 451:
            raise RuntimeError(f"HTTP 451 geo-blocked futures demo {path}")
        if not r.ok:
            raise RuntimeError(f"Futures {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.text else {}

    def ping(self) -> dict:
        return self._public("/fapi/v1/ping")

    def server_time(self) -> dict:
        return self._public("/fapi/v1/time")

    def exchange_info(self) -> dict:
        return self._public("/fapi/v1/exchangeInfo")

    def account(self) -> dict:
        return self._signed("GET", "/fapi/v2/account")

    def position_risk(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self._signed("GET", "/fapi/v2/positionRisk", params)

    def set_leverage(self, symbol: str, leverage: int) -> dict:
        lev = max(1, min(int(leverage), LEVERAGE_HARD_CAP))
        return self._signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": lev})

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> dict:
        try:
            return self._signed(
                "POST", "/fapi/v1/marginType",
                {"symbol": symbol, "marginType": margin_type},
            )
        except RuntimeError as e:
            # -4046 already isolated
            if "-4046" in str(e) or "No need to change" in str(e):
                return {"msg": "already_set", "marginType": margin_type}
            raise

    def new_order(self, **params) -> dict:
        return self._signed("POST", "/fapi/v1/order", params)

    def cancel_all(self, symbol: str) -> dict:
        return self._signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})


def probe_futures() -> dict:
    """Connectivity + auth probe. Never returns secrets."""
    out: dict[str, Any] = {
        "base": FUTURES_DEMO_BASE,
        "ok": False,
        "steps": [],
        "needs_emily": [],
    }
    c = FuturesDemoClient()
    if not c.api_key or not c.api_secret:
        out["steps"].append({"step": "env", "ok": False, "error": "missing_api_key_secret"})
        out["needs_emily"].append(
            "在 Cloud Run Job 的密鑰中設定 BINANCE_DEMO_API_KEY / BINANCE_DEMO_API_SECRET，"
            "並確認此金鑰在 Binance Demo Trading 已開啟 USDⓈ-M 合約權限。"
        )
        return out
    out["steps"].append({"step": "env", "ok": True, "key_present": True})
    try:
        c.ping()
        out["steps"].append({"step": "ping", "ok": True})
    except Exception as e:  # noqa: BLE001
        out["steps"].append({"step": "ping", "ok": False, "error": str(e)[:200]})
        out["needs_emily"].append(
            f"無法連線 {FUTURES_DEMO_BASE}（ping 失敗）。請確認 endpoint 與網路 egress。"
        )
        return out
    try:
        t = c.server_time()
        out["steps"].append({"step": "time", "ok": True, "serverTime": t.get("serverTime")})
    except Exception as e:  # noqa: BLE001
        out["steps"].append({"step": "time", "ok": False, "error": str(e)[:200]})
    try:
        acct = c.account()
        # summarize without dumping full balances
        assets = acct.get("assets") or []
        positions = acct.get("positions") or []
        nonzero = [
            {"asset": a.get("asset"), "wallet": a.get("walletBalance")}
            for a in assets
            if float(a.get("walletBalance") or 0) != 0
        ][:8]
        out["steps"].append({
            "step": "account",
            "ok": True,
            "nonzero_assets": nonzero,
            "n_positions": len(positions),
            "canTrade": acct.get("canTrade"),
        })
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]
        out["steps"].append({"step": "account", "ok": False, "error": err})
        if "401" in err or "-2015" in err or "Invalid API" in err or "unauthorized" in err.lower():
            out["needs_emily"].append(
                "Demo API 金鑰無法簽署 futures account。請到 Binance Demo Trading → API Management "
                "確認金鑰啟用 Futures，或重新產生一把含 USDⓈ-M 權限的 Demo 金鑰並更新 Secret Manager。"
            )
        else:
            out["needs_emily"].append(f"Futures account 呼叫失敗：{err}")
    return out


def clamp_leverage(requested: float | int | None) -> int:
    """Integer leverage for Binance; floor so exchange lev never exceeds catalog; hard cap 3."""
    try:
        v = int(float(requested or 1))  # floor toward 0
    except (TypeError, ValueError):
        v = 1
    return max(1, min(v, LEVERAGE_HARD_CAP))


def _filters(info: dict, symbol: str) -> dict:
    for s in info.get("symbols") or []:
        if s.get("symbol") == symbol:
            out = {"status": s.get("status")}
            for f in s.get("filters") or []:
                out[f.get("filterType")] = f
            return out
    return {}


def round_qty_futures(info: dict, symbol: str, qty: float) -> float:
    import math
    f = _filters(info, symbol).get("LOT_SIZE") or {}
    step = float(f.get("stepSize") or 0.001)
    mn = float(f.get("minQty") or 0)
    if step <= 0:
        return qty
    precision = max(0, int(round(-math.log10(step))) if step < 1 else 0)
    q = math.floor(qty / step) * step
    q = float(f"{q:.{precision}f}")
    if q < mn:
        return 0.0
    return q


def round_price_futures(info: dict, symbol: str, price: float) -> float:
    import math
    f = _filters(info, symbol).get("PRICE_FILTER") or {}
    tick = float(f.get("tickSize") or 0.01)
    if tick <= 0:
        return price
    precision = max(0, int(round(-math.log10(tick))) if tick < 1 else 0)
    p = math.floor(price / tick) * tick
    return float(f"{p:.{precision}f}")


# Alias used by main.py
probe_futures = probe_futures  # main.py alias
