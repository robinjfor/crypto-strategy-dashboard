"""Read-only review figures: position owners, capital bridge, reconciliation, risk, liquidations.

Nothing here places an order, changes leverage, adds margin, or calls Binance.
Numbers come from the snapshot `build_status()` already built and from the virtual
ledgers on state (E5 `demo_e5v_exec`, VBO, R2S, FA, U1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Parity-fix commits in this repo. Dates are the commit dates, not a live clock.
# 9e6f7db (2026-10-08): Donchian live entry matches the backtest state machine.
# a770d7e (2026-10-21): stop anchored at the signal close; S and U1 parity; known causes.
PARITY_FIXES = (
    {
        "commit": "9e6f7db",
        "date": "2026-10-08",
        "summary": "Donchian 實盤只在 0→1 邊緣進場，拿掉回補。10/08 前的 C 槽位準進場都算這段。",
    },
    {
        "commit": "a770d7e",
        "date": "2026-10-21",
        "summary": "停損錨在訊號收盤×(1+20bps)。S 與 U1 納入每日對帳，已知原因寫在每一列。",
    },
)

# C8 APT 1h was live from the 2026-10-01 rotation until the 2026-10-07 stop.
# The repo does not store a fill print for 10/03. The day sits in the pre-fix window,
# and the recorded close of that same slot is 2026-10-07 09:58 (KNOWN_CAUSES).
APT_1003 = {
    "symbol": "APTUSDT",
    "code": "C8",
    "slot": "sat_apt_1h",
    "strategy_id": "donchian55_breakout_atr__APT__1h",
    "closed_at": "2026-10-03T00:00:00+08:00",
    "day": "2026-10-03",
    "match": False,
    "problems": ["before_donchian_entry_fix"],
    "source": "repo_history",
    "fix_commit": "9e6f7db",
    "fix_date": "2026-10-08",
    "reason": (
        "C8 APT 1h（sat_apt_1h）在 2026-10-01 輪入、2026-10-07 09:58 停損出場。"
        "10/03 落在 9e6f7db（2026-10-08）之前：舊版 runner 會回補進場、用位準而不是 0→1 邊緣。"
        "同槽已記錄的出場是 2026-10-07T09:58，原因同為這段舊 runner。"
        "2026-10-07 資本控管停用此槽，repo 狀態沒有 APT 持倉。這裡沒有另一筆 10/03 的成交價。"
    ),
}

E5_TOPUP_NOTE = (
    "PR #18 的 e5_margin.topup 走 active E5 虛擬帳（demo_e5v_exec，槽 demo_e5_min5_x175_t05top20）"
    "每一筆非零數量，不過濾交易所槓桿。E5 擁有的 3x 腿包含在內，交易所槓桿維持 3，不呼叫 set_leverage。"
    "這一層只讀，不加保證金、不改槓桿、不下單。VBO 的 3x 腿列在 vbo_3x，PR #18 不加它們的保證金。"
)

_SLOT = {
    "demo_e5_min5_x175_t05top20": {"owner": "E5", "strategy": "demo_e5_min5_x175_t05top20", "ledger": "demo_e5v_exec"},
    "demo_e5_min5_x175": {"owner": "E5", "strategy": "demo_e5_min5_x175", "ledger": "demo_e5_exec"},
    "demo_vbo1h_top50_1k": {"owner": "VBO", "strategy": "demo_vbo1h_top50_1k", "ledger": "demo_vbo1h_exec"},
    "demo_r2s_top10_3k": {"owner": "R2S", "strategy": "demo_r2s_top10_3k", "ledger": "demo_r2s_exec"},
    "demo_funding_arb_1x": {"owner": "FA", "strategy": "demo_funding_arb_1x", "ledger": "demo_fa_positions"},
}
_LEDGER_OWNER = {v["ledger"]: {**v, "slot": k} for k, v in _SLOT.items()}
_TAIPEI = timezone(timedelta(hours=8))
_ROLL = 7
_SHOCKS = (0.30, -0.30)
_FEE = 0.0005
_MMR = 0.01


def _num(v):
    try:
        if v is None or v is False or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _r2(v):
    n = _num(v)
    return None if n is None else round(n, 2)


def _r4(v):
    n = _num(v)
    return None if n is None else round(n, 4)


def _lev(v):
    n = _num(v)
    if n is None or n <= 0:
        return None
    return int(n) if abs(n - round(n)) < 1e-9 else round(n, 4)


def _sym(row: dict) -> str:
    raw = row.get("raw_symbol") or row.get("symbol") or ""
    s = str(raw).upper().split()[0]
    return s


def _day(value) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 and text[4] == "-" else ""


def _iso_ms(ms) -> str | None:
    n = _num(ms)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / 1000.0, tz=_TAIPEI).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def marks_from(status: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    books = status.get("books") if isinstance(status.get("books"), dict) else {}
    rows = list(status.get("positions") or []) + list(status.get("open_positions") or [])
    for book in books.values():
        fut = (book or {}).get("futures") if isinstance(book, dict) else None
        rows.extend((fut or {}).get("positions") or [])
    netting = status.get("netting") if isinstance(status.get("netting"), dict) else {}
    for slot in (netting.get("slots") or {}).values():
        if not isinstance(slot, dict):
            continue
        for sym, pos in (slot.get("positions") or {}).items():
            if isinstance(pos, dict) and _num(pos.get("mark")):
                out.setdefault(str(sym).upper(), float(pos["mark"]))
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = _sym(row)
        mark = _num(row.get("mark") if row.get("mark") is not None else row.get("mark_price"))
        if sym and mark:
            out[sym] = mark
    return out


def _u1_owners(state: dict) -> dict[str, list[dict]]:
    sl = state.get("u1_live") if isinstance(state.get("u1_live"), dict) else {}
    units = sl.get("units") if isinstance(sl.get("units"), dict) else {}
    quote = str(sl.get("quote") or "USDC").upper()
    if quote not in ("USDT", "USDC"):
        quote = "USDC"
    out: dict[str, list[dict]] = {}
    for coin, qty in units.items():
        q = _num(qty)
        if not q:
            continue
        sym = str(coin).upper() + quote
        out.setdefault(sym, []).append({
            "owner": "U1",
            "strategy": sl.get("strategy_id") or "u1_live",
            "slot": sl.get("slot") or "u1_live",
            "qty": round(q, 8),
        })
    return out


def owners_by_symbol(state: dict) -> dict[str, list[dict]]:
    """Virtual-ledger qty per symbol. FA perp qty is already signed short by netting.slot_books."""
    import netting
    out: dict[str, list[dict]] = {}
    try:
        books = netting.slot_books(state)
    except Exception:  # noqa: BLE001
        books = {}
    for slot, book in books.items():
        meta = _SLOT.get(slot) or {"owner": slot, "strategy": slot}
        for sym, qty in (book or {}).items():
            q = _num(qty)
            if not q:
                continue
            out.setdefault(str(sym).upper(), []).append({
                "owner": meta["owner"],
                "strategy": meta["strategy"],
                "slot": slot,
                "qty": round(q, 8),
            })
    for sym, rows in _u1_owners(state).items():
        out.setdefault(sym, []).extend(rows)
    for rows in out.values():
        rows.sort(key=lambda r: (str(r["owner"]), str(r["strategy"])))
    return out


def _owner_fields(rows: list[dict]) -> dict:
    if not rows:
        return {"owner": "unowned", "strategy": "unowned", "owners": []}
    full = [{"owner": r["owner"], "strategy": r["strategy"], "slot": r["slot"], "qty": r["qty"]} for r in rows]
    if len(rows) == 1:
        return {"owner": rows[0]["owner"], "strategy": rows[0]["strategy"], "owners": full}
    return {
        "owner": [{"owner": r["owner"], "qty": r["qty"]} for r in rows],
        "strategy": [{"strategy": r["strategy"], "qty": r["qty"]} for r in rows],
        "owners": full,
    }


def _futures_by_symbol(status: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    books = status.get("books") if isinstance(status.get("books"), dict) else {}
    for book in books.values():
        fut = (book or {}).get("futures") if isinstance(book, dict) else None
        for pos in (fut or {}).get("positions") or []:
            if isinstance(pos, dict) and _sym(pos):
                out[_sym(pos)] = pos
    return out


def _distance(mark, liq) -> float | None:
    m, q = _num(mark), _num(liq)
    if m is None or q is None or m <= 0 or q <= 0:
        return None
    return round(abs(m - q) / m, 6)


def _side_qty(row: dict) -> float | None:
    qty = _num(row.get("qty"))
    if qty is None:
        amt = _num(row.get("positionAmt"))
        return amt
    side = str(row.get("side") or "").upper()
    if side == "SHORT":
        return -abs(qty)
    if side == "LONG":
        return abs(qty)
    return qty


def annotate_positions(status: dict, state: dict) -> dict:
    """One row per symbol with owner, strategy, and leverage. Does not change ledgers."""
    owned = owners_by_symbol(state if isinstance(state, dict) else {})
    futs = _futures_by_symbol(status if isinstance(status, dict) else {})
    seen: set[str] = set()
    rows = []

    def emit(base: dict, sym: str) -> None:
        fut = futs.get(sym) or {}
        merged = dict(base)
        for key in ("leverage", "liq", "liq_distance", "margin_type", "entry", "mark", "unrealized_pnl", "isolated_wallet"):
            if merged.get(key) in (None, "") and fut.get(key) not in (None, ""):
                merged[key] = fut.get(key)
        mark = _num(merged.get("mark") if merged.get("mark") is not None else merged.get("mark_price"))
        liq = _num(merged.get("liq") if merged.get("liq") is not None else merged.get("liquidationPrice"))
        if merged.get("liq_distance") in (None, "") :
            dist = _distance(mark, liq)
            if dist is not None:
                merged["liq_distance"] = dist
        fields = _owner_fields(owned.get(sym) or [])
        lev = _lev(merged.get("leverage"))
        side_qty = _side_qty(merged)
        rows.append({
            **{k: v for k, v in merged.items() if k not in ("pin", "api_key", "api_secret", "token")},
            "raw_symbol": sym,
            "owner": fields["owner"],
            "strategy": fields["strategy"],
            "owners": fields["owners"],
            "leverage": lev,
            "liq": liq,
            "mark": mark if mark is not None else merged.get("mark"),
            "side_qty": None if side_qty is None else round(side_qty, 8),
        })

    for pos in (status or {}).get("positions") or (status or {}).get("open_positions") or []:
        if not isinstance(pos, dict):
            continue
        sym = _sym(pos)
        if not sym or sym in seen:
            continue
        seen.add(sym)
        emit(pos, sym)
    for sym in sorted(set(owned) - seen):
        seen.add(sym)
        qty = sum(r["qty"] for r in owned[sym])
        emit({
            "symbol": sym,
            "qty": abs(qty),
            "side": "SHORT" if qty < 0 else "LONG",
            "venue": "futures",
            "source": "virtual_ledger",
        }, sym)
    for sym, fut in sorted(futs.items()):
        if sym in seen:
            continue
        seen.add(sym)
        qty = _num(fut.get("qty")) or 0.0
        emit({
            **fut,
            "symbol": sym,
            "side": "SHORT" if qty < 0 else "LONG",
            "qty": abs(qty),
            "venue": "futures",
            "source": "exchange",
        }, sym)

    def leg_list(code: str, leverage: int) -> list[dict]:
        found = []
        for row in rows:
            owners = row.get("owners") or []
            mine = [o for o in owners if o.get("owner") == code]
            if not mine or row.get("leverage") != leverage:
                continue
            found.append({
                "symbol": row.get("raw_symbol"),
                "owner": code,
                "strategy": mine[0].get("strategy"),
                "slot": mine[0].get("slot"),
                "qty": mine[0].get("qty"),
                "leverage": leverage,
                "liq": row.get("liq"),
                "liq_distance": row.get("liq_distance"),
                "mark": row.get("mark"),
                "side": row.get("side"),
                "margin_type": row.get("margin_type"),
                "read_only": True,
            })
        return found

    return {
        "positions": rows,
        "vbo_3x": leg_list("VBO", 3),
        "e5_owned_3x": leg_list("E5", 3),
        "e5_topup": {
            "pr": 18,
            "separate": True,
            "includes_e5_owned_3x": True,
            "note": E5_TOPUP_NOTE,
        },
        "vbo_3x_note": "VBO 3x 腿只列強平距離。不改槓桿、不加保證金、不平倉。",
    }


def _cash_book(ex: dict | None, marks: dict, ref: float) -> dict:
    ex = ex if isinstance(ex, dict) else {}
    cash = _num(ex.get("cash"))
    book = ex.get("book") if isinstance(ex.get("book"), dict) else {}
    mtm = 0.0
    missing = []
    for sym, qty in book.items():
        q = _num(qty) or 0.0
        if not q:
            continue
        mark = marks.get(str(sym).upper())
        if mark is None:
            missing.append(str(sym).upper())
        else:
            mtm += q * mark
    fees = _num(ex.get("fees") if ex.get("fees") is not None else ex.get("fee"))
    funding = _num(ex.get("funding_total") if ex.get("funding_total") is not None else ex.get("funding"))
    complete = cash is not None and not missing
    equity = round(cash + mtm, 4) if complete else None
    net = round(equity - ref, 4) if equity is not None else None
    return {
        "cash": _r4(cash) if cash is not None else None,
        "mtm": round(mtm, 4) if not missing else None,
        "equity": equity,
        "net_pnl": net,
        "fees": _r4(fees) if fees is not None else None,
        "funding": _r4(funding) if funding is not None else None,
        "marks_missing": missing,
        "complete": complete,
    }


def _fa_equity(state: dict, marks: dict) -> dict:
    positions = state.get("demo_fa_positions") if isinstance(state.get("demo_fa_positions"), dict) else {}
    ex = state.get("demo_fa_exec") if isinstance(state.get("demo_fa_exec"), dict) else {}
    if not positions and not ex:
        return {"cash": None, "mtm": None, "equity": None, "net_pnl": None, "fees": None, "funding": None,
                "marks_missing": [], "complete": False,
                "note": "FA 的 1000 與 VBO 是同一筆劃出，沒有 FA 部位時本列不計權益。"}
    upnl = 0.0
    missing = []
    for sym, pos in positions.items():
        if not isinstance(pos, dict):
            continue
        mark = marks.get(str(sym).upper())
        if mark is None and (_num(pos.get("spot_qty")) or _num(pos.get("perp_qty"))):
            missing.append(str(sym).upper())
            continue
        if mark is None:
            continue
        spot = _num(pos.get("spot_qty")) or 0.0
        perp = _num(pos.get("perp_qty")) or 0.0
        spot_px = _num(pos.get("spot_px"))
        perp_px = _num(pos.get("perp_px"))
        if spot and spot_px is None:
            missing.append(str(sym).upper())
            continue
        if perp and perp_px is None:
            missing.append(str(sym).upper())
            continue
        upnl += spot * (mark - (spot_px or mark)) + perp * ((perp_px or mark) - mark)
    fees = _num(ex.get("fees") if ex.get("fees") is not None else ex.get("fee"))
    funding = _num(ex.get("funding_total") if ex.get("funding_total") is not None else ex.get("funding"))
    complete = not missing
    equity = round(upnl, 4) if complete else None
    return {
        "cash": None,
        "mtm": equity,
        "equity": equity,
        "net_pnl": equity,
        "fees": _r4(fees) if fees is not None else None,
        "funding": _r4(funding) if funding is not None else None,
        "marks_missing": missing,
        "complete": complete,
        "note": "FA 的 1000 與 VBO 是同一筆劃出，本列只計腿上的未實現，不加那 1000。",
    }


def _u1_equity(state: dict, marks: dict) -> dict:
    sl = state.get("u1_live") if isinstance(state.get("u1_live"), dict) else {}
    if not sl:
        return {"cash": None, "equity": None, "net_pnl": None, "fees": None, "funding": None,
                "marks_missing": [], "complete": False, "capital": None}
    cash = _num(sl.get("cash"))
    ref = _num(sl.get("capital_usdt"))
    if ref is None:
        ref = 5000.0
    quote = str(sl.get("quote") or "USDC").upper()
    missing = []
    mtm = 0.0
    for coin, qty in (sl.get("units") or {}).items():
        q = _num(qty) or 0.0
        if not q:
            continue
        sym = str(coin).upper() + quote
        mark = marks.get(sym)
        if mark is None:
            missing.append(sym)
        else:
            mtm += q * mark
    complete = cash is not None and not missing
    equity = round(cash + mtm, 4) if complete else None
    net = round(equity - ref, 4) if equity is not None else None
    return {
        "cash": _r4(cash) if cash is not None else None,
        "mtm": round(mtm, 4) if not missing else None,
        "equity": equity,
        "net_pnl": net,
        "fees": _r4(sl.get("fees")),
        "funding": _r4(sl.get("funding")),
        "marks_missing": missing,
        "complete": complete,
        "capital": ref,
    }


def _r2s_ref(state: dict) -> tuple[float, str]:
    mig = (state.get("migrations") or {}).get("r2s_unfunded_principal_20261111") if isinstance(state.get("migrations"), dict) else None
    if isinstance(mig, dict) and mig.get("done"):
        return 0.0, "R2S 的 3000 是虛擬起始資金，2026-11-11 已從帳本扣除，不計入 10000。"
    if "demo_r2s_exec" not in (state or {}):
        return 0.0, "沒有 R2S 帳本。3000 虛擬起始資金不在 10000 裡。"
    return 3000.0, "R2S 帳本仍含 3000 虛擬起始資金（遷移 r2s_unfunded_principal_20261111 尚未標完成）。損益對這 3000 計算，10000 不含它。"


def capital_bridge(status: dict, state: dict) -> dict:
    """start 10000, per-strategy net, fees, funding, then the wallet, with the residual explicit."""
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
    start = _num(basis.get("starting_total"))
    if start is None:
        start = 10000.0
    nav = _num(basis.get("nav"))
    usdt = _num(basis.get("nav_usdt_m"))
    usdc = _num(basis.get("nav_usdc_m"))
    if nav is None and usdt is not None and usdc is not None:
        nav = usdt + usdc
    marks = marks_from(status)
    r2s_ref, r2s_note = _r2s_ref(state)
    e5_key = "demo_e5v_exec" if isinstance(state.get("demo_e5v_exec"), dict) else "demo_e5_exec"
    e5 = _cash_book(state.get(e5_key), marks, 5000.0)
    if e5_key == "demo_e5v_exec" and isinstance(state.get("demo_e5_exec"), dict):
        old_book = (state.get("demo_e5_exec") or {}).get("book") or {}
        if any(_num(q) for q in old_book.values()):
            e5["note"] = "停用的 demo_e5_exec 還有部位。本列現金用 demo_e5v_exec。舊帳部位沒有再加一次現金。"
    vbo = _cash_book(state.get("demo_vbo1h_exec"), marks, 1000.0)
    r2s = _cash_book(state.get("demo_r2s_exec"), marks, r2s_ref)
    fa = _fa_equity(state, marks)
    u1 = _u1_equity(state, marks)
    specs = (
        ("E5", "demo_e5_min5_x175_t05top20", e5_key, 5000.0, e5,
         "E5 參考本金 5000，就是 USDT-M 那一筆入金。"),
        ("VBO", "demo_vbo1h_top50_1k", "demo_vbo1h_exec", 1000.0, vbo,
         "VBO 的 1000 是從 USDT-M 劃出，不是第三筆入金。淨損益＝權益−1000。"),
        ("R2S", "demo_r2s_top10_3k", "demo_r2s_exec", r2s_ref, r2s, r2s_note),
        ("FA", "demo_funding_arb_1x", "demo_fa_positions", 0.0, fa, fa.get("note")),
        ("U1", (state.get("u1_live") or {}).get("slot") or "u1_live", "u1_live", u1.get("capital") or 5000.0, u1,
         "U1 在 USDC 合約錢包。參考本金是 sleeve 的 capital_usdt，沒有 sleeve 時用 USDC 入金 5000。"),
    )
    strategies = []
    sum_equity = 0.0
    sum_known = True
    flow = round(start, 2)
    for code, strategy, ledger, ref, row, note in specs:
        present = ledger in state or (code == "U1" and isinstance(state.get("u1_live"), dict)) or (code == "FA" and state.get("demo_fa_positions"))
        item = {
            "code": code,
            "strategy": strategy,
            "ledger": ledger,
            "capital": ref,
            "in_start": code in ("E5", "U1"),
            "cash": row.get("cash"),
            "equity": row.get("equity"),
            "net_pnl": row.get("net_pnl"),
            "realized_pnl": None,
            "unrealized_pnl": None,
            "fees": row.get("fees"),
            "funding": row.get("funding"),
            "marks_missing": row.get("marks_missing") or [],
            "note": " ".join(x for x in (note, row.get("note")) if x),
            "present": bool(present) or row.get("equity") not in (None, 0),
        }
        if code != "FA":
            item["unrealized_note"] = "帳本只有數量與現金，沒有逐筆進場價時，已實現與未實現合在 net_pnl。fees 與 funding 若有記錄，已經在現金裡，不重複扣。"
        else:
            item["unrealized_pnl"] = row.get("equity")
            item["realized_pnl"] = 0.0 if row.get("equity") is not None else None
        if row.get("equity") is None:
            if present:
                sum_known = False
        else:
            sum_equity += float(row["equity"])
        if row.get("net_pnl") is not None:
            flow = round(flow + float(row["net_pnl"]), 2)
        strategies.append(item)
    sum_equity = round(sum_equity, 2) if sum_known else None
    unowned = 0.0
    unowned_known = True
    owned = owners_by_symbol(state)
    for pos in status.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        sym = _sym(pos)
        if owned.get(sym):
            continue
        upnl = _num(pos.get("unrealized_pnl"))
        if upnl is None:
            unowned_known = False
        else:
            unowned += upnl
    unallocated = round(unowned, 2) if unowned_known else None
    explained = None if sum_equity is None or unallocated is None else round(sum_equity + unallocated, 2)
    residual = None if nav is None or explained is None else round(nav - explained, 2)
    wallet = None if usdt is None or usdc is None else round(usdt + usdc, 2)
    return {
        "as_of": status.get("updated_at"),
        "start": round(start, 2),
        "strategies": strategies,
        "flow_explained": (
            flow if any(s.get("present") for s in strategies)
            and all(s.get("net_pnl") is not None or not s.get("present") for s in strategies)
            else None
        ),
        "flow_note": "flow_explained＝起始資金＋各策略 net_pnl。net_pnl＝權益−參考本金，fees 與 funding 已在現金裡。",
        "sum_ledgers": sum_equity,
        "unallocated": unallocated,
        "unallocated_note": "unallocated 是沒有虛擬帳本認領的交易所部位未實現損益。不是用錢包倒扣出來的。",
        "wallet_nav": None if nav is None else round(nav, 2),
        "wallet_usdt_m": None if usdt is None else round(usdt, 2),
        "wallet_usdc_m": None if usdc is None else round(usdc, 2),
        "wallet_sum": wallet,
        "check": {
            "sum_ledgers": sum_equity,
            "unallocated": unallocated,
            "sum_plus_unallocated": explained,
            "wallet": None if nav is None else round(nav, 2),
            "residual": residual,
            "identity": "sum(ledgers)+unallocated+residual＝wallet",
        },
        "note": (
            "錢包是 USDT-M 保證金餘額＋USDC 保證金餘額。E5 與 U1 的參考本金加總是起始 10000；"
            "VBO 的 1000 在這 10000 裡面。兩邊帳本都還握著同一筆現金時，residual 就是那個缺口。"
        ),
    }


def _fixes(state: dict) -> list[dict]:
    rows = [dict(x) for x in PARITY_FIXES]
    extra = state.get("parity_fixes") if isinstance(state, dict) else None
    if isinstance(extra, list):
        for item in extra:
            if not isinstance(item, dict) or not item.get("date"):
                continue
            key = (str(item.get("date"))[:10], str(item.get("commit") or ""))
            if any((r["date"], r.get("commit") or "") == key for r in rows):
                continue
            rows.append({"commit": item.get("commit"), "date": str(item.get("date"))[:10], "summary": item.get("summary") or ""})
    rows.sort(key=lambda r: r["date"])
    return rows


def _reason(row: dict) -> str:
    if row.get("reason"):
        return str(row["reason"])
    if row.get("root_cause"):
        return str(row["root_cause"])
    try:
        import live_vs_backtest as lvb
        known = lvb.KNOWN_CAUSES.get((row.get("slot"), str(row.get("closed_at") or "")[:16]))
    except Exception:  # noqa: BLE001
        known = None
    if known:
        return str(known)
    problems = [str(p) for p in (row.get("problems") or []) if p]
    if problems:
        return "、".join(problems)
    if row.get("match") is False:
        return "未標原因"
    return ""


def _catalog_rows() -> list[dict]:
    try:
        import live_vs_backtest as lvb
        apt_close = lvb.KNOWN_CAUSES.get(("sat_apt_1h", "2026-10-07T09:58"))
    except Exception:  # noqa: BLE001
        apt_close = None
    close = {
        "symbol": "APTUSDT",
        "code": "C8",
        "slot": "sat_apt_1h",
        "strategy_id": "donchian55_breakout_atr__APT__1h",
        "closed_at": "2026-10-07T09:58:00+08:00",
        "day": "2026-10-07",
        "match": False,
        "source": "known_causes",
        "reason": apt_close or "舊版 runner（10/08 前 backfill／位準進場，9e6f7db 已修）",
    }
    return [dict(APT_1003), close]


def _live_rows(status: dict, state: dict) -> list[dict]:
    lv = status.get("live_vs_backtest") if isinstance(status.get("live_vs_backtest"), dict) else {}
    rows = [r for r in (lv.get("rows") or []) if isinstance(r, dict)]
    hist = state.get("live_vs_backtest_history") if isinstance(state.get("live_vs_backtest_history"), list) else []
    for snap in hist:
        if isinstance(snap, dict):
            rows.extend(r for r in (snap.get("rows") or []) if isinstance(r, dict))
    return rows


def _dedupe_key(row: dict) -> tuple:
    return (str(row.get("slot") or ""), str(row.get("symbol") or ""), str(row.get("closed_at") or row.get("day") or "")[:16])


def reconciliation(status: dict, state: dict) -> dict:
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    fixes = _fixes(state)
    merged: dict[tuple, dict] = {}
    for row in _catalog_rows() + _live_rows(status, state):
        item = dict(row)
        item["day"] = item.get("day") or _day(item.get("closed_at"))
        if item.get("match") is False or item.get("source") == "repo_history":
            item["reason"] = _reason(item)
        elif item.get("match") is True:
            item["reason"] = item.get("reason") or ""
        else:
            item["reason"] = _reason(item) if item.get("problems") else item.get("reason") or ""
        merged[_dedupe_key(item)] = item
    rows = sorted(merged.values(), key=lambda r: str(r.get("closed_at") or r.get("day") or ""))
    mismatches = []
    for row in rows:
        if row.get("match") is False:
            if not row.get("reason"):
                row["reason"] = "未標原因"
            mismatches.append(row)
    cuts = [f["date"] for f in fixes]
    periods = []
    bounds = [("", cuts[0] if cuts else "9999-99-99")]
    for i, day in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else ""
        bounds.append((day, end))
    for start, end in bounds:
        bucket = [r for r in rows if (not start or (r.get("day") or "") >= start) and (not end or (r.get("day") or "9999") < end)]
        decided = [r for r in bucket if r.get("match") is True or r.get("match") is False]
        ok = sum(1 for r in decided if r.get("match") is True)
        bad = sum(1 for r in decided if r.get("match") is False)
        label = f"{start or '起初'} → {end or '之後'}"
        periods.append({
            "from": start or None,
            "until": end or None,
            "label": label,
            "n": len(decided),
            "n_match": ok,
            "n_mismatch": bad,
            "match_rate": round(ok / len(decided), 4) if decided else None,
            "mismatches": [r for r in bucket if r.get("match") is False],
        })
    decided_all = [r for r in rows if r.get("match") is True or r.get("match") is False]
    rolling = []
    for i, row in enumerate(decided_all):
        window = decided_all[max(0, i - (_ROLL - 1)):i + 1]
        ok = sum(1 for r in window if r.get("match") is True)
        rolling.append({
            "day": row.get("day"),
            "closed_at": row.get("closed_at"),
            "symbol": row.get("symbol"),
            "n": len(window),
            "match_rate": round(ok / len(window), 4),
        })
    return {
        "fixes": fixes,
        "apt_1003": next((r for r in mismatches if r.get("day") == "2026-10-03" and "APT" in str(r.get("symbol"))), None),
        "mismatches": mismatches,
        "periods": periods,
        "rolling_match_rate": rolling[-120:],
        "rolling_window": _ROLL,
        "slippage": slippage(status, state, rows),
        "note": "修復日是 repo 的 parity-fix commit 日，再加上 state.parity_fixes。match 為 null 的列不進勝率。",
    }


def _slip_points(status: dict, rows: list[dict]) -> list[dict]:
    pts = []
    for row in rows:
        day = row.get("day") or _day(row.get("closed_at"))
        for key, label in (("entry_bps", "entry"), ("exit_bps", "exit")):
            bps = _num(row.get(key))
            if bps is None:
                continue
            pts.append({"day": day, "symbol": row.get("symbol"), "bps": round(bps, 2), "leg": label, "source": "live_vs_backtest"})
        fill = _num(row.get("live_entry_px") or row.get("price"))
        target = _num(row.get("target_price") or row.get("signal_price") or row.get("target_px") or row.get("bt_entry_px"))
        if fill and target:
            pts.append({
                "day": day, "symbol": row.get("symbol"),
                "bps": round((fill / target - 1.0) * 1e4, 2),
                "leg": "fill_vs_target", "source": "row",
            })
    for src in (status.get("recent_fills") or [], status.get("closed_trades") or []):
        for t in src or []:
            if not isinstance(t, dict):
                continue
            price = _num(t.get("price") or t.get("exit"))
            target = _num(t.get("target_price") or t.get("signal_price") or t.get("target_px"))
            if not price or not target:
                continue
            pts.append({
                "day": _day(t.get("time") or t.get("closed_at")),
                "symbol": t.get("symbol"),
                "bps": round((price / target - 1.0) * 1e4, 2),
                "leg": "fill_vs_target",
                "source": "fill",
            })
    for t in status.get("demo_fa_slippage") or []:
        if isinstance(t, dict) and _num(t.get("bps")) is not None:
            pts.append({"day": str(t.get("date") or "")[:10], "symbol": t.get("symbol"),
                        "bps": round(float(t["bps"]), 2), "leg": t.get("leg") or "fa", "source": "demo_fa_slippage"})
    return [p for p in pts if p.get("day")]


def slippage(status: dict, _state: dict, rows: list[dict] | None = None) -> dict:
    pts = _slip_points(status, rows or [])
    by: dict[str, list[float]] = {}
    for p in pts:
        by.setdefault(p["day"], []).append(p["bps"])
    daily = []
    for day in sorted(by):
        vals = by[day]
        daily.append({"day": day, "n": len(vals), "mean_bps": round(sum(vals) / len(vals), 2)})
    return {"points": pts[-400:], "daily": daily[-400:], "note": "正值代表成交價高於訊號／目標價。entry_bps／exit_bps 沿用對帳列。"}


def _isolated(row: dict) -> bool:
    mt = str(row.get("margin_type") or "").lower()
    if mt == "cross":
        return False
    if mt == "isolated":
        return True
    owners = row.get("owners") or []
    codes = {o.get("owner") for o in owners}
    sym = str(row.get("raw_symbol") or "")
    if codes == {"U1"} or sym.endswith("USDC"):
        return False
    return str(row.get("venue") or "futures") == "futures"


def _liq_price(row: dict, qty: float, entry: float, lev: float) -> float | None:
    liq = _num(row.get("liq"))
    if liq and liq > 0:
        return liq
    try:
        import isolated_liq
        return isolated_liq.liquidation_price(entry, qty, lev, _MMR, 0.0)
    except Exception:  # noqa: BLE001
        if lev <= 0:
            return None
        if qty > 0:
            return entry * (1.0 - 1.0 / lev)
        return entry * (1.0 + 1.0 / lev)


def _shock_pnl(row: dict, shock: float) -> dict:
    qty = _num(row.get("side_qty"))
    if qty is None:
        qty = _side_qty(row)
    mark = _num(row.get("mark"))
    if not qty or not mark:
        return {"pnl": 0.0, "liquidated": False, "skipped": True}
    entry = _num(row.get("entry")) or mark
    lev = _lev(row.get("leverage")) or 1
    mark2 = mark * (1.0 + shock)
    isolated = _isolated(row)
    liq = _liq_price(row, qty, entry, float(lev)) if isolated else None
    breached = False
    if isolated and liq:
        breached = mark2 <= liq if qty > 0 else mark2 >= liq
    if breached:
        wallet = _num(row.get("isolated_wallet"))
        if wallet is None:
            wallet = abs(qty) * entry / float(lev)
        pnl = -(abs(wallet) + _FEE * abs(qty) * (liq or mark2))
        how = "isolated_liquidation"
    else:
        pnl = qty * (mark2 - mark)
        how = "mark_to_market"
    return {"pnl": round(pnl, 4), "liquidated": breached, "method": how, "liq": liq}


def portfolio_risk(status: dict, state: dict) -> dict:
    """Display only. Shocks the exchange net, not each virtual leg, so a netted symbol moves once."""
    status = status if isinstance(status, dict) else {}
    view = annotate_positions(status, state if isinstance(state, dict) else {})
    rows = [r for r in view["positions"] if _num(r.get("side_qty") if r.get("side_qty") is not None else _side_qty(r)) and _num(r.get("mark"))]
    gross_long = 0.0
    gross_short = 0.0
    for row in rows:
        qty = _num(row.get("side_qty"))
        if qty is None:
            qty = _side_qty(row) or 0.0
        notion = qty * (_num(row.get("mark")) or 0.0)
        if notion >= 0:
            gross_long += notion
        else:
            gross_short += -notion
    shocks = {}
    for shock in _SHOCKS:
        legs = []
        total = 0.0
        n_liq = 0
        for row in rows:
            leg = _shock_pnl(row, shock)
            total += leg["pnl"]
            if leg.get("liquidated"):
                n_liq += 1
            legs.append({"symbol": row.get("raw_symbol"), **leg})
        key = "up_30" if shock > 0 else "down_30"
        shocks[key] = {
            "shock": shock,
            "pnl": round(total, 2),
            "loss": round(max(0.0, -total), 2),
            "liquidations": n_liq,
            "legs": legs,
        }
    hist = status.get("nav_history") or []
    if not hist:
        try:
            import capital_basis
            basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
            hist = capital_basis.display_nav_history(
                (state or {}).get("capital_basis_history") if isinstance(state, dict) else [],
                basis.get("starting_total") if basis.get("starting_total") is not None else 10000.0,
            )
        except Exception:  # noqa: BLE001
            hist = []
    worst = None
    for i in range(1, len(hist)):
        try:
            day, val = str(hist[i][0]), float(hist[i][1]) - float(hist[i - 1][1])
        except (TypeError, ValueError, IndexError):
            continue
        if worst is None or val < worst[1]:
            worst = (day, val)
    loss = None if worst is None or worst[1] >= 0 else round(-worst[1], 2)
    return {
        "ok": True,
        "display_only": True,
        "gross_long": round(gross_long, 2),
        "gross_short": round(gross_short, 2),
        "net_exposure": round(gross_long - gross_short, 2),
        "stress_up_30": shocks["up_30"],
        "stress_down_30": shocks["down_30"],
        "worst_day": None if worst is None else worst[0],
        "worst_day_change": None if worst is None else round(worst[1], 2),
        "worst_day_loss": loss if loss is not None else 0.0,
        "note": (
            "總多、總空、淨曝險是交易所淨部位的名目（標記價）。"
            "+30%／−30% 讓所有幣一起動。逐倉在震後價穿過強平價時，虧損約為逐倉錢包加清算手續費，不再按全額標記價計。"
            "U1（USDC、全倉）按全額標記價，不設逐倉上限。單日最大虧損來自淨值日序列的相鄰差。不下單。"
        ),
    }


def _liq_row(base: dict) -> dict:
    row = dict(base)
    row["liquidation"] = True
    row["kind"] = row.get("kind") or "liquidation"
    row["time"] = row.get("time") or row.get("at") or row.get("closed_at")
    return row


def _looks_like_liquidation(row: dict) -> bool:
    if row.get("liquidation") is True or row.get("liquidated") is True:
        return True
    cause = str(row.get("cause") or row.get("reason") or row.get("autoCloseType") or "").lower()
    if "liquidat" in cause:
        return True
    coid = row.get("clientOrderId") or row.get("client_order_id") or ""
    try:
        import exchange_close_sync as xcs
        return xcs.classify_coid(coid, row.get("autoCloseType")) == "liquidation"
    except Exception:  # noqa: BLE001
        return str(coid).startswith("autoclose-")


def liquidation_trades(status: dict, state: dict) -> list[dict]:
    """Recorded forced-liquidation fills. No Binance call."""
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    out = []
    for src in ("recent_fills", "closed_trades"):
        for t in status.get(src) or []:
            if isinstance(t, dict) and _looks_like_liquidation(t):
                out.append(_liq_row({**t, "source": src}))
    closes = state.get("external_closes") if isinstance(state.get("external_closes"), dict) else {}
    for key, rec in closes.items():
        if not isinstance(rec, dict) or str(rec.get("cause") or "").lower() != "liquidation":
            continue
        legs = rec.get("legs") or []
        delta = sum(_num(leg.get("delta")) or 0.0 for leg in legs if isinstance(leg, dict))
        owners = []
        for leg in legs:
            if not isinstance(leg, dict):
                continue
            meta = _SLOT.get(leg.get("slot") or "") or {}
            owners.append({"owner": meta.get("owner") or leg.get("slot"), "strategy": meta.get("strategy") or leg.get("slot"),
                           "slot": leg.get("slot"), "qty": leg.get("qty_before")})
        out.append(_liq_row({
            "symbol": rec.get("symbol"),
            "time": rec.get("at"),
            "price": rec.get("vwap"),
            "qty": abs(delta) if delta else None,
            "side": "BUY" if delta > 0 else "SELL",
            "pnl_usdt": rec.get("realized_pnl"),
            "id": (rec.get("trade_ids") or [None])[0],
            "orderId": (rec.get("order_ids") or [None])[0],
            "clientOrderId": (rec.get("client_order_ids") or [None])[0],
            "owners": owners,
            "owner": owners[0]["owner"] if len(owners) == 1 else ([{"owner": o["owner"], "qty": o["qty"]} for o in owners] or "unowned"),
            "strategy": owners[0]["strategy"] if len(owners) == 1 else [o["strategy"] for o in owners],
            "cause": "liquidation",
            "source": "external_closes",
            "booking": key,
        }))
    for adj in state.get("ledger_adjustments") or []:
        if not isinstance(adj, dict):
            continue
        if str(adj.get("cause") or adj.get("kind") or "").lower() not in ("liquidation", "external_close"):
            if "liquidat" not in str(adj.get("cause") or "").lower():
                continue
        if str(adj.get("cause") or "").lower() != "liquidation" and "liquidat" not in str(adj.get("kind") or "").lower():
            continue
        out.append(_liq_row({
            "symbol": adj.get("symbol"),
            "time": adj.get("at"),
            "pnl_usdt": adj.get("realized_pnl_not_booked") or adj.get("realized_pnl"),
            "id": adj.get("id"),
            "cause": "liquidation",
            "source": "ledger_adjustments",
        }))
    for ledger, meta in _LEDGER_OWNER.items():
        ex = state.get(ledger)
        if not isinstance(ex, dict):
            continue
        for sym, marker in (ex.get("forced_flat") or {}).items():
            if not isinstance(marker, dict) or str(marker.get("cause") or "").lower() != "liquidation":
                continue
            out.append(_liq_row({
                "symbol": sym,
                "time": marker.get("at"),
                "qty": abs(_num(marker.get("qty_before")) or 0) or None,
                "owner": meta["owner"],
                "strategy": meta["strategy"],
                "slot": meta["slot"],
                "cause": "liquidation",
                "source": "forced_flat",
                "id": (marker.get("trade_ids") or [None])[0],
            }))
    sl = state.get("u1_live") if isinstance(state.get("u1_live"), dict) else {}
    for ev in sl.get("events") or []:
        if isinstance(ev, dict) and ev.get("event") == "sleeve_liquidation":
            out.append(_liq_row({
                "symbol": ev.get("symbol") or "U1",
                "time": ev.get("at"),
                "owner": "U1",
                "strategy": sl.get("strategy_id") or "u1_live",
                "cause": "liquidation",
                "source": "u1_events",
            }))
    for fill in sl.get("fills") or []:
        if isinstance(fill, dict) and _looks_like_liquidation(fill):
            out.append(_liq_row({**fill, "owner": "U1", "strategy": sl.get("strategy_id") or "u1_live", "source": "u1_fills",
                                "time": fill.get("time") or fill.get("filled_at")}))
    dedup = []
    seen = set()
    for row in out:
        if not row.get("symbol") and not row.get("time"):
            continue
        key = (str(row.get("symbol") or ""), str(row.get("time") or ""), str(row.get("id") or ""), str(row.get("source") or ""))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(row)
    return dedup
