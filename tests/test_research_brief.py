import unittest

from research_brief import build_research_brief


def finance(status="可用", revenue="-12.5 %（累计同比）", profit="14.2 %（累计同比）"):
    return {
        "状态": status, "报告期": "2026-09-30", "报告币种": "CNY",
        "期间": "2026 年初至报告期末（累计）",
        "指标": {
            "营业总收入同比": revenue,
            "归母净利润同比": profit,
            "经营活动现金流量净额": "-300 CNY（期间合计）",
        },
    }


def stock():
    return {
        "名称": "样例股份", "代码": "000690", "评分": 81.1,
        "趋势分": 90, "活跃分": 79.5, "估值分": 70.2,
        "涨跌幅": 2.3, "换手率": 5.6,
        "入选原因": "60日上涨10%，换手率5.6%", "风险提示": "短期涨幅较大",
    }


class ResearchBriefTests(unittest.TestCase):
    def test_counter_evidence_and_checkpoints_depend_on_financial_facts(self):
        result = build_research_brief(stock(), finance(), None)
        self.assertIn("相对排名", result["行情入选依据"])
        self.assertIn("营收同比下降", result["财务变化线索"])
        self.assertIn("归母净利润同比增长", result["财务变化线索"])
        self.assertIn("增长不等于已盈利", result["财务变化线索"])
        self.assertTrue(any("营收同比下降" in x for x in result["反面证据"]))
        self.assertTrue(any("短期涨幅" in x for x in result["反面证据"]))
        self.assertTrue(any("对应期官方财报PDF" in x for x in result["后续验证点"]))

    def test_report_excerpt_is_not_claimed_full_text_audit(self):
        disclosure = {
            "对应报告期财报": {
                "报告期": "2026-09-30", "状态": "标题与报告期匹配，PDF正文待核验",
            },
            "公告": [{
                "报告期匹配": "2026-09-30",
                "正文摘录": [
                    {"页": 2, "摘录": "营业总收入", "表头": "未定位",
                     "单位": "未定位", "期间上下文": "2026年第三季度"},
                ],
            }],
        }
        result = build_research_brief(stock(), finance(), disclosure)
        coverage = "；".join(result["证据覆盖"])
        self.assertIn("已读取 1 个带页码片段", coverage)
        self.assertIn("非全篇审计", coverage)
        self.assertIn("缺少单位", coverage)
        self.assertFalse(any("官方财报PDF正文" in x for x in result["后续验证点"]))

    def test_missing_metrics_dont_invent_growth(self):
        record = finance(status="接口无记录")
        record["指标"] = {}
        record["报告期"] = None
        result = build_research_brief(stock(), record, {"公告": []})
        self.assertIn("无可核对指标", result["财务变化线索"])
        self.assertIn("未取得可核对片段", "；".join(result["证据覆盖"]))
        self.assertNotIn("营收同比下降", "；".join(result["反面证据"]))

    def test_reject_wrong_basis_or_untrusted_percent_format(self):
        record = finance()
        record["指标"]["营业总收入同比"] = "9.2 USD（累计）"
        record["指标"]["归母净利润同比"] = "25 %（累计）"
        result = build_research_brief(stock(), record, None)
        self.assertIn("无可核对指标", result["财务变化线索"])
        self.assertFalse(any("同比下降" in x for x in result["反面证据"]))


if __name__ == "__main__":
    unittest.main()
