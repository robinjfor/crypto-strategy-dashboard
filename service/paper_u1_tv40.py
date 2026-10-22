"""U1 tv0.40 cap3 shadow book (影子帳本) — paper only, never places orders, never touches trader state or books.
Same engine as paper_u1 (identical open/close phases, signals and fills at the 00:00 UTC open), with the
account target vol lowered from 0.60 to 0.40 (cap 3 unchanged). Starts flat with 1000 USDC at creation.
The sigma60 history of the 1x book is copied from the live paper U1 at creation (the 1x book does not depend
on the account target vol), so leverage is computed from day one exactly as the engine would."""
from __future__ import annotations

import contextlib
import logging
import os
from datetime import datetime, timedelta, timezone

import pandas as pd

import paper_u1 as P

log = logging.getLogger(__name__)
CODE = "U1-tv0.40"
ACCT_TV = 0.40
LEV_CAP = 3.0
INITIAL = 1000.0
OBJECT = os.environ.get("PAPER_U1_TV40_OBJECT", "trader/paper/u1_tv40_state.json")
LOCAL = os.environ.get("PAPER_U1_TV40_LOCAL_PATH", "/tmp/paper_u1_tv40_state.json")


@contextlib.contextmanager
def params():
    old = (P.ACCT_TV, P.LEV_CAP)
    P.ACCT_TV, P.LEV_CAP = ACCT_TV, LEV_CAP
    try:
        yield
    finally:
        P.ACCT_TV, P.LEV_CAP = old


START_FILL_BAR = "2026-10-21T00:00:00+00:00"   # same start as live U1 and the analyst's shadow ledger


def _aligned_start(main_state: dict | None):
    """Decision for the 10/21 00:00 UTC fill, re-levered with tv0.40 (w1x and sigma60 are tv-independent)."""
    if not main_state:
        return None
    dec = next((d for d in main_state.get("decisions") or [] if d.get("fill_bar") == START_FILL_BAR), None)
    if not dec:
        return None
    day = dec["indicator_day"]
    closes = main_state.get("e1x_closes") or []
    rets = list(main_state.get("returns_1x") or [])
    dates = [c[0] for c in closes][-len(rets):] if rets else []
    rets = [r for d, r in zip(dates, rets) if d <= day][-P.SIG60_WIN:]
    with params():
        L, s60 = P.leverage_from_returns(rets)
        tl = P.lev_targets(dec["w1x"], L)
    pend = {**{k: dec[k] for k in ("decision_bar", "fill_bar", "indicator_day", "w1x")}, "L": L, "sigma60_1x": s60,
            "targets_1x": dict(dec["w1x"]), "targets_lev": tl}
    return rets, pend


def initial_state(now: datetime, main_state: dict | None) -> dict:
    st = P.new_engine_state()
    st["books"] = {"x1": P.new_book(INITIAL), "lev": P.new_book(INITIAL)}
    st["e1x_prev"] = INITIAL
    al = _aligned_start(main_state)
    if al:
        st["returns_1x"], st["pending"] = al
        st["decisions"] = [al[1]]
        st["last_closed_bar"] = al[1]["decision_bar"]
    else:
        if main_state:
            st["returns_1x"] = list(main_state.get("returns_1x") or [])[-P.SIG60_WIN:]
        cur = pd.Timestamp(now.astimezone(timezone.utc)).floor("h")
        st["last_closed_bar"] = P._iso(cur - P.H)
    st.update({"code": CODE, "mode": "paper_only", "label_zh": "影子帳本（模擬，不下單）", "orders": "none",
               "params": {"acct_tv": ACCT_TV, "lev_cap": LEV_CAP, "asset_tv": P.ASSET_TV, "n_sma": P.N_SMA},
               "paper_start": {"bar": st["last_closed_bar"], "equity": INITIAL, "at_utc": START_FILL_BAR if al else now.isoformat(),
                               "aligned_to_live_u1": bool(al), "created_utc": now.isoformat(),
                               "quote": "USDC"},
               "equity_hourly": [], "runs": [], "errors": []})
    return st


def store():
    return P.PaperStore(object_name=OBJECT, local_path=LOCAL)


def run(now: datetime | None = None, main_state: dict | None = None, fetchers=None, st_store=None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = st_store or store()
    st = s.load()
    seeded = st is None
    if seeded:
        st = initial_state(now, main_state)
    with params():
        try:
            summ = P.step_live(st, now, fetchers)
            summ["ok"] = summ["stopped"] is None
        except Exception as e:  # noqa: BLE001
            log.exception("paper_u1_tv40_fail")
            summ = {"ok": False, "now_utc": now.isoformat(), "error": str(e)[:300]}
            st["errors"] = (st.get("errors", []) + [summ])[-50:]
    summ["seeded"] = seeded
    st["updated_at_utc"] = now.isoformat()
    st["runs"] = (st.get("runs", []) + [{k: summ.get(k) for k in ("now_utc", "ok", "bars_closed", "stopped", "error")}])[-200:]
    s.save(st)
    return summ


def public_view(st: dict | None) -> dict | None:
    if not st:
        return None
    lev = st["books"]["lev"]
    eq = P.book_equity(lev)
    start = st.get("paper_start") or {}
    last_dec = (st.get("decisions") or [None])[-1]
    return {"code": CODE, "label_zh": st.get("label_zh"), "mode": "paper_only", "orders": "none",
            "params": st.get("params"), "equity": eq, "start_equity": start.get("equity"), "started_at_utc": start.get("at_utc"),
            "pnl": eq - float(start.get("equity") or INITIAL),
            "return_pct": (eq / float(start.get("equity") or INITIAL) - 1) * 100,
            "units": dict(lev["units"]), "mark": dict(lev["last"]), "fees": lev["fees"], "funding_pnl": lev["funding_pnl"],
            "last_decision": ({k: last_dec.get(k) for k in ("indicator_day", "fill_bar", "L", "targets_lev")} if last_dec else None),
            "pending": ({k: st["pending"][k] for k in ("fill_bar", "L", "targets_lev")} if st.get("pending") else None),
            "n_fills": len(st.get("fills") or []), "updated_at_utc": st.get("updated_at_utc"),
            "last_run": (st.get("runs") or [None])[-1]}
