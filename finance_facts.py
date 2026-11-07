"""Deterministic comparisons of the dated amounts supplied by finance.py."""
from datetime import date
from decimal import Decimal
import re


CASH = "经营活动现金流量净额"
AMOUNT = re.compile(
    r"(?P<number>[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?) "
    r"(?P<currency>[A-Z]{3})（(?P<basis>累计|期间合计|单季|单季度)）"
)


def _context(finance):
    """Validate the snapshot's actual start/end and accepted flow bases."""
    end = date.fromisoformat(finance["报告期"])
    period = finance["期间"]
    if period == f"{end.year} 年初至报告期末（累计）":
        return f"{end.year} 年初至 {end.isoformat()}（累计）", {"累计", "期间合计"}
    interval = re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2}) 至 ([0-9]{4}-[0-9]{2}-[0-9]{2})"
        r"（(按实际期间合计|单季|单季度)）", period
    )
    if interval and interval[2] == end.isoformat() and date.fromisoformat(interval[1]) <= end:
        bases = {"期间合计"} if interval[3] == "按实际期间合计" else {"单季", "单季度"}
        return period, bases
    raise ValueError("期间不明确")


def _amount(finance, label, bases):
    values = finance.get("指标")
    if not isinstance(values, dict) or label in (finance.get("缺失") or {}):
        return None
    raw = values.get(label)
    match = AMOUNT.fullmatch(raw) if isinstance(raw, str) else None
    if match and match["currency"] == finance.get("报告币种") and match["basis"] in bases:
        return Decimal(match["number"].replace(",", ""))
    return None


def financial_comparison(finance) -> str:
    """Summarize usable amounts only. Use signed net cash in the revenue comparison."""
    if finance.get("状态") == "公司财务分析不适用":
        return "该产品不适用公司财务比较。"
    if finance.get("状态") not in ("可用", "部分可用") or not isinstance(finance.get("指标"), dict):
        return "财务数据不可用，无法比较利润、经营现金流和营收。"
    try:
        period, bases = _context(finance)
    except (ValueError, TypeError, KeyError):
        return "报告期或期间口径未核实，无法比较利润、经营现金流和营收。"

    currency = finance.get("报告币种")
    profit = _amount(finance, "归母净利润", bases)
    cash = _amount(finance, CASH, bases)
    facts = []
    if profit is None:
        facts.append("归母盈亏待核实")
    elif profit == 0:
        facts.append("归母净利润为零")
    else:
        direction = "归母盈利" if profit > 0 else "归母亏损"
        facts.append(f"{direction} {profit.copy_abs():,f} {currency}")
    if cash is None:
        facts.append("经营现金净流向待核实")
    elif cash == 0:
        facts.append("经营活动现金流量净额为零")
    else:
        direction = "净流入" if cash > 0 else "净流出"
        facts.append(f"经营活动现金{direction} {cash.copy_abs():,f} {currency}")

    # Preserve the provider's precise revenue label (A-share total / HK revenue).
    revenue_label = "营业总收入" if "营业总收入" in (finance.get("指标") or {}) else "营业收入"
    revenue = _amount(finance, revenue_label, bases)
    if cash is not None and revenue is not None:
        relation = "等于" if cash == revenue else ("高于" if cash > revenue else "低于")
        comparison = f"按有符号金额比较，经营现金流净额{relation}同期{revenue_label} {revenue:,f} {currency}。"
    else:
        comparison = "可比金额不足，无法比较经营现金流净额与同期营收。"
    return period + "，" + "，".join(facts) + "。" + comparison
