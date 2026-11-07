"""Pagination and no-false-completeness for official announcement indexes."""
import unittest
from datetime import date
from unittest.mock import patch

import disclosures


class DisclosurePagingTests(unittest.TestCase):
    def make_cn(self, start, count):
        return [{
            "secCode": "000690", "announcementTime": 1793452800000,
            "announcementTitle": "报告公告",
            "adjunctUrl": f"/{i}.pdf",
        } for i in range(start, start + count)]

    @patch("disclosures.directory", return_value={"000690": "test-org"})
    @patch("disclosures.read_json")
    def test_mainland_fetches_second_page_of_report_titles(self, api, _):
        api.side_effect = [
            {"totalAnnouncement": 31, "announcements": self.make_cn(0, 30)},
            {"totalAnnouncement": 31, "announcements": self.make_cn(30, 1)},
        ]
        received = list(disclosures.cn_rows("000690", date(2026, 10, 1),
                                           date(2026, 11, 6)))
        self.assertEqual(len(received), 31)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(api.call_args_list[-1].kwargs["form"]["pageNum"], 2)

    @patch("disclosures.directory", return_value={"000690": "test-org"})
    @patch("disclosures.read_json")
    def test_mainland_limited_page_count_is_explicit_error(self, api, _):
        api.side_effect = [
            {"totalAnnouncement": 200, "announcements": self.make_cn(i*30, 30)}
            for i in range(5)
        ]
        with self.assertRaisesRegex(ValueError, "未查全"):
            list(disclosures.cn_rows("000690", date(2026, 10, 1),
                                    date(2026, 11, 6)))
        self.assertEqual(api.call_count, 5)

    @patch("disclosures.directory", return_value={"00585": "test-stock"})
    @patch("disclosures.read_json")
    def test_hk_result_limit_does_not_misrepresent_complete_index(self, api, _):
        api.return_value = {"result": [
            {"STOCK_CODE": "00585", "DATE_TIME": "05/11/2026 10:25",
             "TITLE": "公告", "FILE_LINK": "/doc.pdf"} for _ in range(100)
        ]}
        with self.assertRaisesRegex(ValueError, "上限"):
            list(disclosures.hk_rows("00585", date(2026, 10, 1),
                                    date(2026, 11, 6)))
        self.assertEqual(api.call_args.kwargs["query"]["rowRange"], 100)

    @patch("disclosures.directory", return_value={"00585": "test-stock"})
    @patch("disclosures.read_json")
    def test_hk_small_result_remains_accessible(self, api, _):
        api.return_value = {"result": [
            {"STOCK_CODE": "00585", "DATE_TIME": "05/11/2026 10:25",
             "TITLE": "业绩公告", "FILE_LINK": "/doc.pdf"}
        ]}
        result = list(disclosures.hk_rows("00585", date(2026, 10, 1),
                                         date(2026, 11, 6)))
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
