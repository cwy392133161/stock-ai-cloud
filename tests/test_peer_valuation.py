"""Offline sector median and no-fake-valuation regression checks."""
import unittest

import pandas as pd

from peer_valuation import industry_benchmarks, valuation_context
from research_quality import evidence_quality
from research_brief import build_research_brief


class ValuationTests(unittest.TestCase):
    def test_full_sector_sample_used_not_top_ranked_few(self):
        prices = pd.DataFrame([
            {"行业": "电力", "市盈率-动态": i + 5, "市净率": 1 + (i / 20)}
            for i in range(25)
        ])
        prices.loc[len(prices)] = {"行业": "未知", "市盈率-动态": 1, "市净率": 1}
        sectors = industry_benchmarks(prices)
        self.assertEqual(sectors["电力"]["同行样本数"], 25)
        self.assertNotIn("未知", sectors)
        q = {"行业": "电力", "市盈率-动态": 12, "市净率": 1.4,
             "行业估值参考": sectors["电力"]}
        ref = valuation_context(q)
        self.assertIn("动态PE", ref["行业参考"])
        self.assertIn("PB", ref["行业参考"])
        self.assertNotIn("合理价值", str(ref["行业参考"]["动态PE"]["个股倍数"]))

    def test_no_sector_if_few_companies_or_unverified_quote(self):
        rows = pd.DataFrame([
            {"行业": "机械", "市盈率-动态": -10, "市净率": None}
            for _ in range(11)
        ])
        self.assertFalse(industry_benchmarks(rows))
        ref = valuation_context({"入选原因": "市盈率 7 倍，估值分 80"})
        self.assertIsNone(ref["PE动态"])
        self.assertIn("缺乏", ref["状态"])
        self.assertIsNone(ref["行业参考"])

    def test_individual_extreme_pe_pb_cannot_compare_with_filtered_sector(self):
        rows = pd.DataFrame([
            {"行业":"制造业", "市盈率-动态": 15 + i % 6,
             "市净率": 1.2 + i / 100} for i in range(25)
        ])
        benchmark = industry_benchmarks(rows)["制造业"]
        outlier = valuation_context({
            "行业": "制造业", "市盈率-动态": 160,
            "市净率": 18, "行业估值参考": benchmark,
        })
        self.assertIsNone(outlier["PE动态"])
        self.assertIsNone(outlier["PB"])
        self.assertIsNone(outlier["行业参考"])
        ordinary = valuation_context({
            "行业": "制造业", "市盈率-动态": 18,
            "市净率": 1.4, "行业估值参考": benchmark,
        })
        self.assertIn("动态PE", ordinary["行业参考"])
        self.assertIn("PB", ordinary["行业参考"])

    def test_quality_never_claims_professional_on_sparse_evidence(self):
        finance = {"状态": "可用", "指标": {"营业收入": "100 CNY（累计）"},
                   "报告期": "2026-09-30", "披露日期": "2026-10-28", "历史财务": {
                       "可比同季度年度数": 4,
                   }}
        q = evidence_quality(finance, {}, valuation_context({}),
                             "2026-11-06")
        self.assertEqual(q["分级"], "纵向财务研究")
        self.assertIn("同报告期官方PDF有可读摘录", q["缺口"])
        self.assertNotIn("专业", q["分级"])

    def test_hk_percent_yoy_has_correct_basis(self):
        stock = {"代码": "00585", "名称": "示例"}
        finance = {"状态": "部分可用", "报告期": "2026-06-30",
                   "指标": {"营业收入同比": "-5.6 %（期间同比）",
                            "归母净利润同比": "1.2 %（期间同比）"}}
        brief = build_research_brief(stock, finance, None)
        self.assertIn("营收同比下降", brief["财务变化线索"])
        self.assertIn("归母净利润同比增长", brief["财务变化线索"])
        self.assertIn("缺少两个", "；".join(brief["多期财务对照"]))


if __name__ == "__main__":
    unittest.main()
