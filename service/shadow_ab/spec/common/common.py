"""Shared loaders/metrics for spec_2026-11. Cost model = engine.COST (20bps/side), reused."""
import sys, math, numpy as np, pandas as pd, glob, os
from pathlib import Path
sys.path.insert(0, "/workspace/strategy-unified-3y/code")
from engine import COST  # 20bps per side (canonical cost model)
import os
D = Path(os.environ.get("SPEC_DATA_DIR", str(Path(__file__).resolve().parents[1] / "data")))
OOS_START = pd.Timestamp("2023-10-01", tz="UTC"); OOS_END = pd.Timestamp("2026-09-30", tz="UTC")
EXCL = set("""USDCUSDT BTCDOMUSDT DEFIUSDT FOOTBALLUSDT BLUEBIRDUSDT FDUSDUSDT TUSDUSDT USDPUSDT BUSDUSDT XAUUSDT XAGUSDT
PAXGUSDT USDEUSDT RLUSDUSDT CLUSDT BZUSDT EWYUSDT INTCUSDT MRVLUSDT MSTRUSDT MUUSDT QQQUSDT SAMSUNGUSDT SKHYNIXUSDT
SKHYUSDT SNDKUSDT SOXLUSDT SOXSUSDT SPCXUSDT CRCLUSDT TSLAUSDT NVDAUSDT AAPLUSDT GOOGLUSDT METAUSDT AMZNUSDT COINUSDT
HOODUSDT NATGASUSDT XPTUSDT XPDUSDT SPYUSDT USTCUSDT""".split())  # stables, indices, TradFi/equity/commodity perps

def panel():
    p = pd.read_pickle(D / "panel_1d.pkl")
    keep = [s for s in p.columns.levels[0] if s.endswith("USDT") and s not in EXCL]
    f = lambda k: p.xs(k, axis=1, level=1)[keep]
    return {k: f(k) for k in ["open", "high", "low", "close", "quote_volume"]}

def funding_daily(syms):
    """Real Binance funding: sum of settled rates per UTC day (day d = settlements in (d, d+1]) -> paid over bar d."""
    out = {}
    for s in syms:
        fs = sorted(glob.glob(str(D / f"fr/{s}_*.csv")))
        if not fs: continue
        df = pd.concat([pd.read_csv(f) for f in fs])
        tcol = [c for c in df.columns if "time" in c.lower()][0]; rcol = [c for c in df.columns if "rate" in c.lower() and "funding" in c.lower()][0]
        t = pd.to_datetime(pd.to_numeric(df[tcol]), unit="ms", utc=True)
        r = pd.Series(pd.to_numeric(df[rcol]).values, index=t).sort_index()
        r = r[~r.index.duplicated()]
        out[s] = r.groupby((r.index - pd.Timedelta(milliseconds=1)).floor("D")).sum()
    return pd.DataFrame(out)

def spot_close(syms):
    out = {}
    cols = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbq,ignore".split(",")
    for s in syms:
        fs = sorted(glob.glob(str(D / f"sp/{s}_*.csv")))
        if not fs: continue
        df = pd.concat([pd.read_csv(f, header=None, names=cols) for f in fs])
        df = df[pd.to_numeric(df.open_time, errors="coerce").notna()]
        ot = df.open_time.astype("int64"); ot = np.where(ot > 1e14, ot // 1000, ot)  # spot 2025+ uses microseconds
        idx = pd.to_datetime(ot, unit="ms", utc=True).normalize()
        out[s] = pd.DataFrame({"open": df.open.astype(float).values, "close": df.close.astype(float).values}, index=idx)
        out[s] = out[s][~out[s].index.duplicated()].sort_index()
    return out

def metrics(r, bpy=365.0):
    r = r.dropna(); eq = (1 + r).cumprod()
    yrs = len(r) / bpy; sd = r.std()
    return dict(sharpe=float(r.mean() / sd * math.sqrt(bpy)) if sd > 0 else 0.0,
                cagr=float(eq.iloc[-1] ** (1 / yrs) - 1), maxdd=float(-(eq / eq.cummax() - 1).min()),
                total_return=float(eq.iloc[-1] - 1), vol=float(sd * math.sqrt(bpy)), n_days=int(len(r)))

def deflated_sharpe(r, trial_sharpes_ann, n_trials, bpy=365.0):
    """Bailey & Lopez de Prado (2014). SR in per-period units; returns DSR probability."""
    from scipy.stats import norm, skew, kurtosis
    r = r.dropna(); T = len(r); sr = r.mean() / r.std()
    v = np.var(np.asarray(trial_sharpes_ann) / math.sqrt(bpy), ddof=1) if len(trial_sharpes_ann) > 1 else 0.0
    g = 0.5772156649; N = max(n_trials, 2)
    sr0 = math.sqrt(v) * ((1 - g) * norm.ppf(1 - 1 / N) + g * norm.ppf(1 - 1 / (N * math.e)))
    sk = skew(r); ku = kurtosis(r, fisher=False)
    z = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(1 - sk * sr + (ku - 1) / 4 * sr ** 2)
    return dict(dsr=float(norm.cdf(z)), sr0_ann=float(sr0 * math.sqrt(bpy)), skew=float(sk), kurt=float(ku), T=T)

def wf_folds(start=OOS_START, end=OOS_END, train_m=12, test_m=3):
    f = []; t = start
    while t <= end:
        te = min(t + pd.DateOffset(months=test_m) - pd.Timedelta(days=1), end)
        f.append((t - pd.DateOffset(months=train_m), t - pd.Timedelta(days=1), t, te)); t = t + pd.DateOffset(months=test_m)
    return f
