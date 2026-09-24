"""Binance Demo REST client. Never logs API keys/secrets."""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import time
import urllib.parse
from typing import Any

import pandas as pd
import requests

log = logging.getLogger("trader.binance")

DEMO_BASE = os.environ.get("BINANCE_BASE_URL", "https://demo-api.binance.com")
VISION_BASE = os.environ.get("VISION_BASE_URL", "https://data-api.binance.vision")
UA = {"User-Agent": "crypto-strategy-dashboard-trader/1.0"}


class BinanceClient:
    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = DEMO_BASE,
        public_base: str = VISION_BASE,
    ):
        self.api_key = (api_key or os.environ.get("BINANCE_DEMO_API_KEY") or "").strip()
        self.api_secret = (api_secret or os.environ.get("BINANCE_DEMO_API_SECRET") or "").strip()
        self.base_url = base_url.rstrip("/")
        self.public_base = public_base.rstrip("/")
        self._filters: dict[str, dict] = {}

    def _public(self, path: str, params: dict | None = None, base: str | None = None) -> Any:
        url = f"{(base or self.public_base)}{path}"
        r = requests.get(url, params=params or {}, headers=UA, timeout=30)
        if r.status_code == 451:
            raise RuntimeError(f"HTTP 451 geo-blocked from {base or self.public_base} {path}")
        r.raise_for_status()
        return r.json()

    def _signed(self, method: str, path: str, params: dict | None = None) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Missing BINANCE_DEMO_API_KEY/SECRET")
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params.setdefault("recvWindow", 5000)
        qs = urllib.parse.urlencode(params, doseq=True)
        sig = hmac.new(self.api_secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
        url = f"{self.base_url}{path}?{qs}&signature={sig}"
        headers = {**UA, "X-MBX-APIKEY": self.api_key}
        r = requests.request(method, url, headers=headers, timeout=30)
        if r.status_code == 451:
            raise RuntimeError(f"HTTP 451 geo-blocked from demo-api {path}")
        if not r.ok:
            raise RuntimeError(f"Binance {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def ping(self) -> dict:
        return self._public("/api/v3/ping", base=self.base_url)

    def server_time(self) -> dict:
        return self._public("/api/v3/time", base=self.base_url)

    def account(self) -> dict:
        return self._signed("GET", "/api/v3/account")

    def my_trades(self, symbol: str, limit: int = 50) -> list:
        return self._signed("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": limit})

    def open_orders(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self._signed("GET", "/api/v3/openOrders", params)

    def exchange_info(self, symbol: str) -> dict:
        return self._public("/api/v3/exchangeInfo", {"symbol": symbol}, base=self.base_url)

    def load_filters(self, symbol: str) -> dict:
        if symbol in self._filters:
            return self._filters[symbol]
        info = self.exchange_info(symbol)
        f: dict[str, Any] = {"stepSize": 0.001, "minQty": 0.0, "minNotional": 5.0, "tickSize": 0.0001}
        for s in info.get("symbols") or []:
            if s.get("symbol") != symbol:
                continue
            for filt in s.get("filters") or []:
                t = filt.get("filterType")
                if t == "LOT_SIZE":
                    f["stepSize"] = float(filt["stepSize"])
                    f["minQty"] = float(filt["minQty"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    f["minNotional"] = float(filt.get("minNotional") or filt.get("notional") or 5)
                elif t == "PRICE_FILTER":
                    f["tickSize"] = float(filt["tickSize"])
            break
        self._filters[symbol] = f
        return f

    @staticmethod
    def round_step(qty: float, step: float) -> float:
        if step <= 0:
            return qty
        precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        floored = math.floor(qty / step) * step
        return float(f"{floored:.{precision}f}")

    def round_qty(self, symbol: str, qty: float, price: float) -> float:
        f = self.load_filters(symbol)
        q = self.round_step(qty, f["stepSize"])
        if q < f["minQty"] or q * price < f["minNotional"]:
            return 0.0
        return q

    def market_buy(self, symbol: str, quote_usdt: float, client_order_id: str) -> dict:
        return self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{quote_usdt:.2f}",
                "newClientOrderId": client_order_id[:36],
            },
        )

    def market_sell(self, symbol: str, qty: float, client_order_id: str) -> dict:
        f = self.load_filters(symbol)
        q = self.round_step(qty, f["stepSize"])
        return self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": f"{q}",
                "newClientOrderId": client_order_id[:36],
            },
        )

    def fetch_klines(
        self, symbol: str, interval: str, limit: int = 500, use_vision: bool = True
    ) -> pd.DataFrame:
        base = (self.public_base if use_vision else self.base_url)
        raw = self._public(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
            base=base,
        )
        cols = [
            "open_time", "Open", "High", "Low", "Close", "Volume",
            "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        return df.set_index("open_time").sort_index()


    def ticker_price(self, symbol: str) -> float:
        data = self._public("/api/v3/ticker/price", {"symbol": symbol}, base=self.base_url)
        return float(data["price"])

    def round_price(self, symbol: str, price: float) -> float:
        f = self.load_filters(symbol)
        step = float(f.get("tickSize") or 0.01)
        if step <= 0:
            return price
        precision = max(0, int(round(-math.log10(step)))) if step < 1 else 0
        floored = math.floor(price / step) * step
        return float(f"{floored:.{precision}f}")

    def cancel_order(self, symbol: str, order_id: int | None = None, client_order_id: str | None = None) -> dict:
        params: dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = int(order_id)
        if client_order_id:
            params["origClientOrderId"] = client_order_id[:36]
        return self._signed("DELETE", "/api/v3/order", params)

    def cancel_open_orders(self, symbol: str) -> Any:
        return self._signed("DELETE", "/api/v3/openOrders", {"symbol": symbol})

    def stop_loss_limit(
        self,
        symbol: str,
        qty: float,
        stop_price: float,
        limit_price: float | None = None,
        client_order_id: str = "",
    ) -> dict:
        """Hard exchange stop (STOP_LOSS_LIMIT). Spot only."""
        f = self.load_filters(symbol)
        q = self.round_step(qty, f["stepSize"])
        sp = self.round_price(symbol, stop_price)
        lp = self.round_price(symbol, limit_price if limit_price is not None else stop_price * 0.995)
        params = {
            "symbol": symbol,
            "side": "SELL",
            "type": "STOP_LOSS_LIMIT",
            "timeInForce": "GTC",
            "quantity": f"{q}",
            "stopPrice": f"{sp}",
            "price": f"{lp}",
        }
        if client_order_id:
            params["newClientOrderId"] = client_order_id[:36]
        return self._signed("POST", "/api/v3/order", params)

    def nonzero_balances(self, account: dict | None = None) -> list[dict]:
        acct = account or self.account()
        out = []
        for b in acct.get("balances") or []:
            free = float(b.get("free") or 0)
            locked = float(b.get("locked") or 0)
            if free + locked > 0:
                out.append({"asset": b["asset"], "free": free, "locked": locked})
        return out
