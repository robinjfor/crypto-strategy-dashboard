"""The readiness page stays not ready until Robin approves the config and every gate is met."""
import json

import review_gate as G


def test_default_config_is_not_ready_and_a_large_bridge_diff_is_not_met():
    status = {
        "capital_basis": {"nav": 9000.0, "nav_usdt_m": 4000.0, "nav_usdc_m": 5000.0},
        "books": {"usdt": {"futures": {"wallet": 4000.0}}, "usdc": {"futures": {"wallet": 5000.0}}},
        "nav_history": [["2026-11-01", 9000.0]],
    }
    state = {
        "demo_e5v_exec": {"cash": 3000.0, "book": {}},
        "demo_vbo1h_exec": {"cash": 1500.0, "book": {}},
        "demo_r2s_exec": {"cash": 493.19, "book": {}},
        "migrations": {"r2s_unfunded_principal_20261111": {"done": True}},
        "u1_live": {"cash": 1000.0, "capital_usdt": 1000.0, "quote": "USDC", "units": {}},
    }
    page = G.evaluate(status, state, comments=[])
    assert page["config_approved"] is False
    assert page["page_status"] == "not ready"
    assert page["label_zh"] == "未就緒"
    by = {g["id"]: g for g in page["gates"]}
    assert by["G8"]["status"] == "not_met"
    assert "625" not in json.dumps(by)
    assert page["not_thresholds"] == ["625 USDT/月", "回測年報酬"]


def test_g9_reads_the_comment_store(monkeypatch, tmp_path):
    path = tmp_path / "comments.json"
    path.write_text(json.dumps({"version": 1, "comments": [
        {"severity": "critical", "status": "open", "body": "still open"},
        {"severity": "critical", "status": "acknowledged", "body": "not closed"},
        {"severity": "critical", "status": "done", "body": "closed"},
        {"severity": "low", "status": "open", "body": "not critical"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("REVIEW_COMMENTS_PATH", str(path))
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    from review_mcp import get_gate
    g9 = next(g for g in get_gate({}, {}).body["gates"] if g["id"] == "G9")
    assert g9["status"] == "not_met"
    assert g9["value"] == 2
    path.write_text(json.dumps({"version": 1, "comments": [
        {"severity": "critical", "status": "done"},
        {"severity": "critical", "status": "rejected"},
    ]}), encoding="utf-8")
    g9 = next(g for g in get_gate({}, {}).body["gates"] if g["id"] == "G9")
    assert g9["status"] == "met"
    assert g9["value"] == 0


def test_unapproved_config_stays_not_ready_even_if_marks_were_met(tmp_path):
    path = tmp_path / "readiness_gate.json"
    path.write_text(json.dumps({"approved": False, "approved_by": None, "approved_at": None}), encoding="utf-8")
    page = G.evaluate({"nav_history": []}, {}, comments=[], config_path=path)
    assert page["page_status"] == "not ready"
