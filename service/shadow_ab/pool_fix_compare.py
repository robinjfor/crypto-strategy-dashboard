"""Dry-run: sleeve sizing and per-symbol book vs exchange, before and after the USD pool.

Before is main at 93baace: the quote pool is E + R2S only.
After is this branch: pool = E + R2S + USDC sleeve + FA ledger, valued 1:1 in USD.
VBO's book is not added. No orders are sent and nothing is revoked.

USDC is counted into the USDT-M sizing equity. No coin is converted or moved.
U1 and FA stay at weight 0, so their books are not flattened; the added dollars
are E and R2S sizing. A per-symbol "target qty" for E and R2S scales the current
book by (orchestrator sizing / mark-to-market ledger), which is the book you get
when the signal weights are unchanged and the 25% band rebalances. R2S positions
were built at the 1464.08 ceiling, not at the full ledger, so that scale is the
ledger view; the band against 1464.08 is reported separately.

    python3 service/shadow_ab/pool_fix_compare.py /path/to/orch-summary.json \
        -o service/shadow_ab/pool_fix_compare.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

import demo_e5v as V  # noqa: E402
import netting  # noqa: E402
import orchestrator as O  # noqa: E402

KEYS = ("E", "R", "U", "F")


def _r(x, n=2):
    return round(float(x), n)


def equities_from_status(status: dict) -> dict:
    """Mark-to-market the same way the orchestrator does, using marks already on the summary."""
    marks = {p["symbol"]: float(p["mark"]) for p in ((status.get("liq_distance") or {}).get("positions") or []) if p.get("mark")}

    def book_eq(ex):
        cash = float((ex or {}).get("cash") or 0.0)
        notion = 0.0
        for s, q in ((ex or {}).get("book") or {}).items():
            if s not in marks:
                raise KeyError(f"no mark for {s}")
            notion += float(q) * marks[s]
        return cash + notion

    u = status.get("u1_live") or {}
    btc = float((u.get("units") or {}).get("BTC") or 0.0) * float(marks.get("BTCUSDC") or 0.0)
    eth = float((u.get("units") or {}).get("ETH") or 0.0) * float(marks.get("ETHUSDC") or 0.0)
    fa = 1000.0
    for s, p in (status.get("demo_fa_positions") or {}).items():
        if not isinstance(p, dict):
            continue
        m = marks.get(s)
        if m is None:
            continue
        fa += float(p.get("spot_qty") or 0) * (m - float(p.get("spot_px") or m))
        fa += float(p.get("perp_qty") or 0) * (float(p.get("perp_px") or m) - m)
    return {"E": book_eq(status.get("demo_e5v_exec")), "R": book_eq(status.get("demo_r2s_exec")),
            "U": float(u.get("cash") or 0.0) + btc + eth, "F": fa}


def sized_targets(pool: float, *, bear: bool = False, hivol: bool = False) -> dict:
    """Weight × pool × hi-vol, then the R2S equity ceiling. Same cap the live plan uses."""
    w, x = O.weights(bear, hivol)
    out = {}
    for k in KEYS:
        raw = _r(pool * w[k] * x)
        cap = O.equity_cap(O.SLOTS[k])
        out[k] = _r(min(raw, cap)) if cap is not None and raw > cap else raw
    return out


def pools(eq: dict, usdt_margin: float | None) -> dict:
    """Before is the withdrawn USD sum. After is USDT margin minus the VBO reserve."""
    before = eq["E"] + eq["R"] + eq["U"] + eq["F"]
    after = 0.0 if usdt_margin is None else max(0.0, float(usdt_margin) - O.VBO_RESERVE_USDT)
    return {"before": before, "after": after}


def symbol_rows(status: dict, eq: dict, before: dict, after: dict) -> list[dict]:
    marks = {p["symbol"]: float(p["mark"]) for p in ((status.get("liq_distance") or {}).get("positions") or [])}
    exch = {}
    for s, row in ((status.get("net_reconcile") or {}).get("symbols") or {}).items():
        exch[s] = float(row.get("exchange") or 0.0)
    for p in (status.get("liq_distance") or {}).get("positions") or []:
        exch.setdefault(p["symbol"], float(p.get("qty") or 0.0))
    books = {
        "E": (status.get("demo_e5v_exec") or {}).get("book") or {},
        "R": (status.get("demo_r2s_exec") or {}).get("book") or {},
        "U": {},
        "F": {},
    }
    u = status.get("u1_live") or {}
    for coin, qty in (u.get("units") or {}).items():
        if float(qty or 0):
            books["U"][f"{coin}{(u.get('quote') or 'USDC').upper()}"] = float(qty)
    rows = []
    for k, book in books.items():
        ledger = eq[k]
        for sym, qty in sorted(book.items()):
            mark = marks.get(sym)
            scale_b = (before[k] / ledger) if ledger else 1.0
            scale_a = (after[k] / ledger) if ledger else 1.0
            # Weight 0 sleeves are not flattened. Scaling by a zero target would look like a close.
            flat_ok = k in ("U", "F")
            rows.append({
                "sleeve": k, "slot": O.SLOTS[k], "symbol": sym,
                "book_qty": qty, "exchange_qty": exch.get(sym),
                "mark": mark,
                "book_notional": None if mark is None else _r(qty * mark, 4),
                "target_qty_before": qty if flat_ok else _r(qty * scale_b, 8),
                "target_qty_after": qty if flat_ok else _r(qty * scale_a, 8),
                "weight0_not_flattened": flat_ok,
            })
    return rows


class _Fut:
    def __init__(self, pos, px):
        self.pos, self.px, self.orders = dict(pos), px, []

    def exchange_info(self):
        return {"symbols": [{"symbol": s, "filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "5"}]} for s in self.px]}

    def position_risk(self):
        return [{"symbol": s, "positionAmt": str(q), "leverage": "2"} for s, q in self.pos.items()]

    def mark_price(self, s):
        return self.px[s]

    def set_margin_type(self, *a, **k):
        return {}

    def set_leverage(self, *a, **k):
        return {}

    def get_order(self, s, c):
        return None

    def new_order(self, **p):
        signed = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
        self.pos[p["symbol"]] = self.pos.get(p["symbol"], 0.0) + signed
        self.orders.append({k: p[k] for k in ("symbol", "side", "quantity", "reduceOnly") if k in p})
        return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": str(self.px[p["symbol"]])}


def eth_order_check(e_qty: float, r_qty: float, price: float = 4000.0) -> dict:
    """U1 weight 0 on the E5 book. S5 must not emit a second ETH close, and the flush is one order."""
    book = {"ETHUSDT": e_qty}
    px = {"ETHUSDT": price, "BTCUSDT": 100000.0}
    tgt = {"w1": [0.0, 0.0], "ws": {"ETHUSDT": 0.0, "BTCUSDT": 0.0, "SOLUSDT": 0.0},
           "dbs": {"ETHUSDT": 0.0, "BTCUSDT": 0.0, "SOLUSDT": 0.0}}
    plan = V.plan_live(tgt, book, px, 5000.0, m=1.0, lt=1.0)
    eth = [o for o in plan if o["symbol"] == "ETHUSDT" and o["kind"] not in ("skip_min", "skip_thr", "hold")]
    deltas = []
    for o in eth:
        mine = float(book.get(o["symbol"], 0.0))
        full = o["kind"] in ("close", "residual_close")
        deltas.append({"sleeve": o.get("sleeve"), "kind": o["kind"], "d": -mine if full else float(o["delta_qty"])})
    exch = e_qty + r_qty
    fut = _Fut({"ETHUSDT": exch}, px)
    st = {"demo_e5v_exec": {"cash": 0.0, "book": dict(book)}}
    n = netting.Netter(fut, tag="cmp")
    for row in deltas:
        n.add(st, V.SLOT, "ETHUSDT", row["d"], price, netting.ledger_cb(st["demo_e5v_exec"], "ETHUSDT"), kind=row["kind"])
    sent = n.flush()
    return {
        "e5_book": _r(e_qty, 8), "r2s_book": _r(r_qty, 8), "exchange_before": _r(exch, 8),
        "sleeves": sorted({row["sleeve"] for row in deltas}),
        "deltas": deltas,
        "orders": [{"symbol": o["symbol"], "side": o["side"], "qty": o["qty"], "reduce_only": o["reduce_only"]} for o in sent],
        "n_orders": len(sent),
        "exchange_after": _r(fut.pos.get("ETHUSDT") or 0.0, 8),
        "e5_book_after": _r(st["demo_e5v_exec"]["book"].get("ETHUSDT", 0.0), 8),
        "flipped": (exch < 0 < fut.pos.get("ETHUSDT", 0.0)) or (exch > 0 > fut.pos.get("ETHUSDT", 0.0)),
    }


def compare(status: dict, *, bear: bool = False, hivol: bool = False, usdt_margin: float | None = None) -> dict:
    O.load({})
    eq = {k: equities_from_status(status)[k] for k in KEYS}
    p = pools(eq, usdt_margin)
    before = sized_targets(p["before"], bear=bear, hivol=hivol)
    after = sized_targets(p["after"], bear=bear, hivol=hivol)
    e_book = (status.get("demo_e5v_exec") or {}).get("book") or {}
    r_book = (status.get("demo_r2s_exec") or {}).get("book") or {}
    built = 1464.08
    r_after = after["R"]
    return {
        "accounting": "usdt_m_margin_minus_vbo",
        "accounting_note": (
            "Multi-assets margin is off, so USDC is not USDT-M margin. "
            "FA's 1000 is the VBO carve and is subtracted once. "
            "The pool is the USDT margin balance minus that 1000. E takes all of it. R2S weight is 0."
        ),
        "usdt_margin": None if usdt_margin is None else _r(usdt_margin, 2),
        "regime": {"bear": bear, "hivol": hivol},
        "weights": {k: O.BASE_W[k] for k in KEYS},
        "equities": {k: _r(eq[k], 4) for k in KEYS},
        "pool_before": _r(p["before"]),
        "pool_after": _r(p["after"]),
        "added": {"usdc": _r(eq["U"]), "fa": _r(eq["F"]), "vbo_added": 0.0},
        "targets_before": before,
        "targets_after": after,
        "symbol_scale_note": (
            "target_qty_before and target_qty_after scale the current book by orchestrator sizing / "
            "mark-to-market ledger, which is the book when signal weights stay put and the 25% band rebalances. "
            "U1 and FA stay at their current qty because weight 0 does not flatten them. "
            "ETHUSDT's next E5 flush, when the U1 sleeve target is flat, is the separate ethusdt block: one reduce-only buy, not a larger short."
        ),
        "r2s_cap": 1464.08,
        "r2s_band_vs_build": {
            "built_at": built,
            "after": r_after,
            "rel": None if r_after == 0 else _r(abs(r_after - built) / abs(r_after), 4),
            "band": 0.25,
            "hold": bool(r_after) and abs(r_after - built) <= 0.25 * abs(r_after),
        },
        "symbols": symbol_rows(status, eq, before, after),
        "ethusdt": eth_order_check(float(e_book.get("ETHUSDT") or 0.0), float(r_book.get("ETHUSDT") or 0.0)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare quote-pool sizing before and after the USD pool")
    ap.add_argument("summary", help="orch-summary latest.json (read-only)")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--usdt-margin", type=float, default=None,
                    help="USDT margin balance (wallet + unrealized). Pool is this minus the VBO 1000.")
    args = ap.parse_args(argv)
    doc = json.loads(Path(args.summary).read_text())
    status = doc.get("status") or doc
    out = compare(status, bear=bool(((status.get("orchestrator") or {}).get("regime") or {}).get("bear")),
                  hivol=bool(((status.get("orchestrator") or {}).get("regime") or {}).get("hivol")),
                  usdt_margin=args.usdt_margin)
    out["as_of"] = (status.get("net_reconcile") or {}).get("at")
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
    e, r = out["targets_after"]["E"], out["targets_after"]["R"]
    print(f"pool {out['pool_before']} -> {out['pool_after']}  E {out['targets_before']['E']} -> {e}  R2S {out['targets_before']['R']} -> {r}")
    print(f"ETH orders {out['ethusdt']['n_orders']} flip {out['ethusdt']['flipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
