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


def initial_state(now: datetime, main_state: dict | None) -> dict:
    st = P.new_engine_state()
    st["books"] = {"x1": P.new_book(INITIAL), "lev": P.new_book(INITIAL)}
    st["e1x_prev"] = INITIAL
    if main_state:
        st["returns_1x"] = list(main_state.get("returns_1x") or [])[-P.SIG60_WIN:]
    cur = pd.Timestamp(now.astimezone(timezone.utc)).floor("h")
    st["last_closed_bar"] = P._iso(cur - P.H)
    st.update({"code": CODE, "mode": "paper_only", "label_zh": "影子帳本（模擬，不下單）", "orders": "none",
               "params": {"acct_tv": ACCT_TV, "lev_cap": LEV_CAP, "asset_tv": P.ASSET_TV, "n_sma": P.N_SMA},
               "paper_start": {"bar": st["last_closed_bar"], "equity": INITIAL, "at_utc": now.isoformat(),
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
