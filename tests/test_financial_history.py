"""Financial history must be as-of and same-fiscal-period comparable."""
import unittest
from unittest.mock import patch

import pandas as pd

from financial_history import assemble_history
from finance import cashflow, dated_rows


def a_record(year, month, revenue, *, notice=None):
    end = f"{year}-{month:02d}-{30 if month in (6, 9) else 31:02d}"
    return {
        "SECUCODE": "000690.SZ", "REPORT_DATE": end,
        "NOTICE_DATE": notice or f"{year}-11-01" if month == 9 else f"{year}-08-01",
        "CURRENCY": "人民币",
        "TOTALOPERATEREVE": revenue, "PARENTNETPROFIT": revenue / 10,
        "TOTALOPERATEREVETZ": -12.5 if year == 2026 else 5.4,
        "PARENTNETPROFITTZ": 4.2, "ROEJQ": 8, "ZCFZL": 40,
    }


class FinancialHistoryTests(unittest.TestCase):
    def test_four_years_same_reporting_quarter(self):
        records = [a_record(y, 9, float(y - 2000) * 100)
                   for y in (2023, 2024, 2025, 2026)]
        records += [a_record(2026, 6, 1000)]
        frame = pd.DataFrame(records)
        dated = dated_rows(frame, "000690.SZ", pd.Timestamp("2026-11-06"))
        current = dated.loc[dated["_period"] == pd.Timestamp("2026-09-30")].iloc[0]
        history = assemble_history(dated, current, "A股")
        self.assertEqual(history["可比同季度年度数"], 4)
        comparable = history["同口径年度对照"]
        revenue = next(item for item in comparable if item["指标"] == "收入")
        self.assertEqual(revenue["报告期"], "2026-09-30")
        self.assertEqual(revenue["基期"], "2025-09-30")
        self.assertEqual(revenue["类型"], "A股CNY年初累计绝对额")

    def test_post_snapshot_release_is_not_in_history(self):
        records = [a_record(2026, 9, 1300, notice="2026-11-07"),
                   a_record(2026, 6, 1000, notice="2026-08-01")]
        dated = dated_rows(pd.DataFrame(records), "000690.SZ", pd.Timestamp("2026-11-06"))
        self.assertNotIn("2026-09-30", [str(x.date()) for x in dated["_period"]])

    def test_ambiguous_latest_period_is_not_published(self):
        original = a_record(2026, 9, 1200)
        changed = {**original, "TOTALOPERATEREVE": 900}
        dated = dated_rows(pd.DataFrame([original, changed]), "000690.SZ",
                           pd.Timestamp("2026-11-06"))
        history = assemble_history(dated, dated.iloc[0], "A股")
        self.assertIn("无唯一", history["状态"])

    def test_one_cashflow_request_yields_asof_history_without_future(self):
        rows = pd.DataFrame([
            {"SECUCODE": "000690.SZ", "REPORT_DATE": "2026-09-30",
             "NOTICE_DATE": "2026-10-29", "CURRENCY": "人民币",
             "NETCASH_OPERATE": 500},
            {"SECUCODE": "000690.SZ", "REPORT_DATE": "2025-09-30",
             "NOTICE_DATE": "2025-10-25", "CURRENCY": "人民币",
             "NETCASH_OPERATE": -200},
            {"SECUCODE": "000690.SZ", "REPORT_DATE": "2024-09-30",
             "NOTICE_DATE": "2024-10-25", "CURRENCY": "人民币",
             "NETCASH_OPERATE": None},
            {"SECUCODE": "000690.SZ", "REPORT_DATE": "2023-09-30",
             "NOTICE_DATE": "2026-11-07", "CURRENCY": "人民币",
             "NETCASH_OPERATE": 300},
        ])
        current = pd.Series({"_period": pd.Timestamp("2026-09-30"),
                             "CURRENCY": "人民币"})
        with patch("finance.ak.stock_cash_flow_sheet_by_report_em",
                   return_value=rows) as api:
            amount, history = cashflow(
                False, "000690", "000690.SZ", current, pd.Timestamp("2026-11-06")
            )
        api.assert_called_once_with(symbol="SZ000690")
        self.assertEqual(amount, 500)
        self.assertEqual(history, {"2026-09-30": "500", "2025-09-30": "-200"})

    def test_hk_only_ratios_with_matching_actual_start_and_end(self):
        records = []
        for year, start in ((2026, "2026-01-01"), (2025, "2025-01-01")):
            records.append({
                "SECUCODE": "00585.HK", "REPORT_DATE": f"{year}-06-30",
                "NOTICE_DATE": f"{year}-08-01", "CURRENCY": "HKD",
                "START_DATE": start, "DATE_TYPE_CODE": "002",
                "OPERATE_INCOME": 900000000, "HOLDER_PROFIT": 300000,
                "OPERATE_INCOME_YOY": 3.2, "HOLDER_PROFIT_YOY": -8.1,
                "ROE_AVG": 6, "DEBT_ASSET_RATIO": 33,
            })
        dated = dated_rows(pd.DataFrame(records), "00585.HK", pd.Timestamp("2026-11-06"))
        history = assemble_history(dated, dated.iloc[0], "港股")
        self.assertEqual(history["可比同季度年度数"], 2)
        self.assertFalse(any("收入" in r["指标"] and "收入同比" not in r["指标"]
                             for r in history["记录"]))
        self.assertIn("原币金额暂不采用", history["限制"])


if __name__ == "__main__":
    unittest.main()
