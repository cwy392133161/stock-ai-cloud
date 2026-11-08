"""Match official filing titles to a dated financial statement period.

This module *never* infers an amount or a report date from an unrelated
announcement. A match is title-level evidence, not verification of PDF text.
"""
import re
import unicodedata
from datetime import date, timedelta

YEAR_DIGITS = {"零": "0", "〇": "0", "○": "0", "一": "1", "二": "2",
               "三": "3", "四": "4", "五": "5", "六": "6", "七": "7",
               "八": "8", "九": "9"}
MONTHS = {"十二": 12, "十一": 11, "十": 10, "一": 1, "二": 2, "三": 3,
          "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
DAYS = {**{name: value for name, value in MONTHS.items()},
        "三十一": 31, "三十": 30, "二十九": 29, "二十八": 28,
        "十五": 15, "十六": 16, "十七": 17, "十八": 18, "十九": 19,
        "二十": 20, "二十一": 21, "二十二": 22, "二十三": 23,
        "二十四": 24, "二十五": 25, "二十六": 26, "二十七": 27}
REPORT_WORDS = re.compile(
    r"季报|季度报告|季度報告|半年|中期|年报|年報|年度报告|年度報告|"
    r"业绩|業績|财务报告|財務報告|results|report|financial statements", re.I)
SUMMARY_ONLY = re.compile(r"摘要|summary|摘要版|更正公告|更正說明|提示性公告|业绩预告|業績預告|盈利預告|盈利预告|盈利預警|盈利警告|profit warning|forecast", re.I)
EN_MONTHS = {name: i for i, name in enumerate(
    ("january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"), 1)}


def normalize_title(title):
    text = unicodedata.normalize("NFKC", str(title)).lower()
    def cn_year(match):
        return "".join(YEAR_DIGITS[c] for c in match.group(1)) + "年"
    text = re.sub(r"([零〇○一二三四五六七八九]{4})年", cn_year, text)
    # Convert traditional Chinese written month/day dates, longest first.
    for mapping, suffix in ((DAYS, "日"), (MONTHS, "月")):
        for word, number in sorted(mapping.items(), key=lambda pair: -len(pair[0])):
            text = text.replace(word + suffix, str(number) + suffix)
    return text


def dates_in_title(text):
    found = set()
    patterns = [
        r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)",
    ]
    for regex in patterns:
        for year, month, day in re.findall(regex, text):
            try:
                found.add(date(int(year), int(month), int(day)))
            except ValueError:
                continue
    for day, month, year in re.findall(
        r"(?<!\d)(\d{1,2})\s+(january|february|march|april|may|june|"
        r"july|august|september|october|november|december)\s+(\d{4})(?!\d)", text):
        try:
            found.add(date(int(year), EN_MONTHS[month], int(day)))
        except ValueError:
            pass
    return found


def filing_kind(title):
    """Classify the document itself, independently of its dates or score.

    An interim dividend and a notice of board approval are not financial
    reports. Results may mention a dividend, but remain results, not reports.
    """
    text = normalize_title(title)
    if SUMMARY_ONLY.search(text) or re.search(
        r"董事[会會]|board meeting|董事局|寄[发發]|派[发發]|刊[发發]|刊登|"
        r"发布提示|發佈提示|通知|通告|notice|notification|dispatch|despatch|"
        r"availability|月[报報]表|monthly return|补充公告|補充公告|澄清|"
        r"[问問][询詢]函|回复|回覆|clarification|supplemental announcement|"
        r"supplementary announcement|response to", text, re.I
    ):
        return "other"
    if re.search(
        r"(?:年度|中期|季度|半年度|半年|第[一三13]季度)(?:财务|財務)?[报報]告|"
        r"(?<!月)[年季][报報]|半年[报報]|"
        r"\b(?:annual|interim|quarterly|half.year)\s+(?:financial\s+)?report\b|"
        r"(?:财务|財務)[报報]告", text, re.I
    ):
        return "full_report"
    if re.search(r"[业業][绩績]|\bresults\b", text, re.I):
        return "results"
    if re.search(r"financial statements", text, re.I):
        return "full_report"
    return "other"


def period_title_score(title, period, market):
    """Return a score only when period and title clearly agree.

    A return value of 0 means do not label the PDF a matching-period report.
    Never match a report solely because it was published in the same year.
    """
    target = date.fromisoformat(str(period))
    text = normalize_title(title)
    kind = filing_kind(title)
    if kind == "other":
        return 0
    explicit = dates_in_title(text)
    if explicit and target not in explicit:
        return 0
    if str(target.year) not in text and target not in explicit:
        return 0

    q1 = bool(re.search(r"第?[一1]季度|一季报|一季報|first quarter|q1\b", text))
    q3 = bool(re.search(r"第?[三3]季度|三季报|三季報|third quarter|q3\b", text))
    interim = bool(re.search(r"半年度|半年报|半年度報告|中期|interim|"
                             r"six months|6 months|half.year", text))
    # '半年度报告' and '半年报' contain the annual-report substrings;
    # an interim statement must never be classified as both kinds.
    annual = bool(re.search(r"(?<!半)年度报告|(?<!半)年度報告|(?<!半)年报|(?<!半)年報|"
                            r"全年|(?<![a-z])annual(?![a-z])|full.year|final results|"
                            r"(?<!半)年度業績|(?<!半)年度业绩", text))
    kinds = int(q1) + int(q3) + int(interim) + int(annual)
    if kinds > 1:
        return 0  # E.g. a correction comparing both interim and annual periods.
    if market == "A股":
        expected = {3: q1, 6: interim, 9: q3, 12: annual}.get(target.month, False)
    else:
        # HK fiscal year ends vary. Require an explicit ending date for
        # non-standard fiscal year ends rather than guessing from a calendar.
        expected = (q1 or q3 or interim or annual) and (
            target in explicit or
            (target.month == 6 and interim) or
            (target.month == 12 and annual))
    if not expected:
        return 0
    # Full-length reports are preferred over preliminary results announcements.
    return (100 if kind == "full_report" else 40) + (30 if target in explicit else 0)


def filing_search_windows(period, as_of, *, days=420, width=55):
    """Bounded, non-overlapping filing searches; no disclosure after snapshot."""
    start = date.fromisoformat(str(period))
    end = date.fromisoformat(str(as_of))
    if end < start or (end - start).days > days:
        return []
    windows = []
    cursor = start
    while cursor <= end:
        stop = min(cursor + timedelta(days=width - 1), end)
        windows.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return windows



def matched_report_notices(evidence, period):
    """Revalidate stored period tags instead of trusting old misclassifications."""
    if not period:
        return []
    matched = []
    for notice in evidence.get("公告", []):
        try:
            if (notice.get("报告期匹配") == period and
                    max(period_title_score(notice.get("标题", ""), period, market)
                        for market in ("港股", "A股")) >= 100):
                matched.append(notice)
        except (TypeError, ValueError):
            continue
    return matched
