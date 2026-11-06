import unittest
from io import BytesIO
from unittest.mock import patch
from zipfile import ZipFile

import requests
from openpyxl import Workbook

from hk_products import _read_directory, stock_directory


def directory_bytes(rows, false_dimension=False):
    book = Workbook()
    sheet = book.active
    sheet.title = "ListOfSecurities"
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    book.save(output)
    book.close()
    if not false_dimension:
        return output.getvalue()
    # Reproduce HKEX's understated sheet metadata with ordinary workbook data.
    revised = BytesIO()
    with ZipFile(output) as src, ZipFile(revised, "w") as dst:
        for name in src.namelist():
            payload = src.read(name)
            if name == "xl/worksheets/sheet1.xml":
                payload = payload.replace(f'A1:D{len(rows)}'.encode(), b"A1:D3")
            dst.writestr(name, payload)
    return revised.getvalue()


HEADER = ("Stock Code", "Name of Securities", "Category", "Sub-Category")


class ProductDirectoryTests(unittest.TestCase):
    def test_full_directory_overrides_name_heuristics(self):
        rows = [("List of Securities",), ("Updated",), HEADER,
                ("00700", "COMPANY WITH ETF IN ITS NAME", "Equity", "Equity Securities (Main Board)"),
                ("00405", "YUEXIU", "Real Estate Investment Trusts", None),
                ("02461", "SYNAGISTICS W29", "Equity Warrants (Main Board)", None),
                ("07515", "PRODUCT", "Exchange Traded Products", "Leveraged and Inverse"),
                (2800, "TRACKER", "Exchange Traded Products", "Exchange Traded Funds"),
                ("01687", "NOTE", "Debt Securities", None),
                ("00010", "UNRECOGNISED", "Other", "New category")]
        self.assertEqual(_read_directory(directory_bytes(rows, false_dimension=True)), {
            "00700": None, "00405": "REIT", "02461": "股本权证", "07515": "杠杆或反向产品",
            "02800": "ETF", "01687": "债务证券",
        })

    def test_changed_schema(self):
        with self.assertRaises(ValueError):
            _read_directory(directory_bytes([("Code", "Name")]))

    def test_timeout_falls_back_once(self):
        stock_directory.cache_clear()
        self.addCleanup(stock_directory.cache_clear)
        with patch("hk_products.requests.get", side_effect=requests.Timeout) as get:
            self.assertEqual(stock_directory(), {})
            self.assertEqual(stock_directory(), {})
        get.assert_called_once()


if __name__ == "__main__":
    unittest.main()
