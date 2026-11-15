import gzip
import json
import re
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ai_report
from research_trends import START, END, annotate_current, load_checks


DAY = "2026-11-13"
ARCHIVE = Path(__file__).resolve().parents[1] / "docs" / "history" / DAY


class ResearchTrendTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.docs = Path(temp.name)
        self.archive = self.docs / "history" / DAY
        self.archive.mkdir(parents=True)
        summary = json.loads((ARCHIVE / "summary.json").read_bytes())
        self.stocks = summary["a_shares"]["top20"][:5]
        self.data = {"updated": DAY + " 16:15", "a_shares": self.stocks,
                     "quality": {"A股": summary["a_shares"]["quality"]}}
        (self.docs / "data.json").write_text(json.dumps(self.data), encoding="utf-8")
        shutil.copyfile(ARCHIVE / "trend-audit.json.gz", self.archive / "trend-audit.json.gz")

    def test_live_paid_prompt_and_render_include_only_that_security(self):
        checks = load_checks(self.docs, self.data)
        self.assertEqual(sum(c["因子"]["年初至今涨跌幅"]["结果"] == "有差异" for c in checks.values()), 4)
        # Keep the market identities, use neutral issuer names for this test of
        # the daily prompt (the separate bank gate has its own filing fixture).
        for s in self.stocks:
            s["名称"] = "测试公司"
        with patch.dict("os.environ", {"AI_API_KEY": "test", "AI_MODEL": "test",
                                       "AI_BASE_URL": "https://example.invalid"}), \
                patch.object(ai_report, "fetch_financial", return_value={"状态": "接口无记录"}), \
                patch.object(ai_report, "fetch_disclosures", return_value={
                    "来源": "巨潮资讯", "公告": [], "状态": "无记录", "查询期间": DAY}), \
                patch.object(ai_report, "request_deepseek", return_value="test response") as model:
            output = self.docs / "mock.html"
            ai_report.generate_report(self.data, output, trend_checks=checks)
            self.assertEqual(model.call_count, 5)
            for call, stock in zip(model.call_args_list, self.stocks):
                payload = json.loads(call.args[3].split("输入资料：", 1)[1])
                self.assertEqual(payload["当期行情趋势复核"], checks[stock["代码"]])
            self.assertEqual(output.read_text(encoding="utf-8").count(START), 5)

    def test_existing_paid_html_preserved_byte_for_byte_apart_from_annotations(self):
        original = (ARCHIVE / "ai-bank.html").read_bytes()
        (self.docs / "ai.html").write_bytes(original)
        (self.archive / "ai-bank.html").write_bytes(original)
        nav = self.archive / "index.html"
        nav.write_text('<a href="./ai-bank.html">五家银行指标与新版 AI 研究</a>', encoding="utf-8")
        annotate_current(self.docs)
        page = (self.docs / "ai.html").read_bytes()
        self.assertEqual(re.sub(START.encode() + b".*?" + END.encode(), b"", page, flags=re.S), original)
        self.assertEqual(page.count(START.encode()), 5)
        self.assertTrue((self.archive / "trend-audit.json.gz").is_file())
        self.assertIn("./trend-audit.html", nav.read_text(encoding="utf-8"))
        annotate_current(self.docs)
        self.assertEqual((self.docs / "ai.html").read_bytes(), page)
        self.assertEqual((self.archive / "ai-bank.html").read_bytes(), original)

    def test_missing_or_different_inputs_are_never_presented_as_verified(self):
        path = self.archive / "trend-audit.json.gz"
        audit = json.loads(gzip.decompress(path.read_bytes()))
        by_code = {r["code"]: r for r in audit["rows"]}
        field = "年初至今涨跌幅"
        del by_code[self.stocks[0]["代码"]]
        by_code[self.stocks[1]["代码"]]["factors"][field]["quoted_pct"] = 9876
        audit["rows"] = list(by_code.values())
        path.write_bytes(gzip.compress(json.dumps(audit).encode()))
        checks = load_checks(self.docs, self.data)
        for stock in self.stocks[:2]:
            row = checks[stock["代码"]]["因子"][field]
            self.assertIsNone(row["日线复算%"])
            self.assertNotEqual(row["结果"], "一致")
        path.unlink()
        self.assertEqual(load_checks(self.docs, self.data), {})
