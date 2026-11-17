"""External reviewer MCP + REST. No network, no Binance, no deploy."""
from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("CONTROL_PIN_SALT", "crypto-trader-v1")
os.environ.setdefault("CONTROL_PIN_HASH", "b" * 64)
os.environ.setdefault("ALLOW_HTTP_PIN", "1")
os.environ.setdefault("SESSION_HMAC_SECRET", "test-session-secret-for-unit-tests")

TOKEN = "r" * 40
SECRET_MARKERS = (
    "SHOULD_NOT_LEAK_KEY",
    "ACCOUNT-12345678",
    "PIN-9999",
    "LIQ-TOKEN-SECRET",
    "EXEC-SECRET",
    "STATE-PIN-VALUE",
)

STATUS = {
    "ok": True,
    "updated_at": "2026-11-17T12:00:00+08:00",
    "source": "cloud",
    "source_label": "Binance Demo · Cloud Run",
    "mode": "dry-run",
    "paused": False,
    "paused_slots": {"demo_r2s": {"paused_at": "2026-11-12T08:05:00+08:00"}},
    "capital_basis": {
        "nav": 10010.5,
        "pnl": 10.5,
        "peak": 10100.0,
        "max_drawdown": 89.5,
        "max_drawdown_pct": 0.008861,
        "starting_total": 10000.0,
        "nav_usdt_m": 5010.5,
        "nav_usdc_m": 5000.0,
        "api_key": "SHOULD_NOT_LEAK_KEY",
        "account_id": "ACCOUNT-12345678",
    },
    "positions": [
        {
            "symbol": "BTCUSDT",
            "slot": "demo_e5",
            "code": "E5",
            "strategy_id": "e5",
            "side": "LONG",
            "qty": 0.01,
            "mark": 90000.0,
            "unrealized_pnl": 12.5,
            "leverage": 1,
            "liq_distance": 0.42,
            "venue": "futures",
            "status": "FILLED",
            "pin": "PIN-9999",
        }
    ],
    "liq_distance": {
        "threshold": 0.15,
        "positions": [{"symbol": "BTCUSDT", "side": "LONG", "distance": 0.42, "liq": 52000.0, "mark": 90000.0}],
        "near": [],
        "token": "LIQ-TOKEN-SECRET",
    },
    "books": {
        "usdt": {"fees_usdt": 1.25, "equity": 5010.5, "realized_pnl": 4.0},
        "usdc": {"fees_usdt": 0.4, "equity": 5000.0},
    },
    "demo_e5_exec": {"cash": 100.0, "fees": 0.8, "api_secret": "EXEC-SECRET"},
    "netting": {
        "slots": {
            "demo_e5": {"pnl": 3.2, "equity": 5003.2, "cash": 100.0, "capital": 5000.0, "positions": {"BTCUSDT": {"qty": 0.01}}},
        }
    },
    "recent_fills": [
        {"symbol": "BTCUSDT", "code": "E5", "time": "2026-11-17T11:00:00+08:00", "side": "BUY", "qty": 0.01, "price": 89000.0, "quoteQty": 890.0, "commission": 0.1, "id": 1},
        {"symbol": "ETHUSDT", "code": "E5", "time": "2026-11-17T10:00:00+08:00", "side": "SELL", "qty": 0.2, "price": 3000.0, "quoteQty": 600.0, "commission": 0.05, "id": 2},
        {"symbol": "SOLUSDT", "code": "S", "time": "2026-11-16T09:00:00+08:00", "side": "BUY", "qty": 1.0, "price": 150.0, "quoteQty": 150.0, "commission": 0.02, "id": 3},
    ],
    "closed_trades": [
        {"symbol": "ETHUSDT", "code": "E5", "closed_at": "2026-11-15T08:00:00+08:00", "side": "SELL", "qty": 0.1, "price": 3100.0, "pnl_usdt": 8.0},
    ],
    "armed_slots": [
        {
            "slot": "demo_e5",
            "strategy_id": "e5",
            "code": "E5",
            "symbol": "BTCUSDT",
            "family": "ema_cross_atr",
            "venue": "futures",
            "tf": "1h",
            "status": "flat",
            "status_zh": "空手",
            "approved": True,
            "order_mode": "live",
            "label_zh": "已核准 · 上線待命",
            "entry_condition": "EMA12/26 黃金交叉",
            "leverage": 1,
            "quote_usdt": 5000,
            "api_secret": "EXEC-SECRET",
        },
        {
            "slot": "demo_r2s",
            "strategy_id": "r2s",
            "code": "R2S",
            "symbol": "ETHUSDT",
            "family": "short_trend_perp",
            "venue": "futures",
            "tf": "1d",
            "status": "paused",
            "status_zh": "暫停",
            "approved": True,
            "order_mode": "live",
            "label_zh": "已核准 · 上線待命",
            "entry_condition": "死叉做空",
            "leverage": 1,
        },
    ],
    "family_approvals": [{"family": "ema_cross_atr", "code": "E"}],
    "allocation": {"slots": [{"id": "demo_e5", "enabled": True}, {"id": "demo_r2s", "enabled": False}]},
    "orchestrator": {
        "weights": {"demo_e5": 1.0, "demo_r2s": 0.0},
        "targets": {"demo_e5": 5000.0, "demo_r2s": 0.0},
        "portfolio_equity": 10010.5,
        "peak": 10100.0,
        "drawdown": 0.008861,
        "generated_at": "2026-11-17T08:05:00+08:00",
        "actions": ["demo_e5: sizing capital 5000"],
    },
    "orchestrator_policy": {"base_weights": {"demo_e5": 1.0, "demo_r2s": 0.0}},
    "slot_caps": {"demo_e5": {"equity": 5000}},
    "parity": {
        "status": "ok",
        "n_match": 2,
        "n_mismatch": 0,
        "n_unsupported": 0,
        "last_run_at": "2026-11-17T08:30:00+08:00",
        "day_utc": "2026-11-17",
    },
    "live_vs_backtest": {
        "at": "2026-11-17T08:30:00+08:00",
        "day_utc": "2026-11-17",
        "n_match": 2,
        "n_mismatch": 0,
        "rows": [
            {"code": "E5", "symbol": "BTCUSDT", "strategy_id": "e5", "slot": "demo_e5", "problems": [], "pause": False},
            {"code": "R2S", "symbol": "ETHUSDT", "strategy_id": "r2s", "slot": "demo_r2s", "problems": ["weight 0"], "pause": True},
        ],
    },
}

STATE = {
    "capital_basis_history": [["2026-11-16", 10000.0], ["2026-11-17", 10010.5]],
    "orchestrator": {"history": [{"bar": "2026-11-16", "P": 10000.0, "dd": 0.0, "bear": False, "hivol": False}]},
    "pin": "STATE-PIN-VALUE",
    "api_key": "SHOULD_NOT_LEAK_KEY",
}


def _no_secrets(payload) -> None:
    blob = json.dumps(payload, ensure_ascii=False)
    for marker in SECRET_MARKERS:
        assert marker not in blob
    assert TOKEN not in blob


@pytest.fixture
def client(monkeypatch, tmp_path):
    import api
    import review_mcp

    monkeypatch.setattr(api, "PIN_HASH", "b" * 64)
    monkeypatch.setattr(api, "review_snapshot", lambda: (STATUS, STATE))
    monkeypatch.setenv("REVIEWER_TOKEN", TOKEN)
    monkeypatch.setenv("ALLOW_HTTP_PIN", "1")
    monkeypatch.setenv("REVIEW_COMMENTS_PATH", str(tmp_path / "comments.json"))
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    review_mcp.reset_limits()
    return api.app.test_client()


def _auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _rpc(client, method, params=None, token=TOKEN, accept="application/json"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", data=json.dumps(body), headers={**_auth(token), "Accept": accept})


def _tool(client, name, arguments=None):
    res = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert res.status_code == 200, res.get_data(as_text=True)
    payload = res.get_json()
    assert payload["jsonrpc"] == "2.0"
    content = payload["result"]["content"][0]["text"]
    return payload["result"], json.loads(content)


def test_auth_missing_and_wrong_token(client):
    assert client.get("/review/strategies").status_code == 401
    assert client.post("/mcp", data="{}", headers={"Content-Type": "application/json"}).status_code == 401
    bad = _auth("nope")
    assert client.get("/review/performance", headers=bad).status_code == 401
    assert client.post("/mcp", data="{}", headers=bad).status_code == 401
    # Query token is not a session and must not open control routes.
    assert client.post(f"/control/pause?access_token={TOKEN}", json={}).status_code == 401


def test_reviewer_cannot_hit_control_or_status(client):
    h = _auth()
    assert client.get("/status", headers=h).status_code == 403
    for path in ("/control/pause", "/control/resume", "/control/close", "/control/close_all",
                 "/control/approve", "/control/revoke", "/control/approve_family", "/control/revoke_family"):
        res = client.post(path, headers=h, json={"strategy_id": "e5", "family": "ema_cross_atr"})
        assert res.status_code == 403, path
        assert TOKEN not in res.get_data(as_text=True)
    res = client.post("/review/comments/c_1234567890abcdef/status", headers=h, json={"status": "done"})
    assert res.status_code == 403


def test_readonly_token_cannot_review(client, monkeypatch):
    import api
    monkeypatch.setattr(api, "READONLY_TOKEN", "q" * 40)
    res = client.get("/review/strategies", headers=_auth("q" * 40))
    assert res.status_code == 403


def test_query_token_reads(client):
    res = client.get(f"/review/strategies?access_token={TOKEN}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["account"] == "binance_demo"
    assert TOKEN not in res.get_data(as_text=True)


def test_mcp_initialize_and_tools(client):
    res = _rpc(client, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    assert res.status_code == 200
    result = res.get_json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "crypto-strategy-review"
    assert "post_comment" in result["instructions"]
    assert res.headers["MCP-Protocol-Version"] == "2025-03-26"
    listed = _rpc(client, "tools/list").get_json()["result"]["tools"]
    names = [t["name"] for t in listed]
    assert names == [
        "list_strategies", "get_performance", "get_positions", "get_trades",
        "get_backtest", "get_orchestrator_plan", "list_comments", "post_comment", "reply_comment",
    ]
    note = client.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), headers=_auth())
    assert note.status_code == 202
    assert client.get("/mcp", headers=_auth()).status_code == 405


def test_mcp_sse_when_client_asks_only_for_events(client):
    res = _rpc(client, "ping", accept="text/event-stream")
    assert res.status_code == 200
    assert res.mimetype == "text/event-stream"
    raw = res.get_data(as_text=True)
    assert raw.startswith("event: message\ndata: ")
    assert '"result"' in raw


def test_each_read_tool_shape_and_redaction(client):
    _result, strategies = _tool(client, "list_strategies")
    assert strategies["ok"] is True
    by_slot = {s["slot"]: s for s in strategies["strategies"]}
    assert by_slot["demo_e5"]["approved"] is True
    assert by_slot["demo_e5"]["weight"] == 1.0
    assert by_slot["demo_e5"]["params_summary"] == "EMA12/26 黃金交叉"
    assert by_slot["demo_e5"]["status"] == "flat"
    assert by_slot["demo_r2s"]["slot_paused"] is True
    assert by_slot["demo_r2s"]["weight"] == 0.0
    _no_secrets(strategies)

    _result, perf = _tool(client, "get_performance")
    assert perf["capital_basis"]["nav"] == 10010.5
    assert perf["capital_basis"]["pnl"] == 10.5
    assert perf["capital_basis"]["max_drawdown"] == 89.5
    assert perf["capital_basis"]["api_key"] == "[redacted]"
    assert perf["capital_basis"]["account_id"] == "[redacted]"
    assert perf["nav_history"][1] == ["2026-11-17", 10010.5]
    assert perf["virtual_books"]["demo_e5"]["pnl"] == 3.2
    assert perf["fees"]["books"]["usdt"]["fees_usdt"] == 1.25
    assert perf["fees"]["sleeves"]["demo_e5_exec"]["fees"] == 0.8
    assert "api_secret" not in perf["fees"]["sleeves"]["demo_e5_exec"]
    _no_secrets(perf)

    _result, positions = _tool(client, "get_positions")
    assert positions["positions"][0]["qty"] == 0.01
    assert positions["positions"][0]["liq_distance"] == 0.42
    assert "pin" not in positions["positions"][0]
    assert positions["liq_distance"]["positions"][0]["distance"] == 0.42
    assert positions["liq_distance"]["token"] == "[redacted]"
    _no_secrets(positions)

    _result, trades = _tool(client, "get_trades", {"limit": 2})
    assert trades["total"] == 4
    assert len(trades["trades"]) == 2
    assert trades["next_cursor"] == "2"
    assert trades["trades"][0]["kind"] == "fill"
    assert trades["trades"][0]["commission"] == 0.1
    _result, page2 = _tool(client, "get_trades", {"limit": 2, "cursor": trades["next_cursor"]})
    assert len(page2["trades"]) == 2
    assert page2["next_cursor"] is None
    _no_secrets(trades)

    _result, backtest = _tool(client, "get_backtest")
    assert backtest["parity"]["status"] == "ok"
    assert backtest["parity"]["n_match"] == 2
    assert {r["code"] for r in backtest["rows"]} == {"E5", "R2S"}
    _result, only = _tool(client, "get_backtest", {"strategy": "E5"})
    assert [r["code"] for r in only["rows"]] == ["E5"]
    _no_secrets(backtest)

    _result, plan = _tool(client, "get_orchestrator_plan")
    assert plan["latest"]["portfolio_equity"] == 10010.5
    assert plan["latest"]["weights"]["demo_e5"] == 1.0
    assert plan["history"][0]["bar"] == "2026-11-16"
    assert plan["policy"]["base_weights"]["demo_r2s"] == 0.0
    _no_secrets(plan)

    _result, comments = _tool(client, "list_comments")
    assert comments["comments"] == []
    assert comments["timezone"] == "Asia/Taipei"


def test_rest_mirrors_match_tools(client):
    tool = _tool(client, "get_performance")[1]
    rest = client.get("/review/performance", headers=_auth()).get_json()
    assert rest["capital_basis"]["nav"] == tool["capital_basis"]["nav"]
    assert rest["virtual_books"] == tool["virtual_books"]
    trades = client.get("/review/trades?limit=1", headers=_auth()).get_json()
    assert trades["total"] == 4 and len(trades["trades"]) == 1
    assert client.get("/review/backtest?strategy=R2S", headers=_auth()).get_json()["rows"][0]["code"] == "R2S"
    orch = client.get("/review/orchestrator?history_limit=5", headers=_auth()).get_json()
    assert orch["history"][0]["P"] == 10000.0


def test_comment_post_reply_status_and_redaction(client):
    import api
    posted = _tool(client, "post_comment", {
        "reviewer": "chatgpt",
        "topic": "E5 overfitting",
        "strategy": "demo_e5",
        "severity": "high",
        "body": f"live diverges from backtest. leak {TOKEN}",
        "suggested_action": "freeze new entries until parity is explained",
    })[1]
    assert posted["ok"] is True
    comment = posted["comment"]
    assert comment["reviewer"] == "chatgpt"
    assert comment["status"] == "open"
    assert comment["severity"] == "high"
    assert TOKEN not in comment["body"]
    assert "[redacted]" in comment["body"]
    assert comment["created_at"].endswith("+08:00")
    cid = comment["id"]

    replied = _tool(client, "reply_comment", {"comment_id": cid, "reviewer": "claude", "body": "agree on the deviation"})[1]
    assert replied["comment"]["replies"][0]["author"] == "claude"
    assert replied["comment"]["status"] == "open"

    # Reviewer REST mirror.
    rest = client.post("/review/comments", headers=_auth(), json={
        "reviewer": "other", "topic": "fees", "severity": "low", "body": "fees look small",
    })
    assert rest.status_code == 200
    assert rest.get_json()["comment"]["reviewer"] == "other"

    listed = client.get("/review/comments?status=open", headers=_auth()).get_json()
    assert listed["count"] == 2

    # Team session replies and closes. Reviewer token cannot.
    monkey_tok, _exp = api.mint_session_token()
    session = _auth(monkey_tok)
    denied = client.post(f"/review/comments/{cid}/status", headers=_auth(), json={"status": "done"})
    assert denied.status_code == 403
    updated = client.post(f"/review/comments/{cid}/status", headers=session, json={"status": "acknowledged", "note": "will check parity"})
    assert updated.status_code == 200, updated.get_data(as_text=True)
    assert updated.get_json()["comment"]["status"] == "acknowledged"
    assert updated.get_json()["comment"]["status_events"][-1]["by"] == "team"
    team_reply = client.post(f"/review/comments/{cid}/reply", headers=session, json={"body": "parity job rerun queued"})
    assert team_reply.status_code == 200
    authors = [r["author"] for r in team_reply.get_json()["comment"]["replies"]]
    assert authors == ["claude", "team"]
    closed = client.post(f"/review/comments/{cid}/status", headers=session, json={"status": "done"})
    assert closed.get_json()["comment"]["status"] == "done"
    done = client.get("/review/comments?status=done", headers=session).get_json()
    assert done["count"] == 1
    _no_secrets(done)


def test_size_limit(client, monkeypatch):
    monkeypatch.setenv("REVIEW_BODY_MAX", "20")
    res = client.post("/review/comments", headers=_auth(), json={
        "reviewer": "claude", "topic": "risk", "body": "x" * 21,
    })
    assert res.status_code == 400
    assert client.get("/review/comments", headers=_auth()).get_json()["count"] == 0


def test_rate_limit(client, monkeypatch):
    monkeypatch.setenv("REVIEW_WRITE_MAX", "2")
    monkeypatch.setenv("REVIEW_WRITE_BURST", "2")
    for i in range(2):
        res = client.post("/review/comments", headers=_auth(), json={
            "reviewer": "chatgpt", "topic": f"t{i}", "body": "ok",
        })
        assert res.status_code == 200, res.get_data(as_text=True)
    blocked = client.post("/review/comments", headers=_auth(), json={
        "reviewer": "chatgpt", "topic": "t3", "body": "no",
    })
    assert blocked.status_code == 429
    assert blocked.headers.get("Retry-After")
    mcp = _rpc(client, "tools/call", {"name": "post_comment", "arguments": {"reviewer": "claude", "topic": "t4", "body": "no"}})
    assert mcp.status_code == 429

    monkeypatch.setenv("REVIEW_READ_MAX", "2")
    import review_mcp
    review_mcp.reset_limits()
    assert client.get("/review/positions", headers=_auth()).status_code == 200
    assert client.get("/review/positions", headers=_auth()).status_code == 200
    assert client.get("/review/positions", headers=_auth()).status_code == 429


def test_openapi_schema(client):
    res = client.get("/review/openapi.json", headers=_auth())
    assert res.status_code == 200
    spec = res.get_json()
    assert spec["openapi"] == "3.0.3"
    assert spec["components"]["securitySchemes"]["reviewerBearer"]["scheme"] == "bearer"
    for path in ("/review/strategies", "/review/performance", "/review/positions", "/review/trades",
                 "/review/backtest", "/review/orchestrator", "/review/comments"):
        assert path in spec["paths"]
    assert "auto_trader" in json.dumps(spec["components"]["schemas"])
    assert "post" in spec["paths"]["/review/comments"]
    assert "/review/comments/{comment_id}/reply" in spec["paths"]
    assert "/control/revoke" not in json.dumps(spec)
    assert "status" not in spec["paths"]["/review/comments/{comment_id}/reply"]
    blob = json.dumps(spec)
    assert TOKEN not in blob
    assert "access_token" not in blob


def test_store_does_not_publish_objects():
    from pathlib import Path
    import review_mcp
    src = Path(review_mcp.__file__).read_text()
    assert "make_public(" not in src
    assert "generate_signed_url" not in src
    assert "public_url" not in src
    assert "/control/revoke" not in src


def test_session_cannot_post_external_comment(client):
    import api
    tok, _exp = api.mint_session_token()
    res = client.post("/review/comments", headers=_auth(tok), json={"reviewer": "other", "topic": "x", "body": "y"})
    assert res.status_code == 403
    assert client.get("/review/comments", headers=_auth(tok)).status_code == 200


def test_auto_trader_thread(client):
    posted = client.post("/review/comments", headers=_auth(), json={
        "reviewer": "auto_trader",
        "topic": "liquidation distance",
        "strategy": "demo_e5",
        "severity": "high",
        "body": "distance should be checked against the exchange mark",
        "suggested_action": "do not add size",
    })
    assert posted.status_code == 200, posted.get_data(as_text=True)
    root = posted.get_json()["comment"]
    assert root["reviewer"] == "auto_trader"
    assert root["display_name"] == "自動交易專家"
    cid = root["id"]

    denied = client.post("/review/comments", headers=_auth(), json={
        "reviewer": "team", "topic": "nope", "body": "impersonate",
    })
    assert denied.status_code == 400

    claude = client.post(f"/review/comments/{cid}/reply", headers=_auth(), json={
        "reviewer": "claude", "body": "agreed, 0.42 is still wide",
    })
    assert claude.status_code == 200
    first = claude.get_json()["comment"]["replies"][0]
    assert first["author"] == "claude"
    assert first["display_name"] == "Claude"
    assert first["in_reply_to"] is None

    nested = client.post(f"/review/comments/{cid}/reply", headers=_auth(), json={
        "reviewer": "auto_trader",
        "body": "then the open question is the cap, not the distance",
        "in_reply_to": first["id"],
    })
    assert nested.status_code == 200, nested.get_data(as_text=True)
    comment = nested.get_json()["comment"]
    assert [r["author"] for r in comment["replies"]] == ["claude", "auto_trader"]
    assert comment["replies"][1]["in_reply_to"] == first["id"]
    assert comment["replies"][1]["display_name"] == "自動交易專家"
    thread = comment["thread"]
    assert len(thread) == 1
    assert thread[0]["author"] == "claude"
    assert thread[0]["replies"][0]["author"] == "auto_trader"
    assert thread[0]["replies"][0]["display_name"] == "自動交易專家"

    missing = client.post(f"/review/comments/{cid}/reply", headers=_auth(), json={
        "reviewer": "chatgpt", "body": "orphan", "in_reply_to": "r_" + "ab" * 8,
    })
    assert missing.status_code == 400

    listed = _tool(client, "list_comments")[1]
    match = next(c for c in listed["comments"] if c["id"] == cid)
    assert match["display_name"] == "自動交易專家"
    assert match["thread"][0]["replies"][0]["author"] == "auto_trader"
    _no_secrets(listed)
