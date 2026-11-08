"""Recent official filing titles and document links, anchored to the snapshot date."""
from filing_text import IMPORTANT
from period_reports import filing_search_windows, period_title_score, filing_kind

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


CN_PAGE_SIZE = 30
HK_ROW_LIMIT = 100


def cn_rows(code, start, end):
    """Page until the API reports completion; refuse repeated non-progress pages."""
    org = directory("A股")[code]
    column = "sse" if code.startswith("6") else "bj" if code.startswith(("4", "8", "92")) else "szse"
    form = dict(pageNum=1, pageSize=CN_PAGE_SIZE, column=column, tabName="fulltext", plate="",
                stock=f"{code},{org}", searchkey="", secid="", category="", trade="",
                seDate=f"{start}~{end}", sortName="time", sortType="desc", isHLtitle="true")
    seen_urls = set()
    page = 1
    while True:
        form["pageNum"] = page
        data = read_json(CN + "/new/hisAnnouncement/query", form=form,
                         referer=CN + "/new/commonUrl/pageOfSearch")
        rows = data.get("announcements")
        if rows is None:
            if data.get("totalAnnouncement") == 0:
                return
            raise ValueError("巨潮资讯索引响应缺少公告列表，不能推断无公告")
        if not isinstance(rows, list):
            raise ValueError("巨潮资讯索引格式异常")
        new_entries = 0
        for row in rows:
            if row.get("secCode") != code:
                continue
            path = row.get("adjunctUrl")
            if path in seen_urls:
                continue
            seen_urls.add(path)
            new_entries += 1
            day = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, CHINA).date()
            yield day, row["announcementTitle"], path
        total = data.get("totalAnnouncement")
        try:
            total = int(total) if total is not None else None
        except (ValueError, TypeError):
            total = None
        if (total is not None and page * CN_PAGE_SIZE >= total) or len(rows) < CN_PAGE_SIZE:
            return
        if not new_entries:
            # Some providers ignore pageNum and repeat page 1. Stop rather
            # than issuing an unbounded loop and treating it as complete.
            raise ValueError("巨潮资讯分页未推进，索引仍未查全")
        page += 1


def hk_rows(code, start, end):
    """Complete dated HKEX windows by partitioning until the date range ends.

    HKEX's legacy titleSearchServlet exposes a rowRange cap rather than
    durable page cursors. When a window reaches the limit, bisect the date
    interval; only mark it complete once every leaf is below the cap.
    A single day with too many entries is explicitly unverified instead of
    silently truncating the company research. No fixed number-of-calls quota.
    """
    seen = set()
    stock_id = directory("港股")[code]

    def load(window_start, window_end):
        params = dict(stockId=stock_id, market="SEHK", searchType=0,
                      lang="zh", category=0, documentType=-1,
                      fromDate=window_start.strftime("%Y%m%d"),
                      toDate=window_end.strftime("%Y%m%d"),
                      sortDir=0, sortByOptions="DateTime",
                      rowRange=HK_ROW_LIMIT, t1code=-2, t2Gcode=-2,
                      t2code=-2)
        data = read_json(HK + "/search/titleSearchServlet.do", query=params,
                         referer=HK + "/search/titlesearch.xhtml?lang=zh")
        rows = json.loads(data["result"]) if isinstance(data["result"], str) else data["result"]
        if not isinstance(rows, list):
            raise ValueError("港交所索引格式异常")
        if len(rows) >= HK_ROW_LIMIT:
            if window_start >= window_end:
                raise ValueError("港交所单日公告数量达到结果上限，不能当作全量索引")
            mid = window_start + (window_end - window_start) // 2
            yield from load(window_start, mid)
            yield from load(mid + timedelta(days=1), window_end)
            return
        for row in rows:
            if code in re.findall(r"(?<!\d)\d{5}(?!\d)", str(row["STOCK_CODE"])):
                path = row["FILE_LINK"]
                if path in seen:
                    continue
                seen.add(path)
                day = datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M").date()
                if window_start <= day <= window_end:
                    yield day, row["TITLE"], path

    yield from load(start, end)




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

    result["近期公告数量"] = len(recent)
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
                if best_score < 100:
                    for window_start, window_end in windows:
                        for day, title, path in _market_rows(market, code, window_start, window_end):
                            item = _valid_notice(day, title, path, market, window_start, window_end)
                            if not item:
                                continue
                            score = period_title_score(item["标题"], report_period, market)
                            if score > best_score:
                                best, best_score = item, score
                        if best_score >= 100:
                            break
                if best and best_score >= 100:
                    report.update({"状态": "完整财报标题与报告期匹配，PDF正文待核验",
                                   "公告日期": best["日期"], "标题": best["标题"],
                                   "链接": best["链接"], "文档类型": filing_kind(best["标题"])})
                    matched = {**best, "报告期匹配": report_period, "文档类型": "full_report"}
                    # Reserve D1 for the fiscal report, leaving at most two
                    # recent announcements. Total PDF budget is unchanged.
                    ranked = [matched] + [item for item in ranked if item["链接"] != best["链接"]][:2]
                elif best:
                    report.update({"状态": "仅找到对应期间业绩公告，尚未取得完整财报",
                                   "公告日期": best["日期"], "标题": best["标题"],
                                   "链接": best["链接"], "文档类型": filing_kind(best["标题"])})
        except (URLError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            report["状态"] = f"对应报告期财报检索未核实（{type(exc).__name__}）"
            # Preserve a previously matched report if later search fails.
            if best and best_score >= 100:
                report.update({"公告日期": best["日期"], "标题": best["标题"], "链接": best["链接"], "文档类型": filing_kind(best["标题"])})
                ranked = [{**best, "报告期匹配": report_period, "文档类型": "full_report"}] + [
                    item for item in ranked if item["链接"] != best["链接"]][:2]
        result["对应报告期财报"] = report
    result["公告"] = ranked[:3]
    result["历史财报数量"] = sum(n["日期"] < start.isoformat() for n in result["公告"])
    return result

