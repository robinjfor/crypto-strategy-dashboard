"""Demo (testnet) candidate slots awaiting Robin's approval. DISABLED by design (enabled=False).
Nothing here places orders or touches allocation / approvals / catalog. The site shows these under 待審核;
the approve button appears only when BOTH gates pass:
  1. cloud_ordering_ok  — the Demo order path for this slot ran in the cloud and its fills read back
  2. parity_pass        — trade-by-trade parity vs the backtest engine (same decisions, sizes within tolerance)
Gate results live in state["demo_candidates"][id] and are written only by the (future) parity job.
Risk rule once enabled: daily reconcile; weekly |live − backtest| equity deviation > 0.5% -> pause this slot."""
from __future__ import annotations

WEEKLY_DEV_LIMIT = 0.005
# Third gate: acceptance by fund control (自動交易專家). Config flag, default false for every slot.
# Set via env DEMO_ACCEPTED="slot_id,slot_id" on the trader service; nothing in code flips it.
import os as _os
def accepted(slot_id: str) -> bool:
    # ';' also accepted: gcloud --set-env-vars uses ',' as its own separator
    return slot_id in {x.strip() for x in _os.environ.get("DEMO_ACCEPTED", "").replace(";", ",").split(",") if x.strip()}

CANDIDATES = [
    {"id": "demo_e_8020_x175", "name": "E：80%／20% 組合 × 1.75 槓桿", "capital_usdt": 1000.0, "venue": "幣安測試網永續",
     "spec": "research_1022/shadow_AB/ledger_e.py + README.md（80% 波動 0.40 趨勢＋20% 全市場多空，1.75 倍，回撤保護）",
     "decision": "每日 08:00（台北）收盤決策，08:00 開盤成交"},
    {"id": "demo_funding_arb_1x", "name": "資金費率套利：現貨多＋永續空，1 倍", "capital_usdt": 1000.0, "venue": "幣安測試網現貨＋永續",
     "spec": "funding_arb/SPEC.md＋research_1023/REPORT.md（10/23 前推再次通過），出場門檻 0、最多 10 檔；觀察倉，後半段報酬接近現金",
     "decision": "每日 08:00（台北）決策，下一根開盤兩腳同時成交"},
    {"id": "demo_r2s_top10_3k", "name": "R2S：前 10 大幣趨勢多空（平滑＋遲滯，最小下單額）", "capital_usdt": 3000.0, "venue": "幣安測試網永續",
     "spec": "research_regime_v2/r2s_minnot.py＋SPEC_minnot.md（逐幣交易所最小下單額＋stepSize，1 倍）",
     "decision": "每日 08:00（台北）收盤決策，08:00 開盤成交"},
    {"id": "demo_e5_min5_x175", "name": "E：80%／20% 組合 × 1.75 槓桿（逐幣最小下單額）", "capital_usdt": 5000.0, "venue": "幣安測試網永續",
     "spec": "research_1022/shadow_AB/ledger_e_minnot.py＋MINNOTIONAL.md（逐幣交易所最小下單額＋stepSize，5,000U）",
     "decision": "每日 08:00（台北）收盤決策，08:00 開盤成交"},
    {"id": "demo_e5_min5_x175_t05top20", "name": "E5：最小下單額修正（門檻 0.5% 權益＋前 20 大）", "capital_usdt": 5000.0, "venue": "幣安測試網永續",
     "spec": "research_minnot_1026/e5_minnot_variants.py（thr=0.5% S5 權益、top-20 S5 宇集，訊號同 E5）；2026-10-26 承接 E5 帳本與權重",
     "decision": "每日 08:00（台北）收盤決策，08:00 開盤成交"},
]


def view(state: dict | None) -> list[dict]:
    st = (state or {}).get("demo_candidates") or {}
    out = []
    for c in CANDIDATES:
        g = st.get(c["id"]) or {}
        ordering, parity = bool(g.get("cloud_ordering_ok")), bool(g.get("parity_pass"))
        acc = accepted(c["id"])
        out.append({**c, "enabled": bool(g.get("approved") and g.get("enabled")), "cloud_ordering_ok": ordering, "parity_pass": parity,
                    "parity_detail": g.get("parity_detail"), "ordering_detail": g.get("ordering_detail"), "accepted": acc, "approvable": ordering and parity and acc and not g.get("approved"),
                    "approved": bool(g.get("approved")), "approved_at": g.get("approved_at"), "approved_by": g.get("approved_by"),
                    "status": ("等待雲端下單測試與逐筆對帳" if not (ordering and parity)
                               else "等待資金控管驗收" if not acc else "已批准" if g.get("approved") else "可審核")})
    return out


def weekly_breach(live_equity: float, backtest_equity: float) -> bool:
    if not backtest_equity:
        return False
    return abs(live_equity / backtest_equity - 1) > WEEKLY_DEV_LIMIT
