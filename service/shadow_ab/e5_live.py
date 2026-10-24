"""Fresh daily data for demo_e5 live targets. The parity panel (e5_data) is frozen; the live decision needs every
completed UTC day up to yesterday. extend() copies the frozen SPEC_DATA_DIR into dst and appends, for the SAME symbol
set and column order, completed 1d futures klines (fapi, fallback data.binance.vision daily), funding settlements
(fapi) and BTC/ETH spot 1d klines (api.binance.com, fallback vision daily). The ledger (ledger_e_minnot.py, unchanged)
then runs on dst exactly as in the backtest; its last bar is the decided bar, executed at the next 08:00 (Taipei) open."""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("e5_live")
FAPI = "https://fapi.binance.com"
SPOT = "https://api.binance.com"
VISION = "https://data.binance.vision/data"
DAY = 86400000


def _get(url: str, timeout: int = 20):
    return json.loads(urllib.request.urlopen(url, timeout=timeout).read())


def _vision_day(kind: str, sym: str, day: str):
    base = f"{VISION}/futures/um/daily/klines" if kind == "fut" else f"{VISION}/spot/daily/klines"
    try:
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(f"{base}/{sym}/1d/{sym}-1d-{day}.zip", timeout=20).read()))
        rows = [l.split(",") for l in z.read(z.namelist()[0]).decode().splitlines() if l and l[0].isdigit()]
        return [[int(r[0]) // (1000 if int(r[0]) > 1e14 else 1)] + [float(x) for x in r[1:6]] + [int(r[6]), float(r[7])] for r in rows]
    except Exception:  # noqa: BLE001
        return []


def klines(kind: str, sym: str, start_ms: int, end_ms: int) -> tuple[list, str]:
    """Completed 1d bars with open_time in [start_ms, end_ms). Returns ([open_time, o, h, l, c, vol, close_time, qv], src)."""
    url = (f"{FAPI}/fapi/v1/klines" if kind == "fut" else f"{SPOT}/api/v3/klines") + f"?symbol={sym}&interval=1d&startTime={start_ms}&limit=100"
    try:
        r = _get(url)
        return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), int(x[6]), float(x[7])]
                for x in r if start_ms <= int(x[0]) < end_ms], "api"
    except urllib.error.HTTPError as e:
        if e.code == 400:  # unknown / delisted symbol
            return [], "api"
    except Exception:  # noqa: BLE001
        pass
    out = []
    t = start_ms
    while t < end_ms:
        out += _vision_day(kind, sym, datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d")); t += DAY
    return out, "vision"


def funding(sym: str, start_ms: int, end_ms: int) -> list:
    try:
        r = _get(f"{FAPI}/fapi/v1/fundingRate?symbol={sym}&startTime={start_ms}&endTime={end_ms}&limit=1000")
        return [(int(x["fundingTime"]), float(x["fundingRate"])) for x in r]
    except Exception:  # noqa: BLE001
        return []


def tradfi_onboard() -> dict[str, int]:
    """{symbol: TradFi onboardDate ms}: live fapi exchangeInfo (contractType TRADIFI_PERPETUAL / equity, commodity, FX
    underlying) + the committed snapshot service/tradfi_symbols.json."""
    out: dict[str, int] = {}
    try:
        out.update({k: int(v) for k, v in json.loads((Path(__file__).resolve().parent.parent / "tradfi_symbols.json").read_text()).items()})
    except Exception:  # noqa: BLE001
        pass
    try:
        for s in _get(f"{FAPI}/fapi/v1/exchangeInfo").get("symbols") or []:
            if str(s.get("contractType") or "").upper().startswith("TRADIFI") or str(s.get("underlyingType") or "") in (
                    "EQUITY", "HK_EQUITY", "KR_EQUITY", "CN_EQUITY", "COMMODITY", "FX") or "TradFi" in (s.get("underlyingSubType") or []):
                out[s["symbol"]] = int(s.get("onboardDate") or out.get(s["symbol"]) or 0)
    except Exception as e:  # noqa: BLE001
        log.warning("e5_live tradfi exchangeInfo fail %s (snapshot only)", e)
    return out


def mask_tradfi(p, onboard: dict[str, int]):
    """crypto perps only (Robin 2026-10-24): a TradFi symbol is NaN from its TradFi onboardDate on (re-used tickers
    keep their earlier crypto history), so the ledger never selects or prices it after that."""
    import pandas as pd
    syms = set(p.columns.get_level_values(0)); hit = []
    for s, ms in onboard.items():
        if s in syms:
            t = pd.Timestamp(int(ms), unit="ms", tz="UTC").normalize() if ms else p.index[0]
            m = p.index >= t
            if m.any():
                p.loc[m, s] = float("nan"); hit.append(s)
    return p, sorted(hit)


def extend(base: Path, dst: Path, now: datetime | None = None, workers: int = 8) -> dict:
    import pandas as pd
    now = now or datetime.now(timezone.utc)
    end = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)  # today's 00:00 UTC: only completed bars before it
    end_ms = int(end.timestamp() * 1000)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    p = pd.read_pickle(base / "panel_1d.pkl")
    syms = list(dict.fromkeys(p.columns.get_level_values(0)))
    last = p.index[-1]
    start_ms = int((last + pd.Timedelta(days=1)).timestamp() * 1000)
    (dst / "fr").mkdir(); (dst / "sp").mkdir()
    for k in ("fr", "sp"):
        for f in (base / k).glob("*.csv") if (base / k).exists() else []:
            os.symlink(f.resolve(), dst / k / f.name)
    src = {"api": 0, "vision": 0}
    if start_ms >= end_ms:
        full, masked = mask_tradfi(p, tradfi_onboard())
        full.to_pickle(dst / "panel_1d.pkl")
        return {"base_last": str(last.date()), "last_bar": str(last.date()), "added_days": 0, "syms": len(syms), "src": src,
                "tradfi_masked": len(masked)}

    def one(s):
        kl, sr = klines("fut", s, start_ms, end_ms)
        fr = funding(s, start_ms - DAY, end_ms + DAY)
        return s, kl, sr, fr
    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, syms))
    new = {}
    for s, kl, sr, fr in res:
        src[sr] += 1
        if kl:
            idx = pd.to_datetime([r[0] for r in kl], unit="ms", utc=True).normalize()
            new[s] = pd.DataFrame({"open": [r[1] for r in kl], "high": [r[2] for r in kl], "low": [r[3] for r in kl],
                                   "close": [r[4] for r in kl], "quote_volume": [r[7] for r in kl]}, index=idx)
        if fr:
            pd.DataFrame({"calc_time": [t for t, _ in fr], "funding_interval_hours": 8, "last_funding_rate": [v for _, v in fr]}).to_csv(
                dst / "fr" / f"{s}_live.csv", index=False)
    days = pd.date_range(last + pd.Timedelta(days=1), end - timedelta(days=1), freq="D", tz="UTC") if start_ms < end_ms else []
    add = pd.DataFrame(index=pd.DatetimeIndex(days, name=p.index.name), columns=p.columns, dtype=float)
    for s, df in new.items():
        for f in ("open", "high", "low", "close", "quote_volume"):
            if (s, f) in add.columns:
                add.loc[df.index.intersection(add.index), (s, f)] = df[f].reindex(add.index).dropna()
    # a day with no bar for any symbol (data not yet published) must not become the decided bar
    have = add.notna().any(axis=1)
    add = add[have.cumprod().astype(bool)] if len(add) else add
    full, masked = mask_tradfi(pd.concat([p, add]), tradfi_onboard())
    full.to_pickle(dst / "panel_1d.pkl")
    for s in [x for x in ("BTCUSDT", "ETHUSDT") if list((base / "sp").glob(f"{x}_*.csv")) if (base / "sp").exists()]:
        kl, _ = klines("spot", s, start_ms, end_ms)
        if kl:
            with open(dst / "sp" / f"{s}_live.csv", "w") as fh:
                for r in kl:
                    fh.write(",".join(str(x) for x in r[:8]) + ",0,0,0,0\n")
    lb = str((p.index[-1] if not len(add) else add.index[-1]).date())
    out = {"base_last": str(last.date()), "last_bar": lb, "added_days": int(len(add)), "syms": len(syms),
           "with_new_bars": len(new), "src": src, "tradfi_masked": len(masked)}
    log.info("e5_live %s", out)
    return out
