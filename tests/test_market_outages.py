"""Offline regression tests for stale market data and Beijing weekend handling."""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

import market_schedule
import publish_guard


class MarketOutageTests(unittest.TestCase):
    def test_weekend_is_beijing_local_not_utc(self):
        # UTC Friday 17:30 is already Saturday 01:30 in Beijing.
        self.assertFalse(market_schedule.is_trading_weekday(
            datetime(2026, 11, 6, 17, 30, tzinfo=timezone.utc)))
        self.assertFalse(market_schedule.is_trading_weekday(
            datetime(2026, 11, 7, 15, 30, tzinfo=timezone.utc)))
        self.assertTrue(market_schedule.is_trading_weekday(
            datetime(2026, 11, 9, 9, 30, tzinfo=timezone.utc)))
        with self.assertRaises(ValueError):
            market_schedule.is_trading_weekday(datetime(2026, 11, 7, 9, 0))

    def test_no_new_quote_or_ai_report_during_weekend(self):
        snapshot = {"updated": "2026-11-06 23:25"}
        report = '<small>行情快照：2026-11-06 23:25</small>'
        state = publish_guard.research_freshness(
            snapshot, report, True, "skipped", market_state="weekend")
        self.assertEqual(state["state"], "market-weekend")
        self.assertEqual(state["snapshot"], "2026-11-06 23:25")
        self.assertIn("未重新选股", state["description"])

    def test_weekday_provider_outage_preserves_prior_snapshot(self):
        snapshot = {"updated": "2026-11-06 23:25"}
        report = '<small>行情快照：2026-11-06 23:25</small>'
        state = publish_guard.research_freshness(
            snapshot, report, True, "skipped", market_state="failed")
        self.assertEqual(state["state"], "market-unavailable")
        self.assertIn("旧快照", state["description"])
        index = '<h1>旧选股榜</h1><p class="ai-nav"><a href="./ai.html">旧报告</a></p>'
        updated = publish_guard.update_index(index, state, True)
        self.assertIn('data-report-state="market-unavailable"', updated)
        self.assertEqual(updated.count("旧选股榜"), 1)
        self.assertIn("2026-11-06 23:25", updated)

    def test_report_only_can_research_existing_snapshot(self):
        state = publish_guard.research_freshness(
            {"updated": "2026-11-06 23:25"},
            '<small>行情快照：2026-11-06 23:25</small>'
            '<meta name="research-approved-count" content="10">'
            '<meta name="research-total-count" content="10">',
            True, "success", market_state=None)
        self.assertEqual(state["state"], "current")

    def test_main_publishes_status_without_overwriting_market_data(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {"updated": "2026-11-06 23:25", "a_shares": [{"代码": "000690"}]}
            (root / "data.json").write_text(json.dumps(data), encoding="utf-8")
            (root / "index.html").write_text(
                '<h1>旧有效选股榜</h1><p class="ai-nav">旧AI链接</p>', encoding="utf-8")
            (root / "ai.html").write_text(
                '<h1>旧报告</h1><small>行情快照：2026-11-06 23:25</small>', encoding="utf-8")
            original = (root / "data.json").read_bytes()
            with patch.object(publish_guard, "DOCS", root), patch.dict(
                "os.environ", {"AI_ENABLED":"true", "AI_JOB_STATUS":"skipped",
                               "REPORT_ONLY":"false", "SCREENING_OUTCOME":"failure",
                               "TRADING_WEEKDAY":"true"}
            ):
                self.assertEqual(publish_guard.main(), 0)
            self.assertEqual(original, (root / "data.json").read_bytes())
            self.assertIn("旧有效选股榜", (root / "index.html").read_text(encoding="utf-8"))
            status = json.loads((root / "ai-status.json").read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "market-unavailable")
            self.assertIn('data-research-state="market-unavailable"',
                          (root / "ai.html").read_text(encoding="utf-8"))
            self.assertEqual((root / "index.html").read_text(encoding="utf-8").count("market-unavailable"), 1)


if __name__ == "__main__":
    unittest.main()
