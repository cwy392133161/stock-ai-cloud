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


def main():
    now = datetime.now(CHINA)
    weekday = is_trading_weekday(now)
    print("北京时间：" + now.strftime("%Y-%m-%d %H:%M") +
          ("；工作日，尝试读取最新行情" if weekday else
           "；周末非交易日，保留最近交易日快照，不调用行情接口"), flush=True)
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write("trading_weekday=" + str(weekday).lower() + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
