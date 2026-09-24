"""PIN normalize + HMAC session token."""
from __future__ import annotations

import os
import time

# Ensure pin hash env before importing api
os.environ.setdefault("CONTROL_PIN_SALT", "crypto-trader-v1")
os.environ["CONTROL_PIN_HASH"] = "a" * 64  # placeholder; tests set real digests
os.environ["ALLOW_HTTP_PIN"] = "1"
os.environ["SESSION_HMAC_SECRET"] = "test-session-secret-for-unit-tests"


def test_normalize_pin_strip_and_fullwidth():
    from api import normalize_pin

    assert normalize_pin("  abc  ") == "abc"
    assert normalize_pin("１２３４") == "1234"
    assert normalize_pin("  ９９  ") == "99"
    assert normalize_pin(None) == ""


def test_session_token_roundtrip(monkeypatch):
    import api

    monkeypatch.setattr(api, "SESSION_HMAC_SECRET", b"unit-test-secret")
    monkeypatch.setattr(api, "SESSION_TTL_SEC", 3600)
    now = time.time()
    tok, exp = api.mint_session_token(now=now)
    assert exp == int(now) + 3600
    assert api.verify_session_token(tok) is True
    # tamper
    bad = tok[:-4] + ("AAAA" if not tok.endswith("AAAA") else "BBBB")
    assert api.verify_session_token(bad) is False


def test_session_token_expiry(monkeypatch):
    import api

    monkeypatch.setattr(api, "SESSION_HMAC_SECRET", b"unit-test-secret")
    monkeypatch.setattr(api, "SESSION_TTL_SEC", 1)
    tok, _ = api.mint_session_token(now=time.time() - 10)
    assert api.verify_session_token(tok) is False


def test_pin_digest_matches_normalized(monkeypatch):
    import hashlib
    import api

    salt = "crypto-trader-v1"
    pin = "secret42"
    digest = hashlib.sha256((salt + pin).encode()).hexdigest()
    monkeypatch.setattr(api, "PIN_SALT", salt)
    monkeypatch.setattr(api, "PIN_HASH", digest)
    assert api._check_pin_value("  secret42  ") is True
    assert api._check_pin_value("ｓｅｃｒｅｔ４２") is False  # letters not mapped
    assert api._check_pin_value("secret４２") is True  # fullwidth digits only
