"""The live source check must never publish or invoke paid AI."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowSafetyTests(unittest.TestCase):
    def test_source_validation_is_read_only(self):
        source = (ROOT / ".github/workflows/source-validation.yml").read_text()
        self.assertIn("contents: read", source)
        for forbidden in ("ai_report.py", "secrets.", "git push", "contents: write"):
            self.assertNotIn(forbidden, source)

    def test_manual_skip_ai_guards_both_optional_steps(self):
        daily = (ROOT / ".github/workflows/daily.yml").read_text()
        self.assertIn("skip_ai:", daily)
        self.assertEqual(daily.count("!inputs.skip_ai && vars.AI_ENABLED"), 2)
        self.assertIn('os.getenv("SKIP_AI") == "true"', daily)

    def test_failed_generation_is_recovered_and_recorded_before_report_only_fails(self):
        daily = (ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8")
        steps = daily.split("      - name: ")
        ai = next(step for step in steps if "id: deep_research" in step)
        self.assertIn("continue-on-error: true", ai)
        failure_condition = "inputs.report_only && steps.deep_research.outcome != 'success'"
        report_failure = next(step for step in steps if failure_condition in step)
        self.assertIn("exit 1", report_failure)
        markers = (
            "steps.screening.outcome == 'failure'",
            "git restore -- docs/index.html docs/data.json",
            "id: deep_research",
            "steps.deep_research.outcome == 'failure'",
            "git restore -- docs/ai.html",
            "AI_JOB_STATUS: ${{ steps.deep_research.outcome }}",
            "run: python publish_guard.py",
            "git push",
            failure_condition,
        )
        positions = [daily.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))

    def test_ranking_and_manual_report_workflows_only_require_a_shares(self):
        source = (ROOT / ".github/workflows/source-validation.yml").read_text()
        self.assertNotIn("score_hk", source)
        daily = (ROOT / ".github/workflows/daily.yml").read_text(encoding="utf-8")
        self.assertIn('snapshot["a_shares"]', daily)
        self.assertNotIn("hk_shares", daily)


if __name__ == "__main__":
    unittest.main()
