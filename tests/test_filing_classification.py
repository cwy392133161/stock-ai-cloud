"""Regressions for Power Assets dividend misclassification and search promotion."""
import unittest
from datetime import date
from unittest.mock import patch
from disclosures import fetch_disclosures
from period_reports import filing_kind, period_title_score, matched_report_notices


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
        return {"来源": "港交所披露易", "对应报告期财报": {"报告期": "2026-06-30", "标题": title,
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


class HKQuarterReportRegressionTests(unittest.TestCase):
    def test_real_citic_third_quarter_titles_are_full_reports(self):
        for title in ("中信銀行2026年第三季度報告（中國會計准則）",
                      "2026年第三季度報告", "中信银行2026年第三季度报告（中国会计准则）"):
            with self.subTest(title=title):
                self.assertEqual(filing_kind(title), "full_report")
                self.assertEqual(period_title_score(title, "2026-09-30", "港股"), 100)

    def test_real_bocom_quarter_results_are_not_full_reports(self):
        for title in ("2026年三季度業績公告", "2026年三季度业绩公告"):
            with self.subTest(title=title):
                self.assertEqual(filing_kind(title), "results")
                self.assertEqual(period_title_score(title, "2026-09-30", "港股"), 40)

    def test_first_quarter_traditional_simplified_and_english(self):
        for title in ("2026年第一季度報告", "2026年第一季度报告", "2026 Q1 Report"):
            with self.subTest(title=title):
                # Generic English 'Report' is not itself a full-report type;
                # use the unambiguous quarterly-report wording below instead.
                if title == "2026 Q1 Report":
                    self.assertEqual(period_title_score(title, "2026-03-31", "港股"), 0)
                else:
                    self.assertEqual(period_title_score(title, "2026-03-31", "港股"), 100)
        self.assertEqual(period_title_score("2026 Q1 Quarterly Report", "2026-03-31", "港股"), 100)
        self.assertEqual(period_title_score("2026 Q3 Results", "2026-09-30", "港股"), 40)

    def test_wrong_year_quarter_missing_year_and_nonstandard_ends_rejected(self):
        for title, period in (
            ("2025年第三季度報告", "2026-09-30"),
            ("2026年第一季度報告", "2026-09-30"),
            ("2026年第三季度報告", "2026-03-31"),
            ("第三季度報告", "2026-09-30"),
            ("2026年季度報告", "2026-09-30"),
            ("2026年第三季度報告", "2026-09-29"),
            ("2026年第一季度報告", "2026-03-30"),
            ("2026年第三季度報告", "2026-08-31"),
            ("截至2026年8月31日止第三季度報告", "2026-09-30"),
            ("2026年第一季度及第三季度報告", "2026-09-30"),
        ):
            with self.subTest(title=title, period=period):
                self.assertEqual(period_title_score(title, period, "港股"), 0)
        # An explicitly dated non-calendar fiscal quarter remains supported.
        self.assertEqual(period_title_score("截至2026年8月31日止第三季度報告",
                                           "2026-08-31", "港股"), 130)

    def test_quarter_dividends_summaries_notices_and_forecasts_stay_excluded(self):
        for title in ("2026年第三季度股息公告", "2026年三季度業績預告",
                      "2026年第三季度報告摘要", "2026年第三季度報告之寄發通知",
                      "董事會會議通告 - 2026年三季度業績", "2026年第三季度報告補充公告"):
            with self.subTest(title=title):
                self.assertEqual(period_title_score(title, "2026-09-30", "港股"), 0)

    @patch("disclosures._market_rows")
    def test_citic_full_report_is_reserved_and_revalidated_downstream(self, rows):
        title = "中信銀行2026年第三季度報告（中國會計准則）"
        rows.return_value = [(date(2026, 10, 29), title, "/citic-q3.pdf")]
        result = fetch_disclosures("港股", "00998", "2026-11-10", "2026-09-30")
        self.assertEqual(result["对应报告期财报"]["文档类型"], "full_report")
        self.assertIn("PDF正文待核验", result["对应报告期财报"]["状态"])
        self.assertEqual(result["公告"][0]["报告期匹配"], "2026-09-30")
        self.assertEqual(len(matched_report_notices(result, "2026-09-30")), 1)
        self.assertEqual(matched_report_notices(result, "2026-03-31"), [])

    @patch("disclosures._market_rows")
    def test_bocom_results_are_found_without_full_report_promotion(self, rows):
        title = "2026年三季度業績公告"
        rows.return_value = [(date(2026, 10, 29), title, "/bocom-q3.pdf")]
        result = fetch_disclosures("港股", "03328", "2026-11-10", "2026-09-30")
        report = result["对应报告期财报"]
        self.assertEqual(report["文档类型"], "results")
        self.assertIn("尚未取得完整财报", report["状态"])
        self.assertTrue(report["链接"].endswith("/bocom-q3.pdf"))
        self.assertFalse(any(n.get("报告期匹配") for n in result["公告"]))
        self.assertEqual(matched_report_notices(result, "2026-09-30"), [])
        # Even a stale/wrong stored full-report tag cannot promote results.
        result["公告"][0]["报告期匹配"] = "2026-09-30"
        self.assertEqual(matched_report_notices(result, "2026-09-30"), [])


class HKQuarterYearAmbiguityTests(unittest.TestCase):
    def test_revision_year_does_not_become_fiscal_year(self):
        for title in ("2025年第三季度報告（2026年修訂版）",
                      "2025年第三季度报告（2026年修订版）",
                      "2026年第三季度報告（2027年修訂版）",
                      "第三季度報告（2026年修訂版）"):
            with self.subTest(title=title):
                self.assertEqual(period_title_score(title, "2026-09-30", "港股"), 0)

    def test_split_fiscal_year_requires_explicit_ending_date(self):
        for title in ("2026/27年第一季度報告", "2026／27年第一季度報告",
                      "2026-27年第一季度报告", "2026至27年第一季度報告",
                      "2026/2027年第一季度報告", "2026–27 First Quarter Results"):
            with self.subTest(title=title):
                self.assertEqual(period_title_score(title, "2026-03-31", "港股"), 0)
        self.assertEqual(period_title_score(
            "2026/27年第一季度報告（截至2026年6月30日止）", "2026-06-30", "港股"), 130)

    def test_clear_quarter_year_still_matches(self):
        for title in ("中信銀行2026年第三季度報告（中國會計准則）",
                      "二零二六年第三季度報告", "2026 Q3 Quarterly Report"):
            with self.subTest(title=title):
                self.assertEqual(period_title_score(title, "2026-09-30", "港股"), 100)
        self.assertEqual(period_title_score("2026年三季度業績公告", "2026-09-30", "港股"), 40)
        self.assertEqual(period_title_score("Third Quarter Results 2026", "2026-09-30", "港股"), 40)


class StoredReportMarketTests(unittest.TestCase):
    def evidence(self, title, period, source="港交所披露易"):
        return {"来源": source, "公告": [{"标题": title, "报告期匹配": period}]}

    def test_hk_old_tags_cannot_borrow_mainland_year_rules(self):
        for title, period in (
            ("2025年第三季度報告（2026年修訂版）", "2026-09-30"),
            ("2026/27年第一季度報告", "2026-03-31"),
            ("2026年第三季度報告", "2026-09-29"),
        ):
            with self.subTest(title=title):
                # Reproduces why taking max(HK, A) was unsafe.
                self.assertGreaterEqual(period_title_score(title, period, "A股"), 100)
                self.assertEqual(matched_report_notices(self.evidence(title, period), period), [])

    def test_unknown_or_missing_source_is_not_guessed_from_title(self):
        for source in (None, "", "未知", "东方财富", [], {"market": "A股"}):
            with self.subTest(source=source):
                evidence = self.evidence("2026年第三季度報告", "2026-09-30", source)
                self.assertEqual(matched_report_notices(evidence, "2026-09-30"), [])
        evidence = self.evidence("2026年第三季度報告", "2026-09-30")
        del evidence["来源"]
        self.assertEqual(matched_report_notices(evidence, "2026-09-30"), [])

    def test_legitimate_official_a_and_h_reports_remain_usable(self):
        for source, title, period in (
            ("巨潮资讯", "2026年第三季度报告", "2026-09-30"),
            ("港交所披露易", "中信銀行2026年第三季度報告（中國會計准則）", "2026-09-30"),
            ("港交所披露易", "截至2026年3月31日止年度報告", "2026-03-31"),
        ):
            with self.subTest(source=source, title=title):
                self.assertEqual(len(matched_report_notices(self.evidence(title, period, source), period)), 1)
        # A standard calendar-Q1 period cannot borrow an HK fiscal annual match.
        a = self.evidence("截至2026年3月31日止年度報告", "2026-03-31", "巨潮资讯")
        self.assertEqual(matched_report_notices(a, "2026-03-31"), [])

    def test_no_official_source_can_promote_dividend_or_results(self):
        for source in ("巨潮资讯", "港交所披露易"):
            for title in ("2026年第三季度股息公告", "2026年三季度業績公告"):
                with self.subTest(source=source, title=title):
                    evidence = self.evidence(title, "2026-09-30", source)
                    self.assertEqual(matched_report_notices(evidence, "2026-09-30"), [])


class PublicationDateMarketTests(unittest.TestCase):
    def quality(self, source, title, period="2026-09-30"):
        from research_quality import evidence_quality
        finance = {"状态": "可用", "报告期": period, "披露日期": "接口未提供",
                   "指标": {"收入": "1 CNY"}}
        evidence = {"来源": source, "对应报告期财报": {
            "报告期": period, "标题": title, "公告日期": "2026-10-29"}, "公告": []}
        return evidence_quality(finance, evidence, {}, "2026-11-10")

    def test_hk_ambiguous_old_tag_does_not_prove_publication(self):
        for title, period in (("2025年第三季度報告（2026年修訂版）", "2026-09-30"),
                              ("2026/27年第一季度報告", "2026-03-31")):
            with self.subTest(title=title):
                quality = self.quality("港交所披露易", title, period)
                self.assertIn("财务与公告未晚于行情快照", quality["缺口"])
                self.assertEqual(quality["分级"], "资料不足")

    def test_unknown_source_does_not_prove_publication(self):
        for source in (None, "", "未知", []):
            with self.subTest(source=source):
                quality = self.quality(source, "2026年第三季度报告")
                self.assertIn("财务与公告未晚于行情快照", quality["缺口"])

    def test_correct_market_can_prove_publication_but_not_pdf_verification(self):
        for source, title in (("巨潮资讯", "2026年第三季度报告"),
                              ("港交所披露易", "2026年第三季度報告"),
                              ("港交所披露易", "2026年三季度業績公告")):
            with self.subTest(source=source, title=title):
                quality = self.quality(source, title)
                self.assertIn("财务与公告未晚于行情快照", quality["通过项"])
                self.assertEqual(quality["同报告期PDF可读页段"], 0)
                self.assertIn("同报告期官方PDF有可读摘录", quality["缺口"])

    def test_dividend_cannot_prove_period_financial_publication(self):
        for source in ("巨潮资讯", "港交所披露易"):
            quality = self.quality(source, "2026年第三季度股息公告")
            self.assertIn("财务与公告未晚于行情快照", quality["缺口"])

    def test_mainland_does_not_borrow_hk_fiscal_year_end(self):
        quality = self.quality("巨潮资讯", "截至2026年3月31日止年度報告", "2026-03-31")
        self.assertIn("财务与公告未晚于行情快照", quality["缺口"])
