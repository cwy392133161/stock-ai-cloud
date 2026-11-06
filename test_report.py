import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import ai_report


class ReportTests(unittest.TestCase):
    def test_values_keep_precision_and_metadata(self):
        snapshot = {
            "指标": {"归母净利润": "-1234567890123456789.0100 CNY（累计）",
                   "基本每股收益": "0.03500 CNY/股（累计）"},
            "缺失": {"营业收入": "币种待核实 <未知>"},
        }
        html = ai_report.finance_html(snapshot)
        self.assertIn("-1,234,567,890,123,456,789.0100 CNY（累计）", html)
        self.assertIn("0.03500 CNY/股（累计）", html)
        self.assertIn("币种待核实 &lt;未知&gt;", html)
        self.assertEqual(snapshot["指标"]["归母净利润"], "-1234567890123456789.0100 CNY（累计）")

    def test_products_do_not_trigger_outage(self):
        summary, outage = ai_report.finance_summary("港股", [{"状态": "公司财务分析不适用"}] * 5)
        self.assertFalse(outage)
        self.assertIn("产品不适用 5 只", summary)

    def test_generation_warns_on_one_market_outage_and_keeps_ten_articles(self):
        a_error = {"状态": "接口无记录"}
        hk_full = {"状态": "可用", "指标": {"EPS": "1 CNY/股（累计）"}}
        hk_partial = {"状态": "部分可用", "指标": {"ROE": "1 %（报告期）"}, "缺失": {"EPS": "币种待核实"}}
        product = {"状态": "公司财务分析不适用", "产品类型": "ETF"}
        reports = [a_error] * 5 + [hk_full, hk_partial, a_error, product, product]
        stocks = [{"代码": str(i), "名称": str(i)} for i in range(5)]
        data = {"a_shares": stocks, "hk_shares": stocks, "updated": "2026-11-06 13:00"}
        env = {"GITHUB_ACTIONS": "true", "AI_API_KEY": "test-only", "AI_MODEL": "test",
               "AI_BASE_URL": "https://example.invalid"}
        with tempfile.TemporaryDirectory() as tmp, patch.object(ai_report, "DOCS", Path(tmp)), \
                patch.dict(os.environ, env), patch.object(ai_report, "stock_directory", return_value={}), \
                patch.object(ai_report, "fetch_financial", side_effect=reports), \
                patch.object(ai_report.requests, "post") as post, redirect_stdout(io.StringIO()) as log:
            Path(tmp, "data.json").write_text(json.dumps(data), encoding="utf-8")
            post.return_value.json.return_value = {"choices": [{"message": {"content": "示例分析"}}]}
            ai_report.main()
            output = Path(tmp, "ai.html").read_text(encoding="utf-8")
        self.assertEqual(post.call_count, 10)
        self.assertEqual(output.count("<article>"), 10)
        self.assertIn("0/5 家公司取得指标", output)
        self.assertIn("2/3 家公司取得指标（完整 1，部分 1，无可用指标 1），产品不适用 2 只", output)
        self.assertEqual(log.getvalue().count("::warning::"), 1)
        self.assertIn("::warning::A股", log.getvalue())
        self.assertNotIn("test-only", log.getvalue())


if __name__ == "__main__":
    unittest.main()
