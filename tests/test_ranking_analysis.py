"""Versioned point-in-time classification, valuation and ranking comparisons."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

import pandas as pd

from picker import build_a_snapshot
from ranking_analysis import classifications, comparison_html, valuation_component


class RankingAnalysisTests(unittest.TestCase):
    def test_positive_tail_loss_zero_missing_and_negative_equity(self):
        sample = pd.Series([10., 100., 510., -20., 0., float("nan")])
        scores, labels = valuation_component(sample, "PE", 100)
        self.assertGreater(scores[0], scores[1])
        self.assertGreater(scores[1], scores[2])
        self.assertEqual(scores.tolist()[3:], [0., 0., 12.5])
        self.assertEqual(labels.tolist()[1:], ["高倍数（参与排名）", "高倍数（参与排名）", "亏损", "零值（不可比）", "缺失"])
        self.assertEqual(valuation_component(sample, "PB", 10)[1][3], "净资产为负")

    def test_release_never_backdates_or_mixes_classification_systems(self):
        quotes = pd.DataFrame({"代码": ["000001", "000002"], "行业": ["当日甲", "当日乙"]})
        with TemporaryDirectory() as path:
            release = {"published": "2026-09-30", "sectors": {"J66": {"name": "货币金融服务", "members": "000001"}},
                       "title": "分类表", "source_url": "https://www.capco.org.cn/", "pdf_sha256": "digest", "count": 1}
            (Path(path)/"2026-09-30.json").write_text(json.dumps(release), encoding="utf8")
            for before in ("2026-09-29", "2026-09-30"):
                sectors, _ = classifications(quotes, before, path)
                self.assertEqual(sectors.tolist(), ["当日甲", "当日乙"])
            sectors, source = classifications(quotes, "2026-10-01", path)
            self.assertEqual(sectors[0], "货币金融服务")
            self.assertTrue(pd.isna(sectors[1]))
            self.assertEqual(source["matched"], 1)

    def test_frozen_13th_maintains_original_report_and_comparison_cohorts(self):
        folder = Path(__file__).resolve().parents[1]/"docs/history/2026-11-13"
        with ZipFile(folder/"a-quotes.zip") as z:
            with z.open("verified-friday-quotes.csv") as stream:
                frame = pd.read_csv(stream, dtype={"代码": str}, float_precision="round_trip",
                                    encoding="utf-8-sig")
            frame.attrs["quality"] = json.loads(z.read("quality.json"))
        codes = [s["代码"] for s in json.loads((folder/"summary.json").read_text(encoding="utf8"))["a_shares"]["top20"]]
        snapshot, ranked = build_a_snapshot(frame, "2026-11-13 16:15", published_codes=codes)
        analysis, selections = snapshot["ranking_analysis"], snapshot["comparison_selections"]
        self.assertEqual(selections["legacy"], codes)
        self.assertEqual([v["代码"] for v in snapshot["a_shares"]], codes)
        self.assertEqual((analysis["source"]["matched"], len(ranked)), (3487, 3506))
        self.assertTrue(ranked["规模分"].between(0, 100).all())
        self.assertEqual(analysis["concentrations"]["legacy"]["货币金融服务"], 13)
        self.assertEqual(analysis["overlaps"]["sensitivity"], {"top5": 2, "top20": 11})
        self.assertEqual(analysis["overlaps"]["revised"]["top20"], 16)
        self.assertEqual(analysis["valuation_counts"]["PE状态"]["亏损"], 798)
        self.assertEqual(analysis["valuation_counts"]["PE状态"]["高倍数（参与排名）"], 549)
        for key, group in selections.items():
            self.assertEqual(len(set(group)), 20, key)
        self.assertTrue(all(analysis["stocks"][c]["industry_size"] >= 5 for c in selections["industry"]))
        self.assertIn("513.97", comparison_html(snapshot))
        self.assertIn("未分类", comparison_html(snapshot))
        self.assertEqual(comparison_html({"updated": "2026-11-13"}), "")
        json.dumps(snapshot, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
