"""All ledger tests use in-memory CAS or mocked HTTP; no live writes or AI."""
import copy
from datetime import datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import requests
import daily_ledger as dl

BASE = "a" * 40
DAY = "2026-11-18"
NOW = datetime.fromisoformat("2026-11-18T17:10:00+08:00")
ENV = {"GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "100",
       "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": BASE, "GITHUB_EVENT_NAME": "schedule"}
SNAPSHOT = {"updated": "2026-11-13 16:15", "a_shares": [{"代码": str(i)} for i in range(20)]}

class MemoryStore:
    def __init__(self):
        self.records = {}
        self.count = 0
        self.failure = None
        self.before_write = None
        self.api = Mock(return_value={"sha": BASE})

    def read(self, day):
        return copy.deepcopy(self.records.get(day, (None, None)))

    def write(self, day, expected, record, base):
        if self.before_write:
            callback, self.before_write = self.before_write, None
            callback()
        if self.failure == "before":
            raise dl.FenceError("storage failed")
        if self.records.get(day, (None, None))[0] != expected:
            raise dl.FenceError("atomic ref conflict")
        self.count += 1
        head = format(self.count, "040x")
        self.records[day] = (head, copy.deepcopy(record))
        if self.failure == "after":
            raise dl.FenceError("response lost after persisted")
        return head

class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.ledger = dl.Ledger(self.store, DAY, "100:1", BASE)

    def test_concurrent_first_writers_only_one_can_start(self):
        other = dl.Ledger(self.store, DAY, "101:1", BASE)
        self.store.before_write = lambda: other.claim("market_started")
        runner = Mock()
        with self.assertRaises(dl.FenceError):
            dl.execute_market(self.ledger, [["collector"]], runner)
        runner.assert_not_called()
        self.assertEqual(self.store.read(DAY)[1]["owner"], "101:1")

    def test_stale_sibling_update_cannot_advance(self):
        self.ledger.claim("market_started")
        old, record = self.store.read(DAY)
        self.ledger.transition("market_completed")
        with self.assertRaises(dl.FenceError):
            self.store.write(DAY, old, record, BASE)

    def test_attempt_is_durable_before_collector_and_ai(self):
        def market(*a, **kw):
            self.assertEqual(self.store.read(DAY)[1]["phase"], "market_started")
        dl.execute_market(self.ledger, [["collector"], ["audit"]], market)
        def ai(*a, **kw):
            r = self.store.read(DAY)[1]
            self.assertEqual(r["phase"], "ai_started")
            self.assertTrue(r["ai_ever_started"])
        dl.execute_ai(self.ledger, ai)
        self.assertEqual(self.store.read(DAY)[1]["phase"], "ai_completed")
        self.ledger.transition("publication_started")
        self.ledger.transition("published", {"commit": BASE})
        self.ledger.finish()
        with self.assertRaises(dl.FenceError):
            dl.Ledger(self.store, DAY, "102:1", BASE).claim("market_started")

    def test_unconfirmed_market_claim_never_collects(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure):
                store = MemoryStore()
                store.failure = failure
                runner = Mock()
                with self.assertRaises(dl.FenceError):
                    dl.execute_market(dl.Ledger(store, DAY, "100:1", BASE), [["collector"]], runner)
                runner.assert_not_called()
                if failure == "after":
                    store.failure = None
                    with self.assertRaises(dl.FenceError):
                        dl.Ledger(store, DAY, "101:1", BASE).claim("market_started")

    def test_unconfirmed_ai_attempt_never_calls_provider(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure):
                store = MemoryStore()
                ledger = dl.Ledger(store, DAY, "100:1", BASE)
                ledger.claim("report_reserved")
                store.failure = failure
                runner = Mock()
                with self.assertRaises(dl.FenceError):
                    dl.execute_ai(ledger, runner)
                runner.assert_not_called()
                store.failure = None
                with self.assertRaises(dl.FenceError):
                    dl.Ledger(store, DAY, "101:1", BASE).claim("report_reserved")

    def test_failures_and_runner_death_never_clear_attempt(self):
        self.ledger.claim("report_reserved")
        with self.assertRaises(RuntimeError):
            dl.execute_ai(self.ledger, Mock(side_effect=RuntimeError("provider unknown")))
        self.assertEqual(self.store.read(DAY)[1]["phase"], "ai_failed")
        self.assertTrue(self.store.read(DAY)[1]["ai_ever_started"])
        self.ledger.transition("publication_started")
        self.ledger.finish()  # push failed / interrupted
        self.assertEqual(self.store.read(DAY)[1]["phase"], "interrupted_or_unknown")
        with self.assertRaises(dl.FenceError):
            dl.Ledger(self.store, DAY, "101:1", BASE).claim("report_reserved")
        with self.assertRaises(dl.FenceError):
            self.ledger.transition("ai_started")

    def test_hard_kill_without_finalizer_still_blocks_next_run(self):
        self.ledger.claim("report_reserved")
        self.ledger.transition("ai_started")
        with self.assertRaises(dl.FenceError):
            dl.Ledger(self.store, DAY, "101:1", BASE).claim("report_reserved")

    def test_collection_failure_is_distinct_and_can_publish_diagnostics(self):
        with self.assertRaises(RuntimeError):
            dl.execute_market(self.ledger, [["collector"]], Mock(side_effect=RuntimeError("source")))
        record = self.store.read(DAY)[1]
        self.assertEqual(record["phase"], "market_failed")
        self.assertFalse(record["ai_ever_started"])
        self.ledger.transition("publication_started")
        self.ledger.transition("published", {"commit": BASE})
        self.assertIn("market_failed", [e["phase"] for e in self.store.read(DAY)[1]["events"]])

    def test_same_owner_concurrent_ai_entry_cannot_spend_twice(self):
        self.ledger.claim("report_reserved")
        other = dl.Ledger(self.store, DAY, "100:1", BASE)
        self.store.before_write = lambda: other.transition("ai_started")
        runner = Mock()
        with self.assertRaises(dl.FenceError):
            dl.execute_ai(self.ledger, runner)
        runner.assert_not_called()

    def test_new_date_does_not_reuse_or_reset_old_unknown_attempt(self):
        self.ledger.claim("report_reserved")
        self.ledger.transition("ai_started")
        dl.Ledger(self.store, "2026-11-19", "101:1", BASE).claim("market_started")
        self.assertEqual(self.store.read(DAY)[1]["phase"], "ai_started")

    def test_bootstrap_existing_success_is_durably_blocked(self):
        with self.assertRaises(dl.FenceError):
            self.ledger.claim("market_started", existing=True)
        self.assertEqual(self.store.read(DAY)[1]["phase"], "blocked_existing_result")

    def test_manual_recovery_is_append_only_with_exact_lease(self):
        self.ledger.claim("report_reserved")
        self.ledger.transition("ai_started")
        head, old = self.store.read(DAY)
        record = dl.record_recovery(self.store, DAY, head, "b" * 40)
        self.assertEqual(record["events"][:-1], old["events"])
        self.assertEqual(record["phase"], "ai_started")
        self.assertTrue(record["ai_ever_started"])
        with self.assertRaises(dl.FenceError):
            dl.record_recovery(self.store, DAY, head, "b" * 40)
        with self.assertRaises(dl.FenceError):
            dl.Ledger(self.store, DAY, "102:1", BASE).claim("report_reserved")

class ContextTests(unittest.TestCase):
    def test_late_events_weekends_reruns_non_main_fail_closed(self):
        for moment in ("2026-11-16T19:33:00+00:00", "2026-11-17T18:31:00+00:00",
                       "2026-11-18T18:00:00+00:00", "2026-11-21T17:10:00+08:00"):
            with self.assertRaises(dl.FenceError):
                dl.context(ENV, datetime.fromisoformat(moment), SNAPSHOT, {})
        for change in ({"GITHUB_RUN_ATTEMPT": "2"}, {"GITHUB_REF": "refs/heads/fix/test"},
                       {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_RUN_ID": ""}):
            with self.assertRaises(dl.FenceError):
                dl.context({**ENV, **change}, NOW, SNAPSHOT, {})

    def test_existing_and_historical_report_identity(self):
        self.assertFalse(dl.context(ENV, NOW, SNAPSHOT, {})[-1])
        self.assertTrue(dl.context(ENV, NOW, {**SNAPSHOT, "updated": DAY + " 17:05"}, {})[-1])
        env = {**ENV, "REPORT_ONLY": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"}
        with self.assertRaises(dl.FenceError):
            dl.context(env, NOW, SNAPSHOT, {})
        today = {**SNAPSHOT, "updated": DAY + " 17:05"}
        record = dl.context(env, NOW, today, {"report_snapshot": today["updated"],
                                            "research_generation_id": "old-id"})
        self.assertEqual(record[0], DAY)
        self.assertTrue(record[-1])
        with self.assertRaises(dl.FenceError):
            dl.context(env, NOW, {"updated": today["updated"], "a_shares": []}, {})

    def test_cross_midnight_stops_before_ai_or_publication(self):
        with patch.object(dl, "datetime") as clock:
            clock.now.return_value = datetime.fromisoformat("2026-11-19T00:00:00+08:00")
            with self.assertRaises(dl.FenceError):
                dl.window({"day": DAY, "report": False})

    def test_failed_acquisition_cannot_publish_but_late_diagnostics_can(self):
        with TemporaryDirectory() as tmp, patch.dict(os.environ, {"RUNNER_TEMP": tmp}):
            self.assertEqual(dl.main(["before-publish"]), 0)
            dl.local_file().with_suffix(".entered").write_text("attempted")
            with self.assertRaises(dl.FenceError):
                dl.main(["before-publish"])
            self.assertEqual(dl.main(["finish"]), 0)

    def test_cli_stale_checkout_stops_before_collection(self):
        import contextlib
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("docs").mkdir()
            Path("docs/data.json").write_text(json.dumps(SNAPSHOT))
            Path("docs/ai-status.json").write_text("{}")
            with patch.dict(os.environ, {**ENV, "RUNNER_TEMP": tmp}, clear=True), \
                 patch.object(dl, "GitStore") as factory, \
                 patch.object(dl, "datetime") as clock, \
                 patch.object(dl.subprocess, "run") as runner:
                clock.now.return_value = NOW
                factory.return_value.main_head.return_value = "b" * 40
                with self.assertRaises(dl.FenceError):
                    dl.main(["run-market"])
                runner.assert_not_called()
                factory.return_value.write.assert_not_called()

    def test_cli_ai_date_mismatch_does_not_advance_paid_fence(self):
        import contextlib
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path("docs").mkdir()
            Path("docs/data.json").write_text(json.dumps(SNAPSHOT))
            with patch.dict(os.environ, {**ENV, "RUNNER_TEMP": tmp}, clear=True), \
                 patch.object(dl, "GitStore") as factory, \
                 patch.object(dl, "datetime") as clock, \
                 patch.object(dl.subprocess, "run") as runner:
                clock.now.return_value = NOW
                dl.save_context(DAY, "100:1", BASE, False)
                with self.assertRaises(dl.FenceError):
                    dl.main(["run-ai"])
                runner.assert_not_called()
                factory.return_value.write.assert_not_called()

    def test_publication_unknown_is_not_recorded_as_success(self):
        import contextlib
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            with patch.dict(os.environ, {**ENV, "RUNNER_TEMP": tmp}, clear=True), \
                 patch.object(dl, "GitStore") as factory, \
                 patch.object(dl, "datetime") as clock, \
                 patch.object(dl.subprocess, "check_output", return_value=BASE):
                clock.now.return_value = NOW
                dl.save_context(DAY, "100:1", BASE, False)
                factory.return_value.main_head.return_value = "b" * 40
                with self.assertRaises(dl.FenceError):
                    dl.main(["published"])
                factory.return_value.write.assert_not_called()

    def test_manual_recovery_not_callable_by_workflow(self):
        with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(dl.FenceError):
                dl.main(["record-recovery", DAY, BASE, BASE])

class GitStoreTests(unittest.TestCase):
    def test_no_retry_redirect_or_secret_in_transport_error(self):
        session = Mock()
        session.request.side_effect = requests.Timeout("secret-token")
        store = dl.GitStore("owner/repo", "secret-token", session)
        with self.assertRaises(dl.FenceError) as caught:
            store.main_head()
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertEqual(session.request.call_count, 1)
        self.assertFalse(session.request.call_args.kwargs["allow_redirects"])

    def test_api_status_fail_closed_and_only_ref_read_allows_404(self):
        for status in (301, 403, 409, 422, 500):
            session = Mock()
            session.request.return_value.status_code = status
            store = dl.GitStore("owner/repo", "token", session)
            with self.assertRaises(dl.FenceError):
                store.read(DAY)

    def test_create_and_nonforced_update_require_readback(self):
        for expected in (None, BASE):
            store = dl.GitStore("owner/repo", "token", Mock())
            store.api = Mock(side_effect=[{"sha": "t" * 40}, {"sha": "b" * 40}, {}])
            record = {"phase": "ai_started"}
            store.read = Mock(return_value=("b" * 40, record))
            store.write(DAY, expected, record, BASE)
            call = store.api.call_args_list[-1]
            self.assertEqual(call.args[0], "POST" if expected is None else "PATCH")
            if expected is not None:
                self.assertEqual(call.args[2], {"sha": "b" * 40, "force": False})
            self.assertEqual(store.api.call_args_list[1].args[2]["parents"], [BASE])

    def test_readback_mismatch_does_not_grant_permission(self):
        store = dl.GitStore("owner/repo", "token", Mock())
        store.api = Mock(side_effect=[{"sha": "t" * 40}, {"sha": "b" * 40}, {}])
        store.read = Mock(return_value=("c" * 40, {}))
        with self.assertRaises(dl.FenceError):
            store.write(DAY, None, {"phase": "market_started"}, BASE)

class WiringTests(unittest.TestCase):
    def test_existing_daily_paths_use_fence_without_new_schedule(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/daily.yml").read_text()
        self.assertIn("run: python daily_ledger.py run-market", text)
        self.assertIn("run: python daily_ledger.py run-ai", text)
        self.assertNotIn("run: python ai_report.py", text)
        self.assertEqual(text.count("cron:"), 1)
        self.assertIn('cron: "10 9 * * 1-5"', text)
        self.assertIn("contents: write", text)
        self.assertLess(text.index("python daily_ledger.py before-publish"), text.index("git push"))
        self.assertLess(text.index("git push"), text.index("python daily_ledger.py published"))
        self.assertIn("if: ${{ always() }}", text)
        self.assertNotIn("record-recovery", text)
