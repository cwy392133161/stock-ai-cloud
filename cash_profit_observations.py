"""Descriptive contradictions in already validated A-share historical evidence.

Consolidated operating cash flow and profit attributable to owners have different
ownership scopes. This module never treats their ratio as cash conversion.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
from financial_timing import provider_date, timing_evidence, validated_cashflow


CASH = "经营现金流净额"
PROFIT = "归母净利润"
AMOUNT_TYPE = "A股CNY年初累计绝对额"


def _number(raw):
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def cash_profit_observation(finance, as_of=None):
    """Return a sourced observation/checkpoint, or nothing on incomplete evidence."""
    snapshot = provider_date(as_of)
    if not snapshot:
        return None
    history = finance.get("历史财务") or {}
    if (finance.get("状态") not in ("可用", "部分可用")
            or finance.get("报告币种") != "CNY"
            or history.get("状态") != "部分历史记录可用"):
        return None
    pairs = {}
    for name in (CASH, PROFIT):
        matches = [x for x in history.get("同口径年度对照", [])
                   if x.get("指标") == name]
        if len(matches) != 1 or matches[0].get("类型") != AMOUNT_TYPE:
            return None
        pairs[name] = matches[0]
    cash, profit = pairs[CASH], pairs[PROFIT]
    current, previous = cash.get("报告期"), cash.get("基期")
    if (current != finance.get("报告期")
            or (current, previous) != (profit.get("报告期"), profit.get("基期"))):
        return None
    try:
        new, old = date.fromisoformat(current), date.fromisoformat(previous)
    except (ValueError, TypeError):
        return None
    if (new.year != old.year + 1 or (new.month, new.day) != (old.month, old.day)
            or (new.month, new.day) not in ((3, 31), (6, 30), (9, 30), (12, 31))
            or finance.get("期间") != f"{new.year} 年初至报告期末（累计）"):
        return None
    # Verify each comparison against its unique, dated original history record.
    values = {}
    for period, side in ((current, "本期"), (previous, "对比期")):
        records = [r for r in history.get("记录", []) if r.get("报告期") == period]
        if len(records) != 1:
            return None
        record = records[0]
        if (record.get("币种") != "CNY"
                or record.get("期间") != period[:4] + "-01-01 至 " + period + "（年初累计）"
                or not record.get("披露时点依据")):
            return None
        timing = timing_evidence(record.get("日期字段"), period, snapshot)
        if not timing or timing["披露时点依据"] != record["披露时点依据"]:
            return None
        cash_source = validated_cashflow(record.get("经营现金流来源"), period, "CNY", snapshot)
        if not cash_source:
            return None
        for name in (CASH, PROFIT):
            value = _number(pairs[name].get(side))
            if value is None or value != _number((record.get("指标") or {}).get(name)):
                return None
            if name == CASH and value != _number(cash_source.get("金额")):
                return None
            values[(name, side)] = value
    cn, co = values[(CASH, "本期")], values[(CASH, "对比期")]
    pn, po = values[(PROFIT, "本期")], values[(PROFIT, "对比期")]
    if pn > 0 and po > 0 and cn < 0 and co < 0:
        headline = "两个同报告季观察期均为归母盈利、经营现金净流出（不代表期间持续净流出）"
    elif pn > po and cn < co:
        headline = "同报告季跨年对照中，归母净利润上升而经营现金流净额下降"
    elif pn > 0 and cn < 0:
        headline = "本期归母盈利与经营现金净流出并存"
    else:
        return None
    detail = (f"{previous} → {current}：归母净利润 {po:,.2f} → {pn:,.2f} CNY；"
              f"经营现金流净额 {co:,.2f} → {cn:,.2f} CNY")
    return {
        "观察": (headline + "。" + detail + "。第三方财务历史计算观察；"
               "经营现金流为合并口径，归母净利润为归属于母公司股东口径，"
               "不是同一权益归属范围；不据此推断成因或现金转化率"),
        "验证点": "下期同报告季核对归母净利润与合并经营现金流净额的方向，并查阅现金流变动披露；不以两者差额推断原因",
    }
