"""Deposits, not the virtual 3000, are the starting capital. VBO's 1000 stays inside USDT-M."""
import capital_basis as C


def test_without_transfers_the_start_is_5000_plus_5000_and_vbo_is_not_added():
    d = C.summarize([], [["2026-10-01", 10000], ["2026-10-15", 11000], ["2026-11-01", 9000]], 9683.50)
    assert d["method"] == "derived_wallet_identity"
    assert d["usdt_m_deposit"] == 5000
    assert d["usdc_deposit"] == 5000
    assert d["vbo_carve_usdt"] == 1000 and d["vbo_source"] == "carve_inside_usdt_m"
    assert d["starting_total"] == 10000
    assert d["r2s_virtual_excluded"] == 3000
    assert d["r2s_note"] == "R2S 原本的 3000 為虛擬起始資金，未實際入金，已於 2026-11-11 扣除"
    assert d["nav"] == 9683.50
    assert d["pnl"] == -316.50
    assert d["peak"] == 11000
    assert d["max_drawdown"] == 2000


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
