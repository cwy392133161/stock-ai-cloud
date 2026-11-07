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

    def test_deepseek_retry_is_not_hard_capped_at_two_calls(self):
        from unittest.mock import Mock
        empty = Mock(status_code=200, headers={})
        empty.json.return_value = {"choices": [
            {"message": {"content": ""}, "finish_reason": "length"}
        ], "usage": {"completion_tokens": 100}}
        valid = Mock(status_code=200, headers={})
        valid.json.return_value = {"choices": [
            {"message": {"content": '{"sections": {}}'},
             "finish_reason": "stop"}
        ]}
        with patch.object(ai_report.requests, "post",
                          side_effect=[empty, empty, empty, valid]) as post, \
             patch.object(ai_report.time, "sleep") as sleep:
            text = ai_report.request_deepseek(
                "https://example.invalid", "test-only", "test", "JSON",
                retry_window_seconds=120
            )
        self.assertEqual(post.call_count, 4)
        self.assertEqual(text, '{"sections": {}}')
        self.assertEqual(sleep.call_count, 3)

    def test_deepseek_respects_provider_retry_after(self):
        from unittest.mock import Mock
        rate_limit = Mock(status_code=429, headers={"Retry-After": "6"})
        valid = Mock(status_code=200, headers={})
        valid.json.return_value = {"choices": [
            {"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}
        ]}
        with patch.object(ai_report.requests, "post",
                          side_effect=[rate_limit, valid]) as post, \
             patch.object(ai_report.time, "sleep") as sleep:
            out = ai_report.request_deepseek(
                "https://example.invalid", "test-only", "test", "JSON",
                retry_window_seconds=60
            )
        self.assertEqual(out, '{"ok":true}')
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(6.0)

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
                patch.object(ai_report, "fetch_disclosures", return_value={
                    "来源": "样例", "状态": "未检索到", "查询期间": "测试", "公告": []}), \
                patch.object(ai_report, "fetch_financial", side_effect=reports), \
                patch.object(ai_report.requests, "post") as post, redirect_stdout(io.StringIO()) as log:
            Path(tmp, "data.json").write_text(json.dumps(data), encoding="utf-8")
            post.return_value.json.return_value = {"choices": [{"message": {"content": "示例分析"}}]}
            ai_report.main()
            output = Path(tmp, "ai.html").read_text(encoding="utf-8")
        self.assertEqual(post.call_count, 10)
        for request in post.call_args_list:
            self.assertEqual(request.kwargs["json"]["response_format"],
                             {"type": "json_object"})
            self.assertIn("JSON", request.kwargs["json"]["messages"][0]["content"])
        self.assertEqual(output.count("<article>"), 10)
        self.assertIn("0/5 家公司取得指标", output)
        self.assertIn("2/3 家公司取得指标（完整 1，部分 1，无可用指标 1），产品不适用 2 只", output)
        # The research quality gate now warns on every short/unsupported
        # mock AI analysis in addition to the market-level data outage.
        self.assertEqual(log.getvalue().count("::warning::A股"), 1)
        self.assertIn("深度研判未通过证据门槛", log.getvalue())
        self.assertIn("research-approved-count", output)
        self.assertIn("::warning::A股", log.getvalue())
        self.assertNotIn("test-only", log.getvalue())


if __name__ == "__main__":
    unittest.main()
