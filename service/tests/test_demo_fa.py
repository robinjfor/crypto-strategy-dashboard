import numpy as np
import demo_fa


def test_decide_entry_exit_and_k_cap():
    prev = np.array([True, False, False, False])
    u = np.array([True, True, True, False])
    f7 = np.array([-0.01, 0.30, 0.20, 0.50])   # held one drops below exit 0; idx3 out of universe
    f1 = np.array([0.0, 0.001, -0.001, 0.001])  # idx2 flipped negative
    new = demo_fa.decide(prev, u, f7, f1, np.ones(4, bool), k=10, exit_thr=0.0)
    assert new.tolist() == [False, True, False, False]
    f1b = np.zeros(4); new2 = demo_fa.decide(np.zeros(4, bool), np.ones(4, bool), np.array([.2, .3, .4, .5]), f1b, np.ones(4, bool), k=2)
    assert new2.tolist() == [False, False, True, True]


def test_simulate_matches_parity_self():
    rng = np.random.default_rng(0); n, m = 40, 6
    f7 = rng.uniform(-0.1, 0.4, (n, m)); f1 = rng.uniform(-0.0005, 0.001, (n, m))
    rs = rng.normal(0, .01, (n, m)); rp = rs + rng.normal(0, .001, (n, m)); fn = np.abs(rng.normal(0, 3e-4, (n, m)))
    r, k, t, hs = demo_fa.simulate(np.ones((n, m), bool), f7, f1, rs, rp, fn, k=3)
    assert k.max() <= 3 and t.sum() > 0
    assert demo_fa.parity(r, k, r, k, t, 0)["pass"]
    assert not demo_fa.parity(r, k, r + 1e-6, k, t, 0)["pass"]


class _Spot:
    def __init__(self): self.calls = []
    def ticker_price(self, s): return 2.0
    def round_qty(self, s, q, px): return round(q, 2)
    def test_order(self, **kw): self.calls.append(kw); return {}


class _Fut(_Spot):
    def exchange_info(self): return {"symbols": [{"symbol": "AUSDT", "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}, {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
    def mark_price(self, s): return 2.0


def test_ordering_check_validates_both_legs_and_probe():
    sp, fu = _Spot(), _Fut()
    r = demo_fa.ordering_check(sp, fu, {"held": [], "probe": "AUSDT", "K": 10, "decided_bar": "2026-10-22"})
    assert r["pass"] and r["orders"] == 2 and sp.calls[0]["side"] == "BUY" and fu.calls[0]["side"] == "SELL"
    assert abs(sp.calls[0]["quantity"] - 37.5) < 1e-9  # 1000/10/(4/3)=75U / 2


def test_reconcile_pause_uses_fa_slot():
    import demo_e
    st = {"demo_candidates": {demo_fa.SLOT: {"enabled": True, "daily": [{"date": f"2026-10-{d}", "demo": 1, "ledger": 1, "dev": 0} for d in range(26, 32)]}}}
    assert demo_e.reconcile(st, 1000, 1010, "2026-11-02", slot=demo_fa.SLOT)["paused"]
    assert demo_fa.SLOT in st["paused_slots"]
