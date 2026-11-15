import gzip
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from a_share_history import CST, _audited_sessions, archive_close, refresh_tracking
from forward_performance import BUY_COST, SELL_COST, Quote, evaluate_cohort, read_bars, target_turnover


def quote(opening=10, close=10, previous=10, buy="open", sell="open"):
    return Quote(opening, close, previous, buy, sell)


class ForwardTests(unittest.TestCase):
    def test_next_open_buy_cost_exit_cost_drawdown_and_adjustment(self):
        # Deliberately use a split-like reference close. Direct endpoint division is wrong.
        bars = [{"A": quote(10, 12), "B": quote()}, {"A": quote(6, 5, 6), "B": quote()}]
        groups = {"published": ["A"], "benchmark": ["A", "B"]}
        result = evaluate_cohort(groups, bars)
        a = result["published"]
        self.assertEqual(a["gross_return_pct"], 0)
        self.assertAlmostEqual(a["max_drawdown_pct"], 100 * (1 - 1 / 1.2), places=5)
        self.assertAlmostEqual(a["marked_return_pct"], 100 * (1 / (1 + BUY_COST) - 1), places=5)
        self.assertAlmostEqual(a["estimated_liquidation_return_pct"],
                               100 * ((1 - SELL_COST) / (1 + BUY_COST) - 1), places=5)
        self.assertEqual(a["excess_pct_points"], 0)

    def test_delayed_entry_denominator_suspension_and_t_plus_one(self):
        a, locked, suspend = quote(), quote(buy="limit"), quote(buy="suspended", sell="suspended")
        bars = [{"A": a, "B": locked, "C": suspend},
                {"A": suspend, "B": quote(5, 6), "C": suspend}]
        r = evaluate_cohort({"benchmark": ["A", "B", "C"]}, bars)["benchmark"]
        self.assertEqual(r["known_count"], 3)
        self.assertEqual(r["delayed_entries"], 2)
        self.assertEqual(r["exit_states"]["t_plus_one"], 1)
        self.assertEqual(r["exit_states"]["cash"], 1)
        self.assertEqual(r["exit_states"]["suspended"], 1)
        self.assertAlmostEqual(r["gross_return_pct"], 100 * .2 / 3, places=5)
        self.assertIsNone(r["estimated_liquidation_return_pct"])

    def test_missing_name_retained_for_benchmark_and_partial_attribution(self):
        bars = [{"A": quote(10, 11), "B": quote()}, {"A": quote(11, 11, 11)}]
        r = evaluate_cohort({"legacy": ["A"], "benchmark": ["A", "B"]}, bars)
        self.assertEqual(r["benchmark"]["unavailable_codes"], ["B"])
        self.assertIsNone(r["benchmark"]["marked_return_pct"])
        self.assertIsNone(r["legacy"]["excess_pct_points"])
        self.assertAlmostEqual(r["benchmark"]["known_contribution_pct_points"],
                               r["legacy"]["marked_return_pct"] / 2, places=5)

    def test_missing_previous_close_and_locked_exit(self):
        marked = evaluate_cohort({"benchmark": ["A"]},
                                 [{"A": quote()}, {"A": quote(close=9, sell="limit")}])["benchmark"]
        self.assertIsNotNone(marked["marked_return_pct"])
        self.assertIsNone(marked["estimated_liquidation_return_pct"])
        missed = evaluate_cohort({"benchmark": ["A"]}, [{"A": quote()}, {"A": quote(previous=None)}])
        self.assertEqual(missed["benchmark"]["unavailable_codes"], ["A"])
        self.assertEqual(target_turnover(["A", "B"], ["A", "C", "D", "E"]), 75)
        self.assertIsNone(target_turnover(["B"], None))

    def test_stale_trade_and_ambiguous_fill_never_treated_as_current(self):
        fields = ["代码", "成交量", "今开", "最新价", "昨收", "最高", "最低", "报价时间", "涨停价", "跌停价"]
        rows = [["A", 5, 10, 10, 9, 10, 10, "20261113151000", None, None],
                ["B", 5, 10, 10, 9, 11, 9, "20261113151000", None, None],
                ["C", 5, 10, 10, 9, 11, 9, "20261113145900", 10, 9],
                ["D", 0, 0, None, 9, 0, 0, "20261112151000", 10, 8],
                ["E", 5, 10, 9, 9, 10, 9, "20261113151000", 10, 9]]
        bars = read_bars({"columns": fields, "rows": rows}, "2026-11-13")
        self.assertEqual((bars["A"].buy, bars["A"].sell), ("unknown", "unknown"))
        self.assertEqual((bars["B"].buy, bars["B"].sell), ("open", "open"))
        self.assertEqual((bars["E"].buy, bars["E"].sell), ("limit", "limit"))
        self.assertEqual(set(bars), {"A", "B", "E"})


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.docs = Path(tmp.name)
        (self.docs / "history").mkdir()
        (self.docs / "history/index.html").write_text(
            '<main><section><p>原专题页</p></section></main>', encoding="utf-8")

    def freeze(self, day, *, eligible=("000001", "000002"), price=12, stamp=None, rankings=None):
        rows = [{"代码": code, "名称": "<公司>", "成交额": 9000, "今开": 10, "最新价": price,
                 "昨收": 12, "最高": max(price, 11), "最低": 9, "涨停价": 15, "跌停价": 8,
                 "报价时间": stamp or day.replace("-", "") + "150501"}
                for code in ("000001", "000002", "000003")]
        frame = pd.DataFrame(rows)
        ranked = frame.loc[frame["代码"].isin(eligible)].copy()
        ranked["评分"] = 70.
        snapshot = {"updated": day + " 16:01", "a_shares": [{"代码": eligible[0]}],
                    "quality": {"A股": {"报价日期": day}}, "comparison_selections": {"industry": []},
                    "ranking_analysis": rankings}
        return archive_close(self.docs, frame, ranked, snapshot, score_version="v1",
                             collected_at=datetime.fromisoformat(day + "T16:01").replace(tzinfo=CST))

    def test_atomic_frozen_quote_and_scoring_data(self):
        day = "2026-11-13"
        with ThreadPoolExecutor(5) as pool:
            paths = list(pool.map(self.freeze, [day] * 5))
        self.assertEqual(len(set(paths)), 1)
        frozen = paths[0].read_bytes()
        record = json.loads(gzip.decompress(frozen))
        self.assertEqual(len(record["quotes"]["rows"]), 3)
        self.assertEqual(len(record["candidates"]["rows"]), 2)
        self.assertIn("评分", record["candidates"]["columns"])
        self.assertEqual(record["selections"]["industry"], [])
        for params in ({"price": 13}, {"rankings": {"version": 2}}):
            with self.assertRaisesRegex(ValueError, "已冻结"):
                self.freeze(day, **params)
        self.assertEqual(paths[0].read_bytes(), frozen)
        with self.assertRaisesRegex(ValueError, "收盘行情"):
            self.freeze("2026-11-16", stamp="20261116143000")
        self.assertFalse((self.docs / "history/2026-11-16").exists())

    def test_market_sessions_fix_exit_and_keep_signal_universe_without_survivorship(self):
        sessions = pd.bdate_range("2026-11-02", periods=22).strftime("%Y-%m-%d").tolist()
        # Later universe includes a newly eligible stock, yet the original benchmark uses only 1 and 2.
        with patch("a_share_history.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 15, 16, tzinfo=CST)
            self.freeze(sessions[0])
        for day in sessions[1:]:
            if day != sessions[3]:
                self.freeze(day, eligible=("000001", "000003"))
        partial = refresh_tracking(self.docs, sessions)
        first = partial["cohorts"][0]
        self.assertEqual(first["horizons"]["5"]["status"], "archive_gap")
        self.assertEqual(first["horizons"]["20"]["missing_dates"], [sessions[3]])
        self.assertIsNone(partial["cohorts"][3]["target_turnover_pct"]["published"])
        self.freeze(sessions[3], eligible=("000001", "000003"))
        result = refresh_tracking(self.docs, sessions)
        self.assertEqual(result["cohorts"][1]["target_turnover_pct"]["benchmark"], 50)
        for h in (5, 20):
            mature = result["cohorts"][0]["horizons"][str(h)]
            self.assertEqual(mature["status"], "mature")
            self.assertEqual(mature["last_session"], sessions[h])
            self.assertEqual(mature["portfolios"]["benchmark"]["known_count"], 2)
        self.assertEqual(result["cohorts"][-1]["horizons"]["5"]["status"], "immature")
        self.assertFalse(result["cohorts"][0]["available_before_entry"])
        catalog = (self.docs / "history/index.html").read_text(encoding="utf-8")
        self.assertIn("原专题页", catalog)
        self.assertEqual(catalog.count('id="frozen-closes"'), 1)
        self.assertIn(f'./{sessions[0]}/ranking.html', catalog)
        self.assertIn("候选池基准", catalog)
        dated = (self.docs / f"history/{sessions[0]}/ranking.html").read_text(encoding="utf-8")
        self.assertIn("&lt;公司&gt;", dated)
        self.assertIn("行业样本不足", dated)
        empty = refresh_tracking(self.docs, [])
        self.assertEqual(empty["cohorts"][0]["horizons"]["5"]["status"], "calendar_unavailable")

    def test_calendar_reuse_requires_continuous_verified_evidence(self):
        day = date(2026, 11, 13)
        path = self.freeze(day.isoformat())
        audit = {"date": day.isoformat(), "calendar_start": "2026-11-10",
                 "calendar_sessions": ["2026-11-10", "2026-11-11", "2026-11-12", day.isoformat()]}
        (path.parent / "trend-audit.json.gz").write_bytes(gzip.compress(json.dumps(audit).encode()))
        self.assertEqual(_audited_sessions(path, day, day, []), [day.isoformat()])
        start = date(2026, 11, 9)
        self.assertIsNone(_audited_sessions(path, start, day, [start.isoformat()]))
        merged = _audited_sessions(path, start, day, [start.isoformat(), "2026-11-10"])
        self.assertEqual(merged, [start.isoformat(), *audit["calendar_sessions"]])
        self.assertIsNone(_audited_sessions(path, start, day, [start.isoformat(), "2026-11-11"]))


if __name__ == "__main__":
    unittest.main()
