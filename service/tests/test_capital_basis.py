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


def test_reconcile_update_uses_snapshot_futures_not_total():
    import reconcile
    s = {"futures_transfers": [{"asset": "USDT", "amount": 5000.0, "time_ms": 1}]}
    snap = {"futures": FUT_1112, "total_usdt": 20120.22, "spot_usdt": 10443.21}
    reconcile.capital_basis_update(s, snap)
    cb = s["capital_basis"]
    assert cb["starting_total"] == 10000 and cb["nav"] == 9677.01 and cb["pnl"] == -322.99
    assert s["capital_basis_history"][-1][1] == 9677.01
