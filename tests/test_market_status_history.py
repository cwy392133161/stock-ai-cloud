"""An overnight skipped run must not hide an unresolved quote failure."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import publish_guard as guard


OLD = "2026-11-10 17:58"
NEW = "2026-11-12 17:30"
REPORT = '<h1>原报告</h1><small>行情快照：' + OLD + '</small><article>原正文</article>'


class MarketStatusHistoryTests(unittest.TestCase):
    def prior(self):
        return {"snapshot": OLD, "market_state": "failed", "review_required": True,
                "state": "review-required", "last_screening_outcome": "failure"}

    def status(self, gate, outcome="skipped", snapshot=OLD, previous=None):
        return guard.research_freshness(
            {"updated": snapshot}, REPORT, True, "skipped", market_state=gate,
            screening_outcome=outcome, previous_status=previous or self.prior())

    def test_skip_weekend_and_report_only_do_not_clear_failure(self):
        for gate in ("pre_close", "weekend", None):
            with self.subTest(gate=gate):
                result = self.status(gate)
                self.assertEqual(result["market_state"], "failed")
                self.assertEqual(result["market_check_state"], gate or "not_rechecked")
                self.assertEqual(result["last_screening_outcome"], "failure")
                self.assertEqual(result["state"], "review-required")
                self.assertIn("旧行情", result["market_description"])
                self.assertIn("不能视作", result["market_description"])

    def test_legacy_failure_without_new_fields_is_preserved(self):
        previous = {"snapshot": OLD, "market_state": "failed"}
        result = self.status("pre_close", previous=previous)
        self.assertEqual(result["state"], "market-unavailable")
        self.assertEqual(result["market_state"], "failed")
        self.assertEqual(result["last_screening_outcome"], "failure")

    def test_report_only_success_does_not_claim_ai_was_not_generated(self):
        result = guard.research_freshness(
            {"updated": OLD}, REPORT, True, "success",
            previous_status={"snapshot": OLD, "market_state": "failed"})
        self.assertEqual(result["market_state"], "failed")
        self.assertEqual(result["ai_step"], "success")
        self.assertNotIn("未对其重新生成", result["description"])
        self.assertIn("研究报告是否更新须另看", result["description"])

    def test_only_successful_screening_with_newer_snapshot_clears(self):
        for outcome, snapshot in (("skipped", NEW), ("failure", NEW),
                                  ("success", OLD), ("success", "2026-11-09 17:30"),
                                  ("success", "invalid")):
            with self.subTest(outcome=outcome, snapshot=snapshot):
                self.assertEqual(self.status(None, outcome, snapshot)["market_state"], "failed")
        result = self.status(None, "success", NEW)
        self.assertEqual(result["market_state"], "updated")
        self.assertEqual(result["last_screening_outcome"], "success")
        self.assertEqual(result["state"], "review-required")
        skipped = self.status("pre_close", previous=result, snapshot=NEW)
        self.assertEqual(skipped["market_state"], "pre_close")

    def test_missing_previous_snapshot_cannot_clear_known_failure(self):
        previous = {"market_state": "failed"}
        self.assertEqual(self.status(None, "success", NEW, previous)["market_state"], "failed")

    def test_regressed_success_cannot_lower_failure_recovery_boundary(self):
        regressed = self.status(None, "success", "2026-11-09 17:30")
        self.assertEqual(regressed["market_failure_snapshot"], OLD)
        repeated = self.status(None, "success", OLD, regressed)
        self.assertEqual(repeated["market_state"], "failed")
        self.assertEqual(repeated["market_failure_snapshot"], OLD)
        self.assertEqual(self.status(None, "success", NEW, repeated)["market_state"], "updated")

    def test_actual_failure_wins_over_preclose_flags(self):
        with self.fixture() as root:
            self.run_main(root, {"SCREENING_OUTCOME": "failure"})
            result = json.loads((root / "ai-status.json").read_text())
            self.assertEqual(result["market_state"], "failed")
            self.assertEqual(result["market_check_state"], "failed")
            self.assertEqual(result["last_screening_outcome"], "failure")

    def test_failure_and_review_hold_visible_on_both_pages_idempotently(self):
        result = self.status("pre_close")
        index = '<h1>原榜单</h1><p class="ai-nav">旧链接</p><table>原行情</table>'
        first_index = guard.update_index(index, result, True)
        first_report = guard.update_report_banner(REPORT, result)
        for text in (first_index, first_report):
            self.assertIn('data-market-state="failed"', text)
            self.assertIn("最近一次行情更新失败", text)
            self.assertIn("尚未完成复审", text)
            self.assertEqual(len(guard.MARKET_BANNER.findall(text)), 1)
        self.assertEqual(guard.update_index(first_index, result, True), first_index)
        self.assertEqual(guard.update_report_banner(first_report, result), first_report)
        self.assertEqual(guard.MARKET_BANNER.sub("", guard.BANNER.sub("", first_report)), REPORT)
        self.assertIn('<table>原行情</table>', first_index)

    def test_missing_report_still_displays_market_warning(self):
        text = guard.update_index('<h1>榜单</h1><p class="ai-nav">旧</p>', self.status(None), False)
        self.assertIn("行情更新失败", text)
        self.assertIn("AI研究页尚不存在", text)

    def test_new_market_banner_cannot_change_old_report_snapshot(self):
        status = self.status(None, "success", NEW)
        decorated = guard.update_report_banner(REPORT, status)
        result = guard.research_freshness({"updated": NEW}, decorated, True, "success")
        self.assertEqual(result["report_snapshot"], OLD)
        self.assertEqual(result["state"], "stale")
        # Defensive compatibility: even old/decorative labels matching the
        # report regex must never be read as the report's original timestamp.
        altered = decorated.replace("榜单数据时点：", "实际行情快照：")
        self.assertEqual(guard.research_freshness({"updated": NEW}, altered, True, "success")
                         ["report_snapshot"], OLD)

    def test_main_skip_sequence_preserves_failure_timestamp_and_raw_quotes(self):
        with self.fixture() as root:
            original = (root / "data.json").read_bytes()
            previous = {**self.prior(), "last_screening_recorded_utc": "2026-11-11T14:25:57+00:00"}
            (root / "ai-status.json").write_text(json.dumps(previous))
            for env in ({}, {"TRADING_WEEKDAY": "false"},
                        {"REPORT_ONLY": "true", "AI_JOB_STATUS": "success"}, {}):
                self.run_main(root, env)
                result = json.loads((root / "ai-status.json").read_text())
                self.assertEqual(result["market_state"], "failed")
                self.assertEqual(result["last_screening_recorded_utc"], previous["last_screening_recorded_utc"])
                self.assertEqual(result["state"], "review-required")
                self.assertEqual((root / "data.json").read_bytes(), original)
                for file in ("index.html", "ai.html"):
                    self.assertEqual(len(guard.MARKET_BANNER.findall((root / file).read_text())), 1)

    def fixture(self):
        from contextlib import contextmanager
        @contextmanager
        def files():
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "data.json").write_text(json.dumps({"updated": OLD, "a_shares": [{"代码": "000001"}]}))
                (root / "index.html").write_text('<h1>旧榜</h1><p class="ai-nav">旧</p>')
                (root / "ai.html").write_text(REPORT)
                yield root
        return files()

    def run_main(self, root, extra):
        env = {"REPORT_ONLY": "false", "TRADING_WEEKDAY": "true", "MARKET_READY": "false",
               "SCREENING_OUTCOME": "skipped", "AI_JOB_STATUS": "skipped", **extra}
        with patch.object(guard, "DOCS", root), patch.dict("os.environ", env, clear=True):
            self.assertEqual(guard.main(), 0)


if __name__ == "__main__":
    unittest.main()
