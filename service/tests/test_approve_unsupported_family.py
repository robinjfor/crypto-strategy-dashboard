import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest
from slots import approve_family, SUPPORTED_FAMILIES
from allocation import RUNNER_FAMILIES


def test_core_satellite_family_not_approvable():
    f = "portfolio_core_satellite_perp"
    assert f not in SUPPORTED_FAMILIES and f not in RUNNER_FAMILIES
    st = {}
    with pytest.raises(ValueError):
        approve_family(st, f, at="2026-10-07T18:10:00+08:00")
    assert f not in (st.get("approved_families") or [])
