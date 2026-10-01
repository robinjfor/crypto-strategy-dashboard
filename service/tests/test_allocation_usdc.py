"""USDT and USDC books are separate 5000 caps; *USDC symbols allowed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allocation import (  # noqa: E402
    validate_allocation, slot_to_runtime, infer_quote_asset, slot_notional,
)
from api import _books_overview  # noqa: E402


def _base_slot(**kw):
    s = {
        "slot": "sat_x", "family": "donchian_atr",
        "strategy_id": "donchian20_s2.0_t3.0__AAA__1h",
        "symbol": "BTCUSDT", "timeframe": "1h",
        "notional_usdt": 500, "enabled": True,
        "params": {"donch_n": 20, "stop_atr_mult": 2.0, "trail_atr_mult": 3.0},
    }
    s.update(kw)
    return s


def test_usdc_slot_valid_and_separate_book():
    doc = {
        "book_usdt": 5000, "book_usdc": 5000,
        "slots": [
            _base_slot(),
            _base_slot(
                slot="sat_btc_usdc", strategy_id="d__BTC__1h_usdc",
                symbol="BTCUSDC", quote_currency="USDC",
                notional_usdc=500, notional_usdt=None, enabled=True,
            ),
        ],
    }
    # remove None key
    doc["slots"][1].pop("notional_usdt", None)
    errs = validate_allocation(doc, check_binance=False)
    assert errs == [], errs
    rt = slot_to_runtime(doc["slots"][1])
    assert rt["quote_asset"] == "USDC" and rt["quote_usdt"] == 500.0


def test_usdc_over_book_rejected():
    doc = {
        "book_usdt": 5000, "book_usdc": 1000,
        "slots": [
            _base_slot(
                slot="sat_a", strategy_id="a", symbol="ETHUSDC",
                quote_currency="USDC", notional_usdc=750, enabled=True,
            ),
            _base_slot(
                slot="sat_b", strategy_id="b", symbol="BTCUSDC",
                quote_currency="USDC", notional_usdc=750, enabled=True,
            ),
        ],
    }
    for s in doc["slots"]:
        s.pop("notional_usdt", None)
    errs = validate_allocation(doc, check_binance=False)
    assert any("book_usdc" in e for e in errs)


def test_books_overview_separate():
    bals = {"USDT": 5658.96, "USDC": 5000.0}
    positions = []
    closed = [
        {"symbol": "FETUSDT", "pnl_usdt": -40.482},
        {"symbol": "OPUSDT", "pnl_usdt": -12.8121},
        {"symbol": "FILUSDT", "pnl_usdt": 73.2713},
        {"symbol": "DOTUSDT", "pnl_usdt": -34.875},
    ]
    books = _books_overview(bals, positions, closed, book_usdt=5000, book_usdc=5000)
    assert books["usdc"]["equity"] == 5000.0
    assert books["usdc"]["realized_pnl"] == 0.0
    assert books["usdt"]["cash"] == 5658.96
    assert abs(books["usdt"]["realized_pnl"] - (-14.8978)) < 1e-3
