"""Run the analyst's ledger engine (ledger_c.py source, unchanged) for ledger E and capture its per-day internal
state, by executing the source with a `dict` builtin that snapshots module globals whenever a ledger row is built.
Used for (a) trade-by-trade parity of service/demo_e.py's planner vs the backtest and (b) next-open targets."""
from __future__ import annotations

import builtins
import os
import sys
from pathlib import Path

import numpy as np


def run(shadow_dir: Path, test_start: str | None) -> dict:
    os.environ.update(LEDGER_NAME="E", LEDGER_W="0.8", LEDGER_LV="1.75")
    if test_start:
        os.environ["LEDGER_TEST_START"] = test_start
    else:
        os.environ.pop("LEDGER_TEST_START", None)
    src = (shadow_dir / "ledger_c.py").read_text()
    snaps: list[dict] = []
    g: dict = {"__name__": "__main__", "__file__": str(shadow_dir / "ledger_c.py")}

    def cap_dict(*a, **kw):
        if "date" in kw and "equity" in kw and "nw" in g and g.get("A") is not None:
            A = g["A"]
            nwv = g["nw"]
            snaps.append({"date": kw["date"], "cur_u": g["cur_u"].copy(), "tw": g["tw"].copy(), "db": g["db"].copy(),
                          "o5": g["o5"].reindex(g["tw"].index).copy(), "nw": nwv.copy(), "nu": g["nu"].copy(),
                          "u1_u": np.array(A["u1"]["u"], dtype=float).copy(), "uo": np.array(g["uo"][g["i"]], dtype=float),
                          "lev": np.array(g["pendU"]["lev"], dtype=float) if g["pendU"] is not None else None,
                          "equity": kw["equity"], "m_used": kw["exposure_mult_used"], "liq": kw["u1_liq"] + kw["s5_leg_liq"]})
        return builtins.dict(*a, **kw)

    g["dict"] = cap_dict
    argv, cwd = sys.argv, os.getcwd()
    sys.argv = [str(shadow_dir / "ledger_c.py"), "--no-fetch"]
    os.chdir(shadow_dir)
    try:
        exec(compile(src, str(shadow_dir / "ledger_c.py"), "exec"), g)
    finally:
        sys.argv = argv
        os.chdir(cwd)
    last = g["idx"][-1]
    W5u, SP5u, LV, m = g["W5u"], g["SP5u"], g["LV"], g["m"]
    tgt = {"decided_bar": str(last.date()), "m_next": m,
           "s5_w": {k: float(v) for k, v in (W5u.loc[last] * LV * m).items() if v != 0} if last in W5u.index else {},
           "s5_band": {k: float(v) for k, v in (SP5u.loc[last] * LV * m * g["S5CFG"]["b"]).items() if v != 0} if last in SP5u.index else {},
           "u1_w": {"BTCUSDC": float(g["pendU"]["lev"][0]), "ETHUSDC": float(g["pendU"]["lev"][1])},
           "w_u1": g["W_U1"]}
    return {"snaps": snaps, "targets": tgt}
