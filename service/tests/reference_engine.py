"""Verbatim subset of strategy-unified-3y/code/engine.py (2026-10-08) for live↔backtest parity tests."""
from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

INIT = 10_000.0
COST = 20.0 / 10_000.0
MAINTENANCE = 0.005

def sma_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def wilder_atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    atr = tr.copy().astype(float)
    atr[:] = np.nan
    if len(tr) < n:
        return atr
    atr.iloc[n - 1] = float(tr.iloc[:n].mean())
    for i in range(n, len(tr)):
        atr.iloc[i] = (float(atr.iloc[i - 1]) * (n - 1) + float(tr.iloc[i])) / n
    return atr


def add_donch(df: pd.DataFrame, n: int, atr_mode: str = "sma") -> pd.DataFrame:
    o = df.copy()
    h, l, c = o["High"], o["Low"], o["Close"]
    o["donch_hi"] = h.rolling(n).max().shift(1)
    o["donch_lo"] = l.rolling(n).min().shift(1)
    o["atr"] = sma_atr(o, 14) if atr_mode == "sma" else wilder_atr(o, 14)
    for span in (8, 9, 12, 20, 21, 26, 50, 200):
        o[f"ema_{span}"] = c.ewm(span=span, adjust=False).mean()
        o[f"sma_{span}"] = c.rolling(span).mean()
    return o


def signal_donchian(df: pd.DataFrame) -> pd.Series:
    hold = np.zeros(len(df), dtype=int)
    state = 0
    closes = df["Close"].values
    his, los = df["donch_hi"].values, df["donch_lo"].values
    for i in range(len(df)):
        if np.isnan(his[i]) or np.isnan(los[i]):
            hold[i] = state
            continue
        if state == 0 and closes[i] > his[i]:
            state = 1
        elif state == 1 and closes[i] < los[i]:
            state = 0
        hold[i] = state
    return pd.Series(hold, index=df.index)


@dataclass
class BTResult:
    equity: pd.Series
    trades: list
    n_liquidations: int = 0


def run_donchian_engine(
    df: pd.DataFrame,
    signal: pd.Series,
    stop_m: float,
    trail_m: float,
    max_hold: int,
    init: float = INIT,
    reset_below_hi: bool = False,
    leverage: float = 1.0,
    funding_ann: float = 0.0,
    check_liq: bool = False,
    risk_frac: float | pd.Series = 1.0,
) -> BTResult:
    """Long-only Donchian-style engine. Entry: edge 0→1; optional reset_below_hi."""
    data = df.dropna(subset=["atr", "donch_hi", "donch_lo"]).copy()
    signal = signal.reindex(data.index).fillna(0).astype(int)
    if isinstance(risk_frac, pd.Series):
        risk_frac_arr = risk_frac.reindex(data.index).fillna(1.0).clip(0.0, 1.0).values
    else:
        risk_frac_arr = np.full(len(data), float(risk_frac))
    cash = init
    shares = 0.0
    entry_px = 0.0
    entry_i = 0
    stop = 0.0
    entry_ts = None
    equity = []
    trades = []
    n_liq = 0
    need_reset = False
    idx = data.index
    opens, closes, highs, lows = data["Open"].values, data["Close"].values, data["High"].values, data["Low"].values
    atrs, sigs, his = data["atr"].values, signal.values, data["donch_hi"].values

    # bars/year approx for funding
    if len(idx) > 1:
        dt = (idx[1] - idx[0]).total_seconds()
        bpy = 365.25 * 86400 / max(dt, 1)
    else:
        bpy = 365.25
    fund_bar = funding_ann / bpy

    for i in range(len(data)):
        ts = idx[i]
        o, c, h, lo, atr = opens[i], closes[i], highs[i], lows[i], atrs[i]

        if shares != 0 and fund_bar and leverage > 1:
            cash -= abs(shares) * c * fund_bar

        if check_liq and shares > 0 and leverage > 1:
            mtm = cash + shares * c
            notional = shares * c
            if mtm <= notional * MAINTENANCE or mtm <= 0:
                px = lo * (1 - COST)
                cash += shares * px
                trades.append({"entry": str(entry_ts), "exit": str(ts), "pnl": shares * (px - entry_px), "reason": "liquidation"})
                shares = 0.0
                n_liq += 1
                cash = max(cash, 0.0)
                equity.append((ts, cash))
                need_reset = True if reset_below_hi else False
                continue

        if shares > 0:
            hit = (stop_m > 0) and (lo <= stop)
            hold_bars = i - entry_i
            exit_sig = sigs[i] == 0
            reason = None
            exit_ref = None
            if hit:
                reason = "stop_loss"
                exit_ref = min(o, stop)
            elif trail_m > 0:
                trail = c - trail_m * atr
                if trail > stop:
                    stop = trail
            if reason is None and (exit_sig or hold_bars >= max_hold):
                reason = "signal_exit" if exit_sig else "max_hold"
                exit_ref = c
            if reason is not None:
                px = exit_ref * (1 - COST)
                cash += shares * px
                trades.append({
                    "entry": str(entry_ts), "exit": str(ts),
                    "entry_px": entry_px, "exit_px": px,
                    "pnl": shares * (px - entry_px),
                    "pnl_pct": (px / entry_px - 1) * 100,
                    "reason": reason,
                })
                shares = 0.0
                entry_px = 0.0
                stop = 0.0
                if reset_below_hi:
                    need_reset = True

        can = shares == 0 and i > 0
        if reset_below_hi and need_reset:
            if c < his[i]:
                need_reset = False
            else:
                can = False
        edge = sigs[i] == 1 and sigs[i - 1] == 0
        trigger = edge  # both core (no reset) and reset_below use edge; reset gates via need_reset
        if can and trigger and atr > 0 and not np.isnan(atr):
            px = c * (1 + COST)
            equity_now = cash
            notional = equity_now * float(risk_frac_arr[i]) * leverage
            if notional > 0 and px > 0:
                shares = notional / px
                cash -= shares * px
                entry_px = px
                entry_i = i
                entry_ts = ts
                stop = (px - stop_m * atr) if stop_m > 0 else 0.0

        equity.append((ts, cash + shares * c))

    if shares > 0:
        px = closes[-1] * (1 - COST)
        cash += shares * px
        trades.append({"entry": str(entry_ts), "exit": str(idx[-1]), "pnl": shares * (px - entry_px), "reason": "eod_flat"})
        equity[-1] = (idx[-1], cash)

    return BTResult(equity=pd.Series({t: v for t, v in equity}), trades=trades, n_liquidations=n_liq)




# --- vendored from strategy-unified-3y/lookahead_fix/live_reval/reval_fill.py (next-open fills) ---
def run_next_open(df,signal,stop_m,trail_m,max_hold,init=INIT,reset_below_hi=False,leverage=1.0,funding_ann=0.0,check_liq=False,risk_frac=1.0):
    data=df.dropna(subset=["atr","donch_hi","donch_lo"]).copy()
    sig=signal.reindex(data.index).fillna(0).astype(int).values
    cash=init;shares=0.0;entry_px=0.0;entry_i=0;stop=0.0;entry_ts=None
    eqs=[];trades=[];need_reset=False;pend_entry=None;pend_exit=None
    idx=data.index;O,C,H,L=data["Open"].values,data["Close"].values,data["High"].values,data["Low"].values
    A,HI=data["atr"].values,data["donch_hi"].values
    def close_pos(ts,ref,reason):
        nonlocal cash,shares,entry_px,stop,need_reset
        px=ref*(1-COST);cash+=shares*px
        trades.append({"entry":str(entry_ts),"exit":str(ts),"entry_px":entry_px,"exit_px":px,"pnl":shares*(px-entry_px),"pnl_pct":(px/entry_px-1)*100,"reason":reason})
        shares=0.0;entry_px=0.0;stop=0.0
        if reset_below_hi: need_reset=True
    for i in range(len(data)):
        ts=idx[i];o,c,lo,atr=O[i],C[i],L[i],A[i]
        if pend_exit and shares>0:
            close_pos(ts,o,pend_exit);pend_exit=None
        if pend_entry is not None and shares==0:
            px=o*(1+COST);shares=cash*leverage/px;cash-=shares*px
            entry_px=px;entry_i=i;entry_ts=ts;stop=(px-stop_m*pend_entry) if stop_m>0 else 0.0
        pend_entry=None
        if shares>0:
            if stop_m>0 and lo<=stop: close_pos(ts,min(o,stop),"stop_loss")
            else:
                if trail_m>0:
                    t=c-trail_m*atr
                    if t>stop: stop=t
                if sig[i]==0 or (i-entry_i)>=max_hold: pend_exit="signal_exit" if sig[i]==0 else "max_hold"
        can=shares==0 and i>0
        if reset_below_hi and need_reset:
            if c<HI[i]: need_reset=False
            else: can=False
        if can and sig[i]==1 and sig[i-1]==0 and atr>0 and not np.isnan(atr) and i+1<len(data):
            pend_entry=atr
        eqs.append((ts,cash+shares*c))
    if shares>0:
        px=C[-1]*(1-COST);cash+=shares*px
        trades.append({"entry":str(entry_ts),"exit":str(idx[-1]),"pnl":shares*(px-entry_px),"reason":"eod_flat"});eqs[-1]=(idx[-1],cash)
    return BTResult(equity=pd.Series({t:v for t,v in eqs}),trades=trades,n_liquidations=0)



# --- EMA cross + BTC regime (engine.signal_ema / apply_btc_regime / add_ema) ---
def add_ema_cols(df: pd.DataFrame, spans=(12, 26)) -> pd.DataFrame:
    o = df.copy()
    for span in spans:
        o[f"ema_{span}"] = o["Close"].ewm(span=span, adjust=False).mean()
    return o


def signal_ema(df: pd.DataFrame, fast: int, slow: int) -> pd.Series:
    return (df[f"ema_{fast}"] > df[f"ema_{slow}"]).astype(int)


def apply_btc_regime(sig: pd.Series, btc: pd.DataFrame) -> pd.Series:
    ok = (btc["Close"] > btc["sma_200"]).astype(int)
    ok = ok.reindex(sig.index).ffill().fillna(0)
    return (sig.astype(int) & ok.astype(int)).astype(int)
