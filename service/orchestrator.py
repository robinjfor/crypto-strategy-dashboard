"""DEMO-only portfolio orchestrator (research_orchestrator 'regime' variant, spec_2026-11/research_orchestrator).

Scales slot *sizing capital* only; never touches entry/exit signals, stops, approvals or ledgers.
Rules (regime formula unchanged; base weights E 85% / R2S 15% from the 2026-11-12 08:05 Taipei plan):
  base weights, 1x:  E(demo_e5) 85% / U1 0% / R2S 15% / FA 0%
  bear   = BTC daily close < 200d SMA (last CLOSED bar)  -> E and U1 halved; freed weight 50/50 to R2S and FA
  hi-vol = BTC 30d realized vol (log, ann. sqrt365) > expanding 66.7th pct of its own history (min 365 obs,
           history from 2022-06-01 = the backtest panel start)  -> every target x0.7 (rest stays cash)
  NO drawdown breaker. DD from peak > 35% only raises an alert flag (log + /status), no trading action.
Quotes: E5 / R2S / FA are USDT virtual ledgers. U1 is a USDC sleeve. E and R2S trade USDT-M.
The allocation pool is one USD number: E + R2S + USDC sleeve + FA, with USDC counted 1:1 as USD.
Nothing is converted or transferred. E and R2S take their weight of that USD pool as USDT sizing.
VBO (demo_vbo1h_top50_1k) is not an orchestrator weight and its book is not added again: the FA
ledger already holds the 1000U that was carved for it. VBO's capital and positions stay as they are.
A quote-pool plan may size a USDT slot above its own virtual ledger, because the sibling USDT ledger is the
same wallet. The old min(target, own ledger) left R2S cash idle whenever E's weight exceeded E's ledger.
U1 weight 0 does not flatten the sleeve (see u1_live.orchestrator_size_equity). U1 new entries stop via
paused_slots; approval is kept.
Runs as Cloud Run job crypto-orchestrator-daily at 00:05 UTC (08:05 Taipei), after the UTC daily bar
closes. The 00:08 UTC reconcile is the one that should fill that open. Earlier reconcile ticks (the
hourly trader at 00:01, the FA job at 00:01, the E5 target job at 00:02) still see yesterday's plan;
they must not claim the bar (plan_covers_open). A claim sets last_bar and the 00:08 tick cannot resize.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("orchestrator")

SLOTS = {"E": "demo_e5_min5_x175_t05top20",  # took over E5 (demo_e5_min5_x175) weight on 2026-10-26
          "U": "u1_live", "R": "demo_r2s_top10_3k", "F": "demo_funding_arb_1x"}
# Base weights from the 2026-11-12 08:05 Taipei plan, replacing E 70% / R2S 30%.
# U1 stays paused (approval kept, 2026-11-11 02:14). FA base stays 0.
BASE_W = {"E": 0.85, "U": 0.0, "R": 0.15, "F": 0.0}
# Ceiling on sizing equity. Does not change BASE_W and does not give the unused share to E.
# R2S single-config replay MaxDD is 49%, above the 45% limit, so sizing stays at the equity
# the live book was built on (2026-11-11). State["slot_caps"] overrides this; equity null clears it.
DEFAULT_SLOT_CAPS = {
    "demo_r2s_top10_3k": {"equity": 1464.08, "reason": "r2s_mdd_review_20261111",
                          "closes_positions": False, "revokes_approval": False},
}
DEFAULT_CAP = {"E": 5000.0, "U": 1000.0, "R": 3000.0, "F": 1000.0}
QUOTE = {"E": "USDT", "U": "USDC", "R": "USDT", "F": "USDT"}
# One USD pool. USDC is counted 1:1 into USDT-M sizing; no coin moves. VBO's book is not a fifth addend.
USDT_KEYS = ("E", "R")
POOL_ACCOUNTING = "usd_1to1"
VBO_SLOT = "demo_vbo1h_top50_1k"
VBO_RESERVE_USDT = 1000.0   # carved from FA idle on 2026-11-04; VBO is not resized here
U1_PAUSE_SLOT = "u1_btc_eth_usdc"
U1_PAUSE_MIGRATION = "u1_pause_20261111"
U1_PAUSE_REASON = ("Robin 2026-11-11 02:14 Asia/Taipei: pause U1 new entries and increasing rebalances. "
                   "Approval kept. Existing positions are not closed by this pause.")
HIVOL_X = 0.7
DD_ALERT = 0.35
MA_N, VOL_N, VOL_MIN_HIST, VOL_Q = 200, 30, 365, 2 / 3
HIST_START = "2022-06-01"
FAPI = os.environ.get("FAPI_BASE_URL", "https://fapi.binance.com")
GCS_PREFIX = "research/orchestrator"
MAX_AGE = timedelta(hours=30)   # a plan older than this is ignored (executors fall back to their fixed capital)


# ---------------- pure rules (tested against the backtest code)
def regime(close: pd.Series) -> dict:
    """close: BTC daily closes (closed bars only, ascending). Decision for the next day uses the last value."""
    close = close.dropna().astype(float)
    ma = close.rolling(MA_N).mean()
    vol = np.log(close).diff().rolling(VOL_N).std() * np.sqrt(365)
    thr = vol.expanding(VOL_MIN_HIST).quantile(VOL_Q)
    c, m, v, t = (float(x.iloc[-1]) if not pd.isna(x.iloc[-1]) else None for x in (close, ma, vol, thr))
    return {"bar": str(close.index[-1].date()), "btc_close": c, "ma200": m, "vol30": v, "vol_thr": t,
            "bear": bool(m is not None and c < m), "hivol": bool(v is not None and t is not None and v > t),
            "vol_hist_n": int(vol.notna().sum())}


def weights(bear: bool, hivol: bool) -> tuple[dict, float]:
    """Backtest regime formula. Base weights changed; the bear/hi-vol arithmetic did not.
    Bear halves E and U, then splits that freed weight 50/50 onto R and F. With U already 0,
    halving U frees nothing. With F already 0, bear still assigns F a positive weight
    (half of the halved E). Hi-vol does not change weights; it returns the 0.7 multiplier."""
    w = dict(BASE_W)
    if bear:
        fr = (w["E"] + w["U"]) / 2; w["E"] /= 2; w["U"] /= 2; w["R"] += fr / 2; w["F"] += fr / 2
    return w, (HIVOL_X if hivol else 1.0)


def _eq(equities: dict, k: str) -> float:
    try:
        return max(0.0, float(equities.get(k) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def eligible_pools(equities: dict) -> dict:
    """One USD allocation pool for every sleeve weight.

    pool = E ledger + R2S ledger + USDC sleeve + FA ledger.
    USDC and USDT are both counted as 1 USD. No coin is converted or moved; E and R2S, which
    trade USDT-M, receive their weight of this USD pool as USDT sizing equity. A target may
    sit above one virtual book because the books are an accounting split.
    VBO's own book is not added. The FA ledger already includes the 1000U carved for VBO on
    2026-11-04, and adding VBO again would count that 1000 twice. VBO is not resized here.
    """
    e = {k: _eq(equities, k) for k in BASE_W}
    reserve = min(VBO_RESERVE_USDT, e["F"])
    usd = e["E"] + e["R"] + e["U"] + e["F"]
    return {"usd": usd, "usdt": e["E"] + e["R"], "usdc": e["U"], "fa": e["F"],
            "vbo_reserve_usdt": reserve, "vbo_in_pool": False, "accounting": POOL_ACCOUNTING,
            "by_slot": e}


def _pool_for(k: str, pools: dict) -> float:
    return float(pools["usd"])


def effective_slot_caps(state: dict | None = None) -> dict:
    """Code default, then state['slot_caps']. A stored equity of null removes that cap."""
    out = {k: dict(v) for k, v in DEFAULT_SLOT_CAPS.items()}
    for sid, spec in ((state or {}).get("slot_caps") or {}).items():
        if spec is None or not isinstance(spec, dict) or spec.get("equity") is None:
            out.pop(sid, None)
        else:
            out[sid] = dict(spec)
    return out


def equity_cap(slot_id: str, state: dict | None = None) -> float | None:
    table = effective_slot_caps(state) if state is not None else (
        _SLOT_CAPS if _SLOT_CAPS is not None else effective_slot_caps(None))
    spec = (table or {}).get(slot_id)
    if not spec or spec.get("equity") is None:
        return None
    try:
        return max(0.0, float(spec["equity"]))
    except (TypeError, ValueError):
        return None


def apply_equity_cap(slot_id: str, value: float | None) -> float | None:
    if value is None:
        return None
    cap = equity_cap(slot_id)
    if cap is None:
        return value
    return min(float(value), cap)


def ensure_slot_caps(state: dict) -> dict:
    """Seed missing caps. Does not overwrite a value already stored, including an explicit clear."""
    cur = dict(state.get("slot_caps") or {})
    for sid, spec in DEFAULT_SLOT_CAPS.items():
        cur.setdefault(sid, dict(spec))
    state["slot_caps"] = cur
    return effective_slot_caps(state)


def plan(close: pd.Series, equities: dict, prev: dict | None, now: datetime, caps: dict | None = None) -> dict:
    rg = regime(close)
    w, x = weights(rg["bear"], rg["hivol"])
    pools = eligible_pools(equities)
    # P is mark-to-market of the four orchestrator books (DD alert only). It is not the sizing base.
    P = float(sum(_eq(equities, k) for k in BASE_W))
    peak = max(P, float((prev or {}).get("peak") or 0.0))
    dd = 1 - P / peak if peak > 0 else 0.0
    raw = {k: round(_pool_for(k, pools) * w[k] * x, 2) for k in w}
    caps = effective_slot_caps(None) if caps is None else caps
    tgt, held_cash, cap_notes = {}, {}, []
    for k, raw_v in raw.items():
        spec = caps.get(SLOTS[k]) or {}
        try:
            cap_v = None if spec.get("equity") is None else max(0.0, float(spec["equity"]))
        except (TypeError, ValueError):
            cap_v = None
        if cap_v is not None and raw_v > cap_v:
            tgt[k] = round(cap_v, 2)
            held_cash[SLOTS[k]] = round(raw_v - cap_v, 2)
            cap_notes.append(f"{SLOTS[k]}: sizing capped at {cap_v:.2f} {QUOTE[k]} "
                             f"(weight target {raw_v:.2f}; leftover stays cash; reason {spec.get('reason')})")
        else:
            tgt[k] = raw_v
    prev_t = (prev or {}).get("targets") or {}
    actions = list(cap_notes)
    for k in w:
        a, b = prev_t.get(SLOTS[k]), tgt[k]
        if a is None:
            actions.append(f"{SLOTS[k]}: set sizing capital {b:.2f} {QUOTE[k]} (was fixed {DEFAULT_CAP[k]:.0f})")
        elif abs(a - b) >= 0.01:
            actions.append(f"{SLOTS[k]}: sizing capital {a:.2f} -> {b:.2f} {QUOTE[k]}")
    alert = dd > DD_ALERT
    if alert:
        actions.append(f"ALERT: portfolio drawdown {dd:.1%} > {DD_ALERT:.0%} (alert only, no trading action)")
    if rg["bear"] and w["F"] > 0:
        actions.append("bear: FA weight is non-zero under the existing halve-E rule; base FA weight is still 0. "
                       "VBO's 1000U is not part of this target.")
    if rg["hivol"]:
        actions.append("hi-vol: every target x0.7; the other 30% stays cash (backtest rule)")
    quote_pool = {SLOTS[k]: round(_pool_for(k, pools), 2) for k in w}
    return {"schema": "orchestrator_daily_v2", "generated_at": now.isoformat(), "decided_bar": rg["bar"],
            "sizing": "quote_pool",
            "regime": rg, "weights": {SLOTS[k]: round(w[k], 4) for k in w},
            "base_weights": {SLOTS[k]: BASE_W[k] for k in BASE_W}, "exposure_mult": x,
            "targets": {SLOTS[k]: tgt[k] for k in w},
            "uncapped_targets": {SLOTS[k]: raw[k] for k in w},
            "slot_caps": caps, "cash_held_by_cap": held_cash,
            "target_quote": {SLOTS[k]: QUOTE[k] for k in w},
            "quote_pool": quote_pool,
            "eligible": {"usd": round(pools["usd"], 2), "usdt": round(pools["usdt"], 2),
                         "usdc": round(pools["usdc"], 2), "fa": round(pools["fa"], 2),
                         "vbo_reserve_usdt": round(pools["vbo_reserve_usdt"], 2),
                         "vbo_in_pool": False, "accounting": pools["accounting"],
                         "vbo_slot": VBO_SLOT},
            "equity": {SLOTS[k]: round(float(equities[k]), 2) for k in equities}, "portfolio_equity": round(P, 2),
            "peak": round(peak, 2), "drawdown": round(dd, 4), "dd_alert": alert, "dd_alert_threshold": DD_ALERT,
            "actions": actions,
            "notes": ("Targets = regime weight × the USD pool × hi-vol multiplier. "
                      "The pool is E + R2S + the USDC sleeve + the FA ledger, valued 1:1 in USD. "
                      "No coin is converted or transferred. E and R2S trade USDT-M at that USDT sizing. "
                      "VBO's book is not added (the FA ledger already contains its 1000U) and VBO is not resized. "
                      "U1 weight 0 does not close U1. A slot_caps ceiling keeps the unused share as cash "
                      "and does not raise E. No DD breaker.")}


def policy_public() -> dict:
    """Configured policy for /status. Percents come from this code, not from the last stored plan."""
    flat, x0 = weights(False, False)
    bear, _ = weights(True, False)
    hv, xh = weights(False, True)
    both, xb = weights(True, True)

    def pack(w: dict, x: float) -> dict:
        return {"weights": {SLOTS[k]: round(w[k], 4) for k in w},
                "weights_keys": {k: round(w[k], 4) for k in w}, "exposure_mult": x}

    return {"approved_by": "Robin", "approved_at": "2026-11-11T02:14:00+08:00",
            "base_weights": {SLOTS[k]: BASE_W[k] for k in BASE_W},
            "base_weights_keys": {k: BASE_W[k] for k in BASE_W},
            "u1_pause": {"slot": U1_PAUSE_SLOT, "mechanism": "paused_slots", "migration": U1_PAUSE_MIGRATION,
                         "revokes_approval": False, "closes_positions": False,
                         "blocks": "new entries and increasing rebalances",
                         "still_runs": "signal exits, reducing rebalances, sleeve liquidation, account-margin derisk"},
            "vbo": {"slot": VBO_SLOT, "reserve_usdt": VBO_RESERVE_USDT, "in_orchestrator_weights": False},
            "regimes": {"flat": pack(flat, x0), "bear": pack(bear, 1.0), "hivol": pack(hv, xh),
                        "bear_hivol": pack(both, xb)},
            "base_weights_effective": "2026-11-12T08:05:00+08:00",
            "regime_notes": [
                "基礎權重自 2026-11-12 08:05（台北）改為 E 85% / R2S 15% / U1 0 / 資金費套利 0，取代 E 70% / R2S 30%。U1 仍暫停，核准保留。",
                "空頭仍把 E 與 U1 權重減半，釋出的權重各半給 R2S 與資金費套利。U1 基礎權重是 0，減半不會釋出 USDC。資金費套利基礎權重是 0，但空頭仍會分到 E 釋出的一半（E 85% 時為 21.25%）。這是原本的空頭公式，不是新的基礎權重。",
                "高波動把每個目標乘上 0.7，剩下 30% 依回測規則留成現金。",
                "配置池＝E 帳本＋R2S 帳本＋USDC 袖套＋資金費套利帳本，USDC 與 USDT 都以 1 美元計，不轉換、不搬幣。E 與 R2S 拿這個池的權重當 USDT-M 的 sizing。VBO 自己的帳本不另加（資金費套利裡的 1,000 就是那筆劃出），VBO 的資金與部位不改。",
                "U1 暫停只擋新開倉與加碼再平衡。減碼、訊號出場、強平與保證金去風險仍會下減少倉單。權重 0 不會把 U1 砍平。是否平掉 U1、USDC 要不要轉走，需要另外決定。",
                "策略自己的 25% 再平衡帶仍有效。",
                "R2S 單一參數回測最大回撤約 49%，超過 45% 上限。sizing 上限仍是 1,464.08 USDT（原因 r2s_mdd_review_20261111）。15% 的目標低於這個上限時用 15% 的金額，上限不生效。超過上限的差額留成現金，不加給 E，也不因為這個上限而減倉或撤銷核准。",
            ],
            "slot_caps": effective_slot_caps(None)}


def apply_u1_pause(state: dict, *, now_iso: str | None = None) -> dict:
    """One-shot paused_slots write. Does not revoke approval, disable the slot, or touch positions.

    A later /control/resume_slot lifts it; this does not re-pause after the migration is recorded.
    Resume then stays lifted until someone pauses again. The policy constant remains the record of
    Robin's decision; clearing the pause in production is an operations choice, not done here.
    """
    mg = state.setdefault("migrations", {})
    if (mg.get(U1_PAUSE_MIGRATION) or {}).get("done"):
        return {"applied": False, "already": True, "slot": U1_PAUSE_SLOT}
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    slots = [U1_PAUSE_SLOT]
    sleeve_slot = str(((state.get("u1_live") or {}).get("slot")) or "")
    if sleeve_slot and sleeve_slot not in slots:
        slots.append(sleeve_slot)
    ps = dict(state.get("paused_slots") or {})
    for sid in slots:
        ps.setdefault(sid, {"paused_at": now_iso, "paused_by": "policy", "reason": U1_PAUSE_REASON,
                            "approval": "kept", "closes_positions": False})
    state["paused_slots"] = ps
    mg[U1_PAUSE_MIGRATION] = {"done": True, "at": now_iso, "slot": U1_PAUSE_SLOT, "closes_positions": False,
                              "revokes_approval": False}
    state["ledger_adjustments"] = (list(state.get("ledger_adjustments") or []) + [{
        "at": now_iso, "kind": "u1_pause", "slot": U1_PAUSE_SLOT, "closes_positions": False,
        "approval": "kept", "orders": 0,
        "note": U1_PAUSE_REASON}])[-200:]
    return {"applied": True, "slot": U1_PAUSE_SLOT, "paused_slots": slots, "orders": 0}


# ---------------- consumer side (executors). Never raises; None/stale plan => unchanged fixed sizing.
_ACTIVE: dict | None = None
_SLOT_CAPS: dict | None = None


def load(state: dict | None) -> dict | None:
    global _ACTIVE, _SLOT_CAPS
    _ACTIVE = (state or {}).get("orchestrator") or None
    _SLOT_CAPS = effective_slot_caps(state)
    return _ACTIVE


def sizing_capital(slot_id: str, default: float, now: datetime | None = None, plan_doc: dict | None = None) -> float:
    p = plan_doc if plan_doc is not None else _ACTIVE
    try:
        if not p or os.environ.get("ORCHESTRATOR_ENABLED", "1") != "1":
            return apply_equity_cap(slot_id, default)
        at = datetime.fromisoformat(str(p["generated_at"]))
        if (now or datetime.now(timezone.utc)) - at > MAX_AGE:
            log.warning("orchestrator_plan_stale at=%s -> fixed capital for %s", p.get("generated_at"), slot_id)
            return apply_equity_cap(slot_id, default)
        v = (p.get("targets") or {}).get(slot_id)
        if v is None:
            return apply_equity_cap(slot_id, default)
        v = max(0.0, float(v))
        if p.get("sizing") == "quote_pool":
            # Target is already weight × the quote pool. Do not also cap at this slot's own
            # virtual ledger: E and R2S share the USDT-M wallet, and that cap left the sibling
            # book's idle USDT undeployed. Still never exceed the pool recorded on the plan.
            pool = (p.get("quote_pool") or {}).get(slot_id)
            if pool is not None:
                v = min(v, max(0.0, float(pool)))
        else:
            # Plans written before quote_pool sizing stay capped at the slot ledger.
            v = v if default is None else min(v, max(0.0, float(default)))
        return apply_equity_cap(slot_id, v)
    except Exception as e:  # noqa: BLE001
        log.warning("orchestrator_sizing_fail %s", e)
        return apply_equity_cap(slot_id, default)


def plan_covers_open(now: datetime | None = None, plan_doc: dict | None = None) -> tuple[bool, str]:
    """True when the loaded plan was generated on this UTC date.

    Sizing for the 08:00 Taipei open is written at 00:05 UTC. Reconcile also runs at
    00:01 (hourly trader, and the FA job once its targets are up) and at 00:02 (E5
    targets job). Those ticks load yesterday's targets. If they claim the bar, last_bar
    blocks the 00:08 tick that can see today's plan, and the new weights miss the open.
    ORCHESTRATOR_ENABLED=0 keeps the fixed-capital path and does not wait.
    """
    if os.environ.get("ORCHESTRATOR_ENABLED", "1") != "1":
        return True, ""
    p = plan_doc if plan_doc is not None else _ACTIVE
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not p or not p.get("generated_at"):
        return False, "waiting for today's orchestrator plan (none loaded)"
    try:
        at = datetime.fromisoformat(str(p["generated_at"]))
    except Exception:
        return False, "waiting for today's orchestrator plan (bad timestamp)"
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    at = at.astimezone(timezone.utc)
    if at.date() != now.date():
        return False, f"waiting for today's orchestrator plan (loaded plan is {at.isoformat()})"
    return True, ""


# ---------------- job side (Cloud Run only: Binance calls)
def btc_closes(now: datetime) -> pd.Series:
    import requests
    rows, start = [], int(pd.Timestamp(HIST_START, tz="UTC").timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    while True:
        r = requests.get(FAPI + "/fapi/v1/klines", params={"symbol": "BTCUSDT", "interval": "1d", "startTime": start, "limit": 1500}, timeout=20)
        r.raise_for_status(); k = r.json()
        rows += [x for x in k if int(x[6]) < end_ms]   # closed bars only
        if len(k) < 1500:
            break
        start = int(k[-1][0]) + 1
    s = pd.Series({pd.Timestamp(int(x[0]), unit="ms", tz="UTC"): float(x[4]) for x in rows}).sort_index()
    return s[~s.index.duplicated()]


def _book_eq(ex: dict | None, default: float, px) -> float:
    if not ex:
        return default
    return float(ex.get("cash", default)) + sum(float(q) * px(s) for s, q in (ex.get("book") or {}).items() if q)


def slot_equities(state: dict, fut) -> dict:
    cache: dict = {}

    def px(s):
        if s not in cache:
            cache[s] = float(fut.mark_price(s))
        return cache[s]
    eq = {"E": _book_eq(state.get("demo_e5v_exec") or state.get("demo_e5_exec"), DEFAULT_CAP["E"], px),
          "R": _book_eq(state.get("demo_r2s_exec"), DEFAULT_CAP["R"], px)}
    fa = DEFAULT_CAP["F"]   # FA has no cash ledger: capital + unrealized spot/perp legs (funding not tracked)
    for s, p in (state.get("demo_fa_positions") or {}).items():
        try:
            m = px(s); fa += float(p.get("spot_qty") or 0) * (m - float(p.get("spot_px") or m)) \
                + float(p.get("perp_qty") or 0) * (float(p.get("perp_px") or m) - m)
        except Exception:  # noqa: BLE001
            pass
    eq["F"] = fa
    sl = state.get("u1_live")
    if sl:
        import u1_live
        pm = u1_live.perp_map(sl.get("quote") or "USDC")
        eq["U"] = u1_live.sleeve_equity(sl, {k: (px(pm[k]) if float((sl.get("units") or {}).get(k) or 0) else 0.0) for k in u1_live.SYMS})
    else:
        eq["U"] = DEFAULT_CAP["U"]
    return eq


def run(dry_run: bool = False, now: datetime | None = None) -> dict:
    from futures_client import FuturesDemoClient
    from state_store import StateStore
    now = now or datetime.now(timezone.utc)
    store = StateStore(); state = store.load()
    prev = state.get("orchestrator")
    doc = plan(btc_closes(now), slot_equities(state, FuturesDemoClient()), prev, now,
               caps=effective_slot_caps(state))
    doc["dry_run"] = bool(dry_run)
    if doc["dd_alert"]:
        log.error("ORCHESTRATOR_DD_ALERT dd=%.4f peak=%.2f equity=%.2f", doc["drawdown"], doc["peak"], doc["portfolio_equity"])
    bucket = os.environ.get("GCS_BUCKET")
    name = f"{GCS_PREFIX}/{now:%Y-%m-%d}.json"
    body = json.dumps(doc, indent=1, default=str)
    if bucket:
        from google.cloud import storage
        storage.Client().bucket(bucket).blob(name).upload_from_string(body, content_type="application/json")
        print(f"wrote gs://{bucket}/{name}")
    if not dry_run:
        def mut(s):
            s["orchestrator"] = {**doc, "history": ((s.get("orchestrator") or {}).get("history") or [])[-399:]
                                 + [{"bar": doc["decided_bar"], "P": doc["portfolio_equity"], "dd": doc["drawdown"],
                                     "bear": doc["regime"]["bear"], "hivol": doc["regime"]["hivol"]}]}
            apply_u1_pause(s, now_iso=now.isoformat())
            ensure_slot_caps(s)
            return s
        store.mutate(mut)
    print(body)
    return doc


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    dry = "--dry-run" in sys.argv or os.environ.get("ORCH_DRY_RUN") == "1"
    run(dry_run=dry)
    sys.exit(0)
