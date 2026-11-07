"""Publish a truthful research freshness banner without changing trading data.

This runs AFTER the optional AI step, even when the AI runner fails.
An old ai.html remains accessible as an archive, never labeled current.
"""
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"

REPORT_SNAPSHOT = re.compile(r"行情快照[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})")
NAV = re.compile(r'<p class="ai-nav">.*?</p>', flags=re.S)


def research_freshness(snapshot, report_html, ai_enabled, job_outcome):
    """Return status with exact full-minute snapshot identity."""
    current = str(snapshot.get("updated", "")).strip()
    report = REPORT_SNAPSHOT.search(report_html or "")
    report_date = report.group(1).strip() if report else None
    exact = bool(current and report_date and current == report_date)
    if not ai_enabled:
        state, description = "disabled", "AI自动研究当前关闭；如有研究页，只显示历史报告"
    elif job_outcome != "success":
        state, description = "failed", "本次AI研究生成失败；旧报告仅供历史查阅"
    elif not exact:
        state, description = "stale", "AI报告未匹配本期行情快照；旧报告仅供历史查阅"
    else:
        state, description = "current", "AI报告与本期选股快照一致"
    return {
        "state": state,
        "description": description,
        "snapshot": current,
        "report_snapshot": report_date,
        "ai_step": str(job_outcome),
    }


def update_index(index_html, status, has_report):
    label = status["description"]
    text = (
        '<p class="ai-nav" data-report-state="' + status["state"] + '">'
        '<a href="./ai.html">🧠 DeepSeek AI 股票研究</a>'
        ' <strong>' + label + '</strong>'
        ' <span>选股快照：' + status["snapshot"] +
        '；报告快照：' + (status["report_snapshot"] or "未核实") + '</span></p>'
    ) if has_report else (
        '<p class="ai-nav" data-report-state="missing">⚠️ AI研究页尚不存在，'
        '当前只有规则选股榜。</p>'
    )
    if not NAV.search(index_html):
        raise ValueError("无法定位首页AI状态导航，拒绝静默发布错误状态")
    return NAV.sub(lambda _: text, index_html, count=1)


def main():
    data = json.loads((DOCS / "data.json").read_text(encoding="utf-8"))
    home_path = DOCS / "index.html"
    report_path = DOCS / "ai.html"
    enabled = os.getenv("AI_ENABLED", "").lower().strip() == "true"
    outcome = os.getenv("AI_JOB_STATUS", "skipped").strip()
    report_html = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    state = research_freshness(data, report_html, enabled, outcome)
    rewritten = update_index(home_path.read_text(encoding="utf-8"), state,
                             has_report=bool(report_html))
    home_path.write_text(rewritten, encoding="utf-8")
    state["recorded_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (DOCS / "ai-status.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("AI发布状态:", state["state"], "榜单:", state["snapshot"],
          "AI:", state["report_snapshot"], flush=True)
    if enabled and state["state"] != "current":
        print("::warning::AI研究与当前榜单不同步；旧报告已明确标记", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
