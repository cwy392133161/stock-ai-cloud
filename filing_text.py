"""Bounded extraction of page-numbered evidence from official disclosure PDFs."""
import re
import subprocess
from http.client import HTTPException
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

HOSTS = {"static.cninfo.com.cn", "www1.hkexnews.hk"}
MAX_BYTES = 24 * 1024 * 1024
IMPORTANT = re.compile(
    r"季度报告|季度報告|年度报告|年度報告|半年度|中期報告|中期报告|業績|业绩|盈利[警預预喜]|"
    r"重大|盈利預警|盈利預告|盈利警示|盈警|中[標标]|重要合同|收[購购]|出售|盈利警告|須予披露|内幕消息|內幕消息|"
    r"annual report|interim report|results|profit warning|material contract|major transaction", re.I)
TOPICS = [
    re.compile(pattern, re.I) for pattern in (
        r"营业收入|營業收入|收益|营收|revenue|turnover",
        r"净利润|淨利潤|溢利|虧損|亏损|profit|loss",
        r"现金流|現金流|現金淨額|cash flows?",
        r"主要原因|主要由於|主要由于|主要因|主要系|归因于|attributable|mainly due",
        r"经营风险|經營風險|不确定性|不確定性|风险因素|風險因素|risk factors?",
        r"合同金额|合同金額|交易代价|交易代價|中标金额|交易对方|交易對方|签署|簽訂|履行期限|诉讼|訴訟|停产|停產|重组|重組|收購|收购|合約|consideration|contract value",
    )
]


def trusted(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.netloc in HOSTS


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not trusted(newurl):
            raise ValueError("原文跳转到非官方地址")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url):
    if not trusted(url):
        raise ValueError("原文地址未核实")
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with build_opener(OfficialRedirect()).open(req, timeout=15) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("文件超过大小上限")
    if b"%PDF-" not in data[:1024]:
        raise ValueError("返回内容非 PDF")
    return data



# Only capture metadata printed in the same PDF page. Never assume a number is
# in yuan, HKD, thousands, or millions simply because the issuer is listed in
# a particular market.
UNIT_LINE = re.compile(
    r"(?:金额单位|金額單位|货币单位|貨幣單位|计量单位|計量單位|单位|單位|units?)"
    r"\s*[：:=]\s*(?:人民币|人民幣|港币|港幣|港元|美元|元|千元|万元|萬元|百万元|百萬|亿|億|"
    r"RMB|HKD|USD|HK\$|US\$|CNY)[^。；;]{0,40}|"
    r"(?:RMB|HKD|USD|HK\$|US\$)\s*['’]?\s*0{3}\b",
    re.I,
)
PERIOD_LINE = re.compile(
    r"(?:报告期|報告期|截至|截至於|截至于|止[三六九十]?[個个]?[月年]|"
    r"period ended|year ended|months ended|季度报告|季度報告|中期報告|年度報告)"
    r".{0,70}(?:\d{4}|本期|上年|同期)|"
    r"\d{4}\s*年\s*第?[一二三四1234]\s*季度",
    re.I,
)
COLUMN_WORDS = re.compile(
    r"本期|本报告期|本報告期|上期|上年同期|上年同[期日]|"
    r"当期|當期|期末|期初|本年累计|本年累計|"
    r"current period|prior period|previous year|comparative|"
    r"\b20\d{2}\b",
    re.I,
)


def _table_header(line):
    """Identify likely financial column headings, not a financial value row."""
    text = line.strip()
    labels = len(COLUMN_WORDS.findall(text))
    has_label = bool(re.search(r"项目|項目|指标|指標|科目|item|particulars", text, re.I))
    columns = bool(re.search(r"\s{2,}|\t", line))
    return bool((has_label and labels >= 1) or (labels >= 2 and columns))


def _nearest(lines, i, predicate, *, radius=24):
    for index in range(i, max(-1, i - radius - 1), -1):
        item = lines[index]
        if predicate(item):
            return item.strip()[:180]
    return None


def extract(data):
    """Find short page-cited passages, retaining only explicit page-local context.

    Table heading and unit fields state '未定位' if absent; the LLM must not
    borrow a heading from a different page, row, or reporting period.
    """
    proc = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                          input=data, capture_output=True, check=True, timeout=25)
    candidates = []
    for page, text in enumerate(proc.stdout.decode("utf-8", errors="replace").split("\f"), 1):
        lines = [line.rstrip().strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if re.search(r"\.{4,}|…{2,}", line):
                continue
            topics = {j for j, pattern in enumerate(TOPICS) if pattern.search(line)}
            if not topics:
                continue
            numeric = bool(re.search(r"\d[\d,.]*[%％]|\d[,.]\d", line))
            score = len(topics) + numeric + 2 * bool(topics & {3, 4, 5})
            # Never borrow a unit or comparative column label from another PDF
            # page. Use the last preceding metadata line only, when available.
            unit = _nearest(lines, i, lambda x: bool(UNIT_LINE.search(x)), radius=32)
            header = _nearest(lines, i, _table_header, radius=18)
            period = _nearest(lines, i, lambda x: bool(PERIOD_LINE.search(x)), radius=32)
            body = lines[max(0, i-2):i] + [line] + lines[i+1:i+3]
            passage = "\n".join(x[:240] for x in body)
            if len(passage) > 570:
                passage = "\n".join(x[:110] for x in body)
            context = {"页": page, "摘录": passage[:570],
                       "表头": header or "未定位", "单位": unit or "未定位",
                       "期间上下文": period or "未定位"}
            # Prefer candidates with usable units/headers but still include
            # non-numeric management explanations and explicit risk factors.
            score += int(bool(unit)) + int(bool(header)) + int(bool(period))
            candidates.append((score, page, i, topics, context))
    chosen, covered = [], set()
    while candidates and len(chosen) < 4:
        best = max(candidates, key=lambda c: (len(c[3] - covered), c[0]))
        _, page, index, topics, context = best
        chosen.append(context)
        covered.update(topics)
        candidates = [c for c in candidates if c[1] != page or abs(c[2] - index) > 4]
    return sorted(chosen, key=lambda c: c["页"])


def read_filings(notices):
    successes = 0
    for index, notice in enumerate(notices, 1):
        notice["编号"] = f"D{index}"
        if successes >= 2:
            notice["正文状态"] = "仅列索引（已选取两份正文）"
            continue
        try:
            passages = extract(download(notice["链接"]))
            notice["正文摘录"] = passages
            notice["正文状态"] = "已提取正文片段" if passages else "未提取到相关可读文字"
            successes += bool(passages)
        except (HTTPException, OSError, ValueError, subprocess.SubprocessError) as exc:
            notice["正文状态"] = f"正文读取失败（{type(exc).__name__}）"
