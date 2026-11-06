import json
import unittest
from unittest.mock import patch

from disclosures import directory, fetch_disclosures


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        directory.cache_clear()
        self.addCleanup(directory.cache_clear)

    @patch("disclosures.read_json")
    def test_exact_hk_counter_and_trusted_document(self, read):
        base = dict(TITLE="业绩公告", DATE_TIME="06/11/2026 18:30",
                    FILE_LINK="/listedco/report.pdf", STOCK_CODE="09988<br/>89988")
        rows = [base, dict(base, STOCK_CODE="09988", TITLE="其他柜台"),
                dict(base, FILE_LINK="//other.example/report.pdf"), base]
        read.side_effect = [[{"c": "89988", "i": 17}], {"result": json.dumps(rows)}]
        evidence = fetch_disclosures("港股", "89988", "2026-11-06")
        self.assertEqual(read.call_args.kwargs["query"]["stockId"], 17)
        self.assertEqual(evidence["公告"], [{"日期": "2026-11-06", "标题": "业绩公告",
                                       "链接": "https://www1.hkexnews.hk/listedco/report.pdf"}])

    @patch("disclosures.read_json")
    def test_cn_prefix_and_document(self, read):
        read.side_effect = [{"stockList": [{"code": "000690", "orgId": "mock"}]},
                            {"announcements": [{"secCode": "000690", "announcementTime": 1793894400000,
                                                "announcementTitle": "<em>季报</em>",
                                                "adjunctUrl": "finalpage/report.pdf"}]}]
        evidence = fetch_disclosures("A股", "sz000690", "2026-11-06")
        self.assertEqual(read.call_args.kwargs["form"]["stock"], "000690,mock")
        self.assertEqual(evidence["公告"][0]["标题"], "季报")

    @patch("disclosures.read_json")
    def test_empty_and_failed_queries_have_distinct_status(self, read):
        read.side_effect = [[{"c": "09988", "i": 23}], {"result": "[]"}, TimeoutError()]
        empty = fetch_disclosures("港股", "09988", "2026-11-06")
        failed = fetch_disclosures("港股", "09988", "2026-11-06")
        self.assertIn("未检索到", empty["状态"])
        self.assertIn("未核实", failed["状态"])
