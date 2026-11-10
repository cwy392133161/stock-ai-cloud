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
NAV = re.compile(r'<p class="ai-nav"(?:\s+[^>]*)?>.*?</p>', flags=re.S)

APPROVED = re.compile(r'<meta name="research-approved-count" content="([0-9]+)">')
LIMITED = re.compile(r'<meta name="research-limited-count" content="([0-9]+)">')
TOTAL = re.compile(r'<meta name="research-total-count" content="([0-9]+)">')


def research_freshness(snapshot, report_html, ai_enabled, job_outcome, market_state=None, ai_skipped=False):
    """Return status with exact full-minute snapshot identity."""
    current = str(snapshot.get("updated", "")).strip()
    report = REPORT_SNAPSHOT.search(report_html or "")
    report_date = report.group(1).strip() if report else None
    exact = bool(current and report_date and current == report_date)
    ok = APPROVED.search(report_html or "")
    limited = LIMITED.search(report_html or "")
    total = TOTAL.search(report_html or "")
    verified_count = int(ok.group(1)) if ok else None
    limited_count = int(limited.group(1)) if limited else 0
    total_count = int(total.group(1)) if total else None
    if market_state == "weekend":
        state, description = ("market-weekend", "周末非交易日：保留最近一次有效行情快照，"
                              "未重新选股，也未自动触发AI研究")
    elif market_state == "pre_close":
        state, description = ("market-awaiting-close", "尚未到北京时间17:00收盘发布窗口：保留旧快照，"
                              "未重新选股，也未自动触发AI研究；这不是行情源故障")
    elif market_state == "failed":
        state, description = ("market-unavailable", "行情数据源故障：本次选股未成功，"
                              "保留旧快照；未对其重新生成AI研究")
    elif ai_skipped and job_outcome == "skipped":
        state, description = ("skipped", "本次按要求跳过AI研究；未生成新报告，旧报告仅供历史查阅")
    elif not ai_enabled:
        state, description = "disabled", "AI自动研究当前关闭；如有研究页，只显示历史报告"
    elif job_outcome != "success":
        state, description = "failed", "本次AI研究生成失败；旧报告仅供历史查阅"
    elif not exact:
        state, description = "stale", "AI报告未匹配本期行情快照；旧报告仅供历史查阅"
    elif verified_count is None or total_count is None or total_count != 10:
        state, description = "unverified", "本期报告缺少独立研究质量标记，不能视作深度研究"
    elif verified_count + limited_count > total_count:
        state, description = "unverified", "研究状态数量异常，禁止标记为通过"
    elif verified_count == 0 and limited_count == 0:
        state, description = "facts-only", "本期研究资料已更新，但10只均未通过AI解释发布门槛"
    elif verified_count == 0:
        state, description = ("limited", "本期有" + str(limited_count) + "/" +
                              str(total_count) + "只显示条件性研究，缺少可逐字核对的官方原文主张")
    elif verified_count < total_count:
        state, description = ("partial", "本期研究已更新，" + str(verified_count) +
                              "/" + str(total_count) + "只有逐字证据研究，另有" +
                              str(limited_count) + "只条件性研究")
    else:
        state, description = "current", "本期10只股票研究均通过基础证据发布门槛"
    return {
        "state": state,
        "description": description,
        "snapshot": current,
        "report_snapshot": report_date,
        "ai_step": str(job_outcome),
        "market_state": market_state or "not_rechecked",
        "research_approved": verified_count,
        "research_limited": limited_count,
        "research_total": total_count,
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


BANNER = re.compile(r'<p class="research-publication-alert"[^>]*>.*?</p>', re.S)


def update_report_banner(report_html, status):
    """Warn even when readers open the old AI URL without visiting home."""
    if not report_html:
        return report_html
    state = status["state"]
    report_html = BANNER.sub("", report_html)
    message = status["description"]
    banner = (
        '<p class="research-publication-alert" data-research-state="' +
        state + '" style="border:1px solid #cbd5e1;padding:12px;border-radius:8px">'
        '<b>研究发布状态：</b>' + message + '。'
        '榜单时点：' + status["snapshot"] + '；本页原研究时点：' +
        (status["report_snapshot"] or "未核实") + '。</p>'
    )
    # Build-generated pages use a predictable H1; no generic insertion into
    # arbitrary untrusted HTML and no duplicate banners on repeated runs.
    header = re.search(r"</h1>", report_html)
    if not header:
        raise ValueError("AI页面缺少标题，无法显式标记旧报告")
    return report_html[:header.end()] + banner + report_html[header.end():]


def main():
    data = json.loads((DOCS / "data.json").read_text(encoding="utf-8"))
    home_path = DOCS / "index.html"
    report_path = DOCS / "ai.html"
    enabled = os.getenv("AI_ENABLED", "").lower().strip() == "true"
    outcome = os.getenv("AI_JOB_STATUS", "skipped").strip()
    report_only = os.getenv("REPORT_ONLY", "false").lower() == "true"
    screening = os.getenv("SCREENING_OUTCOME", "").strip()
    weekday = os.getenv("TRADING_WEEKDAY", "").strip()
    ready = os.getenv("MARKET_READY", "").strip()
    # report_only deliberately uses the last published quote snapshot.
    # On a weekend or real market outage no new financial report is generated.
    market_state = None
    if not report_only and weekday == "false":
        market_state = "weekend"
    elif not report_only and weekday == "true" and ready == "false":
        market_state = "pre_close"
    elif not report_only and screening == "failure":
        market_state = "failed"
    report_html = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    state = research_freshness(data, report_html, enabled, outcome,
                               market_state=market_state,
                               ai_skipped=os.getenv("SKIP_AI", "").lower() == "true")
    rewritten = update_index(home_path.read_text(encoding="utf-8"), state,
                             has_report=bool(report_html))
    home_path.write_text(rewritten, encoding="utf-8")
    if report_html:
        report_path.write_text(update_report_banner(report_html, state), encoding="utf-8")
    state["recorded_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (DOCS / "ai-status.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("AI发布状态:", state["state"], "榜单:", state["snapshot"],
          "AI:", state["report_snapshot"], flush=True)
    if enabled and state["state"] != "current":
        print("::warning::AI研究尚未全部通过发布门槛：" + state["description"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

