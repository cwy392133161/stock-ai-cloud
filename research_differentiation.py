"""Evidence-selected issuer focus and cross-report boilerplate detector.

This evaluates diversity of explanatory content, not investment merit.
Two companies with genuinely similar fundamentals may legitimately share a
focus; duplicate model prose is downgraded rather than silently published.
"""
from collections import Counter
import re
import unicodedata

HEADINGS = ("入选逻辑", "经营和估值", "反面证据", "跟踪条件")


def _pct(finance, *names):
    for name in names:
        raw = (finance.get("指标") or {}).get(name)
        if not isinstance(raw, str):
            continue
        m = re.match(r"\s*([+-]?[0-9]+(?:\.[0-9]+)?)\s*%", raw)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def focal_question(stock, finance, evidence=None, peer=None):
    """Highlight an actual issuer-specific contradiction or unknown.

    Purely deterministic: do not infer market share or operating causality.
    """
    revenue = _pct(finance, "营业总收入同比", "营业收入同比")
    profit = _pct(finance, "归母净利润同比")
    debt = _pct(finance, "资产负债率")
    roe = _pct(finance, "加权 ROE", "平均 ROE")
    snapshot = stock.get("风险提示", "")
    metrics = finance.get("历史财务", {}).get("同口径年度对照", [])
    label = "资料边界核查"
    why = "官方财报与当期可比较数据是否足以支持一项企业经营判断"
    follow = "补充同报告期官方财报，核对关键科目和披露时点"

    if revenue is not None and profit is not None:
        if revenue > 0 and profit < 0:
            label, why = "增收不增利", "收入同比增加但归母净利润同比下降，核验毛利与费用变化来源"
            follow = "核对下一期同口径收入与利润变化及财务报表解释"
        elif revenue < 0 and profit > 0:
            label, why = "收入与利润方向分化", "收入同比下降而归母净利润同比上升，核实利润改善来自主营业务还是非经常性因素"
            follow = "核对下一期收入与利润同比，以及非经常性损益披露"
        elif revenue < 0 and profit < 0:
            label, why = "双指标承压", "收入和归母净利润同比均下降，研究行情关注度与业绩走势之间的背离"
            follow = "跟踪下期同口径两个指标是否止跌及官方原因披露"
        elif revenue > 0 and profit > 0:
            label, why = "增长质量与可持续性", "收入与归母净利润同比均增长，核实现金流、ROE与利润改善是否一致"
            follow = "核对经营现金流与盈利方向、后续同口径增长能否维持"
        else:
            label, why = "增长方向不一致", "收入或利润同比缺少连续改善，核实是否为阶段性变化"
            follow = "核对下期同口径利润与收入及管理层说明"
    elif revenue is not None or profit is not None:
        label, why = "单侧财务证据", "只有部分增长指标可验证，不能据单项指标建立完整经营增长逻辑"
        follow = "补足缺失的配套财务科目并核对同一报告期间"

    if debt is not None and debt > 80 and label in ("增长质量与可持续性", "资料边界核查"):
        label, why = "高负债与增长质量", "资产负债率较高，应重点核验现金流与偿债安排，不直接推断违约风险"
        follow = "下一期披露中核验债务结构及经营现金流"
    elif roe is not None and roe < 0 and label in ("增长质量与可持续性", "资料边界核查"):
        label, why = "负ROE与盈利质量", "当前ROE为负，应核实利润绝对额及权益变动来源"
        follow = "核对下期ROE、归母净利润及相关权益口径"

    if peer and peer.get("状态") == "有限同行财务对照（并非投资评级）":
        follow += "；同步观察同行样本的同报告期数据变化"
    if not metrics:
        follow += "；补齐跨年同季历史可比资料"
    if snapshot and snapshot != "无明显异常信号":
        follow += "；检查规则风险提示是否仍然存在"

    return {
        "代码": str(stock.get("代码", "")),
        "研究主题": label,
        "核心矛盾": why,
        "下一期关键问题": follow,
        "区别来源": "同报告期财务增长方向、杠杆、ROE、历史覆盖及行情风险提示",
    }


def _shingles(text, n=5):
    """Character shingles ignore trivial punctuation and stock-code changes."""
    text = unicodedata.normalize("NFKC", str(text or "")).lower()
    text = re.sub(r"(?:[a-z]{1,4})?[0-9]{4,6}", "", text)
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    if len(text) < n:
        return {text} if text else set()
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def prose_similarity(a, b):
    x, y = _shingles(a), _shingles(b)
    if not x or not y:
        return 0.0
    # Jaccard is conservative for shared stock-research boilerplate.
    return len(x & y) / len(x | y)


def duplicate_reports(rows, *, threshold=0.79):
    """Return indices to downgrade; preserve first, require distinct substance.

    Input: iterable of (market, code, sections_dict). Compare the combined
    four-section substantive text, not stock names, score or source links.
    """
    seen = []
    blocked = {}
    for i, (market, code, sections) in enumerate(rows):
        body = "。".join(str(sections.get(k, "")) for k in HEADINGS)
        if not body.strip("。"):
            continue
        for earlier, previous in seen:
            similarity = prose_similarity(body, previous)
            if similarity >= threshold:
                blocked[i] = {
                    "原因": "与另一家公司研究正文高度重复，不符合差异化研究要求",
                    "相似度": round(similarity, 3),
                    "比较对象索引": earlier,
                }
                break
        if i not in blocked:
            seen.append((i, body))
    return blocked
