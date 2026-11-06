"""Offline regression tests for market-wide quality and existing score rules."""
import json
import unittest
from unittest.mock import patch

import pandas as pd

import picker
from market_quality import MarketDataQualityError, validate_market_data


def quotes(n=1200):
    codes = [f"{i:06d}" for i in range(1, n + 1)]
    return pd.DataFrame({
        "代码": codes,
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
    def test_complete_market_validates_and_is_json_safe(self):
        q = validate_market_data(quotes(), "A股", "fixture")
        self.assertEqual(q["有效股票数"], 1200)
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

    def test_hk_market(self):
        self.assertEqual(validate_market_data(quotes(500), "港股", "fixture")["有效股票数"], 500)

    def test_picker_retains_weight_formulas(self):
        ranked = picker.score_a(quotes())
        self.assertEqual(len(ranked), 1200)
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

    def test_fetch_market_skips_low_quality_fallback(self):
        good = quotes()
        bad = good.drop(columns=["流通市值"])
        with patch.object(picker, "ak", None), patch.object(
            picker, "fetch_em_market", side_effect=RuntimeError("blocked")
        ):
            with self.assertRaisesRegex(RuntimeError, "未找到字段完整"):
                picker.fetch_market("A股", [], "", "A", lambda: good, lambda: bad)


if __name__ == "__main__":
    unittest.main()
