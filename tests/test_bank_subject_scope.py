"""Explicit bank subjects remain literal evidence, never inferred scope."""
import unittest

from evidence_guard import validate_source_claim, validate_source_quote


class BankSubjectScopeTests(unittest.TestCase):
    def entry(self, text):
        return {"citation": "D1 p3", "type": "直接披露", "quote": text, "claim": text}

    def docs(self, text):
        return [{"编号": "D1", "链接": "https://static.cninfo.com.cn/finalpage/report.PDF",
                 "正文摘录": [{"页": 3, "摘录": text, "表头": "本期及上年同期",
                              "单位": "人民币亿元", "期间上下文": "2026年前三季度"}]}]

    def test_explicit_bank_and_bounded_temporal_prefixes(self):
        for text in ("本行不良贷款率下降。", "报告期末，本行不良贷款率下降。",
                     "報告期內，本行不良貸款率下降。",
                     "2026年9月末，本行逾期贷款余额占比1.59%，较上年末上升0.25个百分点。",
                     "2026年前三季度，本行实现营业收入108.18亿元，同比增长9.01%。",
                     "报告期内，本公司收入持续增长。"):
            with self.subTest(text=text):
                result = validate_source_claim(self.entry(text), self.docs(text))
                self.assertIsNotNone(result)
                self.assertEqual(result["主张原文"], text)
                self.assertIn("不据此认证合并口径", result["主体范围"])

    def test_no_inferred_bank_subject_or_arbitrary_prefix(self):
        for text in ("不良贷款率持续下降。", "该银行不良贷款率下降。",
                     "若市场回暖，本行收入持续增长。", "据客户表示，本行收入持续增长。",
                     "未来，本行收入持续增长。", "2026年99月末，本行收入持续增长。",
                     "报告期末，客户声称本行收入持续增长。"):
            with self.subTest(text=text):
                self.assertIsNotNone(validate_source_quote(self.entry(text), self.docs(text)))
                self.assertIsNone(validate_source_claim(self.entry(text), self.docs(text)))

    def test_bank_possessive_and_segment_subjects_rejected(self):
        for text in ("本行客户收入持续增长。", "本行客戶收入持續增長。",
                     "报告期末，本行分行收入持续增长。", "本行支行收入持续增长。",
                     "本行员工收入持续增长。", "本行关联方收入持续增长。",
                     "本行的客户收入持续增长。", "本行股东收入持续增长。",
                     "本行子公司收入持续增长。", "本行业务分部收入持续增长。"):
            with self.subTest(text=text):
                self.assertIsNone(validate_source_claim(self.entry(text), self.docs(text)))
        text = "报告期末，本行收入持续增长。"
        self.assertIsNone(validate_source_claim(self.entry(text), self.docs("附属公司。" + text)))

    def test_other_truthfulness_checks_unchanged(self):
        text = "2026年前三季度，本行实现营业收入108.18亿元，同比增长9.01%。"
        entry, docs = self.entry(text), self.docs(text)
        entry["claim"] = "本行现金流质量优异。"
        self.assertIsNone(validate_source_claim(entry, docs))
        entry = self.entry(text)
        entry["citation"] = "D1 p4"
        self.assertIsNone(validate_source_claim(entry, docs))
        entry = self.entry(text)
        docs[0]["正文摘录"][0]["单位"] = "未定位"
        self.assertIsNone(validate_source_claim(entry, docs))
        text = "本行收入持续增长"
        self.assertIsNone(validate_source_claim(self.entry(text), self.docs(text + "，仅在需求复苏的假设下才可能实现。")))


if __name__ == "__main__":
    unittest.main()
