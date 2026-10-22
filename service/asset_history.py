"""Daily total-asset series (USDT terms, Taipei day ends) reconstructed from real Binance account history.
Read-only. Method: start from today's Binance balances (spot all assets + USD-M USDT/USDC wallets + open
perps) and undo every ledger event back to the start date (spot fills incl. commission, futures realized
PnL − commission, funding income). Then walk forward and value holdings at each Taipei day end with
Binance 1h spot closes. Internal spot↔futures transfers cancel in the total; external deposits/withdrawals
are not in the ledger and would show as a jump (flagged by `check`)."""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from execution import base_asset

log = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
STABLES = {"USDT", "USDC", "FDUSD"}
START = datetime(2026, 10, 1, tzinfo=TZ)


def _quote(sym: str) -> str:
    b = base_asset(sym)
    return sym[len(b):] if sym.startswith(b) else "USDT"


def events(ledger_doc: dict) -> list[dict]:
    """Each event: time_ms + deltas {('spot'|'fut', asset): amount} + futures position delta."""
    out = []
    for f in (ledger_doc or {}).get("fills") or []:
        sym, q, b = f["symbol"], _quote(f["symbol"]), base_asset(f["symbol"])
        sgn = 1.0 if f["side"] == "BUY" else -1.0
        d = defaultdict(float)
        if f["venue"] == "spot":
            d[("spot", b)] += sgn * f["qty"]
            d[("spot", q)] -= sgn * f["quoteQty"]
            if f.get("commission"):
                d[("spot", f["commissionAsset"] or q)] -= f["commission"]
            out.append({"t": f["time_ms"], "d": d, "pos": None})
        else:
            d[("fut", q)] += float(f.get("realizedPnl") or 0)
            if f.get("commission"):
                d[("fut", f["commissionAsset"] or q)] -= f["commission"]
            out.append({"t": f["time_ms"], "d": d, "pos": (sym, b, sgn * f["qty"], f["price"])})
    for r in (ledger_doc or {}).get("income") or []:
        out.append({"t": r["time_ms"], "d": {("fut", str(r.get("asset") or "USDT").upper()): float(r["income"])}, "pos": None})
    return sorted(out, key=lambda e: e["t"])


def build(ledger_doc: dict, spot_now: dict, fut_wallet_now: dict, fut_pos_now: dict, price_at, now: datetime,
          start: datetime = START) -> dict:
    """spot_now {asset: qty}; fut_wallet_now {USDT: walletBalance, USDC: ...}; fut_pos_now {sym: (base, qty, entry)};
    price_at(asset, dt_utc) -> USDT price or None. Returns {points: [[YYYY-MM-DD, total]], ...}."""
    ev = [e for e in events(ledger_doc) if e["t"] >= int(start.timestamp() * 1000)]
    bal = defaultdict(float)
    for a, v in spot_now.items():
        bal[("spot", a.upper())] += float(v)
    for a, v in fut_wallet_now.items():
        bal[("fut", a.upper())] += float(v)
    pos = {s: [b, float(q), float(px)] for s, (b, q, px) in (fut_pos_now or {}).items()}
    for e in reversed(ev):                       # undo back to `start`
        for k, v in e["d"].items():
            bal[k] -= v
        if e["pos"]:
            s, b, dq, _ = e["pos"]
            p = pos.setdefault(s, [b, 0.0, 0.0])
            p[1] -= dq
    pos = {s: [b, q, 0.0] for s, (b, q, _) in pos.items()}   # entry price unknown at start; perps assumed flat
    flat_at_start = all(abs(q) < 1e-12 for _, q, _ in pos.values())
    points, unpriced, i = [], set(), 0

    def value(at):
        tot = 0.0
        eu = at.astimezone(timezone.utc)
        for (venue, a), v in bal.items():
            if abs(v) < 1e-12:
                continue
            px = 1.0 if a in STABLES else price_at(a, eu)
            if px is None:
                unpriced.add(a)
                continue
            tot += v * px
        for s, (b, q, entry) in pos.items():
            if q:
                px = price_at(b, eu)
                if px is not None:
                    tot += q * (px - entry)
        return round(tot, 2)

    points.append([(start - timedelta(days=1)).strftime("%Y-%m-%d"), value(start)])   # day end before start
    day = start
    while True:
        end = day + timedelta(days=1)
        if end > now:
            break
        tms = int(end.timestamp() * 1000)
        while i < len(ev) and ev[i]["t"] < tms:
            e = ev[i]
            for k, v in e["d"].items():
                bal[k] += v
            if e["pos"]:
                s, b, dq, px = e["pos"]
                p = pos.setdefault(s, [b, 0.0, 0.0])
                nq = p[1] + dq
                if p[1] == 0 or (p[1] > 0) == (dq > 0):
                    p[2] = (p[2] * abs(p[1]) + px * abs(dq)) / max(abs(nq), 1e-12)
                p[1] = 0.0 if abs(nq) < 1e-12 else nq
            i += 1
        points.append([(end - timedelta(days=1)).strftime("%Y-%m-%d"), value(end)])
        day = end
    return {"points": points, "start": start.isoformat(), "unpriced": sorted(unpriced),
            "perps_flat_at_start": flat_at_start, "n_events": len(ev), "built_at": now.astimezone(TZ).isoformat(timespec="seconds"),
            "method": "幣安現況餘額逆推帳本成交、資金費，台北日終以幣安 1 小時收盤估值"}


def kline_price_fn(fetch=None):
    """price_at(asset, dt_utc): close of the Binance spot 1h bar ending at dt (public data API)."""
    cache: dict = {}

    def _fetch(sym, start_ms):
        import requests
        r = requests.get("https://data-api.binance.vision/api/v3/klines",
                         params={"symbol": sym, "interval": "1h", "startTime": start_ms, "limit": 1000}, timeout=30)
        r.raise_for_status()
        return r.json()
    fetch = fetch or _fetch

    def price_at(asset, dt):
        sym = asset.upper() + "USDT"
        if sym not in cache:
            rows, s = {}, int(START.timestamp() * 1000) - 3600_000
            try:
                for _ in range(3):
                    k = fetch(sym, s) or []
                    for x in k:
                        rows[int(x[6]) + 1] = float(x[4])     # close time + 1ms = bar end
                    if len(k) < 1000:
                        break
                    s = int(k[-1][0]) + 3600_000
            except Exception as e:  # noqa: BLE001
                log.warning("asset_history_kline_fail %s err=%s", sym, e)
            cache[sym] = rows
        return cache[sym].get(int(dt.timestamp() * 1000))
    return price_at
