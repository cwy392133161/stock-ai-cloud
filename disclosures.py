"""Recent official filing titles and document links, anchored to the snapshot date."""
from filing_text import IMPORTANT
from period_reports import filing_search_windows, period_title_score

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from urllib.error import URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

CN = "https://www.cninfo.com.cn"
HK = "https://www1.hkexnews.hk"
STATIC = "https://static.cninfo.com.cn"
CHINA = timezone(timedelta(hours=8))


def read_json(url, *, query=None, form=None, referer):
    if query:
        url += "?" + urlencode(query)
    req = Request(url, data=urlencode(form).encode() if form else None,
                  headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    with urlopen(req, timeout=12) as response:
        return json.load(response)


@lru_cache(maxsize=2)
def directory(market):
    if market == "A股":
        rows = read_json(CN + "/new/data/szse_stock.json", referer=CN)["stockList"]
        return {row["code"]: row["orgId"] for row in rows}
    rows = read_json(HK + "/ncms/script/eds/activestock_sehk_e.json", referer=HK)
    if isinstance(rows, dict):
        rows = rows["stockInfo"]
    return {str(row["c"]).zfill(5): row["i"] for row in rows}


def cn_rows(code, start, end):
    org = directory("A股")[code]
    column = "sse" if code.startswith("6") else "bj" if code.startswith(("4", "8", "92")) else "szse"
    form = dict(pageNum=1, pageSize=30, column=column, tabName="fulltext", plate="",
                stock=f"{code},{org}", searchkey="", secid="", category="", trade="",
                seDate=f"{start}~{end}", sortName="time", sortType="desc", isHLtitle="true")
    data = read_json(CN + "/new/hisAnnouncement/query", form=form, referer=CN + "/new/commonUrl/pageOfSearch")
    rows = data["announcements"]
    if rows is None and data.get("totalAnnouncement") == 0:
        return
    for row in rows:
        if row["secCode"] == code:
            day = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, CHINA).date()
            yield day, row["announcementTitle"], row["adjunctUrl"]


def hk_rows(code, start, end):
    params = dict(stockId=directory("港股")[code], market="SEHK", searchType=0, lang="zh", category=0, documentType=-1,
                  fromDate=start.strftime("%Y%m%d"), toDate=end.strftime("%Y%m%d"),
                  sortDir=0, sortByOptions="DateTime", rowRange=30, t1code=-2, t2Gcode=-2, t2code=-2)
    data = read_json(HK + "/search/titleSearchServlet.do", query=params,
                     referer=HK + "/search/titlesearch.xhtml?lang=zh")
    rows = json.loads(data["result"]) if isinstance(data["result"], str) else data["result"]
    for row in rows:
        if code in re.findall(r"(?<!\d)\d{5}(?!\d)", row["STOCK_CODE"]):
            day = datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M").date()
            yield day, row["TITLE"], row["FILE_LINK"]



def _market_rows(market, code, start, end):
    if market == "A股":
        return cn_rows(re.sub(r"^(sh|sz|bj)", "", code.lower()), start, end)
    return hk_rows(code.zfill(5), start, end)


def _valid_notice(day, raw_title, path, market, start, end):
    host = STATIC if market == "A股" else HK
    title = html.unescape(re.sub("<[^>]+>", "", str(raw_title))).strip()
    if not isinstance(path, str) or not path or not isinstance(day, date):
        return None
    url = urljoin(host + "/", path)
    parts = urlsplit(url)
    if not (title and start <= day <= end and parts.scheme == "https"
            and parts.netloc == urlsplit(host).netloc and parts.path.lower().endswith(".pdf")):
        return None
    return {"日期": day.isoformat(), "标题": title, "链接": url}


def fetch_disclosures(market, code, as_of, report_period=None):
    """Recent notices plus at most one independently matched fiscal-period report.

    A matching title is never presented as verified PDF financial information.
    Reports outside the recent 30-day window can be recovered without returning
    extra low-value announcements to the model.
    """
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=29)
    result = {"来源": "巨潮资讯" if market == "A股" else "港交所披露易",
              "查询期间": f"{start} 至 {end}", "状态": "未核实", "公告": []}
    recent = {}
    skipped = False
    try:
        for day, title, path in _market_rows(market, code, start, end):
            item = _valid_notice(day, title, path, market, start, end)
            if item is None:
                skipped = True
                continue
            recent[item["链接"]] = item
        result["状态"] = ("已取得公告索引" if recent else
                         "索引条目未核实" if skipped else "查询期内未检索到匹配公告")
    except (URLError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result["状态"] = f"公告查询未核实（{type(exc).__name__}）"

    ranked = sorted(recent.values(),
                    key=lambda n: (bool(IMPORTANT.search(n["标题"])), n["日期"]),
                    reverse=True)
    if report_period:
        report = {"报告期": report_period, "状态": "未找到标题可确认的同报告期财报"}
        best = None
        best_score = 0
        try:
            windows = filing_search_windows(report_period, as_of)
            if not windows:
                report["状态"] = "报告期超出有限查询窗口，未核实"
            else:
                # Check already fetched notices before querying historical windows.
                for item in recent.values():
                    score = period_title_score(item["标题"], report_period, market)
                    if score > best_score:
                        best, best_score = item, score
                if best_score < 60:
                    for window_start, window_end in windows:
                        for day, title, path in _market_rows(market, code, window_start, window_end):
                            item = _valid_notice(day, title, path, market, window_start, window_end)
                            if not item:
                                continue
                            score = period_title_score(item["标题"], report_period, market)
                            if score > best_score:
                                best, best_score = item, score
                        if best_score >= 60:
                            break
                if best:
                    report.update({"状态": "标题与报告期匹配，PDF正文待核验",
                                   "公告日期": best["日期"], "标题": best["标题"],
                                   "链接": best["链接"]})
                    matched = {**best, "报告期匹配": report_period}
                    # Reserve D1 for the fiscal report, leaving at most two
                    # recent announcements. Total PDF budget is unchanged.
                    ranked = [matched] + [item for item in ranked if item["链接"] != best["链接"]][:2]
        except (URLError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            report["状态"] = f"对应报告期财报检索未核实（{type(exc).__name__}）"
            # Preserve a previously matched report if later search fails.
            if best:
                report.update({"公告日期": best["日期"], "标题": best["标题"], "链接": best["链接"]})
                ranked = [{**best, "报告期匹配": report_period}] + [
                    item for item in ranked if item["链接"] != best["链接"]][:2]
        result["对应报告期财报"] = report
    result["公告"] = ranked[:3]
    return result
