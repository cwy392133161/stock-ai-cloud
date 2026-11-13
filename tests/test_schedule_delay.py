"""Replay late scheduled starts without sources, secrets, or AI calls."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import market_schedule
import publish_guard

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))


class ScheduleDecisionTests(unittest.TestCase):
    def test_recorded_production_delays_fail_closed(self):
        for value in ("2026-11-11T17:05:21+00:00", "2026-11-12T17:34:04+00:00"):
            decision = market_schedule.scheduling_decision(datetime.fromisoformat(value), "schedule", "1")
            self.assertEqual(decision["schedule_state"], "schedule_missed_window")
            self.assertTrue(decision["trading_weekday"])
            self.assertFalse(decision["market_ready"])

    def test_normal_same_day_and_midnight_boundaries(self):
        for hour, minute, state in ((16, 59, "schedule_missed_window"),
                                    (17, 0, "schedule_ready"),
                                    (17, 10, "schedule_ready"),
                                    (23, 59, "schedule_ready"),
                                    (0, 0, "schedule_missed_window")):
            with self.subTest(hour=hour, minute=minute):
                d = market_schedule.scheduling_decision(datetime(2026, 11, 13, hour, minute, tzinfo=CST), "schedule", "1")
                self.assertEqual(d["schedule_state"], state)
                self.assertEqual(d["market_ready"], state == "schedule_ready")

    def test_weekend_late_runs_do_not_backdate_to_friday(self):
        for day in (14, 15):
            for hour in (1, 18):
                d = market_schedule.scheduling_decision(datetime(2026, 11, day, hour, tzinfo=CST), "schedule", "1")
                self.assertEqual(d["schedule_state"], "schedule_missed_window")
                self.assertFalse(d["market_ready"])

    def test_reruns_are_blocked_even_during_new_days_close_window(self):
        for attempt in ("2", "3", "100"):
            for day in (13, 16):
                d = market_schedule.scheduling_decision(datetime(2026, 11, day, 18, tzinfo=CST), "schedule", attempt)
                self.assertEqual(d["schedule_state"], "schedule_repeat_blocked")
                self.assertFalse(d["market_ready"])

    def test_bad_attempt_is_fail_closed(self):
        for attempt in ("", "0", "-1", "1.0", " 1", "true", "１", "1\n", "1" * 5000, None, 1):
            d = market_schedule.scheduling_decision(datetime(2026, 11, 13, 18, tzinfo=CST), "schedule", attempt)
            self.assertEqual(d["schedule_state"], "schedule_invalid_context")
            self.assertFalse(d["market_ready"])

    def test_manual_behavior_is_unchanged(self):
        for event in ("workflow_dispatch", "", "push"):
            for hour, ready in ((1, False), (16, False), (17, True), (23, True)):
                d = market_schedule.scheduling_decision(datetime(2026, 11, 13, hour, tzinfo=CST), event, "2")
                self.assertEqual(d["schedule_state"], "not_scheduled")
                self.assertEqual(d["market_ready"], ready)

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            market_schedule.scheduling_decision(datetime(2026, 11, 13, 18), "schedule", "1")

    def test_cli_exports_guard_and_actual_observation_only(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "outputs"
            output = io.StringIO()
            with patch.object(market_schedule, "datetime") as clock, patch.dict(os.environ, {
                "SCHEDULE_EVENT_NAME": "schedule", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_OUTPUT": str(path)
            }, clear=True), contextlib.redirect_stdout(output):
                clock.now.return_value = datetime(2026, 11, 13, 18, tzinfo=CST)
                self.assertEqual(market_schedule.main(), 0)
            values = dict(line.split("=", 1) for line in path.read_text().splitlines())
            self.assertEqual(values["market_ready"], "false")
            self.assertEqual(values["schedule_state"], "schedule_repeat_blocked")
            self.assertEqual(values["schedule_observed_at"], "2026-11-13T18:00:00+08:00")
            self.assertNotIn("尚未到17:00", output.getvalue())


class SchedulePublicationTests(unittest.TestCase):
    def fixture(self, root, prior_failure=False, frozen=False):
        (root / "data.json").write_text(json.dumps({"updated": "2026-11-10 17:58", "a_shares": [{"代码": "600001"}]}))
        (root / "index.html").write_text('<h1>榜</h1><p class="ai-nav">旧</p>')
        (root / "ai.html").write_text('<h1>研究</h1><small>行情快照：2026-11-10 17:58</small>')
        old = {"snapshot": "2026-11-10 17:58", "last_screening_recorded_utc": "2026-11-11T14:25:57Z"}
        if prior_failure:
            old.update(market_state="failed", last_screening_outcome="failure", market_failure_snapshot="2026-11-10 17:58")
        if frozen:
            old.update(state="review-required", review_required=True)
        (root / "ai-status.json").write_text(json.dumps(old))

    def run_guard(self, root, state, event="schedule", extra=None):
        env = {"SCHEDULE_EVENT_NAME": event, "GITHUB_RUN_ID": "43270737555", "GITHUB_RUN_ATTEMPT": "1",
               "SCHEDULE_STATE": state, "SCHEDULE_OBSERVED_AT": "2026-11-13T01:34:04+08:00",
               "TRADING_WEEKDAY": "true", "MARKET_READY": "false", "SCREENING_OUTCOME": "skipped",
               "AI_JOB_STATUS": "skipped", "AI_ENABLED": "true", "REPORT_ONLY": "false"}
        env.update(extra or {})
        with patch.object(publish_guard, "DOCS", root), patch.dict(os.environ, env, clear=True):
            self.assertEqual(publish_guard.main(), 0)
        return json.loads((root / "ai-status.json").read_text())

    def test_missed_schedule_not_reported_as_ai_or_quote_source_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            before = (root / "data.json").read_bytes()
            status = self.run_guard(root, "schedule_missed_window")
            self.assertEqual(status["state"], "market-schedule-blocked")
            self.assertEqual(status["market_state"], "schedule_missed_window")
            self.assertEqual(status["ai_step"], "skipped")
            self.assertEqual(status["last_schedule_run_id"], "43270737555")
            self.assertEqual(status["last_schedule_observed_at"], "2026-11-13T01:34:04+08:00")
            self.assertEqual((root / "data.json").read_bytes(), before)
            for path in (root / "index.html", root / "ai.html"):
                text = path.read_text()
                self.assertIn("不能把次日凌晨当成原交易日补跑", text)
                self.assertIn("2026-11-10 17:58", text)

    def test_previous_outage_review_hold_and_raw_report_date_survive_repeated_decoration(self):
        for reason in market_schedule.SCHEDULE_BLOCK_REASONS:
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root, prior_failure=True, frozen=True)
                before = (root / "data.json").read_bytes()
                for _ in range(2):
                    status = self.run_guard(root, reason)
                self.assertEqual(status["state"], "review-required")
                self.assertEqual(status["research_counts_status"], "historical-unreviewed")
                self.assertEqual(status["market_state"], "failed")
                self.assertEqual(status["market_check_state"], reason)
                self.assertEqual(status["last_screening_outcome"], "failure")
                self.assertEqual(status["last_screening_recorded_utc"], "2026-11-11T14:25:57Z")
                self.assertEqual(status["report_snapshot"], "2026-11-10 17:58")
                self.assertEqual((root / "data.json").read_bytes(), before)
                for path in (root / "index.html", root / "ai.html"):
                    text = path.read_text()
                    self.assertIn("最近一次行情更新失败", text)
                    self.assertEqual(text.count('class="market-publication-alert"'), 1)

    def test_invalid_schedule_output_is_not_treated_as_ready(self):
        for state in ("", "unexpected", "not_scheduled"):
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                status = self.run_guard(root, state)
                self.assertEqual(status["schedule_state"], "schedule_invalid_context")

    def test_manual_preclose_ignores_injected_schedule_state_and_keeps_last_schedule_record(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.run_guard(root, "schedule_missed_window")
            status = self.run_guard(root, "schedule_missed_window", event="workflow_dispatch")
            self.assertEqual(status["schedule_state"], "not_scheduled")
            self.assertEqual(status["last_schedule_state"], "schedule_missed_window")
            self.assertEqual(status["market_check_state"], "pre_close")

    def test_workflow_fails_only_after_publishing_diagnostics_and_does_not_add_triggers(self):
        text = (ROOT / ".github/workflows/daily.yml").read_text()
        self.assertEqual(text.count('cron:'), 1)
        self.assertIn('cron: "10 9 * * 1-5"', text)
        self.assertNotIn("repository_dispatch:", text)
        self.assertNotIn("workflow_run:", text)
        self.assertIn("SCHEDULE_STATE: ${{ steps.trading_day.outputs.schedule_state }}", text)
        self.assertEqual(text.count("SCHEDULE_EVENT_NAME: ${{ github.event_name }}"), 2)
        self.assertLess(text.index("git push"), text.index('if status.get("schedule_state") in SCHEDULE_BLOCK_REASONS:'))
        self.assertEqual(text.count("steps.trading_day.outputs.market_ready == 'true'"), 1)
        self.assertEqual(text.count("inputs.report_only || steps.screening.outcome == 'success'"), 2)

    def test_actual_final_workflow_check_marks_missed_or_repeated_run_red(self):
        text = (ROOT / ".github/workflows/daily.yml").read_text()
        # Execute only the final local status check, never any workflow action.
        inline = text.rsplit("python - <<'PY'", 1)[1].split("          PY", 1)[0]
        code = "\n".join(line[10:] if line.startswith("          ") else line for line in inline.splitlines())
        for reason in (*market_schedule.SCHEDULE_BLOCK_REASONS, "schedule_ready", "not_scheduled"):
            with self.subTest(reason=reason), TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "docs").mkdir()
                (root / "docs/ai-status.json").write_text(json.dumps({"schedule_state": reason, "market_state": "not_rechecked"}))
                with contextlib.chdir(root):
                    if reason in market_schedule.SCHEDULE_BLOCK_REASONS:
                        with self.assertRaises(SystemExit) as caught:
                            exec(compile(code, "workflow-status-check", "exec"), {})
                        self.assertEqual(caught.exception.code, 1)
                    else:
                        exec(compile(code, "workflow-status-check", "exec"), {})


if __name__ == "__main__":
    unittest.main()
