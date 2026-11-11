"""Starting capital from real deposits, plus NAV, PnL, peak and max drawdown since 2026-10-01.

USDT-M and the USDC wallet U1 uses are the deposits. VBO's 1000 is a carve inside the USDT-M
deposit (2026-11-04), not a third deposit. R2S's 3000 was a virtual credit, removed 2026-11-11.

TRANSFER rows from GET /fapi/v1/income are the deposit record when Binance has them.
With none, USDT-M is 5000: after booked pnl the wallet matches that one deposit, not 8000.
USDC is the configured 5000 deposit. The method field says which path was used.
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
    by = {"USDT": 0.0, "USDC": 0.0}
    seen = False
    for t in transfers or []:
        if t.get("asset") in by:
            by[t["asset"]] += float(t["amount"])
            seen = True
    if seen and (by["USDT"] > 0 or by["USDC"] > 0):
        usdt, usdc = max(0.0, by["USDT"]), max(0.0, by["USDC"])
        method = "binance_income_transfer"
        note = "入金來自幣安 USD-M 收入紀錄的 TRANSFER。"
    else:
        usdt, usdc = USDT_M_DERIVED, USDC_DERIVED
        method = "derived_wallet_identity"
        note = ("沒有 TRANSFER 入金紀錄。USDT-M 以 5000 計：扣掉已入帳損益後，錢包吻合這一筆，不是 8000。"
                "USDC 以配置的 5000 計，供 U1 使用。")
    return {
        "usdt_m_deposit": round(usdt, 2),
        "usdc_deposit": round(usdc, 2),
        "vbo_carve_usdt": VBO_CARVE,
        "vbo_source": "carve_inside_usdt_m",
        "vbo_note": "VBO 的 1000 是 2026-11-04 從 USDT-M 入金裡劃出，不是另一筆入金。",
        "starting_total": round(usdt + usdc, 2),
        "r2s_virtual_excluded": 3000.0,
        "r2s_note": R2S_NOTE,
        "method": method,
        "method_note": note,
    }


def performance(points, starting_total: float, nav_now: float | None = None) -> dict:
    """Peak and max drawdown from the exchange daily series since 2026-10-01, then today's NAV."""
    series: list[float] = []
    for p in points or []:
        if not p or len(p) < 2 or str(p[0]) < "2026-10-01":
            continue
        try:
            series.append(float(p[1]))
        except (TypeError, ValueError):
            continue
    if nav_now is not None:
        try:
            series.append(float(nav_now))
        except (TypeError, ValueError):
            pass
    if not series:
        return {"nav": None, "pnl": None, "peak": None, "max_drawdown": None, "max_drawdown_pct": None}
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


def summarize(income_rows, points, nav_now, futures_nav=None) -> dict:
    dep = deposits(transfers_from_income(income_rows))
    out = {**dep, **performance(points, dep["starting_total"], nav_now), "nav_scope": "account_total"}
    if futures_nav is not None:
        try:
            fv = round(float(futures_nav), 2)
        except (TypeError, ValueError):
            fv = None
        if fv is not None:
            out["futures_nav"] = fv
            out["futures_pnl"] = round(fv - dep["starting_total"], 2)
    return out
