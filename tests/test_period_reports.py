"""Offline tests: reporting-period match is not equivalent to a recent title."""
import unittest
from datetime import date
from unittest.mock import patch

from disclosures import fetch_disclosures
from period_reports import filing_search_windows, normalize_title, period_title_score


class PeriodReportTests(unittest.TestCase):
    def test_mainland_quarter_matches_without_confusing_year(self):
        self.assertGreater(period_title_score("2026年第三季度报告", "2026-09-30", "A股"), 0)
        self.assertEqual(period_title_score("2026年半年度报告", "2026-09-30", "A股"), 0)
        self.assertEqual(period_title_score("2025年第三季度报告", "2026-09-30", "A股"), 0)
        self.assertEqual(period_title_score("2026年第三季度报告摘要", "2026-09-30", "A股"), 0)
        self.assertEqual(period_title_score("2026年第三季度业绩预告", "2026-09-30", "A股"), 0)

    def test_hk_exact_fiscal_year_in_chinese_and_english(self):
        self.assertIn("2026年3月31日", normalize_title("二零二六年三月三十一日"))
        cn = "截至二零二六年三月三十一日止年度之全年業績公告"
        en = "Annual results for the year ended 31 March 2026"
        self.assertGreater(period_title_score(cn, "2026-03-31", "港股"), 0)
        self.assertGreater(period_title_score(en, "2026-03-31", "港股"), 0)
        self.assertEqual(period_title_score(en, "2025-03-31", "港股"), 0)
        self.assertEqual(period_title_score("2026年度業績公告", "2026-03-31", "港股"), 0)

    def test_period_date_mismatch_rejects_same_type(self):
        self.assertEqual(
            period_title_score("截至2026年6月30日中期報告", "2026-09-30", "港股"), 0
        )
        self.assertGreater(
            period_title_score("2026年中期報告", "2026-06-30", "港股"), 0
        )

    def test_search_windows_bounded_to_snapshot_date(self):
        windows = filing_search_windows("2026-06-30", "2026-11-06", width=55)
        self.assertEqual(windows[0][0], date(2026, 6, 30))
        self.assertEqual(windows[-1][1], date(2026, 11, 6))
        for left, right in zip(windows, windows[1:]):
            self.assertEqual((right[0] - left[1]).days, 1)
        self.assertEqual(filing_search_windows("2024-01-01", "2026-11-06"), [])

    @patch("disclosures._market_rows")
    def test_report_fetched_beyond_recent_30_days_without_adding_titles(self, rows):
        rows.side_effect = [
            [(date(2026, 11, 5), "一般股东公告", "/recent.pdf")],
            [(date(2026, 8, 4), "2026年半年度报告", "/financial.pdf")],
        ]
        result = fetch_disclosures("A股", "000690", "2026-11-06", report_period="2026-06-30")
        self.assertEqual(result["对应报告期财报"]["状态"], "标题与报告期匹配，PDF正文待核验")
        self.assertEqual(result["公告"][0]["报告期匹配"], "2026-06-30")
        self.assertTrue(result["公告"][0]["链接"].endswith("/financial.pdf"))
        self.assertEqual(len(result["公告"]), 2)
        self.assertEqual(rows.call_count, 2)

    @patch("disclosures._market_rows")
    def test_incorrect_period_does_not_make_up_report(self, rows):
        rows.side_effect = [[(date(2026, 11, 5), "一般事项", "/notice.pdf")]] + [
            [(date(2026, 8, 4), "2025年半年度报告", "/old.pdf")] for _ in range(3)]
        result = fetch_disclosures("A股", "000690", "2026-11-06", report_period="2026-06-30")
        self.assertFalse(any("报告期匹配" in x for x in result["公告"]))
        self.assertIn("未找到", result["对应报告期财报"]["状态"])


if __name__ == "__main__":
    unittest.main()
