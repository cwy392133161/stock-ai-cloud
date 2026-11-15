import unittest
from bs4 import BeautifulSoup
from correct_bank_ratio_report import correct_page


def fixture():
    return ('<html><meta name="research-generation-id" content="original-id"><h1>研究</h1>' +
            ''.join(f'<article><b id="stock-{i}">示例银行{i}</b>'
                    '<section class="bank-evidence">原数值与来源保持不变</section>'
                    '<section class="ai-insight"><p><b>经营和估值</b><br>'
                    '需核验拨备覆盖率上升来自贷款分母收缩。</p>'
                    '<p><b>跟踪条件</b><br>请核对资本口径。</p>'
                    '<details><blockquote>必须保留的原文：拨备覆盖率上升。</blockquote></details>'
                    '</section></article>' for i in range(5)) + '</html>')


class BankRatioCorrectionTests(unittest.TestCase):
    def test_bounded_removal_preserves_quotes_facts_and_identity(self):
        source = fixture()
        output, audit = correct_page(source)
        before, after = BeautifulSoup(source, 'html.parser'), BeautifulSoup(output, 'html.parser')
        self.assertEqual(audit['original_generation_id'], 'original-id')
        self.assertEqual(len(audit['removed_paragraphs']), 5)
        self.assertEqual(before.select('blockquote'), after.select('blockquote'))
        self.assertEqual(before.select('.bank-evidence'), after.select('.bank-evidence'))
        self.assertEqual(before.select('meta'), after.select('meta'))
        self.assertIn('<meta name="research-generation-id" content="original-id">', output)
        self.assertEqual(output.count('请核对资本口径。'), 5)
        self.assertNotIn('贷款分母收缩', output)
        self.assertIn('未恢复或重新审核模型原答', output)
        self.assertIn('贷款分母收缩', audit['removed_paragraphs'][0]['original_text'])

    def test_repeated_or_wrong_scope_fails_closed(self):
        output, _ = correct_page(fixture())
        for text in (output, fixture().replace('示例银行0', '软件公司'),
                     fixture().replace('bank-evidence', 'unverified'), '<h1>empty</h1>'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                correct_page(text)

    def test_no_op_does_not_claim_a_review(self):
        with self.assertRaises(ValueError):
            correct_page(fixture().replace('需核验拨备覆盖率上升来自贷款分母收缩。', '请核对来源。'))


if __name__ == '__main__':
    unittest.main()
