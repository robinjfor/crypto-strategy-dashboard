"""U1 tv0.40 cap3 paper shadow ledger (no orders). Same spec as live U1 (u1.py / paper_u1.py):
signal = spot close > SMA200, w1 = 0.5*min(1, 0.60/sigma20_spot); L = min(3, tv/max(sigma60 of 1x book, 0.05));
25% band rebalance at next 00:00 UTC open on BTCUSDC/ETHUSDC perps, 20bps/side, real funding.
Shadow tv=0.40; reference 'orig' tv=0.60 (backtest twin of live U1). Both lev books start 1000 USDC at 2026-10-21 open.
Data: Binance public daily klines + funding only. Writes ledger.csv, state.json, run.log."""
import json, math, time, urllib.request, numpy as np, pandas as pd
from pathlib import Path
OUT = Path(__file__).resolve().parent
SYMS = ["BTC", "ETH"]; SLEEVE, ASSET_TV, BAND, MAINT, CAP, COST = 0.5, 0.60, 0.25, 0.005, 3.0, 0.002
START = pd.Timestamp("2026-10-21", tz="UTC"); END = pd.Timestamp("2026-11-18", tz="UTC")
def get(url):
    for a in range(4):
        try: return json.loads(urllib.request.urlopen(url, timeout=30).read())
        except Exception as e: err = e; time.sleep(3)
    raise err
def klines(base, sym, days=520):
    st = int((pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=days)).timestamp() * 1000); rows = []
    while True:
        r = get(f"{base}?symbol={sym}&interval=1d&startTime={st}&limit=1000"); rows += r
        if len(r) < 1000: break
        st = r[-1][0] + 1
    df = pd.DataFrame([[x[0], float(x[1]), float(x[3]), float(x[4]), x[6]] for x in rows], columns=["t", "o", "l", "c", "ct"])
    df = df[df.ct < time.time() * 1000]  # closed bars only
    df.index = pd.to_datetime(df.t, unit="ms", utc=True); return df
def funding(sym, days=120):
    st = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).timestamp() * 1000)
    r = get(f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={sym}&startTime={st}&limit=1000")
    s = pd.Series([float(x["fundingRate"]) for x in r], index=pd.to_datetime([x["fundingTime"] for x in r], unit="ms", utc=True))
    return s
SP = {k: klines("https://api.binance.com/api/v3/klines", k + "USDT") for k in SYMS}
FU = {k: klines("https://fapi.binance.com/fapi/v1/klines", k + "USDC") for k in SYMS}
FR = {k: funding(k + "USDC") for k in SYMS}
idx = FU["BTC"].index.intersection(FU["ETH"].index).intersection(SP["BTC"].index).intersection(SP["ETH"].index)
o = np.c_[[FU[k].o.reindex(idx).values for k in SYMS]].T; lo = np.c_[[FU[k].l.reindex(idx).values for k in SYMS]].T
c = np.c_[[FU[k].c.reindex(idx).values for k in SYMS]].T
sc = {k: SP[k].c.reindex(idx) for k in SYMS}
sig20 = {k: np.log(sc[k] / sc[k].shift(1)).rolling(20, min_periods=15).std() * math.sqrt(365) for k in SYMS}
sma = {k: sc[k].rolling(200, min_periods=200).mean() for k in SYMS}
W1 = pd.DataFrame({k: ((sc[k] > sma[k]).astype(float) * SLEEVE * np.minimum(1.0, ASSET_TV / sig20[k])).fillna(0.0) for k in SYMS})
# funding: settlements in (d, d+1] -> applied over bar d (00:00 d+1 settlement applied at open d+1, as u1.py)
def fday(k):
    s = FR[k]; g = s.groupby((s.index - pd.Timedelta(milliseconds=1)).floor("D")).sum(); return g.reindex(idx).fillna(0.0).values
FD = np.c_[[fday(k) for k in SYMS]].T; fr_prev = np.r_[np.zeros((1, 2)), FD[:-1]]
TV = {"shadow_tv040": 0.40, "orig_tv060": 0.60}
books = {"x1": dict(cash=1.0, u=np.zeros(2))}; e1 = []; prev1 = 1.0; pend = None; rows = []
for i, d in enumerate(idx):
    if d == START:
        for b in TV: books[b] = dict(cash=1000.0, u=np.zeros(2), fees=0.0, fund=0.0, liq=0, trades=0)
    for b, B in books.items():
        f = (B["u"] * o[i] * fr_prev[i]).sum(); B["cash"] -= f
        if b != "x1": B["fund"] += f
        if pend is not None and b in pend:
            V = B["cash"] + (B["u"] * o[i]).sum()
            for j in range(2):
                wt = pend[b][j]; wc = B["u"][j] * o[i, j] / V if V > 0 else 0
                tr = (B["u"][j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > BAND * abs(wt))
                if tr and V > 0:
                    nu = wt * V / o[i, j]; dd = nu - B["u"][j]; fee = abs(dd) * o[i, j] * COST
                    B["cash"] -= dd * o[i, j] + fee; B["u"][j] = nu
                    if b != "x1": B["fees"] += fee; B["trades"] += 1
        gw = (B["u"] * lo[i]).sum(); worst = B["cash"] + gw
        if gw > 0 and worst <= MAINT * gw:
            B["cash"] = max(worst - gw * COST, 0); B["u"][:] = 0
            if b != "x1": B["liq"] += 1
    E1 = books["x1"]["cash"] + (books["x1"]["u"] * c[i]).sum(); e1.append(E1 / prev1 - 1); prev1 = E1
    w1 = W1.iloc[i].values; r = np.array(e1[-60:]); s60 = np.std(r, ddof=1) * math.sqrt(365) if len(r) >= 20 else float("nan")
    pend = {"x1": w1}
    for b, tv in TV.items():
        L = 1.0 if len(r) < 20 else min(CAP, tv / max(s60, 0.05)); pend[b] = w1 * L
        if b in books and d >= START:
            B = books[b]; eq = B["cash"] + (B["u"] * c[i]).sum(); gross = (np.abs(B["u"]) * c[i]).sum()
            rows.append(dict(date=str(d.date()), book=b, equity=round(eq, 4), btc_units=B["u"][0], eth_units=B["u"][1],
                             gross_lev=round(gross / eq, 4) if eq > 0 else None, target_L_next=round(L, 4),
                             w_next_btc=round(pend[b][0], 4), w_next_eth=round(pend[b][1], 4), sigma60=round(s60, 4),
                             sig_btc=int(sc["BTC"].iloc[i] > sma["BTC"].iloc[i]), sig_eth=int(sc["ETH"].iloc[i] > sma["ETH"].iloc[i]),
                             fees=round(B["fees"], 4), funding=round(B["fund"], 4), trades=B["trades"], liq=B["liq"],
                             btc_close=c[i, 0], eth_close=c[i, 1]))
L = pd.DataFrame(rows); L.to_csv(OUT / "ledger.csv", index=False)
last = str(idx[-1].date()); print("last closed bar", last)
print(L.tail(6).to_string())
