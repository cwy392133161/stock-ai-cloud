"""Freeze A-share inputs and publish cohort performance from dated closes."""
import argparse
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import gzip
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import tempfile

import pandas as pd

from forward_performance import (BUY_COST, SELL_COST, HORIZONS, METHODS,
                                 evaluate_cohort, read_bars, target_turnover)


CST = timezone(timedelta(hours=8))
FILENAME = "a-snapshot.json.gz"
SCHEMA = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                      sort_keys=True).encode("utf-8")


def _table(frame):
    if "代码" not in frame or not frame.columns.is_unique:
        raise ValueError("快照需要股票代码和唯一列名")
    rows = frame.astype(object).where(pd.notna(frame), None)
    codes = rows["代码"].tolist()
    if len(set(codes)) != len(codes) or not all(isinstance(c, str) and re.fullmatch(r"[0-9]{6}", c) for c in codes):
        raise ValueError("股票代码必须唯一且保留六位数字")
    return {"columns": list(rows.columns), "rows": rows.values.tolist()}, set(codes)


def _timestamp(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("采集时间需要包含时区")
    return value.astimezone(CST).isoformat(timespec="seconds")


def _read(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _check_close(frame, eligible, day, collected_at):
    cutoff = datetime.fromisoformat(day + "T15:00:00+08:00")
    if collected_at.utcoffset() is None or collected_at < cutoff:
        raise ValueError("收盘前采集的行情不能作为日终快照")
    rows = frame.loc[frame["代码"].isin(eligible)]
    if "报价时间" in rows:
        stamps = pd.to_datetime(rows["报价时间"].astype(str), format="%Y%m%d%H%M%S",
                                errors="coerce").dt.tz_localize(CST)
    elif "f124" in rows:
        stamps = pd.to_datetime(pd.to_numeric(rows["f124"], errors="coerce"), unit="s",
                                errors="coerce", utc=True).dt.tz_convert(CST)
    else:
        raise ValueError("缺少可验证的收盘报价时间")
    if not ((stamps.dt.date == cutoff.date()) & (stamps >= cutoff)
            & (stamps <= collected_at + timedelta(minutes=1))).all():
        raise ValueError("候选池含非当日收盘行情")


def archive_close(docs, frame, ranked, snapshot, *, collected_at, score_version):
    """Publish atomically. A repeat may reuse, but cannot replace, a frozen input."""
    day = date.fromisoformat(snapshot["updated"][:10]).isoformat()
    quality = snapshot.get("quality", {}).get("A股", frame.attrs.get("quality", {}))
    for field in ("交易日期", "报价日期"):
        if quality.get(field) and str(quality[field])[:10] != day:
            raise ValueError(f"{field}与榜单日期不符")
    quotes, raw_codes = _table(frame.sort_values("代码"))
    candidates, eligible = _table(ranked)
    if not eligible or not eligible.issubset(raw_codes):
        raise ValueError("候选池与原始行情不符")
    _check_close(frame, eligible, day, collected_at)
    selections = dict(snapshot.get("comparison_selections", {}))
    selections["published"] = [s["代码"] for s in snapshot["a_shares"]]
    for name, codes in selections.items():
        if (not isinstance(codes, list) or (not codes and name != "industry") or len(codes) > 20 or
                len(set(codes)) != len(codes) or not set(codes).issubset(eligible)):
            raise ValueError(f"组合 {name} 与当日候选池不符")
    inputs = {"schema": SCHEMA, "trade_date": day, "score_version": score_version,
              "quotes": quotes, "candidates": candidates, "selections": selections,
              "ranking_analysis": snapshot.get("ranking_analysis")}
    digest = hashlib.sha256(_json(inputs)).hexdigest()
    record = dict(inputs, input_sha256=digest, collected_at=_timestamp(collected_at),
                  archived_at=_timestamp(datetime.now(CST)), quality=quality)
    path = Path(docs) / "history" / day / FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tmp_path.write_bytes(gzip.compress(_json(record), compresslevel=6, mtime=0))
        try:
            os.link(tmp_path, path)
        except FileExistsError:
            if _read(path).get("input_sha256") != digest:
                raise ValueError(f"{day} 已冻结，新的行情、评分或名单有差异")
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def _paths(docs):
    paths = {}
    for path in (Path(docs) / "history").glob(f"*/{FILENAME}"):
        day = path.parent.name
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError(f"无效的归档目录 {day}")
        paths[day] = path
    return paths


def _page(title, nav, body, style="archive.css"):
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title><link rel="stylesheet" href="{style}"></head>'
            f'<body><nav>{nav}</nav><main><h1>{title}</h1>{body}</main></body></html>\n')


def _table_html(headers, rows):
    heads = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(v))}</td>" for v in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{heads}</tr></thead><tbody>{body}</tbody></table></div>'


def _write(path, value):
    if isinstance(value, str):
        value = value.encode("utf-8")
    if path.exists() and path.read_bytes() == value:
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(value)
        except BaseException:
            stream.close()
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _signal(record):
    table = record["candidates"]
    index = {field: i for i, field in enumerate(table["columns"])}
    names = {row[index["代码"]]: str(row[index["名称"]]) if "名称" in index else "" for row in table["rows"]}
    groups = dict(record["selections"], benchmark=list(names))
    return names, groups


def _write_ranking(path, record, names):
    day = record["trade_date"]
    selection = record["selections"]
    title = f"{day} A 股收盘选股快照"
    nav = '<a href="../">历史收盘行情</a> / <a href="../performance.html">前瞻跟踪</a>'
    body = (f'<p>全市场 {len(record["quotes"]["rows"]):,} 只，符合当日筛选条件 {len(names):,} 只。'
            f'评分版本 {escape(str(record["score_version"]))}。'
            f'行情采集于 {escape(record["collected_at"])}，组合冻结于 {escape(record["archived_at"])}。</p>'
            f'<p><a download href="./{FILENAME}">下载全量行情、候选评分和各方案名单（gzip JSON）</a></p>')
    if (path.parent / "summary.json").exists() and (path.parent / "index.html").exists():
        body += '<p><a href="./">当日原始发布记录</a></p>'
    if (path.parent / "a-execution-quotes.json.gz").exists():
        body += ('<p><a download href="./a-execution-quotes.json.gz">下载逐股开、高、低、昨收和涨跌停价'
                 '及原始响应校验值（gzip JSON）</a></p>')
    analysis = record.get("ranking_analysis")
    if analysis:
        from ranking_analysis import comparison_html
        detail = comparison_html({"ranking_analysis": analysis, "comparison_selections": selection},
                                 history_href="..")
        body += detail.replace('class="comparison-scroll"', 'class="scroll comparison-scroll"')
    for key in [k for k in METHODS if k in selection and not analysis]:
        rows = [(i, code, names[code]) for i, code in enumerate(selection[key], start=1)]
        if not rows:
            body += f'<section><h2>{METHODS[key]}</h2><p>当日行业样本不足，未形成组合。</p></section>'
            continue
        body += f'<section><h2>{METHODS[key]}</h2>' + _table_html(["序号", "代码", "名称"], rows) + '</section>'
    _write(path.parent / "ranking.html", _page(title, nav, body, "../archive.css"))


def _calendar(sessions):
    values = [date.fromisoformat(s).isoformat() for s in sessions]
    if values != sorted(set(values)):
        raise ValueError("交易日历包含重复或乱序日期")
    return values


def _audited_sessions(path, start, end, previous):
    """Use a continuous index-calendar window already captured by the trend audit."""
    audit_path = path.parent / "trend-audit.json.gz"
    if not audit_path.exists() or path.parent.name < end.isoformat():
        return None
    audit = _read(audit_path)
    if audit.get("date") != path.parent.name or not audit.get("calendar_start"):
        return None
    published = _calendar(audit.get("calendar_sessions", []))
    bound = date.fromisoformat(audit["calendar_start"]).isoformat()
    if not published or published[0] < bound or published[-1] != audit["date"]:
        return None
    if bound > start.isoformat():
        if (not previous or previous[0] > start.isoformat() or previous[-1] < bound or
                [d for d in previous if bound <= d <= published[-1]] !=
                [d for d in published if bound <= d <= previous[-1]]):
            return None
        published = [d for d in previous if d < bound] + published
    return [d for d in published if start.isoformat() <= d <= end.isoformat()]


def refresh_tracking(docs, sessions, *, calendar_issue=""):
    """Recompute from actual exchange sessions. Missing archives never shift a horizon."""
    docs = Path(docs)
    paths = _paths(docs)
    if not paths:
        raise ValueError("没有已冻结的 A 股快照")
    sessions = _calendar(sessions)
    session_index = {day: i for i, day in enumerate(sessions)}

    @lru_cache(maxsize=max(HORIZONS))
    def bars(day):
        return read_bars(_read(paths[day])["quotes"], day)

    prior_groups = {}
    prior_day = None
    cohorts = []
    for day, path in sorted(paths.items()):
        record = _read(path)
        if day != record["trade_date"] or record["schema"] != SCHEMA:
            raise ValueError(f"{day} 归档版本或日期无效")
        names, groups = _signal(record)
        _write_ranking(path, record, names)
        i = session_index.get(day)
        is_consecutive = i is not None and i > 0 and sessions[i - 1] == prior_day
        turnover = {key: target_turnover(codes, prior_groups.get(key) if is_consecutive else None)
                    for key, codes in groups.items()}
        frozen = max(datetime.fromisoformat(record[k]) for k in ("archived_at", "collected_at"))
        future = sessions[i + 1:i + 1 + max(HORIZONS)] if i is not None else []
        available = None
        if future:
            opening = datetime.fromisoformat(future[0] + "T09:30:00+08:00")
            available = frozen < opening
        cohort = {"signal_date": day, "score_version": record["score_version"],
                  "full_count": len(record["quotes"]["rows"]), "eligible_count": len(names),
                  "signal_frozen_at": frozen.isoformat(), "available_before_entry": available,
                  "target_turnover_pct": turnover, "horizons": {}}
        for h in HORIZONS:
            dates = future[:h]
            missing = [d for d in dates if d not in paths]
            if i is None:
                status = "calendar_unavailable"
            elif len(dates) < h:
                status = "immature"
            elif missing:
                status = "archive_gap"
            else:
                status = "mature"
            metric = {"status": status, "sessions_observed": len(dates), "missing_dates": missing,
                      "first_session": dates[0] if dates else None, "last_session": dates[-1] if dates else None}
            if status == "mature":
                metric["portfolios"] = evaluate_cohort(groups, [bars(d) for d in dates])
            cohort["horizons"][str(h)] = metric
        cohorts.append(cohort)
        prior_groups, prior_day = groups, day
    report = {"schema": SCHEMA, "calendar_sessions": sessions, "calendar_issue": calendar_issue,
              "buy_cost_bps": BUY_COST * 10000, "sell_cost_bps": SELL_COST * 10000,
              "cohorts": cohorts}
    output = docs / "history"
    _write(output / "performance.json", _json(report))
    _write(output / "performance.html", _performance_page(report))
    _update_catalog(output, cohorts)
    return report


def _update_catalog(history, cohorts):
    path = history / "index.html"
    nav = '<a href="../">返回 A 股选股榜</a>'
    html = path.read_text(encoding="utf-8") if path.exists() else _page("A 股历史收盘行情", nav, "")
    items = "".join(
        f'<li><a href="./{c["signal_date"]}/ranking.html">{c["signal_date"]} 收盘快照</a>'
        f' · 全市场 {c["full_count"]:,} 只 · 候选 {c["eligible_count"]:,} 只</li>'
        for c in reversed(cohorts))
    block = ('<section id="frozen-closes"><h2>每日收盘快照</h2>'
             '<p><a href="./performance.html">查看 5 日、20 日组合跟踪与候选池基准</a></p>'
             f'<ul>{items}</ul></section>')
    if 'id="frozen-closes"' in html:
        html, n = re.subn(r'<section\b[^>]*\bid="frozen-closes"[^>]*>.*?</section>', lambda _: block,
                          html, flags=re.S)
        if n != 1:
            raise ValueError("历史快照目录包含重复或无法解析的栏目")
    elif "</main>" in html:
        html = html.replace("</main>", block + "\n</main>", 1)
    else:
        raise ValueError("无法找到历史快照目录正文")
    _write(path, html)


def _percent(value, *, signed=True):
    if value is None:
        return "—"
    return f"{value:+.2f}%" if signed else f"{value:.2f}%"


def _performance_page(report):
    nav = '<a href="../">选股榜</a> / <a href="./">历史收盘行情</a>'
    body = '<p><a href="./performance.json">下载逐期数据和行情缺口明细（JSON）</a></p>'
    if report["calendar_issue"]:
        body += f'<p>{escape(report["calendar_issue"])}</p>'
    if report["calendar_sessions"]:
        body += f'<p>交易日历已核对至 {report["calendar_sessions"][-1]}。</p>'
    for c in reversed(report["cohorts"]):
        day = c["signal_date"]
        status = {None: "首次可交易时点尚待日历确认", True: "名单在下一交易日开盘前已冻结",
                  False: "事后回放，冻结时间晚于或等于下一交易日开盘"}[c["available_before_entry"]]
        body += (f'<section><h2><a href="./{day}/ranking.html">{day} 组合</a></h2>'
                 f'<p>全市场 {c["full_count"]:,} 只，候选池 {c["eligible_count"]:,} 只。'
                 f'{status}。评分版本 {escape(str(c["score_version"]))}。</p>')
        for h, entry in c["horizons"].items():
            body += f'<h3>{h} 个交易日</h3>'
            if entry["status"] == "calendar_unavailable":
                body += '<p>该日交易日历未核实。</p>'
            elif entry["status"] == "immature":
                body += f'<p>观察期未满，已确认后续 {entry["sessions_observed"]} 个交易日。</p>'
            if entry["missing_dates"]:
                body += '<p>缺少全市场收盘快照：' + escape("、".join(entry["missing_dates"])) + '。</p>'
            if entry["status"] != "mature":
                continue
            body += f'<p>{entry["first_session"]} 开始尝试买入，{entry["last_session"]} 收盘估值。</p>'
            rows = []
            for key, metrics in sorted(entry["portfolios"].items(), key=lambda item: list(METHODS).index(
                    item[0]) if item[0] in METHODS else len(METHODS)):
                ex = metrics["excess_pct_points"]
                known = metrics["known_contribution_pct_points"]
                coverage = f'{metrics["known_count"]}/{metrics["target_count"]}'
                rows.append([METHODS.get(key, key), coverage,
                             "—" if known is None else f"{known:+.2f}",
                             _percent(metrics["gross_return_pct"]),
                             _percent(metrics["marked_return_pct"]), "—" if ex is None else f"{ex:+.2f}",
                             _percent(metrics["estimated_liquidation_return_pct"]),
                             _percent(metrics["max_drawdown_pct"], signed=False),
                             _percent(c["target_turnover_pct"][key], signed=False),
                             metrics["delayed_entries"], metrics["exit_states"]["cash"]])
            body += _table_html(["组合", "可全程估值/目标数", "已知贡献（百分点）", "毛收益", "扣买入成本市值收益",
                                 "超额（百分点）", "清仓净收益", "最大回撤", "目标换手", "延迟入场数",
                                 "期末仍为现金数"], rows)
        body += '</section>'
    body += ('<section><h2>计算口径</h2><p>每期将资金等分给当日名单，基准等分给当天所有候选。'
             '自下一交易日起在首个可买入开盘价建仓。停牌或开盘价达到涨停价的份额留存现金并于下日重试。'
             '卖出假设使用第 5 或第 20 个交易日的收盘价。同日买入、停牌、收盘跌停或无法确认可卖时，'
             '不列示整组清仓净收益。未取得涨跌停价时，仅在实际最高价高于开盘价、'
             '最低价低于收盘价分别足以验证可买、可卖时采信成交。</p>'
             '<p>建仓日按收盘价与开盘价之比计价，持仓期逐日使用行情源的收盘价与参考昨收之比。'
             '停牌按上次估值，未单独计现金分红、分红税和现金利息。代码消失、行情缺档或持仓期间'
             '缺少参考昨收时保留原权重，该组合相关收益留空。'
             '覆盖列展示全程可估值证券数量，已知贡献按这些证券在原始组合中的权重计算，'
             '其余证券的收益未知。缺失名单见逐期数据。</p>'
             '<p>假设单边买入总成本 10 个基点、卖出 15 个基点，含简化的交易费用与滑点，允许碎股。'
             '市值收益扣除买入成本，清仓净收益再扣卖出成本。超额为组合与当期等权候选池市值收益之差。'
             '最大回撤取初始本金及每日扣买入成本净值的峰谷跌幅。目标换手按连续两交易日等权目标权重'
             '变动绝对值之和的一半计算。各期分别模拟，重叠观察期不能串接为单一账户业绩。</p></section>')
    return _page("A 股组合前瞻跟踪", nav, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", type=Path, default=Path(__file__).resolve().parent / "docs")
    args = parser.parse_args()
    paths = _paths(args.docs)
    if not paths:
        parser.error("没有冻结的 A 股快照")
    now = datetime.now(CST)
    end = now.date() if now.hour >= 15 else now.date() - timedelta(days=1)
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start = date.fromisoformat(min(paths))
    output = args.docs / "history" / "performance.json"
    saved = _calendar(json.loads(output.read_text(encoding="utf-8"))["calendar_sessions"]) if output.exists() else []
    from trend_audit import trading_sessions
    try:
        sessions = _audited_sessions(paths[max(paths)], start, end, saved)
        if sessions is None:
            sessions = _calendar(trading_sessions(start, end))
        if any(start <= date.fromisoformat(d) <= end and d not in sessions for d in paths):
            raise ValueError("返回的日历缺少已有行情的交易日")
        issue = ""
    except Exception as exc:
        sessions = [d for d in saved if d <= end.isoformat()]
        issue = "本次交易日历查询未成功，仅使用已保存的交易日历。"
        print(f"{issue} {type(exc).__name__}", flush=True)
    report = refresh_tracking(args.docs, sessions, calendar_issue=issue)
    print(f"已更新 {len(report['cohorts'])} 期收盘快照目录及组合跟踪。", flush=True)


if __name__ == "__main__":
    main()
