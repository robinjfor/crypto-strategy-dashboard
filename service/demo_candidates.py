"""Demo (testnet) candidate slots awaiting Robin's approval. DISABLED by design (enabled=False).
Nothing here places orders or touches allocation / approvals / catalog. The site shows these under 待審核;
the approve button appears only when BOTH gates pass:
  1. cloud_ordering_ok  — the Demo order path for this slot ran in the cloud and its fills read back
  2. parity_pass        — trade-by-trade parity vs the backtest engine (same decisions, sizes within tolerance)
Gate results live in state["demo_candidates"][id] and are written only by the (future) parity job.
Risk rule once enabled: daily reconcile; weekly |live − backtest| equity deviation > 0.5% -> pause this slot."""
from __future__ import annotations

WEEKLY_DEV_LIMIT = 0.005

CANDIDATES = [
    {"id": "demo_e_8020_x175", "name": "E：80%／20% 組合 × 1.75 槓桿", "capital_usdt": 1000.0, "venue": "幣安測試網永續",
     "spec": "research_1022/shadow_AB/ledger_e.py + README.md（80% 波動 0.40 趨勢＋20% 全市場多空，1.75 倍，回撤保護）",
     "decision": "每日 08:00（台北）收盤決策，08:00 開盤成交"},
    {"id": "demo_funding_arb_1x", "name": "資金費率套利：現貨多＋永續空，1 倍", "capital_usdt": 1000.0, "venue": "幣安測試網現貨＋永續",
     "spec": "funding_arb/SPEC.md，前推參數：出場門檻 0、最多 10 檔",
     "decision": "每日 08:00（台北）決策，下一根開盤兩腳同時成交",
     "warning": "規格文件標示已封存（10/21）、不建議實盤，需 Robin 確認"},
]


def view(state: dict | None) -> list[dict]:
    st = (state or {}).get("demo_candidates") or {}
    out = []
    for c in CANDIDATES:
        g = st.get(c["id"]) or {}
        ordering, parity = bool(g.get("cloud_ordering_ok")), bool(g.get("parity_pass"))
        out.append({**c, "enabled": False, "cloud_ordering_ok": ordering, "parity_pass": parity,
                    "parity_detail": g.get("parity_detail"), "approvable": ordering and parity,
                    "status": "等待雲端下單測試與逐筆對帳" if not (ordering and parity) else "可審核"})
    return out


def weekly_breach(live_equity: float, backtest_equity: float) -> bool:
    if not backtest_equity:
        return False
    return abs(live_equity / backtest_equity - 1) > WEEKLY_DEV_LIMIT
