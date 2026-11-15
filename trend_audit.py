"""Cross-check quoted A-share trend factors against provider-labelled adjusted closes."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
from html import escape
import json
import math
from pathlib import Path
import re
from threading import Event
import time

import requests
import trend_bars


CST = timezone(timedelta(hours=8))
TOLERANCE = 0.15  # percentage points, to allow for rounding of published prices/returns
PROVIDERS = {"tencent": "腾讯前复权日线", "eastmoney": "东方财富前复权日线",
             "sina": "新浪日线及前复权因子"}
OUTAGES = {"来源限流", "日线请求失败", "日线不可用", "交易日历不可用"}


def trading_sessions(start: date, end: date) -> list[str]:
    return sorted(trend_bars.sina_raw("sh000001", start, end))


def factor_anchors(sessions, day):
    dates = sorted({d for d in sessions if d <= day})
    if not dates or dates[-1] != day:
        raise ValueError("交易日历未覆盖本期收盘")
    before_year = [d for d in dates if d < day[:4] + "-01-01"]
    return {"60日涨跌幅": dates[-61] if len(dates) >= 61 else None,
            "年初至今涨跌幅": before_year[-1] if before_year else None}


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _symbol(code):
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError("证券代码格式异常")
    if code.startswith(("6", "9")) and not code.startswith("92"):
        return "sh" + code
    return ("bj" if code.startswith(("4", "8", "92")) else "sz") + code


def check_row(row, day, anchors, closes, unavailable=None, source=None):
    current = _number(closes.get(day))
    quote = _number(row.get("最新价"))
    # A different terminal close can signal a stale series or a subsequent
    # adjustment. In either case it cannot validate this frozen snapshot.
    aligned = (quote is not None and quote > 0 and current is not None and
               current > 0 and abs(current - quote) <= 0.00501)
    factors = {}
    for field, anchor in anchors.items():
        stated = _number(row.get(field))
        base = _number(closes.get(anchor))
        output = {"anchor": anchor, "anchor_close": base, "quoted_pct": stated}
        if unavailable:
            status = unavailable
        elif not aligned:
            status = "期末收盘未对齐"
        elif anchor is None or base is None or base <= 0:
            status = "缺少基准日复权收盘"
        elif stated is None:
            status = "原行情缺值"
        else:
            derived = (current / base - 1) * 100
            difference = derived - stated
            output.update(daily_pct=round(derived, 4), difference_pp=round(difference, 4))
            status = "一致" if abs(difference) <= TOLERANCE else "有差异"
        output["status"] = status
        factors[field] = output
    return {"code": row["代码"], "name": row.get("名称", ""), "quote_close": quote,
            "daily_close": current, "source": source, "factors": factors}


def audit(rows, day, *, cache_dir=None, skip_sources=()):
    trade_day = date.fromisoformat(day)
    start = min(date(trade_day.year - 1, 12, 1), trade_day - timedelta(days=125))
    try:
        sessions = trading_sessions(start, trade_day)
        anchors = factor_anchors(sessions, day)
    except (trend_bars.ProviderUnavailable, requests.RequestException, ValueError, KeyError, TypeError):
        sessions = []
        anchors = {"60日涨跌幅": None, "年初至今涨跌幅": None}
    methods = {"tencent": trend_bars.tencent, "eastmoney": trend_bars.eastmoney, "sina": trend_bars.sina}
    halted = {provider: Event() for provider in methods}
    for provider in skip_sources:
        halted[provider].set()
    if cache_dir:
        cache_dir.mkdir(exist_ok=True, parents=True)

    def one(row):
        code = row["代码"]
        symbol = _symbol(code)
        if not sessions:
            return check_row(row, day, anchors, {}, unavailable="交易日历不可用")
        caches = {p: (cache_dir / (f"{day}-{symbol}.json" if p == "tencent" else
                                   f"{day}-{p}-{symbol}.json")) if cache_dir else None for p in methods}
        for provider, cache in caches.items():
            if cache and cache.is_file():
                try:
                    bars = json.loads(cache.read_text(encoding="utf-8"))
                    if isinstance(bars, dict) and day in bars:
                        return check_row(row, day, anchors, bars, source=provider)
                except (OSError, ValueError):
                    pass
        for provider, load in methods.items():
            if halted[provider].is_set():
                continue
            try:
                bars = load(symbol, start, trade_day)
                if day not in bars:
                    continue
                cache = caches[provider]
                if cache:
                    cache.write_text(json.dumps(bars), encoding="utf-8")
                return check_row(row, day, anchors, bars, source=provider)
            except trend_bars.ProviderUnavailable:
                halted[provider].set()
            except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
                pass
            finally:
                time.sleep(.18)
        return check_row(row, day, anchors, {}, unavailable="日线不可用")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, rows))
    return {"date": day, "checked_at": datetime.now(CST).isoformat(timespec="seconds"),
            "schema": 2, "sources": {"tencent": trend_bars.TENCENT, "eastmoney": trend_bars.EASTMONEY,
                                      "sina": [trend_bars.SINA, trend_bars.SINA_FACTORS]},
            "adjustment": "前复权收盘；新浪按当日有效因子还原至核验期末",
            "calendar": "上证综合指数日线", "calendar_start": start.isoformat(),
            "calendar_source": trend_bars.SINA, "calendar_sessions": sessions,
            "tolerance_pp": TOLERANCE, "anchors": anchors, "security_count": len(results),
            "source_counts": dict(Counter(r["source"] or "unavailable" for r in results)),
            "counts": {field: dict(Counter(r["factors"][field]["status"] for r in results))
                       for field in anchors}, "rows": sorted(results, key=lambda r: r["code"])}


def write_audit(directory, result, ordered):
    directory.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode()
    (directory / "trend-audit.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    by_code = {r["code"]: r for r in result["rows"]}
    fields = list(result["anchors"])
    lines = [f'<!DOCTYPE html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="../archive.css"><title>趋势因子复核 · {result["date"]}</title><main>',
             '<p><a href="./">当日归档</a> · <a href="../../">最新榜单</a></p>',
             f'<h1>{result["date"]} 趋势因子复核</h1>',
             f'<p>共 {len(by_code):,} 只。60 日基准为上证指数第 60 个先前交易日，年初至今基准为上年最后一个交易日。'
             f'逐只用前复权收盘重新计算，容差为 {result["tolerance_pp"]} 个百分点，差值为复算涨幅减原行情涨幅。'
             '腾讯日线与腾讯行情同属一家供应商。新浪以当日收盘价除以当日有效因子，再按核验期末因子还原。'
             '供应商的复权和小数位数可能不同。无对应交易日收盘时单列为未能核验。</p>']
    counts_by_source = Counter(r.get("source") or "unavailable" for r in result["rows"])
    lines.append("<p>日线来源：" + "，".join(f'{PROVIDERS.get(p, "未取得")} {count:,} 只'
                                      for p, count in counts_by_source.items()) + "。</p>")
    for field in fields:
        counts = "，".join(f"{escape(status)} {count:,} 只" for status, count in result["counts"][field].items())
        lines.append(f'<p><b>{field}</b>　基准日 {escape(result["anchors"][field] or "未取得")}，{counts}。</p>')
    lines.append('<details><summary>分来源核验结果</summary><ul>')
    for provider in counts_by_source:
        group = [r for r in result["rows"] if (r.get("source") or "unavailable") == provider]
        for field in fields:
            counts = Counter(r["factors"][field]["status"] for r in group)
            desc = "，".join(f"{escape(s)} {n:,} 只" for s, n in counts.items())
            lines.append(f'<li>{PROVIDERS.get(provider, "未取得日线")} · {field}：{desc}</li>')
    lines.append("</ul></details>")
    lines.extend(['<p><a href="./trend-audit.json.gz" download>下载逐股数据、基准收盘和核对时间</a></p>',
                  '<h2>各对照榜入选证券</h2><div class="scroll"><table><thead><tr><th>证券 / 榜单名次</th><th>日线来源</th><th>因子</th><th>行情 %</th><th>日线复算 %</th><th>差值（百分点）</th><th>结果</th></tr></thead><tbody>'])
    memberships = {}
    labels = {"legacy": "原榜", "revised": "修订估值", "industry": "行业内", "sensitivity": "活跃度加权"}
    for key, codes in ordered.items():
        if key == "published" and "legacy" in ordered:
            continue
        for i, code in enumerate(codes, 1):
            memberships.setdefault(code, []).append(f"{labels.get(key, '发布榜')}第 {i} 名")
    for code, ranks in memberships.items():
        if code not in by_code:
            continue
        row = by_code[code]
        for field, factor in row["factors"].items():
            values = [row["name"] + " " + code + " · " + " / ".join(ranks),
                      PROVIDERS.get(row.get("source"), "未取得"), field, factor.get("quoted_pct"),
                      factor.get("daily_pct"), factor.get("difference_pp"), factor["status"]]
            lines.append("<tr>" + "".join("<td>" + escape(str(x) if x is not None else "—") + "</td>"
                                           for x in values) + "</tr>")
    lines.append("</tbody></table></div></main></html>")
    (directory / "trend-audit.html").write_text("\n".join(lines), encoding="utf-8")


def audit_archive(directory, cache_dir=None, skip_sources=()):
    with gzip.open(directory / "a-snapshot.json.gz", "rt", encoding="utf-8") as stream:
        snapshot = json.load(stream)
    trade_day = snapshot["trade_date"]
    if directory.name != trade_day:
        raise ValueError("归档目录和交易日不一致")
    candidates = snapshot["candidates"]
    col = candidates["columns"].index("代码")
    eligible = {r[col] for r in candidates["rows"]}
    table = snapshot["quotes"]
    records = (dict(zip(table["columns"], r)) for r in table["rows"])
    rows = [r for r in records if r["代码"] in eligible]
    if len(rows) != len(eligible):
        raise ValueError("归档缺少候选股原始行情")
    ordered = snapshot["selections"]
    path = directory / "trend-audit.json.gz"
    if path.is_file():
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            prior = json.load(stream)
        if (prior.get("schema") == 2 and prior.get("date") == trade_day
                and {r["code"] for r in prior["rows"]} == eligible and
                all(not OUTAGES.intersection(counts)
                    for counts in prior["counts"].values())):
            write_audit(directory, prior, ordered)
            return prior
    result = audit(rows, trade_day, cache_dir=cache_dir, skip_sources=skip_sources)
    # Preserve previously verified closes when a partial provider outage
    # affects a repeat audit of the same immutable inputs.
    if path.is_file() and prior.get("date") == trade_day:
        saved = {r["code"]: r for r in prior["rows"]}
        if not result["calendar_sessions"] and prior.get("calendar_sessions"):
            for key in ("anchors", "calendar_sessions", "calendar_source", "calendar_start"):
                result[key] = prior[key]
        for i, row in enumerate(result["rows"]):
            old = saved.get(row["code"])
            if (old and prior.get("anchors") == result["anchors"]
                    and all(f["status"] in OUTAGES for f in row["factors"].values())
                    and any(f["status"] not in OUTAGES for f in old["factors"].values())):
                result["rows"][i] = old
                if "source" not in old and prior.get("source") == trend_bars.TENCENT and old.get("daily_close"):
                    old["source"] = "tencent"
        result["counts"] = {k: dict(Counter(r["factors"][k]["status"] for r in result["rows"]))
                            for k in result["anchors"]}
        result["source_counts"] = dict(Counter(r.get("source") or "unavailable" for r in result["rows"]))
    write_audit(directory, result, ordered)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=Path("docs"))
    parser.add_argument("--day")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--skip-source", action="append", choices=list(PROVIDERS), default=[],
                        help="Skip new network requests for a currently unavailable provider")
    args = parser.parse_args()
    stamp = args.day or json.loads((args.docs / "data.json").read_text(encoding="utf-8"))["updated"][:10]
    result = audit_archive(args.docs / "history" / date.fromisoformat(stamp).isoformat(), args.cache, args.skip_source)
    print(result["date"], result["security_count"], result["counts"])
