"""Cloud risk guards (key_signals_2026-10-22 §4). They can only REDUCE exposure or pause:
  1) macro events (FOMC/CPI/NFP): block new entries from 1h before to 4h after the release;
  2) Binance delisting / contract-change announcements: slot-pause the affected coins + alert (never close);
  4) USDT/USDC depeg > 0.5%: alert + set futures leverage to 1x (leverage setting only, never an order);
  5) extreme 15m range (> 4σ): block new entries for 2h.
Entry blocks live in state["guard_blocks"] and expire by themselves. Guards never lift a slot pause or the
account pause, and never place, change or cancel an order. Every action is written to control_log, actor=guard.
Market indicators (crowding light, ETF flows, fear & greed, open interest) are display only."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)
TZ = timezone(timedelta(hours=8))
CAL_PATH = Path(__file__).resolve().parent / "macro_calendar.json"
PRE, POST = timedelta(hours=1), timedelta(hours=4)
SPIKE_BLOCK = timedelta(hours=2)
DEPEG_LIMIT = 0.005
SPIKE_SIGMA = 4.0
ANN_CATALOGS = (161, 48)          # Delisting, Latest Binance News
ANN_WORDS = re.compile(r"delist|removal|will remove|perpetual contract|margin tier|leverage|settle|migrat|suspend|contract (adjust|update|change)", re.I)
KIND_ZH = {"FOMC": "聯準會利率決議", "CPI": "美國 CPI", "NFP": "美國非農就業"}
FUT_LEV_SYMBOLS = ("BTCUSDC", "ETHUSDC", "BTCUSDT", "ETHUSDT")


def _iso(d: datetime) -> str:
    return d.astimezone(TZ).isoformat(timespec="seconds")


def _get(url, params=None, timeout=20, text=False, headers=None):
    import requests
    r = requests.get(url, params=params, timeout=timeout,
                     headers=headers or {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) crypto-trader-guard/1.0"})
    r.raise_for_status()
    return r.text if text else r.json()


def log_event(st: dict, action: str, slot: str | None = None, **extra) -> None:
    ev = {"at": _iso(datetime.now(timezone.utc)), "action": action, "slot": slot, "actor": "guard",
          "source": "cloud-guard", "ip": "", "user_agent": ""}
    ev.update(extra)
    st["control_log"] = (list(st.get("control_log") or []) + [ev])[-200:]


# ------------------------------------------------------------------ entry blocks
def active_blocks(st: dict, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for b in (st.get("guard_blocks") or {}).values():
        try:
            if datetime.fromisoformat(b["from"]) <= now <= datetime.fromisoformat(b["until"]):
                out.append(b)
        except Exception:  # noqa: BLE001
            continue
    return out


def entry_blocked(st: dict, now: datetime | None = None) -> tuple[bool, str]:
    """For the runner: True while any guard block (incl. a calendar window read straight from the file) is active."""
    now = now or datetime.now(timezone.utc)
    bl = active_blocks(st, now)
    if not bl:
        ev = calendar_window(now)
        if ev:
            return True, ev["reason_zh"]
    return (True, bl[0].get("reason_zh") or "風控暫停新開倉") if bl else (False, "")


def _set_block(st: dict, key: str, start: datetime, until: datetime, reason_zh: str, kind: str) -> bool:
    gb = dict(st.get("guard_blocks") or {})
    new = key not in gb
    gb[key] = {"key": key, "kind": kind, "from": _iso(start), "until": _iso(until), "reason_zh": reason_zh}
    st["guard_blocks"] = gb
    if new:
        log_event(st, "guard_block_entries", None, kind=kind, reason_zh=reason_zh, until=_iso(until))
    return new


def expire_blocks(st: dict, now: datetime) -> None:
    keep = {}
    for k, b in (st.get("guard_blocks") or {}).items():
        try:
            if datetime.fromisoformat(b["until"]) + timedelta(days=2) >= now:
                keep[k] = b
        except Exception:  # noqa: BLE001
            pass
    st["guard_blocks"] = keep


# ------------------------------------------------------------------ 1) macro calendar
def load_calendar() -> dict:
    try:
        return json.loads(CAL_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {"events": []}


def calendar_window(now: datetime, cal: dict | None = None) -> dict | None:
    for e in (cal or load_calendar()).get("events") or []:
        t = datetime.fromisoformat(e["at_utc"].replace("Z", "+00:00"))
        if t - PRE <= now <= t + POST:
            return {"kind": e["kind"], "at": t, "from": t - PRE, "until": t + POST,
                    "reason_zh": f"{KIND_ZH.get(e['kind'], e['kind'])}（{_iso(t)[5:16].replace('T', ' ')} 公布）前 1 小時至後 4 小時，暫停新開倉"}
    return None


def check_calendar(st: dict, now: datetime, cal: dict | None = None) -> dict:
    cal = cal or load_calendar()
    w = calendar_window(now, cal)
    if w:
        _set_block(st, f"cal|{w['kind']}|{w['at'].isoformat()}", w["from"], w["until"], w["reason_zh"], "macro")
    fut = sorted(datetime.fromisoformat(e["at_utc"].replace("Z", "+00:00")) for e in cal.get("events") or [])
    fut = [t for t in fut if t > now]
    nxt = [{"kind_zh": KIND_ZH.get(e["kind"], e["kind"]), "at": _iso(datetime.fromisoformat(e["at_utc"].replace("Z", "+00:00")))}
           for e in cal.get("events") or [] if datetime.fromisoformat(e["at_utc"].replace("Z", "+00:00")) > now][:4]
    stale = not fut or fut[-1] - now < timedelta(days=30)
    return {"active": bool(w), "next": nxt, "calendar_stale": stale,
            "alert_zh": "經濟日曆剩不到 30 天，需要補上新日程" if stale else None}


# ------------------------------------------------------------------ 2) Binance announcements
def tracked_coins(slots: list[dict]) -> dict[str, list[str]]:
    """{symbol: [slot ids]} for every known slot (spot and futures)."""
    out: dict[str, list[str]] = {}
    for s in slots or []:
        sym = str(s.get("symbol") or "").upper()
        if sym and s.get("id"):
            out.setdefault(sym, []).append(s["id"])
    out.setdefault("BTCUSDC", []).append("u1_btc_eth_usdc")
    out.setdefault("ETHUSDC", []).append("u1_btc_eth_usdc")
    return out


def match_article(text: str, sym: str) -> bool:
    from execution import base_asset
    b = base_asset(sym)
    if re.search(rf"\b{re.escape(sym)}\b", text):
        return True
    # spot "COIN/USDT"-style pair or a title "Delist COIN" mention (not as a quote asset "XYZ/COIN")
    if re.search(rf"(?<![/\w]){re.escape(b)}/(USDT|USDC)\b", text):
        return True
    if b not in ("BTC", "ETH") and re.search(rf"[Dd]elist[^.\n]*?(?<![/\w]){re.escape(b)}\b(?!/)", text):
        return True
    return False


def fetch_announcements(fetch=None) -> list[dict]:
    fetch = fetch or _get
    arts = []
    for cat in ANN_CATALOGS:
        d = fetch("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                  {"type": 1, "catalogId": cat, "pageNo": 1, "pageSize": 20})
        for c in ((d or {}).get("data") or {}).get("catalogs") or []:
            for a in c.get("articles") or []:
                arts.append({"id": a.get("id"), "code": a.get("code"), "title": a.get("title") or "",
                             "release_ms": int(a.get("releaseDate") or 0)})
    return arts


def article_body(code: str, fetch=None) -> str:
    fetch = fetch or _get
    try:
        d = fetch("https://www.binance.com/bapi/composite/v1/public/cms/article/detail/query", {"articleCode": code})
        body = ((d or {}).get("data") or {}).get("body") or ""
        return re.sub(r"<[^>]+>|\\\"|\{|\}|\"", " ", body)[:60000]
    except Exception:  # noqa: BLE001
        return ""


def check_announcements(st: dict, slots: list[dict], now: datetime, fetch=None, max_age_days: int = 14) -> dict:
    seen = set(st.get("guard_seen_articles") or [])
    coins = tracked_coins(slots)
    hits = []
    try:
        arts = fetch_announcements(fetch)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200], "hits": []}
    for a in arts:
        if a["id"] in seen or not ANN_WORDS.search(a["title"]):
            continue
        if a["release_ms"] and now.timestamp() * 1000 - a["release_ms"] > max_age_days * 86400e3:
            seen.add(a["id"])
            continue
        text = a["title"] + "\n" + article_body(a["code"], fetch)
        for sym, sids in coins.items():
            if match_article(text, sym):
                hits.append({"article_id": a["id"], "title": a["title"], "symbol": sym, "slots": sorted(set(sids)),
                             "url": f"https://www.binance.com/en/support/announcement/{a['code']}"})
        seen.add(a["id"])
    ps = dict(st.get("paused_slots") or {})
    for h in hits:
        for sid in h["slots"]:
            if sid not in ps:
                ps[sid] = {"paused_at": _iso(now), "paused_by": "guard", "reason": "binance_announcement",
                           "reason_zh": f"幣安公告涉及 {h['symbol']}：{h['title'][:80]}"}
                log_event(st, "pause_slot", sid, reason_zh=ps[sid]["reason_zh"], article_id=h["article_id"])
        st.setdefault("reconcile_alerts", []).append({"at": _iso(now), "key": f"announcement|{h['article_id']}|{h['symbol']}",
                                                      "kind": "binance_announcement", "symbol": h["symbol"], "slots": h["slots"],
                                                      "pause": True, "title": h["title"], "url": h["url"]})
    st["paused_slots"] = ps
    st["guard_seen_articles"] = sorted(seen)[-500:]
    return {"hits": hits, "checked": len(arts)}


# ------------------------------------------------------------------ 4) stablecoin depeg
def check_depeg(st: dict, now: datetime, price_fn, fc=None) -> dict:
    px = float(price_fn("USDCUSDT"))
    dev = px - 1.0
    out = {"usdc_usdt": px, "deviation_pct": dev * 100, "limit_pct": DEPEG_LIMIT * 100}
    g = dict(st.get("guard_depeg") or {})
    if abs(dev) > DEPEG_LIMIT:
        if not g.get("active"):
            log_event(st, "guard_depeg_leverage_1x", None, usdc_usdt=px,
                      reason_zh=f"USDC/USDT 偏離 {dev * 100:+.2f}%（超過 0.5%），永續槓桿設定改為 1 倍")
            st.setdefault("reconcile_alerts", []).append({"at": _iso(now), "key": f"depeg|{now.date()}", "kind": "stable_depeg",
                                                          "pause": False, "usdc_usdt": px})
        g.update(active=True, since=g.get("since") or _iso(now), last=_iso(now), usdc_usdt=px,
                 hold_until=_iso(now + timedelta(hours=24)))
        res = {}
        if fc is not None:
            for sym in FUT_LEV_SYMBOLS:
                try:
                    fc.set_leverage(sym, 1, cap=1)          # leverage setting only; never an order
                    res[sym] = "1x"
                except Exception as e:  # noqa: BLE001
                    res[sym] = str(e)[:120]
        g["leverage_set"] = res
    elif g.get("active") and datetime.fromisoformat(g["hold_until"]) < now:
        g = {"active": False, "ended": _iso(now)}
        log_event(st, "guard_depeg_cleared", None, usdc_usdt=px, reason_zh="穩定幣報價恢復正常")
    st["guard_depeg"] = g
    out["active"] = bool(g.get("active"))
    return out


def leverage_override(st: dict) -> int | None:
    return 1 if (st.get("guard_depeg") or {}).get("active") else None


# ------------------------------------------------------------------ 5) extreme 15m range
def check_spike(st: dict, now: datetime, kl_fn, symbols=("BTCUSDT", "ETHUSDT")) -> dict:
    import numpy as np
    out = {}
    for sym in symbols:
        k = kl_fn(sym) or []
        closed = [x for x in k if int(x[6]) < now.timestamp() * 1000]
        if len(closed) < 200:
            continue
        rng = np.array([(float(x[2]) - float(x[3])) / float(x[1]) for x in closed])
        hist, last = rng[:-1], rng[-1]
        mu, sd = float(hist.mean()), float(hist.std(ddof=1))
        z = (last - mu) / sd if sd > 0 else 0.0
        out[sym] = {"range_pct": last * 100, "z": z}
        if z > SPIKE_SIGMA:
            bar = datetime.fromtimestamp(int(closed[-1][0]) / 1000, tz=timezone.utc)
            _set_block(st, f"spike|{sym}|{bar.isoformat()}", now, now + SPIKE_BLOCK,
                       f"{sym[:-4]} 15 分鐘振幅 {last * 100:.2f}%（{z:.1f}σ），暫停新開倉 2 小時", "spike")
    return out


def _kl15(sym):
    from perp_market import PERP_MARKET_BASE
    return _get(f"{PERP_MARKET_BASE}/fapi/v1/klines", {"symbol": sym, "interval": "15m", "limit": 1000})


def _spot_price(sym):
    return float(_get("https://data-api.binance.vision/api/v3/ticker/price", {"symbol": sym})["price"])


# ------------------------------------------------------------------ 6) market indicators (display only)
def market_indicators(fetch=None) -> dict:
    fetch = fetch or _get
    from perp_market import PERP_MARKET_BASE as B
    out: dict = {"at": _iso(datetime.now(timezone.utc))}
    try:
        import numpy as np
        fr = fetch(f"{B}/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 1000}) or []
        r = np.array([float(x["fundingRate"]) for x in fr])
        r7 = np.convolve(r, np.ones(21) / 21, mode="valid")        # 7 days × 3 fundings
        pct_f = float((r7[:-1] < r7[-1]).mean() * 100) if len(r7) > 30 else None
        pi = fetch(f"{B}/fapi/v1/premiumIndex", {"symbol": "BTCUSDT"}) or {}
        basis = (float(pi["markPrice"]) / float(pi["indexPrice"]) - 1) * 100 if pi.get("indexPrice") else None
        light = None if pct_f is None else ("red" if pct_f >= 90 else "yellow" if pct_f >= 75 else "green")
        out["crowding"] = {"funding_7d_avg_pct": float(r7[-1]) * 100, "funding_percentile": pct_f, "basis_pct": basis, "light": light,
                           "light_zh": {"red": "擁擠（偏多過熱）", "yellow": "偏熱", "green": "正常"}.get(light, "資料不足")}
    except Exception as e:  # noqa: BLE001
        out["crowding"] = {"error": str(e)[:150]}
    try:
        oi = fetch(f"{B}/futures/data/openInterestHist", {"symbol": "BTCUSDT", "period": "1d", "limit": 30}) or []
        v = [float(x["sumOpenInterestValue"]) for x in oi]
        out["open_interest"] = {"btc_usd": v[-1], "chg_7d_pct": (v[-1] / v[-8] - 1) * 100 if len(v) >= 8 else None,
                                "chg_30d_pct": (v[-1] / v[0] - 1) * 100 if len(v) >= 2 else None}
    except Exception as e:  # noqa: BLE001
        out["open_interest"] = {"error": str(e)[:150]}
    try:
        d = fetch("https://api.alternative.me/fng/", {"limit": 8}) or {}
        rows = d.get("data") or []
        zh = {"Extreme Fear": "極度恐懼", "Fear": "恐懼", "Neutral": "中性", "Greed": "貪婪", "Extreme Greed": "極度貪婪"}
        out["fear_greed"] = {"value": int(rows[0]["value"]), "label_zh": zh.get(rows[0]["value_classification"], ""),
                             "week_ago": int(rows[-1]["value"]) if len(rows) > 1 else None}
    except Exception as e:  # noqa: BLE001
        out["fear_greed"] = {"error": str(e)[:150]}
    try:
        out["etf_flows"] = farside_flows(fetch)
    except Exception as e:  # noqa: BLE001
        out["etf_flows"] = {"error": str(e)[:150]}
    return out


def farside_flows(fetch=None) -> dict:
    fetch = fetch or _get
    h = fetch("https://farside.co.uk/bitcoin-etf-flow-all-data/", None, text=True,
              headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}) \
        if fetch is _get else fetch("farside")
    days = []
    for r in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
        if cells and re.match(r"\d{1,2} \w{3} \d{4}$", cells[0]):
            t = cells[-1].replace(",", "")
            if t in ("-", ""):
                continue
            v = -float(t.strip("()")) if t.startswith("(") else float(t)
            days.append([datetime.strptime(cells[0], "%d %b %Y").strftime("%Y-%m-%d"), v])
    last = days[-10:]
    return {"days": last, "unit": "百萬美元", "sum_5d": sum(v for _, v in last[-5:])}


# ------------------------------------------------------------------ batch (reconcile job, every 15 min)
def run_all(st: dict, *, slots: list[dict], fc=None, now: datetime | None = None, fetchers: dict | None = None,
            with_market: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    f = fetchers or {}
    rep: dict = {"at": _iso(now)}
    expire_blocks(st, now)
    for name, fn in (("calendar", lambda: check_calendar(st, now, f.get("calendar"))),
                     ("announcements", lambda: check_announcements(st, slots, now, f.get("http"))),
                     ("depeg", lambda: check_depeg(st, now, f.get("price") or _spot_price, fc)),
                     ("spike", lambda: check_spike(st, now, f.get("kl15") or _kl15))):
        try:
            rep[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("guard_%s_fail err=%s", name, e)
            rep[name] = {"error": str(e)[:200]}
    if with_market:
        prev = st.get("market_indicators") or {}
        if not prev.get("at") or datetime.fromisoformat(prev["at"]) < now - timedelta(hours=1):
            st["market_indicators"] = market_indicators(f.get("http"))
    rep["blocks_active"] = active_blocks(st, now)
    st["guards"] = rep
    return rep
