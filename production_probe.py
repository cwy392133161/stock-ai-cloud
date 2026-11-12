"""Exercise the real picker and publication guard in a disposable local copy.

No AI code is invoked, no repository output is overwritten, and nothing is
published. Only complete, current market data can pass the production gates.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from datetime import datetime, timezone, timedelta

import market_schedule
import publish_guard

ROOT = Path(__file__).resolve().parent
CST = timezone(timedelta(hours=8))
FILES = ("data.json", "index.html", "ai.html", "ai-status.json")


def report_body(text):
    return publish_guard.MARKET_BANNER.sub("", publish_guard.BANNER.sub("", text))


def check_result(before, after, screening_ok):
    old = json.loads(before["data.json"])
    new = json.loads(after["data.json"])
    previous = json.loads(before.get("ai-status.json", "{}"))
    state = json.loads(after["ai-status.json"])
    if report_body(before.get("ai.html", "")) != report_body(after.get("ai.html", "")):
        raise ValueError("只读验证意外修改了既有研究正文")
    if previous.get("review_required") or previous.get("state") == "review-required":
        if state.get("state") != "review-required" or not state.get("review_required"):
            raise ValueError("跳过AI不能解除既有研究复核冻结")
    if state.get("ai_step") != "skipped":
        raise ValueError("无AI验证的AI步骤必须为skipped")
    if not screening_ok:
        if after["data.json"] != before["data.json"]:
            raise ValueError("选股失败后行情文件被改写")
        if state.get("market_state") != "failed":
            raise ValueError("选股失败后故障状态未保留")
        return state
    old_stamp = datetime.strptime(old["updated"], "%Y-%m-%d %H:%M")
    new_stamp = datetime.strptime(new["updated"], "%Y-%m-%d %H:%M")
    if new_stamp <= old_stamp or state.get("market_state") != "updated":
        raise ValueError("成功步骤没有更新的快照，不能证明恢复")
    for key in ("a_shares", "hk_shares"):
        rows = new[key]
        if len(rows) != 20 or len({row["代码"] for row in rows}) != 20:
            raise ValueError(f"真实生产榜单不完整：{key}")
    for key in ("index.html", "ai.html"):
        if key in after and 'data-market-state="updated"' not in after[key]:
            raise ValueError(f"{key}没有同步的行情恢复状态")
    return state


def main():
    now = datetime.now(CST)
    if not market_schedule.is_market_ready(now):
        print("未到北京时间工作日17:00；不执行生产恢复验收，也不视为恢复。", flush=True)
        return 2
    original = {name: (ROOT / "docs" / name).read_text(encoding="utf-8")
                for name in FILES if (ROOT / "docs" / name).exists()}
    with TemporaryDirectory(prefix="market-production-probe-") as tmp:
        work = Path(tmp)
        for script in ROOT.glob("*.py"):
            shutil.copy2(script, work / script.name)
        (work / "docs").mkdir()
        for name, text in original.items():
            (work / "docs" / name).write_text(text, encoding="utf-8")
        # Do not pass model credentials even to the disposable quote process.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("AI_")}
        env.update({"AI_ENABLED": "false", "SKIP_AI": "true", "AI_JOB_STATUS": "skipped",
                    "REPORT_ONLY": "false", "TRADING_WEEKDAY": "true", "MARKET_READY": "true"})
        print("完整生产路径：港股→A股，共享预算；无AI、无外部发布。", flush=True)
        screening = subprocess.run([sys.executable, "-u", "picker.py"], cwd=work, env=env)
        env["SCREENING_OUTCOME"] = "success" if screening.returncode == 0 else "failure"
        subprocess.run([sys.executable, "-u", "publish_guard.py"], cwd=work, env=env, check=True)
        after = {name: (work / "docs" / name).read_text(encoding="utf-8")
                 for name in FILES if (work / "docs" / name).exists()}
        state = check_result(original, after, screening.returncode == 0)
        for name, text in original.items():
            if (ROOT / "docs" / name).read_text(encoding="utf-8") != text:
                raise ValueError("只读验证改动了仓库内发布文件")
        print(json.dumps({"market_state": state["market_state"], "snapshot": state["snapshot"],
                          "research_state": state["state"], "ai_step": state["ai_step"]},
                         ensure_ascii=False, indent=2), flush=True)
        if screening.returncode:
            print("生产采集失败；旧行情和复核状态保护已验证，仍不能发布新行情。", flush=True)
            return 1
        print(json.dumps(json.loads(after["data.json"])["quality"], ensure_ascii=False, indent=2), flush=True)
        print("生产恢复验收通过；临时结果未发布。", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
