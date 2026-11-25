"""Audited skip_ai handoff and safe diagnostics. No network/provider calls."""
import contextlib
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import requests
import daily_ledger as dl
from test_daily_ledger import MemoryStore, BASE, DAY, NOW, ENV, SNAPSHOT

NEW_BASE = 'b' * 40
TODAY = {**SNAPSHOT, 'updated': DAY + ' 17:05'}
STATUS = {'snapshot': TODAY['updated'], 'screening_outcome': 'success',
          'last_screening_outcome': 'success', 'market_state': 'updated', 'ai_step': 'skipped',
          'report_snapshot': '2026-11-13 16:15', 'research_generation_id': 'old-generation',
          'review_required': True, 'state': 'review-required'}

def hashed(data):
    return hashlib.sha256(dl.encode(data).encode()).hexdigest()

class FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)

def published_skip(store, skip=True):
    ledger = dl.Ledger(store, DAY, '100:1', BASE)
    ledger.claim('market_started', skip_ai=skip)
    ledger.transition('market_completed')
    details = {'snapshot_updated': TODAY['updated'], 'files_sha256': {
        'docs/data.json': hashed(TODAY), 'docs/ai-status.json': hashed(STATUS)}}
    ledger.transition('publication_started', details)
    ledger.transition('published', {**details, 'commit': NEW_BASE})
    return ledger

class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.old = published_skip(self.store)
        self.ledger = dl.Ledger(self.store, DAY, '101:1', NEW_BASE)

    def claim(self, ledger=None, snapshot=None, status=None, now=NOW):
        snapshot = TODAY if snapshot is None else snapshot
        status = STATUS if status is None else status
        return (ledger or self.ledger).claim_report(snapshot, hashed(snapshot), status, hashed(status), now)

    def test_same_day_skip_ai_can_complete_report_once_without_rewriting_history(self):
        head, original = self.store.read(DAY)
        result = self.claim()
        self.assertEqual(result['events'][:-1], original['events'])
        self.assertEqual(result['owner'], '101:1')
        self.assertEqual(result['base'], NEW_BASE)
        self.assertTrue(result['ai_claimed_once'])
        self.assertFalse(result['ai_ever_started'])
        self.assertEqual(result['events'][-1]['details']['previous_ledger_head'], head)
        def provider(*args, **kwargs):
            self.assertTrue(self.store.read(DAY)[1]['ai_ever_started'])
        dl.execute_ai(self.ledger, provider)
        self.ledger.transition('publication_started')
        self.ledger.transition('published', {'commit': 'c' * 40})
        for owner in ('101:1', '102:1'):
            with self.assertRaises(dl.FenceError):
                self.claim(dl.Ledger(self.store, DAY, owner, NEW_BASE))

    def test_two_contenders_have_exactly_one_owner_and_one_possible_ai_call(self):
        winner = dl.Ledger(self.store, DAY, '102:1', NEW_BASE)
        self.store.before_write = lambda: self.claim(winner)
        with self.assertRaises(dl.FenceError):
            self.claim()
        calls = Mock()
        with self.assertRaises(dl.FenceError):
            dl.execute_ai(self.ledger, calls)
        calls.assert_not_called()
        dl.execute_ai(winner, calls)
        self.assertEqual(calls.call_count, 1)

    def test_old_collector_owner_cannot_spend_or_mutate_after_handoff(self):
        self.claim()
        with self.assertRaises(dl.FenceError):
            self.old.transition('ai_started')
        with self.assertRaises(dl.FenceError):
            self.old.finish()

    def test_crash_after_claim_before_ai_is_not_a_reclaimable_lease(self):
        self.claim()
        # Hard kill: no finalizer. Paid start is false, but claim remains occupied.
        self.assertFalse(self.store.read(DAY)[1]['ai_ever_started'])
        with self.assertRaises(dl.FenceError):
            self.claim(dl.Ledger(self.store, DAY, '102:1', NEW_BASE))

    def test_crash_after_ai_start_or_failed_provider_keeps_permanent_fence(self):
        for explicit_failure in (False, True):
            with self.subTest(failure=explicit_failure):
                store = MemoryStore()
                published_skip(store)
                ledger = dl.Ledger(store, DAY, '101:1', NEW_BASE)
                ledger.claim_report(TODAY, hashed(TODAY), STATUS, hashed(STATUS), NOW)
                if explicit_failure:
                    with self.assertRaises(RuntimeError):
                        dl.execute_ai(ledger, Mock(side_effect=RuntimeError('provider result unknown')))
                else:
                    ledger.transition('ai_started')
                with self.assertRaises(dl.FenceError):
                    dl.Ledger(store, DAY, '102:1', NEW_BASE).claim_report(TODAY, hashed(TODAY), STATUS, hashed(STATUS), NOW)
                self.assertTrue(store.read(DAY)[1]['ai_ever_started'])

    def test_storage_failure_or_lost_ack_never_grants_ai_permission(self):
        for failure in ('before', 'after'):
            with self.subTest(failure=failure):
                store = MemoryStore()
                published_skip(store)
                ledger = dl.Ledger(store, DAY, '101:1', NEW_BASE)
                store.failure = failure
                with self.assertRaises(dl.FenceError):
                    ledger.claim_report(TODAY, hashed(TODAY), STATUS, hashed(STATUS), NOW)
                self.assertFalse(store.read(DAY)[1]['ai_ever_started'])
                if failure == 'after':
                    store.failure = None
                    with self.assertRaises(dl.FenceError):
                        dl.Ledger(store, DAY, '102:1', NEW_BASE).claim_report(TODAY, hashed(TODAY), STATUS, hashed(STATUS), NOW)

    def test_old_different_future_incomplete_and_out_of_window_snapshots_are_rejected(self):
        for snapshot in (SNAPSHOT, {**TODAY, 'updated': '2026-11-19 17:05'},
                         {**TODAY, 'updated': DAY + ' 17:11'}, {**TODAY, 'a_shares': []}):
            with self.subTest(snapshot=snapshot['updated']), self.assertRaises(dl.FenceError):
                self.claim(snapshot=snapshot)
        for moment in ('2026-11-18T16:59:00+08:00', '2026-11-19T17:10:00+08:00',
                       '2026-11-19T00:00:00+08:00'):
            with self.subTest(now=moment), self.assertRaises(dl.FenceError):
                self.claim(now=datetime.fromisoformat(moment))
        self.assertEqual(self.store.read(DAY)[1]['phase'], 'published')

    def test_same_timestamp_different_data_or_status_hash_is_rejected(self):
        for snapshot, status in (({**TODAY, 'extra': 'changed'}, STATUS),
                                 (TODAY, {**STATUS, 'extra': 'changed'})):
            with self.assertRaises(dl.FenceError):
                self.claim(snapshot=snapshot, status=status)

    def test_missing_legacy_or_failed_proof_and_existing_ai_are_rejected(self):
        head, baseline = self.store.read(DAY)
        alterations = []
        for key, value in (('skip_ai', False), ('skip_ai', None), ('origin_kind', 'report'),
                           ('ai_ever_started', True), ('ai_claimed_once', True),
                           ('phase', 'publication_started'), ('phase', 'interrupted_or_unknown')):
            r = copy.deepcopy(baseline); r[key] = value; alterations.append(r)
        r = copy.deepcopy(baseline); del r['ai_claimed_once']; alterations.append(r)
        for phase in ('market_failed', 'ai_started', 'manual_recovery_reference'):
            r = copy.deepcopy(baseline); r['events'].insert(2, {'phase': phase}); alterations.append(r)
        for key in ('snapshot_updated', 'commit'):
            r = copy.deepcopy(baseline); r['events'][-1]['details'].pop(key); alterations.append(r)
        r = copy.deepcopy(baseline)
        r['events'][-2]['details']['files_sha256']['docs/data.json'] = '0' * 64
        alterations.append(r)
        for r in alterations:
            self.store.records[DAY] = (head, r)
            with self.assertRaises(dl.FenceError):
                self.claim()
        self.store.records.clear()
        with self.assertRaises(dl.FenceError):
            self.claim()

    def test_report_only_without_ledger_cannot_bootstrap_paid_request(self):
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path('docs').mkdir()
            Path('docs/data.json').write_text(dl.encode(TODAY))
            Path('docs/ai-status.json').write_text(dl.encode(STATUS))
            store = MemoryStore(); store.main_head = Mock(return_value=NEW_BASE)
            env = {**ENV, 'REPORT_ONLY': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_RUN_ID': '101', 'GITHUB_SHA': NEW_BASE, 'RUNNER_TEMP': tmp}
            with patch.dict(os.environ, env, clear=True), patch.object(dl, 'datetime', FrozenClock), \
                    patch.object(dl, 'GitStore', return_value=store), patch.object(dl.subprocess, 'run') as run:
                with self.assertRaises(dl.FenceError): dl.main(['run-ai'])
                run.assert_not_called()
                self.assertFalse(dl.local_file().exists())
                self.assertEqual(store.records, {})

    def test_cli_handoff_and_snapshot_change_after_claim(self):
        for tamper in (False, True):
            with self.subTest(tamper=tamper), TemporaryDirectory() as tmp, contextlib.chdir(tmp):
                Path('docs').mkdir()
                Path('docs/data.json').write_text(dl.encode(TODAY))
                Path('docs/ai-status.json').write_text(dl.encode(STATUS))
                store = MemoryStore(); published_skip(store)
                store.main_head = Mock(return_value=NEW_BASE)
                env = {**ENV, 'REPORT_ONLY': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                       'GITHUB_RUN_ID': '101', 'GITHUB_SHA': NEW_BASE, 'RUNNER_TEMP': tmp}
                real_save = dl.save_context
                def saving(*args):
                    real_save(*args)
                    if tamper: Path('docs/data.json').write_text(dl.encode({**TODAY, 'extra': 1}))
                with patch.dict(os.environ, env, clear=True), patch.object(dl, 'datetime', FrozenClock), \
                        patch.object(dl, 'GitStore', return_value=store), patch.object(dl, 'save_context', side_effect=saving), \
                        patch.object(dl.subprocess, 'run') as run:
                    if tamper:
                        with self.assertRaises(dl.FenceError): dl.main(['run-ai'])
                        run.assert_not_called()
                    else:
                        self.assertEqual(dl.main(['run-ai']), 0)
                        self.assertEqual(run.call_count, 1)
                    with self.assertRaises(dl.FenceError):
                        dl.Ledger(store, DAY, '102:1', NEW_BASE).claim_report(TODAY, hashed(TODAY), STATUS, hashed(STATUS), NOW)

    def test_cli_skip_ai_publishes_then_new_report_only_run_completes(self):
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path('docs').mkdir()
            Path('docs/data.json').write_text(dl.encode(SNAPSHOT))
            Path('docs/ai-status.json').write_text('{}')
            Path('collector-temp').mkdir(); Path('report-temp').mkdir()
            store = MemoryStore(); store.main_head = Mock(return_value=BASE)
            env = {**ENV, 'SKIP_AI': 'true', 'RUNNER_TEMP': str(Path('collector-temp').resolve())}
            def collect(command, **kwargs):
                if command[-1] == 'picker.py':
                    Path('docs/data.json').write_text(dl.encode(TODAY))
            with patch.dict(os.environ, env, clear=True), patch.object(dl, 'datetime', FrozenClock), \
                    patch.object(dl, 'GitStore', return_value=store), \
                    patch.object(dl.subprocess, 'run', side_effect=collect) as commands, \
                    patch.object(dl.subprocess, 'check_output', return_value=NEW_BASE):
                dl.main(['run-market'])
                self.assertEqual(commands.call_count, 3)
                Path('docs/ai-status.json').write_text(dl.encode(STATUS))
                dl.main(['before-publish'])
                store.main_head.return_value = NEW_BASE
                dl.main(['published'])
                dl.main(['finish'])
            initial = copy.deepcopy(store.read(DAY)[1])
            report_env = {**ENV, 'REPORT_ONLY': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                          'GITHUB_RUN_ID': '101', 'GITHUB_SHA': NEW_BASE,
                          'RUNNER_TEMP': str(Path('report-temp').resolve())}
            with patch.dict(os.environ, report_env, clear=True), patch.object(dl, 'datetime', FrozenClock), \
                    patch.object(dl, 'GitStore', return_value=store), patch.object(dl.subprocess, 'run') as run:
                dl.main(['run-ai'])
                run.assert_called_once()
                self.assertEqual(store.read(DAY)[1]['events'][:4], initial['events'])
                self.assertTrue(store.read(DAY)[1]['ai_claimed_once'])
                with self.assertRaises(dl.FenceError): dl.main(['run-ai'])
                run.assert_called_once()

    def test_cli_crash_saving_local_context_after_transfer_cannot_reclaim(self):
        with TemporaryDirectory() as tmp, contextlib.chdir(tmp):
            Path('docs').mkdir()
            Path('docs/data.json').write_text(dl.encode(TODAY))
            Path('docs/ai-status.json').write_text(dl.encode(STATUS))
            self.store.main_head = Mock(return_value=NEW_BASE)
            env = {**ENV, 'REPORT_ONLY': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                   'GITHUB_RUN_ID': '101', 'GITHUB_SHA': NEW_BASE, 'RUNNER_TEMP': tmp}
            with patch.dict(os.environ, env, clear=True), patch.object(dl, 'datetime', FrozenClock), \
                    patch.object(dl, 'GitStore', return_value=self.store), \
                    patch.object(dl, 'save_context', side_effect=OSError('disk failed')), \
                    patch.object(dl.subprocess, 'run') as run:
                with self.assertRaises(OSError): dl.main(['run-ai'])
                run.assert_not_called()
            with self.assertRaises(dl.FenceError):
                self.claim(dl.Ledger(self.store, DAY, '102:1', NEW_BASE))

class DiagnosticTests(unittest.TestCase):
    def test_permission_conflict_rate_limit_and_uncertain_writes_have_safe_categories(self):
        for status, code in ((401, 'permission_or_policy_denied'), (403, 'permission_or_policy_denied'),
                             (409, 'conflict_or_validation'), (422, 'conflict_or_validation'),
                             (429, 'rate_limited'), (302, 'redirect_rejected'), (503, 'write_uncertain')):
            session = Mock(); session.request.return_value.status_code = status
            session.request.return_value.text = 'SENSITIVE_BODY secret-token'
            store = dl.GitStore('owner/repo', 'secret-token', session)
            with self.assertRaises(dl.FenceError) as caught: store.api('PATCH', '/git/refs/heads/test', {})
            text = dl.diagnostic(caught.exception)
            self.assertIn('code=' + code, text)
            self.assertIn('operation=update-reference', text)
            self.assertIn('http=' + str(status), text)
            self.assertNotIn('secret-token', text)
            self.assertNotIn('SENSITIVE_BODY', text)
            self.assertNotIn('Authorization', text)

    def test_transport_read_write_and_readback_failures_are_distinct(self):
        for method, code in (('GET', 'read_uncertain'), ('POST', 'write_uncertain')):
            session = Mock(); session.request.side_effect = requests.Timeout('secret-token')
            with self.assertRaises(dl.FenceError) as caught:
                dl.GitStore('owner/repo', 'secret-token', session).api(method, '/git/trees')
            self.assertIn('code=' + code, dl.diagnostic(caught.exception))
            self.assertNotIn('secret-token', dl.diagnostic(caught.exception))
        store = dl.GitStore('owner/repo', 'secret-token', Mock())
        store.api = Mock(side_effect=[{'sha': 'a' * 40}, {'sha': 'b' * 40}, {}])
        store.read = Mock(side_effect=dl.FenceError('secret-token', 'read_uncertain'))
        with self.assertRaises(dl.FenceError) as caught:
            store.write(DAY, BASE, {'phase': 'report_reserved'}, BASE)
        self.assertEqual(dl.diagnostic(caught.exception), 'code=readback_uncertain operation=confirm-write')

    def test_error_text_and_untrusted_diagnostic_fields_never_echo(self):
        for error in (dl.FenceError('secret-token'), dl.FenceError('x', 'secret-token', 'secret-token'),
                      OSError('secret-token'), KeyError('secret-token')):
            self.assertNotIn('secret-token', dl.diagnostic(error))
        store = dl.GitStore('owner/repo', 'secret-token', Mock())
        store.api = Mock(side_effect=[{'sha': 'a' * 40}, {'sha': 'b' * 40}, {}])
        store.read = Mock(return_value=('c' * 40, {}))
        with self.assertRaises(dl.FenceError) as caught:
            store.write(DAY, BASE, {'phase': 'report_reserved'}, BASE)
        self.assertIn('code=readback_mismatch', dl.diagnostic(caught.exception))
