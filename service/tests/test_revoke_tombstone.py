import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("CONTROL_PIN_HASH", "a" * 64)
os.environ.setdefault("SESSION_HMAC_SECRET", "t")
import api
from slots import approval_mode

OP = "donchian20_s1.5_t1.5__OP__4h"  # in DEFAULT_APPROVED


def test_revoked_default_is_not_reseeded_and_not_live():
    st = {"approved_families": ["donchian_atr"], "approved": {}, "revoked_strategies": {OP: {"at": "x"}}}
    ap = api._ensure_approved(st)
    assert OP not in ap
    assert approval_mode(st, OP) == "signal_only"


def test_without_tombstone_default_still_seeded():
    st = {"approved_families": ["donchian_atr"], "approved": {}}
    assert OP in api._ensure_approved(st)
