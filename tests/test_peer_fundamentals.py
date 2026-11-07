"""Offline matched fiscal peer evidence, no calls to public APIs."""
import unittest
from unittest.mock import patch

import pandas as pd

from peer_fundamentals import candidate_pool, analyze_peer_financials


def stock():
    return {"代码": "000690", "行业": "电力", "流通市值数值": 1e10}


def financial():
    return {
        "报告期": "2026-09-30", "披露日期": "2026-10-28",
        "状态": "可用",
        "指标": {
            "营业总收入同比": "-12.0 %（累计同比）",
            "归母净利润同比": "15.0 %（累计同比）",
            "加权 ROE": "7.0 %（报告期）",
            "资产负债率": "50.0 %（期末）",
        },
    }


class FinancialPeerTests(unittest.TestCase):
    def test_candidate_pool_is_size_matched_issuer_not_top20(self):
        frame = pd.DataFrame([
            {"代码": "000690", "名称": "本公司", "行业": "电力", "流通市值": 1e10},
            {"代码": "000001", "名称": "同业A", "行业": "电力", "流通市值": 1.2e10},
            {"代码": "000002", "名称": "同业B", "行业": "电力", "流通市值": 2e10},
            {"代码": "000003", "名称": "异常规模", "行业": "电力", "流通市值": 1e12},
            {"代码": "000004", "名称": "其他行业", "行业": "机械", "流通市值": 1.1e10},
        ])
        pool = candidate_pool(frame, [stock()])
        self.assertEqual([v["代码"] for v in pool["000690"]], ["000001", "000002"])

    @patch("peer_fundamentals.ak.stock_financial_analysis_indicator_em")
    def test_peer_medians_same_period_and_stale_release_excluded(self, api):
        def response(symbol, indicator):
            code = symbol[:6]
            base = int(code[-1])
            return pd.DataFrame([
                {"SECUCODE": symbol, "REPORT_DATE": "2026-09-30",
                 "NOTICE_DATE": "2026-10-28" if code != "000004" else "2026-11-08",
                 "CURRENCY": "人民币", "TOTALOPERATEREVETZ": 10+base,
                 "PARENTNETPROFITTZ": 20+base, "ROEJQ": 8+base,
                 "ZCFZL": 45+base},
                {"SECUCODE": symbol, "REPORT_DATE": "2025-09-30",
                 "NOTICE_DATE": "2025-10-29", "CURRENCY": "人民币",
                 "TOTALOPERATEREVETZ": 9},
            ])
        api.side_effect = response
        candidates = [{"代码": f"00000{i}", "行业": "电力"}
                      for i in range(1, 5)]
        cache, budget = {}, {"calls": 0, "max_calls": 5}
        result = analyze_peer_financials(
            stock(), financial(), candidates, "2026-11-06 23:25", cache, budget
        )
        self.assertEqual(result["状态"], "有限同行财务对照（并非投资评级）")
        self.assertEqual(result["同行有效公司"], 3)
        self.assertEqual(result["可比指标"]["ROE"]["同行中位数"], "10")
        self.assertEqual(budget["calls"], 4)
        again = analyze_peer_financials(stock(), financial(), candidates,
                                        "2026-11-06", cache, budget)
        self.assertEqual(again["同行有效公司"], 3)
        self.assertEqual(budget["calls"], 4)

    @patch("peer_fundamentals.ak.stock_financial_analysis_indicator_em")
    def test_missing_period_or_too_few_peers_rejects_peer_claim(self, api):
        api.return_value = pd.DataFrame([{
            "SECUCODE": "000001.SZ", "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-01", "CURRENCY": "人民币",
            "TOTALOPERATEREVETZ": 15
        }])
        result = analyze_peer_financials(
            stock(), financial(), [{"代码": "000001", "行业": "电力"}],
            "2026-11-06", {}, {"calls":0, "max_calls":1}
        )
        self.assertIn("不足", result["状态"])
        self.assertNotIn("可比指标", result)

    def test_network_budget_exhaustion_degrades_without_false_peer_median(self):
        result = analyze_peer_financials(
            stock(), financial(), [{"代码":"000001","行业":"电力"}]*4,
            "2026-11-06", {}, {"calls":0, "max_calls":0}
        )
        self.assertIn("不足", result["状态"])


if __name__ == "__main__":
    unittest.main()
