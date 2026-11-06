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


def extract(data):
    proc = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"],
                          input=data, capture_output=True, check=True, timeout=25)
    candidates = []
    for page, text in enumerate(proc.stdout.decode("utf-8").split("\f"), 1):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            # Keep nearby headings, table units and comparison periods with the matched line.
            if re.search(r"\.{4,}|…{2,}", line):
                continue
            topics = {j for j, pattern in enumerate(TOPICS) if pattern.search(line)}
            if not topics:
                continue
            numeric = bool(re.search(r"\d[\d,.]*[%％]|\d[,.]\d", line))
            score = len(topics) + numeric + 2 * bool(topics & {3, 4, 5})
            passage = "\n".join(lines[max(0, i-3):i] + [line[:360]] + lines[i+1:i+3])
            # Keep the matching line even when a neighbouring table row is unusually long.
            if len(passage) > 580:
                passage = "\n".join([part[:80] for part in lines[max(0, i-2):i]] +
                                    [line[:270]] + [part[:65] for part in lines[i+1:i+3]])
            candidates.append((score, page, i, topics, passage))
    chosen, covered = [], set()
    while candidates and len(chosen) < 4:
        best = max(candidates, key=lambda c: (len(c[3] - covered), c[0]))
        _, page, index, topics, passage = best
        chosen.append({"页": page, "摘录": passage})
        covered.update(topics)
        candidates = [c for c in candidates if c[1] != page or abs(c[2]-index) > 4]
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
