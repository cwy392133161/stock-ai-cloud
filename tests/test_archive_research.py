import json
import unittest
from pathlib import Path
from shutil import copyfile
from tempfile import TemporaryDirectory

from bs4 import BeautifulSoup

from archive_research import publish_archive_research


class ArchiveResearchTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.archive = Path(tmp.name) / "2026-11-13"
        self.archive.mkdir()
        original = Path(__file__).resolve().parents[1] / "docs" / "history"
        for filename in ("index.html", "summary.json"):
            copyfile(original / self.archive.name / filename, self.archive / filename)
        copyfile(original / "index.html", self.archive.parent / "index.html")
        self.source = self.archive.parent / "generated.html"
        summary = json.loads((self.archive / "summary.json").read_text(encoding="utf-8"))
        self.stocks = summary["a_shares"]["top20"][:5]
        parts = ['<small>行情快照：2026-11-13 16:15；AI 生成：2026-11-15 23:26</small>']
        for key, value in (("audit-version", "evidence-scope-v2"),
                           ("generation-id", "8b403699061441529c28a9d8ba51480d"),
                           ("approved-count", "1"), ("limited-count", "3"), ("total-count", "5")):
            parts.append(f'<meta name="research-{key}" content="{value}">')
        for stock in self.stocks:
            code, name = stock["代码"], stock["名称"]
            parts.append(f'<article><b id="stock-{code}">A股 · {name} ({code})</b></article>')
        self.report = "".join(parts)
        self.source.write_text(self.report, encoding="utf-8")

    def test_publishes_verified_batch_with_working_relative_links_once(self):
        for _ in range(2):
            research = publish_archive_research(self.archive, self.source)
        self.assertEqual((self.archive / "ai.html").read_text(encoding="utf-8"), self.report)
        self.assertEqual(research["facts_only"], 1)
        page = BeautifulSoup((self.archive / "index.html").read_text(encoding="utf-8"), "html.parser")
        self.assertEqual(len(page.select('a[href="./ai.html"]')), 1)
        self.assertEqual([a["href"] for a in page.select("#a-top20 a")],
                         ["./ai.html#stock-" + s["代码"] for s in self.stocks])
        self.assertIsNotNone(page.select_one('#a-shares a[href="./a-quotes.zip"]'))
        self.assertFalse(page.select('#hong-kong, a[href="#hong-kong"], a[href="./hkex-quotes.zip"]'))
        self.assertNotIn("港股", page.get_text())
        catalog = BeautifulSoup((self.archive.parent / "index.html").read_text(encoding="utf-8"), "html.parser")
        self.assertEqual(len(catalog.select('a[href="./2026-11-13/ai.html"]')), 1)
        self.assertNotIn("港股", catalog.get_text())
        self.assertEqual(json.loads((self.archive / "summary.json").read_text(encoding="utf-8"))["research"], research)

    def test_optional_sources_file_has_one_download_and_summary_entry(self):
        path = self.archive / "a-research-sources.json.gz"
        path.touch()
        for available in (True, True, False):
            if not available:
                path.unlink()
            publish_archive_research(self.archive, self.source)
            page = BeautifulSoup((self.archive / "index.html").read_text(encoding="utf-8"), "html.parser")
            downloads = page.select(f'#a-shares a[download][href="./{path.name}"]')
            self.assertEqual(len(downloads), int(available))
            summary = json.loads((self.archive / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["research"].get("sources"), path.name if available else None)

    def test_rejects_mismatched_report_before_touching_archive(self):
        files = [self.archive / "index.html", self.archive / "summary.json", self.archive.parent / "index.html"]
        before = [f.read_bytes() for f in files]
        for old, new in (("2026-11-13", "2026-11-10"), ("stock-601169", "stock-000001"),
                         ('limited-count" content="3', 'limited-count" content="5')):
            with self.subTest(old=old):
                self.source.write_text(self.report.replace(old, new), encoding="utf-8")
                with self.assertRaises(ValueError):
                    publish_archive_research(self.archive, self.source)
                self.assertFalse((self.archive / "ai.html").exists())
                self.assertEqual(before, [f.read_bytes() for f in files])


if __name__ == "__main__":
    unittest.main()
