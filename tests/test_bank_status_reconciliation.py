import hashlib
import json
import re
import unittest
from pathlib import Path
from bs4 import BeautifulSoup
from correct_bank_narrative_report import correct_page, reconcile_status_page

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'docs/history/2026-11-13'


class StatusReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.original = (ARCHIVE / 'ai-before-narrative-correction.html').read_text()
        self.current = (ARCHIVE / 'ai-before-narrative-status-reconciliation.html').read_text()
        self.prior = json.loads((ARCHIVE / 'bank-narrative-correction.json').read_text())
        self.audit = json.loads((ARCHIVE / 'bank-narrative-status-reconciliation.json').read_text())

    def test_both_hash_chains_replay_and_latest_alerts_survive(self):
        previous, _ = correct_page(self.original)
        output, audit = reconcile_status_page(self.original, self.current, self.prior)
        self.assertEqual(hashlib.sha256(previous.encode()).hexdigest(), self.prior['corrected_sha256'])
        self.assertEqual(hashlib.sha256(output.encode()).hexdigest(), self.audit['corrected_sha256'])
        for key in audit:
            if key != 'previous_correction':
                self.assertEqual(audit[key], self.audit[key])
        self.assertEqual(audit['removed_paragraphs'], self.prior['removed_paragraphs'])
        self.assertEqual(len(audit['removed_paragraphs']), 9)
        old, new = BeautifulSoup(self.current, 'html.parser'), BeautifulSoup(output, 'html.parser')
        for selector in ('meta', '.bank-evidence', 'blockquote', '.bank-ratio-correction',
                         '.market-publication-alert', '.research-publication-alert'):
            self.assertEqual(old.select(selector), new.select(selector))
        for name in ('market-publication-alert', 'research-publication-alert'):
            pattern = r'<p\b[^>]*class="' + name + r'"[^>]*>.*?</p>'
            self.assertEqual(re.findall(pattern, self.current, re.S), re.findall(pattern, output, re.S))
        self.assertIn('data-market-state="schedule_missed_window"', output)
        self.assertIn('data-research-state="review-required"', output)
        self.assertIn('href="./history/2026-11-13/bank-narrative-status-reconciliation.json"', output)

    def test_frozen_status_and_history_are_retained(self):
        status = json.loads((ARCHIVE / self.audit['source_status']).read_text())
        self.assertEqual(status['last_schedule_run_id'], '43940500630')
        self.assertEqual(status['last_schedule_observed_at'], '2026-11-17T03:33:32+08:00')
        self.assertEqual(status['schedule_state'], 'schedule_missed_window')
        self.assertEqual(status['state'], 'review-required')
        self.assertEqual(status['research_counts_status'], 'historical-unreviewed')
        self.assertEqual((status['research_approved'], status['research_limited']), (0, 5))
        self.assertEqual(status['snapshot'], '2026-11-13 16:15')
        self.assertEqual(status['research_generation_id'], self.prior['original_generation_id'])
        for path, expected in self.audit['preserved_files_sha256'].items():
            # Live docs can change in subsequent normal daily runs.
            if path.startswith('docs/history/'):
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected)
        self.assertEqual(hashlib.sha256((ARCHIVE / 'bank-narrative-correction.json').read_bytes()).hexdigest(),
                         self.audit['previous_correction']['audit_sha256'])

    def test_nonstatus_changes_or_unreplayable_prior_fail_closed(self):
        for changed in (self.current.replace('逾期贷款占比上升的披露', '别的正文'),
                        self.current.replace('market-publication-alert', 'unknown-alert'),
                        self.current.replace('7d2681103b17462da0236d64d1e54164', 'new-generation')):
            with self.assertRaises(ValueError):
                reconcile_status_page(self.original, changed, self.prior)
        for key in ('source_sha256', 'corrected_sha256', 'removed_paragraphs'):
            with self.assertRaises(ValueError):
                reconcile_status_page(self.original, self.current, {**self.prior, key: None})
