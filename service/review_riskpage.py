"""Extra risk-page fields. Display only. Does not change positions."""
from __future__ import annotations

import review_audit as audit


def _f(value):
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def legs(status: dict, state: dict) -> list[dict]:
    view = audit.annotate_positions(status, state)
    out = []
    for row in view["positions"]:
        qty = _f(row.get("side_qty"))
        if qty is None:
            qty = audit._side_qty(row)
        mark = _f(row.get("mark"))
        entry = _f(row.get("entry")) or mark
        wallet = _f(row.get("isolated_wallet"))
        lev = row.get("leverage")
        eff = None
        if qty and entry and wallet and wallet > 0:
            eff = round(abs(qty) * entry / wallet, 4)
        owners = row.get("owners") or []
        owner = row.get("owner")
        if isinstance(owner, list):
            owner_text = ",".join(str(x.get("owner")) for x in owner)
        else:
            owner_text = owner or "unowned"
        flagged = []
        if lev == 3:
            flagged.append("3x")
        if eff is not None and eff > 1.75:
            flagged.append("eff_gt_1_75")
        notional = None if qty is None or mark is None else abs(qty * mark)
        out.append({
            "symbol": row.get("raw_symbol"),
            "side": "SHORT" if (qty or 0) < 0 else ("LONG" if qty else "missing"),
            "exchange_leverage": lev if lev is not None else "missing",
            "isolated_wallet": wallet if wallet is not None else "missing",
            "effective_leverage": eff if eff is not None else "missing",
            "mark": mark if mark is not None else "missing",
            "liq": row.get("liq") if row.get("liq") is not None else "missing",
            "liq_distance": row.get("liq_distance") if row.get("liq_distance") is not None else "missing",
            "owner": owner_text,
            "strategy": owners[0]["strategy"] if len(owners) == 1 else (row.get("strategy") or "unowned"),
            "notional": None if notional is None else round(notional, 2),
            "flags": flagged,
        })
    return out


def shorts_squeezed(status: dict, state: dict) -> dict:
    """All shorts marked up 100%. Longs are unchanged. Isolated names that cross liq lose the wallet."""
    view = audit.annotate_positions(status, state)
    total = 0.0
    n = 0
    for row in view["positions"]:
        qty = _f(row.get("side_qty"))
        if qty is None or qty >= 0:
            continue
        leg = audit._shock_pnl(row, 1.0)
        total += leg["pnl"]
        if leg.get("liquidated"):
            n += 1
    return {"shock": 1.0, "side": "short", "pnl": round(total, 2), "loss": round(max(0.0, -total), 2), "liquidations": n}


def page(status: dict, state: dict, risk: dict) -> dict:
    status = status if isinstance(status, dict) else {}
    basis = status.get("capital_basis") if isinstance(status.get("capital_basis"), dict) else {}
    nav = _f(basis.get("nav"))
    table = legs(status, state)
    gross = (risk.get("gross_long") or 0) + (risk.get("gross_short") or 0)
    ranked = sorted((r for r in table if isinstance(r.get("notional"), (int, float))), key=lambda r: -r["notional"])
    top = []
    for row in ranked[:5]:
        share = None if not gross else round(row["notional"] / gross, 4)
        top.append({"symbol": row["symbol"], "notional": row["notional"], "share": share if share is not None else "missing"})
    squeeze = shorts_squeezed(status, state)
    down = (risk.get("stress_down_30") or {}).get("loss")
    def ratio(loss):
        if nav is None or not nav or loss is None:
            return "missing"
        return round(float(loss) / float(nav), 6)
    guards = status.get("guards") if isinstance(status.get("guards"), dict) else {}
    if "entry_blocked" in guards or status.get("paused") is not None:
        halted = bool(status.get("paused") or guards.get("entry_blocked"))
        halt = "halted" if halted else "not_halted"
    else:
        halt = "missing"
    worst = risk.get("worst_day_loss")
    return {
        "current_drawdown": basis.get("current_drawdown") if basis.get("current_drawdown") is not None else "missing",
        "max_drawdown_pct": basis.get("max_drawdown_pct") if basis.get("max_drawdown_pct") is not None else "missing",
        "top5": top,
        "correlation": {"display": "missing", "label_zh": "沒有各幣日報酬，相關熱圖 missing"},
        "stress_loss_down_30": ratio(down),
        "stress_loss_short_squeeze": ratio(squeeze["loss"]),
        "short_squeeze": squeeze,
        "daily_loss": worst if worst is not None else "missing",
        "daily_loss_ratio": ratio(worst),
        "halt": halt,
        "legs": table,
        "flagged_legs": [r for r in table if r["flags"]],
    }
