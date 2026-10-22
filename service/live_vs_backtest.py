"""Daily live-vs-backtest trade-by-trade comparison (spec 3.3 last row, daily after UTC 00:30).

For each live closed trade of a supported family, the slot's backtest (bt_reference = the research
engine, fill at signal-bar close) is replayed on Binance klines and matched by bars:
entry bar, exit bar, direction, exit reason, entry/exit price (<= 30 bps).
A mismatch on a trade closed in the last 48 h pauses that slot (single-slot pause) and records an alert;
older history is shown, never paused on. Manual trades (人工單) and U1 are excluded.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pandas as pd

log = logging.getLogger(__name__)

PRICE_BPS_TOL = 30.0
PAUSE_WINDOW_H = 48
CLOSE_FILL = {"donchian_atr", "donchian", "donchian_btc_regime"}
NEXT_OPEN = {"short_trend_perp"}
U1_FAMILY = "btc_eth_trend_voltarget_perp"
SUPPORTED = CLOSE_FILL | NEXT_OPEN
TF_DELTA = {"1h": pd.Timedelta(hours=1), "2h": pd.Timedelta(hours=2), "4h": pd.Timedelta(hours=4),
            "6h": pd.Timedelta(hours=6), "12h": pd.Timedelta(hours=12),
            "1d": pd.Timedelta(days=1)}
REASON = {"stop": "stop_loss", "stop_loss": "stop_loss", "donch_lo": "signal_exit", "signal_exit": "signal_exit",
          "max_hold": "max_hold", "exit": "signal_exit", "exit_signal": "signal_exit", "reverse": "reverse"}


def _backtest(slot: dict, kl: pd.DataFrame, btc: pd.DataFrame | None) -> list[dict]:
    import bt_reference as ref
    ind = ref.add_donch(kl, int(slot.get("donch_n") or 20), atr_mode=str(slot.get("atr_mode") or "sma"))
    sig = ref.signal_donchian(ind)
    if slot.get("btc_regime") and btc is not None:
        b = btc.copy()
        b["sma_200"] = b["Close"].rolling(200).mean()
        sig = ref.apply_btc_regime(sig, b)
    res = ref.run_donchian_engine(ind, sig, float(slot.get("stop_atr_mult") or 2.0),
                                  float(slot.get("trail_atr_mult") or 0.0), int(slot.get("max_hold_bars") or 10**6),
                                  reset_below_hi=bool(slot.get("require_reset_below_hi")))
    out = []
    for t in res.trades:
        if t["reason"] == "eod_flat":
            continue
        out.append({"entry_bar": pd.Timestamp(t["entry"]), "exit_bar": pd.Timestamp(t["exit"]), "side": "LONG",
                    "reason": t["reason"], "entry_px": t["entry_px"] / (1 + ref.COST),
                    "exit_px": t["exit_px"] / (1 - ref.COST)})
    return out


def _backtest_short(slot: dict, kl: pd.DataFrame, funding, btc_daily, coin_daily) -> list[dict]:
    import bt_short_trend as S
    D = S.Data(kl, funding, btc_daily, coin_daily, str(slot["tf"]))
    fam = str(slot.get("signal") or "sema")
    p = {"filt": str(slot.get("filter") or "none")}
    if fam == "sema":
        p["pair"] = S.PAIRS.index((int(slot["ema_fast"]), int(slot["ema_slow"])))
    else:
        p["n"] = int(slot["donch_n"])
    ent, *x = S.signals(D, fam, p)
    trades, _ = S.bt(D, ent, tuple(x), float(slot["stop_atr_mult"]), float(slot["trail_atr_mult"]),
                     int(slot.get("max_hold_bars") or 60))
    out = []
    for t in trades:
        if t["reason"] == "eod_flat":
            continue
        short = t["side"] < 0
        out.append({"entry_bar": pd.Timestamp(t["entry"]), "exit_bar": pd.Timestamp(t["exit"]),
                    "side": "SHORT" if short else "LONG", "reason": t["reason"],
                    "entry_px": t["entry_px"] / ((1 - S.COST) if short else (1 + S.COST)),
                    "exit_px": t["exit_px"] / ((1 + S.COST) if short else (1 - S.COST))})
    return out


# Root causes found 2026-10-21 (rd_daily/2026-10-21/PARITY_ROOTCAUSE.md), keyed by slot + close time.
_PRE = "舊版 runner（10/08 前 backfill／位準進場，9e6f7db 已修）"
_SLIP = "Demo 市價單滑價（非 bug）"
KNOWN_CAUSES = {
    ("sat_fil_1h", "2026-09-27T03:25"): _PRE, ("sat_dot_4h", "2026-09-27T04:40"): _PRE,
    ("sat_op_4h", "2026-09-27T08:01"): _PRE, ("sat_fet_4h", "2026-09-29T06:06"): _PRE,
    ("sat_fil_1h", "2026-10-04T03:03"): "停損錨點用成交價（bug，10/21 已修：改用訊號收盤×(1+20bps)）",
    ("sat_fil_1h", "2026-10-05T23:32"): _PRE, ("sat_fil_1h", "2026-10-06T03:05"): _PRE,
    ("sat_apt_1h", "2026-10-07T09:58"): _PRE,
    ("sat_op_4h", "2026-10-12T21:29"): _SLIP, ("sat_dot_4h", "2026-10-13T00:54"): _SLIP,
}


def _bps(a, b):
    return None if not a or not b else round((float(a) / float(b) - 1) * 1e4, 2)


def compare_trade(live: dict, bt: list[dict], tf: str, model: str = "close_fill") -> dict:
    """live: {entry_ms, exit_ms, entry_px, exit_px, side, reason}. Bars: open time of the bar.
    close_fill (C): backtest fills at the signal-bar close → live fill lands in the next bar.
    next_open (S): backtest fills at the next bar's open → live fill lands in that same bar."""
    d = TF_DELTA.get(tf, pd.Timedelta(hours=1))
    e_fill = pd.Timestamp(live["entry_ms"], unit="ms", tz="UTC")
    x_fill = pd.Timestamp(live["exit_ms"], unit="ms", tz="UTC")
    if model == "next_open":
        live_entry_bar, live_exit_bar = e_fill.floor(d), x_fill.floor(d)
    else:
        live_entry_bar = e_fill.floor(d) - d   # runner fills right after the signal bar closes
        live_exit_bar = x_fill.floor(d)        # resting stop fills inside the stop bar
        if REASON.get(str(live.get("reason")), "signal_exit") != "stop_loss":
            live_exit_bar = x_fill.floor(d) - d
    m = min(bt, key=lambda t: abs((t["entry_bar"] - live_entry_bar).total_seconds()), default=None)
    row = {"live_entry_bar": live_entry_bar.isoformat(), "live_exit_bar": live_exit_bar.isoformat(),
           "live_reason": REASON.get(str(live.get("reason")), live.get("reason")), "live_side": live.get("side", "LONG"),
           "live_entry_px": live.get("entry_px"), "live_exit_px": live.get("exit_px")}
    if m is None or abs((m["entry_bar"] - live_entry_bar).total_seconds()) > d.total_seconds() * 0.5:
        return {**row, "match": False, "problems": ["no_backtest_trade_at_entry_bar"],
                "bt_entry_bar": m["entry_bar"].isoformat() if m else None}
    probs = []
    if m["exit_bar"] != live_exit_bar:
        probs.append("exit_bar")
    if m["side"] != row["live_side"]:
        probs.append("direction")
    if m["reason"] != row["live_reason"]:
        probs.append("reason")
    eb, xb = _bps(live.get("entry_px"), m["entry_px"]), _bps(live.get("exit_px"), m["exit_px"])
    if eb is None or abs(eb) > PRICE_BPS_TOL:
        probs.append("entry_price")
    if xb is None or abs(xb) > PRICE_BPS_TOL:
        probs.append("exit_price")
    return {**row, "bt_entry_bar": m["entry_bar"].isoformat(), "bt_exit_bar": m["exit_bar"].isoformat(),
            "bt_reason": m["reason"], "bt_entry_px": m["entry_px"], "bt_exit_px": m["exit_px"],
            "entry_bps": eb, "exit_bps": xb, "match": not probs, "problems": probs}


def live_trades_from_ledger(ledger_doc: dict, closed: list[dict]) -> list[dict]:
    """Closed slot trades with Binance entry/exit fills (avg price, first fill time)."""
    from ledger import diff_closed_trades  # noqa: F401  (same matching rules)
    fills = (ledger_doc or {}).get("fills") or []
    by_oid: dict[str, list] = {}
    for r in fills:
        by_oid.setdefault(f"{r['venue']}|{r['orderId']}", []).append(r)
    out = []
    for t in closed or []:
        if t.get("manual") or str(t.get("slot") or "").startswith("u1") or t.get("family") == "btc_eth_trend_voltarget_perp":
            continue
        venue = t.get("venue") or "spot"
        ex = by_oid.get(f"{venue}|{t.get('order_id')}", [])
        if not ex:
            continue
        x_ms = min(r["time_ms"] for r in ex)
        q = sum(r["qty"] for r in ex)
        ent = [r for r in fills if r["symbol"] == t.get("symbol") and r["venue"] == venue
               and r["side"] != ex[0]["side"] and r["time_ms"] < x_ms and r.get("slot") == t.get("slot")]
        if not ent:
            continue
        last_oid = ent[-1]["orderId"]
        ent = [r for r in ent if r["orderId"] == last_oid]
        eq = sum(r["qty"] for r in ent)
        out.append({"slot": t.get("slot"), "code": t.get("code"), "symbol": t.get("symbol"), "family": t.get("family"),
                    "closed_at": t.get("closed_at"), "side": t.get("side") or "LONG", "reason": t.get("reason"),
                    "entry_ms": min(r["time_ms"] for r in ent), "exit_ms": x_ms,
                    "entry_px": sum(r["qty"] * r["price"] for r in ent) / eq,
                    "exit_px": sum(r["qty"] * r["price"] for r in ex) / q})
    return out


U1_WEIGHT_TOL = 0.10  # |live weight − paper target weight| as a fraction of the target


def u1_rows(state: dict, paper_u1: dict | None) -> list[dict]:
    """U1 executes the paper engine's *decision* (targets for the fill bar), not the paper book's fills
    (the paper book was already at target on 10/21, so it had no fill while the new live sleeve bought).
    Each runner fill is checked against the paper decision with the same fill bar: direction toward the
    target and resulting weight within 10% of targets_lev. Decisions older than the paper's retained
    window give match=None (not a mismatch)."""
    sl = state.get("u1_live") or {}
    decs = {pd.Timestamp(d["fill_bar"]).tz_convert("UTC") if pd.Timestamp(d["fill_bar"]).tzinfo
            else pd.Timestamp(d["fill_bar"], tz="UTC"): d
            for d in (paper_u1 or {}).get("decisions") or [] if d.get("fill_bar")}
    rows = []
    for f in sl.get("fills") or []:
        if f.get("external") or f.get("manual"):
            continue
        coin = "BTC" if str(f.get("symbol", "")).startswith("BTC") else "ETH"
        tag = str(f.get("fill_bar") or "")
        bar = pd.Timestamp(tag[:8] + "T" + tag[8:10] + ":00:00", tz="UTC") if len(tag) >= 10 else (
            pd.Timestamp(f["filled_at"]).tz_convert("UTC").floor("h") if f.get("filled_at") else None)
        side = "LONG" if float(f.get("qty_signed") or 0) > 0 else "SHORT"
        base = {"slot": sl.get("slot") or "u1", "code": "U1", "symbol": f.get("symbol"), "family": U1_FAMILY,
                "tf": "1d", "closed_at": f.get("filled_at"), "live_entry_bar": bar.isoformat() if bar is not None else None,
                "live_side": side, "live_entry_px": f.get("price")}
        d = decs.get(bar) if bar is not None else None
        if d is None:
            rows.append({**base, "match": None, "problems": ["paper_decision_not_retained" if decs else "paper_state_unavailable"]})
            continue
        tw = float((d.get("targets_lev") or {}).get(coin) or 0)
        eq = float(f.get("equity_before") or sl.get("capital_usdt") or 0)
        lw = float(f.get("units_after") or 0) * float(f.get("price") or 0) / eq if eq else None
        wb = lw - float(f.get("qty_signed") or 0) * float(f.get("price") or 0) / eq if (eq and lw is not None) else None
        probs = []
        if wb is not None and ((tw - wb) > 0) != (side == "LONG"):
            probs.append("direction")
        if lw is None or (abs(lw - tw) > U1_WEIGHT_TOL * max(abs(tw), 1e-9)):
            probs.append("weight")
        rows.append({**base, "bt_entry_bar": bar.isoformat(), "bt_side": "LONG" if tw >= (wb or 0) else "SHORT",
                     "live_weight": round(lw, 4) if lw is not None else None, "bt_weight": round(tw, 4),
                     "decision_bar": d.get("decision_bar"), "match": not probs, "problems": probs})
    return rows


def run(state: dict, ledger_doc: dict, *, client, slot_lookup, now: datetime | None = None,
        paper_u1: dict | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    rows, cache = [], {}
    btc = None
    for lt in live_trades_from_ledger(ledger_doc, state.get("closed_trades") or []):
        slot = slot_lookup(lt["slot"]) or {}
        fam = str(slot.get("family") or lt.get("family") or "")
        base = {k: lt.get(k) for k in ("slot", "code", "symbol", "closed_at")}
        if fam not in SUPPORTED:
            rows.append({**base, "family": fam, "match": None, "problems": ["family_not_supported_yet"]})
            continue
        tf = str(slot.get("tf") or "1h")
        key = (lt["symbol"], tf, fam in NEXT_OPEN)
        try:
            if fam in NEXT_OPEN:
                from perp_market import KLINES_MAX, fetch_funding, fetch_perp_klines
                if key not in cache:
                    filt = str(slot.get("filter") or "none")
                    cache[key] = (fetch_perp_klines(lt["symbol"], tf, limit=KLINES_MAX),
                                  fetch_funding(lt["symbol"], limit=1000) if "fpos" in filt else None,
                                  fetch_perp_klines("BTCUSDT", "1d", limit=KLINES_MAX) if filt.startswith("btc") else None,
                                  fetch_perp_klines(lt["symbol"], "1d", limit=400) if filt == "coin50" else None)
                bt = _backtest_short(slot, *cache[key])
                rows.append({**base, "family": fam, "tf": tf, **compare_trade(lt, bt, tf, "next_open")})
                continue
            if key not in cache:
                cache[key] = client.fetch_klines(lt["symbol"], tf, limit=1000, use_vision=True)
            if slot.get("btc_regime") and btc is None:
                btc = client.fetch_klines("BTCUSDT", "1d", limit=400, use_vision=True)
            bt = _backtest(slot, cache[key], btc)
            rows.append({**base, "family": fam, "tf": tf, **compare_trade(lt, bt, tf)})
        except Exception as e:  # noqa: BLE001
            rows.append({**base, "family": fam, "match": None, "problems": [f"error: {str(e)[:120]}"]})
    rows.extend(u1_rows(state, paper_u1))
    for r in rows:
        c = KNOWN_CAUSES.get((r.get("slot"), str(r.get("closed_at") or "")[:16]))
        if c and r.get("match") is False:
            r["root_cause"] = c
    recent_cut = now - timedelta(hours=PAUSE_WINDOW_H)
    for r in rows:
        try:
            closed = datetime.fromisoformat(str(r.get("closed_at")))
        except Exception:  # noqa: BLE001
            closed = None
        r["pause"] = bool(r.get("match") is False and closed and closed >= recent_cut)
    n_ok = sum(1 for r in rows if r.get("match") is True)
    n_bad = sum(1 for r in rows if r.get("match") is False)
    return {"at": now.astimezone(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "day_utc": now.date().isoformat(), "rows": rows, "n_match": n_ok, "n_mismatch": n_bad,
            "n_unsupported": sum(1 for r in rows if r.get("match") is None),
            "tolerance_bps": PRICE_BPS_TOL, "pause_window_h": PAUSE_WINDOW_H}


def due(state: dict, now: datetime | None = None, force: bool = False) -> bool:
    if force:
        return True
    now = now or datetime.now(timezone.utc)
    last = (state.get("live_vs_backtest") or {}).get("day_utc")
    return (now.hour, now.minute) >= (0, 30) and last != now.date().isoformat()


def apply(st: dict, rep: dict) -> dict:
    ps = dict(st.get("paused_slots") or {})
    alerts = list(st.get("reconcile_alerts") or [])
    keys = {a.get("key") for a in alerts}
    for r in rep["rows"]:
        if not r.get("pause"):
            continue
        key = f"lvb|{r['slot']}|{r.get('closed_at')}"
        if key not in keys:
            alerts.append({"at": rep["at"], "key": key, "venue": "backtest", "symbol": r.get("symbol"),
                           "slots": [r["slot"]], "kind": "live_vs_backtest_mismatch", "pause": True,
                           "problems": r.get("problems"), "trader": None, "binance": None})
        if r["slot"] not in ps:
            ps[r["slot"]] = {"paused_at": rep["at"], "paused_by": "live_vs_backtest",
                             "reason": "live_vs_backtest " + ",".join(r.get("problems") or [])}
    st["paused_slots"] = ps
    st["reconcile_alerts"] = alerts[-100:]
    st["live_vs_backtest"] = rep
    return st
