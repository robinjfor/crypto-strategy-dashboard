"""R2S backtest ledger (box-side; needs the local data.binance.vision cache). Runs research_v2.py verbatim up to the R2S WF call,
then the exact min5_check.py K=1000 path (2 equity passes, deadband_abs=5U/equity) with return_held, and dumps per-day target
weights, pre-trade drifted weights, equity at open and every order (day, symbol, new_w, pre_w, signed notional). No orders, no keys."""
import sys; sys.argv=["x"]; from pathlib import Path
HERE0=Path("/workspace/strategy-unified-3y/spec_2026-11/research_regime_v2")
__file__=str(HERE0/"research_v2.py")
exec(open(HERE0/"research_v2.py").read().split("wf_generic(\"R2S_smooth_hyst_1d\"")[0])
import json, pickle, os
OUT=os.environ.get("R2S_OUT","/workspace/r2s")
K=1000.0
picks=[p for p in zip([f[2] for f in wf_folds()],[f[3] for f in wf_folds()],json.load(open(HERE/"results.json"))["results"]["R2S_smooth_hyst_1d"]["picks"])]
def W_of(k):
    d=dict(th=int(k.split("_")[0][2:]),h=int(k.split("_")[1][1:]),hv=float(k.split("_")[2][2:]),L=float(k.split("_")[3][1:])); return r2s_W(d)
Ws={b:W_of(b) for _,_,b in picks}
W=pd.concat([Ws[b].loc[a-pd.Timedelta(days=1):e-pd.Timedelta(days=1)] for a,e,b in picks]); W=W[~W.index.duplicated()]
r0,to=pnl(W,deadband_rel=BAND)
eq=(1+r0).cumprod()*K
for it in range(2):
    eqo=eq.shift(1).reindex(W.index+pd.Timedelta(days=1)).fillna(K).values
    dba=pd.DataFrame(np.repeat((5.0/eqo)[:,None],W.shape[1],1),index=W.index,columns=W.columns)
    r,_,HM=pnl(W,deadband_rel=BAND,deadband_abs=dba,return_held=True); eq=(1+r[OOS_START:]).cumprod()*K; eq=eq.reindex(r.index).ffill().fillna(K)
# drifted pre-trade held weights, recomputed exactly as pnl does
cols=W.columns; R=G0["RET_OO"][cols].reindex(W.index).fillna(0).values; Hm=HM.values
pre=np.zeros_like(Hm); held=np.zeros(len(cols))
for i in range(len(W)):
    pre[i]=held; new=Hm[i]; g=(new*R[i]).sum(); held=new*(1+R[i])/(1+g) if 1+g>0 else new*0
orders=[]
for i in range(len(W)):
    for j in np.nonzero(Hm[i]!=pre[i])[0]:
        orders.append((str((W.index[i]+pd.Timedelta(days=1)).date()),cols[j],float(Hm[i,j]),float(pre[i,j]),float((Hm[i,j]-pre[i,j])*eqo[i])))
pickle.dump(dict(W=W.values,cols=list(cols),idx=[str(d.date()) for d in W.index],eqo=eqo,pre=pre,held=Hm,orders=orders,picks=[(str(a.date()),str(e.date()),b) for a,e,b in picks],
  sharpe=metrics(r[OOS_START:OOS_END])["sharpe"],
  ev=list(EV)),open(OUT+"/r2s_ledger.pkl","wb"))
pd.to_pickle(dict(C=C,H=H,L=Lo,QV=QV),OUT+"/r2s_panel.pkl")
print("orders",len(orders),"sharpe",metrics(r[OOS_START:OOS_END])["sharpe"])
