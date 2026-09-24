"""GDELT timeline + Binance CMS → GCS/local daily_agg, announcements, signals.

Folded into the hourly Cloud Run job when stale (no extra scheduler).
News families fail-closed when news_pipeline_ok() is False.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from news_lib import (
    GDELT_QUERY,
    NEG_WORDS,
    gdelt_z,
    parse_gdelt_timeline,
    shift_daily_to_next_bar,
    z_series_from_vol_tone,
)

log = logging.getLogger("trader.news_ingest")

SEED_DIR = Path(__file__).resolve().parent / "news_seed"
GCS_PREFIX = os.environ.get("NEWS_GCS_PREFIX") or "trader/news"
DEFAULT_SYMBOLS = ("BTC", "ETH", "SOL", "OP", "DOT", "FET", "FIL")
# Live GDELT fetch set (keep tiny for Cloud Run hourly budget); others use seed/BTC proxy
LIVE_SYMBOLS = ("BTC", "FIL", "OP", "DOT")
LIVE_MODES = ("timelinevol",)  # catalog news rows use field=vol
REFRESH_HOURS = float(os.environ.get("NEWS_REFRESH_HOURS") or "6")
STALE_HOURS = float(os.environ.get("NEWS_STALE_HOURS") or "36")
GDELT_SLEEP_S = float(os.environ.get("NEWS_GDELT_SLEEP_S") or "4")
GDELT_TIMESPAN = os.environ.get("NEWS_GDELT_TIMESPAN") or "3m"
CMS_CATALOGS = (48, 161)
USER_AGENT = "crypto-trader-news-ingest/1.0"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get_json(url: str, timeout: int = 45) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001
        log.warning("news_http_fail url=%s err=%s", url[:140], e)
        return None


def fetch_gdelt_timeline(query: str, mode: str, timespan: str = GDELT_TIMESPAN) -> dict | None:
    q = urllib.parse.quote(query)
    url = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={q}&mode={mode}&format=json&timespan={timespan}"
    )
    raw = _http_get_json(url)
    if not isinstance(raw, dict) or "timeline" not in raw:
        return None
    return raw


def fetch_binance_cms(catalog_id: int, page_size: int = 20, page_no: int = 1) -> list[dict]:
    url = (
        "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
        f"?type=1&pageNo={page_no}&pageSize={page_size}&catalogId={catalog_id}"
    )
    raw = _http_get_json(url)
    if not isinstance(raw, dict):
        return []
    data = raw.get("data") or {}
    catalogs = data.get("catalogs") or []
    articles: list = catalogs[0].get("articles") if catalogs else (data.get("articles") or [])
    out: list[dict] = []
    for a in articles or []:
        title = a.get("title") or ""
        kind = "listing" if catalog_id == 48 else "other"
        if "delist" in title.lower():
            kind = "delist"
        tickers = [
            m
            for m in re.findall(r"\b([A-Z]{2,10})\b", title)
            if m not in ("THE", "AND", "FOR", "WILL", "FROM", "WITH", "USD", "USDT", "BINANCE", "NEW")
        ]
        out.append(
            {
                "id": a.get("id") or a.get("code"),
                "code": a.get("code"),
                "title": title,
                "releaseDate_raw": a.get("releaseDate"),
                "catalog": catalog_id,
                "kind": kind,
                "tickers": list(dict.fromkeys(tickers))[:8],
                "neg_kw": bool(NEG_WORDS.search(title)),
            }
        )
    return out


class NewsStore:
    def __init__(self, bucket: str | None = None, local_dir: str | Path | None = None):
        self.bucket = bucket if bucket is not None else (os.environ.get("GCS_BUCKET") or "")
        self.local_dir = Path(local_dir or os.environ.get("NEWS_LOCAL_DIR") or "/tmp/trader_news")
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self._client = None

    def _gcs(self):
        if self._client is None:
            from google.cloud import storage

            self._client = storage.Client()
        return self._client

    def _local_path(self, object_name: str) -> Path:
        return self.local_dir / object_name.replace("/", "__")

    def load_json(self, object_name: str) -> dict | list | None:
        if self.bucket:
            try:
                blob = self._gcs().bucket(self.bucket).blob(object_name)
                if blob.exists():
                    return json.loads(blob.download_as_text())
            except Exception as e:  # noqa: BLE001
                log.warning("news_gcs_load_fail obj=%s err=%s", object_name, e)
        lp = self._local_path(object_name)
        if lp.is_file():
            return json.loads(lp.read_text())
        return None

    def save_json(self, object_name: str, data: Any) -> None:
        payload = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
        lp = self._local_path(object_name)
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(payload)
        if not self.bucket:
            return
        try:
            blob = self._gcs().bucket(self.bucket).blob(object_name)
            blob.upload_from_string(payload, content_type="application/json")
        except Exception as e:  # noqa: BLE001
            log.error("news_gcs_save_fail obj=%s err=%s", object_name, e)
            raise


def _points_to_series(points: list[dict]) -> pd.Series:
    rows = []
    for p in points or []:
        d = p.get("date")
        if not d:
            continue
        ts = pd.Timestamp(str(d), tz="UTC").normalize()
        rows.append((ts, float(p.get("value", 0))))
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series(dict(rows)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def merge_series(base: pd.Series, new: pd.Series) -> pd.Series:
    if base is None or base.empty:
        return new.sort_index() if new is not None and len(new) else pd.Series(dtype=float)
    if new is None or new.empty:
        return base
    s = pd.concat([base, new]).sort_index()
    return s[~s.index.duplicated(keep="last")]


def load_seed_timelines() -> dict[str, dict[str, pd.Series]]:
    out: dict[str, dict[str, pd.Series]] = {}
    if not SEED_DIR.is_dir():
        return out
    for p in SEED_DIR.glob("gdelt_*_timeline*.json"):
        parts = p.stem.split("_")
        if len(parts) < 3:
            continue
        sym, mode = parts[1].upper(), parts[2]
        s = parse_gdelt_timeline(p)
        if s.empty:
            continue
        out.setdefault(sym, {})[mode] = s
    return out


def build_daily_agg_doc(
    symbol: str, vol: pd.Series, tone: pd.Series, *, proxy: dict | None = None
) -> dict:
    z_vol = gdelt_z(vol) if len(vol) else pd.Series(dtype=float)
    z_tone = gdelt_z(tone) if len(tone) else pd.Series(dtype=float)
    dates = sorted(set(vol.index.tolist()) | set(tone.index.tolist()))
    rows = []
    for ts in dates:
        rows.append(
            {
                "date": pd.Timestamp(ts).strftime("%Y-%m-%d"),
                "vol": None if ts not in vol.index or pd.isna(vol.loc[ts]) else float(vol.loc[ts]),
                "tone": None if ts not in tone.index or pd.isna(tone.loc[ts]) else float(tone.loc[ts]),
                "z_vol": None if ts not in z_vol.index or pd.isna(z_vol.loc[ts]) else float(z_vol.loc[ts]),
                "z_tone": None if ts not in z_tone.index or pd.isna(z_tone.loc[ts]) else float(z_tone.loc[ts]),
            }
        )
    return {
        "symbol": symbol,
        "updated_at": _iso(),
        "last_date": rows[-1]["date"] if rows else None,
        "n_days": len(rows),
        "proxy": proxy or {},
        "days": rows,
    }


def build_signal_doc(symbol: str, agg: dict, announcements: list[dict]) -> dict:
    days = agg.get("days") or []
    last = days[-1] if days else {}
    z_vol = last.get("z_vol")
    z_tone = last.get("z_tone")
    neg = False
    names = {"OP": "Optimism", "DOT": "Polkadot", "FET": "Fetch", "FIL": "Filecoin"}
    for a in (announcements or [])[-120:]:
        title = a.get("title") or ""
        tickers = a.get("tickers") or []
        is_neg = bool(a.get("neg_kw")) or bool(NEG_WORDS.search(title)) or (a.get("kind") == "delist")
        if not is_neg:
            continue
        sym_hit = symbol in tickers or bool(re.search(rf"\b{re.escape(symbol)}\b", title, re.I))
        if names.get(symbol) and names[symbol].lower() in title.lower():
            sym_hit = True
        severe = bool(re.search(r"hack|exploit|\bsec\b|lawsuit|ban\b", title, re.I))
        if sym_hit or severe:
            neg = True
            break
    return {
        "symbol": symbol,
        "news_z_vol": z_vol,
        "news_z_tone": z_tone,
        "burst": bool(z_vol is not None and z_vol > 1.5),
        "neg_block": neg,
        "sources": ["gdelt", "binance_cms"],
        "generated_at": _iso(),
        "last_date": agg.get("last_date"),
        "n_days": agg.get("n_days"),
        "proxy": agg.get("proxy") or {},
    }


def _meta_path() -> str:
    return f"{GCS_PREFIX}/meta.json"


def load_meta(store: NewsStore | None = None) -> dict:
    store = store or NewsStore()
    m = store.load_json(_meta_path())
    return m if isinstance(m, dict) else {}


def news_fresh(meta: dict | None = None, store: NewsStore | None = None) -> bool:
    store = store or NewsStore()
    meta = meta if meta is not None else load_meta(store)
    if not meta.get("ok"):
        return False
    ts = meta.get("last_ok_at") or meta.get("updated_at")
    if not ts:
        return False
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        age_h = (_now_utc() - t.to_pydatetime()).total_seconds() / 3600.0
    except Exception:
        return False
    if age_h > STALE_HOURS:
        return False
    return int(meta.get("n_symbols_ok") or 0) >= 1


def _load_agg_series(store: NewsStore, symbol: str) -> tuple[pd.Series, pd.Series, dict]:
    doc = store.load_json(f"{GCS_PREFIX}/daily_agg/{symbol}.json")
    if not isinstance(doc, dict):
        return pd.Series(dtype=float), pd.Series(dtype=float), {}
    days = doc.get("days") or []
    vol_pts = [{"date": d["date"], "value": d["vol"]} for d in days if d.get("vol") is not None]
    tone_pts = [{"date": d["date"], "value": d["tone"]} for d in days if d.get("tone") is not None]
    return _points_to_series(vol_pts), _points_to_series(tone_pts), doc.get("proxy") or {}


def ensure_seeded(store: NewsStore | None = None) -> dict:
    store = store or NewsStore()
    meta = load_meta(store)
    existing = store.load_json(f"{GCS_PREFIX}/daily_agg/BTC.json")
    if isinstance(existing, dict) and int(existing.get("n_days") or 0) >= 30:
        n_ok = 0
        for sym in DEFAULT_SYMBOLS:
            d = store.load_json(f"{GCS_PREFIX}/daily_agg/{sym}.json")
            if isinstance(d, dict) and int(d.get("n_days") or 0) >= 30:
                n_ok += 1
        if meta.get("ok") and news_fresh(meta, store):
            return meta
        meta = {
            "ok": n_ok > 0,
            "seeded": True,
            "n_symbols_ok": n_ok,
            "last_ok_at": meta.get("last_ok_at") or _iso(),
            "updated_at": _iso(),
            "sources": meta.get("sources") or {"gdelt": "cache", "binance_cms": "cache"},
        }
        store.save_json(_meta_path(), meta)
        return meta

    seeds = load_seed_timelines()
    if not seeds:
        log.warning("news_seed_empty dir=%s", SEED_DIR)
        meta = {"ok": False, "seeded": False, "updated_at": _iso(), "error": "no_seed"}
        store.save_json(_meta_path(), meta)
        return meta

    btc_vol = seeds.get("BTC", {}).get("timelinevol", pd.Series(dtype=float))
    symbols_ok = 0
    for sym in DEFAULT_SYMBOLS:
        vol = seeds.get(sym, {}).get("timelinevol", pd.Series(dtype=float))
        tone = seeds.get(sym, {}).get("timelinetone", pd.Series(dtype=float))
        proxy: dict[str, str] = {}
        if vol.empty and not btc_vol.empty:
            vol = btc_vol
            proxy["timelinevol"] = "BTC"
        if vol.empty and tone.empty:
            continue
        doc = build_daily_agg_doc(sym, vol, tone, proxy=proxy)
        store.save_json(f"{GCS_PREFIX}/daily_agg/{sym}.json", doc)
        store.save_json(f"{GCS_PREFIX}/signals/{sym}.json", build_signal_doc(sym, doc, []))
        symbols_ok += 1
    meta = {
        "ok": symbols_ok > 0,
        "seeded": True,
        "n_symbols_ok": symbols_ok,
        "last_ok_at": _iso(),
        "updated_at": _iso(),
        "sources": {"gdelt": "seed", "binance_cms": "none"},
        "note": "seeded from image news_seed; live refresh merges when available",
    }
    store.save_json(_meta_path(), meta)
    log.info("news_seeded n=%s", symbols_ok)
    return meta


def refresh_news(*, force: bool = False, symbols: tuple[str, ...] | None = None) -> dict:
    store = NewsStore()
    meta = ensure_seeded(store)
    if not force and meta.get("last_fetch_at"):
        try:
            t = pd.Timestamp(meta["last_fetch_at"])
            if t.tzinfo is None:
                t = t.tz_localize("UTC")
            age_h = (_now_utc() - t.to_pydatetime()).total_seconds() / 3600.0
            if age_h < REFRESH_HOURS and news_fresh(meta, store):
                log.info("news_refresh_skip age_h=%.2f", age_h)
                return meta
        except Exception:
            pass

    syms = symbols or LIVE_SYMBOLS
    gdelt_ok = 0
    gdelt_fail = 0
    updated_syms: list[str] = []

    for sym in syms:
        query = GDELT_QUERY.get(sym)
        if not query:
            continue
        base_vol, base_tone, proxy = _load_agg_series(store, sym)
        new_vol = pd.Series(dtype=float)
        new_tone = pd.Series(dtype=float)
        for mode in LIVE_MODES:
            time.sleep(GDELT_SLEEP_S)
            raw = fetch_gdelt_timeline(query, mode)
            if raw is None:
                gdelt_fail += 1
                continue
            s = parse_gdelt_timeline(raw)
            if s.empty:
                gdelt_fail += 1
                continue
            if mode == "timelinevol":
                new_vol = s
            else:
                new_tone = s
            try:
                store.save_json(
                    f"{GCS_PREFIX}/raw/gdelt_{sym}_{mode}_{_now_utc().strftime('%Y%m%d')}.json",
                    {"query": query, "mode": mode, "fetched_at": _iso(), "timeline": raw.get("timeline")},
                )
            except Exception as e:  # noqa: BLE001
                log.warning("news_raw_save_fail err=%s", e)
            gdelt_ok += 1
        vol = merge_series(base_vol, new_vol)
        tone = merge_series(base_tone, new_tone)
        if vol.empty and not base_vol.empty:
            vol = base_vol
        if vol.empty:
            btc_v, _, _ = _load_agg_series(store, "BTC")
            if not btc_v.empty:
                vol = btc_v
                proxy = {**proxy, "timelinevol": "BTC"}
        if vol.empty and tone.empty:
            continue
        doc = build_daily_agg_doc(sym, vol, tone, proxy=proxy)
        store.save_json(f"{GCS_PREFIX}/daily_agg/{sym}.json", doc)
        updated_syms.append(sym)

    announcements: list[dict] = []
    prev = store.load_json(f"{GCS_PREFIX}/announcements.json")
    if isinstance(prev, dict):
        announcements = list(prev.get("articles") or [])
    elif isinstance(prev, list):
        announcements = list(prev)
    cms_ok = False
    for cid in CMS_CATALOGS:
        arts = fetch_binance_cms(cid)
        if arts:
            cms_ok = True
            announcements.extend(arts)
    seen: set = set()
    deduped: list[dict] = []
    for a in announcements:
        key = (a.get("id") or a.get("code") or a.get("title"), a.get("catalog"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    announcements = deduped[-5000:]
    store.save_json(
        f"{GCS_PREFIX}/announcements.json",
        {"updated_at": _iso(), "articles": announcements, "cms_ok": cms_ok},
    )

    for sym in sorted(set(updated_syms) | set(syms)):
        agg = store.load_json(f"{GCS_PREFIX}/daily_agg/{sym}.json")
        if isinstance(agg, dict) and agg.get("n_days"):
            store.save_json(
                f"{GCS_PREFIX}/signals/{sym}.json",
                build_signal_doc(sym, agg, announcements),
            )

    n_ok = 0
    for sym in DEFAULT_SYMBOLS:
        agg = store.load_json(f"{GCS_PREFIX}/daily_agg/{sym}.json")
        if isinstance(agg, dict) and int(agg.get("n_days") or 0) >= 30:
            n_ok += 1

    prior_ok = news_fresh(meta, store)
    last_ok_at = meta.get("last_ok_at") if prior_ok else None
    if n_ok > 0 and (gdelt_ok > 0 or prior_ok or meta.get("seeded")):
        last_ok_at = _iso()
    new_meta = {
        "ok": bool(last_ok_at and n_ok > 0),
        "seeded": True,
        "n_symbols_ok": n_ok,
        "gdelt_ok": gdelt_ok,
        "gdelt_fail": gdelt_fail,
        "cms_ok": cms_ok,
        "updated_syms": updated_syms,
        "last_fetch_at": _iso(),
        "last_ok_at": last_ok_at,
        "updated_at": _iso(),
        "sources": {
            "gdelt": "live" if gdelt_ok else ("cache" if prior_ok else "fail"),
            "binance_cms": "live" if cms_ok else "cache",
        },
        "refresh_hours": REFRESH_HOURS,
        "stale_hours": STALE_HOURS,
    }
    if new_meta["ok"] and not news_fresh(new_meta, store):
        new_meta["ok"] = False
    store.save_json(_meta_path(), new_meta)
    log.info(
        "news_refresh_done ok=%s gdelt_ok=%s fail=%s cms=%s n_sym=%s",
        new_meta["ok"],
        gdelt_ok,
        gdelt_fail,
        cms_ok,
        n_ok,
    )
    return new_meta


def maybe_refresh_news() -> dict:
    try:
        return refresh_news(force=False)
    except Exception as e:  # noqa: BLE001
        log.exception("news_refresh_crash err=%s", e)
        store = NewsStore()
        meta = load_meta(store)
        meta["ok"] = False
        meta["last_error"] = str(e)[:400]
        meta["updated_at"] = _iso()
        try:
            store.save_json(_meta_path(), meta)
        except Exception:  # noqa: BLE001
            pass
        return meta


def load_vol_tone(symbol: str, store: NewsStore | None = None) -> tuple[pd.Series, pd.Series, dict]:
    store = store or NewsStore()
    ensure_seeded(store)
    sym = symbol.upper().replace("USDT", "")
    vol, tone, proxy = _load_agg_series(store, sym)
    if vol.empty:
        btc_v, btc_t, _ = _load_agg_series(store, "BTC")
        if not btc_v.empty:
            vol = btc_v
            proxy = {**proxy, "timelinevol": "BTC"}
        if tone.empty and not btc_t.empty:
            tone = btc_t
            proxy = {**proxy, "timelinetone": "BTC"}
    return vol, tone, proxy


def aligned_news_z(
    symbol: str, field: str, index: pd.DatetimeIndex, store: NewsStore | None = None
) -> pd.Series:
    vol, tone, _ = load_vol_tone(symbol, store)
    z = z_series_from_vol_tone(vol, tone, field)
    return shift_daily_to_next_bar(z, index)


def news_pipeline_ok(store: NewsStore | None = None) -> bool:
    store = store or NewsStore()
    ensure_seeded(store)
    return news_fresh(store=store)
