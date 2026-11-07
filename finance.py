"""Financial snapshots from the pinned AKShare Eastmoney adapters."""
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import akshare as ak
import pandas as pd

from hk_original import fetch_hk_original
from financial_history import assemble_history
from hk_products import SOURCE_URL


# label, provider column, unit (money uses the validated reporting currency), basis
A_FIELDS = (
    ("营业总收入", "TOTALOPERATEREVE", "元", "累计"),
    ("归母净利润", "PARENTNETPROFIT", "元", "累计"),
    ("加权 ROE", "ROEJQ", "%", "报告期"),
    ("资产负债率", "ZCFZL", "%", "期末"),
    ("营业总收入同比", "TOTALOPERATEREVETZ", "%", "累计同比"),
    ("归母净利润同比", "PARENTNETPROFITTZ", "%", "累计同比"),
    ("基本每股收益", "EPSJB", "元/股", "累计"),
)
HK_FIELDS = (
    ("营业收入", "OPERATE_INCOME", "元", "期间合计"),
    ("归母净利润", "HOLDER_PROFIT", "元", "期间合计"),
    ("平均 ROE", "ROE_AVG", "%", "报告期"),
    ("资产负债率", "DEBT_ASSET_RATIO", "%", "期末"),
    ("营业收入同比", "OPERATE_INCOME_YOY", "%", "期间同比"),
    ("归母净利润同比", "HOLDER_PROFIT_YOY", "%", "期间同比"),
    ("基本每股收益", "BASIC_EPS", "元/股", "期间合计"),
)


def product_type(market, code, name):
    name = str(name).upper()
    for marker, label in (("ETN", "ETN"), ("做空", "反向产品"), ("反向", "反向产品"),
                          ("杠杆", "杠杆产品"), ("槓桿", "杠杆产品"), ("两倍做多", "杠杆产品"),
                          ("ETF", "ETF"), ("REIT", "REIT"), ("房地产投资信托", "REIT")):
        if marker in name:
            return label
    if market == "港股":
        # HKEX Stock Code Allocation Plan, including USD and RMB counters.
        prefix = int(code) // 100
        if prefix in {72, 73, 75, 77, 92, 93, 95, 97, 872, 873, 875, 877}:
            return "杠杆或反向产品"
        if prefix in {30, 31, 34, 35, 90, 91, 94, 415, 830, 831, 834, 835} or any(
            lo <= int(code) <= hi for lo, hi in ((2800, 2849), (9800, 9849), (82800, 82849))
        ):
            return "ETF"
        if 10000 <= int(code) <= 29999 or 47000 <= int(code) <= 48999:
            return "权证"
        if 49500 <= int(code) <= 69999:
            return "牛熊证"
    return None


class FinancialDataError(ValueError):
    pass


def a_exchange(code):
    if code.startswith(("4", "8", "920")):
        return "BJ"
    if code.startswith(("6", "900")):
        return "SH"
    if code.startswith(("0", "3")):
        return "SZ"
    raise FinancialDataError("无法识别 A 股交易所")


def dated_rows(frame, symbol, as_of):
    if frame is None or frame.empty:
        raise FinancialDataError("接口无记录")
    if not {"SECUCODE", "REPORT_DATE"}.issubset(frame.columns):
        raise FinancialDataError("缺少证券标识或实际报告期")
    rows = frame.loc[frame["SECUCODE"] == symbol].copy()
    # Parse provider dates as dates. Pandas treats integer YYYYMMDD as nanoseconds.
    rows["_period"] = pd.to_datetime(rows["REPORT_DATE"].astype("string"), format="mixed", errors="coerce").dt.normalize()
    rows = rows.loc[rows["_period"] <= as_of]
    for field in ("NOTICE_DATE", "UPDATE_DATE"):
        if field in rows:
            published = pd.to_datetime(rows[field].astype("string"), format="mixed", errors="coerce")
            rows = rows.loc[rows[field].isna() | (published.dt.normalize() <= as_of)]
    if rows.empty:
        raise FinancialDataError("没有匹配证券且日期有效的记录")
    return rows


def metric(raw, unit, money_issue):
    try:
        number = Decimal(str(raw))
    except InvalidOperation:
        return None, "字段为空或非数值"
    if not number.is_finite():
        return None, "字段为空或非有限数值"
    if unit != "%" and money_issue:
        return None, money_issue
    return format(number, "f"), None


def cashflow(hk, code, symbol, row, as_of):
    if hk:
        frame = ak.stock_financial_hk_report_em(stock=code, symbol="现金流量表", indicator="报告期")
    else:
        frame = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol[-2:] + code)
    rows = dated_rows(frame, symbol, as_of)
    rows = rows.loc[rows["_period"] == row["_period"]]
    if hk:
        rows = rows.loc[(rows["STD_ITEM_NAME"] == "经营业务现金净额") &
                        (rows["START_DATE"] == row["START_DATE"]) &
                        (rows["DATE_TYPE_CODE"] == row["DATE_TYPE_CODE"])]
        field = "AMOUNT"
    else:
        rows = rows.loc[rows["CURRENCY"] == row["CURRENCY"]]
        field = "NETCASH_OPERATE"
    if len(rows) != 1:
        raise FinancialDataError("没有唯一匹配的同口径现金流记录")
    return rows.iloc[0][field]


def fetch_financial(market, code, name="", as_of=None, *, directory=None):
    hk = market == "港股"
    width = 5 if hk else 6
    code = str(code).strip()
    if market == "A股":
        tagged = re.fullmatch(r"(sh|sz|bj)([0-9]{6})", code, flags=re.IGNORECASE)
        if tagged:
            exchange, code = tagged.groups()
            try:
                expected = a_exchange(code)
            except FinancialDataError:
                return {"状态": "无法识别 A 股交易所"}
            if exchange.upper() != expected:
                return {"状态": "行情代码的交易所与编号不匹配"}
    if market not in ("A股", "港股") or not re.fullmatch(r"[0-9]{1,%d}" % width, code):
        return {"状态": "市场或证券代码无效"}
    code = code.zfill(width)
    listed = hk and directory is not None and code in directory
    product = directory[code] if listed else product_type(market, code, name)
    if product:
        return {"产品类型": product, "分类来源": SOURCE_URL if listed else "行情名称或产品代码段",
                "状态": "公司财务分析不适用"}
    result = {"来源": "东方财富（AKShare 主要指标、现金流量表，按报告期）"}
    as_of = pd.Timestamp(as_of or datetime.now(timezone(timedelta(hours=8))).date()).normalize()
    try:
        if hk:
            symbol = code + ".HK"
            frame = ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator="报告期")
        else:
            symbol = code + "." + a_exchange(code)
            frame = ak.stock_financial_analysis_indicator_em(symbol=symbol, indicator="按报告期")
        rows = dated_rows(frame, symbol, as_of)
        latest = rows.loc[rows["_period"] == rows["_period"].max()].drop_duplicates()
        if len(latest) != 1:
            raise FinancialDataError("同一报告期有多条记录，口径不唯一")
        row = latest.iloc[0]
    except Exception as exc:
        result["状态"] = str(exc) if isinstance(exc, FinancialDataError) else "财务接口不可用（" + type(exc).__name__ + "）"
        return result

    currency = str(row.get("CURRENCY", "")).strip()
    currency = {"人民币": "CNY", "港元": "HKD", "美元": "USD"}.get(currency, currency)
    money_issue = None
    if not re.fullmatch("[A-Z]{3}", currency):
        money_issue = "金额币种缺失"
    elif hk and currency != "CNY":
        # In 1.20.34 CURRENCY is overwritten with the filing currency, even
        # when the provider's amounts differ from the original-currency filing.
        money_issue = "接口金额的折算币种未核实"
    result.update({"报告期": str(row["_period"].date()), "报告币种": currency if re.fullmatch("[A-Z]{3}", currency) else "未知"})
    notice = pd.to_datetime(str(row.get("NOTICE_DATE", "")), errors="coerce")
    result["披露日期"] = "接口未提供" if pd.isna(notice) else str(notice.date())
    if hk:
        start = pd.to_datetime(str(row.get("START_DATE", "")), errors="coerce")
        if pd.isna(start) or start > row["_period"]:
            return {**result, "状态": "期间起点缺失或无效，未采用指标"}
        result["期间"] = f"{start.date()} 至 {row['_period'].date()}（按实际期间合计）"
    else:
        result["期间"] = f"{row['_period'].year} 年初至报告期末（累计）"
    fields = HK_FIELDS if hk else A_FIELDS
    metrics = {}
    missing = {}
    for label, field, unit, basis in fields:
        value, issue = metric(row.get(field), unit, money_issue)
        if issue:
            missing[label] = issue
        else:
            suffix = "%" if unit == "%" else currency + ("/股" if unit == "元/股" else "")
            metrics[label] = f"{value} {suffix}（{basis}）"
    label = "经营活动现金流量净额"
    try:
        if money_issue:
            missing[label] = money_issue
        else:
            value, issue = metric(cashflow(hk, code, symbol, row, as_of), "元", None)
            if issue:
                missing[label] = issue
            else:
                metrics[label] = f"{value} {currency}（期间合计）"
    except Exception as exc:
        missing[label] = str(exc) if isinstance(exc, FinancialDataError) else f"现金流量表不可用（{type(exc).__name__}）"
    if hk and currency != "CNY" and re.fullmatch("[A-Z]{3}", currency):
        original, unavailable = fetch_hk_original(
            code, str(start.date()), result["报告期"], str(row.get("DATE_TYPE_CODE", "")), currency
        )
        metrics.update(original)
        for recovered in original:
            missing.pop(recovered, None)
        missing.update(unavailable)
        if original:
            result["原币金额来源"] = "东方财富原币财务报表（按科目匹配，以原币单位展示）"
    result["状态"] = ("部分可用" if missing else "可用") if metrics else "无可用指标"
    result["指标"] = metrics
    try:
        result["历史财务"] = assemble_history(rows, row, market)
    except (ValueError, TypeError, KeyError) as exc:
        # History is optional; it must never break existing current-period facts.
        result["历史财务"] = {"状态": f"历史数据不可核实（{type(exc).__name__}）", "记录": [], "同口径年度对照": []}
    if missing:
        result["缺失"] = missing
    return result
