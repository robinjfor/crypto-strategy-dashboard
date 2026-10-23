"""Cloud Run job: analyst shadow ledgers A–E (research_1022/shadow_AB/run_all.py), paper only, no orders.
The analyst's scripts are vendored byte-identical under shadow_ab/spec (+ engine.py for COST) and run unchanged:
they hardcode /workspace/strategy-unified-3y/... paths, so those paths are symlinked to a writable copy.
The public-data cache (daily klines + funding for all USDT-M perps) persists in GCS as one tarball, so only the
first run does the full ~1h fetch. Outputs: gs://$GCS_BUCKET/trader/shadow_ab/{ledger_*.csv,run.log,latest.json}."""
from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("shadow_ab")
HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("SHADOW_AB_WORK", "/tmp/shadow_ab"))
ROOT = Path("/workspace/strategy-unified-3y")
PREFIX = "trader/shadow_ab/"
TZ = timezone(timedelta(hours=8))
LEDGERS = ("A", "B", "C", "D", "E")


def _bucket():
    from google.cloud import storage
    return storage.Client().bucket(os.environ["GCS_BUCKET"])


def setup() -> Path:
    if WORK.exists():
        shutil.rmtree(WORK)
    shutil.copytree(HERE, WORK, ignore=shutil.ignore_patterns("__pycache__", "run_cloud.py"))
    ROOT.mkdir(parents=True, exist_ok=True)
    for name, target in (("spec_2026-11", WORK / "spec"), ("code", WORK / "code")):
        p = ROOT / name
        if p.is_symlink() or p.exists():
            if p.is_symlink():
                p.unlink()
            else:
                shutil.rmtree(p)
        p.symlink_to(target)
    return ROOT / "spec_2026-11" / "research_1022" / "shadow_AB"


def pull_cache(d: Path, bucket) -> bool:
    b = bucket.blob(PREFIX + "data.tgz")
    if not b.exists():
        return False
    buf = io.BytesIO(b.download_as_bytes())
    with tarfile.open(fileobj=buf, mode="r:gz") as t:
        t.extractall(d)
    for n in ("run.log",):
        bl = bucket.blob(PREFIX + n)
        if bl.exists():
            (d / n).write_bytes(bl.download_as_bytes())
    return True


def push(d: Path, bucket, ok: bool, err: str | None) -> dict:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        t.add(d / "data", arcname="data")
    bucket.blob(PREFIX + "data.tgz").upload_from_string(buf.getvalue(), content_type="application/gzip")
    out = {"updated_at": datetime.now(TZ).isoformat(timespec="seconds"), "ok": ok, "error": err, "ledgers": {},
           "source": "cloud job crypto-shadow-ab (analyst run_all.py, vendored unchanged)"}
    import csv
    for k in LEDGERS:
        p = d / f"ledger_{k}.csv"
        if p.exists():
            bucket.blob(PREFIX + p.name).upload_from_filename(str(p), content_type="text/csv")
            txt = p.read_text().strip()
            rows = list(csv.DictReader(io.StringIO(txt))) if txt else []
            out["ledgers"][k] = {"rows": rows[-60:], "n": len(rows), "last": rows[-1] if rows else None}
    for p in sorted(d.glob("test_ledger_*.csv")):
        bucket.blob(PREFIX + "test/" + p.name).upload_from_filename(str(p), content_type="text/csv")
        out.setdefault("test_ledgers", []).append(p.name)
    for p in sorted(d.glob("test_ledger_*.csv")):
        txt = p.read_text().strip()
        rows = list(csv.DictReader(io.StringIO(txt))) if txt else []
        out["ledgers"]["T" + p.stem[-1]] = {"rows": rows, "n": len(rows), "last": rows[-1] if rows else None}
    if os.environ.get("LEDGER_TEST_START"):
        out["test_start"] = os.environ["LEDGER_TEST_START"]
        bucket.blob(PREFIX + "test/latest.json").upload_from_string(json.dumps(out, ensure_ascii=False), content_type="application/json")
        return out
    tv = d.parents[1] / "u1_shorts" / "shadow_tv040"
    if (tv / "ledger.csv").exists():
        for n in ("ledger.csv", "state.json"):
            if (tv / n).exists():
                bucket.blob(PREFIX + "tv040/" + n).upload_from_filename(str(tv / n))
        out["tv040"] = list(csv.DictReader(io.StringIO((tv / "ledger.csv").read_text())))[-60:]
    if (d / "run.log").exists():
        bucket.blob(PREFIX + "run.log").upload_from_filename(str(d / "run.log"), content_type="text/plain")
        out["run_log_tail"] = (d / "run.log").read_text().splitlines()[-10:]
    bucket.blob(PREFIX + "latest.json").upload_from_string(json.dumps(out, ensure_ascii=False), content_type="application/json")
    return out


def fa_only() -> int:
    """FA_ONLY=1 (job crypto-fa-daily, 00:01 UTC = 08:01 Taipei): refresh public data, compute funding-arb
    targets, upload them, then start the reconcile job at once so the Demo legs fill close to the 08:00 open.
    Does not push the data cache or ledgers (the 08:20 full job still does)."""
    import time
    t0 = time.time()
    d = setup(); bucket = _bucket(); pull_cache(d, bucket)
    src = (d / "ledger_c.py").read_text()
    cut = src.index('if "--no-fetch" not in sys.argv')
    g = {"__name__": "fa_refresh", "__file__": str(d / "ledger_c.py")}
    exec(compile(src[:cut], g["__file__"], "exec"), g)
    g["refresh"]()
    t1 = time.time()
    sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
    import fa_capture
    fa = fa_capture.run(ROOT / "spec_2026-11", d / "data", os.environ.get("FA_PARITY_START", "2026-06-01"))
    t2 = time.time()
    fa["targets"]["computed_at"] = datetime.now(TZ).isoformat(timespec="seconds")
    fa["targets"]["timing_s"] = {"refresh": round(t1 - t0), "compute": round(t2 - t1)}
    bucket.blob(PREFIX + "parity_FA.json").upload_from_string(json.dumps(fa["parity"]), content_type="application/json")
    bucket.blob(PREFIX + "fa_targets.json").upload_from_string(json.dumps(fa["targets"]), content_type="application/json")
    log.info("fa_only targets bar=%s held=%s timing=%s", fa["targets"]["decided_bar"], fa["targets"]["held"], fa["targets"]["timing_s"])
    job = os.environ.get("RECONCILE_JOB_URI")
    if job:
        import google.auth, google.auth.transport.requests
        cred, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        r = google.auth.transport.requests.AuthorizedSession(cred).post(job, json={}, timeout=30)
        log.info("fa_only reconcile trigger status=%s", r.status_code)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("FA_ONLY"):
        return fa_only()
    d = setup()
    bucket = _bucket()
    had = pull_cache(d, bucket)
    log.info("shadow_ab cache=%s", had)
    ok, err = True, None
    try:
        subprocess.run([sys.executable, "run_all.py"], cwd=d, check=True)
    except subprocess.CalledProcessError as e:
        ok, err = False, f"run_all exit {e.returncode}"
        log.error("shadow_ab run_all failed: %s", err)
    if not os.environ.get("LEDGER_TEST_START"):
        tv = ROOT / "spec_2026-11" / "u1_shorts" / "shadow_tv040"
        try:
            st = bucket.blob(PREFIX + "tv040/state.json")
            if st.exists():
                (tv / "state.json").write_bytes(st.download_as_bytes())
            subprocess.run([sys.executable, "shadow.py"], cwd=tv, check=True)
        except Exception as e:  # noqa: BLE001
            ok, err = False, (err or "") + f" tv040 {e}"[:200]
    if not os.environ.get("LEDGER_TEST_START"):
        try:
            sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
            import e_capture, demo_e
            par = e_capture.run(d, os.environ.get("E_PARITY_START", "2026-09-01"))
            res = demo_e.parity(par["snaps"])
            res["from"] = os.environ.get("E_PARITY_START", "2026-09-01")
            bucket.blob(PREFIX + "parity_E.json").upload_from_string(json.dumps(res), content_type="application/json")
            tgt = e_capture.run(d, None)["targets"]
            bucket.blob(PREFIX + "e_targets.json").upload_from_string(json.dumps(tgt), content_type="application/json")
            log.info("demo_e parity=%s targets_bar=%s", res, tgt["decided_bar"])
        except Exception as e:  # noqa: BLE001
            log.exception("demo_e parity failed")
            bucket.blob(PREFIX + "parity_E.json").upload_from_string(json.dumps({"pass": False, "error": str(e)[:300]}))
    if not os.environ.get("LEDGER_TEST_START"):
        try:  # demo_e5 (E-min5, 5,000U): trade-by-trade parity over the full OOS history + next-open targets
            sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
            import e5_capture, demo_e5
            cap = e5_capture.run(demo_e5.CAPITAL, d)
            mins = dict(demo_e5.EXCHANGE_MIN_DEFAULT)
            try:
                import urllib.request as _u
                info = json.loads(_u.urlopen("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=20).read())
                mins.update({s["symbol"]: demo_e5.exchange_min(info, s["symbol"]) for s in info.get("symbols") or []})
            except Exception as e:  # noqa: BLE001
                log.warning("demo_e5 exchangeInfo fail %s (fallback mins)", e)
            res = demo_e5.parity(cap, mins)
            bucket.blob(PREFIX + "parity_E5.json").upload_from_string(json.dumps(res, default=str), content_type="application/json")
            bucket.blob(PREFIX + "e5_targets.json").upload_from_string(json.dumps(cap["targets"]), content_type="application/json")
            log.info("demo_e5 parity=%s", {k: res[k] for k in ("orders", "mismatches", "pass")})
        except Exception as e:  # noqa: BLE001
            log.exception("demo_e5 parity failed")
            bucket.blob(PREFIX + "parity_E5.json").upload_from_string(json.dumps({"pass": False, "error": str(e)[:300]}))
    if not os.environ.get("LEDGER_TEST_START"):
        try:
            sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
            import fa_capture
            fa = fa_capture.run(ROOT / "spec_2026-11", ROOT / "spec_2026-11" / "research_1022" / "shadow_AB" / "data", os.environ.get("FA_PARITY_START", "2026-06-01"))
            bucket.blob(PREFIX + "parity_FA.json").upload_from_string(json.dumps(fa["parity"]), content_type="application/json")
            bucket.blob(PREFIX + "fa_targets.json").upload_from_string(json.dumps(fa["targets"]), content_type="application/json")
            log.info("demo_fa parity=%s held=%s", fa["parity"], fa["targets"]["held"])
        except Exception as e:  # noqa: BLE001
            log.exception("demo_fa parity failed")
            bucket.blob(PREFIX + "parity_FA.json").upload_from_string(json.dumps({"pass": False, "error": str(e)[:300]}))
    out = push(d, bucket, ok, err)
    log.info("shadow_ab ok=%s ledgers=%s", ok, {k: v["n"] for k, v in out["ledgers"].items()})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
