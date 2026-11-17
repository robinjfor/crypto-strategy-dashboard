"""E5 target weight/qty versus the virtual book. No network, no Binance."""
import e5_tracking as T

SLOT = "demo_e5_min5_x175_t05top20"
PRICES = {"BTCUSDT": 50000.0, "ETHUSDT": 2500.0, "SOLUSDT": 100.0, "XRPUSDT": 1.0}


def _targets(**extra):
    base = {
        "decided_bar": "2026-11-16",
        "w1": [0.5, 0.0],
        "ws": {"SOLUSDT": 0.05, "XRPUSDT": 0.001},
        "dbs": {"SOLUSDT": 0.01},
        "m": 1.0,
        "rw": [0.01],
        "lv": 1.75,
        "prices": PRICES,
    }
    base.update(extra)
    return base


def _state(book, targets=None, history=None):
    st = {
        "orchestrator": {"targets": {SLOT: 1000.0}, "weights": {SLOT: 1.0}},
        "e5_targets": targets or _targets(),
        "demo_e5v_exec": {
            "cash": 800.0,
            "book": book,
            "sizing_equity": 1000.0,
            "last": {
                "bar": "2026-11-16",
                "equity": 1000.0,
                "skipped": ["BTCUSDT net not filled"],
            },
        },
    }
    if history is not None:
        st["e5_targets_history"] = history
    return st


def test_latest_day_diffs_reasons_and_summary():
    book = {"BTCUSDT": 0.004, "ETHUSDT": 1.0, "SOLUSDT": 0.17, "XRPUSDT": 10.0}
    report = T.compare(_state(book), status={})
    assert report["slot"] == SLOT
    assert report["targets_source"] == "state"
    assert report["orchestrator_target"] == 1000.0
    assert len(report["days"]) == 1
    day = report["days"][0]
    assert day["bar"] == "2026-11-16"
    assert day["sizing_capital"] == 1000.0
    assert day["actual_source"] == "e5_exec"
    by = {r["symbol"]: r for r in day["symbols"]}

    btc = by["BTCUSDT"]
    assert btc["target_weight"] == 0.5
    assert btc["target_qty"] == 0.008
    assert btc["actual_qty"] == 0.004
    assert btc["qty_diff"] == -0.004
    assert btc["reason"] == "unfilled"

    eth = by["ETHUSDT"]
    assert eth["target_qty"] == 0.0
    assert eth["actual_qty"] == 1.0
    assert eth["reason"] == "unfilled"

    sol = by["SOLUSDT"]
    assert sol["target_weight"] == 0.0875
    assert sol["target_qty"] == 0.175
    assert sol["actual_qty"] == 0.17
    assert sol["reason"] == "deadband"

    xrp = by["XRPUSDT"]
    assert xrp["target_qty"] == 0.35
    assert xrp["actual_qty"] == 10.0
    assert xrp["reason"] == "residual_close"

    # A coin that is not on the E5 book is not a row.
    assert "VBOUSDT" not in by

    summary = day["summary"]
    assert summary["reasons"]["unfilled"] == 2
    assert summary["reasons"]["deadband"] == 1
    assert summary["reasons"]["residual_close"] == 1
    assert summary["reasons"]["skip_min"] == 0
    assert summary["pct_target_notional_achieved"] == round(217.35 / 417.85, 6)
    weights = [(r["actual_account_weight"], r["target_account_weight"]) for r in day["symbols"]]
    import math
    te = math.sqrt(sum((a - t) ** 2 for a, t in weights) / len(weights))
    assert summary["tracking_error"] == round(te, 6)


def test_skip_min_when_the_open_is_below_the_minimum():
    book = {"BTCUSDT": 0.008, "ETHUSDT": 0.0, "SOLUSDT": 0.175, "XRPUSDT": 0.0}
    report = T.compare(_state(book), status={})
    by = {r["symbol"]: r for r in report["days"][0]["symbols"]}
    assert by["XRPUSDT"]["target_qty"] == 0.35
    assert by["XRPUSDT"]["actual_qty"] == 0.0
    assert by["XRPUSDT"]["reason"] == "skip_min"
    assert by["BTCUSDT"]["qty_diff"] == 0.0
    assert by["BTCUSDT"]["reason"] is None
    assert by["SOLUSDT"]["reason"] is None


def test_older_day_uses_its_own_book_and_not_today():
    older = _targets(decided_bar="2026-11-15", equity=1000.0, book={"BTCUSDT": 0.008, "ETHUSDT": 0.0},
                     ws={}, dbs={})
    report = T.compare(_state({"BTCUSDT": 0.004}, history=[older]), status={})
    assert [d["bar"] for d in report["days"]] == ["2026-11-15", "2026-11-16"]
    old = report["days"][0]
    assert old["actual_source"] == "snapshot"
    old_btc = next(r for r in old["symbols"] if r["symbol"] == "BTCUSDT")
    assert old_btc["actual_qty"] == 0.008
    assert old_btc["qty_diff"] == 0.0
    today = next(r for r in report["days"][1]["symbols"] if r["symbol"] == "BTCUSDT")
    assert today["actual_qty"] == 0.004


def test_symbol_filter_and_missing_plan(monkeypatch):
    import builtins
    import review_mcp
    monkeypatch.setenv("GCS_BUCKET", "not-a-real-bucket")
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "google.cloud" or str(name).startswith("google.cloud"):
            raise AssertionError("GCS is not a source when the plan is already on state")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    state = _state({"BTCUSDT": 0.004})
    out = review_mcp.get_backtest({}, state, {}).body
    assert out["e5"]["targets_source"] == "state"
    assert out["e5"]["days"][0]["symbols"]
    only = review_mcp.get_backtest({}, state, {"strategy": "SOLUSDT"}).body
    assert [r["symbol"] for r in only["e5"]["days"][0]["symbols"]] == ["SOLUSDT"]
    assert review_mcp.get_backtest({}, state, {"strategy": "R2S"}).body.get("e5") is None
    monkeypatch.setattr(builtins, "__import__", real_import)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    empty = T.compare({}, status={})
    assert empty["targets_source"] == "missing"
    assert empty["days"] == []
