"""Generate and publish the evidence-enriched edition beside the original report."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path

from bs4 import BeautifulSoup

from ai_report import _atomic_write, _validate_evidence_snapshot, generate_report, render_bank_evidence
from archive_research import _read_research
from archived_a_snapshot import load_snapshot
from bank_evidence import bank_metrics
from publish_guard import research_freshness, update_index, update_report_banner
from research_trends import annotate_current

REPORT = "ai-bank.html"
REVIEWED_REPORT = "ai-bank-reviewed.html"
EVIDENCE = "a-bank-evidence.json.gz"


def inputs(archive):
    snapshot = load_snapshot(archive)
    stocks = snapshot["a_shares"][:5]
    with gzip.open(archive / EVIDENCE, "rt", encoding="utf-8") as stream:
        evidence = json.load(stream)
    day = archive.name
    _validate_evidence_snapshot(evidence, [("A股", s) for s in stocks], day)
    counts = {}
    for stock in stocks:
        code = stock["代码"]
        row = evidence[code]
        metrics = bank_metrics(stock, row["finance"], row["disclosures"], day)
        if not metrics or not metrics["指标"]:
            raise ValueError(f"{code} 缺少同报告期银行指标，无法生成该版本")
        counts[code] = len(metrics["指标"])
    return snapshot, evidence, counts


def generate(archive, output, cache):
    snapshot, evidence, counts = inputs(archive)
    if (archive / REPORT).exists():
        output.write_bytes((archive / REPORT).read_bytes())
        return
    print("本期已复核的发行人及银行指标数", counts, flush=True)
    generate_report(snapshot, output, markets=("A股",), evidence_snapshot=evidence, cache_dir=cache)


def publish(archive, report, *, reviewed=False):
    snapshot, evidence, counts = inputs(archive)
    text = report.read_text(encoding="utf-8")
    details = _read_research(text, archive.name, snapshot["a_shares"][:5])
    if details["snapshot"] != snapshot["updated"]:
        raise ValueError("银行版报告与行情快照时间不一致")
    for article, stock in zip(BeautifulSoup(text, "html.parser").select("article"), snapshot["a_shares"]):
        row = evidence[stock["代码"]]
        metrics = bank_metrics(stock, row["finance"], row["disclosures"], archive.name)
        expected = BeautifulSoup(render_bank_evidence(metrics), "html.parser").section
        if article.select("section.bank-evidence") != [expected]:
            raise ValueError("生成页缺少可定位的原始财报指标")
    filename = REVIEWED_REPORT if reviewed else REPORT
    destination = archive / filename
    if destination.exists() and destination.read_text(encoding="utf-8") != text:
        raise ValueError("该期银行研究版本已经发布，不能覆盖")
    details.update(report=filename, evidence=EVIDENCE, metric_counts=counts,
                   evidence_sha256=hashlib.sha256((archive / EVIDENCE).read_bytes()).hexdigest())
    for article in details["articles"]:
        article["href"] = f'{filename}#stock-{article["code"]}'
    run = os.getenv("GITHUB_RUN_ID")
    if run:
        details["run"] = (f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{run}')
    summary_path = archive / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    section = "reviewed_bank_research" if reviewed else "bank_research"
    if reviewed:
        original = summary["bank_research"]
        tag = BeautifulSoup(text, "html.parser").select_one('meta[name="research-original-generation-id"]')
        if (not tag or tag.get("content") != original["generation_id"] or
                details["generation_id"] == original["generation_id"]):
            raise ValueError("复审文件无法关联原付费回复生成记录")
        details.update(original_generation_id=original["generation_id"], paid_run=original.get("run"))
    existing = summary.get(section)
    if existing and existing.get("generation_id") == details["generation_id"]:
        if existing.get("run"):
            details["run"] = existing["run"]
    summary[section] = details
    path = archive / "index.html"
    page = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    if not destination.exists():
        _atomic_write(destination, text)
    _atomic_write(summary_path, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if not page.find("a", href="./" + filename):
        nav = page.select_one('a[href="./ai.html"]').parent
        links = ([(filename, "五家银行研究（按最新银行口径复审）")] if reviewed else
                 [(filename, "五家银行指标与新版 AI 研究"), (EVIDENCE, "下载新版研究依据")])
        for href, label in links:
            link = page.new_tag("a", href="./" + href)
            link.string = label
            nav.extend([" · ", link])
        _atomic_write(path, str(page).rstrip() + "\n")

    docs = archive.parent.parent
    current = json.loads((docs / "data.json").read_text(encoding="utf-8"))
    if (current["updated"] != snapshot["updated"] or [r["代码"] for r in current["a_shares"][:5]] != list(counts)):
        return
    status_path = docs / "ai-status.json"
    old = json.loads(status_path.read_text(encoding="utf-8"))
    fresh = research_freshness(current, text, True, "success", previous_status=old, report_only=True)
    # Publishing a new interpretation of the same close is not a market run.
    old.update({k: v for k, v in fresh.items() if not k.startswith("market_") and
                k not in ("screening_outcome", "last_screening_outcome")})
    old.update(research_archive=f"./history/{archive.name}/{filename}",
               recorded_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if run:
        old["research_run"] = details["run"]
    if reviewed:
        old["paid_research_run"] = details["paid_run"]
    home = docs / "index.html"
    home.write_text(update_index(home.read_text(encoding="utf-8"), old, True), encoding="utf-8")
    (docs / "ai.html").write_text(update_report_banner(text, old), encoding="utf-8")
    status_path.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    annotate_current(docs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("generate", "publish"))
    parser.add_argument("archive", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--reviewed", action="store_true")
    args = parser.parse_args()
    if args.action == "generate":
        generate(args.archive, args.report, args.cache)
    else:
        publish(args.archive, args.report, reviewed=args.reviewed)
