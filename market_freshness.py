"""Time gate for the explicitly delayed Eastmoney HK endpoint."""
from datetime import datetime, timedelta, timezone
import pandas as pd
from market_quality import MarketDataQualityError

CST = timezone(timedelta(hours=8))


def validate_delayed_hk_times(frame, now=None, min_amount=1e7, min_float_cap=20e8):
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

