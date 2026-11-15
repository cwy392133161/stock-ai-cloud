"""Minimal A-share selections and corresponding rendered article headings."""
ROWS = [{"代码": f"{n:06d}"} for n in range(1, 6)]
ARTICLES = "".join(
    f'<article><b id="stock-{row["代码"]}">A股 · 样例 ({row["代码"]})</b></article>'
    for row in ROWS
)
