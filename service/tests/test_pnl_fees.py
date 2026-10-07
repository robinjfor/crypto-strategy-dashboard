import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from execution import fee_usdt_from_rows, finalize_closed_pnl


def test_spot_fees_quote_and_base():
    buy = [{"price": "0.2289", "qty": "5460.3", "commission": "5.4603", "commissionAsset": "FET"}]
    sell = [{"price": "0.25", "qty": "5454.8", "commission": "1.3637", "commissionAsset": "USDT"}]
    ef = fee_usdt_from_rows(buy, "FETUSDT"); xf = fee_usdt_from_rows(sell, "FETUSDT")
    assert abs(ef - 5.4603 * 0.2289) < 1e-6 and xf == 1.3637
    c = finalize_closed_pnl({"pnl_usdt": 100.0}, {"entry_fee_usdt": ef}, xf)
    assert c["pnl_gross_usdt"] == 100.0 and c["fees_complete"] is True
    assert abs(c["pnl_usdt"] - (100.0 - ef - xf)) < 1e-4


def test_unknown_fee_asset_not_guessed():
    assert fee_usdt_from_rows([{"price": "1", "qty": "1", "commission": "0.001", "commissionAsset": "BNB"}], "DOTUSDT") is None
    c = finalize_closed_pnl({"pnl_usdt": -10.0}, {}, None)
    assert c["pnl_usdt"] == -10.0 and c["fees_complete"] is False and c["fee_usdt"] is None
