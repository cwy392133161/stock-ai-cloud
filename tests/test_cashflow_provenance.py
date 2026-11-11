"""Synthetic offline fixtures: date provenance must survive the entire pipeline."""
import unittest
from unittest.mock import patch

import pandas as pd

from cash_profit_observations import cash_profit_observation
from finance import fetch_financial
from financial_history import assemble_history
from financial_timing import CASHFLOW_SOURCE, provider_date, timing_evidence


SNAPSHOT = "2026-11-06"


def indicator_rows():
    return [dict(SECUCODE="000690.SZ", REPORT_DATE=f"{y}-09-30",
                 NOTICE_DATE=f"{y}-10-29", UPDATE_DATE=f"{y}-10-30",
                 CURRENCY="CNY", PARENTNETPROFIT=p, TOTALOPERATEREVE=1000)
            for y, p in ((2026, 380), (2025, 100))]


def cash_rows():
    return [dict(SECUCODE="000690.SZ", REPORT_DATE=f"{y}-09-30",
                 NOTICE_DATE=f"{y}-10-28", UPDATE_DATE=f"{y}-10-31",
                 CURRENCY="CNY", NETCASH_OPERATE=c)
            for y, c in ((2026, 200), (2025, 400))]


def fetch(indicators, flows):
    with patch("finance.ak.stock_financial_analysis_indicator_em", return_value=pd.DataFrame(indicators)), \
            patch("finance.ak.stock_cash_flow_sheet_by_report_em", return_value=pd.DataFrame(flows)) as api:
        result = fetch_financial("A股", "000690", as_of=SNAPSHOT)
        api.assert_called_once_with(symbol="SZ000690")
        return result


class CashflowProvenanceTests(unittest.TestCase):
    def test_distinct_dates_survive_end_to_end_and_still_allow_observation(self):
        result = fetch(indicator_rows(), cash_rows())
        self.assertEqual(result["经营现金流来源"]["披露时点依据"], "2026-10-31")
        for record in result["历史财务"]["记录"]:
            year = record["报告期"][:4]
            source = record["经营现金流来源"]
            self.assertEqual(record["披露时点依据"], year + "-10-30")
            self.assertEqual(source["日期字段"], {"NOTICE_DATE": year + "-10-28", "UPDATE_DATE": year + "-10-31"})
            self.assertEqual(source["披露时点依据"], year + "-10-31")
            self.assertEqual(source["来源"], CASHFLOW_SOURCE)
            self.assertEqual(source["金额"], record["指标"]["经营现金流净额"])
        self.assertIn("归母净利润上升", cash_profit_observation(result, SNAPSHOT)["观察"])

    def test_no_cash_dates_cannot_borrow_indicator_dates(self):
        for dates in ({}, {"NOTICE_DATE": None, "UPDATE_DATE": None},
                      {"NOTICE_DATE": float("nan"), "UPDATE_DATE": pd.NaT}):
            with self.subTest(dates=dates):
                flows = [{k: v for k, v in r.items() if k not in ("NOTICE_DATE", "UPDATE_DATE")} for r in cash_rows()]
                for row in flows:
                    row.update(dates)
                result = fetch(indicator_rows(), flows)
                self.assertNotIn("经营活动现金流量净额", result["指标"])
                self.assertTrue(all("经营现金流净额" not in r["指标"] for r in result["历史财务"]["记录"]))
                self.assertIsNone(cash_profit_observation(result, SNAPSHOT))

    def test_bad_cash_dates_fail_closed_for_each_comparison_side(self):
        for index in (0, 1):
            for field, value in (
                ("NOTICE_DATE", "bad"), ("NOTICE_DATE", "2024-01-01"),
                ("NOTICE_DATE", "2026-11-07"), ("UPDATE_DATE", "bad"),
                ("UPDATE_DATE", "2024-01-01"), ("UPDATE_DATE", "2026-11-07"),
                ("UPDATE_DATE_1", "2026-11-07"),
            ):
                with self.subTest(index=index, field=field, value=value):
                    flows = cash_rows()
                    flows[index][field] = value
                    result = fetch(indicator_rows(), flows)
                    self.assertIsNone(cash_profit_observation(result, SNAPSHOT))
                    period = flows[index]["REPORT_DATE"]
                    record = next(r for r in result["历史财务"]["记录"] if r["报告期"] == period)
                    self.assertNotIn("经营现金流净额", record["指标"])

    def test_revision_only_and_numeric_dates_are_usable(self):
        flows = cash_rows()
        for row in flows:
            del row["NOTICE_DATE"]
            row["UPDATE_DATE"] = int(row["UPDATE_DATE"].replace("-", ""))
        result = fetch(indicator_rows(), flows)
        self.assertIsNotNone(cash_profit_observation(result, SNAPSHOT))

    def test_zero_cash_and_ambiguous_rows(self):
        flows = cash_rows()
        flows[0]["NETCASH_OPERATE"] = 0
        self.assertIsNotNone(cash_profit_observation(fetch(indicator_rows(), flows), SNAPSHOT))
        flows.append({**flows[1], "NETCASH_OPERATE": 900})
        self.assertIsNone(cash_profit_observation(fetch(indicator_rows(), flows), SNAPSHOT))

    def test_latest_notice_cannot_hide_later_revision(self):
        rows = indicator_rows()
        rows[1]["UPDATE_DATE"] = "2026-11-07"
        self.assertIsNone(cash_profit_observation(fetch(rows, cash_rows()), SNAPSHOT))

    def test_missing_prior_year_cannot_fall_back_to_two_year_gap(self):
        rows, flows = indicator_rows(), cash_rows()
        for row in (rows[1], flows[1]):
            for field in ("REPORT_DATE", "NOTICE_DATE", "UPDATE_DATE"):
                row[field] = row[field].replace("2025", "2024")
        result = fetch(rows, flows)
        self.assertEqual(len(result["历史财务"]["记录"]), 2)
        self.assertFalse(result["历史财务"]["同口径年度对照"])
        self.assertIsNone(cash_profit_observation(result, SNAPSHOT))

    def test_missing_prior_year_metric_cannot_use_older_metric(self):
        rows, flows = indicator_rows(), cash_rows()
        rows.append({k: v.replace("2025", "2024") if isinstance(v, str) else v for k, v in rows[1].items()})
        rows[1]["PARENTNETPROFIT"] = None
        result = fetch(rows, flows)
        self.assertFalse(any(x["指标"] == "归母净利润" for x in result["历史财务"]["同口径年度对照"]))

    def test_assembly_rejects_legacy_cash_amounts_and_requires_cutoff(self):
        from finance import dated_rows
        rows = dated_rows(pd.DataFrame(indicator_rows()), "000690.SZ", pd.Timestamp(SNAPSHOT))
        history = assemble_history(rows, rows.iloc[0], "A股", {"2026-09-30": "200"}, as_of=SNAPSHOT)
        self.assertTrue(all("经营现金流净额" not in r["指标"] for r in history["记录"]))
        self.assertFalse(assemble_history(rows, rows.iloc[0], "A股")["记录"])

    def test_provider_date_only_accepts_calendar_formats(self):
        for raw in (20261029, "20261029", "2026-10-29", "2026-10-29 00:00:00", pd.Timestamp("2026-10-29")):
            self.assertEqual(provider_date(raw), "2026-10-29")
        for raw in (True, 20261029.0, 1720000000, "2026-10-29 garbage", "2026-02-30", pd.NaT):
            self.assertIsNone(provider_date(raw))
        self.assertIsNone(timing_evidence({"NOTICE_DATE": "2026-10-29", "UPDATE_DATE": "bad"}, "2026-09-30", SNAPSHOT))

    def test_non_quarter_end_is_not_a_cumulative_reporting_period(self):
        rows = indicator_rows()
        rows[0]["REPORT_DATE"] = "2026-09-29"
        result = fetch(rows, cash_rows())
        self.assertFalse(any(r["报告期"] == "2026-09-29" for r in result["历史财务"]["记录"]))


if __name__ == "__main__":
    unittest.main()
