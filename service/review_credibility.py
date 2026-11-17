"""Backtest credibility for the reviewer. Does not rerun a backtest or place an order."""
from __future__ import annotations

import e5_tracking
import review_audit

HOLDOUT_FROM = "2026-10-01"
MISSING = "缺值"


def _show(value):
    if value is None or value == "" or value == "missing":
        return MISSING
    return value
PARITY_DAYS_REQUIRED = 21
# Assumptions already fixed in the Demo code. Not a measured live sample.
ASSUMPTIONS = {
    "cost": "E5 規劃用研究成本；實盤淨額手續費見 netting.FEE 0.0005。本頁不重跑回測。",
    "slippage": "對帳容忍 30 bps（live_vs_backtest.PRICE_BPS_TOL）。超過才算價格不符。",
    "funding": "資金費記在各策略現金，不另造一套假設。",
}


def build(status: dict | None, state: dict | None) -> dict:
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    report = e5_tracking.compare(state, status)
    days = report.get("days") or []
    latest = days[-1] if days else {}
    parity_days = len(days)
    te = (latest.get("summary") or {}).get("tracking_error") if isinstance(latest, dict) else None
    achieved = (latest.get("summary") or {}).get("pct_target_notional_achieved") if isinstance(latest, dict) else None
    verified = parity_days >= PARITY_DAYS_REQUIRED
    recon = review_audit.reconciliation(status, state)
    unpriced = []
    for day in days:
        for row in day.get("unpriced_signals") or []:
            unpriced.append({**row, "bar": day.get("bar")})
    return {
        "ok": True,
        "env": "demo",
        "commit": _show(status.get("api_version") or status.get("runner_version")),
        "sample_period": MISSING,
        "holdout_from": HOLDOUT_FROM,
        "walk_forward": MISSING,
        "assumptions": ASSUMPTIONS,
        "trial_count": MISSING,
        "dsr": MISSING,
        "parity_days": parity_days,
        "parity_status": "unverified" if not verified else "verified",
        "label_zh": "未驗證" if not verified else "已驗證",
        "parity_te": _show(te),
        "parity_achieved": _show(achieved),
        "parity_capital": _show(latest.get("sizing_capital") if latest else None),
        "virtual_capital": _show(latest.get("virtual_capital") if latest else None),
        "capital_basis_label": _show(latest.get("capital_basis_label") if latest else None),
        "orchestrator_target": report.get("orchestrator_target"),
        "before_after": [
            {"label": p.get("label"), "from": p.get("from"), "until": p.get("until"),
             "n_match": p.get("n_match"), "n_mismatch": p.get("n_mismatch"), "match_rate": p.get("match_rate")}
            for p in recon.get("periods") or []
        ],
        "unpriced_signals": unpriced,
        "note": (
            "對帳資金用編排目標，不用虛擬 5000。交易日少於 21 顯示 unverified。"
            "有訊號、沒有價格、也沒有持倉的幣列在 unpriced_signals，原因是 tradfi、not_listed_on_demo 或 no_mark。"
        ),
    }
