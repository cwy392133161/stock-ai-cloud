import unittest
from pathlib import Path
from unittest.mock import patch

from archived_a_snapshot import load_snapshot, score_a


class ArchivedSnapshotTests(unittest.TestCase):
    archive = Path(__file__).resolve().parents[1] / "docs/history/2026-11-13"

    def test_ties_keep_published_order_and_every_issuer_is_verified(self):
        def reorder_ties(frame):
            return score_a(frame).sort_values(["评分", "代码"], ascending=[False, True])
        with patch("archived_a_snapshot.score_a", side_effect=reorder_ties):
            rows = load_snapshot(self.archive)["a_shares"]
        self.assertEqual([r["代码"] for r in rows[5:8]], ["600977", "002931", "601328"])

        def change_factor(frame):
            result = reorder_ties(frame)
            result.loc[result["代码"] == "002931", "估值分"] += 1
            return result
        with patch("archived_a_snapshot.score_a", side_effect=change_factor):
            with self.assertRaisesRegex(ValueError, "002931.*估值分"):
                load_snapshot(self.archive)
