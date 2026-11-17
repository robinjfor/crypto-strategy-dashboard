"""Explicit Demo capital bridge. Display only.

The residual is reported. It is not subtracted from a ledger to force the
identity to zero. This module does not import the order path.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Orchestrator ceiling for U1 (orchestrator.DEFAULT_CAP["U"]). The USDC deposit is 5000.
U1_ALLOCATED_CAP = 1000.0
# Retired USDT satellite. Not USDC cash. Shown so it is not mistaken for the 4000.
APT_USDT_SLOT = 750.0
VBO_CARVE = 1000.0
E5_VIRTUAL = 5000.0
DIFF_RED = 0.5


def figure(value, *, formula: str, source: str, as_of: str | None, env: str = "demo", unit: str = "USDT"):
    """One number with its environment. Missing stays missing. Real with no account is not 0."""
    if env == "real":
        return {
            "value": None,
            "display": "not yet enabled",
            "label_zh": "尚未啟用",
            "env": "real",
            "formula": formula,
            "source": source,
            "as_of": as_of,
            "unit": unit,
        }
    if env not in ("demo", "paper"):
        env = "demo"
    if value is None:
        return {
            "value": None,
            "display": "missing",
            "label_zh": "missing",
            "env": env,
            "formula": formula,
            "source": source,
            "as_of": as_of,
            "unit": unit,
        }
    try:
        num = round(float(value), 2)
    except (TypeError, ValueError):
        return {
            "value": None,
            "display": "missing",
            "label_zh": "missing",
            "env": env,
            "formula": formula,
            "source": source,
            "as_of": as_of,
            "unit": unit,
        }
    return {
        "value": num,
        "display": f"{num:.2f}",
        "label_zh": f"{num:.2f}",
        "env": env,
        "formula": formula,
        "source": source,
        "as_of": as_of,
        "unit": unit,
    }


def _f(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _wallet(status: dict, quote: str) -> tuple[float | None, str]:
    """Margin balance for NAV. Prefer capital_basis, then the futures book."""
    basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
    key = "nav_usdt_m" if quote == "USDT" else "nav_usdc_m"
    direct = _f(basis.get(key))
    books = status.get("books") if isinstance(status.get("books"), dict) else {}
    fut = ((books.get(quote.lower()) or {}).get("futures") or {}) if isinstance(books.get(quote.lower()), dict) else {}
    margin = _f(fut.get("margin_balance"))
    wallet = _f(fut.get("wallet"))
    if direct is not None:
        return direct, "capital_basis." + key
    if margin is not None:
        return margin, f"books.{quote.lower()}.futures.margin_balance"
    if wallet is not None:
        return wallet, f"books.{quote.lower()}.futures.wallet"
    return None, "missing"


def _balance(status: dict, quote: str) -> float | None:
    """Wallet balance before unrealized, when the futures book has it. Else the margin figure."""
    books = status.get("books") if isinstance(status.get("books"), dict) else {}
    fut = ((books.get(quote.lower()) or {}).get("futures") or {}) if isinstance(books.get(quote.lower()), dict) else {}
    wallet = _f(fut.get("wallet"))
    if wallet is not None:
        return wallet
    return _wallet(status, quote)[0]


def _orch_target(state: dict, slot: str) -> float | None:
    orch = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else {}
    targets = orch.get("targets") if isinstance(orch.get("targets"), dict) else {}
    return _f(targets.get(slot))


def build_bridge(status: dict | None, state: dict | None) -> dict:
    import review_audit
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    as_of = status.get("updated_at")
    basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
    marks = review_audit.marks_from(status)
    e5_key = "demo_e5v_exec" if isinstance(state.get("demo_e5v_exec"), dict) else "demo_e5_exec"
    e5 = review_audit._cash_book(state.get(e5_key) if isinstance(state.get(e5_key), dict) else None, marks, E5_VIRTUAL)
    vbo = review_audit._cash_book(state.get("demo_vbo1h_exec") if isinstance(state.get("demo_vbo1h_exec"), dict) else None, marks, VBO_CARVE)
    r2s_ref, r2s_note = review_audit._r2s_ref(state)
    r2s = review_audit._cash_book(state.get("demo_r2s_exec") if isinstance(state.get("demo_r2s_exec"), dict) else None, marks, r2s_ref)
    fa = review_audit._fa_equity(state, marks)
    u1 = review_audit._u1_equity(state, marks)
    e5_target = _orch_target(state, "demo_e5_min5_x175_t05top20")
    u1_target = _orch_target(state, "u1_live")
    u1_cap = u1.get("capital")
    if u1_cap is None:
        u1_cap = u1_target if u1_target is not None else U1_ALLOCATED_CAP
    usdt_nav, usdt_src = _wallet(status, "USDT")
    usdc_nav, usdc_src = _wallet(status, "USDC")
    nav = _f(basis.get("nav"))
    if nav is None and usdt_nav is not None and usdc_nav is not None:
        nav = usdt_nav + usdc_nav
    usdt_balance = _balance(status, "USDT")
    usdc_balance = _balance(status, "USDC")
    book_parts = [e5.get("equity"), vbo.get("equity"), r2s.get("equity")]
    book_sum = None if any(v is None for v in book_parts) else round(sum(book_parts), 2)
    over = None if book_sum is None or usdt_nav is None else round(book_sum - usdt_nav, 2)
    # Allocated USDC is the U1 sleeve capital (1000 unless the sleeve says otherwise), not the 5000 deposit.
    allocated_usdc = round(float(u1_cap), 2) if u1_cap is not None else None
    unallocated_usdc = None if usdc_balance is None or allocated_usdc is None else round(usdc_balance - allocated_usdc, 2)
    # A book that already exceeds the USDT wallet contributes 0 unallocated, not a negative plug.
    unallocated_usdt = None
    if usdt_balance is not None and book_sum is not None:
        unallocated_usdt = round(max(0.0, usdt_balance - book_sum), 2)
    unallocated_cash = None
    if unallocated_usdc is not None or unallocated_usdt is not None:
        unallocated_cash = round((unallocated_usdc or 0.0) + (unallocated_usdt or 0.0), 2)
    eq_parts = [e5.get("equity"), vbo.get("equity"), r2s.get("equity"), u1.get("equity"), fa.get("equity")]
    # Absent FA (equity None and not present) counts as 0. A present ledger with a missing mark stays missing.
    fa_present = bool(state.get("demo_fa_positions") or state.get("demo_fa_exec"))
    sum_eq = 0.0
    sum_known = True
    for value, required in (
        (e5.get("equity"), isinstance(state.get(e5_key), dict)),
        (vbo.get("equity"), isinstance(state.get("demo_vbo1h_exec"), dict)),
        (r2s.get("equity"), isinstance(state.get("demo_r2s_exec"), dict)),
        (u1.get("equity"), isinstance(state.get("u1_live"), dict)),
        (fa.get("equity"), fa_present),
    ):
        if value is None:
            if required:
                sum_known = False
            continue
        sum_eq += float(value)
    sum_eq = round(sum_eq, 2) if sum_known else None
    explained = None if sum_eq is None or unallocated_cash is None else round(sum_eq + unallocated_cash, 2)
    diff = None if nav is None or explained is None else round(nav - explained, 2)
    paper = _f(((status.get("paper") or {}) if isinstance(status.get("paper"), dict) else {}).get("nav"))
    rows = [
        _strategy("E5", "demo_e5_min5_x175_t05top20", e5_key, E5_VIRTUAL, e5_target, e5, as_of,
                  "虛擬本金 5000。對帳用的資金基準是編排目標，不是這 5000。"),
        _strategy("VBO", "demo_vbo1h_top50_1k", "demo_vbo1h_exec", VBO_CARVE, None, vbo, as_of,
                  "1000 是從 USDT-M 劃出。E5 虛擬帳若仍以 5000 計，這 1000 會被加第二次。"),
        _strategy("R2S", "demo_r2s_top10_3k", "demo_r2s_exec", r2s_ref, _orch_target(state, "demo_r2s_top10_3k"), r2s, as_of, r2s_note),
        _strategy("FA", "demo_funding_arb_1x", "demo_fa_positions", 0.0, _orch_target(state, "demo_funding_arb_1x"), fa, as_of,
                  "FA 的 1000 與 VBO 是同一筆劃出，權益只計腿上的未實現。"),
        _strategy("U1", "u1_live", "u1_live", float(u1_cap) if u1_cap is not None else U1_ALLOCATED_CAP, u1_target, u1, as_of,
                  "U1 編排上限 1000。USDC 入金 5000 減這 1000，剩下 4000 沒有策略帳。"),
    ]
    return {
        "ok": True,
        "account": "binance_demo",
        "env": "demo",
        "as_of": as_of,
        "nav": figure(nav, formula="nav = USDT-M 保證金餘額 + USDC 保證金餘額。不含現貨，不含 Paper。",
                      source="capital_basis.nav", as_of=as_of),
        "nav_status": None if _f(basis.get("nav")) is None else round(float(basis["nav"]), 2),
        "usdt_m": figure(usdt_nav, formula="USDT-M 合約錢包＋未實現。", source=usdt_src, as_of=as_of),
        "usdc_m": figure(usdc_nav, formula="USDC 合約錢包＋未實現。", source=usdc_src, as_of=as_of),
        "book_sum_usdt": figure(book_sum, formula="E5 權益 + VBO 權益 + R2S 權益。", source="virtual ledgers", as_of=as_of),
        "usdt_book_over_wallet": figure(
            over, formula="book_sum_usdt − USDT-M 錢包。正值是帳本高過錢包，不從帳本扣掉。",
            source="bridge", as_of=as_of),
        "unallocated_usdc": figure(
            unallocated_usdc, formula="USDC 錢包餘額 − U1 配置本金（sleeve capital，否則編排上限 1000）。",
            source="books.usdc.futures.wallet", as_of=as_of),
        "unallocated_usdt": figure(
            unallocated_usdt, formula="max(0, USDT 錢包餘額 − E5+VBO+R2S 權益)。帳本超過錢包時這裡是 0，差額留在 over。",
            source="books.usdt.futures.wallet", as_of=as_of),
        "unallocated_cash": figure(
            unallocated_cash, formula="unallocated_usdc + unallocated_usdt。不是把 bridge_diff 塞回來的餘數。",
            source="bridge", as_of=as_of),
        "sum_strategy_equity": figure(sum_eq, formula="E5+VBO+R2S+U1+FA 權益。沒有 FA 部位時 FA 以 0 計。",
                                     source="virtual ledgers", as_of=as_of),
        "bridge_diff": figure(diff, formula="nav − (sum strategy_equity + unallocated_cash)。",
                              source="bridge", as_of=as_of),
        "bridge_diff_value": diff,
        "bridge_diff_red": bool(diff is not None and abs(diff) > DIFF_RED),
        "absorbed": False,
        "paper_nav": figure(paper, formula="Paper 淨值。不加入 Demo NAV。", source="status.paper.nav", as_of=as_of, env="paper"),
        "paper_in_demo_nav": False,
        "real": figure(None, formula="沒有實盤帳戶。", source="none", as_of=as_of, env="real"),
        "strategies": rows,
        "sources": [
            {
                "id": "vbo_carve_counted_twice",
                "amount": VBO_CARVE,
                "in_diff": False,
                "note": "VBO 的 1000 在 USDT-M 入金裡，也在 VBO 虛擬帳。E5 若仍用 5000 虛擬本金，加總會多算最多 1000。這 1000 是原因，不拿來把差額改成 0。",
            },
            {
                "id": "usdt_book_over_wallet",
                "amount": over,
                "in_diff": True,
                "note": "E5+VBO+R2S 減 USDT-M 錢包。活帳上這筆是 993.19 時，就是 1000 的雙重計算減掉約 6.81 的現金移動或損益，原樣顯示。",
            },
            {
                "id": "unallocated_usdc",
                "amount": unallocated_usdc,
                "in_diff": False,
                "note": "USDC 入金 5000，U1 編排與 sleeve 以 1000 計。5000−1000=4000 沒有策略。不是把差額吸進未分配。",
            },
            {
                "id": "apt_750_usdt_slot",
                "amount": APT_USDT_SLOT,
                "in_diff": False,
                "note": "sat_apt_1h（C8）是 750 USDT 的現貨槽，2026-10-07 停用。它不在 USDC 錢包裡，不能拿來解釋 4000。",
            },
        ],
        "identity": "nav = sum(strategy_equity) + unallocated_cash + bridge_diff",
        "note": "bridge_diff 留在原位。abs(bridge_diff) > 0.5 時 bridge_diff_red 為 true。",
    }


def _strategy(code, slot, ledger, virtual, target, row, as_of, note) -> dict:
    basis = target if target is not None else virtual
    label = "orchestrator_target" if target is not None else "virtual"
    return {
        "code": code,
        "slot": slot,
        "ledger": ledger,
        "env": "demo",
        "virtual_capital": virtual,
        "orchestrator_target": target,
        "strategy_capital_basis": basis,
        "strategy_capital_basis_label": label,
        "equity": row.get("equity"),
        "net_pnl": row.get("net_pnl"),
        "fees": row.get("fees"),
        "funding": row.get("funding"),
        "as_of": as_of,
        "note": note,
    }


def data_age_min(as_of: str | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    if not as_of:
        return {"data_age_min": None, "display": "missing", "level": "missing"}
    try:
        stamp = datetime.fromisoformat(str(as_of))
    except ValueError:
        return {"data_age_min": None, "display": "missing", "level": "missing"}
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    minutes = (now - stamp.astimezone(timezone.utc)).total_seconds() / 60.0
    if minutes < 0:
        minutes = 0.0
    level = "ok"
    if minutes > 26 * 60:
        level = "stale"
    elif minutes > 2 * 60:
        level = "yellow"
    return {"data_age_min": round(minutes, 1), "display": f"{round(minutes, 1)}", "level": level,
            "label_zh": "資料過舊" if level == "stale" else ("資料偏舊" if level == "yellow" else "資料時間正常")}
