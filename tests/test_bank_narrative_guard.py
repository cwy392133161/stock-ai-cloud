import gzip
import hashlib
import json
import re
import unittest
from pathlib import Path
from bs4 import BeautifulSoup
from ai_report import review_research_hypotheses
from bank_ratio_guard import blocks_bank_ratio_inference, blocks_bank_fact_assertion
from correct_bank_narrative_report import correct_page

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / 'docs/history/2026-11-13'


class BankNarrativeTests(unittest.TestCase):
    def test_abbreviated_mechanisms_are_not_licensed_by_hedges(self):
        for text in (
            '拨备覆盖率下降，若由不良上升或核销消耗驱动，则风险抵补缓冲在变薄。',
            '拨备覆盖率微升可能由分母变化驱动，而非计提增强。',
            '拨备覆盖率提高，可能来自减值计提增加或不良处置加快。',
            '撥備覆蓋率下降，若由核銷消耗驅動，仍需核驗。',
        ):
            with self.subTest(text=text):
                self.assertTrue(blocks_bank_ratio_inference(text))
                self.assertEqual(review_research_hypotheses(text, True), ('', 1))

    def test_bank_facts_do_not_bypass_literal_claims(self):
        for text in ('逾期贷款占比上升的披露也削弱“资产质量稳定”的推断。',
                     '逾期贷款占比上升，但仍需核验。', '下期逾期贷款占比是否继续上升。',
                     '逾期貸款佔比上升，需核驗。', '不良率持平。', '净息差回升。'):
            with self.subTest(text=text):
                self.assertTrue(blocks_bank_fact_assertion(text))
                self.assertEqual(review_research_hypotheses(text, True), ('', 1))
        for text in ('若逾期贷款占比上升，则需核验风险。', '下期逾期贷款占比是否上升。',
                     '应核验贷款损失准备余额与不良贷款余额。'):
            with self.subTest(text=text):
                self.assertEqual(review_research_hypotheses(text, True), (text, 0))
        self.assertEqual(review_research_hypotheses('逾期贷款占比上升。', False)[1], 0)

    def test_actual_evidence_contains_data_not_an_absence_claim(self):
        for name in ('a-bank-evidence.json.gz', 'a-research-sources.json.gz'):
            evidence = json.loads(gzip.decompress((ARCHIVE / name).read_bytes()))
            notice = evidence['000001']['disclosures']['公告'][0]
            quote = next(x for x in notice['正文摘录'] if x['页'] == 23)
            normalized = re.sub(r'\s+', '', quote['摘录'])
            self.assertIn('逾期贷款余额占比1.59%，较上年末上升0.25个百分点', normalized)

    def test_actual_page_correction_preserves_evidence_identity_and_audit(self):
        source = (ARCHIVE / 'ai-before-narrative-correction.html').read_text()
        output, audit = correct_page(source)
        before, after = BeautifulSoup(source, 'html.parser'), BeautifulSoup(output, 'html.parser')
        for selector in ('meta', '.bank-evidence', 'blockquote', '.trend-check', '.bank-ratio-correction'):
            self.assertEqual(before.select(selector), after.select(selector))
        self.assertEqual(re.findall(r'<meta\b[^>]*>', source), re.findall(r'<meta\b[^>]*>', output))
        self.assertEqual(len(audit['removed_paragraphs']), 9)
        pingan = [p for p in audit['removed_paragraphs'] if '000001' in p['issuer'] and p['section'] == '反面证据'][0]
        self.assertEqual(len(pingan['reasons']), 2)
        self.assertIn('逾期贷款占比上升', pingan['original_text'])
        self.assertNotIn('逾期贷款占比上升的披露', output)
        self.assertIn('不表示财报未披露相关数据', output)
        saved = json.loads((ARCHIVE / 'bank-narrative-correction.json').read_text())
        self.assertEqual(saved['source_sha256'], hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(saved['corrected_sha256'], hashlib.sha256(output.encode()).hexdigest())
        self.assertEqual(saved['removed_paragraphs'], audit['removed_paragraphs'])
        for value in (output, source.replace('bank-evidence', 'invalid'), source.replace('research-generation-id', 'invalid')):
            with self.assertRaises(ValueError):
                correct_page(value)
        self.assertEqual(after.select_one('meta[name="research-generation-id"]')['content'], audit['original_generation_id'])
        # Only frozen archive files are regression fixtures. The daily workflow
        # must remain free to replace docs/ai.html and its live status normally.
        for path, expected in saved['preserved_files_sha256'].items():
            if path.startswith('docs/history/'):
                self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), expected)
