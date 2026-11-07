"""As-of, same-period fundamental history from the EXISTING AKShare response.

The response is already date-filtered by finance.dated_rows. We never mix
cumulative Q3 numbers with annual / quarterly results or convert HK amounts
whose original currencies have not been verified.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import re

MAX_PERIODS = 16
A_FIELDS = {
    "收入": "TOTALOPERATEREVE", "归母净利润": "PARENTNETPROFIT",
    "收入同比": "TOTALOPERATEREVETZ", "归母净利润同比": "PARENTNETPROFITTZ",
    "ROE": "ROEJQ", "资产负债率": "ZCFZL",
}
HK_FIELDS = {
    "收入同比": "OPERATE_INCOME_YOY", "归母净利润同比": "HOLDER_PROFIT_YOY",
    "ROE": "ROE_AVG", "资产负债率": "DEBT_ASSET_RATIO",
}
CURRENCIES = {"人民币": "CNY", "港元": "HKD", "美元": "USD"}
PERCENT_NAMES = {"收入同比", "归母净利润同比", "ROE", "资产负债率"}


def _decimal(value):
    if value is None:
        return None
    try:
        dec = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return dec if dec.is_finite() else None


def _iso(value):
    if hasattr(value, "date"):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _fiscal_basis(row, market):
    end = _iso(row.get("_period"))
    if not end:
        return None
    if market == "A股":
        if end[5:7] not in ("03", "06", "09", "12"):
            return None
        return end[:4] + "-01-01 至 " + end + "（年初累计）"
    start = _iso(row.get("START_DATE"))
    if not start or start > end:
        return None
    return start + " 至 " + end + "（实际报告期间）"


def _financial_row(row, market):
    end = _iso(row.get("_period"))
    basis = _fiscal_basis(row, market)
    if not (end and basis):
        return None
    currency = CURRENCIES.get(str(row.get("CURRENCY", "")).strip(),
                              str(row.get("CURRENCY", "")).strip())
    result = {"报告期": end, "期间": basis, "币种": currency if re.fullmatch(r"[A-Z]{3}", currency) else "未知"}
    fields = A_FIELDS if market == "A股" else HK_FIELDS
    amounts_allowed = market == "A股" and currency == "CNY"
    values = {}
    for label, column in fields.items():
        num = _decimal(row.get(column))
        if num is None or (label not in PERCENT_NAMES and not amounts_allowed):
            continue
        values[label] = format(num, "f")
    result["指标"] = values
    result["金额口径"] = ("A股人民币年初累计，可作同季度跨年比较"
                       if amounts_allowed else "仅使用百分比/比率；原币金额未核实")
    return result


def _comparable(a, b, market):
    if not a or not b:
        return False
    ae, be = date.fromisoformat(a["报告期"]), date.fromisoformat(b["报告期"])
    if ae.month != be.month or ae.day != be.day:
        return False
    if market == "港股":
        # Fiscal year start follows issuer reporting calendar, not January 1.
        aa = date.fromisoformat(a["期间"][:10])
        bb = date.fromisoformat(b["期间"][:10])
        if aa.month != bb.month or aa.day != bb.day:
            return False
        if abs((ae - aa).days - (be - bb).days) > 2:
            return False
    return True


def assemble_history(rows, latest, market, cashflows=None):
    """Transform provider rows into dated, defensible evidence (no network)."""
    if market not in ("A股", "港股"):
        return {"状态": "市场无效", "记录": [], "同口径年度对照": []}
    selected = []
    # datetime-backed _period has already been screened for publication timing.
    for _, frame_row in rows.sort_values("_period", ascending=False).iterrows():
        # A period end is not a release date. If the provider has no notice or
        # revision timestamp, do not claim this history was known as of the
        # market snapshot. Upstream dated_rows already excludes future dates.
        notice_date = _iso(frame_row.get("NOTICE_DATE"))
        revision_date = _iso(frame_row.get("UPDATE_DATE"))
        if not notice_date and not revision_date:
            continue
        record = _financial_row(frame_row, market)
        if record:
            record["披露时点依据"] = notice_date or revision_date
            selected.append(record)
    # Attach matching-period, dated original cash-flow statement amounts.
    # Only CNY A-share cumulative lines; HK original currency is not presumed.
    flows = cashflows or {}
    if market == "A股":
        for item in selected:
            amount = _decimal(flows.get(item["报告期"]))
            if item["币种"] == "CNY" and amount is not None:
                item["指标"]["经营现金流净额"] = format(amount, "f")
    # A duplicated period can represent incompatible revisions. Never choose
    # the first arbitrarily; only publish a period when all copies agree.
    grouped = {}
    for record in selected:
        grouped.setdefault(record["报告期"], []).append(record)
    unambiguous = {
        period: items[0] for period, items in grouped.items()
        if all(item == items[0] for item in items)
    }
    periods = sorted(unambiguous, reverse=True)[:MAX_PERIODS]
    points = [unambiguous[p] for p in periods if unambiguous[p]["指标"]]
    current = _iso(latest.get("_period"))
    if current not in unambiguous:
        return {"状态": "最新报告期无唯一可信记录", "记录": points,
                "同口径年度对照": []}
    reference = unambiguous[current]
    comparable = [x for x in points if _comparable(reference, x, market)
                  and bool(x["指标"])]
    comparable = sorted(comparable, key=lambda x: x["报告期"], reverse=True)[:4]
    states = []
    for field in ("收入", "归母净利润", "经营现金流净额", "ROE", "资产负债率", "收入同比", "归母净利润同比"):
        # Never compare two old years if the latest period lacks this metric.
        if field not in reference["指标"]:
            continue
        observed = [x for x in comparable if field in x["指标"]]
        if len(observed) >= 2:
            new, old = reference, observed[1]
            states.append({"指标": field, "本期": new["指标"][field],
                           "对比期": old["指标"][field],
                           "报告期": new["报告期"], "基期": old["报告期"],
                           "类型": ("百分比或比率" if field in PERCENT_NAMES else
                                  "A股CNY年初累计绝对额"),
                           "解释": "仅作同季度跨年对照，不代表全年增速"})
    return {
        "来源": "东方财富（AKShare 同一个财务接口返回的历史报告期）",
        "状态": "部分历史记录可用" if points else "无历史记录",
        "报告期覆盖": len(points),
        "可比同季度年度数": len(comparable),
        "记录": points,
        "同口径年度对照": states,
        "限制": ("仅保留截至快照日可获取且报告期唯一的数据；"
                "港股原币金额暂不采用（历史期无原币逐期校验）；不计算股票合理价或确定性收益预测"),
    }
