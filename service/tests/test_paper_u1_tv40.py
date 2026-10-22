from datetime import datetime, timezone

import paper_u1 as P
import paper_u1_tv40 as T


def test_params_scoped_and_restored():
    with T.params():
        assert P.ACCT_TV == 0.40 and P.LEV_CAP == 3.0
        L, _ = P.leverage_from_returns([0.01, -0.01] * 30)
    assert P.ACCT_TV == 0.60
    L60, _ = P.leverage_from_returns([0.01, -0.01] * 30)
    assert abs(L / L60 - 0.40 / 0.60) < 1e-9 or L60 == 3.0


def test_initial_state_flat_1000_and_copies_sigma_history():
    st = T.initial_state(datetime(2026, 10, 22, 13, 5, tzinfo=timezone.utc), {"returns_1x": [0.01] * 70})
    assert P.book_equity(st["books"]["lev"]) == 1000.0 and len(st["returns_1x"]) == 60
    assert st["last_closed_bar"].startswith("2026-10-22T12:00") and st["orders"] == "none"
    v = T.public_view(st)
    assert v["pnl"] == 0.0 and v["equity"] == 1000.0
