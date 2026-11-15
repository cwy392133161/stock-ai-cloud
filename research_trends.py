"""Attach the same day's independent price-factor audit to the matching issuer."""
from datetime import date
import gzip
from html import escape
import json
from pathlib import Path
import re

from publish_guard import REPORT_SNAPSHOT
from trend_audit import PROVIDERS, _number


FIELDS = ("60日涨跌幅", "年初至今涨跌幅")
START = "<!--issuer-trend-audit-->"
END = "<!--/issuer-trend-audit-->"


def load_checks(docs, snapshot):
    day = date.fromisoformat(snapshot["updated"][:10]).isoformat()
    archive = docs / "history" / day
    path = archive / "trend-audit.json.gz"
    if not path.is_file():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        audit = json.load(stream)
    if audit["date"] != day:
        raise ValueError("趋势复核日期与行情不一致")
    rows = {row["code"]: row for row in audit["rows"]}
    result = {}
    quote_source = (snapshot.get("quality", {}).get("A股") or {}).get("行情来源", "当期收盘快照")
    for stock in snapshot.get("a_shares", [])[:5]:
        code = stock["代码"]
        row = rows.get(code, {})
        source = PROVIDERS.get(row.get("source"), "未记录日线来源")
        if not row.get("source") and str(audit.get("source") or "").startswith("https://web.ifzq.gtimg.cn/"):
            source = PROVIDERS["tencent"]
        check = {"日期": day, "行情来源": quote_source, "日线来源": source,
                 "容差（百分点）": audit["tolerance_pp"], "因子": {}}
        for field in FIELDS:
            factor = row.get("factors", {}).get(field, {})
            value = _number(stock.get(field))
            status = factor.get("status", "本期没有逐股复核记录")
            quoted = _number(factor.get("quoted_pct"))
            if factor and value is not None and (quoted is None or abs(value - quoted) > 0.00501):
                status = "原行情值与复核输入不符"
                factor = {}
            elif value is None:
                value = quoted
            check["因子"][field] = {"行情%": value, "日线复算%": factor.get("daily_pct"),
                                    "差值（百分点）": factor.get("difference_pp"),
                                    "基准日": factor.get("anchor"), "结果": status}
        result[code] = check
    return result


def render_check(check):
    if not check:
        return ""
    day = check["日期"]
    parts = ['<section class="issuer-trend-audit"><h3>行情复核，独立于模型正文</h3>',
             '<p>行情来自' + escape(check["行情来源"]) + '，复算采用' + escape(check["日线来源"])
             + '。差值为复算减行情，容差为 ' + escape(str(check["容差（百分点）"])) + ' 个百分点。</p><ul>']
    for name, row in check["因子"].items():
        values = []
        for key, label, unit in (("行情%", "行情", "%"), ("日线复算%", "日线复算", "%"),
                                 ("差值（百分点）", "差值", " 个百分点")):
            value = row[key]
            values.append(label + ("未取得" if value is None else f" {value}{unit}"))
        if row["基准日"]:
            values.append("基准日 " + row["基准日"])
        parts.append('<li><b>' + escape(name) + '</b>　' + escape("，".join(values)) +
                     '。' + escape(row["结果"]) + '。</li>')
    parts.append('</ul><a href="./history/' + day + '/trend-audit.html">查看当期逐股行情复核</a></section>')
    return START + "".join(parts) + END


def annotate_current(docs):
    snapshot = json.loads((docs / "data.json").read_text(encoding="utf-8"))
    checks = load_checks(docs, snapshot)
    if not checks:
        return
    report = docs / "ai.html"
    source = report.read_bytes().decode("utf-8")
    stamp = REPORT_SNAPSHOT.search(source)
    if not stamp or stamp[1] != snapshot["updated"]:
        raise ValueError("现行报告与行情时间不同，不能补注")
    body = re.sub(re.escape(START) + ".*?" + re.escape(END), "", source, flags=re.S)
    codes = re.findall(r'<article><b id="stock-(\d+)"', body)
    if codes != list(checks):
        raise ValueError("现行报告与核验股票不一致")
    for code in codes:
        pattern = rf'(<article><b id="stock-{code}">[^<]*</b>)'
        body = re.sub(pattern, lambda m: m[0] + render_check(checks[code]), body, count=1)
    if body != source:
        report.write_bytes(body.encode("utf-8"))

    directory = docs / "history" / snapshot["updated"][:10]
    path = directory / "index.html"
    page = path.read_bytes().decode("utf-8")
    label = "行情因子复核（独立于该版 AI 正文）"
    if (directory / "ai-bank.html").is_file() and label not in page:
        page, n = re.subn(r'(<a\b[^>]*href="\./ai-bank\.html"[^>]*>[^<]+</a>)',
                          r'\1 · <a href="./trend-audit.html">' + label + '</a>', page, count=1)
        if n:
            path.write_bytes(page.encode("utf-8"))


if __name__ == "__main__":
    annotate_current(Path(__file__).resolve().parent / "docs")
