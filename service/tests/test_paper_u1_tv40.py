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


def test_aligned_start_relevers_live_decision():
    main = {"decisions": [{"decision_bar": "2026-10-20T23:00:00+00:00", "fill_bar": T.START_FILL_BAR, "indicator_day": "2026-10-20",
                           "w1x": {"BTC": 0.5, "ETH": 0.5}, "L": 1.77, "targets_lev": {"BTC": 0.88, "ETH": 0.88}}],
            "e1x_closes": [[f"2026-09-{d:02d}", 1] for d in range(1, 31)] + [["2026-10-20", 1], ["2026-10-21", 1]],
            "returns_1x": [0.02, -0.02] * 16}
    st = T.initial_state(datetime(2026, 10, 22, 13, 5, tzinfo=timezone.utc), main)
    assert st["last_closed_bar"] == "2026-10-20T23:00:00+00:00" and len(st["returns_1x"]) == 31
    assert st["pending"]["fill_bar"] == T.START_FILL_BAR and st["pending"]["targets_lev"]["BTC"] < 0.88
    assert st["paper_start"]["aligned_to_live_u1"] is True
