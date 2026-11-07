import unittest

from finance_facts import financial_comparison


def snapshot(profit, cash, revenue):
    return {
        "报告期": "2026-09-30", "期间": "2026 年初至报告期末（累计）", "状态": "可用",
        "报告币种": "CNY", "指标": {
            "营业总收入": f"{revenue} CNY（累计）", "归母净利润": f"{profit} CNY（累计）",
            "经营活动现金流量净额": f"{cash} CNY（期间合计）", "归母净利润同比": "-7.812 %（累计同比）",
        },
    }


class FinancialComparisonTests(unittest.TestCase):
    def test_baoxin_keeps_profit_distinct_from_falling_yoy(self):
        result = financial_comparison(snapshot("756843065.48", "2416230536.58", "6742522877.54"))
        self.assertIn("归母盈利 756,843,065.48 CNY", result)
        self.assertIn("现金净流入 2,416,230,536.58 CNY", result)
        self.assertIn("低于同期营业总收入 6,742,522,877.54 CNY", result)
        self.assertNotIn("亏损", result)

    def test_shandong_cash_outflow_is_compared_with_its_sign(self):
        result = financial_comparison(snapshot("1049221538.42", "-3819034073.83", "36742695287.83"))
        self.assertIn("归母盈利", result)
        self.assertIn("净流出 3,819,034,073.83 CNY", result)
        self.assertIn("低于同期营业总收入", result)
        self.assertIn("低于同期营业总收入", financial_comparison(snapshot("0", "-11", "10")))

    def test_loss_zero_and_near_equal_values(self):
        f = snapshot("-0.00001", "10.00000001", "10")
        f["指标"]["归母净利润同比"] = "90 %（累计同比）"
        self.assertIn("归母亏损 0.00001", financial_comparison(f))
        self.assertIn("高于同期营业总收入", financial_comparison(f))
        result = financial_comparison(snapshot("-0.00", "0", "0.00"))
        self.assertIn("归母净利润为零", result)
        self.assertIn("现金流量净额为零", result)
        self.assertIn("等于同期营业总收入", result)

    def test_unverified_or_malformed_amounts(self):
        for raw in ("5 USD（累计）", "5 CNY/股（累计）", "5 CNY（累计同比）",
                    "5 CNY（期末）", "5 CNY（单季）", "5,00 CNY（累计）", "5 CNY（累计）xx", None):
            with self.subTest(raw=raw):
                f = snapshot("1", "2", "3")
                f["指标"]["营业总收入"] = raw
                self.assertIn("可比金额不足", financial_comparison(f))
        f = snapshot("1", "2", "3")
        f["缺失"] = {"归母净利润": "缺失"}
        self.assertIn("归母盈亏待核实", financial_comparison(f))

    def test_hk_and_explicit_quarter(self):
        f = {"状态": "部分可用", "报告期": "2026-06-30", "报告币种": "HKD",
             "期间": "2026-01-01 至 2026-06-30（按实际期间合计）",
             "指标": {"营业收入": "10 HKD（期间合计）", "经营活动现金流量净额": "5 HKD（期间合计）"}}
        self.assertIn("低于同期营业收入 10 HKD", financial_comparison(f))
        f.update(期间="2026-04-01 至 2026-06-30（单季）")
        self.assertIn("可比金额不足", financial_comparison(f))
        f["指标"] = {k: v.replace("期间合计", "单季") for k, v in f["指标"].items()}
        self.assertIn("低于同期营业收入 10 HKD", financial_comparison(f))
        f["报告期"] = "2026-09-30"
        self.assertIn("期间口径未核实", financial_comparison(f))
