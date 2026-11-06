"""Read currency-tagged, original HK statement lines for a specific fiscal period."""
from decimal import Decimal, InvalidOperation

import requests


URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
COLUMNS = "SECUCODE,START_DATE,REPORT_DATE,DATE_TYPE_CODE,CURRENCY_CODE,STD_ITEM_CODE,ITEM_NAME,AMOUNT"
STATEMENTS = {
    "RPT_HKF10_FN_INCOME": (
        ("营业收入", "004001001", "营业额"),
        ("归母净利润", "004025002", "股东应占溢利"),
        ("基本每股收益", "004027002", "每股基本盈利"),
    ),
    "RPT_HKSK_FN_CASHFLOW": (("经营活动现金流量净额", "003999", "经营业务现金净额"),),
}


def _statement(report, symbol, end):
    response = requests.get(
        URL,
        params={
            "reportName": report, "columns": COLUMNS,
            "filter": f"""(SECUCODE="{symbol}")(REPORT_DATE='{end}')""",
            "pageSize": "500", "source": "F10", "client": "PC",
        },
        timeout=(3, 10),
    )
    response.raise_for_status()
    body = response.json(parse_float=Decimal)
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict) or not body.get("success") or result.get("pages") != 1:
        raise ValueError("无完整报表")
    rows = result.get("data")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError("无法识别报表")
    return rows


def fetch_hk_original(code, start, end, period_type, currency):
    """Return display metrics and per-item omissions, with ISO start/end dates."""
    metrics, missing = {}, {}
    symbol = code + ".HK"
    for report, fields in STATEMENTS.items():
        try:
            rows = _statement(report, symbol, end)
        except (requests.RequestException, ValueError) as exc:
            for label, _, _ in fields:
                missing[label] = f"原币报表不可用（{type(exc).__name__}）"
            continue
        period = [r for r in rows if r.get("SECUCODE") == symbol
                  and str(r.get("REPORT_DATE")).split(" ")[0] == end
                  and str(r.get("START_DATE")).split(" ")[0] == start
                  and r.get("DATE_TYPE_CODE") == period_type]
        for label, item_code, item_name in fields:
            matches = [r for r in period if r.get("STD_ITEM_CODE") == item_code]
            if len(matches) != 1 or matches[0].get("ITEM_NAME") != item_name:
                missing[label] = "缺少唯一匹配的同期间原币科目"
                continue
            row = matches[0]
            if row.get("CURRENCY_CODE") != currency:
                missing[label] = "原币科目币种与报告币种不一致"
                continue
            try:
                amount = Decimal(str(row.get("AMOUNT")))
            except InvalidOperation:
                amount = Decimal("NaN")
            if not amount.is_finite():
                missing[label] = "原币金额为空或非有限数值"
                continue
            unit = currency + ("/股" if label == "基本每股收益" else "")
            basis = "报告期间" if label == "基本每股收益" else "期间合计"
            metrics[label] = f"{amount:f} {unit}（{basis}）"
    return metrics, missing
