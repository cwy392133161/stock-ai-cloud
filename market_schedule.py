"""Check the A-share publication window in Beijing time.

This is a conservative *weekday* filter, not a complete exchange holiday
calendar. A user can still choose report_only to research the last snapshot.
"""
import os
import sys
from time import sleep
from datetime import datetime, timedelta, timezone

CHINA = timezone(timedelta(hours=8))
SCHEDULE_START = (12, 30)
CLOSE_WINDOW_START = (17, 0)

SCHEDULE_BLOCK_REASONS = {
    "schedule_missed_window": (
        "定时任务未能进入同一北京时间工作日17:00至24:00发布窗口；"
        "本次未采集行情、未生成AI。不能把次日凌晨当成原交易日补跑，"
        "也不能把旧快照改成新日期"),
    "schedule_repeat_blocked": (
        "本次是同一定时运行的重复执行，已阻止再次采集行情和调用AI；"
        "重跑记录不代表行情或研究已更新"),
    "schedule_invalid_context": (
        "定时运行缺少有效执行次数或同日放行记录，已停止采集行情和调用AI"),
}


def attempt_block(run_attempt):
    if (not isinstance(run_attempt, str) or not run_attempt.isascii() or
            not run_attempt.isdigit() or len(run_attempt) > 9 or int(run_attempt) < 1):
        return "schedule_invalid_context"
    if int(run_attempt) > 1:
        return "schedule_repeat_blocked"
    return None


def wait_for_close(clock, pause, run_attempt):
    """Admit a scheduled run for the observed Beijing day only."""
    blocked = attempt_block(run_attempt)
    if blocked:
        return blocked
    first = clock()
    local = first.astimezone(CHINA) if is_trading_weekday(first) else None
    if local is None or (local.hour, local.minute) < SCHEDULE_START:
        return "schedule_missed_window"
    hour, minute = CLOSE_WINDOW_START
    close = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    seconds = (close - local).total_seconds()
    if seconds > 0:
        pause(seconds)
    current = clock().astimezone(CHINA)
    if current.date() != local.date() or not is_market_ready(current):
        return "schedule_missed_window"
    return local.date().isoformat()


def scheduling_decision(timestamp, event_name="", run_attempt="1", *, admission=None):
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
        blocked = attempt_block(run_attempt)
        if blocked:
            state = blocked
        elif admission in SCHEDULE_BLOCK_REASONS:
            state = admission
        elif admission == "":
            state = "schedule_invalid_context"
        elif not ready or (admission is not None and admission != timestamp.astimezone(CHINA).date().isoformat()):
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
    """The daily A-share ranking publication window opens at 17:00 Beijing.

    This is still a weekday/time filter, not an exchange holiday calendar.
    The quote quality and date gates remain mandatory after this check.
    Read-only probes deliberately do not call this publication gate.
    """
    if not is_trading_weekday(timestamp):
        return False
    local = timestamp.astimezone(CHINA)
    return (local.hour, local.minute) >= CLOSE_WINDOW_START


def prepare_main():
    if os.getenv("GITHUB_EVENT_NAME") != "schedule" or not os.getenv("GITHUB_OUTPUT"):
        raise SystemExit("定时等待步骤缺少 GitHub Actions 上下文")
    admission = wait_for_close(lambda: datetime.now(CHINA), sleep, os.getenv("GITHUB_RUN_ATTEMPT", ""))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
        handle.write("schedule_admission=" + admission + "\n")
    print("定时等待结果：" + admission, flush=True)
    return 0


def main():
    now = datetime.now(CHINA)
    decision = scheduling_decision(now, os.getenv("SCHEDULE_EVENT_NAME", ""),
                                   os.getenv("GITHUB_RUN_ATTEMPT", ""),
                                   admission=os.getenv("SCHEDULE_ADMISSION", ""))
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
    if sys.argv[1:] == ["--wait-for-close"]:
        raise SystemExit(prepare_main())
    if sys.argv[1:]:
        raise SystemExit("未知运行选项")
    raise SystemExit(main())

