import unittest
from unittest.mock import patch

import ai_report


class ReportGuardTests(unittest.TestCase):
    def test_drop_wrong_comparison_and_keep_independent_cited_paragraph(self):
        content = ("财务费用同比增加，公司解释为借款利息暂停资本化[D1 p4]。\n"
                   "现金流24.16亿元明显高于营收67.43亿元。请核查原因。\n"
                   "两者走势相反。")
        notices = [{"编号": "D1", "链接": "https://example.com/notice.pdf",
                    "正文摘录": [{"页": 4}]}]
        body = ai_report.cited_narrative(content, notices)
        self.assertIn("借款利息暂停资本化", body)
        self.assertIn('href="https://example.com/notice.pdf#page=4"', body)
        self.assertNotIn("24.16", body)
        self.assertNotIn("核查原因", body)
        self.assertNotIn("两者", body)

    def test_fallback_also_applies_when_figures_are_missing(self):
        for unsafe in ["经营现金净额较营收显著偏高。", "归母已实现盈利。", "兩者方向不同。",
                       "本期溢利遠低於營業額。", "ＯＣＦ exceeds sales."]:
            with self.subTest(unsafe=unsafe):
                html = ai_report.cited_narrative(unsafe, [])
                self.assertNotIn(unsafe, html)
                self.assertIn("未通过发布校验", html)

    def test_cited_causal_explanation_survives_in_same_paragraph(self):
        notices = [{"编号": "D1", "链接": "https://example.com/notice.pdf",
                    "正文摘录": [{"页": 2}]}]
        content = ("现金流远高于营收。两者方向相反。"
                   "公司披露利润下滑主要由于内容成本刚性及渠道合作模式调整[D1 p2]。"
                   "相关版权合同正在续签。")
        body = ai_report.cited_narrative(content, notices)
        self.assertIn("内容成本刚性", body)
        self.assertIn("#page=2", body)
        self.assertIn("版权合同", body)
        self.assertNotIn("高于", body)
        self.assertNotIn("相反", body)

    def test_reference_cannot_whitelist_amount_sign_or_invalid_page(self):
        notices = [{"编号": "D1", "链接": "https://example.com/notice.pdf",
                    "正文摘录": [{"页": 2}]}]
        for unsafe in (
            "公司披露利润下滑由于成本增加[D1 p99]。",
            "公司披露利润下滑由于成本增加[D1 p2][D2 p2]。",
            "公司披露利润减少20%主要由于成本增加[D1 p2]。",
            "公司披露归母亏损因成本增加[D1 p2]。",
            "公司披露现金流高于营收因为借款增加[D1 p2]。",
            "公告说明现金净流入由于收款增加[D1 p2]。",
            "公司披露利润增长三成因为费用减少[D1 p2]。",
        ):
            with self.subTest(unsafe=unsafe):
                body = ai_report.cited_narrative(unsafe, notices)
                self.assertIn("未通过发布校验", body)

    def test_only_program_can_publish_comparison(self):
        rows = [("A股", {"代码": "000690", "名称": "宝新能源"},
                 "现金流远高于营收。", {"状态": "可用"})]
        with patch.object(ai_report, "financial_comparison", return_value="同期间 24.16 < 67.43"):
            page = ai_report.render_report({"updated": "2026-11-06"}, rows, "2026-11-07")
        self.assertIn("同期间 24.16 &lt; 67.43", page)
        self.assertNotIn("现金流远高于营收", page)
        with patch.object(ai_report, "financial_comparison", return_value=""):
            page = ai_report.render_report({"updated": "2026-11-06"}, rows, "2026-11-07")
        self.assertNotIn("同口径财务核对", page)
        self.assertNotIn("现金流远高于营收", page)


if __name__ == "__main__":
    unittest.main()
