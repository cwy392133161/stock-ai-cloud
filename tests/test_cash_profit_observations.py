import copy
import unittest

from cash_profit_observations import cash_profit_observation as observe, AMOUNT_TYPE, CASH, PROFIT


def cash_profit_observation(finance):
    return observe(finance, "2026-11-10")
from research_brief import build_research_brief


def fixture(cash=(200, 400), profit=(380, 100)):
    periods = ("2026-09-30", "2025-09-30")
    rows = [{"报告期": p, "币种": "CNY",
             "期间": p[:4] + "-01-01 至 " + p + "（年初累计）",
             "披露时点依据": p[:4] + "-10-29",
             "指标": {CASH: str(cash[i]), PROFIT: str(profit[i])}}
            for i, p in enumerate(periods)]
    pairs = [{"指标": name, "类型": AMOUNT_TYPE, "报告期": periods[0],
              "基期": periods[1], "本期": str(values[0]), "对比期": str(values[1])}
             for name, values in ((CASH, cash), (PROFIT, profit))]
    return {"状态": "可用", "报告币种": "CNY", "报告期": periods[0],
            "期间": "2026 年初至报告期末（累计）", "指标": {},
            "历史财务": {"状态": "部分历史记录可用", "记录": rows,
                     "同口径年度对照": pairs}}


class CashProfitObservationTests(unittest.TestCase):
    def test_divergence_has_dates_amounts_and_scope(self):
        result = cash_profit_observation(fixture())["观察"]
        for text in ("归母净利润上升", "净额下降", "2025-09-30 → 2026-09-30",
                     "400.00 → 200.00 CNY", "合并口径", "不是同一权益归属范围"):
            self.assertIn(text, result)

    def test_two_periods_not_continuous_outflow(self):
        result = cash_profit_observation(fixture(cash=(-500, -600)))["观察"]
        self.assertIn("两个同报告季观察期", result)
        self.assertIn("不代表期间持续净流出", result)
        self.assertNotIn("连续", result)

    def test_current_only_positive_profit_negative_cash(self):
        self.assertIn("本期归母盈利", cash_profit_observation(
            fixture(cash=(-1, 2), profit=(5, 6)))["观察"])
        self.assertIsNone(cash_profit_observation(fixture(cash=(400, 200))))
        self.assertIsNone(cash_profit_observation(fixture(cash=(-1, -2), profit=(0, 1))))

    def test_invalid_inputs_fail_closed(self):
        base = fixture()
        mutations = [
            lambda f: f.update(状态="不可用"),
            lambda f: f.update(报告币种="HKD"),
            lambda f: f.update(期间="2026-07-01 至 2026-09-30（单季）"),
            lambda f: f["历史财务"].update(状态="最新报告期无唯一可信记录"),
            lambda f: f["历史财务"]["同口径年度对照"][0].update(基期="2024-09-30"),
            lambda f: f["历史财务"]["同口径年度对照"][0].update(本期="NaN"),
            lambda f: f["历史财务"]["同口径年度对照"][0].update(本期="Infinity"),
            lambda f: f["历史财务"]["同口径年度对照"][0].update(本期="bad"),
            lambda f: f["历史财务"]["同口径年度对照"].pop(),
            lambda f: f["历史财务"]["记录"][0].update(币种="未知"),
            lambda f: f["历史财务"]["记录"][0].update(期间="单季"),
            lambda f: f["历史财务"]["记录"][0].update(披露时点依据=""),
            lambda f: f["历史财务"]["记录"][0]["指标"].update(经营现金流净额="999"),
            lambda f: f["历史财务"]["记录"].append(f["历史财务"]["记录"][0]),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(i=i):
                f = copy.deepcopy(base)
                mutate(f)
                self.assertIsNone(cash_profit_observation(f))

    def test_missing_or_early_snapshot_does_not_license_observation(self):
        self.assertIsNone(observe(fixture()))
        self.assertIsNone(observe(fixture(), "invalid"))
        self.assertIsNone(observe(fixture(), "2026-10-01"))

    def test_brief_replaces_generic_followup_and_keeps_source_repair(self):
        brief = build_research_brief({}, fixture(), {}, "2026-11-10")
        self.assertIn("归母净利润上升", brief["反面证据"][0])
        self.assertIn("合并经营现金流", brief["后续验证点"][1])
        self.assertIn("官方财报PDF", brief["后续验证点"][2])


if __name__ == "__main__":
    unittest.main()
