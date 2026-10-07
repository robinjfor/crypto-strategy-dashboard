"""Fallback btc_daily_regime_on() must ignore the forming daily bar."""
import os, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
import strategy


class FakeClient:
    def __init__(self, df):
        self.df = df

    def fetch_klines(self, *a, **k):
        return self.df


def _df(last_close):
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    idx = pd.DatetimeIndex([today - timedelta(days=i) for i in range(220, -1, -1)])  # last = forming today
    closes = [100.0] * 220 + [last_close]
    closes[219] = 90.0  # yesterday's CLOSED bar is below SMA200 -> bear
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes, "Volume": 1.0}, index=idx)


def test_forming_bar_ignored_bull_spike():
    # Forming bar spikes far above SMA; closed-bar regime is bear -> False
    assert strategy.btc_daily_regime_on(FakeClient(_df(10_000.0))) is False


def test_matches_series_path():
    c = FakeClient(_df(10_000.0))
    ser = strategy.btc_daily_regime_series(c)
    assert ser is not None
    assert bool(int(ser.iloc[-1])) == strategy.btc_daily_regime_on(c)
