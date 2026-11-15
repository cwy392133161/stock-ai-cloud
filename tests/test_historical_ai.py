"""Historical single-market research must preserve the daily report and its review rules."""
from contextlib import ExitStack, redirect_stdout
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import ai_report


class HistoricalResearchTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.docs = root / "docs"
        self.docs.mkdir()
        self.cache = root / "checkpoints"
        self.page = self.docs / "ai.html"
        self.codes = ["601169", "002948", "000001", "600919", "601665"]
        self.data = {"updated": "2026-11-13 16:15",
                     "a_shares": [{"代码": code, "名称": code} for code in self.codes]}
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {
            "AI_API_KEY": "unit-test-secret", "AI_MODEL": "unit-test",
            "AI_BASE_URL": "https://example.invalid"}))
        self.stack.enter_context(patch.object(ai_report, "DOCS", self.docs))
        self.directory = self.stack.enter_context(patch.object(ai_report, "stock_directory"))
        self.finance = self.stack.enter_context(patch.object(
            ai_report, "fetch_financial", return_value={"状态": "接口无记录"}))
        self.disclosures = self.stack.enter_context(patch.object(
            ai_report, "fetch_disclosures", return_value={
                "来源": "样例", "状态": "未检索到", "查询期间": "测试", "公告": []}))
        self.model = self.stack.enter_context(patch.object(
            ai_report, "request_deepseek", return_value="unsupported model text"))

    def generate(self):
        ai_report.generate_report(self.data, self.page, markets=("A股",), cache_dir=self.cache)

    def test_interrupted_run_resumes_only_matching_prompts_and_rechecks_every_report(self):
        self.page.write_text("prior", encoding="utf-8")
        self.model.side_effect = ["unsupported model text"] * 2 + [RuntimeError("upstream")]
        with self.assertRaisesRegex(RuntimeError, "upstream"):
            self.generate()
        self.assertEqual(self.page.read_text(encoding="utf-8"), "prior")
        self.assertEqual(len(list(self.cache.iterdir())), 2)
        self.model.reset_mock()
        self.model.side_effect = None
        self.generate()
        self.assertEqual(self.model.call_count, 3)
        self.directory.assert_not_called()
        self.assertTrue(all(call.args[3] == "2026-11-13" for call in self.finance.call_args_list))
        self.assertTrue(all(call.args[2] == "2026-11-13" for call in self.disclosures.call_args_list))
        page = self.page.read_text(encoding="utf-8")
        self.assertIn("A股前5只，共5只", page)
        self.assertNotIn("港股前5只", page)
        self.assertNotIn("unsupported model text", page)
        self.assertIn('name="research-total-count" content="5"', page)
        self.assertIn('name="research-approved-count" content="0"', page)
        self.assertIn('name="research-generation-id"', page)
        for code in self.codes:
            self.assertEqual(page.count(f'id="stock-{code}"'), 1)
        self.data["a_shares"][1]["评分"] = 80
        self.model.reset_mock()
        self.generate()
        self.assertEqual(self.model.call_count, 1)

    def test_daily_scope_and_invalid_selections_fail_before_paid_requests(self):
        with self.assertRaisesRegex(ValueError, "港股"):
            ai_report.generate_report(self.data, self.page, markets=("港股",))
        with self.assertRaisesRegex(ValueError, "发布目录"):
            ai_report.generate_report(self.data, self.page, markets=("A股",), cache_dir=self.docs / "cache")
        self.data["a_shares"][1] = self.data["a_shares"][0]
        with self.assertRaisesRegex(ValueError, "代码互异"):
            self.generate()
        self.model.assert_not_called()

    def frozen(self):
        return {code: {
            "code": code, "market": "A股", "as_of": "2026-11-13",
            "finance": {"状态": "部分可用", "报告期": "2026-09-30", "披露日期": "2026-10-31"},
            "disclosures": {"来源": "巨潮资讯", "状态": "已取得公告索引", "查询期间": "截至11月13日",
                            "公告": [{"编号": "D1", "标题": "第三季度报告", "日期": "2026-10-31",
                                      "链接": f"https://static.cninfo.com.cn/finalpage/{code}.PDF",
                                      "正文摘录": [{"页": 2, "摘录": "已核验的官方正文"}],
                                      "正文状态": "已提取正文片段"}]},
        } for code in self.codes}

    def test_uses_frozen_evidence_and_preserves_original_page_labels(self):
        rows = self.frozen()
        rows[self.codes[0]]["disclosures"]["公告"].append({
            "编号": "D2", "标题": "公告二", "日期": "2026-11-13",
            "链接": "https://static.cninfo.com.cn/finalpage/other.pdf"})

        def extract(notices):
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["标题"], "公告二")
            notices[0].update({"编号": "D1", "正文摘录": [{"页": 4, "摘录": "补读的 PDF"}]})

        with patch.object(ai_report, "read_filings", side_effect=extract) as pdf:
            ai_report.generate_report(self.data, self.page, markets=("A股",), evidence_snapshot=rows)
        self.finance.assert_not_called()
        self.disclosures.assert_not_called()
        pdf.assert_called_once()
        page = self.page.read_text(encoding="utf-8")
        self.assertIn("D1 p2", page)
        self.assertIn("D2 p4", page)
        self.assertIn("已核验的官方正文", page)
        self.assertNotIn("正文摘录", rows[self.codes[0]]["disclosures"]["公告"][1])

    def test_rejects_bad_frozen_batch_before_any_paid_calls(self):
        for what in ("code", "market", "as_of", "财报日期", "历史财报日期", "公告日期", "PDF域名", "引用编号"):
            with self.subTest(what=what):
                rows = self.frozen()
                last = rows[self.codes[-1]]
                notice = last["disclosures"]["公告"][0]
                if what in ("code", "market", "as_of"):
                    last[what] = "不匹配"
                elif what == "财报日期":
                    last["finance"]["披露日期"] = "2026-11-14"
                elif what == "历史财报日期":
                    last["finance"]["历史财务"] = {"记录": [{"报告期": "2026-12-31"}]}
                elif what == "公告日期":
                    notice["日期"] = "2026-11-15"
                elif what == "PDF域名":
                    notice["链接"] = "https://example.invalid/report.pdf"
                elif what == "引用编号":
                    notice["编号"] = "D2"
                with self.assertRaises(ValueError):
                    ai_report.generate_report(self.data, self.page, markets=("A股",), evidence_snapshot=rows)
        self.model.assert_not_called()
