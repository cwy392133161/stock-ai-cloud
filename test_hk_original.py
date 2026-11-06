import unittest
from unittest.mock import patch

import requests

from hk_original import fetch_hk_original


class HKOriginalTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "SECUCODE": "00585.HK", "START_DATE": "2026-01-01 00:00:00",
            "REPORT_DATE": "2026-06-30 00:00:00", "DATE_TYPE_CODE": "002",
            "CURRENCY_CODE": "HKD", "STD_ITEM_CODE": "004001001", "ITEM_NAME": "营业额",
            "AMOUNT": 0,
        }

    def fetch(self, income_rows):
        with patch("hk_original._statement", side_effect=[income_rows, requests.Timeout()]):
            return fetch_hk_original("00585", "2026-01-01", "2026-06-30", "002", "HKD")

    def test_zero_and_independent_statement_outage(self):
        metrics, missing = self.fetch([self.row])
        self.assertEqual(metrics["营业收入"], "0 HKD（期间合计）")
        self.assertIn("Timeout", missing["经营活动现金流量净额"])

    def test_reject_ambiguous_or_mismatched_data(self):
        for changes in ({"CURRENCY_CODE": "USD"}, {"DATE_TYPE_CODE": "001"},
                        {"START_DATE": "2026-04-01"}, {"REPORT_DATE": "2026-12-31"},
                        {"SECUCODE": "00688.HK"}, {"AMOUNT": "NaN"}, {"ITEM_NAME": "其他收入"}):
            with self.subTest(changes=changes):
                metrics, missing = self.fetch([self.row | changes])
                self.assertNotIn("营业收入", metrics)
                self.assertIn("营业收入", missing)
        self.assertIn("营业收入", self.fetch([self.row, self.row])[1])
