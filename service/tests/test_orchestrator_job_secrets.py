"""2026-11-12 08:05 Taipei: crypto-orchestrator-daily wrote no plan.

PR #11 made the pool read GET /fapi/v2/account (signed), but the job was still deployed with
--clear-secrets, so the read failed, the job logged a plain-text error, skipped, and exited 0.
"""
import json
import logging
import pathlib
import sys
import types

import orchestrator as O

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _orch_deploy_block() -> str:
    y = (ROOT / ".github/workflows/deploy-trader.yml").read_text()
    i = y.index('gcloud run jobs deploy "${ORCH_JOB}"')
    return y[i:y.index("gcloud run jobs add-iam-policy-binding", i)]


def test_orchestrator_job_mounts_the_demo_keys():
    blk = _orch_deploy_block()
    assert "--clear-secrets" not in blk
    assert "BINANCE_DEMO_API_KEY=BINANCE_DEMO_API_KEY:latest" in blk
    assert "BINANCE_DEMO_API_SECRET=BINANCE_DEMO_API_SECRET:latest" in blk
    assert "orchestrator.py" in blk


class _Store:
    def __init__(self, state):
        self.state = state
        self.mutated = None

    def load(self):
        return dict(self.state)

    def mutate(self, fn):
        self.mutated = fn(dict(self.state))
        return self.mutated


def _patch(monkeypatch, client):
    store = _Store({})
    monkeypatch.setitem(sys.modules, "futures_client", types.SimpleNamespace(FuturesDemoClient=lambda: client))
    monkeypatch.setitem(sys.modules, "state_store", types.SimpleNamespace(StateStore=lambda: store))
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    return store


class _NoKeys:
    api_key = ""
    api_secret = ""

    def account(self):
        raise AssertionError("must not call the signed endpoint without keys")


def test_missing_keys_skip_loudly_and_exit_nonzero(monkeypatch, caplog):
    _patch(monkeypatch, _NoKeys())
    with caplog.at_level(logging.ERROR, logger="orchestrator"):
        res = O.run(dry_run=False)
    assert res["skipped"] == "missing demo api key"
    assert O.exit_code(res) == 1
    assert any("missing_demo_api_key" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_with_keys_the_plan_is_written_with_e100_and_pool_3875(monkeypatch):
    import pandas as pd

    class _Keys:
        api_key = "k"
        api_secret = "s"

        def account(self):
            return {"assets": [{"asset": "USDT", "marginBalance": "4875.17"},
                               {"asset": "USDC", "marginBalance": "4808.33"}]}

        def mark_price(self, s):
            return 1.0

    store = _patch(monkeypatch, _Keys())
    idx = pd.date_range("2022-06-01", periods=1600, freq="D", tz="UTC")
    close = pd.Series([100.0 + 0.01 * i for i in range(1600)], index=idx)   # up-trend, calm: not bear, not hi-vol
    monkeypatch.setattr(O, "btc_closes", lambda now: close)
    res = O.run(dry_run=False)
    assert O.exit_code(res) == 0
    assert res["eligible"]["usd"] == 3875.17
    assert res["weights"] == {"demo_e5_min5_x175_t05top20": 1.0, "u1_live": 0.0,
                              "demo_r2s_top10_3k": 0.0, "demo_funding_arb_1x": 0.0}
    assert res["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17
    assert res["targets"]["demo_r2s_top10_3k"] == 0.0
    assert store.mutated["orchestrator"]["targets"]["demo_e5_min5_x175_t05top20"] == 3875.17


def test_bear_halves_e_and_hivol_scales_by_07():
    w, x = O.weights(bear=True, hivol=True)
    assert w["E"] == 0.5 and w["R"] == 0 and w["U"] == 0 and w["F"] == 0 and x == 0.7


def test_json_log_lines_carry_severity():
    rec = logging.LogRecord("orchestrator", logging.ERROR, __file__, 1, "orchestrator_skip reason=%s", ("x",), None)
    d = json.loads(O.CloudJsonFormatter().format(rec))
    assert d["severity"] == "ERROR" and "orchestrator_skip reason=x" in d["message"]


def test_deploy_has_a_live_orchestrator_rerun_input():
    y = (ROOT / ".github/workflows/deploy-trader.yml").read_text()
    assert "run_orchestrator_live_now:" in y
    i = y.index("Run orchestrator once (LIVE)")
    blk = y[i:y.index("- name:", i + 10)]
    assert "--dry-run" not in blk and "assert d['dry_run'] is False" in blk
