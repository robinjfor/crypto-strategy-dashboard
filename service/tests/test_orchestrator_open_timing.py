"""11/11 08:05 Taipei plan was right; the 11/11 bar had already been claimed on the previous plan.

Hourly reconcile at 00:01 UTC and the E5/FA jobs at 00:01/00:02 start reconcile before the
00:05 orchestrator write. Those ticks must not claim the bar. No network, no orders.
"""
from datetime import datetime, timezone

import demo_r2s
import orchestrator as O
import reconcile as R


NOV11_0002 = datetime(2026, 11, 11, 0, 2, tzinfo=timezone.utc)   # Taipei 08:02, before the plan
NOV11_0008 = datetime(2026, 11, 11, 0, 8, tzinfo=timezone.utc)   # Taipei 08:08, after the plan
OLD_PLAN = {"generated_at": "2026-11-10T00:05:00+00:00",
            "targets": {"demo_r2s_top10_3k": 1464.08, "demo_e5_min5_x175_t05top20": 5856.32}}
NEW_PLAN = {"generated_at": "2026-11-11T00:05:12+00:00", "sizing": "quote_pool",
            "targets": {"demo_r2s_top10_3k": 2359.17, "demo_e5_min5_x175_t05top20": 5504.74},
            "quote_pool": {"demo_r2s_top10_3k": 7863.91, "demo_e5_min5_x175_t05top20": 7863.91}}


class _Store:
    def __init__(self, s):
        self.s = s
        self.mutated = False

    def load(self):
        return self.s

    def mutate(self, fn):
        self.mutated = True
        self.s = fn(self.s)
        return self.s


def test_observed_1464_is_the_previous_plan_not_the_new_target_or_the_ledger():
    """11/11 R2S orders were sized at 1464.08. The 08:05 quote_pool target cannot produce that number."""
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOV11_0008, plan_doc=NEW_PLAN) == 2359.17
    assert O.sizing_capital("demo_e5_min5_x175_t05top20", 5200.0, now=NOV11_0008, plan_doc=NEW_PLAN) == 5504.74
    # previous plan, no quote_pool flag: min(target, ledger) == the old target
    assert O.sizing_capital("demo_r2s_top10_3k", 3000.0, now=NOV11_0002, plan_doc=OLD_PLAN) == 1464.08
    # a ledger of 1464.08 under the NEW plan still sizes at 2359.17 (quote_pool is not capped at the book)
    assert O.sizing_capital("demo_r2s_top10_3k", 1464.08, now=NOV11_0008, plan_doc=NEW_PLAN) == 2359.17


def test_unchanged_r2s_weight_holds_at_1464_and_scales_at_2359():
    """XRP/SOL/DOGE staying put is the 25% band at the old equity. At 2359 every such coin would resize."""
    price = 10.0
    weight = 0.1
    qty = weight * 1464.08 / price
    held_kind = demo_r2s.decide_minnot(weight, qty, price, 1464.08, 5.0, 0.0)[1]
    new_kind = demo_r2s.decide_minnot(weight, qty, price, 2359.17, 5.0, 0.0)[1]
    assert held_kind == "hold"
    assert new_kind == "order"
    # sign change and a brand-new coin trade at whatever equity was loaded; they do not prove the new target
    assert demo_r2s.decide_minnot(0.0, qty, price, 1464.08, 5.0, 0.0)[1] == "close"
    assert demo_r2s.decide_minnot(-0.05, 0.0, price, 1464.08, 5.0, 0.0)[1] == "order"


def test_plan_from_yesterday_does_not_cover_todays_open():
    ok, why = O.plan_covers_open(NOV11_0002, OLD_PLAN)
    assert ok is False and "2026-11-10" in why
    ok, why = O.plan_covers_open(NOV11_0008, NEW_PLAN)
    assert ok is True and why == ""
    ok, _ = O.plan_covers_open(NOV11_0002, None)
    assert ok is False


def test_early_tick_does_not_claim_and_the_0008_tick_can(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_ENABLED", raising=False)
    st = _Store({"demo_r2s_exec": {"last_bar": "2026-11-09", "cash": 3000.0, "book": {"XRPUSDT": 1.0}}})
    O.load({"orchestrator": OLD_PLAN})
    why = R.claim_daily_bar(st, "demo_r2s_exec", st.load(), "2026-11-10", "early", now=NOV11_0002)
    assert why and "waiting" in why
    assert st.mutated is False
    assert st.s["demo_r2s_exec"]["last_bar"] == "2026-11-09"
    assert "claim" not in st.s["demo_r2s_exec"]

    O.load({"orchestrator": NEW_PLAN})
    assert R.claim_daily_bar(st, "demo_r2s_exec", st.load(), "2026-11-10", "on-time", now=NOV11_0008) is None
    assert st.s["demo_r2s_exec"]["claim"]["bar"] == "2026-11-10"
    assert st.s["demo_r2s_exec"]["claim"]["run"] == "on-time"
    assert st.s["demo_r2s_exec"]["last_bar"] == "2026-11-09"  # claim does not mark the bar executed


def test_disabled_orchestrator_does_not_wait(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_ENABLED", "0")
    assert O.plan_covers_open(NOV11_0002, None) == (True, "")
