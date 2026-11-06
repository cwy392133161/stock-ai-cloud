"""Security types in HKEX's published list of securities, indexed by counter code."""
from functools import lru_cache
from io import BytesIO
from itertools import islice

import requests
from openpyxl import load_workbook


SOURCE_URL = "https://www.hkex.com.hk/eng/services/trading/securities/securitieslists/ListOfSecurities.xlsx"

# Only explicit exchange categories are authoritative. Omit unknown categories
# so the caller can use its ordinary name/code fallback.
PRODUCTS = {
    ("Exchange Traded Products", "Exchange Traded Funds"): "ETF",
    ("Exchange Traded Products", "Leveraged and Inverse"): "杠杆或反向产品",
    ("Exchange Traded Products", "Exchange Traded Notes"): "ETN",
    ("Real Estate Investment Trusts", None): "REIT",
    ("Debt Securities", None): "债务证券",
    ("Derivative Warrants", None): "衍生权证",
    ("Equity Warrants (Main Board)", None): "股本权证",
    ("Equity Warrants (GEM)", None): "股本权证",
    ("Callable Bull/Bear Contracts", None): "牛熊证",
    ("Equity", "Depositary Receipts"): "存托凭证",
}

COMPANIES = {
    ("Equity", "Equity Securities (Main Board)"),
    ("Equity", "Equity Securities (GEM)"),
    ("Equity", "Investment Companies"),
}


def _read_directory(contents):
    book = load_workbook(BytesIO(contents), read_only=True, data_only=True)
    try:
        sheet = book["ListOfSecurities"]
        # The official workbook declares a dimension ending at row 8 even
        # though the XML contains the full list (over 17,000 counters).
        sheet.reset_dimensions()
        rows = sheet.iter_rows(min_col=1, max_col=4, values_only=True)
        header = ("Stock Code", "Name of Securities", "Category", "Sub-Category")
        if not any(row == header for row in islice(rows, 10)):
            raise ValueError("无法识别证券名录表头")
        directory = {}
        for raw_code, _name, category, subtype in rows:
            code = str(raw_code).strip()
            if not (code.isascii() and code.isdigit() and len(code) <= 5):
                continue
            code = code.zfill(5)
            key = (category, subtype)
            if key in COMPANIES:
                label = None
            elif key in PRODUCTS:
                label = PRODUCTS[key]
            else:
                continue
            if code in directory and directory[code] != label:
                raise ValueError("同一证券有矛盾的类型")
            directory[code] = label
        if not directory:
            raise ValueError("没有识别出证券类型")
        return directory
    finally:
        book.close()


@lru_cache(maxsize=1)
def stock_directory() -> dict[str, str | None]:
    """Load once per report process. Missing keys fall back to local checks."""
    try:
        response = requests.get(SOURCE_URL, timeout=(4, 10))
        response.raise_for_status()
        return _read_directory(response.content)
    except Exception as exc:
        # The optional data provider must not stop the daily report. Do not
        # publish request details or third-party content in Actions logs.
        print(f"港交所证券名录不可用（{type(exc).__name__}），使用名称与代码识别。", flush=True)
        return {}
