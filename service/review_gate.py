"""Real-money readiness. Reads the config Robin approves. Does not trade."""
from __future__ import annotations

import json
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / "config" / "readiness_gate.json"

GATES = (
    ("G1", "Demo／影子連續 90 天"),
    ("G2", "扣費後日報酬均值 > 0，且 95% 區塊拔靴下限 > 0"),
    ("G3", "實盤與回測月報酬連續 3 個月落在 ±3 個百分點"),
    ("G4", "E5 對帳連續 21 個交易日、沒有未解釋差額、每日 TE < 0.5%"),
    ("G5", "每條腿有效槓桿 ≤ 1.75，且回測使用逐幣強平模型"),
    ("G6", "壓力損失在 Robin 核准的預算內（最大回撤 50%）"),
    ("G7", "對帳連續 21 日差額為 0"),
    ("G8", "資金橋差額絕對值 ≤ 0.5 USDT"),
    ("G9", "沒有未處理的嚴重審閱評論"),
)


def load_config(path: Path | None = None) -> dict:
    raw_path = path or CONFIG
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data


def _shown(value):
    if value is None or value == "" or value == "missing":
        return "缺值"
    return value


def _status_row(gid, name, status, value, threshold) -> dict:
    label = {"met": "達到", "not_met": "未達到", "unverified": "未驗證"}.get(status, "未驗證")
    return {
        "id": gid,
        "name_zh": name,
        "threshold": threshold,
        "value": _shown(value),
        "status": status,
        "label_zh": label,
    }


def unresolved_critical(comments: list | None) -> list:
    """Critical comments that are not done and not rejected."""
    out = []
    for c in comments or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("severity") or "").lower() != "critical":
            continue
        if str(c.get("status") or "open") in ("done", "rejected"):
            continue
        out.append(c)
    return out


def evaluate(status: dict | None, state: dict | None, *, comments: list | None = None, config_path: Path | None = None) -> dict:
    import review_bridge
    import review_credibility
    cfg = load_config(config_path)
    approved = bool(cfg.get("approved"))
    status = status if isinstance(status, dict) else {}
    state = state if isinstance(state, dict) else {}
    cred = review_credibility.build(status, state)
    bridge = review_bridge.build_bridge(status, state)
    hist = status.get("nav_history") or []
    days = len(hist) if isinstance(hist, list) else 0
    g1 = "met" if days >= 90 else ("not_met" if days else "unverified")
    parity_days = cred.get("parity_days") or 0
    te = cred.get("parity_te")
    if parity_days == 0 or te in (None, "missing", "缺值"):
        g4 = "unverified"
    elif parity_days < 21 or not (isinstance(te, (int, float)) and te < 0.005):
        g4 = "not_met"
    else:
        g4 = "met"
    diff = bridge.get("bridge_diff_value")
    if diff is None:
        g8 = "unverified"
    else:
        g8 = "met" if abs(diff) <= 0.5 else "not_met"
    if comments is None:
        g9 = "unverified"
        g9_value = None
    else:
        g9_value = len(unresolved_critical(comments))
        g9 = "not_met" if g9_value else "met"
    rows = [
        _status_row("G1", GATES[0][1], g1, days or None, "90 個連續日"),
        _status_row("G2", GATES[1][1], "unverified", None, "均值 > 0 且拔靴下限 > 0"),
        _status_row("G3", GATES[2][1], "unverified", None, "連續 3 個月 ±3 個百分點"),
        _status_row("G4", GATES[3][1], g4, {"parity_days": parity_days, "te": _shown(te)}, "21 日且 TE < 0.5%"),
        _status_row("G5", GATES[4][1], "unverified", None, "有效槓桿 ≤ 1.75 且逐幣強平模型"),
        _status_row("G6", GATES[5][1], "unverified", None, "最大回撤 50% 以內，預算需 Robin 核准"),
        _status_row("G7", GATES[6][1], "unverified", None, "21 日差額為 0"),
        _status_row("G8", GATES[7][1], g8, diff, "絕對差額 ≤ 0.5 USDT"),
        _status_row("G9", GATES[8][1], g9, g9_value, "沒有未處理的 critical"),
    ]
    any_not_met = any(r["status"] == "not_met" for r in rows)
    all_met = all(r["status"] == "met" for r in rows)
    ready = bool(approved and all_met and not any_not_met)
    if any_not_met or not approved or not all_met:
        page = "not ready"
        label = "未就緒"
    else:
        page = "ready"
        label = "就緒"
    return {
        "ok": True,
        "env": "demo",
        "page_status": page,
        "label_zh": label,
        "config_approved": approved,
        "approver": _shown(cfg.get("approved_by")),
        "approved_at": _shown(cfg.get("approved_at")),
        "gates": rows,
        "not_thresholds": ["625 USDT/月", "回測年報酬"],
        "note": "任一門檻未達到，整頁就是未就緒。設定檔未核准時也是未就緒。",
    }
