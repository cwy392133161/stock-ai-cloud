"""Publish an audited A-share research batch beside its historical close."""
import argparse
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from publish_guard import REPORT_SNAPSHOT


def _read_research(report_html, trade_date, stocks):
    report = BeautifulSoup(report_html, "html.parser")
    snapshot = REPORT_SNAPSHOT.search(report_html)
    if not snapshot or snapshot[1][:10] != trade_date:
        raise ValueError("研究报告与归档交易日不一致")
    articles = report.find_all("article")
    if len(articles) != 5 or len(stocks) != 5:
        raise ValueError("归档研究须包含 A 股榜单前五只")
    for article, stock in zip(articles, stocks):
        code, name = stock["代码"], stock["名称"]
        heading = article.find("b", recursive=False)
        anchor = f"stock-{code}"
        if (not heading or (article.get("id") != anchor and heading.get("id") != anchor) or
                len(report.find_all(id=anchor)) != 1 or
                heading.get_text(strip=True) != f"A股 · {name} ({code})"):
            raise ValueError(f"研究文章与归档榜单不一致：{code}")

    def meta(name):
        tags = report.select(f'meta[name="research-{name}"]')
        if len(tags) != 1:
            raise ValueError(f"研究报告缺少唯一审核标记：{name}")
        return tags[0].get("content", "")

    generation = meta("generation-id")
    if (meta("audit-version") != "evidence-scope-v2" or
            not re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", generation)):
        raise ValueError("报告缺少本次生成或审核记录")
    approved, limited, total = (int(meta(name + "-count"))
                                for name in ("approved", "limited", "total"))
    if total != len(stocks) or min(approved, limited) < 0 or approved + limited > total:
        raise ValueError("研究审核数量与归档文章数量不一致")
    return {
        "market": "A股", "snapshot": snapshot[1], "generation_id": generation,
        "report": "ai.html", "approved": approved, "conditional": limited,
        "facts_only": total - approved - limited,
        "articles": [{"code": s["代码"], "name": s["名称"],
                      "href": f'ai.html#stock-{s["代码"]}'} for s in stocks],
    }


def publish_archive_research(archive_dir, report_path):
    archive_dir = Path(archive_dir)
    summary_path = archive_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    day = summary["trade_date"]
    if day != archive_dir.name:
        raise ValueError("归档目录名和交易日不一致")
    report_html = Path(report_path).read_text(encoding="utf-8")
    research = _read_research(report_html, day, summary["a_shares"]["top20"][:5])

    index_path = archive_dir / "index.html"
    catalog_path = archive_dir.parent / "index.html"
    page = BeautifulSoup(index_path.read_text(encoding="utf-8"), "html.parser")
    catalog = BeautifulSoup(catalog_path.read_text(encoding="utf-8"), "html.parser")
    cells = page.select("#a-top20 tbody td.stock")[:5]
    if len(cells) != 5:
        raise ValueError("归档页缺少 A 股榜单前五只")
    for cell, entry in zip(cells, research["articles"]):
        name, code = cell.strong, cell.small
        if (not name or name.get_text(strip=True) != entry["name"] or
                not code or code.get_text(strip=True) != entry["code"]):
            raise ValueError("归档页与评分明细的 A 股榜单不同")
        link = name.parent if name.parent.name == "a" else name.wrap(page.new_tag("a"))
        link["href"] = "./" + entry["href"]
        link["aria-label"] = f'阅读{entry["name"]}的 AI 研究'

    nav = page.select_one('a[href="#a-shares"]').parent
    if not nav.find("a", href="./ai.html"):
        link = page.new_tag("a", href="./ai.html")
        link.string = "A 股前五名 AI 研究"
        nav.extend([" · ", link])
    sources_path = archive_dir / "a-research-sources.json.gz"
    sources_note = page.select_one("#a-research-sources")
    if sources_note is not None:
        sources_note.decompose()
    if sources_path.is_file():
        research["sources"] = sources_path.name
        sources_note = page.new_tag("p", id="a-research-sources")
        link = page.new_tag("a", href="./" + sources_path.name, download="")
        link.string = "下载 A 股前五名的财务数据、公告及财报摘录（JSON 压缩包）"
        sources_note.append(link)
        page.select_one("#a-shares").append(sources_note)
    day_link = catalog.find("a", href=f"./{day}/")
    if day_link is None:
        raise ValueError("历史归档目录缺少该交易日")
    description = day_link.find_parent("section").p
    report_link = catalog.new_tag("a", href=f"./{day}/ai.html")
    report_link.string = "A 股前五名 AI 研究"
    description.clear()
    description.extend(["A 股 Top 20、", report_link, "及公司公告样本。"])
    summary["research"] = research

    # The workflow publishes the resulting files together in one git commit.
    (archive_dir / "ai.html").write_text(report_html, encoding="utf-8")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    index_path.write_text(str(page).rstrip() + "\n", encoding="utf-8")
    catalog_path.write_text(str(catalog).rstrip() + "\n", encoding="utf-8")
    return research


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_dir", type=Path)
    parser.add_argument("report_path", type=Path)
    args = parser.parse_args()
    print(json.dumps(publish_archive_research(args.archive_dir, args.report_path), ensure_ascii=False))
