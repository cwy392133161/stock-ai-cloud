"""Offline regression tests for market-wide quality and existing score rules."""
import json
import time
import unittest
from unittest.mock import patch

import pandas as pd

import picker
from market_quality import MarketDataQualityError, validate_market_data


def quotes(n=3500):
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    return pd.DataFrame({
        "代码": codes,
        "f124": [time.time()] * n,
        "名称": ["样本"] * n,
        "最新价": [10.0 + i / 1000 for i in range(n)],
        "涨跌幅": [float((i % 100) - 50) / 10 for i in range(n)],
        "成交额": [100000000.0 + i * 1000 for i in range(n)],
        "流通市值": [4000000000.0 + i * 100000 for i in range(n)],
        "60日涨跌幅": [float(i % 65) for i in range(n)],
        "年初至今涨跌幅": [float(i % 80) for i in range(n)],
        "换手率": [1.0 + i / 1000 for i in range(n)],
        "量比": [1.0 + i / 10000 for i in range(n)],
        "市盈率-动态": [9.0 + i / 100 for i in range(n)],
        "市净率": [1.0 + i / 5000 for i in range(n)],
    })


class DataQualityTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(picker, "fetch_tencent_a", side_effect=RuntimeError("offline source unavailable"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_complete_market_validates_and_is_json_safe(self):
        q = validate_market_data(quotes(), "A股", "fixture")
        self.assertEqual(q["有效股票数"], 3500)
        self.assertEqual(q["字段有效覆盖率%"]["流通市值"], 100.0)
        json.dumps(q)

    def test_weak_sina_fallback_rejected(self):
        bad = quotes().drop(columns=[
            "换手率", "量比", "市盈率-动态", "市净率",
            "流通市值", "60日涨跌幅", "年初至今涨跌幅"
        ])
        with self.assertRaisesRegex(MarketDataQualityError, "关键字段覆盖不足"):
            validate_market_data(bad, "A股", "新浪")

    def test_partial_pages_rejected(self):
        with self.assertRaisesRegex(MarketDataQualityError, "疑似全市场数据不完整"):
            validate_market_data(quotes(100), "A股", "one-page")

    def test_duplicate_codes_rejected(self):
        bad = quotes()
        bad.loc[0, "代码"] = bad.loc[1, "代码"]
        with self.assertRaisesRegex(MarketDataQualityError, "重复"):
            validate_market_data(bad, "A股", "duplicate")

    def test_nonfinite_values_rejected(self):
        bad = quotes()
        bad["流通市值"] = float("inf")
        with self.assertRaises(MarketDataQualityError):
            validate_market_data(bad, "A股", "infinite")

    def test_hk_rmb_and_hkd_counters_keep_distinct_codes(self):
        self.assertEqual(picker._norm_hk_code("09988"), "09988")
        self.assertEqual(picker._norm_hk_code("89988"), "89988")
        self.assertEqual(picker._norm_hk_code("9988"), "09988")
        self.assertNotEqual(picker._norm_hk_code("09988"), picker._norm_hk_code("89988"))
        for invalid in ("", "89988.HK", "899880", "-"):
            with self.assertRaises(ValueError):
                picker._norm_hk_code(invalid)

    def test_em_hk_raw_feed_does_not_collapse_dual_counters(self):
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {"data": {"total": 2, "diff": [
                    {"f12": "09988", "f14": "阿里巴巴-W", "f2": 120, "f3": 1},
                    {"f12": "89988", "f14": "阿里巴巴-R", "f2": 110, "f3": 1},
                ]}}
        with patch.object(picker.requests, "get", return_value=Response()):
            frame = picker.fetch_em_clist("72", "", "HK")
        self.assertEqual(list(frame["代码"]), ["09988", "89988"])
        self.assertFalse(frame["代码"].duplicated().any())

    def test_hk_market(self):
        self.assertEqual(validate_market_data(quotes(1500), "港股", "fixture")["有效股票数"], 1500)
    def test_hk_80_pct_turnover_coverage_is_acceptable(self):
        frame = quotes(1500)
        # Live HKEX-wide feed has ~80% nonzero trade turnover.
        frame.loc[:299, "成交额"] = 0
        report = validate_market_data(frame, "港股", "东财直连")
        self.assertEqual(report["字段有效覆盖率%"]["成交额"], 80.0)
        # Existing 10m HKD eligibility threshold still excludes those rows.
        self.assertEqual(len(picker.score_hk(frame)), 1200)

    def test_hk_70_pct_turnover_coverage_still_rejected(self):
        frame = quotes(1500)
        frame.loc[:449, "成交额"] = 0
        with self.assertRaisesRegex(MarketDataQualityError, "成交额"):
            validate_market_data(frame, "港股", "异常行情")

    def test_a_share_turnover_80_pct_still_rejected(self):
        frame = quotes(3500)
        frame.loc[:699, "成交额"] = 0
        with self.assertRaisesRegex(MarketDataQualityError, "成交额"):
            validate_market_data(frame, "A股", "异常行情")


    def test_picker_retains_weight_formulas(self):
        ranked = picker.score_a(quotes())
        self.assertEqual(len(ranked), 3500)
        self.assertGreater(ranked["评分"].nunique(), 10)
        self.assertAlmostEqual(
            float(ranked.iloc[0]["评分"]),
            round(0.4 * ranked.iloc[0]["趋势分"]
                  + 0.25 * ranked.iloc[0]["活跃分"]
                  + 0.2 * ranked.iloc[0]["估值分"]
                  + 0.15 * (
                      picker._rank_pct(ranked["流通市值"]).loc[ranked.index[0]]
                  ), 1),
            delta=0.2
        )

    def test_missing_cap_does_not_bypass_old_filter(self):
        bad = quotes()
        bad["流通市值"] = float("nan")
        self.assertEqual(len(picker.score_a(bad)), 0)

    def test_fetch_market_skips_bad_direct_and_accepts_complete_source(self):
        good = quotes()
        bad = good.drop(columns=["流通市值"])
        with patch.object(picker, "ak", object()), patch.object(
            picker, "fetch_em_market", side_effect=[bad, good]
        ), patch.object(picker.time, "sleep", return_value=None):
            result = picker.fetch_market("港股", [], "", "HK", None, lambda: bad)
            self.assertEqual(result.attrs["quality"]["行情来源"], "东财港股原始字段（AKShare传输）")
            self.assertEqual(len(result), 3500)

    def test_bad_source_never_publishes_fake_scores(self):
        bad = quotes().drop(columns=["流通市值"])
        with patch.object(picker, "ak", None), patch.object(
            picker, "fetch_em_market", return_value=bad
        ):
            with self.assertRaisesRegex(RuntimeError, "未找到字段完整"):
                picker.fetch_market("A股", [], "", "A", lambda: bad, lambda: bad)

    def test_em_incomplete_pagination_does_not_succeed(self):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
            def raise_for_status(self):
                pass
            def json(self):
                return {"data": self.data}

        first = {"total": 300, "diff": [{"f12": "000001", "f2": 10}]}
        rest = {"total": 300, "diff": []}
        with patch.object(picker.requests, "get", side_effect=[
            FakeResponse(first), FakeResponse(rest), FakeResponse(rest)
        ]), patch.object(picker.time, "sleep", return_value=None):
            with self.assertRaisesRegex(ValueError, "分页不完整"):
                picker.fetch_em_clist("82", "", "A")


if __name__ == "__main__":
    unittest.main()
