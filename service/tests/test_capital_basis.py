"""Deposits, not the virtual 3000, are the starting capital. VBO's 1000 stays inside USDT-M.
NAV is USDT-M + USDC-M margin balance (wallet + unrealized). Spot is never in NAV or PnL."""
import capital_basis as C

FUT_1112 = {"USDT": 4868.68, "USDC": 4808.33}   # 2026-11-12 08:52 Taipei total 9677.01


def test_without_transfers_the_start_is_5000_plus_5000_and_vbo_is_not_added():
    d = C.summarize([], FUT_1112, [], account_total=20120.22)
    assert d["method"] == "derived_wallet_identity"
    assert d["usdt_m_deposit"] == 5000 and d["usdc_deposit"] == 5000
    assert d["vbo_carve_usdt"] == 1000 and d["vbo_source"] == "carve_inside_usdt_m"
    assert d["starting_total"] == 10000
    assert d["r2s_virtual_excluded"] == 3000
    assert d["r2s_note"] == "R2S 原本的 3000 為虛擬起始資金，未實際入金，已於 2026-11-11 扣除"


def test_usdt_transfer_only_keeps_the_usdc_5000():
    """11/12 bug: a USDT TRANSFER row alone made USDC 0 and the start 5000."""
    rows = [{"incomeType": "TRANSFER", "asset": "USDT", "income": "5000", "time": 1}]
    d = C.summarize(rows, FUT_1112, [], account_total=20120.22)
    assert d["usdt_m_deposit"] == 5000 and d["usdt_m_deposit_method"] == "binance_income_transfer"
    assert d["usdc_deposit"] == 5000 and d["usdc_deposit_method"] == "derived_wallet_identity"
    assert d["method"] == "mixed"
    assert d["starting_total"] == 10000


def test_nav_is_futures_only_and_matches_the_1112_snapshot():
    d = C.summarize([{"asset": "USDT", "amount": 5000, "time_ms": 1}], FUT_1112, [], account_total=20120.22)
    assert d["nav"] == 9677.01
    assert d["pnl"] == -322.99
    assert d["nav_usdt_m"] == 4868.68 and d["nav_usdc_m"] == 4808.33
    assert d["account_total_incl_spot"] == 20120.22   # reference only
    assert d["nav_scope"] == "usd_m_futures_usdt_plus_usdc"
    assert d["peak"] == 10000 and d["max_drawdown"] == 322.99


def test_missing_leg_gives_no_nav_instead_of_a_fake_loss():
    d = C.summarize([], {"USDT": 4868.68, "USDC": None}, [], account_total=20120.22)
    assert d["nav"] is None and d["pnl"] is None
    assert d["starting_total"] == 10000


def test_history_drives_peak_and_drawdown_and_replaces_today():
    h = C.update_history([], 10300.0, "2026-11-01")
    h = C.update_history(h, 9500.0, "2026-11-05")
    h = C.update_history(h, 9600.0, "2026-11-12")
    h = C.update_history(h, 9677.01, "2026-11-12")
    assert h[-1] == ["2026-11-12", 9677.01] and len(h) == 3
    d = C.summarize([], FUT_1112, h)
    assert d["peak"] == 10300.0
    assert d["max_drawdown"] == 800.0


def test_transfer_income_replaces_the_derived_deposits():
    rows = [
        {"incomeType": "TRANSFER", "asset": "USDT", "income": "5000", "time": 1},
        {"incomeType": "TRANSFER", "asset": "USDC", "income": "4000", "time": 2},
        {"incomeType": "FUNDING_FEE", "asset": "USDT", "income": "-3", "time": 3},
    ]
    d = C.deposits(C.transfers_from_income(rows))
    assert d["method"] == "binance_income_transfer"
    assert d["usdt_m_deposit"] == 5000 and d["usdc_deposit"] == 4000
    assert d["starting_total"] == 9000
    assert d["vbo_carve_usdt"] == 1000


def test_wallet_portfolio_is_futures_nav_and_ignores_virtual_books():
    """2026-11-12 08:52 Taipei wallet, versus the 2026-11-11 20:08 book sum.

    The book sum is E+R+U+F from service/shadow_ab/pool_fix_compare.json and still
    includes R2S's virtual 3000. It must not become portfolio_equity.
    """
    d = C.summarize([], FUT_1112, [])
    w = C.wallet_portfolio(d)
    assert w["portfolio_basis"] == "usd_m_futures_usdt_plus_usdc"
    assert w["starting_total"] == 10000
    assert w["portfolio_equity"] == 9677.01
    assert w["nav_usdt_m"] == 4868.68 and w["nav_usdc_m"] == 4808.33
    assert w["peak"] == 10000
    assert w["max_drawdown"] == 322.99
    assert w["drawdown"] == d["max_drawdown_pct"] == 0.032299
    book_sum = round(4776.0098 + 3085.5987 + 808.478 + 1000.0, 2)
    assert book_sum == 9670.09
    assert w["portfolio_equity"] != book_sum


def test_wallet_portfolio_peak_and_drawdown_follow_the_daily_series():
    h = [["2026-11-01", 10300.0], ["2026-11-05", 9500.0]]
    d = C.summarize([], FUT_1112, h)
    w = C.wallet_portfolio(d)
    assert w["portfolio_equity"] == 9677.01
    assert w["peak"] == 10300.0
    assert w["max_drawdown"] == 800.0
    assert w["drawdown"] == d["max_drawdown_pct"]
    assert w["starting_total"] == 10000


def test_overlay_replaces_the_book_sum_and_leaves_sizing_alone():
    stored = {
        "targets": {"demo_e5_min5_x175_t05top20": 3875.17, "demo_r2s_top10_3k": 0.0,
                    "u1_live": 0.0, "demo_funding_arb_1x": 0.0},
        "weights": {"demo_e5_min5_x175_t05top20": 1.0, "demo_r2s_top10_3k": 0.0,
                    "u1_live": 0.0, "demo_funding_arb_1x": 0.0},
        "quote_pool": {"demo_e5_min5_x175_t05top20": 3875.17},
        "sizing": "quote_pool",
        "equity": {"demo_e5_min5_x175_t05top20": 4776.01, "demo_r2s_top10_3k": 3085.60,
                   "u1_live": 808.48, "demo_funding_arb_1x": 1000.0},
        "portfolio_equity": 9670.09,
        "peak": 10000.0,
        "drawdown": 0.033,
        "history": [{"bar": "2026-11-11", "P": 9670.09, "dd": 0.033}],
    }
    basis = C.summarize([], FUT_1112, [["2026-11-01", 10300.0], ["2026-11-05", 9500.0]])
    view = C.overlay_orchestrator(stored, basis)
    assert view["portfolio_equity"] == 9677.01
    assert view["peak"] == 10300.0
    assert view["max_drawdown"] == 800.0
    assert view["drawdown"] == basis["max_drawdown_pct"]
    assert view["starting_total"] == 10000
    assert view["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert view["targets"]["demo_r2s_top10_3k"] == 0.0
    assert view["quote_pool"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert view["weights"]["demo_e5_min5_x175_t05top20"] == 1.0
    assert view["sizing"] == "quote_pool"
    assert "history" not in view
    assert stored["portfolio_equity"] == 9670.09 and stored["peak"] == 10000.0
    assert stored["history"][0]["P"] == 9670.09


def test_missing_wallet_leg_does_not_fall_back_to_the_virtual_book_sum():
    basis = C.summarize([], {"USDT": 4868.68, "USDC": None}, [])
    stored = {"portfolio_equity": 13000.0, "peak": 14000.0, "drawdown": 0.1,
              "targets": {"demo_e5_min5_x175_t05top20": 3875.17}}
    view = C.overlay_orchestrator(stored, basis)
    assert view["portfolio_equity"] is None and view["peak"] is None
    assert view["drawdown"] is None and view["max_drawdown"] is None
    assert view["starting_total"] == 10000
    assert view["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert stored["portfolio_equity"] == 13000.0


def test_reconcile_update_uses_snapshot_futures_not_total():
    import reconcile
    s = {"futures_transfers": [{"asset": "USDT", "amount": 5000.0, "time_ms": 1}]}
    snap = {"futures": FUT_1112, "total_usdt": 20120.22, "spot_usdt": 10443.21}
    reconcile.capital_basis_update(s, snap)
    cb = s["capital_basis"]
    assert cb["starting_total"] == 10000 and cb["nav"] == 9677.01 and cb["pnl"] == -322.99
    assert s["capital_basis_history"][-1][1] == 9677.01
