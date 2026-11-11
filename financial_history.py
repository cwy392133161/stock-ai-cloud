"""As-of, same-period fundamental history from the EXISTING AKShare response.

The response is already date-filtered by finance.dated_rows. We never mix
cumulative Q3 numbers with annual / quarterly results or convert HK amounts
whose original currencies have not been verified.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import re
from financial_timing import timing_evidence, validated_cashflow

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
        if end[5:] not in ("03-31", "06-30", "09-30", "12-31"):
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


def assemble_history(rows, latest, market, cashflows=None, *, as_of=None):
    """Transform provider rows into dated, defensible evidence (no network)."""
    if market not in ("A股", "港股"):
        return {"状态": "市场无效", "记录": [], "同口径年度对照": []}
    selected = []
    # datetime-backed _period has already been screened for publication timing.
    for _, frame_row in rows.sort_values("_period", ascending=False).iterrows():
        # A period end is not a release date. If the provider has no notice or
        # revision timestamp, do not claim this history was known as of the
        # market snapshot. Upstream dated_rows already excludes future dates.
        timing = timing_evidence(frame_row, frame_row.get("_period"), as_of)
        if not timing:
            continue
        record = _financial_row(frame_row, market)
        if record:
            record.update(timing)
            selected.append(record)
    # Attach matching-period, dated original cash-flow statement amounts.
    # Only CNY A-share cumulative lines; HK original currency is not presumed.
    flows = cashflows or {}
    if market == "A股":
        for item in selected:
            evidence = validated_cashflow(flows.get(item["报告期"]), item["报告期"], item["币种"], as_of)
            amount = _decimal(evidence.get("金额")) if evidence else None
            if item["币种"] == "CNY" and amount is not None:
                item["指标"]["经营现金流净额"] = format(amount, "f")
                item["经营现金流来源"] = {**evidence, "金额": format(amount, "f")}
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
        previous_year = str(int(current[:4]) - 1) + current[4:]
        observed = [x for x in comparable if x["报告期"] == previous_year and field in x["指标"]]
        # Missing prior-year evidence must not silently turn a two-year gap
        # into an annual comparison; older records remain visible as records.
        if observed:
            new, old = reference, observed[0]
            states.append({"指标": field, "本期": new["指标"][field],
                           "对比期": old["指标"][field],
                           "报告期": new["报告期"], "基期": old["报告期"],
                           "类型": ("百分比或比率" if field in PERCENT_NAMES else
                                  "A股CNY年初累计绝对额"),
                           "解释": "仅作相邻年度同季度对照，不代表全年增速"})
    return {
        "来源": "东方财富（AKShare 同一个财务接口返回的历史报告期）",
        "状态": "部分历史记录可用" if points else "无历史记录",
        "报告期覆盖": len(points),
        "可比同季度年度数": len(comparable),
        "记录": points,
        "同口径年度对照": states,
        "限制": ("仅保留日期字段不晚于快照且报告期唯一的数据；现金流单独校验自身披露/修订日期，"
                "日期字段来自第三方，不代表已审计的时点数据库；缺少上年同期时不作年度对照；"
                "港股原币金额暂不采用（历史期无原币逐期校验）；不计算股票合理价或确定性收益预测"),
    }
