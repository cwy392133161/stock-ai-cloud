"""Same-snapshot, as-of valuation references. Not intrinsic fair value.

Industry samples are all LIQUID candidates in the same market quote snapshot,
not a cherry-picked Top20 or an externally validated sector universe.
"""
import math


def safe_number(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return round(x, 4) if math.isfinite(x) else None


def positive_ratio(value, ceiling):
    x = safe_number(value)
    return x if x is not None and 0 < x <= ceiling else None


def industry_benchmarks(frame, *, min_pe=12, min_pb=12, min_group=20, min_coverage=0.60):
    """A-share quote industry cross-section; call after original screening."""
    if "行业" not in frame.columns:
        return {}
    groups = {}
    for _, row in frame.iterrows():
        name = str(row.get("行业", "")).strip()
        if not name or name.lower() in ("nan", "none", "其他", "未知", "-", "—"):
            continue
        groups.setdefault(name, []).append(row)
    output = {}
    for sector, rows in groups.items():
        if len(rows) < min_group:
            continue
        pe = sorted(v for r in rows if (v := positive_ratio(r.get("市盈率-动态"), 100)) is not None)
        pb = sorted(v for r in rows if (v := positive_ratio(r.get("市净率"), 10)) is not None)
        def med(seq):
            n = len(seq)
            return round((seq[(n-1)//2] + seq[n//2])/2, 3) if n else None
        output[sector] = {
            "同行样本数": len(rows),
            "有效PE样本数": len(pe),
            "有效PB样本数": len(pb),
            "行业PE中位数": med(pe) if len(pe) >= min_pe and len(pe) / len(rows) >= min_coverage else None,
            "行业PB中位数": med(pb) if len(pb) >= min_pb and len(pb) / len(rows) >= min_coverage else None,
            "PE样本覆盖率": round(len(pe) / len(rows), 3),
            "PB样本覆盖率": round(len(pb) / len(rows), 3),
            "样本说明": "同一行情快照中通过流动性过滤的A股供应商行业标签组；剔除无效及极端倍数，只做横截面描述，未调整行业细分/盈利质量，不代表估值合理或便宜",
        }
    return output


def valuation_context(stock):
    """Use structured quote fields. Do NOT parse numbers from prose/score."""
    # Use the same PE/PB validity window for the individual and its sector.
    # A value excluded from the reference sample cannot be compared to it.
    pe = positive_ratio(stock.get("市盈率-动态"), 100)
    pb = positive_ratio(stock.get("市净率"), 10)
    industry = stock.get("行业")
    raw = stock.get("行业估值参考")
    reference = raw if isinstance(raw, dict) else {}
    obj = {
        "来源": "榜单同一行情快照的东方财富行情数据（动态PE与PB）",
        "行情时点": "与榜单快照相同；不是财报披露时点",
        "PE动态": pe,
        "PB": pb,
        "行业": industry or "未核实",
        "行业参考": None,
        "解释": "低于行业中位数不等于低估；动态PE并非TTM或未来盈利预测",
    }
    if pe is None and pb is None:
        obj["状态"] = "缺乏可核实的结构化估值倍数"
    else:
        obj["状态"] = "行情倍数可用，仅作截面对照"
    count = reference.get("同行样本数")
    if not isinstance(count, int) or count < 20:
        obj["行业限制"] = "未取得足够的同一行业、同一快照估值样本"
        return obj
    comparison = {}
    for label, actual, median_label in (
        ("动态PE", pe, "行业PE中位数"), ("PB", pb, "行业PB中位数"),
    ):
        median = positive_ratio(reference.get(median_label), 100 if label == "动态PE" else 10)
        if median is None or actual is None:
            continue
        sample_n = reference.get("有效PE样本数" if label == "动态PE" else "有效PB样本数")
        if not isinstance(sample_n, int) or sample_n < 12 or sample_n / count < 0.60:
            continue
        comparison[label] = {
            "个股倍数": actual, "行业样本中位数": median,
            "有效同业样本数": sample_n,
            "与行业中位数差异%": round((actual / median - 1) * 100, 2),
            "结论限制": "仅同供应商行业标签与可用报价样本中位数差异；不是合理价值判断，未调整盈利质量、资本结构、增速和商业模式",
        }
    obj["行业参考"] = comparison or None
    if not comparison:
        obj["行业限制"] = "同行样本或相应估值字段不足，不作同行价值高低判断"
    return obj
