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
    assert g["approved"] is True and g["enabled"] is False and "approved" not in store
    assert store["control_log"][-1]["action"] == "demo_approve"


def test_demo_approve_needs_pin(app, monkeypatch):
    import api
    c, store = app
    monkeypatch.setattr(api, "_check_pin", lambda: (False, "PIN 錯誤", 401))
    assert c.post("/control/approve", json={"strategy_id": demo_fa.SLOT}).status_code == 401
