"""Validate an archived A-share closing universe before generating paid research."""
import argparse
import gzip
import json
from datetime import date
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from market_quality import validate_market_data
from picker import pick_rows, score_a


def load_snapshot(archive):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evidence", type=Path, help="Frozen financial data and official notices")
    args = parser.parse_args()
    snapshot = load_snapshot(args.archive)
    if (args.archive / "ai.html").exists():
        raise SystemExit("该期研究已生成，请复用已归档的研究页")
    evidence = None
    if args.evidence:
        with gzip.open(args.evidence, "rt", encoding="utf-8") as stream:
            evidence = json.load(stream)
    from ai_report import generate_report
    generate_report(snapshot, args.output, markets=("A股",), evidence_snapshot=evidence)
