"""One strategy's review card. Reads the bridge. Does not trade."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import review_bridge as bridge


def _rows(status, state):
    built = bridge.build_bridge(status, state)
    return {s["code"]: s for s in built["strategies"]}, built


def _when(value) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def detail(status: dict | None, state: dict | None, code: str | None) -> dict:
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    want = str(code or "E5").strip().upper()
    rows, built = _rows(status, state)
    row = rows.get(want)
    as_of = status.get("updated_at")
    if row is None:
        return {"ok": False, "error": "missing", "display": "missing", "code": want}
    weight = None
    orch = state.get("orchestrator") if isinstance(state.get("orchestrator"), dict) else {}
    weights = orch.get("weights") if isinstance(orch.get("weights"), dict) else {}
    weight = weights.get(row["slot"])
    version = (status.get("runner_version") or status.get("api_version") or None)
    trades = []
    for src in ("recent_fills", "closed_trades"):
        for t in status.get(src) or []:
            if isinstance(t, dict) and str(t.get("code") or "").upper() == want:
                trades.append(t)
    now = _when(as_of) or datetime.now(timezone.utc)
    window = now - timedelta(days=30)
    notion = 0.0
    dated = False
    for t in trades:
        stamp = _when(t.get("time") or t.get("closed_at"))
        px = t.get("quoteQty")
        if px is None and t.get("price") is not None and t.get("qty") is not None:
            try:
                px = abs(float(t["price"]) * float(t["qty"]))
            except (TypeError, ValueError):
                px = None
        if stamp is None or px is None:
            continue
        dated = True
        if stamp >= window:
            notion += abs(float(px))
    capital = row.get("strategy_capital_basis")
    turnover = None
    if dated and capital:
        turnover = round(notion / float(capital), 4)
    elif not dated:
        turnover = None
    start = None
    sl = state.get("u1_live") if want == "U1" and isinstance(state.get("u1_live"), dict) else {}
    start = sl.get("started_at") if sl else None
    if start is None:
        ex = state.get(row["ledger"]) if isinstance(state.get(row["ledger"]), dict) else {}
        start = ex.get("started_at") or ex.get("since")
    return {
        "ok": True,
        "env": "demo",
        "code": want,
        "version": version if version else None,
        "version_display": version or "missing",
        "slot": row["slot"],
        "strategy_capital_basis": row["strategy_capital_basis"],
        "strategy_capital_basis_label": row["strategy_capital_basis_label"],
        "virtual_capital": row["virtual_capital"],
        "orchestrator_target": row["orchestrator_target"],
        "weight": weight if weight is not None else None,
        "weight_display": weight if weight is not None else "missing",
        "equity": row["equity"] if row["equity"] is not None else None,
        "equity_display": row["equity"] if row["equity"] is not None else "missing",
        "realized": "missing",
        "unrealized": "missing",
        "net_pnl": row["net_pnl"] if row["net_pnl"] is not None else "missing",
        "fees": row["fees"] if row["fees"] is not None else "missing",
        "funding": row["funding"] if row["funding"] is not None else "missing",
        "turnover_30d": turnover if turnover is not None else "missing",
        "n_trades": len(trades),
        "start": start or "missing",
        "end": "missing",
        "as_of": as_of,
        "footer": {
            "text_zh": "策略權益合計 + 未分配現金 + 差額 = 錢包淨值",
            "sum_strategy_equity": built["sum_strategy_equity"]["value"],
            "unallocated_cash": built["unallocated_cash"]["value"],
            "bridge_diff": built["bridge_diff_value"],
            "nav": built["nav"]["value"],
            "bridge_diff_red": built["bridge_diff_red"],
            "identity": built["identity"],
        },
    }
