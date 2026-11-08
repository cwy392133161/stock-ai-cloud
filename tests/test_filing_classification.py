"""Regressions for Power Assets dividend misclassification and search promotion."""
import unittest
from datetime import date
from unittest.mock import patch
from disclosures import fetch_disclosures
from period_reports import filing_kind, period_title_score


class FilingClassificationTests(unittest.TestCase):
    def test_dividend_is_never_a_financial_report(self):
        for title in (
            "截至2026年6月30日止6個月的中期股息",
            "Interim dividend for the six months ended 30 June 2026",
            "2026年中期報告之寄發通知",
            "董事會會議通告 - 2026年中期業績",
            "Notification of publication of 2026 interim report",
            "2026年度报告摘要", "2026年中期業績盈利警告",
            "关于2026年半年度报告的补充公告",
            "2026年中期報告之澄清公告",
            "2026年半年度报告问询函的回复",
        ):
            with self.subTest(title=title):
                self.assertEqual(period_title_score(title, "2026-06-30", "港股"), 0)

    def test_explicit_date_cannot_promote_results_to_full_report(self):
        title = "截至2026年6月30日止六個月中期業績及股息公告"
        self.assertEqual(filing_kind(title), "results")
        self.assertEqual(filing_kind("2026 Interim Results and Condensed Consolidated Financial Statements"), "results")
        self.assertLess(period_title_score(title, "2026-06-30", "港股"), 100)
        self.assertGreaterEqual(period_title_score("2026年中期報告", "2026-06-30", "港股"), 100)

    @patch("disclosures._market_rows")
    def test_keep_searching_after_dated_results_for_actual_report(self, rows):
        rows.side_effect = [[],
            [(date(2026, 8, 12), "截至2026年6月30日止六個月中期業績公告", "/results.pdf")],
            [(date(2026, 9, 15), "2026年中期報告", "/report.pdf")]]
        evidence = fetch_disclosures("港股", "00006", "2026-11-06", "2026-06-30")
        self.assertTrue(evidence["对应报告期财报"]["链接"].endswith("/report.pdf"))
        self.assertEqual(evidence["对应报告期财报"]["文档类型"], "full_report")
        self.assertEqual(evidence["近期公告数量"], 0)
        self.assertEqual(evidence["历史财报数量"], 1)

    @patch("disclosures._market_rows")
    def test_results_only_not_tagged_as_full_report(self, rows):
        rows.return_value = [(date(2026, 8, 12), "截至2026年6月30日止六個月中期業績公告", "/results.pdf")]
        evidence = fetch_disclosures("港股", "00006", "2026-11-06", "2026-06-30")
        self.assertIn("尚未取得完整财报", evidence["对应报告期财报"]["状态"])
        self.assertFalse(any(n.get("报告期匹配") for n in evidence["公告"]))

    @patch("disclosures._market_rows")
    def test_real_dividend_title_cannot_mark_period_disclosed(self, rows):
        rows.return_value = [(date(2026, 8, 12), "截至2026年6月30日止6個月的中期股息", "/dividend.pdf")]
        evidence = fetch_disclosures("港股", "00006", "2026-11-06", "2026-06-30")
        self.assertNotIn("链接", evidence["对应报告期财报"])
        self.assertFalse(any(n.get("报告期匹配") for n in evidence["公告"]))

class StoredEvidenceTests(unittest.TestCase):
    def evidence(self, title):
        return {"对应报告期财报": {"报告期": "2026-06-30", "标题": title,
                    "公告日期": "2026-08-12"},
                "公告": [{"报告期匹配": "2026-06-30", "标题": title,
                    "日期": "2026-08-12", "正文摘录": [{"页": 1, "摘录": "股息",
                        "单位": None, "表头": "", "期间上下文": " "}]}]}

    def test_old_dividend_match_does_not_raise_quality_or_prove_publication(self):
        from research_quality import evidence_quality
        from research_brief import build_research_brief
        e = self.evidence("截至2026年6月30日止6個月的中期股息")
        f = {"报告期": "2026-06-30", "状态": "可用", "指标": {"收入": "1 HKD"},
             "披露日期": "接口未提供", "历史财务": {"可比同季度年度数": 3}}
        quality = evidence_quality(f, e, {"PE动态": 8}, "2026-11-06")
        self.assertEqual(quality["分级"], "资料不足")
        self.assertEqual(quality["同报告期PDF可读页段"], 0)
        brief = build_research_brief({}, f, e, "2026-11-06")
        self.assertIn("未取得可核对片段", " ".join(brief["证据覆盖"]))

    def test_blank_table_fields_do_not_count_as_usable(self):
        from research_quality import evidence_quality
        e = self.evidence("2026年中期報告")
        f = {"报告期": "2026-06-30", "状态": "可用", "指标": {"收入": "1 HKD"},
             "披露日期": "2026-08-12", "历史财务": {"可比同季度年度数": 3}}
        quality = evidence_quality(f, e, {"PE动态": 8}, "2026-11-06")
        self.assertEqual(quality["同报告期PDF可读页段"], 1)
        self.assertEqual(quality["表头单位期间齐备片段"], 0)
        self.assertNotEqual(quality["分级"], "扩展证据研究")
