import unittest

from bank_ratio_guard import BANK_RATIO_RULES, blocks_bank_ratio_inference
from ai_report import review_research_hypotheses, render_bank_evidence


class BankRatioGuardTests(unittest.TestCase):
    def test_program_formula_and_direction_have_explicit_conditions(self):
        self.assertIn("贷款损失准备余额÷不良贷款余额", BANK_RATIO_RULES)
        self.assertIn("不良贷款余额÷贷款总额", BANK_RATIO_RULES)
        self.assertIn("仅在其他条件不变且口径可比时", BANK_RATIO_RULES)
        self.assertIn("不是任何一家银行的实际成因", BANK_RATIO_RULES)
        rendered = render_bank_evidence({"指标": [], "未定位": []})
        self.assertIn("比率口径（非公司成因判断）", rendered)
        self.assertIn("贷款总额变化不能直接解释", rendered)

    def test_actual_errors_and_conditional_variants_blocked(self):
        for text in (
            "需核验拨备覆盖率上升来自拨备余额增加还是贷款分母收缩。",
            "拨备覆盖率下降6.28个百分点，须区分是不良余额下降、贷款扩张还是准备计提放缓。",
            "若拨备覆盖率下降，是否是不良贷款余额下降导致。",
            "撥備覆蓋率下降，須區分是不良餘額下降還是準備計提放緩。",
            "拨备覆盖率提高，或许是贷款总额减少，仍需核验。",
            "拨备覆盖率上升，不良贷款余额减少。",
            "拨备覆盖率由不良贷款余额下降驱动，不能确定。",
        ):
            with self.subTest(text=text):
                self.assertTrue(blocks_bank_ratio_inference(text))
                self.assertEqual(review_research_hypotheses(text, bank_context=True), ("", 1))

    def test_numeric_facts_and_neutral_questions_are_not_changed_by_guard(self):
        for text in (
            "拨备覆盖率为215.3%，较上期下降6.28个百分点。",
            "需核验同口径贷款损失准备余额与不良贷款余额。",
            "拨备覆盖率变化尚不能确定原因，需核验同口径原始资料。",
            "拨备覆盖率下降，需核验贷款损失准备余额和不良贷款余额。",
            "不良贷款率下降可能来自贷款总额增加，需核验。",
        ):
            with self.subTest(text=text):
                self.assertFalse(blocks_bank_ratio_inference(text))
        # A fact is preserved by this guard, not promoted past the existing
        # model-number gate. Trusted numeric rows remain on their own channel.
        self.assertEqual(review_research_hypotheses("拨备覆盖率为215.3%。", True), ("", 1))

    def test_no_cross_sentence_component_causation(self):
        self.assertFalse(blocks_bank_ratio_inference("拨备覆盖率待核验。贷款总额增加。"))
        self.assertFalse(blocks_bank_ratio_inference("贷款总额增加。拨备覆盖率待核验。"))


if __name__ == "__main__":
    unittest.main()
