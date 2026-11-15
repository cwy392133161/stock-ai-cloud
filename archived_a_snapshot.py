"""Validate an archived A-share closing universe before generating paid research."""
import argparse
import gzip
import json
from datetime import date, datetime
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from market_quality import validate_market_data
from archive_execution import merge_execution
from picker import pick_rows, score_a_legacy as score_a


def _inputs(archive):
    summary = json.loads((archive / "summary.json").read_text(encoding="utf-8"))
    day = date.fromisoformat(summary["trade_date"])
    if day.isoformat() != archive.name:
        raise ValueError("归档路径与交易日期不一致")
    market = summary["a_shares"]
    with ZipFile(archive / "a-quotes.zip") as quotes:
        quality = json.loads(quotes.read("quality.json"))
        with quotes.open("verified-friday-quotes.csv") as stream:
            frame = pd.read_csv(stream, dtype={"代码": str, "报价时间": str},
                                encoding="utf-8-sig", float_precision="round_trip")
    if (quality != market["quality"] or quality["交易日期"] != day.isoformat()
            or len(frame) != quality["证券总数"]):
        raise ValueError("A 股归档日期、证券数量或来源记录不一致")
    stamps = pd.to_datetime(frame["报价时间"], format="%Y%m%d%H%M%S", errors="raise")
    if not ((stamps.dt.date == day) & (stamps.dt.hour >= 15)).all():
        raise ValueError("A 股归档包含其他交易日或收盘前报价")
    verified = validate_market_data(frame, "A股", quality["行情来源"])
    if verified["有效股票数"] != quality["有效股票数"]:
        raise ValueError("A 股有效行情数与归档不符")
    frame = merge_execution(archive, frame)
    frame.attrs["quality"] = quality
    return market, frame, stamps


def load_snapshot(archive):
    market, frame, stamps = _inputs(archive)
    eligible = score_a(frame)
    published = market["top20"]
    if len(eligible) != market["eligible"] or len(published) != 20:
        raise ValueError("A 股可选范围或榜单数量与归档不符")
    codes = [row["代码"] for row in published]
    if (len(set(codes)) != 20 or not set(codes).issubset(eligible["代码"])
            or [row["评分"] for row in published] != eligible["评分"].head(20).tolist()):
        raise ValueError("归档榜单与全市场最高二十个分数不符")
    # Scores are rounded to one decimal. Tied rows can appear in a different
    # order under NumPy's platform-specific quicksort. Keep their archived
    # order while independently recomputing the factors of every named stock.
    ordered = eligible.set_index("代码", drop=False).loc[codes]
    ranked = pick_rows(ordered, "A", 20)
    quote_times = dict(zip(frame["代码"], frame["报价时间"]))
    for result, saved in zip(ranked, published):
        fields = [key for key, value in result.items() if saved.get(key) != value]
        if saved["报价时间"] != quote_times[result["代码"]]:
            fields.append("报价时间")
        if fields:
            raise ValueError(f"A 股评分或行情与存档不符：{result['代码']} {fields}")
    return {"updated": stamps.max().strftime("%Y-%m-%d %H:%M"),
            "a_shares": published}


def stage_comparisons(archive, docs):
    """Recompute into a separate docs tree or reuse identical frozen inputs."""
    from a_share_history import archive_close
    from picker import build_a_snapshot
    original = load_snapshot(archive)
    market, frame, _ = _inputs(archive)
    codes = [x["代码"] for x in original["a_shares"]]
    rebuilt, ranked = build_a_snapshot(frame, original["updated"], published_codes=codes)
    collected = datetime.fromisoformat(market["quality"]["实际采集结束"])
    archive_close(docs, frame, ranked, rebuilt, collected_at=collected, score_version=rebuilt["score_version"])
    return original, codes, rebuilt


def rebuild_comparisons(archive):
    """Replay a frozen historical close without overwriting a newer live date."""
    from picker import render_index
    from publish_guard import update_index
    docs = archive.parent.parent
    original, codes, rebuilt = stage_comparisons(archive, docs)
    current = docs / "data.json"
    data = json.loads(current.read_text(encoding="utf-8"))
    if data.get("updated") == original["updated"]:
        if [r["代码"] for r in data["a_shares"]] != codes:
            raise ValueError("发布中的同日榜单与归档名单不同")
        for key in ("ranking_analysis", "comparison_selections", "score_version"):
            data[key] = rebuilt[key]
        current.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                           encoding="utf-8")
        status = json.loads((docs / "ai-status.json").read_text(encoding="utf-8"))
        has_report = (docs / "ai.html").is_file()
        html = update_index(render_index(data, has_report), status, has_report)
        (docs / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--rebuild-comparisons", action="store_true")
    parser.add_argument("--stage-comparisons", type=Path, metavar="DOCS",
                        help="Build only the frozen comparison snapshot in the specified docs directory")
    parser.add_argument("--evidence", type=Path, help="Frozen financial data and official notices")
    args = parser.parse_args()
    if args.stage_comparisons:
        stage_comparisons(args.archive, args.stage_comparisons)
        raise SystemExit(0)
    if args.rebuild_comparisons:
        rebuild_comparisons(args.archive)
        raise SystemExit(0)
    if not args.output:
        parser.error("请指定研究报告输出路径")
    snapshot = load_snapshot(args.archive)
    if (args.archive / "ai.html").exists():
        raise SystemExit("该期研究已生成，请复用已归档的研究页")
    evidence = None
    if args.evidence:
        with gzip.open(args.evidence, "rt", encoding="utf-8") as stream:
            evidence = json.load(stream)
    from ai_report import generate_report
    generate_report(snapshot, args.output, markets=("A股",), evidence_snapshot=evidence)
