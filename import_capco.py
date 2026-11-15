"""Extract an official CAPCO classification release, ordered by security code.

Used when CAPCO publishes a new semiannual PDF. Requires pdfplumber only for
the import. Daily ranking reads the checked-in, dated result offline.
"""
import argparse
from collections import defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def extract(pdf, published, title, source_url):
    import pdfplumber
    date.fromisoformat(published)
    url = urlsplit(source_url)
    if url.scheme != "https" or not (url.hostname or "").endswith(".capco.org.cn"):
        raise ValueError("需要中上协官网的 HTTPS 附件链接")
    names, members, seen = {}, defaultdict(list), set()
    with pdfplumber.open(pdf) as doc:
        for index, page in enumerate(doc.pages):
            tables = page.extract_tables()
            if len(tables) != 1:
                raise ValueError(f"第 {index+1} 页表格数异常")
            rows = tables[0]
            if index == 0:
                if rows.pop(0)[0] != title:
                    raise ValueError("首页标题与期次不一致")
            if [str(v).replace("\n", "") for v in rows.pop(0)] != [
                "上市公司代码", "上市公司简称", "门类代码", "门类简称", "次类代码",
                "次类简称", "大类代码", "大类简称",
            ]:
                raise ValueError(f"第 {index+1} 页表头不一致")
            for row in rows:
                code, sector, name = row[0], row[2] + row[6], "".join(row[7].split())
                if (not re.fullmatch(r"[0-9]{6}", code) or code in seen
                        or not re.fullmatch(r"[A-Z][0-9]{2}", sector)
                        or not name or names.setdefault(sector, name) != name):
                    raise ValueError(f"分类行异常 {row!r}")
                seen.add(code)
                members[sector].append(code)
            page.close()
    return {
        "title": title, "published": published, "source_url": source_url,
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "count": len(seen),
        "sectors": {s: {"name": names[s], "members": " ".join(sorted(members[s]))}
                    for s in sorted(members)},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("published", help="Official publication date, YYYY-MM-DD")
    parser.add_argument("title")
    parser.add_argument("source_url")
    args = parser.parse_args()
    release = extract(args.pdf, args.published, args.title, args.source_url)
    target = Path(__file__).resolve().parent / "industry" / f"{args.published}.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(release, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{target.name} • {release['count']} securities")
