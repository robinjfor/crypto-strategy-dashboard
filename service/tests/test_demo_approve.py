import json, pytest
import demo_fa


@pytest.fixture
def app(monkeypatch):
    import api
    store = {"demo_candidates": {demo_fa.SLOT: {"cloud_ordering_ok": True, "parity_pass": True, "enabled": False}}}
    class S:
        def load(self): return json.loads(json.dumps(store))
        def mutate(self, fn): store.update(fn(json.loads(json.dumps(store))))
    monkeypatch.setattr(api, "StateStore", S)
    monkeypatch.setattr(api, "_check_pin", lambda: (True, "", 200))
    return api.app.test_client(), store


def test_demo_approve_requires_all_gates_and_sets_flag_only(app, monkeypatch):
    c, store = app
    monkeypatch.setenv("DEMO_ACCEPTED", "")
    r = c.post("/control/approve", json={"strategy_id": demo_fa.SLOT})
    assert r.status_code == 400 and not store["demo_candidates"][demo_fa.SLOT].get("approved")
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    r = c.post("/control/approve", json={"strategy_id": demo_fa.SLOT})
    assert r.status_code == 200
    g = store["demo_candidates"][demo_fa.SLOT]
    assert g["approved"] is True and g["enabled"] is True and "approved" not in store
    assert store["control_log"][-1]["action"] == "demo_approve"


def test_demo_approve_needs_pin(app, monkeypatch):
    import api
    c, store = app
    monkeypatch.setattr(api, "_check_pin", lambda: (False, "PIN 錯誤", 401))
    assert c.post("/control/approve", json={"strategy_id": demo_fa.SLOT}).status_code == 401


def test_demo_revoke_disables(app, monkeypatch):
    import demo_candidates
    c, store = app
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    c.post("/control/approve", json={"strategy_id": demo_fa.SLOT})
    assert next(x for x in demo_candidates.view(store) if x["id"] == demo_fa.SLOT)["enabled"] is True
    r = c.post("/control/revoke", json={"strategy_id": demo_fa.SLOT})
    g = store["demo_candidates"][demo_fa.SLOT]
    assert r.status_code == 200 and g["approved"] is False and g["enabled"] is False
    assert store["control_log"][-1]["action"] == "demo_revoke"


def test_reconcile_keeps_enabled_only_if_approved():
    import inspect, reconcile
    assert 'g["enabled"] = bool(g.get("approved") and g.get("enabled"))' in inspect.getsource(reconcile)
    import demo_candidates
    v = demo_candidates.view({"demo_candidates": {demo_fa.SLOT: {"enabled": True}}})
    assert next(x for x in v if x["id"] == demo_fa.SLOT)["enabled"] is False


def test_demo_approve_records_x_actor(app, monkeypatch):
    c, store = app
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    r = c.post("/control/approve", json={"strategy_id": demo_fa.SLOT}, headers={"X-Actor": "自動交易專家"})
    assert r.status_code == 200
    assert store["demo_candidates"][demo_fa.SLOT]["approved_by"] == "自動交易專家"
    assert store["control_log"][-1]["actor"] == "自動交易專家"


def test_demo_approve_actor_defaults_to_site(app, monkeypatch):
    c, store = app
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    assert c.post("/control/approve", json={"strategy_id": demo_fa.SLOT}).status_code == 200
    assert store["control_log"][-1]["actor"] == "site"


def test_demo_approve_x_actor_percent_encoded(app, monkeypatch):
    from urllib.parse import quote
    c, store = app
    monkeypatch.setenv("DEMO_ACCEPTED", demo_fa.SLOT)
    assert c.post("/control/approve", json={"strategy_id": demo_fa.SLOT}, headers={"X-Actor": quote("自動交易專家")}).status_code == 200
    assert store["control_log"][-1]["actor"] == "自動交易專家"
