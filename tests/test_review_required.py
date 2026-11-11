"""Offline publication holds never masquerade as a new research audit."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import publish_guard as guard
from ai_report import stamp_generated_report

SNAPSHOT = {"updated": "2026-11-10 17:58"}


def report(approved=1, limited=7, total=10):
    return ('<!doctype html><html><title>研究档案</title>'
            f'<meta name="research-approved-count" content="{approved}">'
            f'<meta name="research-limited-count" content="{limited}">'
            f'<meta name="research-total-count" content="{total}">'
            '<h1>原报告</h1><small>行情快照：2026-11-10 17:58；'
            'AI 生成：2026-11-10 18:09（北京时间）</small>'
            '<article><p>原模型正文不得静默改写</p>'
            '<a href="https://www1.hkexnews.hk/original.pdf#page=3">D1 p3</a>'
            '<blockquote>原始PDF证据</blockquote></article></html>')


class ReviewHoldTests(unittest.TestCase):
    def freeze(self, text=None):
        return guard.research_freshness(SNAPSHOT, text or report(), True, "success",
                                        review_required=True)

    def evaluate(self, text, previous, outcome="success", **kwargs):
        return guard.research_freshness(SNAPSHOT, text, True, outcome,
                                        previous_status=previous, **kwargs)

    def test_explicit_hold_preserves_original_counts_and_missing_generation(self):
        result = self.freeze()
        self.assertEqual(result["state"], "review-required")
        self.assertIsNone(result["review_frozen_generation"])
        self.assertEqual((result["research_approved"], result["research_limited"], result["research_total"]), (1, 7, 10))
        self.assertEqual(result["research_counts_status"], "historical-unreviewed")
        self.assertIn("未经此次复审确认", result["description"])

    def test_current_generation_is_frozen_even_when_it_has_new_audit_markers(self):
        text = stamp_generated_report(report())
        result = self.freeze(text)
        self.assertTrue(result["review_frozen_generation"])
        self.assertEqual(result["review_frozen_generation"], result["research_generation_id"])
        self.assertEqual(self.evaluate(text, result)["state"], "review-required")

    def test_html_decoration_and_prior_success_cannot_clear_hold(self):
        old = stamp_generated_report(report())
        previous = self.freeze(old)
        decorated = guard.update_report_banner(old, previous) + '<!-- changed hash -->'
        previous["ai_step"] = "success"
        for outcome in ("skipped", "failure", "cancelled", ""):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.evaluate(decorated, previous, outcome)["state"], "review-required")
        self.assertEqual(self.evaluate(decorated, previous)["state"], "review-required")

    def test_ordinary_refreshes_do_not_hide_hold(self):
        previous = self.freeze()
        for market in (None, "pre_close", "weekend", "failed"):
            with self.subTest(market=market):
                state = self.evaluate(report(), previous, "skipped", market_state=market, ai_skipped=True)
                self.assertEqual(state["state"], "review-required")
                self.assertEqual(state["market_state"], market or "not_rechecked")

    def test_only_new_successful_audited_matching_generation_clears(self):
        old = stamp_generated_report(report())
        previous = self.freeze(old)
        new = stamp_generated_report(report())
        result = self.evaluate(new, previous)
        self.assertEqual(result["state"], "partial")
        self.assertFalse(result["review_required"])
        self.assertIsNone(result["review_frozen_generation"])
        self.assertNotEqual(result["research_generation_id"], previous["review_frozen_generation"])
        # An old report lacking generation markers can also be replaced.
        self.assertEqual(self.evaluate(new, self.freeze())["state"], "partial")

    def test_new_markers_without_current_run_success_stay_frozen(self):
        new = stamp_generated_report(report())
        for outcome in ("skipped", "failure", "cancelled", ""):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.evaluate(new, self.freeze(), outcome)["state"], "review-required")

    def test_new_generation_wrong_snapshot_or_version_stays_frozen(self):
        new = stamp_generated_report(report())
        for text in (new.replace("行情快照：2026-11-10 17:58", "行情快照：2026-11-09 17:58"),
                     new.replace("evidence-scope-v2", "evidence-scope-v1"),
                     new.replace('name="research-generation-id"', 'name="ignored-id"'),
                     new.replace('name="research-audit-version"', 'name="ignored-version"')):
            with self.subTest(text=text[:160]):
                self.assertEqual(self.evaluate(text, self.freeze())["state"], "review-required")

    def test_empty_or_duplicate_markers_do_not_clear(self):
        new = stamp_generated_report(report())
        for text in (new + '<meta name="research-generation-id" content="extra">',
                     new + '<meta name="research-audit-version" content="evidence-scope-v2">',
                     report() + '<meta name="research-generation-id" content=" ">'
                     '<meta name="research-audit-version" content="evidence-scope-v2">'):
            self.assertEqual(self.evaluate(text, self.freeze())["state"], "review-required")

    def test_only_canonical_uuid4_hex_generation_can_clear(self):
        import re
        new = stamp_generated_report(report())
        for generation in ("decoration", "0" * 32,
                           " 0123456789ab4def8123456789abcdef ",  # not canonical .hex
                           "0123456789ab1def8123456789abcdef",  # UUID v1
                           "0123456789ab4def7123456789abcdef",  # invalid variant
                           "ABCDEF0123454ABC8DEF0123456789AB",  # noncanonical uppercase
                           "01234567-89ab-4def-8123-456789abcdef",  # hyphenated, not .hex
                           "0123456789ab4def8123456789abcde",  # short
                           "0123456789ab4def8123456789abcdef0"):  # long
            with self.subTest(generation=generation):
                altered = re.sub(r'(<meta name="research-generation-id" content=")[^"]*(">)',
                                 lambda m: m.group(1) + generation + m.group(2), new)
                self.assertEqual(self.evaluate(altered, self.freeze())["state"], "review-required")
        self.assertEqual(self.evaluate(new, self.freeze())["state"], "partial")

    def test_invalid_or_missing_counts_do_not_clear(self):
        new = stamp_generated_report(report())
        invalid = [new.replace('name="research-total-count" content="10"',
                               'name="research-total-count" content="9"'),
                   new.replace('name="research-approved-count" content="1"',
                               'name="research-approved-count" content="-1"'),
                   new.replace('name="research-limited-count" content="7"',
                               'name="research-limited-count" content="-2"'),
                   new.replace('name="research-approved-count" content="1"',
                               'name="research-approved-count" content="4"'),
                   new.replace('<meta name="research-limited-count" content="7">', ''),
                   new + '<meta name="research-total-count" content="10">']
        for text in invalid:
            with self.subTest(text=text[:220]):
                self.assertEqual(self.evaluate(text, self.freeze())["state"], "review-required")

    def test_explicit_refreeze_always_wins_and_records_latest_generation(self):
        new = stamp_generated_report(report())
        result = self.evaluate(new, self.freeze(), review_required=True)
        self.assertEqual(result["state"], "review-required")
        self.assertEqual(result["review_frozen_generation"], result["research_generation_id"])

    def test_normal_non_hold_behavior_is_unchanged(self):
        self.assertEqual(guard.research_freshness(SNAPSHOT, report(), True, "success")["state"], "partial")
        self.assertEqual(guard.research_freshness(SNAPSHOT, report(10, 0), True, "success")["state"], "current")

    def test_main_persists_hold_without_reusing_stored_success_or_mutating_report(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = report()
            data = json.dumps(SNAPSHOT, ensure_ascii=False)
            (root / "data.json").write_text(data)
            (root / "index.html").write_text('<h1>原榜单</h1><p class="ai-nav">旧状态</p>')
            (root / "ai.html").write_text(raw)
            (root / "ai-status.json").write_text(json.dumps({"ai_step": "success", "audit_note": "preserve"}))
            env = {"AI_ENABLED": "true", "RESEARCH_REVIEW_REQUIRED": "true"}
            with patch.object(guard, "DOCS", root), patch.dict("os.environ", env, clear=True):
                self.assertEqual(guard.main(), 0)
            first = json.loads((root / "ai-status.json").read_text())
            self.assertEqual(first["state"], "review-required")
            self.assertEqual(first["ai_step"], "skipped")
            self.assertEqual(first["audit_note"], "preserve")
            self.assertEqual(guard.MARKET_BANNER.sub("", guard.BANNER.sub("", (root / "ai.html").read_text())), raw)
            self.assertEqual((root / "data.json").read_text(), data)
            self.assertIn('data-report-state="review-required"', (root / "index.html").read_text())
            # Ordinary skip + banner rewrite cannot unlock or duplicate banner.
            with patch.object(guard, "DOCS", root), patch.dict("os.environ", {"AI_ENABLED": "true", "SKIP_AI": "true"}, clear=True):
                guard.main()
            self.assertEqual(json.loads((root / "ai-status.json").read_text())["state"], "review-required")
            self.assertEqual(len(guard.BANNER.findall((root / "ai.html").read_text())), 1)
            # Fresh pipeline marker alone still cannot lift without current success.
            generated = stamp_generated_report(raw)
            (root / "ai.html").write_text(generated)
            with patch.object(guard, "DOCS", root), patch.dict("os.environ", {"AI_ENABLED": "true"}, clear=True):
                guard.main()
            self.assertEqual(json.loads((root / "ai-status.json").read_text())["state"], "review-required")
            with patch.object(guard, "DOCS", root), patch.dict("os.environ", {"AI_ENABLED": "true", "AI_JOB_STATUS": "success"}, clear=True):
                guard.main()
            last = json.loads((root / "ai-status.json").read_text())
            self.assertEqual(last["state"], "partial")
            self.assertEqual(last["audit_note"], "preserve")
            self.assertEqual(guard.MARKET_BANNER.sub("", guard.BANNER.sub("", (root / "ai.html").read_text())), generated)
            self.assertEqual((root / "data.json").read_text(), data)

    def test_corrupt_prior_state_is_not_silently_discarded(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.json").write_text(json.dumps(SNAPSHOT))
            (root / "index.html").write_text('<p class="ai-nav">keep</p>')
            (root / "ai.html").write_text(report())
            (root / "ai-status.json").write_text('{invalid')
            with patch.object(guard, "DOCS", root), self.assertRaises(ValueError):
                guard.main()
            self.assertEqual((root / "index.html").read_text(), '<p class="ai-nav">keep</p>')


if __name__ == "__main__":
    unittest.main()
