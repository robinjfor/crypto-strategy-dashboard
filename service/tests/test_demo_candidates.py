import demo_candidates as dc


def test_disabled_and_not_approvable_by_default():
    v = dc.view({})
    assert len(v) == 2 and all(x["enabled"] is False and x["approvable"] is False for x in v)


def test_approvable_needs_both_gates():
    s = {"demo_candidates": {"demo_e_8020_x175": {"cloud_ordering_ok": True}}}
    assert not dc.view(s)[0]["approvable"]
    s["demo_candidates"]["demo_e_8020_x175"]["parity_pass"] = True
    assert dc.view(s)[0]["approvable"] and dc.view(s)[0]["enabled"] is False


def test_weekly_breach():
    assert dc.weekly_breach(1006, 1000) and not dc.weekly_breach(1004, 1000)
