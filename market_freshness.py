"""Quote-time checks for the A-share and Hong Kong market feeds."""
from datetime import datetime, timedelta, timezone
import pandas as pd
from market_quality import MarketDataQualityError

CST = timezone(timedelta(hours=8))


def _observed_at(now):
    now = now or datetime.now(CST)
    if now.utcoffset() is None:
        raise ValueError("行情检查时间必须包含时区")
    return now.astimezone(CST)


def _eastmoney_timestamps(frame, source):
    if "f124" not in frame:
        raise MarketDataQualityError(f"{source}缺少原始报价时间 f124")
    return pd.to_datetime(pd.to_numeric(frame["f124"], errors="coerce"),
                          unit="s", errors="coerce", utc=True).dt.tz_convert(CST)


def a_quote_cutoff(observed):
    """Earliest usable quote time for an A-share observation in Beijing time."""
    close = observed.replace(hour=15, minute=0, second=0, microsecond=0)
    lunch = observed.replace(hour=11, minute=30, second=0, microsecond=0)
    reopen = observed.replace(hour=13, minute=0, second=0, microsecond=0)
    if observed >= close:
        return close
    if lunch <= observed < reopen:
        return lunch - timedelta(minutes=2)
    return observed - timedelta(minutes=10)


def validate_eastmoney_a_times(frame, now=None):
    now = _observed_at(now)
    stamps = _eastmoney_timestamps(frame, "东财A股")
    fresh = ((stamps.dt.date == now.date()) & (stamps >= a_quote_cutoff(now))
             & (stamps <= now + timedelta(seconds=60)))
    active = pd.to_numeric(frame["成交额"], errors="coerce") > 0
    if not active.any() or fresh.loc[active].mean() < .95:
        raise MarketDataQualityError(
            f"东财A股有成交证券的新鲜报价不足95%：{int((active & fresh).sum())}/{int(active.sum())}")
    result = frame.copy()
    result.loc[~fresh, "最新价"] = float("nan")
    result.attrs["freshness"] = {
        "报价日期": now.date().isoformat(), "时效检查时间": now.isoformat(),
        "有成交证券数": int(active.sum()), "新鲜有成交证券数": int((active & fresh).sum()),
        "行情时效": "东财原始报价时间已核对；盘后入榜证券须含当日15:00或之后的报价",
    }
    return result


def validate_delayed_hk_times(frame, now=None, min_amount=1e7, min_float_cap=20e8):
    now = _observed_at(now)
    stamps = _eastmoney_timestamps(frame, "港股延迟源")
    amount = pd.to_numeric(frame["成交额"], errors="coerce")
    price = pd.to_numeric(frame["最新价"], errors="coerce")
    cap = pd.to_numeric(frame["流通市值"], errors="coerce")
    active = (amount > 0) & (price > 0)
    close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    lunch = now.replace(hour=12, minute=0, second=0, microsecond=0)
    reopen = now.replace(hour=13, minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(minutes=30)
    if lunch <= now < reopen + timedelta(minutes=30):
        elapsed = max(now - reopen, timedelta(0))
        cutoff = lunch - (timedelta(minutes=30) - elapsed)
    if now >= close + timedelta(minutes=30):
        cutoff = close
    same_day = (stamps.dt.date == now.date()) & (stamps <= now + timedelta(seconds=60))
    fresh = same_day & (stamps >= cutoff)
    # f124 is the security's last quote time. An illiquid security that traded
    # once today must not make the whole provider look stale. It cannot enter
    # the ranking unless the original turnover/cap filters permit it.
    eligible = active & (amount >= min_amount) & (cap >= min_float_cap)
    if not active.any() or same_day.loc[active].mean() < .95:
        raise MarketDataQualityError("港股延迟源有成交证券的当日报价不足95%，拒绝旧快照")
    if not eligible.any() or fresh.loc[eligible].mean() < .95:
        raise MarketDataQualityError(
            f"港股延迟源可入榜证券的新鲜报价不足95%：{int((eligible & fresh).sum())}/{int(eligible.sum())}"
        )
    result = frame.copy()
    # The small accepted stale remainder still cannot enter a ranked list.
    result.loc[(active & ~same_day) | (eligible & ~fresh), "最新价"] = float("nan")
    result.attrs["freshness"] = {
        "行情时效": "东方财富官方延迟源；入榜候选盘中允许最多30交易分钟延迟，16:30后必须含16:00后报价",
        "时效检查时间": now.isoformat(),
        "有成交证券数": int(active.sum()),
        "新鲜有成交证券数": int((active & fresh).sum()),
        "当日有成交证券数": int((active & same_day).sum()),
        "可入榜证券数": int(eligible.sum()),
        "新鲜可入榜证券数": int((eligible & fresh).sum()),
    }
    return result

