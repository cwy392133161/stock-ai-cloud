"""Point-in-time A-share screening comparisons on one eligible universe."""
from collections import Counter
from datetime import date
from html import escape
import json
from pathlib import Path

import pandas as pd

from market_quality import numeric_column
from peer_valuation import safe_number


SCORE_VERSION = "a-legacy-v1+comparisons-v2"
MIN_SECTOR_SIZE = 5
STRATEGIES = {
    "legacy": "原规则",
    "revised": "修订估值",
    "industry": "行业内排名",
    "sensitivity": "活跃度加权",
}
METRICS = {"legacy": "评分", "revised": "修订评分", "industry": "行业百分位",
           "sensitivity": "扰动评分"}


def classifications(frame, trade_day, directory=None):
    directory = Path(directory) if directory else Path(__file__).parent / "industry"
    day = date.fromisoformat(trade_day)
    # A release without an intraday publication timestamp is usable the next day.
    releases = sorted(p for p in directory.glob("????-??-??.json")
                      if date.fromisoformat(p.stem) < day)
    if releases:
        release = json.loads(releases[-1].read_text(encoding="utf-8"))
        mapping = {}
        for sector in release["sectors"].values():
            for code in sector["members"].split():
                if code in mapping:
                    raise ValueError(f"中上协分类表代码重复 {code}")
                mapping[code] = sector["name"]
        if release["published"] != releases[-1].stem or len(mapping) != release["count"]:
            raise ValueError("中上协分类表日期或记录数不符")
        labels = frame["代码"].astype(str).map(mapping)
        source = {k: release[k] for k in ("title", "published", "source_url", "pdf_sha256")}
    else:
        labels = frame.get("行业", pd.Series(None, index=frame.index, dtype=object))
        labels = labels.astype("string").str.strip()
        labels = labels.mask(labels.str.lower().isin(["", "其他", "未知", "-", "—", "nan", "none"]))
        source = {"title": "当日行情供应商行业字段", "published": trade_day}
    return labels, {**source, "matched": int(labels.notna().sum()), "total": len(labels)}


def valuation_component(values, label, extreme):
    """All finite positive multiples compete. Loss/negative book value get no credit.

    The midpoint of the worst quartile (12.5) explicitly discounts missing
    evidence. This disclosed modeling choice applies only to the comparison.
    """
    positive = values.where(values > 0)
    points = 100 * (1 - positive.rank(pct=True))
    points = points.mask(values <= 0, 0.0).fillna(12.5)
    status = pd.Series("正倍数", index=values.index)
    status.loc[values >= extreme] = "高倍数（参与排名）"
    status.loc[values == 0] = "零值（不可比）"
    status.loc[values < 0] = "亏损" if label == "PE" else "净资产为负"
    status.loc[values.isna()] = "缺失"
    return points, status


def compare_scores(ranked, day, n=20, directory=None):
    d = ranked.copy()
    d["行业"], source = classifications(d, day, directory)
    pe, d["PE状态"] = valuation_component(numeric_column(d, "市盈率-动态"), "PE", 100)
    pb, d["PB状态"] = valuation_component(numeric_column(d, "市净率"), "PB", 10)
    d["修订估值分"] = .6 * pe + .4 * pb
    common = .4 * d["_trend"] + .15 * d["_size"]
    d["修订评分"] = common + .25 * d["_active"] + .2 * d["修订估值分"]
    d["扰动评分"] = common + .30 * d["_active"] + .15 * d["_value"]
    groups = d.groupby("行业")["修订评分"]
    d["同行数"] = groups.transform("count")
    # Rank revision market-wide and inside each verified group; using the
    # midpoint keeps a singleton from achieving a perfect percentile.
    d["行业名次"] = groups.rank(method="min", ascending=False)
    d["行业百分位"] = (100 * (groups.rank(method="average") - .5) / d["同行数"]
                  ).where(d["同行数"] >= MIN_SECTOR_SIZE)
    source["rankable"] = int(d["行业百分位"].notna().sum())
    tops = {"legacy": d.head(n)}
    for key in ("revised", "industry", "sensitivity"):
        keys = [METRICS[key], "修订评分", "代码"] if key == "industry" else [METRICS[key], "代码"]
        tops[key] = (d.dropna(subset=[METRICS[key]])
                     .sort_values(keys, ascending=[False] * (len(keys) - 1) + [True]).head(n))
    selections = {k: top["代码"].tolist() for k, top in tops.items()}
    overlaps, concentrations = {}, {}
    for k, top in tops.items():
        overlaps[k] = {f"top{cut}": len(set(selections["legacy"][:cut]) & set(selections[k][:cut]))
                       for cut in (5, n)}
        concentrations[k] = dict(Counter(top["行业"].fillna("未分类")).most_common())
    union = {code for codes in selections.values() for code in codes}
    stocks = {}
    market_ranks = {k: {code: pos for pos, code in enumerate(
        (d if k == "legacy" else d.sort_values([METRICS[k], "代码"], ascending=[False, True]))["代码"], 1)}
        for k in ("legacy", "revised")}
    for _, row in d.loc[d["代码"].isin(union)].iterrows():
        stocks[row["代码"]] = {
            "name": row["名称"], "industry": row["行业"] if pd.notna(row["行业"]) else None,
            "pe": safe_number(row["市盈率-动态"]), "pb": safe_number(row["市净率"]),
            "pe_status": row["PE状态"], "pb_status": row["PB状态"],
            "value_legacy": safe_number(row["估值分"]), "value_revised": safe_number(row["修订估值分"]),
            "industry_rank": safe_number(row["行业名次"]), "industry_size": safe_number(row["同行数"]),
            "market_ranks": {k: ranks[row["代码"]] for k, ranks in market_ranks.items()},
            "scores": {k: safe_number(row[col]) for k, col in METRICS.items()},
        }
    analysis = {"version": SCORE_VERSION, "trade_date": day, "universe": len(d), "source": source,
                "min_sector_size": MIN_SECTOR_SIZE, "stocks": stocks, "overlaps": overlaps,
                "concentrations": concentrations,
                "valuation_counts": {s: {str(k): int(v) for k, v in d[s].value_counts().items()}
                                     for s in ("PE状态", "PB状态")}}
    return d, selections, analysis


def comparison_html(snapshot, *, history_href="./history"):
    analysis = snapshot.get("ranking_analysis")
    if not analysis:
        return ""
    source, selections = analysis["source"], snapshot["comparison_selections"]
    day = date.fromisoformat(analysis["trade_date"]).isoformat()
    title = escape(source["title"])
    if source.get("source_url"):
        title = f'<a href="{escape(source["source_url"], quote=True)}">{title}</a>'
    blocks = [
        "<h2>同日评分对照</h2>",
        f'<p class="sub">行业取自{title}（{escape(source["published"])}）。'
        f'可选股票 {source["total"]} 只，已分类 {source["matched"]} 只，'
        f'未分类 {source["total"]-source["matched"]} 只。至少 {analysis["min_sector_size"]} 只'
        f'同行才进入行业对照榜，覆盖 {source["rankable"]} 只。</p>',
        '<p class="sub">修订估值中，所有正 PE、PB 均参与排名，倍数越低得分越高。'
        'PE 达到 100 倍或 PB 达到 10 倍标为高倍数。'
        '负值、零值计 0 分，缺失取最低四分位的中点 12.5 分，PE 占 60%，PB 占 40%。'
        '行业榜按修订总分在本行业的百分位排序。活跃度加权对照沿用原估值，'
        '活跃度从 25% 提至 30%，估值从 20% 降至 15%。</p>',
        f'<p class="sub"><a href="{escape(history_href, quote=True)}/performance.html">后续交易日表现</a> · '
        f'<a href="{escape(history_href, quote=True)}/{day}/trend-audit.html">趋势因子交叉核验</a></p>',
    ]
    for key, label in STRATEGIES.items():
        codes = selections[key]
        breakdown = analysis["concentrations"][key]
        parts = "、".join(f"{escape(name)} {cnt}" for name, cnt in breakdown.items()) or "无"
        overlap = analysis["overlaps"][key]
        overlap_line = (f' · 与原榜重合：前五 {overlap["top5"]} 只，前二十 {overlap["top20"]} 只'
                        if key != "legacy" else "")
        blocks.append(f'<details><summary>{label} · {len(codes)} 只{overlap_line}</summary>'
                      f'<p class="sub">行业分布：{parts}。</p>'
                      '<div class="comparison-scroll"><table class="comparison"><tr>'
                      '<th>名次 / 股票</th><th>全市场名次<br>原 → 修订</th>'
                      '<th>行业 / 同行名次</th><th>动态 PE / PB</th>'
                      '<th>估值分<br>原 → 修订</th><th>榜单得分</th></tr>')
        for pos, code in enumerate(codes, 1):
            s = analysis["stocks"][code]
            rank = f'{int(s["industry_rank"])}/{int(s["industry_size"])}' if s["industry_rank"] else "—"
            def ratio(field):
                raw = s[field]
                value = f"{raw:g}" if raw is not None else "—"
                return f'{value} · {escape(s[field+"_status"])}'
            blocks.append(f'<tr><td>{pos}. {escape(s["name"])}<br>{escape(code)}</td>'
                          f'<td>{s["market_ranks"]["legacy"]} → {s["market_ranks"]["revised"]}</td>'
                          f'<td>{escape(s["industry"] or "未分类")}<br>{rank}</td>'
                          f'<td>PE {ratio("pe")}<br>PB {ratio("pb")}</td>'
                          f'<td>{s["value_legacy"]:.1f} → {s["value_revised"]:.1f}</td>'
                          f'<td>{s["scores"][key]:.1f}</td></tr>')
        blocks.append("</table></div></details>")
    return "\n".join(blocks)
