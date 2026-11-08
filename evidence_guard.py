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


def _bounded_excerpt(quote, passage):
    """Reject cuts through words/clauses, including dropped negation prefixes.

    Whitespace may differ in PDF extraction, but is not a semantic boundary:
    a line wrap must not turn "未\n确认" into a positive assertion.
    """
    whole = _normalized(passage)

    def boundary(index, *, start=False):
        char = whole[index]
        if char in ("。！？!?；;:：" if start else "。！？!?；;"):
            return True
        # An English full stop is a sentence boundary, not a decimal point.
        return char == "." and not (
            index > 0 and index + 1 < len(whole)
            and whole[index - 1].isdigit() and whole[index + 1].isdigit())

    for match in re.finditer(re.escape(quote), whole):
        begins = match.start() == 0 or boundary(match.start() - 1, start=True)
        # Accept a complete sentence with or without its terminal punctuation.
        # Commas/colons are not terminal: a following qualification may reverse
        # the apparent meaning of the prefix.
        ends = (match.end() == len(whole) or boundary(match.end())
                or boundary(match.end() - 1))
        if begins and ends:
            return True
    return False


def _needs_numeric_context(quote):
    # Bare table values and currency-prefix notation are quantities too.
    # Requiring context for any digit is deliberately conservative; a date-only
    # quote can be omitted rather than certifying an ambiguous numerical row.
    normalized = _normalized(quote)
    return bool(re.search(r"[0-9]", normalized) or FINANCIAL_QUANTITY.search(normalized)
                or re.search(r"[零〇一二两兩三四五六七八九十百千万萬亿億]+"
                             r"(?:元|港元|美元|倍|%|％)", normalized))


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
    claims = obj.get("source_claims", [])
    if not isinstance(quotes, list) or not isinstance(claims, list):
        return None
    return {"sections": {key: sections[key] for key in SECTIONS},
            "source_quotes": quotes[:6],
            "source_claims": claims[:8]}


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
    if EXTERNAL_INSTRUCTION.search(quote):
        return None
    if not (8 <= len(canonical) <= 180):
        return None
    for notice in notices:
        if str(notice.get("编号")) != match[1] or not trusted(str(notice.get("链接", ""))):
            continue
        for page in notice.get("正文摘录", []):
            if str(page.get("页")) != match[2]:
                continue
            raw = _normalized(page.get("摘录", ""))
            if not _bounded_excerpt(canonical, raw):
                continue
            # The original PDF may omit table units. The model may quote a
            # numeric row only if table headers and comparable period are present.
            if _needs_numeric_context(quote):
                if any(not isinstance(page.get(k), str) or page[k].strip() in ("", "未定位")
                       for k in ("单位", "表头", "期间上下文")):
                    continue
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


CAUSAL_WORDS = re.compile(r"主要系|主要由|原因|由于|由於|导致|導致|因而|归因|歸因|mainly due|attributable", re.I)
EXTERNAL_INSTRUCTION = re.compile(
    r"忽略(?:之前|前面|以上|所有).{0,15}(?:指令|限制|规则)|"
    r"要求(?:模型|系统|AI).{0,10}(?:执行|泄露|输出)|"
    r"ignore previous instructions|system prompt|assistant should", re.I
)


def validate_source_claim(entry, notices):
    """Publish a disclosure claim ONLY as a direct quotation, not a paraphrase.

    A title/link/valid page number cannot establish entailment. Requiring the
    actual claim text to equal the quote, and a bounded quote on the cited
    page, prevents contradiction-by-paraphrase and wrong-page sourcing. We
    cannot certify financial meaning automatically; higher-order inference
    stays in an explicitly hypothetical section.
    """
    if not isinstance(entry, dict):
        return None
    label = entry.get("claim")
    quote = entry.get("quote")
    if not isinstance(label, str) or not isinstance(quote, str):
        return None
    exact = _normalized(label)
    whole = _normalized(quote)
    if not 8 <= len(exact) <= 150 or exact != whole:
        return None
    if EXTERNAL_INSTRUCTION.search(label) or EXTERNAL_INSTRUCTION.search(quote):
        return None
    source = validate_source_quote(entry, notices)
    if not source:
        return None
    typ = entry.get("type")
    if typ not in ("直接披露", "经营原因"):
        return None
    if typ == "经营原因" and not CAUSAL_WORDS.search(label):
        return None
    return {
        **source,
        "主张原文": label,
        "主张类别": typ,
        "证据校验": "主张与核对后的原文短引逐字一致，未采用AI改写；仍需人工复核原文语境",
    }


def verified_claims(entries, notices):
    kept = []
    seen = set()
    for item in (entries or [])[:8]:
        result = validate_source_claim(item, notices)
        if result and (result["引用"], result["主张原文"]) not in seen:
            seen.add((result["引用"], result["主张原文"]))
            kept.append(result)
    return kept


def verified_quotes(entries, notices):
    kept = []
    seen = set()
    for entry in entries:
        matched = validate_source_quote(entry, notices)
        if matched and (matched["引用"], matched["原文短引"]) not in seen:
            kept.append(matched)
            seen.add((matched["引用"], matched["原文短引"]))
    return kept

