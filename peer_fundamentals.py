"""Same-period A-share peer fundamentals, never a fair-value model.

The quote snapshot supplies issuer/sector/cap candidates; current-period
financial reports come from the already-used Eastmoney financial endpoint.
Exclude unknown release dates, nonmatching fiscal quarters and weak coverage.
No extra LLM call or premium data provider is required.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import math
import re

import akshare as ak
import pandas as pd

from finance import a_exchange, dated_rows

PEER_METRICS = {
    "收入同比": "TOTALOPERATEREVETZ",
    "归母净利润同比": "PARENTNETPROFITTZ",
    "ROE": "ROEJQ",
    "资产负债率": "ZCFZL",
}
MIN_FINANCIAL_PEERS = 3
MIN_VALID_PER_FIELD = 3


def _num(value):
    if value is None:
        return None
    try:
        x = Decimal(str(value).strip().replace(",", ""))
        return x if x.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _code(value):
    s = re.sub(r"^(?:sh|sz|bj)", "", str(value).strip().lower())
    return s if re.fullmatch(r"[0-9]{6}", s) else None


def candidate_pool(ranked, top):
    """Choose similarly sized liquid sector constituents, not only Top 20.

    This is a supplier industry-label sample, not rigorously defined economic
    comparables; incomplete groups must explicitly fail the finance benchmark.
    """
    groups = defaultdict(list)
    for _, row in ranked.iterrows():
        code = _code(row.get("代码"))
        industry = str(row.get("行业") or "").strip()
        cap = _num(row.get("流通市值"))
        if not code or not industry or industry.lower() in ("nan", "none", "其他", "未知"):
            continue
        if cap is None or cap <= 0:
            continue
        groups[industry].append({
            "代码": code, "名称": str(row.get("名称", code)),
            "行业": industry, "流通市值": float(cap),
        })
    out = {}
    for item in top:
        code = _code(item.get("代码"))
        industry = item.get("行业")
        cap = _num(item.get("流通市值数值"))
        if not code or cap is None or cap <= 0 or not industry:
            continue
        values = [x for x in groups.get(industry, [])
                  if x["代码"] != code
                  and 1 / 3 <= x["流通市值"] / float(cap) <= 3]
        values.sort(key=lambda x: (abs(math.log(x["流通市值"] / float(cap))), x["代码"]))
        out[code] = values
    return out


def _matching_row(code, report_period, as_of):
    symbol = code + "." + a_exchange(code)
    frame = ak.stock_financial_analysis_indicator_em(symbol=symbol, indicator="按报告期")
    rows = dated_rows(frame, symbol, pd.Timestamp(as_of[:10]))
    rows = rows.loc[rows["_period"] == pd.Timestamp(report_period)]
    if len(rows) != 1:
        return None
    row = rows.iloc[0]
    published = row.get("NOTICE_DATE")
    updated = row.get("UPDATE_DATE")
    if pd.isna(published) and pd.isna(updated):
        return None
    currency = str(row.get("CURRENCY", "")).strip()
    if currency not in ("人民币", "CNY"):
        return None
    return row


def _values(row):
    if row is None:
        return {}
    vals = {}
    for label, field in PEER_METRICS.items():
        x = _num(row.get(field))
        if x is None:
            continue
        # A live published ratio can be extremely large when base is tiny.
        # Do not let extreme ratios define a tiny-sample peer median.
        cap = 150 if label in ("ROE", "资产负债率") else 1000
        if -cap <= x <= cap:
            vals[label] = x
    return vals


def _value_from_finance(finance, label):
    names = {
        "收入同比": "营业总收入同比",
        "归母净利润同比": "归母净利润同比",
        "ROE": "加权 ROE",
        "资产负债率": "资产负债率",
    }
    v = str((finance.get("指标") or {}).get(names[label], ""))
    m = re.fullmatch(r"\s*([+-]?[0-9][0-9,.]*)\s*%\s*（(?:累计同比|报告期|期末)）\s*", v)
    return _num(m.group(1)) if m else None


def analyze_peer_financials(stock, finance, candidates, as_of, cache, budget):
    """Only same-ending-period, same-provider ratio medians; otherwise N/A.

    The shared cache deduplicates requests across companies and budget only
    tracks how many API requests were made; there is no quota or sample-count
    cut-off. The available snapshot candidate pool remains finite.
    Never claim company-specific outperformance from a weak peer sample.
    """
    unavailable = {"状态": "同行财务口径或样本不足", "行业": stock.get("行业") or "未核实",
                   "原因": "需要至少3家同供应商行业、相近规模、同报告期且披露时间合规的同行"}
    if not as_of or not finance.get("报告期") or not candidates:
        return unavailable
    if str(finance.get("报告期")) > str(as_of)[:10]:
        return unavailable
    target_period = str(finance["报告期"])
    parsed = []
    unique_peer_codes = set()
    for item in candidates:
        code = _code(item.get("代码"))
        if not code or item.get("行业") != stock.get("行业"):
            continue
        if code == _code(stock.get("代码")) or code in unique_peer_codes:
            continue
        unique_peer_codes.add(code)
        key = (code, target_period, str(as_of)[:10])
        if key not in cache:
            budget["calls"] = budget.get("calls", 0) + 1
            try:
                cache[key] = _matching_row(code, target_period, as_of)
            except (ValueError, KeyError, OSError, TypeError, AttributeError):
                cache[key] = None
        row = cache[key]
        if row is None:
            continue
        parsed.append((_values(row), code))
    if len(parsed) < MIN_FINANCIAL_PEERS:
        return {**unavailable, "同期间成功样本": len(parsed)}
    values = {}
    for label in PEER_METRICS:
        observations = sorted(v[label] for v, _ in parsed if label in v)
        own = _value_from_finance(finance, label)
        if len(observations) < MIN_VALID_PER_FIELD or own is None:
            continue
        n = len(observations)
        median = (observations[(n - 1) // 2] + observations[n // 2]) / 2
        delta = own - median
        values[label] = {
            "本公司": format(own.normalize(), "f"),
            "同行中位数": format(median.normalize(), "f"),
            "差异百分点": format(delta.normalize(), "f"),
            "同行有效样本数": n,
            "解释限制": "同报告期百分比指标的分布位置，不等于投资优劣或因果比较",
        }
    if not values:
        return {**unavailable, "同期间成功样本": len(parsed)}
    return {
        "状态": "有限同行财务对照（并非投资评级）",
        "行业": stock.get("行业"),
        "报告期": target_period,
        "来源": "东方财富（AKShare 财务指标接口）",
        "选样口径": "同供应商行业标签、流通市值相差不超过3倍、同报告期、已披露",
        "同行有效公司": len(parsed),
        "同行代码": [code for _, code in parsed],
        "可比指标": values,
        "限制": "行业与市值匹配不保证细分业务与会计政策可比；"
                "不得把PE/PB差异解读为合理价值，不能和港股不同币种随意比较",
    }
