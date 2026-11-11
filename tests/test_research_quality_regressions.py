"""Deterministic publication quality, not a claim of semantic verification."""
import json
import unittest
from ai_report import review_research_hypotheses, assess_model_research
from research_brief import build_research_brief
from research_differentiation import focal_question
from test_research_security import source, financial, model


class ResearchQualityRegressions(unittest.TestCase):
    def test_condition_does_not_license_later_fact_in_same_sentence(self):
        for text in (
            "若收入增长，公司订单持续增长。",
            "如果公司订单持续增长，营收同比增长且利润持续改善。",
            "若订单增长但公司营收同比增长。",
            "若收入增长同时公司利润持续改善。",
            "不能证明公司订单持续增长，公司产能大幅扩大。",
            "如果收入增长，需核对利润；公司订单持续增长。",
        ):
            with self.subTest(text=text):
                out, removed = review_research_hypotheses(text)
                self.assertNotIn("公司订单持续增长", out)
                self.assertNotIn("公司产能大幅扩大", out)
                self.assertNotIn("公司营收同比增长", out)
                self.assertGreater(removed, 0)

    def test_all_matches_checked_not_only_first(self):
        # First fact has a local hedge; second no longer does (beyond its scope).
        text = "若订单增长" + "应审慎核对相关报告并保留判断边界" * 5 + "公司产能持续增长。"
        self.assertEqual(review_research_hypotheses(text), ("", 1))

    def test_traditional_assertions_and_qualified_questions(self):
        for text in ("公司訂單持續增長。", "本集團營收同比增長。",
                     "經營現金流持續改善。", "本期虧損持續擴大。", "本期經調整口徑改善。", "公司市場份額顯著提升。",
                     "如果收入增長，公司產能大幅擴大。"):
            with self.subTest(text=text):
                self.assertEqual(review_research_hypotheses(text), ("", 1))
        text = "下期應核對營收是否增長。若公司訂單持續增長，需核實來源。"
        self.assertEqual(review_research_hypotheses(text), (text, 0))

    def assessment(self, raw, evidence):
        brief = build_research_brief({"代码": "000690"}, financial(), evidence, "2026-11-06")
        return assess_model_research(json.dumps(raw, ensure_ascii=False), brief, evidence["公告"])

    def test_irrelevant_address_quote_cannot_promote_research(self):
        evidence = source()
        for quote in ("本公司注册地址位于香港中环。",
                      "本公司报告目录包含利润表及现金流量表。",
                      "本公司财务报告包括现金流量表。"):
            with self.subTest(quote=quote):
                evidence["公告"][0]["正文摘录"][0]["摘录"] = quote
                raw = model()
                raw["source_quotes"] = [{"citation": "D1 p3", "quote": quote}]
                raw["source_claims"] = [{"citation": "D1 p3", "quote": quote, "claim": quote, "type": "直接披露"}]
                result = self.assessment(raw, evidence)
                self.assertNotEqual(result["状态"], "解释性研究已通过基础发布门槛")
                self.assertEqual(result["研究相关证据主张数"], 0)

    def test_repeated_four_sections_are_not_four_independent_sections(self):
        raw = model()
        paragraph = "如果未来收入增长，需要核对相关资料并审慎判断经营趋势，不能将短期走势当作持久优势，需要继续补足同口径资料。"
        raw["sections"] = {key: paragraph for key in raw["sections"]}
        result = self.assessment(raw, source())
        self.assertEqual(result["状态"], "仅发布可核对事实")
        self.assertEqual(len(result["重复章节"]), 3)
        self.assertFalse(result["章节"])

    def test_bank_and_unknown_industry_not_generic_high_debt_warning(self):
        finance = financial()
        finance["指标"].update({"营业总收入同比": "12 %（累计同比）", "归母净利润同比": "8 %（累计同比）", "资产负债率": "92 %（期末）"})
        for industry in ("银行", "銀行", "保险", "", None, "未知", "—", "nan", "Securities", "Brokerage"):
            with self.subTest(industry=industry):
                result = focal_question({"行业": industry}, finance)
                self.assertNotEqual(result["研究主题"], "高负债与增长质量")
                self.assertIn("阈值", result["下一期关键问题"])
        result = focal_question({"行业": "通用设备"}, finance)
        self.assertEqual(result["研究主题"], "高负债与增长质量")
