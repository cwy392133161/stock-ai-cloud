"""Publish a truthful research freshness banner without changing trading data.

This runs AFTER the optional AI step, even when the AI runner fails.
An old ai.html remains accessible as an archive, never labeled current.
"""
import json
import os
import pathlib
import re
import sys
from html import escape
from datetime import datetime, timezone
from market_schedule import SCHEDULE_BLOCK_REASONS

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"

REPORT_SNAPSHOT = re.compile(r"行情快照[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})")
NAV = re.compile(r'<p class="ai-nav"(?:\s+[^>]*)?>.*?</p>', flags=re.S)

APPROVED = re.compile(r'<meta name="research-approved-count" content="([0-9]+)">')
LIMITED = re.compile(r'<meta name="research-limited-count" content="([0-9]+)">')
TOTAL = re.compile(r'<meta name="research-total-count" content="([0-9]+)">')


def _single_meta(report_html, name):
    """Read one canonical renderer marker; duplicates fail closed."""
    values = re.findall(r'<meta name="' + re.escape(name) + r'" content="([^"]*)">',
                        report_html or "")
    return values[0] if len(values) == 1 else None


def market_freshness(current, market_state, screening_outcome, previous):
    """Separate this run's gate from the unresolved result of actual screening.

    A skipped job, weekend, pre-close check or report-only run cannot establish
    that the quote source recovered. Only successful screening with a newer
    snapshot can clear a known failure.
    """
    prior_failure = (previous.get("market_state") == "failed" or
                     previous.get("last_screening_outcome") == "failure")
    failure_snapshot = previous.get("market_failure_snapshot") or previous.get("snapshot", "")
    if market_state == "failed" or screening_outcome == "failure":
        # Keep the original failure's recovery boundary across later skipped,
        # unchanged or regressed snapshots. A new failure may only raise it.
        valid = []
        for value in (failure_snapshot, current):
            try:
                valid.append((datetime.strptime(value, "%Y-%m-%d %H:%M"), value))
            except (TypeError, ValueError):
                pass
        failure_snapshot = max(valid)[1] if valid else None
    advanced = False
    try:
        advanced = (datetime.strptime(current, "%Y-%m-%d %H:%M") >
                    datetime.strptime(failure_snapshot, "%Y-%m-%d %H:%M"))
    except (TypeError, ValueError):
        pass
    recovered = screening_outcome == "success" and advanced
    failed = (market_state == "failed" or screening_outcome == "failure" or
              (prior_failure and not recovered))
    effective = "failed" if failed else ("updated" if screening_outcome == "success"
                                         else market_state or "not_rechecked")
    if effective == "failed":
        description = ("最近一次行情更新失败后尚无更新的成功快照；仍显示旧行情，"
                       "不能视作本期结果或行情源已恢复")
    elif effective == "updated":
        description = "本次选股步骤成功；行情时点以实际快照为准，不代表实时行情"
    else:
        description = "本次未重新检查行情源；保留旧快照，不能据此判断行情源已恢复"
    if market_state in SCHEDULE_BLOCK_REASONS:
        description += "。" + SCHEDULE_BLOCK_REASONS[market_state]
    elif market_state == "pre_close":
        description += "。本次因未到北京时间17:00发布窗口跳过选股"
    elif market_state == "weekend":
        description += "。本次因北京时间周末跳过选股"
    elif screening_outcome not in ("success", "failure"):
        description += "。本次没有执行选股"
    return {
        "market_state": effective,
        "market_check_state": market_state or "not_rechecked",
        "market_failure_snapshot": failure_snapshot if failed else None,
        "market_description": description,
        "screening_outcome": screening_outcome or "not_rechecked",
        "last_screening_outcome": (screening_outcome if screening_outcome in ("success", "failure")
                                   else previous.get("last_screening_outcome") or
                                   ("failure" if prior_failure else None)),
    }


def research_freshness(snapshot, report_html, ai_enabled, job_outcome, market_state=None, ai_skipped=False,
                       review_required=False, previous_status=None, screening_outcome=None):
    """Return status with exact full-minute snapshot identity."""
    current = str(snapshot.get("updated", "")).strip()
    # Status decoration is not source evidence for the report's original date.
    undecorated = BANNER.sub("", MARKET_BANNER.sub("", report_html or ""))
    report = REPORT_SNAPSHOT.search(undecorated)
    report_date = report.group(1).strip() if report else None
    exact = bool(current and report_date and current == report_date)
    ok = APPROVED.search(report_html or "")
    limited = LIMITED.search(report_html or "")
    total = TOTAL.search(report_html or "")
    verified_count = int(ok.group(1)) if ok else None
    limited_count = int(limited.group(1)) if limited else 0
    total_count = int(total.group(1)) if total else None
    previous = previous_status if isinstance(previous_status, dict) else {}
    market = market_freshness(current, market_state, screening_outcome, previous)
    market_state = market["market_state"]
    generation = _single_meta(report_html, "research-generation-id")
    audit_version = _single_meta(report_html, "research-audit-version")
    was_frozen = previous.get("state") == "review-required" or previous.get("review_required") is True
    frozen_generation = (generation if review_required else
                         previous.get("review_frozen_generation", previous.get("research_generation_id")))
    count_markers = [_single_meta(report_html, name) for name in (
        "research-approved-count", "research-limited-count", "research-total-count")]
    counts_valid = (all(value is not None and re.fullmatch(r"[0-9]+", value)
                        for value in count_markers) and
                    int(count_markers[2]) == 10 and
                    int(count_markers[0]) + int(count_markers[1]) <= 10)
    # Only the current run's success can lift a hold. Never reuse ai_step from
    # previous status, a content hash, new banners, or a mere skipped refresh.
    new_audited_generation = (job_outcome == "success" and exact and
                             audit_version == "evidence-scope-v2" and
                             bool(generation and re.fullmatch(
                                 r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", generation)) and
                             generation != frozen_generation and counts_valid)
    frozen = bool(review_required or (was_frozen and not new_audited_generation))
    if frozen:
        state, description = ("review-required", "既有报告发现归因与主体范围问题，需重新审核；"
                              "尚未完成复审，原通过标记不可作为当前核验结论。"
                              "原计数仅为历史生成记录，未经此次复审确认")
    elif market_state in SCHEDULE_BLOCK_REASONS:
        state, description = "market-schedule-blocked", SCHEDULE_BLOCK_REASONS[market_state]
    elif market_state == "weekend":
        state, description = ("market-weekend", "周末非交易日：保留最近一次有效行情快照，"
                              "未重新选股，也未自动触发AI研究")
    elif market_state == "pre_close":
        state, description = ("market-awaiting-close", "尚未到北京时间17:00收盘发布窗口：保留旧快照，"
                              "未重新选股，也未自动触发AI研究；本次未检查行情源是否可用")
    elif market_state == "failed":
        state, description = ("market-unavailable", "行情更新失败后尚无更新的成功快照："
                              "保留旧行情；研究报告是否更新须另看报告时点与AI生成记录")
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
        **market,
        "research_approved": verified_count,
        "research_limited": limited_count,
        "research_total": total_count,
        "review_required": frozen,
        "review_frozen_generation": frozen_generation if frozen else None,
        "research_generation_id": generation,
        "research_audit_version": audit_version,
        "research_counts_status": "historical-unreviewed" if frozen else "generation-record",
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
    return update_market_banner(NAV.sub(lambda _: text, index_html, count=1), status)


BANNER = re.compile(r'<p class="research-publication-alert"[^>]*>.*?</p>', re.S)
MARKET_BANNER = re.compile(r'<p class="market-publication-alert"[^>]*>.*?</p>', re.S)


def update_market_banner(page_html, status):
    """Market outages remain visible independently of the AI review hold."""
    page_html = MARKET_BANNER.sub("", page_html)
    header = re.search(r"</h1>", page_html)
    if not header:
        raise ValueError("页面缺少标题，无法显式标记行情状态")
    banner = ('<p class="market-publication-alert" data-market-state="' +
              escape(status.get("market_state", "not_rechecked"), quote=True) +
              '" style="border:1px solid #b45309;padding:12px;border-radius:8px">'
              '<b>行情更新状态：</b>' + escape(status.get("market_description", "本次未复查行情")) +
              '。榜单数据时点：' + escape(status["snapshot"]) + '（北京时间）。</p>')
    return page_html[:header.end()] + banner + page_html[header.end():]


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
    return update_market_banner(report_html[:header.end()] + banner + report_html[header.end():], status)


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
    # Step-scoped input: offline tests run inside scheduled Actions too and
    # must not inherit the runner's ambient GITHUB_EVENT_NAME as an instruction.
    scheduled = os.getenv("SCHEDULE_EVENT_NAME", "") == "schedule"
    schedule_state = os.getenv("SCHEDULE_STATE", "") if scheduled else "not_scheduled"
    if scheduled and schedule_state not in {"schedule_ready", *SCHEDULE_BLOCK_REASONS}:
        schedule_state = "schedule_invalid_context"
    # report_only deliberately uses the last published quote snapshot.
    # On a weekend or real market outage no new financial report is generated.
    market_state = None
    if not report_only and screening == "failure":
        market_state = "failed"
    elif not report_only and schedule_state in SCHEDULE_BLOCK_REASONS:
        market_state = schedule_state
    elif not report_only and weekday == "false":
        market_state = "weekend"
    elif not report_only and weekday == "true" and ready == "false":
        market_state = "pre_close"
    report_html = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    status_path = DOCS / "ai-status.json"
    previous = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
    if not isinstance(previous, dict):
        raise ValueError("已有发布状态格式异常，不能安全判断复审冻结状态")
    state = {**previous, **research_freshness(data, report_html, enabled, outcome,
                               market_state=market_state,
                               ai_skipped=os.getenv("SKIP_AI", "").lower() == "true",
                               review_required=os.getenv("RESEARCH_REVIEW_REQUIRED", "").lower().strip() == "true",
                               previous_status=previous,
                               screening_outcome=screening if not report_only else None)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state["schedule_state"] = schedule_state
    if scheduled:
        state["last_schedule_state"] = schedule_state
        state["last_schedule_observed_at"] = os.getenv("SCHEDULE_OBSERVED_AT", "") or now
        state["last_schedule_run_id"] = os.getenv("GITHUB_RUN_ID", "")
        state["last_schedule_run_attempt"] = os.getenv("GITHUB_RUN_ATTEMPT", "")
    if not report_only and screening in ("success", "failure"):
        state["last_screening_recorded_utc"] = now
    rewritten = update_index(home_path.read_text(encoding="utf-8"), state,
                             has_report=bool(report_html))
    home_path.write_text(rewritten, encoding="utf-8")
    if report_html:
        report_path.write_text(update_report_banner(report_html, state), encoding="utf-8")
    state["recorded_utc"] = now
    (DOCS / "ai-status.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("AI发布状态:", state["state"], "榜单:", state["snapshot"],
          "AI:", state["report_snapshot"], flush=True)
    if state["market_state"] == "failed":
        print("::warning::行情更新仍未恢复：" + state["market_description"], flush=True)
    if schedule_state in SCHEDULE_BLOCK_REASONS:
        print("::warning::" + SCHEDULE_BLOCK_REASONS[schedule_state], flush=True)
    if enabled and state["state"] != "current":
        print("::warning::AI研究尚未全部通过发布门槛：" + state["description"], flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

