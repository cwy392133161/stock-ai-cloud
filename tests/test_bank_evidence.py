import gzip
import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ai_report
from bank_evidence import bank_focus, bank_metrics, select_bank_passages


DAY = "2026-11-13"
PERIOD = "2026-09-30"
ARCHIVE = Path(__file__).resolve().parents[1] / "docs" / "history" / DAY


class BankEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.stock = {"名称": "齐鲁银行", "代码": "601665"}
        self.notice = {"日期": "2026-10-30", "编号": "D1", "标题": "2026年第三季度报告",
                       "链接": "https://static.cninfo.com.cn/finalpage/report.pdf"}
        self.finance = {"状态": "可用", "报告期": PERIOD, "披露日期": "2026-10-30"}
        self.evidence = {"来源": "巨潮资讯", "状态": "可用", "查询期间": DAY, "公告": [self.notice]}

    def facts(self, text):
        self.notice["银行指标摘录"] = select_bank_passages(["", text], PERIOD)
        return bank_metrics(self.stock, self.finance, self.evidence, DAY)["指标"]

    def test_narrative_requires_current_period_and_issuer_scope(self):
        for text in (
            "2025年9月末，本行不良贷款率1.05%。",
            "报告期末，本公司子公司不良贷款率0.01%。",
            "报告期末，个人贷款不良贷款率0.02%。",
            "报告期末，本行预计不良贷款率0.03%。",
            "报告期末，本行不良贷款率同比下降0.04个百分点。",
            "2026年前三季度，本行净利差1.23%。",
            "项目 本报告期末 上年末\n不良贷款率 0.98 1.05",
            "单位：%\n项目 上年末 本报告期末\n不良贷款率 1.05 0.98",
            "项目 报告期末 上年末\n本行口径 集团口径 本行口径 集团口径\n核心一级资本充足率（%） 8.99 9.20 8.16 8.37",
            "项目 报告期末 上年末\n监管标准\n不良贷款率（%） 5.00 0.98 1.05",
        ):
            with self.subTest(text=text):
                self.assertEqual(self.facts(text), [])

    def test_official_pdf_title_source_and_publication_cutoff(self):
        fragment = "报告期末，本行不良贷款率0.98%，较上年末下降0.07个百分点。"
        self.assertEqual(self.facts(fragment)[0]["对照期"], "2025-12-31")
        for key, value in [("链接", "https://evil.invalid/report.pdf"), ("日期", "2026-11-14"),
                           ("标题", "2026年第三季度业绩说明会"), ("报告期匹配", "2026-06-30")]:
            with self.subTest(key=key), patch.dict(self.notice, {key: value}):
                self.assertEqual(self.facts(fragment), [])

    def test_group_date_columns_select_corresponding_prior_group(self):
        facts = self.facts("2026年9月30日  2025年12月31日\n项目\n集团口径 本行口径 集团口径 本行口径"
                           "\n6.核心一级资本充足率  9.20%  8.99%  8.37%  8.16%")
        self.assertEqual([(r["数值"], r["变化"], r["口径"]) for r in facts],
                         [("9.20", "较上年末上升0.83个百分点", "集团口径")])

    def test_actual_five_filings_dates_pages_and_distinct_focus(self):
        raw = json.loads(gzip.decompress((ARCHIVE / "a-bank-evidence.json.gz").read_bytes()))
        prior = json.loads(gzip.decompress((ARCHIVE / "a-research-sources.json.gz").read_bytes()))
        themes = set()
        by_code = {}
        for code, row in raw.items():
            bank = bank_metrics(self.stock, row["finance"], row["disclosures"], DAY)
            by_code[code] = {item["指标"]: item for item in bank["指标"]}
            themes.add(bank_focus(self.stock, bank)["研究主题"])
            for idx, notice in enumerate(row["disclosures"]["公告"]):
                self.assertEqual(notice.get("原文SHA256"),
                                 prior[code]["disclosures"]["公告"][idx].get("原文SHA256"))
        self.assertEqual(len(themes), 5)
        qilu = by_code["601665"]
        self.assertEqual((qilu["不良贷款率"]["数值"], qilu["不良贷款率"]["页"],
                          qilu["不良贷款率"]["变化"]), ("0.98", 3, "较上年末下降0.07个百分点"))
        self.assertEqual((qilu["拨备覆盖率"]["数值"], qilu["拨备覆盖率"]["页"],
                          qilu["拨备覆盖率"]["变化"]), ("359.91", 3, "较上年末提高4个百分点"))
        nim = by_code["000001"]["净息差"]
        self.assertEqual((nim["页"], nim["数值"], nim["期间"]), (19, "1.80", "2026-01-01 至 2026-09-30"))
        capital = by_code["600919"]["核心一级资本充足率"]
        self.assertEqual((capital["数值"], capital["页"], capital["表头页"]), ("8.86", 7, 6))

    def test_five_bank_cards_with_no_numeric_evidence_skip_paid_requests(self):
        stocks = [{"代码": str(600001+i), "名称": f"样例{i}银行"} for i in range(5)]
        data = {"updated": DAY + " 16:15", "a_shares": stocks}
        evidence = deepcopy(self.evidence)
        evidence["公告"] = []
        rows = {s["代码"]: {"market": "A股", "code": s["代码"], "as_of": DAY,
                         "finance": self.finance, "disclosures": evidence} for s in stocks}
        with TemporaryDirectory() as scratch, patch.dict("os.environ", {
                "AI_API_KEY": "test", "AI_MODEL": "test", "AI_BASE_URL": "https://example.invalid"}), \
                patch.object(ai_report, "request_deepseek") as model:
            out = Path(scratch) / "report.html"
            ai_report.generate_report(data, out, evidence_snapshot=rows)
            model.assert_not_called()
            page = out.read_text(encoding="utf-8")
            self.assertEqual(page.count("<article>"), 5)
            self.assertEqual(page.count(ai_report.BANK_NO_CALL), 5)
