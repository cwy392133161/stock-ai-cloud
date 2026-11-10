"""Offline regressions for 01548 segment scope and 605577 source/date mixing."""
import unittest
from evidence_guard import validate_source_claim, validate_source_quote
from ai_report import review_research_hypotheses


class EvidenceScopeTests(unittest.TestCase):
    def entry(self, text):
        return {'citation': 'D1 p10', 'type': '经营原因', 'quote': text, 'claim': text}

    def docs(self, text):
        return [{'编号': 'D1', '日期': '2026-09-16',
                 '链接': 'https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0916/2026091600224_c.pdf',
                 '正文摘录': [{'页': 10, '摘录': text, '表头': '未定位',
                              '单位': '未定位', '期间上下文': '未定位'}]}]

    def test_genscript_real_literal_is_not_issuer_level_evidence(self):
        text = ('收益、經調整毛利增長以及經調整經營虧損收窄，主要由於 CRDMO 行業上游需求強勢復蘇，'
                '支撐生物制劑、質粒\n及 mRNA 業務快速擴張，海外市場亦持續增長。')
        # Still a valid position match. The missing subject must not silently
        # inherit the issuer's consolidated scope from the PDF title.
        self.assertIsNotNone(validate_source_quote(self.entry(text), self.docs(text)))
        self.assertIsNone(validate_source_claim(self.entry(text), self.docs(text)))

    def test_explicit_segment_and_subsidiary_not_issuer_claim(self):
        for text in ('公司分部收益改善主要由于需求复苏。', '本集团子公司收入增长主要由于需求复苏。',
                     '本集团CRDMO业务收益改善主要由于需求复苏。',
                     '本公司控股企業收益改善主要由於需求復蘇。'):
            self.assertIsNone(validate_source_claim(self.entry(text), self.docs(text)))

    def test_segment_context_cannot_be_lost_at_sentence_boundary(self):
        text = '公司收益改善主要由于需求复苏。'
        self.assertIsNone(validate_source_claim(self.entry(text), self.docs('附属公司。' + text)))

    def test_mixed_segment_context_fails_closed_even_with_group_subject(self):
        text = '本集團收益改善主要由於需求復蘇。'
        self.assertIsNone(validate_source_claim(self.entry(text), self.docs('附屬公司。' + text)))

    def test_explicit_issuer_literal_still_accepted(self):
        text = '本集團收益改善主要由於需求復蘇。'
        result = validate_source_claim(self.entry(text), self.docs(text))
        self.assertIsNotNone(result)
        self.assertIn('不据此认证合并口径', result['主体范围'])

    def test_longban_actual_source_date_conflation_removed(self):
        text = '公司已发布交易风险提示，指出今日涨幅过大、追高风险与日内波动大，说明短期价格信号与基本面方向并不一致。'
        self.assertEqual(review_research_hypotheses(text), ('', 1))

    def test_attribution_variants_not_licensed_by_cautious_suffix(self):
        for text in ('公告指出今日涨幅过大，仍需警惕。',
                     '公告称今日涨幅过大，仍需警惕。',
                     '公告稱今日漲幅過大，仍需警惕。',
                     '公告中提及今日涨幅过大，仍需警惕。',
                     '公司昨日發出風險公告，仍需警惕。',
                     '公司昨日发出风险公告，仍需警惕。',
                     '据公司公告，今日涨幅过大，需谨慎。',
                     '公司已發佈交易風險提示，需審慎。'):
            self.assertEqual(review_research_hypotheses(text), ('', 1))

    def test_unverified_adjusted_and_parent_loss_assertion_removed(self):
        self.assertEqual(review_research_hypotheses('归母亏损扩大与经调整口径改善并存，须追问两者差异来源。'), ('', 1))

    def test_program_risk_and_forward_question_remain_separate(self):
        for text in ('规则已提示今日涨幅过大且暂无盈利。',
                     '下期归母亏损能否收窄，需核对同口径数据。',
                     '不能推断集团收入增长。'):
            self.assertEqual(review_research_hypotheses(text), (text, 0))

if __name__ == '__main__':
    unittest.main()
