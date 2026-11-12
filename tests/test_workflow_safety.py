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

    def test_probe_uses_shared_budget_production_path(self):
        source = (ROOT / ".github/workflows/source-validation.yml").read_text()
        self.assertIn('python -u production_probe.py', source)
        self.assertNotIn('fetch_em_market(', source)


if __name__ == "__main__":
    unittest.main()
