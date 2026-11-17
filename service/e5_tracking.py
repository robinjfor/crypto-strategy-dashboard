"""E5 target-vs-actual for the reviewer. Read-only.

Compares the daily E5 target plan (weights and the qty those weights size) with the
virtual book reconcile keeps on the E5 ledger. It does not call Binance, place an
order, or change leverage. Prices come from the plan snapshot, the exec log, or the
marks already on the status/reconcile payload.

Reasons are attached only when the executor recorded one, or when the same planner
the executor uses (demo_e5v.plan_live / demo_e5.plan_live) classifies the gap:
skip_min, deadband (skip_thr or inside the rebalance band), residual_close, unfilled.
"""
from __future__ import annotations

import math
from typing import Any

import demo_e5
import demo_e5v

E5_SLOT = demo_e5v.SLOT
E5_KEY = demo_e5v.STATE_KEY
OLD_SLOT = demo_e5.SLOT
OLD_KEY = "demo_e5_exec"
E5_CODES = {"E5", "e5", E5_SLOT, OLD_SLOT, E5_KEY, OLD_KEY}

_REASON_RANK = {"unfilled": 1, "residual_close": 2, "deadband": 3, "skip_min": 4}
_QTY_TOL = 1e-8


def _f(value) -> float | None:
    try:
        if value is None or value == "":
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _trunc(qty: float, step: float) -> float:
    if step and step > 0:
        return math.trunc(qty / step) * step
    return qty


def _round(value, places: int):
    if value is None:
        return None
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def ledger(state: dict | None) -> tuple[str, str, dict, Any]:
    """Active E5 ledger. The migrated t05top20 book wins when it has been written."""
    state = state or {}
    variant = state.get(E5_KEY) if isinstance(state.get(E5_KEY), dict) else None
    original = state.get(OLD_KEY) if isinstance(state.get(OLD_KEY), dict) else None

    def used(ex: dict | None) -> bool:
        if not ex:
            return False
        return bool(ex.get("book") or ex.get("last") or ex.get("sizing_equity") or ex.get("history"))

    if used(variant) or (variant is not None and not used(original)):
        return E5_KEY, E5_SLOT, variant or {}, demo_e5v
    if used(original):
        return OLD_KEY, OLD_SLOT, original or {}, demo_e5
    return E5_KEY, E5_SLOT, {}, demo_e5v


def _put_price(out: dict, symbol, price) -> None:
    px = _f(price)
    sym = str(symbol or "").strip()
    if px is None or px <= 0 or not sym:
        return
    # Status rows sometimes label the symbol with a side suffix. Keep the raw perp name.
    base = sym.split()[0]
    out[base] = px


def status_prices(status: dict | None, state: dict | None) -> dict[str, float]:
    """Marks already on the status or reconcile payload. No exchange call."""
    out: dict[str, float] = {}
    status = status or {}
    for pos in status.get("positions") or status.get("open_positions") or []:
        if isinstance(pos, dict):
            _put_price(out, pos.get("raw_symbol") or pos.get("symbol"), pos.get("mark") or pos.get("markPrice"))
    books = status.get("books") or {}
    if isinstance(books, dict):
        for book in books.values():
            fut = (book or {}).get("futures") if isinstance(book, dict) else None
            for pos in (fut or {}).get("positions") or []:
                if isinstance(pos, dict):
                    _put_price(out, pos.get("symbol"), pos.get("mark") or pos.get("markPrice"))
    rec = (state or {}).get("reconcile") if isinstance((state or {}).get("reconcile"), dict) else {}
    wallets = rec.get("wallets") if isinstance(rec, dict) else None
    if isinstance(wallets, dict):
        for wallet in wallets.values():
            if not isinstance(wallet, dict):
                continue
            for pos in wallet.get("positions") or []:
                if isinstance(pos, dict):
                    _put_price(out, pos.get("symbol"), pos.get("mark") or pos.get("markPrice") or pos.get("entryPrice"))
    return out


def _exec_prices(ex: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    last = ex.get("last") if isinstance(ex.get("last"), dict) else {}
    for order in last.get("sent") or []:
        if isinstance(order, dict):
            _put_price(out, order.get("symbol"), order.get("px") or order.get("price"))
    return out


def _snapshot_prices(snap: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    raw = snap.get("prices") or snap.get("px") or {}
    if isinstance(raw, dict):
        for sym, px in raw.items():
            _put_price(out, sym, px)
    return out


def _bar(snap: dict) -> str:
    return str(snap.get("decided_bar") or snap.get("bar") or snap.get("day") or "")


def _plans(state: dict, latest: dict | None) -> list[dict]:
    rows: list[dict] = []
    extra = state.get("e5_targets_history")
    if isinstance(extra, list):
        rows.extend(x for x in extra if isinstance(x, dict))
    for key in (E5_KEY, OLD_KEY):
        ex = state.get(key)
        if isinstance(ex, dict) and isinstance(ex.get("target_history"), list):
            rows.extend(x for x in ex["target_history"] if isinstance(x, dict))
    if isinstance(latest, dict) and (latest.get("w1") is not None or latest.get("ws")):
        rows.append(latest)
    by: dict[str, dict] = {}
    order: list[str] = []
    for snap in rows:
        bar = _bar(snap) or f"#{len(order)}"
        if bar not in by:
            order.append(bar)
        by[bar] = snap
    return [by[b] for b in order]


def _virtual_capital(snap: dict, ex: dict) -> float | None:
    """The ledger's own capital figure (often the virtual 5000), not the orchestrator target."""
    for key in ("sizing_capital", "equity", "K"):
        found = _f(snap.get(key))
        if found is not None:
            return found
    sized = _f(ex.get("sizing_equity"))
    if sized is not None:
        return sized
    last = ex.get("last") if isinstance(ex.get("last"), dict) else {}
    return _f(last.get("equity"))


def _sizing(snap: dict, ex: dict, state: dict, slot: str, latest: bool) -> tuple[float | None, float | None, str]:
    """Parity capital is the orchestrator target on the latest day when that target exists.

    The virtual book (5000, or whatever the snapshot stored as equity/K) is returned beside it
    and is not used to size the comparison.
    """
    virtual = _virtual_capital(snap, ex if latest else {})
    target = None
    if latest:
        orch = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else {}
        target = _f((orch.get("targets") or {}).get(slot))
    if target is not None:
        return target, virtual, "orchestrator_target"
    if virtual is not None:
        return virtual, virtual, "virtual"
    if not latest:
        return None, None, "missing"
    return None, None, "missing"


def _qty_map(raw) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        qty = _f(value)
        if qty is None:
            continue
        out[str(key)] = qty
    return out


def _book_for(snap: dict, ex: dict, latest: bool) -> tuple[dict | None, str]:
    if isinstance(snap.get("book"), dict):
        return _qty_map(snap["book"]), "snapshot"
    if latest:
        return _qty_map(ex.get("book") if isinstance(ex.get("book"), dict) else {}), "e5_exec"
    return None, "not_retained"


def _lt_m(snap: dict, ex: dict) -> tuple[float, float]:
    m = _f(snap.get("m"))
    if m is None:
        last = ex.get("last") if isinstance(ex.get("last"), dict) else {}
        m = _f(last.get("m"))
    if m is None:
        m = 1.0
    rw = snap.get("rw")
    lt = None
    if isinstance(rw, (list, tuple)) and rw:
        try:
            import numpy as np
            lt = float(demo_e5.plan_lt(np.array(rw, dtype=float), m))
        except (TypeError, ValueError):
            lt = None
    if lt is None:
        last = ex.get("last") if isinstance(ex.get("last"), dict) else {}
        lt = _f(snap.get("lt"))
        if lt is None:
            lt = _f(last.get("lt"))
    if lt is None:
        lt = 1.0
    return m, lt


def _u1_rows(snap: dict, prices: dict, eu: float, m: float, lt: float) -> dict[str, dict]:
    w1 = snap.get("w1")
    if not isinstance(w1, (list, tuple)):
        return {}
    out = {}
    steps = snap.get("step") if isinstance(snap.get("step"), dict) else {}
    for j, sym in enumerate(demo_e5.U1_SYMS):
        raw = _f(w1[j]) if j < len(w1) else 0.0
        raw = 0.0 if raw is None else raw
        wt = raw * lt if m > 0 else 0.0
        px = prices.get(sym)
        qty = None
        if px:
            qty = _trunc(wt * eu / px, _f(steps.get(sym)) or 0.0) if wt else 0.0
        out[sym] = {
            "sleeve": "u1",
            "signal_weight": raw,
            "target_weight": wt,
            "deadband": demo_e5.BAND * abs(wt),
            "target_qty": qty,
            "price": px,
        }
    return out


def _s5_rows(mod, snap: dict, book: dict, prices: dict, es: float, m: float) -> dict[str, dict]:
    tws = snap.get("ws") if isinstance(snap.get("ws"), dict) else {}
    held = book or {}
    allw = {s: float(v or 0) for s, v in tws.items() if s not in demo_e5.U1_SYMS}
    syms = demo_e5.s5_symbols(allw, held)
    if not syms:
        return {}
    import numpy as np
    w_all = np.array([allw.get(s, 0.0) for s in syms], dtype=float)
    dbs = np.array([float((snap.get("dbs") or {}).get(s, 0.0) or 0.0) for s in syms], dtype=float)
    if mod is demo_e5v:
        kept, scale = demo_e5v.topn_weights(w_all)
        dbm = demo_e5v.DBM
    else:
        kept, scale = w_all, 1.0
        dbm = 1.0
    lv = float(snap.get("lv") or demo_e5.LV)
    steps = snap.get("step") if isinstance(snap.get("step"), dict) else {}
    out = {}
    for i, sym in enumerate(syms):
        tw = float(kept[i]) * float(scale) * lv * m
        db = float(dbs[i]) * float(scale) * dbm * lv * m
        px = prices.get(sym)
        qty = None
        if px:
            qty = _trunc(tw * es / px, _f(steps.get(sym)) or 0.0) if tw else 0.0
        out[sym] = {
            "sleeve": "s5",
            "signal_weight": float(w_all[i]),
            "target_weight": tw,
            "deadband": db,
            "target_qty": qty,
            "price": px,
        }
    return out


def _parse_recorded(text: str) -> str | None:
    low = text.lower()
    if "not filled" in low or "unfilled" in low or "below step" in low:
        return "unfilled"
    if "residual_close" in low or "residual close" in low:
        return "residual_close"
    if "skip_min" in low or "skip min" in low:
        return "skip_min"
    if "deadband" in low or "skip_thr" in low:
        return "deadband"
    return None


def _remember(out: dict[str, str], symbol: str, reason: str | None) -> None:
    if not symbol or reason not in _REASON_RANK:
        return
    prev = out.get(symbol)
    if prev is None or _REASON_RANK[reason] >= _REASON_RANK[prev]:
        out[symbol] = reason


def recorded_reasons(snap: dict, ex: dict, bar: str) -> dict[str, str]:
    """Reasons the exec log or the plan snapshot already named, for this bar."""
    out: dict[str, str] = {}
    blobs: list[dict] = []
    if isinstance(snap.get("last"), dict):
        blobs.append(snap["last"])
    for key in ("orders", "plan", "sent"):
        if isinstance(snap.get(key), list):
            blobs.append({key: snap[key], "bar": bar})
    last = ex.get("last") if isinstance(ex.get("last"), dict) else {}
    if last and (not bar or str(last.get("bar") or "") in ("", bar)):
        blobs.append(last)
    for blob in blobs:
        for order in list(blob.get("sent") or []) + list(blob.get("orders") or []) + list(blob.get("plan") or []):
            if not isinstance(order, dict):
                continue
            kind = str(order.get("kind") or "")
            reason = {"skip_min": "skip_min", "skip_thr": "deadband", "residual_close": "residual_close",
                      "unfilled": "unfilled"}.get(kind)
            _remember(out, str(order.get("symbol") or ""), reason)
        for line in blob.get("skipped") or []:
            text = str(line)
            parts = text.split()
            if not parts:
                continue
            _remember(out, parts[0], _parse_recorded(text))
    return out


def _planner_reasons(mod, snap: dict, book: dict, prices: dict, equity: float, m: float, lt: float) -> dict[str, str]:
    if snap.get("w1") is None or equity is None:
        return {}
    try:
        orders = mod.plan_live(
            snap, book, prices, float(equity), m=m, lt=lt,
            mins=snap.get("min_notional") if isinstance(snap.get("min_notional"), dict) else None,
            steps=snap.get("step") if isinstance(snap.get("step"), dict) else None,
        )
    except Exception:
        return {}
    out: dict[str, str] = {}
    for order in orders or []:
        if not isinstance(order, dict):
            continue
        kind = str(order.get("kind") or "")
        reason = {"skip_min": "skip_min", "skip_thr": "deadband", "residual_close": "residual_close",
                  "order": "unfilled", "close": "unfilled", "rescale": "unfilled"}.get(kind)
        _remember(out, str(order.get("symbol") or ""), reason)
    return out


def _inside_band(row: dict, actual_qty: float) -> bool:
    px = row.get("price")
    tw = row.get("target_weight")
    if px is None or tw is None:
        return False
    sleeve_eq = row.get("sleeve_equity") or 0.0
    if sleeve_eq <= 0:
        return False
    cw = actual_qty * px / sleeve_eq
    db = row.get("deadband") or 0.0
    if tw == 0 and cw != 0:
        return False
    if cw == 0 or (tw > 0) != (cw > 0):
        return False
    return abs(tw - cw) <= db + 1e-12


def _symbol_row(sym: str, spec: dict, actual: float | None, reason: str | None, sleeve_eq: float,
                equity: float) -> dict:
    px = spec.get("price")
    target_qty = spec.get("target_qty")
    actual_qty = None if actual is None else float(actual)
    qty_diff = None
    if target_qty is not None and actual_qty is not None:
        qty_diff = actual_qty - target_qty
        if abs(qty_diff) <= _QTY_TOL * max(1.0, abs(target_qty)):
            qty_diff = 0.0
            reason = None
    target_notional = None if target_qty is None or not px else target_qty * px
    actual_notional = None if actual_qty is None or not px else actual_qty * px
    notional_diff = None
    if target_notional is not None and actual_notional is not None:
        notional_diff = actual_notional - target_notional
    target_w = None if target_notional is None or equity <= 0 else target_notional / equity
    actual_w = None if actual_notional is None or equity <= 0 else actual_notional / equity
    if qty_diff not in (None, 0.0) and _inside_band({**spec, "sleeve_equity": sleeve_eq}, actual_qty or 0.0):
        reason = "deadband"
    return {
        "symbol": sym,
        "sleeve": spec.get("sleeve"),
        "target_weight": _round(spec.get("target_weight"), 6),
        "signal_weight": _round(spec.get("signal_weight"), 6),
        "target_qty": _round(target_qty, 8),
        "actual_qty": _round(actual_qty, 8),
        "qty_diff": _round(qty_diff, 8),
        "price": _round(px, 8),
        "target_notional": _round(target_notional, 4),
        "actual_notional": _round(actual_notional, 4),
        "notional_diff": _round(notional_diff, 4),
        "target_account_weight": _round(target_w, 6),
        "actual_account_weight": _round(actual_w, 6),
        "reason": reason,
    }


def summarize_rows(rows: list[dict]) -> dict:
    errs = []
    matched = 0.0
    target_gross = 0.0
    actual_same = 0.0
    counts = {k: 0 for k in _REASON_RANK}
    priced = 0
    for row in rows:
        reason = row.get("reason")
        if reason in counts:
            counts[reason] += 1
        tw = row.get("target_account_weight")
        aw = row.get("actual_account_weight")
        if tw is None or aw is None:
            continue
        priced += 1
        errs.append((float(aw) - float(tw)) ** 2)
        tn = float(row.get("target_notional") or 0.0)
        an = float(row.get("actual_notional") or 0.0)
        target_gross += abs(tn)
        same = tn != 0 and an != 0 and (tn > 0) == (an > 0)
        if same:
            matched += min(abs(an), abs(tn))
            actual_same += abs(an)
    te = math.sqrt(sum(errs) / len(errs)) if errs else None
    pct = (matched / target_gross) if target_gross > 0 else None
    return {
        "symbols": len(rows),
        "priced_symbols": priced,
        "tracking_error": _round(te, 6),
        "tracking_error_note": "帳戶權重（名義／編排資金）的均方根誤差。只計有價格的幣。",
        "target_notional": _round(target_gross, 4),
        "actual_notional_same_sign": _round(actual_same, 4),
        "matched_notional": _round(matched, 4),
        "pct_target_notional_achieved": _round(pct, 6),
        "reasons": counts,
    }


def _day(snap: dict, ex: dict, state: dict, slot: str, mod, prices: dict, latest: bool) -> dict:
    bar = _bar(snap)
    equity, virtual_capital, capital_basis_label = _sizing(snap, ex, state, slot, latest)
    book, book_source = _book_for(snap, ex, latest)
    m, lt = _lt_m(snap, ex if latest else {})
    eu = es = None
    specs: dict[str, dict] = {}
    if equity is not None:
        w_u1 = float(snap.get("w_u1") or demo_e5.W_U1)
        eu = w_u1 * equity
        es = (1.0 - w_u1) * equity
        held = book or {}
        specs.update(_u1_rows(snap, prices, eu, m, lt))
        specs.update(_s5_rows(mod, snap, held, prices, es, m))
    reasons: dict[str, str] = {}
    if equity is not None and book is not None:
        reasons.update(_planner_reasons(mod, snap, book, prices, equity, m, lt))
    for sym, reason in recorded_reasons(snap, ex if latest else {}, bar).items():
        reasons.setdefault(sym, reason)
    rows = []
    for sym in sorted(specs):
        spec = specs[sym]
        sleeve_eq = eu if spec.get("sleeve") == "u1" else es
        actual = None if book is None else float(book.get(sym) or 0.0)
        if book is not None and sym not in book and actual == 0.0 and not spec.get("target_weight"):
            # A zero target with no position is not a diff. U1's two names still show.
            if spec.get("sleeve") != "u1":
                continue
        reason = reasons.get(sym)
        rows.append(_symbol_row(sym, spec, actual, reason, sleeve_eq or 0.0, equity or 0.0))
    if book is not None:
        for sym, qty in sorted(book.items()):
            if sym in specs or abs(float(qty or 0)) <= _QTY_TOL:
                continue
            rows.append(_symbol_row(sym, {"sleeve": "s5" if sym not in demo_e5.U1_SYMS else "u1",
                                           "target_weight": 0.0, "signal_weight": 0.0, "target_qty": 0.0,
                                           "price": prices.get(sym), "deadband": 0.0},
                                    float(qty), reasons.get(sym), es or eu or 0.0, equity or 0.0))
    unpriced = []
    for row in rows:
        signal = row.get("signal_weight") or 0.0
        actual = row.get("actual_qty") or 0.0
        if row.get("price") is not None or abs(float(signal)) <= _QTY_TOL or abs(float(actual)) > _QTY_TOL:
            continue
        cause = price_cause(row["symbol"], state, snap, ex if latest else {})
        row["price_cause"] = cause
        unpriced.append({"symbol": row["symbol"], "cause": cause, "signal_weight": row.get("signal_weight")})
    return {
        "bar": bar or None,
        "latest": latest,
        "sizing_capital": _round(equity, 4),
        "virtual_capital": _round(virtual_capital, 4),
        "capital_basis_label": capital_basis_label,
        "parity_capital_source": capital_basis_label,
        "u1_equity": _round(eu, 4),
        "s5_equity": _round(es, 4),
        "m": _round(m, 6),
        "lt": _round(lt, 6),
        "actual_source": book_source,
        "symbols": rows,
        "unpriced_signals": unpriced,
        "summary": summarize_rows(rows),
    }


def price_cause(symbol: str, state: dict, snap: dict, ex: dict) -> str:
    """Why a signaled coin has no mark and no position. Display only."""
    import tradfi
    sym = str(symbol or "")
    try:
        tradfi_hit = tradfi.is_tradfi(sym, state=state)
    except Exception:
        tradfi_hit = False
    if tradfi_hit:
        return "tradfi"
    lines: list[str] = []
    for blob in (snap, ex if isinstance(ex, dict) else {}):
        last = blob.get("last") if isinstance(blob.get("last"), dict) else {}
        for line in list(blob.get("skipped") or []) + list(last.get("skipped") or []):
            lines.append(str(line))
    for line in lines:
        head = line.split()[0] if line.split() else ""
        if head == sym and "not listed" in line.lower():
            return "not_listed_on_demo"
    return "no_mark"


def targets_from_state(state: dict | None) -> dict | None:
    state = state or {}
    direct = state.get("e5_targets")
    if isinstance(direct, dict) and (direct.get("w1") is not None or direct.get("ws")):
        return direct
    for key in (E5_KEY, OLD_KEY):
        ex = state.get(key)
        if isinstance(ex, dict) and isinstance(ex.get("targets"), dict):
            tgt = ex["targets"]
            if tgt.get("w1") is not None or tgt.get("ws"):
                return tgt
    return None


def load_targets(state: dict | None) -> tuple[dict | None, str]:
    """State first, then the existing GCS object the executor already reads.

    Missing credentials or a missing object become a source tag. No other network.
    """
    found = targets_from_state(state)
    if found is not None:
        return found, "state"
    import os
    bucket = (os.environ.get("GCS_BUCKET") or "").strip()
    if not bucket:
        return None, "missing"
    try:
        import json
        from google.cloud import storage
        blob = storage.Client().bucket(bucket).blob("trader/shadow_ab/e5_targets.json")
        if not blob.exists():
            return None, "missing"
        data = json.loads(blob.download_as_bytes())
    except Exception:
        return None, "unavailable"
    if isinstance(data, dict) and (data.get("w1") is not None or data.get("ws")):
        return data, "gcs:trader/shadow_ab/e5_targets.json"
    return None, "missing"


def compare(state: dict | None, status: dict | None = None, targets: dict | None = None,
            targets_source: str | None = None) -> dict:
    state = state if isinstance(state, dict) else {}
    key, slot, ex, mod = ledger(state)
    if targets is None:
        targets, targets_source = load_targets(state)
    elif not targets_source:
        targets_source = "argument"
    plans = _plans(state, targets)
    latest_bar = _bar(plans[-1]) if plans else ""
    live_px = status_prices(status, state)
    live_px.update(_exec_prices(ex))
    days = []
    for snap in plans:
        bar = _bar(snap)
        latest = bar == latest_bar
        px = dict(live_px) if latest else {}
        px.update(_snapshot_prices(snap))
        days.append(_day(snap, ex, state, slot, mod, px, latest))
    orch = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else {}
    weight = _f((orch.get("weights") or {}).get(slot))
    return {
        "code": "E5",
        "slot": slot,
        "ledger": key,
        "planner": "demo_e5v" if mod is demo_e5v else "demo_e5",
        "targets_source": targets_source or "missing",
        "orchestrator_weight": _round(weight, 4),
        "orchestrator_target": _round(_f((orch.get("targets") or {}).get(slot)), 4),
        "days": days,
        "note": (
            "每個交易日的目標權重與目標數量來自編排資金和 E5 目標計畫（w1／ws）。"
            "實際數量是對帳寫在 E5 虛擬帳本上的持倉，不是交易所淨額。"
            "原因只在有紀錄或同一套規劃規則能歸類時附上：skip_min、deadband、residual_close、unfilled。"
            "tracking_error 是帳戶權重的均方根誤差；pct_target_notional_achieved 是同向名義落在目標以內的比例。"
        ),
    }


def wanted(strategy: str, report: dict) -> bool:
    """True when the backtest filter is empty or names E5 / one of its symbols."""
    want = str(strategy or "").strip()
    if not want:
        return True
    if want in E5_CODES or want.upper() == "E5":
        return True
    for day in report.get("days") or []:
        for row in day.get("symbols") or []:
            if want == row.get("symbol"):
                return True
    return False
