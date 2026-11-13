"""Avoid fetching 'new' A/H shares on Saturday/Sunday in China.

This is a conservative *weekday* filter, not a complete exchange holiday
calendar. A user can still choose report_only to research the last snapshot.
"""
import os
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))

SCHEDULE_BLOCK_REASONS = {
    "schedule_missed_window": (
        "定时任务实际启动时已不在北京时间工作日17:00至24:00发布窗口；"
        "本次未采集行情、未生成AI。不能把次日凌晨当成原交易日补跑，"
        "也不能把旧快照改成新日期"),
    "schedule_repeat_blocked": (
        "本次是同一定时运行的重复执行，已阻止再次采集行情和调用AI；"
        "重跑记录不代表行情或研究已更新"),
    "schedule_invalid_context": (
        "定时运行缺少有效执行次数，已停止采集行情和调用AI，"
        "不能确认本次是否重复执行"),
}


def scheduling_decision(timestamp, event_name="", run_attempt="1"):
    """Fail closed for late or re-run scheduled jobs; never backdate quotes.

    GitHub provides the cron expression, not an authoritative nominal firing
    timestamp. An out-of-window invocation therefore cannot safely select a
    prior trading date. Manual runs retain the existing close-window behavior.
    This is not a catch-up scheduler and does not add any new AI opportunities.
    """
    weekday = is_trading_weekday(timestamp)
    ready = is_market_ready(timestamp)
    state = "not_scheduled"
    if event_name == "schedule":
        if (not isinstance(run_attempt, str) or not run_attempt.isascii() or
                not run_attempt.isdigit() or len(run_attempt) > 9 or int(run_attempt) < 1):
            state = "schedule_invalid_context"
        elif int(run_attempt) > 1:
            state = "schedule_repeat_blocked"
        elif not ready:
            state = "schedule_missed_window"
        else:
            state = "schedule_ready"
        ready = ready and state == "schedule_ready"
    return {"trading_weekday": weekday, "market_ready": ready,
            "schedule_state": state,
            "schedule_observed_at": timestamp.astimezone(CHINA).isoformat(timespec="seconds")}


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
    decision = scheduling_decision(now, os.getenv("SCHEDULE_EVENT_NAME", ""),
                                   os.getenv("GITHUB_RUN_ATTEMPT", ""))
    weekday = decision["trading_weekday"]
    ready = decision["market_ready"]
    if decision["schedule_state"] in SCHEDULE_BLOCK_REASONS:
        print("北京时间：" + now.strftime("%Y-%m-%d %H:%M") + "；定时更新未执行", flush=True)
        print("::warning::" + SCHEDULE_BLOCK_REASONS[decision["schedule_state"]], flush=True)
    else:
        print("北京时间：" + now.strftime("%Y-%m-%d %H:%M") +
              ("；已过收盘缓冲，尝试读取完整行情" if ready else
               "；尚未到17:00收盘发布窗口，保留旧快照；请用只读探针诊断" if weekday else
               "；周末非交易日，保留最近交易日快照，不调用行情接口"), flush=True)
    output = os.getenv("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in decision.items():
                handle.write(key + "=" + (str(value).lower() if isinstance(value, bool) else value) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

