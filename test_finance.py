import unittest
from unittest.mock import patch

import pandas as pd

from finance import a_exchange, dated_rows, fetch_financial, metric, product_type


class FinancialTests(unittest.TestCase):
    def test_exchange_and_product_boundaries(self):
        self.assertEqual([a_exchange(c) for c in ("000690", "601939", "920001", "830001")],
                         ["SZ", "SH", "BJ", "BJ"])
        self.assertIsNone(product_type("港股", "07400", "公司"))
        for code in ("07347", "07515", "02800", "03033", "09101"):
            with patch("finance.ak.stock_financial_hk_analysis_indicator_em") as api:
                self.assertEqual(fetch_financial("港股", code)["状态"], "公司财务分析不适用")
                api.assert_not_called()
        self.assertEqual(product_type("港股", "01234", "示例 ETN"), "ETN")

    def test_period_identity_and_disclosure(self):
        df = pd.DataFrame([
            ("A", "2026-06-30", "2026-08-01"), ("B", "2026-09-30", None),
            ("A", "2099-01-01", None), ("A", "2026-09-30", "2026-11-07"),
            ("A", "无效", None),
        ], columns=["SECUCODE", "REPORT_DATE", "NOTICE_DATE"])
        self.assertEqual(dated_rows(df, "A", pd.Timestamp("2026-11-06")).index.tolist(), [0])

    def test_no_metrics_is_explicit(self):
        df = pd.DataFrame([{"SECUCODE": "000690.SZ", "REPORT_DATE": "2026-09-30"}])
        with patch("finance.ak.stock_financial_analysis_indicator_em", return_value=df) as api:
            result = fetch_financial("A股", "690", as_of="2026-11-06")
        api.assert_called_once_with(symbol="000690.SZ", indicator="按报告期")
        self.assertEqual(result["状态"], "无可用指标")
        self.assertFalse(result["指标"])

    def test_zero_negative_missing_and_uncertain_currency(self):
        self.assertEqual(metric(0, "元", None), ("0", None))
        self.assertEqual(metric(-12, "元", None), ("-12", None))
        for raw in ("--", None, "inf", float("nan")):
            self.assertIsNone(metric(raw, "元", None)[0])
        self.assertEqual(metric(12, "元", "未知币种"), (None, "未知币种"))


if __name__ == "__main__":
    unittest.main()
