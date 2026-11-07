"""Deterministic research brief for each screened issuer.

These are questions and source-coverage observations, NOT a price forecast.
Amounts, profit and cash-flow direction are handled by finance_facts.py.
"""
import re
from decimal import Decimal, InvalidOperation

PERCENT = re.compile(
    r"^([+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)"
    r"\s*%\s*（(?:累计同比|同比|本期同比|报告期同比)）$"
)


def _percent(finance, keys):
    values = finance.get("指标") or {}
    for key in keys:
        raw = values.get(key)
        if not isinstance(raw, str):
            continue
        match = PERCENT.fullmatch(raw)
        if not match:
            continue
        try:
            return key, Decimal(match[1].replace(",", ""))
        except InvalidOperation:
            pass
    return None, None


def _direction(label, value):
    if value is None:
        return None
    if value > 0:
        return label + "同比增长"
    if value < 0:
        return label + "同比下降"
    return label + "同比持平"


def _valid_snapshot_value(value):
    return isinstance(value, (str, int, float)) and str(value).strip() not in ("", "—", "None", "nan")


def build_research_brief(stock, finance, disclosure):
    """Return grounded investment-research questions and evidence coverage.

    No inferred financial amounts, valuations, industry rankings, targets,
    catalysts or causal explanations are created by this function.
    """
    evidence = disclosure or {}
    metrics = finance.get("指标") or {}
    status = finance.get("状态", "未核实")
    period = finance.get("报告期")
    market_view = []
    for label in ("评分", "趋势分", "活跃分", "估值分", "涨跌幅", "换手率"):
        value = stock.get(label)
        if _valid_snapshot_value(value):
            market_view.append(f"{label} {value}")
    if stock.get("入选原因"):
        market_view.append(f"选股规则解释：{stock['入选原因']}")
    market_view.append("评分是相对排名，不是收益概率；估值分不等于市盈率")

    revenue_key, revenue_yoy = _percent(
        finance, ("营业总收入同比", "营业收入同比", "营收同比"))
    profit_key, profit_yoy = _percent(
        finance, ("归母净利润同比", "归母利润同比"))
    change = []
    revenue_label = _direction("营收", revenue_yoy)
    profit_label = _direction("归母净利润", profit_yoy)
    if revenue_label:
        change.append(f"{revenue_label}（第三方财务指标：{revenue_key}）")
    if profit_label:
        change.append(f"{profit_label}（第三方财务指标：{profit_key}；增长不等于已盈利）")
    if not change:
        change.append("财务同比变化无可核对指标，不推断增长或转亏原因")

    report = evidence.get("对应报告期财报") or {}
    matched = any(n.get("报告期匹配") == period for n in evidence.get("公告", [])) if period else False
    excerpt_pages = sum(len(n.get("正文摘录", [])) for n in evidence.get("公告", []))
    report_pages = sum(
        len(n.get("正文摘录", [])) for n in evidence.get("公告", [])
        if n.get("报告期匹配") == period
    ) if matched else 0

    coverage = [f"财务指标：{status}（{len(metrics)} 项）"]
    if period:
        coverage.append(f"指标报告期：{period}")
    if matched and report_pages:
        coverage.append(f"对应期官方PDF：已读取 {report_pages} 个带页码片段，仅为摘录、非全篇审计")
    elif matched:
        coverage.append("对应期官方PDF：仅匹配标题，尚无可读正文摘录")
    else:
        coverage.append("对应期官方PDF：未取得可核对片段；财务指标未与官方原文逐项复核")
    coverage.append(f"近期公告：{len(evidence.get('公告', []))} 条纳入索引；已读摘录 {excerpt_pages} 段")
    if any(
        p.get("单位") == "未定位" or p.get("表头") == "未定位" or p.get("期间上下文") == "未定位"
        for n in evidence.get("公告", []) for p in n.get("正文摘录", [])
    ):
        coverage.append("部分PDF摘录缺少单位、表头或期间上下文，禁止从这些摘录引用金额")

    counterarguments = []
    if revenue_yoy is not None and revenue_yoy < 0:
        counterarguments.append("营收同比下降，与行情强势不必然一致")
    if profit_yoy is not None and profit_yoy < 0:
        counterarguments.append("归母净利润同比下降，不应仅凭行情评分忽略")
    if not matched or not report_pages:
        counterarguments.append("财务指标缺少同报告期官方正文片段交叉核实")
    if stock.get("风险提示") and stock["风险提示"] != "无明显异常信号":
        counterarguments.append(f"规则风险提示：{stock['风险提示']}")
    if not counterarguments:
        counterarguments.append("尚未获得证明行业优势、长期盈利改善或估值低估的独立证据")

    checkpoints = []
    if revenue_yoy is not None:
        checkpoints.append("下期同口径营业收入同比是否改善或转弱")
    else:
        checkpoints.append("后续披露能否补充可比较的收入变化数据")
    if profit_yoy is not None:
        checkpoints.append("下期归母净利润同比是否改善，并核对利润绝对额正负")
    else:
        checkpoints.append("后续披露的净利润绝对额及同比是否可比")
    if not matched or not report_pages:
        checkpoints.append("获取对应期官方财报PDF正文，复核关键数值、表头与币种")
    else:
        checkpoints.append("后续公告能否对已披露的经营变化提供持续证据")
    return {
        "行情入选依据": "；".join(market_view),
        "财务变化线索": "；".join(change),
        "反面证据": counterarguments[:3],
        "后续验证点": checkpoints[:3],
        "证据覆盖": coverage,
    }
