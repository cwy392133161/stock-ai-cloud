"""Bounded offline removal of ratio inferences, never a full model re-audit.

The source report and all evidence remain untouched. Do not mint generation
IDs or recompute approval counts from already-filtered published HTML.
"""
import hashlib
import re
from bs4 import BeautifulSoup
from bank_ratio_guard import BANK_RATIO_RULES, blocks_bank_ratio_inference


MARKER = "bank-ratio-correction"


def correct_page(source):
    page = BeautifulSoup(source, "html.parser")
    if page.select_one("." + MARKER):
        raise ValueError("页面已有局部更正，不得重复处理")
    articles = page.select("article")
    if len(articles) != 5 or not page.h1:
        raise ValueError("仅处理已核对的五篇银行报告")
    generation = page.select('meta[name="research-generation-id"]')
    if len(generation) != 1:
        raise ValueError("缺少唯一原生成编号")
    removed = []
    for article in articles:
        heading = article.find("b", recursive=False)
        if not heading or "银行" not in heading.get_text() or not article.select_one(".bank-evidence"):
            raise ValueError("银行主体或已显示的银行证据缺失")
        insight = article.select_one(".ai-insight")
        if insight is None:
            raise ValueError("缺少明确的模型正文边界")
        # Only four direct model paragraphs; never touch quotes or source tables.
        for paragraph in insight.find_all("p", recursive=False):
            label = paragraph.find("b", recursive=False)
            if not label or label.get_text() not in ("入选逻辑", "经营和估值", "反面证据", "跟踪条件"):
                continue
            original = paragraph.get_text(" ", strip=True)
            if not blocks_bank_ratio_inference(original):
                continue
            removed.append({"issuer": heading.get_text(), "section": label.get_text(),
                            "original_text": original})
            title = label.get_text()
            paragraph.clear()
            bold = page.new_tag("b")
            bold.string = title
            paragraph.extend([bold, page.new_tag("br"),
                "本段涉及尚未独立核验的拨备比率机制推断，已保守屏蔽。请按上方公式核对同口径余额；不据此认定其他段落已复审。"])
    if not removed:
        raise ValueError("未发现目标推断，不能伪称已更正")
    alert = page.new_tag("aside", attrs={"class": MARKER, "role": "alert",
        "style": "border:3px solid #b91c1c;background:#fff1f2;padding:16px;border-radius:8px"})
    title = page.new_tag("strong")
    title.string = "局部更正：拨备比率机制推断待复审"
    alert.append(title)
    for text in (BANK_RATIO_RULES,
                 "本次只对已发布正文作规则筛查并屏蔽相关段落，未调用AI、未恢复或重新审核模型原答。保留原生成ID、时间和证据，原通过计数仅为历史记录；不代表其余推论已核实。"):
        p = page.new_tag("p"); p.string = text; alert.append(p)
    page.h1.insert_after(alert)
    result = str(page)
    # Publication guard still reads some legacy meta markers by exact syntax.
    # Preserve their original bytes and ordering rather than reserializing them.
    original_meta = re.findall(r"<meta\b[^>]*>", source, re.I)
    if len(original_meta) != len(re.findall(r"<meta\b[^>]*>", result, re.I)):
        raise ValueError("原审核标记数量改变")
    markers = iter(original_meta)
    result = re.sub(r"<meta\b[^>]*>", lambda _: next(markers), result, flags=re.I)
    return result, {"scope": "bounded-published-text-correction-not-full-reaudit",
                    "original_generation_id": generation[0]["content"],
                    "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "corrected_sha256_before_status_banner": hashlib.sha256(result.encode()).hexdigest(),
                    "removed_paragraphs": removed,
                    "formula_source": "https://jrs.mof.gov.cn/zhengcefabu/cwjx/202006/t20200602_3524593.htm"}
