"""Net-position aggregation for the Demo strategy slots (Robin 2026-10-24).

Every Demo slot (R2S, E5, FA perp leg) keeps a VIRTUAL ledger of its own positions (book qty + cash); the one-way
Demo account holds only the per-symbol NET of all ledgers. A run's slot decisions are submitted as intents
(slot, symbol, signed delta); per symbol the netter
  * crosses opposite intents internally (no order, no fee) at the fill price (mark if nothing is sent),
  * sends ONE order for the net delta: the part that reduces the exchange position is reduceOnly (no min notional),
    the part that opens/increases must pass the exchange min notional + stepSize (checked on the NET, not per slot),
  * allocates the filled qty and fee pro rata to the net-side slots' deltas; callbacks update each virtual ledger.
No more "held by another slot" skips: sum(virtual ledgers) == exchange positionAmt per symbol (checked in reconcile).
U1 (BTCUSDC/ETHUSDC) shares no symbol with these slots and is not netted (its own sleeve)."""
from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from datetime import datetime, timezone

import tradfi
from guards import log_event

log = logging.getLogger("netting")
FEE = 0.0005
LEVERAGE = 3
LEV_BY_SLOT = {"demo_e5_min5_x175_t05top20": 2}   # E5v: isolated 2x (2026-10-28); symbol leverage = min over slots trading it
EPS = 1e-9

# slot id -> (state key, book getter). FA's perp leg is short: virtual qty = -perp_qty.
SLOTS = {
    "demo_r2s_top10_3k": "demo_r2s_exec",
    "demo_e5_min5_x175": "demo_e5_exec",            # paused since 2026-10-26, ledger moved to the t05top20 slot
    "demo_e5_min5_x175_t05top20": "demo_e5v_exec",
    "demo_funding_arb_1x": "demo_fa_positions",
}


def slot_books(state: dict) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for slot, key in SLOTS.items():
        v = state.get(key) or {}
        if key == "demo_fa_positions":
            b = {s: -abs(float(p.get("perp_qty") or 0)) for s, p in v.items() if isinstance(p, dict) and float(p.get("perp_qty") or 0)}
        else:
            b = {s: float(q) for s, q in (v.get("book") or {}).items() if float(q or 0)}
        if b:
            out[slot] = b
    return out


def virtual_net(state: dict) -> dict[str, float]:
    net: dict[str, float] = defaultdict(float)
    for b in slot_books(state).values():
        for s, q in b.items():
            net[s] += q
    return {s: q for s, q in net.items() if abs(q) > EPS}


def holders(state: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for slot, b in slot_books(state).items():
        for s in b:
            out[s].append(slot)
    return dict(out)


def funding_share(fut, state: dict, slot: str, book: dict, since: int, now_ms: int) -> float:
    """Funding income on the netted symbol, attributed pro rata to this slot's signed share of the virtual net."""
    net = virtual_net(state); tot = 0.0
    for s, q in book.items():
        if not q:
            continue
        n = net.get(s, q)
        if abs(n) < EPS or q * n <= 0:
            continue  # this slot is on the crossed side: exchange funding belongs to the net side
        try:
            inc = sum(float(r.get("income") or 0) for r in fut.income(s, "FUNDING_FEE", since, now_ms))
        except Exception:  # noqa: BLE001
            continue
        tot += inc * q / n
    return tot


def _filters(info: dict, sym: str) -> tuple[float, float]:
    step = mn = 0.0
    for s in info.get("symbols") or []:
        if s.get("symbol") == sym:
            for f in s.get("filters") or []:
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize") or 0)
                elif f.get("filterType") == "MIN_NOTIONAL":
                    mn = float(f.get("notional") or f.get("minNotional") or 0)
    return step, mn


def floor_step(q: float, step: float) -> float:
    if step <= 0:
        return abs(q)
    n = math.floor(abs(q) / step + 1e-9); dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return float(f"{n * step:.{dec}f}")


class Netter:
    def __init__(self, fut, info: dict | None = None, tag: str | None = None):
        self.fut = fut
        self.info = info if info is not None else fut.exchange_info()
        self.tag = tag or datetime.now(timezone.utc).strftime("%y%m%d%H%M")
        self.intents: dict[str, list[dict]] = defaultdict(list)
        self.after: list = []
        self.orders: list[dict] = []
        self.n = 0

    def add(self, state: dict, slot: str, sym: str, delta: float, ref_px: float, cb, kind: str = "") -> None:
        """cb(filled_signed_qty, px, fee) updates the slot's virtual ledger (called once, possibly with 0)."""
        if abs(delta) > EPS:
            self.intents[sym].append({"state": state, "slot": slot, "d": float(delta), "px": float(ref_px or 0), "cb": cb, "kind": kind})

    def lev_for(self, sym: str) -> int:
        return min([LEV_BY_SLOT.get(i["slot"], LEVERAGE) for i in self.intents.get(sym, [])] or [LEVERAGE])

    def drop(self, slot: str) -> None:
        """forget a slot's intents (its execute failed; its scratch state is discarded)."""
        for sym in list(self.intents):
            self.intents[sym] = [i for i in self.intents[sym] if i["slot"] != slot]
            if not self.intents[sym]:
                del self.intents[sym]

    def _send(self, st, sym, side, qty, ro) -> tuple[float, float]:
        coid = f"net-{sym[:14]}-{self.tag}-{self.n}"[:36]; self.n += 1
        try:
            if not ro:
                self.fut.set_margin_type(sym, "ISOLATED"); self.fut.set_leverage(sym, self.lev_for(sym))
            kw = {"reduceOnly": "true"} if ro else {}
            o = self.fut.new_order(symbol=sym, side=side, type="MARKET", quantity=qty, newClientOrderId=coid, **kw)
            for _ in range(6):
                if o.get("status") == "FILLED" and float(o.get("executedQty") or 0) > 0:
                    break
                time.sleep(0.5); o = self.fut.get_order(sym, coid) or o
            fq, px = float(o.get("executedQty") or 0), float(o.get("avgPrice") or 0)
            self.orders.append({"symbol": sym, "side": side, "qty": fq, "px": px, "reduce_only": ro, "coid": coid})
            log_event(st, "net_order", None, symbol=sym, side=side, qty=fq, px=px, reduce_only=ro, coid=coid)
            return fq, px
        except Exception as e:  # noqa: BLE001
            if tradfi.note_error(st, sym, str(e)):
                log_event(st, "tradfi_blocked", None, symbol=sym, error=str(e)[:120])
            log_event(st, "net_order_error", None, symbol=sym, side=side, qty=qty, error=str(e)[:120])
            return 0.0, 0.0

    def flush(self) -> list[dict]:
        if not self.intents:
            for f in self.after:
                f()
            self.after = []
            return self.orders
        pos = {r["symbol"]: float(r.get("positionAmt") or 0) for r in self.fut.position_risk()}
        nets = {sym: sum(i["d"] for i in its) for sym, its in self.intents.items()}
        # reducing the exchange position first (frees margin), then opens
        for sym in sorted(self.intents, key=lambda x: not (pos.get(x, 0.0) * nets[x] < 0)):
            its = self.intents[sym]
            st = its[0]["state"]
            step, mn = _filters(self.info, sym)
            buy = sum(i["d"] for i in its if i["d"] > 0); sell = -sum(i["d"] for i in its if i["d"] < 0)
            net = buy - sell
            p0 = pos.get(sym, 0.0); sgn = 1.0 if net > 0 else -1.0
            mag = floor_step(net, step)
            close = min(mag, abs(p0)) if p0 * net < 0 else 0.0
            opn = floor_step(mag - close, step)
            ref = next((i["px"] for i in its if i["px"] > 0), 0.0)
            if not ref:
                try:
                    ref = float(self.fut.mark_price(sym))
                except Exception:  # noqa: BLE001
                    ref = 0.0
            if opn > 0 and tradfi.is_tradfi(sym, self.info, st):
                log_event(st, "net_skip_tradfi", None, symbol=sym, qty=opn); opn = 0.0
            if opn > 0 and mn and opn * ref < mn:
                log_event(st, "net_skip_min", None, symbol=sym, net_qty=sgn * opn, notional=round(opn * ref, 4), min=mn,
                          slots={i["slot"]: i["d"] for i in its})
                opn = 0.0
            filled = 0.0; cost = 0.0
            side = "BUY" if sgn > 0 else "SELL"
            for q, ro in ((close, True), (opn, False)):
                if q > 0:
                    fq, px = self._send(st, sym, side, q, ro)
                    filled += fq; cost += fq * px
                    if fq < q - EPS:
                        break
            px = cost / filled if filled > 0 else ref
            fee = filled * px * FEE
            cross = min(buy, sell); tot_net = max(buy, sell)
            frac = (cross + filled) / tot_net if tot_net > 0 else 0.0
            for i in its:
                net_side = (i["d"] > 0) == (net > 0) and abs(net) > EPS
                if net_side:
                    q = i["d"] if abs(frac - 1) < 1e-12 else i["d"] * frac
                    f = fee * i["d"] / (sgn * tot_net)
                else:
                    q, f = i["d"], 0.0
                try:
                    i["cb"](q, px, f)
                except Exception as e:  # noqa: BLE001
                    log.error("net cb %s %s %s", i["slot"], sym, e)
            if len(its) > 1:
                log_event(st, "net_alloc", None, symbol=sym, slots={i["slot"]: i["d"] for i in its}, net=net,
                          filled=sgn * filled, crossed=cross, px=px)
        self.intents = defaultdict(list)
        for f in self.after:
            f()
        self.after = []
        return self.orders


def ledger_cb(ex: dict, sym: str, book_key: str = "book", on_fill=None):
    """standard virtual-ledger update for slots with {cash, book}; on_fill(q, px, fee) for the slot's own log."""
    def cb(q: float, px: float, fee: float):
        book = ex.setdefault(book_key, {})
        nb = float(book.get(sym, 0.0)) + q
        ex["cash"] = float(ex.get("cash") or 0) - q * px - fee
        if abs(nb) < 1e-10:
            book.pop(sym, None)
        else:
            book[sym] = nb
        if on_fill:
            on_fill(q, px, fee)
    return cb


def net_reconcile(state: dict, exch: dict[str, float], quote: str = "USDT") -> dict:
    """sum of virtual ledgers vs exchange positionAmt per symbol (Demo slots only; legacy positions/U1 added by caller)."""
    books = slot_books(state); vn = virtual_net(state)
    syms = sorted({s for s in vn if s.endswith(quote)} | {s for s in exch if s.endswith(quote) and s in vn})
    rows = {}
    for s in syms:
        v = vn.get(s, 0.0); x = float(exch.get(s, 0.0))
        rows[s] = {"slots": {k: b[s] for k, b in books.items() if s in b}, "virtual_sum": v, "exchange": x,
                   "diff": x - v, "ok": abs(x - v) <= 1e-8 * max(1.0, abs(x))}
    u1 = {"BTCUSDC", "ETHUSDC"}   # U1 sleeve (own ledger, reconciled separately)
    unowned = {s: float(x) for s, x in exch.items() if s.endswith(quote) and s not in vn and s not in u1 and abs(float(x or 0)) > 0}
    return {"ok": all(r["ok"] for r in rows.values()), "symbols": rows,
            "unowned": unowned, "unowned_alert": bool(unowned)}   # alert only: exchange legs no slot ledger owns
