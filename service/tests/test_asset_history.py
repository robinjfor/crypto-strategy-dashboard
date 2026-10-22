from datetime import datetime, timedelta, timezone

import asset_history as ah

TZ = timezone(timedelta(hours=8))


def test_reconstruct_undo_and_value():
    start = datetime(2026, 10, 1, tzinfo=TZ)
    t = int((start + timedelta(days=1, hours=12)).timestamp() * 1000)   # 10/2 midday
    doc = {"fills": [{"venue": "spot", "symbol": "FILUSDT", "side": "BUY", "qty": 10.0, "quoteQty": 30.0,
                      "commission": 0.03, "commissionAsset": "USDT", "time_ms": t, "price": 3.0}],
           "income": [{"time_ms": t, "asset": "USDC", "income": -1.0}]}
    now = start + timedelta(days=3, hours=1)
    px = lambda a, dt: 4.0 if a == "FIL" else None  # noqa: E731
    out = ah.build(doc, {"USDT": 969.97, "FIL": 10.0}, {"USDC": 99.0}, {}, px, now, start=start)
    pts = dict(out["points"])
    assert pts["2026-10-01"] == 1100.0          # before the buy: 1000 USDT + 100 USDC
    assert pts["2026-10-02"] == round(969.97 + 40 + 99, 2)
    assert len(out["points"]) == 4 and pts["2026-09-30"] == 1100.0 and out["perps_flat_at_start"]
