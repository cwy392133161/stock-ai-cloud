"""Recent official filing titles and document links, anchored to the snapshot date."""
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


def fetch_disclosures(market, code, as_of):
    end = date.fromisoformat(as_of)
    start = end - timedelta(days=29)
    mainland = market == "A股"
    host = STATIC if mainland else HK
    result = {"来源": "巨潮资讯" if mainland else "港交所披露易",
              "查询期间": f"{start} 至 {end}", "状态": "未核实", "公告": []}
    try:
        rows = (cn_rows(re.sub(r"^(sh|sz|bj)", "", code.lower()), start, end) if mainland
                else hk_rows(code.zfill(5), start, end))
        notices = {}
        skipped = False
        for day, raw_title, path in rows:
            title = html.unescape(re.sub("<[^>]+>", "", raw_title)).strip()
            url = urljoin(host + "/", path)
            parts = urlsplit(url)
            if not (title and path and start <= day <= end and parts.scheme == "https"
                    and parts.netloc == urlsplit(host).netloc and parts.path.lower().endswith(".pdf")):
                skipped = True
                continue
            notices[url] = {"日期": day.isoformat(), "标题": title, "链接": url}
        result["公告"] = sorted(notices.values(), key=lambda item: item["日期"], reverse=True)[:3]
        result["状态"] = "已取得公告索引" if notices else ("索引条目未核实" if skipped else "查询期内未检索到匹配公告")
    except (URLError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        result["状态"] = f"公告查询未核实（{type(exc).__name__}）"
    return result
