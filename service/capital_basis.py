"""Starting capital from real deposits, plus NAV, PnL, peak and max drawdown since 2026-10-01.

USDT-M and the USDC wallet U1 uses are the deposits. VBO's 1000 is a carve inside the USDT-M
deposit (2026-11-04), not a third deposit. R2S's 3000 was a virtual credit, removed 2026-11-11.

TRANSFER rows from GET /fapi/v1/income are the deposit record when Binance has them.
With none, USDT-M is 5000: after booked pnl the wallet matches that one deposit, not 8000.
USDC is the configured 5000 deposit. Each asset falls back on its own; the method field says which path was used.
NAV is USDT-M + USDC-M margin balance (wallet + unrealized). Spot is not in NAV or PnL.
"""
from __future__ import annotations

USDT_M_DERIVED = 5000.0
USDC_DERIVED = 5000.0
VBO_CARVE = 1000.0
R2S_NOTE = "R2S 原本的 3000 為虛擬起始資金，未實際入金，已於 2026-11-11 扣除"


def transfers_from_income(rows) -> list[dict]:
    out = []
    for r in rows or []:
        typ = str(r.get("incomeType") or r.get("type") or "TRANSFER").upper()
        if typ != "TRANSFER":
            continue
        asset = str(r.get("asset") or "USDT").upper()
        try:
            amt = float(r.get("income") if r.get("income") is not None else r.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        try:
            tms = int(r.get("time") or r.get("time_ms") or 0)
        except (TypeError, ValueError):
            tms = 0
        out.append({"asset": asset, "amount": amt, "time_ms": tms})
    return out


def deposits(transfers: list[dict] | None) -> dict:
    """Per asset: the TRANSFER sum when Binance has one, else the known 5000 deposit.

    2026-11-12: Binance returned a USDT TRANSFER only, and the old code then set USDC to 0,
    so the start read 5000 instead of 10000. Each asset now falls back on its own.
    """
    by = {"USDT": 0.0, "USDC": 0.0}
    seen = {"USDT": False, "USDC": False}
    for t in transfers or []:
        a = t.get("asset")
        if a in by:
            by[a] += float(t["amount"])
            seen[a] = True
    src = {}
    vals = {}
    for a, derived in (("USDT", USDT_M_DERIVED), ("USDC", USDC_DERIVED)):
        if seen[a] and by[a] > 0:
            vals[a], src[a] = by[a], "binance_income_transfer"
        else:
            vals[a], src[a] = derived, "derived_wallet_identity"
    usdt, usdc = vals["USDT"], vals["USDC"]
    if src["USDT"] == src["USDC"]:
        method = src["USDT"]
    else:
        method = "mixed"
    notes = {
        "binance_income_transfer": "入金來自幣安 USD-M 收入紀錄的 TRANSFER。",
        "derived_wallet_identity": ("沒有 TRANSFER 入金紀錄。USDT-M 以 5000 計：扣掉已入帳損益後，錢包吻合這一筆，不是 8000。"
                                    "USDC 以配置的 5000 計，供 U1 使用。"),
        "mixed": ("USDT-M 入金" + ("來自 TRANSFER 紀錄" if src["USDT"] == "binance_income_transfer" else "以 5000 計")
                  + "；USDC 入金" + ("來自 TRANSFER 紀錄" if src["USDC"] == "binance_income_transfer" else "沒有 TRANSFER 紀錄，以配置的 5000 計")
                  + "。"),
    }
    return {
        "usdt_m_deposit": round(usdt, 2),
        "usdc_deposit": round(usdc, 2),
        "usdt_m_deposit_method": src["USDT"],
        "usdc_deposit_method": src["USDC"],
        "vbo_carve_usdt": VBO_CARVE,
        "vbo_source": "carve_inside_usdt_m",
        "vbo_note": "VBO 的 1000 是 2026-11-04 從 USDT-M 入金裡劃出，不是另一筆入金。",
        "starting_total": round(usdt + usdc, 2),
        "r2s_virtual_excluded": 3000.0,
        "r2s_note": R2S_NOTE,
        "method": method,
        "method_note": notes[method],
    }


def futures_nav(futures: dict | None) -> float | None:
    """USDT-M margin balance + USDC-M margin balance (wallet + unrealized each). No spot.

    None when either leg is missing, so a partial read never shows as a loss.
    """
    f = futures or {}
    try:
        if f.get("USDT") is None or f.get("USDC") is None:
            return None
        return round(float(f["USDT"]) + float(f["USDC"]), 2)
    except (TypeError, ValueError):
        return None


def update_history(history, nav: float | None, day: str, keep: int = 400) -> list:
    """Daily futures-NAV series [[YYYY-MM-DD, nav]]. Today's row is replaced, not appended."""
    rows = [list(r) for r in (history or []) if r and len(r) >= 2 and str(r[0]) != day]
    if nav is not None:
        rows.append([day, round(float(nav), 2)])
    rows.sort(key=lambda r: str(r[0]))
    return rows[-keep:]


def performance(points, starting_total: float, nav_now: float | None = None) -> dict:
    """Peak and max drawdown: the start, then the futures-NAV daily series since 2026-10-01, then today."""
    series: list[float] = [float(starting_total)]
    for p in points or []:
        if not p or len(p) < 2 or str(p[0]) < "2026-10-01":
            continue
        try:
            series.append(float(p[1]))
        except (TypeError, ValueError):
            continue
    if nav_now is None:
        return {"nav": None, "pnl": None, "peak": None, "max_drawdown": None, "max_drawdown_pct": None}
    series.append(float(nav_now))
    peak = series[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0:
            dd = peak - v
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd / peak
    nav = series[-1]
    return {
        "nav": round(nav, 2),
        "pnl": round(nav - float(starting_total), 2),
        "peak": round(peak, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 6),
    }


def wallet_portfolio(basis: dict | None) -> dict:
    """Dashboard portfolio fields. Same numbers as summarize(): futures NAV, not virtual books.

    drawdown is the series max drawdown as a fraction of the peak it fell from
    (max_drawdown_pct). max_drawdown is that drop in USDT. starting_total is the
    deposit basis (USDT-M + USDC), which does not include R2S's virtual 3000.
    """
    b = basis or {}
    return {
        "portfolio_equity": b.get("nav"),
        "peak": b.get("peak"),
        "drawdown": b.get("max_drawdown_pct"),
        "max_drawdown": b.get("max_drawdown"),
        "starting_total": b.get("starting_total"),
        "nav_usdt_m": b.get("nav_usdt_m"),
        "nav_usdc_m": b.get("nav_usdc_m"),
        "portfolio_basis": b.get("nav_scope") or "usd_m_futures_usdt_plus_usdc",
    }


def overlay_orchestrator(stored: dict | None, basis: dict | None) -> dict | None:
    """Copy a stored plan for /status, then replace the portfolio fields.

    Weights, targets, quote_pool and the per-slot equity map are left as the job
    wrote them. portfolio_equity, peak and drawdown become the futures-wallet
    basis. The caller's stored plan is not modified, so the job's drawdown alert
    and sizing still read the book-sum series.
    """
    out = {k: v for k, v in (stored or {}).items() if k != "history"}
    fields = wallet_portfolio(basis)
    if not out and fields.get("starting_total") is None and fields.get("portfolio_equity") is None:
        return None
    out.update(fields)
    return out


def summarize(income_rows, futures: dict | None, history=None, account_total=None) -> dict:
    """NAV = USDT-M (wallet + unrealized) + USDC-M (wallet + unrealized). Spot is not in NAV.

    futures: {"USDT": marginBalance, "USDC": marginBalance} from GET /fapi/v2/account.
    history: futures-NAV daily series from update_history. account_total (spot included) is
    reported for reference only and never used for PnL.
    """
    dep = deposits(transfers_from_income(income_rows))
    nav = futures_nav(futures)
    out = {**dep, **performance(history, dep["starting_total"], nav),
           "nav_scope": "usd_m_futures_usdt_plus_usdc",
           "nav_note": "淨值＝USDT-M 合約錢包＋未實現 ＋ USDC 合約錢包＋未實現，不含現貨。損益＝淨值 − 起始資金。",
           "nav_usdt_m": None if (futures or {}).get("USDT") is None else round(float(futures["USDT"]), 2),
           "nav_usdc_m": None if (futures or {}).get("USDC") is None else round(float(futures["USDC"]), 2)}
    if account_total is not None:
        try:
            out["account_total_incl_spot"] = round(float(account_total), 2)
        except (TypeError, ValueError):
            pass
    return out
