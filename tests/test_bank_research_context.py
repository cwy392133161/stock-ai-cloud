"""Bank framework selection must not manufacture industry or issuer facts."""
import unittest
from unittest.mock import patch

from ai_report import review_research_hypotheses
from finance_facts import financial_comparison
from research_brief import build_research_brief
from research_differentiation import bank_research_context, focal_question
from test_finance_facts import snapshot


def evidence(title="示例银行2026年第三季度报告", text="本行不良贷款率下降。"):
    return {"市场": "A股", "来源": "巨潮资讯", "公告": [{
        "标题": title, "报告期匹配": "2026-09-30", "编号": "D1",
        "链接": "https://static.cninfo.com.cn/finalpage/2026-10-30/report.PDF",
        "正文摘录": [{"页": 3, "摘录": text}]}]}


class BankResearchContextTests(unittest.TestCase):
    def test_explicit_bank_industry_and_report_backed_fallback(self):
        finance = snapshot("10", "200", "30")
        for industry in ("银行", "銀行", "Banking"):
            self.assertTrue(bank_research_context({"行业": industry}, finance))
        stock = {"名称": "示例银行"}
        self.assertTrue(bank_research_context(stock, finance, evidence()))
        self.assertTrue(bank_research_context(stock, finance, evidence("2026年第三季度报告")))
        self.assertTrue(bank_research_context(stock, finance, evidence("2026年第三季度报告", "本行不存在实际控制人和收购人。")))
        untagged = evidence("2026年三季度报告", "本行不存在实际控制人和收购人。")
        del untagged["公告"][0]["报告期匹配"]
        self.assertTrue(bank_research_context(stock, finance, untagged))

    def test_no_inference_from_name_leverage_or_unrelated_reports(self):
        finance = snapshot("10", "200", "30")
        stock = {"名称": "示例银行", "行业": None}
        self.assertFalse(bank_research_context(stock, finance))
        untagged = evidence()
        untagged["公告"][0].pop("报告期匹配")
        for period in ("未核实", "2026-13-30", None):
            self.assertFalse(bank_research_context(stock, {**finance, "报告期": period}, untagged))
        for title in ("2025年第三季度报告", "示例银行2026年第三季度报告摘要", "股东会通知"):
            self.assertFalse(bank_research_context(stock, finance, evidence(title)))
        self.assertFalse(bank_research_context({"名称": "示例银行科技"}, finance, evidence()))
        self.assertFalse(bank_research_context(stock, finance, evidence("2026年第三季度报告", "客户银行贷款增长。")))
        self.assertFalse(bank_research_context({"行业": "银行软件"}, finance))
        self.assertFalse(bank_research_context({**stock, "行业": "软件开发"}, finance, evidence()))
        for url in ("", "https://evil.example/report.pdf"):
            bad = evidence()
            bad["公告"][0]["链接"] = url
            self.assertFalse(bank_research_context(stock, finance, bad))

    def test_brief_suppresses_generic_cash_quality_observations(self):
        finance = snapshot("10", "200", "30")
        stock = {"名称": "示例银行"}
        with patch("research_brief.cash_profit_observation") as observation:
            brief = build_research_brief(stock, finance, evidence(), "2026-11-13")
            observation.assert_not_called()
        self.assertTrue(brief["银行研究口径"])
        self.assertIn("拨备", brief["后续验证点"][0])
        focus = focal_question(stock, finance, evidence())
        self.assertIn("银行", focus["研究主题"])
        self.assertNotIn("现金流与盈利方向", focus["下一期关键问题"])

    def test_dated_amounts_stay_visible_but_bank_comparison_is_not_inferred(self):
        finance = snapshot("10", "200", "30")
        text = financial_comparison(finance, True)
        self.assertIn("归母盈利 10 CNY", text)
        self.assertIn("净流入 200 CNY", text)
        self.assertNotIn("高于同期", text)
        self.assertIn("高于同期", financial_comparison(finance))

    def test_conditional_labels_cannot_license_inapplicable_bank_method(self):
        for text in (
            "经营现金流远超营收，若不能由经营解释，则现金流质量存疑。",
            "现金流与营收的异常关系缺乏官方解释，不能视为质量证据。",
            "若经营现金流与盈利背离，则需要追问回款质量。",
            "若經營現金流與營收背離，則需核驗盈利質量。",
            "若经营活动产生的现金超过收入，则需评估回收质量。",
        ):
            with self.subTest(text=text):
                self.assertEqual(review_research_hypotheses(text, True), ("", 1))
        self.assertEqual(review_research_hypotheses("应核验净息差、不良贷款及拨备口径。", True)[1], 0)


if __name__ == "__main__":
    unittest.main()
