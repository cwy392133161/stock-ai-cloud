"""Strict source anchoring for DeepSeek research.

A matching page number alone is NOT a verified claim. For current reports we
only display a source excerpt when a short verbatim quote occurs on that page.
Free-form analyst commentary is separately filtered, and no model-generated
paraphrase is certified as semantically entailed by the quote.
"""
import html
import json
import re
import unicodedata

from filing_text import trusted

SECTIONS = ("入选逻辑", "经营和估值", "反面证据", "跟踪条件")
REF = re.compile(r"^(D[1-9][0-9]*) p([1-9][0-9]*)$")
FINANCIAL_QUANTITY = re.compile(
    r"[0-9][0-9,.]*\s*(?:[%％]|元|万元|萬元|亿元|億元|港元|港币|港幣|HKD|CNY|USD|RMB|倍)",
    flags=re.I,
)


def _normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", html.unescape(str(text))))


def decode_report(content):
    """Expect four meaningful sections and a list of optional exact quotes."""
    if not isinstance(content, str):
        return None
    raw = content.strip()
    if raw.startswith(chr(96) * 3):
        raw = re.sub(r"^\x60{3}(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*\x60{3}$", "", raw)
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("sections"), dict):
        return None
    sections = obj["sections"]
    if not all(isinstance(sections.get(key), str) for key in SECTIONS):
        return None
    quotes = obj.get("source_quotes", [])
    if not isinstance(quotes, list):
        return None
    return {"sections": {key: sections[key] for key in SECTIONS},
            "source_quotes": quotes[:6]}


def validate_source_quote(entry, notices):
    """Return a reliable verbatim excerpt + link; never certify the AI's claim."""
    if not isinstance(entry, dict):
        return None
    ref = entry.get("citation")
    quote = entry.get("quote")
    if not isinstance(ref, str) or not isinstance(quote, str):
        return None
    match = REF.fullmatch(ref.strip())
    if not match:
        return None
    quote = quote.strip()
    canonical = _normalized(quote)
    if not (8 <= len(canonical) <= 180):
        return None
    for notice in notices:
        if str(notice.get("编号")) != match[1] or not trusted(str(notice.get("链接", ""))):
            continue
        for page in notice.get("正文摘录", []):
            if str(page.get("页")) != match[2]:
                continue
            raw = _normalized(page.get("摘录", ""))
            if canonical not in raw:
                continue
            # The original PDF may omit table units. The model may quote a
            # numeric row only if table headers and comparable period are present.
            if FINANCIAL_QUANTITY.search(quote):
                if any(page.get(k, "未定位") in (None, "", "未定位")
                       for k in ("单位", "表头", "期间上下文")):
                    return None
            return {
                "原文短引": quote,
                "引用": ref.strip(),
                "官方PDF": str(notice["链接"]) + "#page=" + match[2],
                "表头": page.get("表头", "未定位"),
                "单位": page.get("单位", "未定位"),
                "期间": page.get("期间上下文", "未定位"),
                "校验": "仅逐字核对摘录位置；不认证模型的因果解释",
            }
    return None


def verified_quotes(entries, notices):
    kept = []
    seen = set()
    for entry in entries:
        matched = validate_source_quote(entry, notices)
        if matched and (matched["引用"], matched["原文短引"]) not in seen:
            kept.append(matched)
            seen.add((matched["引用"], matched["原文短引"]))
    return kept
