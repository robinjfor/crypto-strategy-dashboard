"""Multi-year public data for ledger_e_min5 (E5 parity) in the cloud: USDT-M 1d klines + funding (2022-06..2026-09)
and BTC/ETH spot 1d klines, fetched from data.binance.vision exactly like the analyst's data/dl.py / dl_spot.py,
verified file-by-file against the analyst's local copy (sha256 manifest), panel_1d.pkl rebuilt with data/build.py
logic and the analyst's column order. Cached in GCS as trader/shadow_ab/e5_data.tgz (fixed range -> fetched once)."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tarfile
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("e5_data")
HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "e5_data_manifest.json.gz"
BLOB = "trader/shadow_ab/e5_data.tgz"
VISION = "https://data.binance.vision/data"


def manifest() -> dict:
    return json.loads(gzip.open(MANIFEST, "rt").read())


def _url(kind: str, fn: str) -> str:
    s, mo = fn[:-4].rsplit("_", 1)
    if kind == "kl":
        return f"{VISION}/futures/um/monthly/klines/{s}/1d/{s}-1d-{mo}.zip"
    if kind == "fr":
        return f"{VISION}/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{mo}.zip"
    return f"{VISION}/spot/monthly/klines/{s}/1d/{s}-1d-{mo}.zip"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def verify(d: Path, m: dict | None = None) -> dict:
    m = m or manifest(); missing = bad = 0
    for k in ("kl", "fr", "sp"):
        for fn, h in m[k].items():
            p = d / k / fn
            if not p.exists():
                missing += 1
            elif _sha(p) != h:
                bad += 1
    return {"files": sum(len(m[k]) for k in ("kl", "fr", "sp")), "missing": missing, "hash_mismatch": bad,
            "panel": (d / "panel_1d.pkl").exists()}


def fetch(d: Path, m: dict, workers: int = 48) -> None:
    for k in ("kl", "fr", "sp"):
        (d / k).mkdir(parents=True, exist_ok=True)
    jobs = [(k, fn) for k in ("kl", "fr", "sp") for fn in m[k] if not (d / k / fn).exists()]

    def g(a):
        k, fn = a
        for _ in range(4):
            try:
                b = urllib.request.urlopen(_url(k, fn), timeout=60).read(); z = zipfile.ZipFile(io.BytesIO(b))
                (d / k / fn).write_bytes(z.read(z.namelist()[0])); return
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return
            except Exception:  # noqa: BLE001
                pass
    log.info("e5_data fetching %d files", len(jobs))
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(g, jobs))


def build_panel(d: Path, m: dict) -> None:
    import collections
    import pandas as pd
    cols = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbq,ignore".split(",")
    acc = collections.defaultdict(list)
    for f in sorted((d / "kl").glob("*.csv")):
        df = pd.read_csv(f, header=None, names=cols)
        acc[f.name.rsplit("_", 1)[0]].append(df[pd.to_numeric(df.open_time, errors="coerce").notna()])
    out = {}
    for s, l in acc.items():
        df = pd.concat(l); df["ts"] = pd.to_datetime(df.open_time.astype("int64"), unit="ms", utc=True).dt.normalize()
        df = df.drop_duplicates("ts").set_index("ts").sort_index()
        out[s] = df[["open", "high", "low", "close", "quote_volume"]].astype(float)
    order = [s for s in m["panel_cols"] if s in out] + [s for s in out if s not in m["panel_cols"]]
    pd.concat({s: out[s] for s in order}, axis=1).to_pickle(d / "panel_1d.pkl")


def ensure(d: Path, bucket=None) -> dict:
    """Make d a complete SPEC_DATA_DIR for ledger_e_min5. Returns verify() summary (+source)."""
    m = manifest(); d.mkdir(parents=True, exist_ok=True); src = "local"
    if bucket is not None and not (d / "panel_1d.pkl").exists():
        b = bucket.blob(BLOB)
        if b.exists():
            with tarfile.open(fileobj=io.BytesIO(b.download_as_bytes()), mode="r:gz") as t:
                t.extractall(d)
            src = "gcs"
    v = verify(d, m)
    if v["missing"] or not v["panel"]:
        fetch(d, m); build_panel(d, m); v = verify(d, m); src = "vision"
        if bucket is not None and not v["missing"]:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as t:
                for k in ("kl", "fr", "sp", "panel_1d.pkl"):
                    t.add(d / k, arcname=k)
            bucket.blob(BLOB).upload_from_string(buf.getvalue(), content_type="application/gzip")
    v["source"] = src
    log.info("e5_data %s", v)
    return v
