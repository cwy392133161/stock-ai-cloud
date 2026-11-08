"""Evidence-quality gate: grade the dataset, never the future stock return.

A PDF link is not a verified financial figure, a citation tag is not proof
that a sentence is entailed by a passage, and a peer PE median is not a DCF.
"""
from datetime import date
from period_reports import matched_report_notices, period_title_score


def evidence_quality(finance, disclosure, valuation, snapshot_date):
    evidence = disclosure or {}
    history = finance.get("历史财务") or {}
    periods = history.get("可比同季度年度数", 0)
    linked = matched_report_notices(evidence, finance.get("报告期"))
    pages = [p for n in linked for p in n.get("正文摘录", [])]
    usable_table = sum(
        all(isinstance(p.get(k), str) and p[k].strip() not in ("", "未定位")
            for k in ("单位", "表头", "期间上下文"))
        for p in pages
    )
    current = bool(finance.get("指标")) and finance.get("状态") in ("可用", "部分可用")
    valuation_good = bool(valuation.get("PE动态") or valuation.get("PB"))
    peer_good = bool(valuation.get("行业参考"))
    as_of = str(snapshot_date)[:10]
    # No market snapshot date means no as-of claim. A fiscal period is NOT
    # the publication date and cannot substitute for a missing timestamp.
    not_future = False
    snapshot_verified = False
    try:
        snapshot = date.fromisoformat(as_of)
        snapshot_verified = True
        current_period = finance.get("报告期")
        if current_period:
            not_future = date.fromisoformat(str(current_period)) <= snapshot
        else:
            not_future = False
        published = finance.get("披露日期")
        if published and published != "接口未提供":
            not_future = not_future and date.fromisoformat(str(published)[:10]) <= snapshot
        else:
            # The exact latest financial period being released before the
            # snapshot must be supported by an independent official index.
            report = evidence.get("对应报告期财报") or {}
            if (report.get("报告期") != current_period or not report.get("公告日期")
                    or max(period_title_score(report.get("标题", ""), current_period, market)
                           for market in ("港股", "A股")) <= 0):
                not_future = False
            else:
                not_future = not_future and date.fromisoformat(str(report["公告日期"])[:10]) <= snapshot
        if any(date.fromisoformat(str(n["日期"])[:10]) > snapshot
               for n in evidence.get("公告", [])):
            not_future = False
    except (TypeError, ValueError, KeyError):
        not_future = False

    criteria = {
        "行情快照日期已确认": snapshot_verified,
        "财务与公告未晚于行情快照": not_future,
        "当前财务指标": current and not_future,
        "至少2个同报告季跨年样本": isinstance(periods, int) and periods >= 2,
        "至少3个同报告季跨年样本": isinstance(periods, int) and periods >= 3,
        "同报告期官方PDF有可读摘录": bool(pages),
        "同报告期官方PDF表头/单位/期间齐备": usable_table > 0,
        "行情估值倍数有结构化来源": valuation_good,
        "同一行业合格样本有估值对照": peer_good,
    }
    grade = "资料不足"
    if criteria["当前财务指标"]:
        grade = "基础研究"
    if criteria["当前财务指标"] and criteria["至少2个同报告季跨年样本"]:
        grade = "纵向财务研究"
    if (criteria["当前财务指标"] and criteria["至少3个同报告季跨年样本"]
        and criteria["同报告期官方PDF有可读摘录"]
        and criteria["同报告期官方PDF表头/单位/期间齐备"] and valuation_good):
        grade = "扩展证据研究"
    # No grade claims 'professional' merely for meeting numeric coverage.
    missing = [name for name, ok in criteria.items() if not ok]
    return {
        "分级": grade,
        "通过项": [name for name, ok in criteria.items() if ok],
        "缺口": missing,
        "同报告期PDF可读页段": len(pages),
        "表头单位期间齐备片段": usable_table,
        "说明": ("覆盖度是研究资料质量，不是盈利预测、个股评级、专业审计或绝对可靠性保证；"
                "若无同报告期PDF全文与可审计同行数据，应明确承认结论边界。"),
    }

