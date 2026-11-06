"""Fail-closed data-quality gate for the existing four-factor stock screener.

No score weight or screening threshold is changed here. A source without
the necessary fields must not publish a misleading ranked list.
"""
import math

import pandas as pd


class MarketDataQualityError(ValueError):
    pass


# Minimum coverage among tradable rows in a *market-wide* snapshot.
# The PE and PB floors are lower because unprofitable firms legitimately
# have non-usable multiples; both still need meaningful market coverage.
MIN_COVERAGE = {
    "成交额": 0.85,
    "流通市值": 0.80,
    "60日涨跌幅": 0.65,
    "年初至今涨跌幅": 0.65,
    "换手率": 0.65,
    "量比": 0.55,
    "市盈率-动态": 0.30,
    "市净率": 0.45,
}
MIN_UNIVERSE = {"A股": 3000, "港股": 1200}


def _numeric(frame, column):
    if column not in frame:
        return pd.Series(float("nan"), index=frame.index)
    values = pd.to_numeric(frame[column], errors="coerce")
    return values.where(values.map(lambda v: bool(pd.notna(v) and math.isfinite(float(v)))))


def validate_market_data(frame, market, source):
    """Return JSON-safe diagnostics or raise before ranking/publishing.

    The checks protect against partial downloads and limited-column fallbacks.
    A few missing individual values remain neutral in the original score,
    but a whole missing factor cannot silently become a score of 50.
    """
    if market not in MIN_UNIVERSE:
        raise MarketDataQualityError(f"未知市场 {market}")
    if frame is None or frame.empty or "代码" not in frame:
        raise MarketDataQualityError(f"{market}/{source} 没有有效股票列表")
    code = frame["代码"].astype(str).str.strip()
    if code.eq("").any() or code.duplicated().any():
        collisions = code.loc[code.duplicated(keep=False)]
        sample = ",".join(collisions.drop_duplicates().head(6).tolist())
        raise MarketDataQualityError(
            f"{market}/{source} 证券代码缺失或重复；重复行 {len(collisions)}，样例：{sample}"
        )
    price = _numeric(frame, "最新价")
    change = _numeric(frame, "涨跌幅")
    tradable = (price > 0) & change.notna()
    valid = frame.loc[tradable]
    if len(valid) < MIN_UNIVERSE[market]:
        raise MarketDataQualityError(
            f"{market}/{source} 只有 {len(valid)} 只有效行情，疑似全市场数据不完整"
        )
    coverage = {}
    failed = []
    for field, floor in MIN_COVERAGE.items():
        values = _numeric(valid, field)
        if field in ("成交额", "流通市值", "量比"):
            present = values > 0
        elif field == "换手率":
            present = values >= 0
        elif field == "市盈率-动态":
            present = (values > 0) & (values < 100)
        elif field == "市净率":
            present = (values > 0) & (values < 10)
        else:
            present = values.notna()
        ratio = float(present.mean())
        coverage[field] = round(ratio * 100, 1)
        if ratio < floor:
            failed.append(f"{field} {ratio:.0%}（最低 {floor:.0%}）")
    if failed:
        raise MarketDataQualityError(
            f"{market}/{source} 关键字段覆盖不足：" + "，".join(failed)
        )
    return {"行情来源": source, "有效股票数": len(valid), "字段有效覆盖率%": coverage}
