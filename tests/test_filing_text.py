import unittest
from unittest.mock import Mock, patch

from filing_text import extract, read_filings, trusted
from disclosures import fetch_disclosures


class FilingTextTests(unittest.TestCase):
    def test_prioritizes_material_item_before_cutoff(self):
        from datetime import date
        rows = [(date(2026, 11, 6), "日常通知", f"/file{i}.pdf") for i in range(4)]
        rows.append((date(2026, 10, 30), "第三季度报告", "/report.pdf"))
        with patch("disclosures.cn_rows", return_value=rows):
            notices = fetch_disclosures("A股", "000690", "2026-11-06")["公告"]
        self.assertEqual(notices[0]["标题"], "第三季度报告")

    def test_extraction_retains_page_and_table_context(self):
        pages = "目录\f单位：人民币百万元\n项目  本期  上期\n营业收入  3,200  2,600\n主要由于销量上升。"
        with patch("filing_text.subprocess.run", return_value=Mock(stdout=pages.encode())):
            evidence = extract(b"fixture")
        self.assertEqual(evidence[0]["页"], 2)
        self.assertIn("人民币百万元", evidence[0]["摘录"])
        self.assertIn("主要由于销量上升", evidence[0]["摘录"])

    def test_bad_pdf_falls_back_to_next_and_keeps_index(self):
        notices = [{"链接": str(i)} for i in range(3)]
        with patch("filing_text.download", side_effect=[ValueError(), b"", b""]), \
                patch("filing_text.extract", side_effect=[[], [{"页": 2, "摘录": "正文"}]]):
            read_filings(notices)
        self.assertIn("读取失败", notices[0]["正文状态"])
        self.assertIn("未提取", notices[1]["正文状态"])
        self.assertEqual(notices[2]["编号"], "D3")
        self.assertTrue(notices[2]["正文摘录"])

    def test_explicit_table_columns_unit_and_reporting_context(self):
        pages = ("2026年第三季度报告\\n单位：人民币百万元\n"
                 "项目  本期金额  上年同期\\n营业收入  3,200  2,600")
        with patch("filing_text.subprocess.run",
                   return_value=Mock(stdout=pages.encode())):
            snippets = extract(b"fixture")
        matched = next(e for e in snippets if "营业收入" in e["摘录"])
        self.assertEqual(matched["单位"], "单位：人民币百万元")
        self.assertIn("本期金额", matched["表头"])
        self.assertIn("第三季度", matched["期间上下文"])

    def test_missing_unit_never_inferred(self):
        content = "项目  本期  上期\\n营业收入  3,200  2,600"
        with patch("filing_text.subprocess.run",
                   return_value=Mock(stdout=content.encode())):
            snippets = extract(b"fixture")
        self.assertEqual(snippets[0]["单位"], "未定位")
        self.assertEqual(snippets[0]["期间上下文"], "未定位")

    def test_hk_thousands_unit_not_converted_to_full_currency(self):
        content = ("For the six months ended 30 June 2026\\n"
                   "Units: HK$'000\\nParticulars  2026  2025\\n"
                   "Revenue  3,200  2,600")
        with patch("filing_text.subprocess.run",
                   return_value=Mock(stdout=content.encode())):
            snippets = extract(b"fixture")
        self.assertTrue(any("HK$'000" in x["单位"] for x in snippets))
        self.assertTrue(any("2026  2025" in x["表头"] for x in snippets))
        self.assertTrue(any("six months" in x["期间上下文"] for x in snippets))

    def test_only_official_https(self):
        self.assertFalse(trusted("https://static.cninfo.com.cn.example.org/report.pdf"))
        self.assertFalse(trusted("http://www1.hkexnews.hk/report.pdf"))
