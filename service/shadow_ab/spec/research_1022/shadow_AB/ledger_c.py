"""Generic paper ledger (env LEDGER_NAME/LEDGER_W/LEDGER_LV; defaults = C). Paper shadow ledger C (NO orders, public Binance data only). Same daily method as shadow A/B
(u1_shorts/shadow_tv040/shadow.py): every run refetches closed daily bars + real funding and recomputes the whole ledger from START.
C = 80% U1 tv0.40 x2.5 (tv=1.00, L cap 3; BTCUSDC/ETHUSDC perps, cross) + 20% S5 long/short x2.5 (X=5M, tv0.2, b0.1 = WF pick for
2026-Q4, frozen; USDT-M perps, isolated per leg), breaker -10% -> x0.5 (back to x1 when DD > -5%), -40% -> flat 30 days then x0.5 with HWM reset.
Start 2026-10-23 00:00 UTC open, 1000 USDC, 4 weeks (to 2026-11-20). Run daily after 00:05 UTC (08:05 Taipei): python3 ledger_c.py"""
import os, sys, json, math, time, urllib.request, numpy as np, pandas as pd
from pathlib import Path
OUT = Path(__file__).resolve().parent; DD_ = OUT / "data"; (DD_ / "fr").mkdir(parents=True, exist_ok=True); (DD_ / "kl").mkdir(exist_ok=True)
START = pd.Timestamp(os.environ.get("LEDGER_TEST_START", "2026-10-23"), tz="UTC"); END = pd.Timestamp("2026-11-20", tz="UTC"); INIT = 1000.0
NAME = os.environ.get("LEDGER_NAME", "C"); W_U1 = float(os.environ.get("LEDGER_W", "0.8")); LV = float(os.environ.get("LEDGER_LV", "2.5")); S5CFG = dict(X=5e6, tv=0.2, b=0.1); COST, MAINT, CAP = 0.002, 0.005, 3.0
FAPI = "https://fapi.binance.com"
def get(url):
    for a in range(5):
        try: return json.loads(urllib.request.urlopen(url, timeout=30).read())
        except Exception as e: err = e; time.sleep(2 + 2 * a)
    raise err
def log(msg):
    with open(OUT / "run.log", "a") as f: f.write(f"{pd.Timestamp.now(tz='Asia/Taipei'):%Y-%m-%d %H:%M} (Asia/Taipei) ledger_{NAME} (w={W_U1}, Lv={LV}): {msg}\n")
# ---------------- 1. data refresh (incremental cache; delisted symbols keep their cached history)
def refresh():
    info = get(FAPI + "/fapi/v1/exchangeInfo")
    syms = {x["symbol"] for x in info["symbols"] if x["contractType"] == "PERPETUAL" and x["quoteAsset"] == "USDT" and x["status"] == "TRADING"}
    syms |= {p.stem for p in (DD_ / "kl").glob("*.csv")}
    now_ms = time.time() * 1000
    for s in sorted(syms):
        kp = DD_ / "kl" / f"{s}.csv"; old = pd.read_csv(kp) if kp.exists() else None
        st = int(old.ot.max()) + 1 if old is not None and len(old) else int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=700)).timestamp() * 1000)
        try: r = get(f"{FAPI}/fapi/v1/klines?symbol={s}&interval=1d&startTime={st}&limit=1500")
        except Exception: r = []
        new = pd.DataFrame([[x[0], float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7]), x[6]] for x in r], columns=["ot", "open", "high", "low", "close", "quote_volume", "ct"])
        new = new[new.ct < now_ms].drop(columns="ct")
        df = pd.concat([old, new]) if old is not None else new
        if len(df): df.drop_duplicates("ot").sort_values("ot").to_csv(kp, index=False)
        fp = DD_ / "fr" / f"{s}_all.csv"; fo = pd.read_csv(fp) if fp.exists() else None
        fst = int(fo.calc_time.max()) + 1 if fo is not None and len(fo) else int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=400)).timestamp() * 1000)
        rows = []
        while True:
            try: rr = get(f"{FAPI}/fapi/v1/fundingRate?symbol={s}&startTime={fst}&limit=1000")
            except Exception: rr = []
            rows += rr
            if len(rr) < 1000: break
            fst = rr[-1]["fundingTime"] + 1
        fn = pd.DataFrame([[x["fundingTime"], 8, float(x["fundingRate"])] for x in rows], columns=["calc_time", "funding_interval_hours", "last_funding_rate"])
        f = pd.concat([fo, fn]) if fo is not None else fn
        if len(f): f.drop_duplicates("calc_time").sort_values("calc_time").to_csv(fp, index=False)
        time.sleep(0.15)
    frames = {}
    for kp in (DD_ / "kl").glob("*.csv"):
        d = pd.read_csv(kp); d.index = pd.to_datetime(d.ot, unit="ms", utc=True); frames[kp.stem] = d[["open", "high", "low", "close", "quote_volume"]]
    pd.concat(frames, axis=1).sort_index().to_pickle(DD_ / "panel_1d.pkl")
if "--no-fetch" not in sys.argv: refresh()
os.environ["SPEC_DATA_DIR"] = str(DD_)
sys.path.insert(0, str(OUT.parent)); sys.path.insert(0, "/workspace/strategy-unified-3y/spec_2026-11/u1_shorts/shadow_tv040")
import lib, s5_ewmac as S5
# ---------------- 2. S5 targets (decided at close t), exposure-unit weights
W5u, SP5u = S5.weights(S5CFG, False)            # pre-leverage targets & standard position
P = lib.P; O5, H5, L5, C5 = P["open"], P["high"], P["low"], P["close"]; F5 = lib.fund()
# ---------------- 3. U1 inputs (same fetch as shadow A/B)
def klines(base, sym, days=520):
    st = int((pd.Timestamp.now(tz="UTC").normalize() - pd.Timedelta(days=days)).timestamp() * 1000); rows = []
    while True:
        r = get(f"{base}?symbol={sym}&interval=1d&startTime={st}&limit=1000"); rows += r
        if len(r) < 1000: break
        st = r[-1][0] + 1
    df = pd.DataFrame([[x[0], float(x[1]), float(x[3]), float(x[4]), x[6]] for x in rows], columns=["t", "o", "l", "c", "ct"])
    df = df[df.ct < time.time() * 1000]; df.index = pd.to_datetime(df.t, unit="ms", utc=True); return df
def funding(sym, days=120):
    st = int((pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)).timestamp() * 1000)
    r = get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&startTime={st}&limit=1000")
    return pd.Series([float(x["fundingRate"]) for x in r], index=pd.to_datetime([x["fundingTime"] for x in r], unit="ms", utc=True))
U = ["BTC", "ETH"]
SP = {k: klines("https://api.binance.com/api/v3/klines", k + "USDT") for k in U}
FU = {k: klines(FAPI + "/fapi/v1/klines", k + "USDC") for k in U}; FR = {k: funding(k + "USDC") for k in U}
idx = FU["BTC"].index.intersection(FU["ETH"].index).intersection(SP["BTC"].index).intersection(SP["ETH"].index)
uo = np.c_[[FU[k].o.reindex(idx).values for k in U]].T; ul = np.c_[[FU[k].l.reindex(idx).values for k in U]].T; uc = np.c_[[FU[k].c.reindex(idx).values for k in U]].T
sc = {k: SP[k].c.reindex(idx) for k in U}
sg20 = {k: np.log(sc[k] / sc[k].shift(1)).rolling(20, min_periods=15).std() * math.sqrt(365) for k in U}
W1 = pd.DataFrame({k: ((sc[k] > sc[k].rolling(200, min_periods=200).mean()).astype(float) * 0.5 * np.minimum(1.0, 0.60 / sg20[k])).fillna(0.0) for k in U})
def fday(k):
    s = FR[k]; return s.groupby((s.index - pd.Timedelta(milliseconds=1)).floor("D")).sum().reindex(idx).fillna(0.0).values
ufr_prev = np.r_[np.zeros((1, 2)), np.c_[[fday(k) for k in U]].T[:-1]]
# ---------------- 4. ledger simulation
x1 = dict(cash=1.0, u=np.zeros(2)); e1 = []; prev1 = 1.0; pendU = None
acct = None; rows = []; m = 1.0; hwm = INIT; flat_left = 0
fund5_prev = F5.shift(1).fillna(0.0)               # settlement bucketed to day d-1 = 00:00 d, applied at open d
for i, d in enumerate(idx):
    # 1x shadow book (needed for sigma60 / L, as A/B)
    x1["cash"] -= (x1["u"] * uo[i] * ufr_prev[i]).sum()
    if pendU is not None:
        V = x1["cash"] + (x1["u"] * uo[i]).sum()
        for j in range(2):
            wt = pendU["x1"][j]; wc = x1["u"][j] * uo[i, j] / V if V > 0 else 0
            if (x1["u"][j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > 0.25 * abs(wt)):
                nu = wt * V / uo[i, j]; dd_ = nu - x1["u"][j]; x1["cash"] -= dd_ * uo[i, j] + abs(dd_) * uo[i, j] * COST; x1["u"][j] = nu
    if d == START:
        acct = dict(u1=dict(cash=INIT * W_U1, u=np.zeros(2)), s5=dict(cash=INIT * (1 - W_U1), u=pd.Series(dtype=float)), fees=0.0, fund=0.0, liq=0, leg_liq=0)
    if acct is not None and d >= START and d in O5.index:
        A = acct; o5 = O5.loc[d]
        # funding at open
        f = (A["u1"]["u"] * uo[i] * ufr_prev[i]).sum(); A["u1"]["cash"] -= f; A["fund"] += f
        if len(A["s5"]["u"]):
            fr = fund5_prev.loc[d].reindex(A["s5"]["u"].index).fillna(0); f5 = (A["s5"]["u"] * o5.reindex(A["s5"]["u"].index) * fr).sum(); A["s5"]["cash"] -= f5; A["fund"] += f5
        # equity at open, daily internal re-split to 80/20 (no cost)
        eu = A["u1"]["cash"] + (A["u1"]["u"] * uo[i]).sum(); es = A["s5"]["cash"] + (A["s5"]["u"] * o5.reindex(A["s5"]["u"].index).fillna(0)).sum()
        E = eu + es; A["u1"]["cash"] += W_U1 * E - eu; A["s5"]["cash"] += (1 - W_U1) * E - es; eu, es = W_U1 * E, (1 - W_U1) * E
        # U1 rebalance (25% band) to targets decided last close
        if pendU is not None and "lev" in pendU:
            for j in range(2):
                wt = pendU["lev"][j]; wc = A["u1"]["u"][j] * uo[i, j] / eu if eu > 0 else 0
                if (A["u1"]["u"][j] != 0) if wt == 0 else (wc == 0 or (wt > 0) != (wc > 0) or abs(wt - wc) > 0.25 * abs(wt)):
                    nu = wt * eu / uo[i, j]; dd_ = nu - A["u1"]["u"][j]; fee = abs(dd_) * uo[i, j] * COST
                    A["u1"]["cash"] -= dd_ * uo[i, j] + fee; A["u1"]["u"][j] = nu; A["fees"] += fee
        # S5 rebalance with buffer to targets decided at previous close
        prev_d = d - pd.Timedelta(days=1)
        if prev_d in W5u.index:
            tw = W5u.loc[prev_d] * LV * m; db = SP5u.loc[prev_d] * LV * m * S5CFG["b"]
            cur_u = A["s5"]["u"].reindex(tw.index).fillna(0.0); cw = cur_u * o5.reindex(tw.index).fillna(0) / es if es > 0 else cur_u * 0
            trade = ((tw - cw).abs() > db) | ((tw == 0) & (cw != 0)) | (np.sign(tw) != np.sign(cw))
            trade &= o5.reindex(tw.index).notna()
            nw = cw.where(~trade, tw); dn = (nw - cw).abs().sum() * es * COST
            nu = (nw * es / o5.reindex(tw.index)).where(nw != 0, 0.0).fillna(0.0)
            A["s5"]["cash"] -= ((nu - cur_u) * o5.reindex(tw.index).fillna(0)).sum() + dn; A["fees"] += dn
            A["s5"]["u"] = nu[nu != 0]
        # intraday liquidation: U1 cross on Lows
        gw = (A["u1"]["u"] * ul[i]).sum(); worst = A["u1"]["cash"] + gw
        if gw > 0 and worst <= MAINT * gw: A["u1"]["cash"] = max(worst - gw * COST, 0); A["u1"]["u"][:] = 0; A["liq"] += 1
        # S5 isolated per leg
        if len(A["s5"]["u"]):
            uu = A["s5"]["u"]; op = o5.reindex(uu.index); notional = (uu.abs() * op); G = notional.sum() / es if es > 0 else 0
            if G > 0:
                adv = np.where(uu > 0, 1 - L5.loc[d].reindex(uu.index) / op, H5.loc[d].reindex(uu.index) / op - 1)
                liq = pd.Series(adv >= 1 / G - MAINT, index=uu.index)
                for s in uu.index[liq.values]:
                    margin = notional[s] / G; A["s5"]["cash"] += uu[s] * op[s] - margin - abs(uu[s]) * op[s] * COST  # lose margin, close leg
                    A["s5"]["u"] = A["s5"]["u"].drop(s); A["leg_liq"] += 1
        # mark at close
        c5 = C5.loc[d]
        eu = A["u1"]["cash"] + (A["u1"]["u"] * uc[i]).sum(); es = A["s5"]["cash"] + (A["s5"]["u"] * c5.reindex(A["s5"]["u"].index).fillna(0)).sum(); E = eu + es
        hwm = max(hwm, E); ddv = E / hwm - 1; m_used = m
        if m == 0:
            flat_left -= 1
            if flat_left <= 0: m = 0.5; hwm = E
        elif ddv <= -0.40: m = 0.0; flat_left = 30
        elif m == 1 and ddv <= -0.10: m = 0.5
        elif m == 0.5 and ddv > -0.05: m = 1.0
        rows.append(dict(date=str(d.date()), equity=round(E, 4), u1_equity=round(eu, 4), s5_equity=round(es, 4), exposure_mult_used=m_used,
                         exposure_mult_next=m, drawdown=round(ddv, 4), hwm=round(hwm, 4), u1_btc_units=A["u1"]["u"][0], u1_eth_units=A["u1"]["u"][1],
                         u1_gross=round((np.abs(A["u1"]["u"]) * uc[i]).sum() / max(eu, 1e-9), 3),
                         s5_gross=round((A["s5"]["u"].abs() * c5.reindex(A["s5"]["u"].index).fillna(0)).sum() / max(es, 1e-9), 3), s5_n_pos=len(A["s5"]["u"]),
                         fees=round(A["fees"], 4), funding=round(A["fund"], 4), u1_liq=A["liq"], s5_leg_liq=A["leg_liq"], flat_days_left=flat_left))
    # next-day U1 targets
    E1 = x1["cash"] + (x1["u"] * uc[i]).sum(); e1.append(E1 / prev1 - 1); prev1 = E1
    r = np.array(e1[-60:]); s60 = np.std(r, ddof=1) * math.sqrt(365) if len(r) >= 20 else float("nan")
    Lt = 1.0 if len(r) < 20 else min(CAP, 0.40 * LV * m / max(s60, 0.05))
    pendU = {"x1": W1.iloc[i].values, "lev": W1.iloc[i].values * Lt if m > 0 else np.zeros(2)}
L = pd.DataFrame(rows); L.to_csv(OUT / (f"ledger_{NAME}.csv" if "LEDGER_TEST_START" not in os.environ else f"test_ledger_{NAME}.csv"), index=False)
msg = f"last closed bar {idx[-1].date()}; rows {len(L)}" + (f"; equity {L.equity.iloc[-1]:.2f} DD {L.drawdown.iloc[-1]:.3f} mult {L.exposure_mult_next.iloc[-1]}" if len(L) else " (ledger starts 2026-10-23 open; first row after that bar closes)")
(log(msg) if "LEDGER_TEST_START" not in os.environ else None); print(msg)
if len(L): print(L.tail(5).to_string())
