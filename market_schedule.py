"""Avoid fetching 'new' A/H shares on Saturday/Sunday in China.

This is a conservative *weekday* filter, not a complete exchange holiday
calendar. A user can still choose report_only to research the last snapshot.
"""
import os
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))


def is_trading_weekday(timestamp):
    """Weekdays in Beijing; explicitly NOT a trading-holiday check."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("需要包含时区的时间")
    return timestamp.astimezone(CHINA).weekday() < 5


def is_market_ready(timestamp):
    """Daily A/H ranking waits until 17:00 Beijing, including HK delay buffer.

    This is still a weekday/time filter, not an exchange holiday calendar.
    The quote quality and date gates remain mandatory after this check.
    Read-only probes deliberately do not call this publication gate.
    """
    if not is_trading_weekday(timestamp):
        return False
    local = timestamp.astimezone(CHINA)
    return (local.hour, local.minute) >= (17, 0)


def main():
    now = datetime.now(CHINA)
    weekday = is_trading_weekday(now)
    ready = is_market_ready(now)
    print("北京时间：" + now.strftime("%Y-%m-%d %H:%M") +
          ("；已过收盘缓冲，尝试读取完整行情" if ready else
           "；尚未到17:00收盘发布窗口，保留旧快照；请用只读探针诊断" if weekday else
           "；周末非交易日，保留最近交易日快照，不调用行情接口"), flush=True)
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("trading_weekday=" + str(weekday).lower() + "\n")
            handle.write("market_ready=" + str(ready).lower() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

