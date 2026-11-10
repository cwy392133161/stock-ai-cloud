"""Time gate for the explicitly delayed Eastmoney HK endpoint."""
from datetime import datetime, timedelta, timezone
import pandas as pd
from market_quality import MarketDataQualityError

CST = timezone(timedelta(hours=8))


def validate_delayed_hk_times(frame, now=None):
    now = now or datetime.now(CST)
    if now.tzinfo is None:
        raise ValueError("行情检查时间必须包含时区")
    now = now.astimezone(CST)
    if "f124" not in frame:
        raise MarketDataQualityError("港股延迟源缺少原始报价时间 f124")
    stamps = pd.to_datetime(pd.to_numeric(frame["f124"], errors="coerce"),
                            unit="s", errors="coerce", utc=True).dt.tz_convert(CST)
    amount = pd.to_numeric(frame["成交额"], errors="coerce")
    price = pd.to_numeric(frame["最新价"], errors="coerce")
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
    fresh = (stamps.dt.date == now.date()) & (stamps >= cutoff) & (
        stamps <= now + timedelta(seconds=60))
    if not active.any() or fresh.loc[active].mean() < .95:
        raise MarketDataQualityError("港股延迟源有成交证券的新鲜报价不足95%，拒绝旧快照")
    result = frame.copy()
    # The small accepted stale remainder still cannot enter a ranked list.
    result.loc[active & ~fresh, "最新价"] = float("nan")
    result.attrs["freshness"] = {
        "行情时效": "东方财富官方延迟源；盘中允许最多30分钟延迟，16:30后必须含16:00后报价",
        "时效检查时间": now.isoformat(),
        "有成交证券数": int(active.sum()),
        "新鲜有成交证券数": int((active & fresh).sum()),
    }
    return result

