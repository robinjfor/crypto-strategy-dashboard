"""Home desk copy: Demo, Paper, and Real stay separate, and stale data is named."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import review_bridge as B

HOME = Path(__file__).resolve().parents[2] / "home.js"


def test_home_desk_phrases():
    text = HOME.read_text(encoding="utf-8")
    assert "data-tab=\"demo\"" in text
    assert "data-tab=\"paper\"" in text
    assert "data-tab=\"real\"" in text
    assert "尚未開通" in text
    assert "not yet enabled" not in text
    assert "尚未啟用" not in text
    assert "不計入 Demo 淨值" in text
    assert "目前回撤" in text and "最大回撤" in text
    assert "資料過期" in text
    assert "stale data" not in text
    assert "缺值" in text
    assert "未分配" in text
    assert "差額不自動抹平" in text
    assert "未經 income 對到" in text or "unallocated_usdt_warning" in text
    assert "/review/bridge" in text


def test_data_age_turns_yellow_after_two_hours_and_red_after_twenty_six():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    yellow = B.data_age_min((now - timedelta(hours=3)).isoformat(), now)
    stale = B.data_age_min((now - timedelta(hours=27)).isoformat(), now)
    fresh = B.data_age_min((now - timedelta(minutes=30)).isoformat(), now)
    assert yellow["level"] == "yellow"
    assert stale["level"] == "stale"
    assert fresh["level"] == "ok"
