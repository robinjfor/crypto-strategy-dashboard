import derivs_collector as dc


def _s(sym, **kw):
    d = {"symbol": sym, "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING", "underlyingType": "COIN"}
    d.update(kw); return d


def test_universe_excludes_tradfi_gold_and_ranks_by_quote_volume():
    info = {"symbols": [_s("BTCUSDT"), _s("ETHUSDT"), _s("PAXGUSDT"), _s("XAUTUSDT"),
                        _s("TSLAUSDT", contractType="TRADIFI_PERPETUAL", underlyingType="EQUITY", onboardDate=1767225600000),
                        _s("BTCUSDC", quoteAsset="USDC"), _s("OLDUSDT", status="SETTLING")]}
    tk = [{"symbol": s, "quoteVolume": str(v)} for s, v in
          [("BTCUSDT", 5), ("ETHUSDT", 9), ("PAXGUSDT", 99), ("XAUTUSDT", 98), ("TSLAUSDT", 97), ("BTCUSDC", 96), ("OLDUSDT", 95)]]
    assert [t["symbol"] for t in dc.select_universe(info, tk, 30)] == ["ETHUSDT", "BTCUSDT"]
    assert len(dc.select_universe(info, tk, 1)) == 1
