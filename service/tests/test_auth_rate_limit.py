"""/auth/login lockout: per client IP (rightmost XFF hop), only wrong PINs count,
5 fails/5 min → 5 min lock, success resets, expired tokens/empty never count."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CONTROL_PIN_SALT", "crypto-trader-v1")
os.environ.setdefault("CONTROL_PIN_HASH", "a" * 64)
os.environ["ALLOW_HTTP_PIN"] = "1"
os.environ.setdefault("SESSION_HMAC_SECRET", "test-session-secret-for-unit-tests")

import api  # noqa: E402

PIN = "unit-pin-42"


@pytest.fixture()
def c(monkeypatch):
    monkeypatch.setattr(api, "PIN_SALT", "salt")
    monkeypatch.setattr(api, "PIN_HASH", hashlib.sha256(("salt" + PIN).encode()).hexdigest())
    monkeypatch.setattr(api, "RATE_MAX", 5)
    monkeypatch.setattr(api, "RATE_WINDOW", 300.0)
    monkeypatch.setattr(api, "LOCKOUT_SEC", 300.0)
    monkeypatch.setattr(api, "TRUSTED_PROXY_HOPS", 0)
    monkeypatch.setattr(api, "build_status", lambda: {"ok": True})
    api._fail_buckets.clear()
    api._locked_until.clear()
    yield api.app.test_client()
    api._fail_buckets.clear()
    api._locked_until.clear()


def _h(ip, spoof=None):
    xff = f"{spoof}, {ip}" if spoof else ip
    return {"X-Forwarded-For": xff, "X-Forwarded-Proto": "https"}


def _login(c, pin, ip, spoof=None):
    return c.post("/auth/login", json={"pin": pin}, headers=_h(ip, spoof))


def test_five_wrong_pins_lock_that_ip_only(c):
    for _ in range(5):
        assert _login(c, "wrong", "1.1.1.1").status_code == 401
    r = _login(c, PIN, "1.1.1.1")
    assert r.status_code == 429 and r.headers.get("Retry-After")
    # other client unaffected (never global)
    assert _login(c, PIN, "2.2.2.2").status_code == 200


def test_lock_expires(c, monkeypatch):
    for _ in range(5):
        _login(c, "wrong", "1.1.1.1")
    assert _login(c, PIN, "1.1.1.1").status_code == 429
    real = api.time.time
    monkeypatch.setattr(api.time, "time", lambda: real() + 301)
    assert _login(c, PIN, "1.1.1.1").status_code == 200


def test_success_resets_counter(c):
    for _ in range(4):
        _login(c, "wrong", "1.1.1.1")
    assert _login(c, PIN, "1.1.1.1").status_code == 200
    for _ in range(4):
        _login(c, "wrong", "1.1.1.1")
    assert _login(c, PIN, "1.1.1.1").status_code == 200


def test_expired_token_polling_and_empty_never_lock(c):
    for _ in range(50):
        assert c.get("/status", headers={**_h("3.3.3.3"), "Authorization": "Bearer expired.bogus"}).status_code == 401
        assert c.get("/status", headers=_h("3.3.3.3")).status_code == 401
        assert _login(c, "", "3.3.3.3").status_code == 401
    assert _login(c, PIN, "3.3.3.3").status_code == 200


def test_valid_session_not_blocked_by_lock(c):
    tok = _login(c, PIN, "4.4.4.4").get_json()["token"]
    for _ in range(5):
        _login(c, "wrong", "4.4.4.4")
    assert c.get("/status", headers={**_h("4.4.4.4"), "Authorization": f"Bearer {tok}"}).status_code == 200


def test_client_ip_uses_rightmost_hop_not_spoofable_left(c):
    # attacker rotates a spoofed left-most XFF; real IP (rightmost) still locks
    for i in range(5):
        _login(c, "wrong", "5.5.5.5", spoof=f"10.0.0.{i}")
    assert _login(c, PIN, "5.5.5.5", spoof="10.9.9.9").status_code == 429
    # a victim whose spoofed/left value equals attacker's doesn't matter
    assert _login(c, PIN, "6.6.6.6", spoof="5.5.5.5").status_code == 200


def test_trusted_proxy_hops(c, monkeypatch):
    monkeypatch.setattr(api, "TRUSTED_PROXY_HOPS", 1)
    with api.app.test_request_context(headers={"X-Forwarded-For": "spoof, 7.7.7.7, 35.1.1.1"}):
        assert api._client_ip() == "7.7.7.7"
