import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

import ai_report
import bank_research_edition as edition


class BankEditionTests(unittest.TestCase):
    def test_generate_review_and_publish_on_pinned_13_november_filings(self):
        original = Path(__file__).resolve().parents[1] / "docs"
        day = "2026-11-13"
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            docs = root / "docs"
            archive = docs / "history" / day
            archive.mkdir(parents=True)
            for name in ("summary.json", "index.html", "ai.html", edition.EVIDENCE,
                         "a-quotes.zip", "a-research-sources.json.gz"):
                shutil.copyfile(original / "history" / day / name, archive / name)
            for name in ("data.json", "ai-status.json", "index.html"):
                shutil.copyfile(original / name, docs / name)
            # Future repo HEAD may have a newer current close. Explicitly test
            # publication against the same close, then against a newer close.
            snapshot, _, counts = edition.inputs(archive)
            (docs / "data.json").write_text(json.dumps(snapshot), encoding="utf-8")
            summary_path = archive / "summary.json"
            original_summary = json.loads(summary_path.read_text(encoding="utf-8"))
            original_summary.pop("bank_research", None)
            summary_path.write_text(json.dumps(original_summary), encoding="utf-8")
            hashes = {name: hashlib.sha256((archive / name).read_bytes()).digest()
                      for name in ("ai.html", "a-research-sources.json.gz", edition.EVIDENCE)}
            amounts = {"601169": 3, "002948": 4, "000001": 4, "600919": 3, "601665": 3}
            self.assertEqual(counts, amounts)
            output = root / "mock-reviewed.html"
            cache = root / "mock-only-cache"
            # The renderer still runs the real review gates. This fixture
            # deliberately produces a facts-only card for every issuer.
            with patch.dict("os.environ", {"AI_API_KEY": "test", "AI_MODEL": "test",
                                           "AI_BASE_URL": "https://example.invalid"}), \
                    patch.object(ai_report, "request_deepseek", return_value="test response") as model, \
                    patch.object(ai_report, "read_filings", side_effect=AssertionError("frozen PDF refetch")):
                edition.generate(archive, output, cache)
                self.assertEqual(model.call_count, 5)
                for call, (code, count) in zip(model.call_args_list, amounts.items()):
                    inputs = json.loads(call.args[3].split("输入资料：", 1)[1])
                    self.assertEqual(inputs["股票"]["代码"], code)
                    self.assertEqual(len(inputs["银行指标核对"]["指标"]), count)
                edition.generate(archive, output, cache)
                self.assertEqual(model.call_count, 5)
            text = output.read_text(encoding="utf-8")
            parsed = BeautifulSoup(text, "html.parser")
            for code, count in amounts.items():
                article = parsed.find(id=f"stock-{code}").parent
                self.assertEqual(len(article.select(".bank-evidence p b")), count)
                links = article.select(".bank-evidence p small a")
                self.assertEqual(len(links), count + (code == "600919"))
                self.assertTrue(all(a["href"].startswith("https://static.cninfo.com.cn/")
                                    and "#page=" in a["href"] for a in links))
            bad = root / "bad.html"
            link = parsed.select_one(".bank-evidence a")["href"]
            bad.write_text(text.replace(link, "https://example.invalid/#page=99", 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "财报指标"):
                edition.publish(archive, bad)
            self.assertFalse((archive / edition.REPORT).exists())
            edition.publish(archive, output)
            info = json.loads(summary_path.read_text(encoding="utf-8"))["bank_research"]
            self.assertEqual(info["metric_counts"], amounts)
            self.assertEqual(info["facts_only"], 5)
            self.assertEqual((archive / edition.REPORT).read_text(encoding="utf-8"), text)
            self.assertIn("0.98%", (docs / "ai.html").read_text(encoding="utf-8"))
            self.assertEqual(info["evidence_sha256"], hashes[edition.EVIDENCE].hex())
            self.assertEqual(len(BeautifulSoup((archive / "index.html").read_text(encoding="utf-8"),
                                               "html.parser").select('a[href="./ai-bank.html"]')), 1)
            data = dict(snapshot, updated="2026-11-16 15:00")
            (docs / "data.json").write_text(json.dumps(data), encoding="utf-8")
            (docs / "ai.html").write_text("newest", encoding="utf-8")
            edition.publish(archive, output)
            self.assertEqual((docs / "ai.html").read_text(encoding="utf-8"), "newest")
            for name, digest in hashes.items():
                self.assertEqual(hashlib.sha256((archive / name).read_bytes()).digest(), digest)
