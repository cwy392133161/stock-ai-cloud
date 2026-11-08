"""As-of quality checks, semantic-evidence limitation and publication gate."""
import json
import unittest

from ai_report import assess_model_research, review_narrative
from evidence_guard import decode_report, validate_source_quote
from publish_guard import research_freshness, update_index
from research_brief import build_research_brief
from research_quality import evidence_quality
from peer_valuation import valuation_context


def source():
    return {
        "来源": "巨潮资讯", "状态": "已取得公告索引",
        "查询期间": "2026-10-01 至 2026-11-06",
        "对应报告期财报": {"报告期": "2026-09-30",
                              "状态": "完整财报标题与报告期匹配，PDF正文待核验"},
        "公告": [{
            "编号": "D1", "日期": "2026-10-28",
            "标题": "2026年第三季度报告",
            "报告期匹配": "2026-09-30",
            "链接": "https://static.cninfo.com.cn/final.pdf",
            "正文摘录": [{
                "页": 3,
                "摘录": "公司财务费用增加主要系银行借款利息不再资本化，后续仍需持续关注融资成本变化。",
                "单位": "未定位", "表头": "未定位",
                "期间上下文": "2026年第三季度",
            }],
        }],
    }


def financial():
    return {
        "状态": "可用", "报告期": "2026-09-30",
        "披露日期": "2026-10-28",
        "指标": {"营业总收入同比": "-12.5 %（累计同比）",
                 "归母净利润同比": "-3.2 %（累计同比）"},
    }


def model():
    return {
        "sections": {
            "入选逻辑": "规则筛选显示短期市场关注度较高，但这只能说明报价和成交相对于其他股票的表现，不能说明公司已经建立长期经营优势或盈利改善。",
            "经营和估值": "现有财务数据显示的变化需要放进相同期间核验，也需要考虑产业与业务周期。行业可比较样本缺失时，不能仅靠单一市场倍数证明相对价值。",
            "反面证据": "交易热度可能领先或背离经营表现。若后续的官方披露无法支持现有故事，或估值比较样本的行业口径不一致，当前逻辑就缺少独立证据。",
            "跟踪条件": "观察下一期官方财报是否提供更充分的经营解释，并核验同一指标的前后年度口径。若新披露与当前分析假设不符，应重新审视当前判断。",
        },
        "source_quotes": [{
            "citation": "D1 p3",
            "quote": "公司财务费用增加主要系银行借款利息不再资本化",
        }],
        "source_claims": [{
            "citation": "D1 p3", "type": "经营原因",
            "quote": "公司财务费用增加主要系银行借款利息不再资本化",
            "claim": "公司财务费用增加主要系银行借款利息不再资本化",
        }],
    }


class EvidenceGateTests(unittest.TestCase):
    def test_missing_snapshot_date_never_falls_back_to_fiscal_date(self):
        f = financial()
        q = evidence_quality(f, source(), valuation_context({}), "")
        self.assertNotIn("行情快照日期已确认", q["通过项"])
        self.assertEqual(q["分级"], "资料不足")
        brief = build_research_brief({}, f, source())
        self.assertEqual(brief["研究证据质量"]["分级"], "资料不足")

    def test_future_publication_and_future_notice_are_rejected(self):
        f = financial()
        f["披露日期"] = "2026-11-07"
        q = evidence_quality(f, source(), valuation_context({}), "2026-11-06")
        self.assertIn("财务与公告未晚于行情快照", q["缺口"])
        f["披露日期"] = "2026-10-28"
        e = source()
        e["公告"][0]["日期"] = "2026-11-08"
        q = evidence_quality(f, e, valuation_context({}), "2026-11-06")
        self.assertIn("财务与公告未晚于行情快照", q["缺口"])

    def test_quoted_pdf_text_must_match_exact_cited_page(self):
        good = model()["source_quotes"][0]
        self.assertIsNotNone(validate_source_quote(good, source()["公告"]))
        self.assertIsNone(validate_source_quote(
            {**good, "quote": "凭空编造的经营事件"}, source()["公告"]))
        self.assertIsNone(validate_source_quote(
            {**good, "citation": "D1 p8"}, source()["公告"]))
        self.assertIsNone(validate_source_quote(
            {**good, "citation": "D2 p3"}, source()["公告"]))

    def test_numeric_excerpt_without_table_basis_is_not_verified(self):
        e = source()
        e["公告"][0]["正文摘录"][0]["摘录"] = "营业收入 3000 万元"
        self.assertIsNone(validate_source_quote(
            {"citation": "D1 p3", "quote": "营业收入 3000 万元"},
            e["公告"]))

    def test_approved_json_must_have_all_four_sections_and_valid_quote(self):
        e = source()
        f = financial()
        brief = build_research_brief({"代码":"000690"}, f, e, "2026-11-06 23:25")
        accepted = assess_model_research(json.dumps(model(), ensure_ascii=False),
                                          brief, e["公告"])
        self.assertEqual(accepted["状态"], "解释性研究已通过基础发布门槛", accepted)
        self.assertEqual(len(accepted["原文短引"]), 1)
        self.assertEqual(len(accepted["直接证据主张"]), 1)
        corrupted = model()
        corrupted["source_claims"][0]["claim"] = "公司财务费用减少主要系借款利息下降"
        corrupted["source_quotes"][0]["quote"] = "不存在于原文的利好"
        rejected = assess_model_research(json.dumps(corrupted, ensure_ascii=False),
                                          brief, e["公告"])
        # A fabricated source quote must never appear as a verified claim.
        # Independent conditional analysis may still be displayed with a
        # lower grade, provided it contains no unverified factual claims.
        self.assertEqual(rejected["状态"], "条件性研究（原文事实证据不足）")
        self.assertFalse(rejected["直接证据主张"])
        self.assertTrue(rejected["章节"])

    def test_missing_pdf_claims_preserve_safe_conditional_thinking(self):
        from ai_report import review_research_hypotheses
        e, f = source(), financial()
        brief = build_research_brief({"代码": "000690"}, f, e, "2026-11-06 23:25")
        raw = model()
        raw["source_claims"] = []
        raw["source_quotes"] = []
        raw["sections"]["反面证据"] += "公司核心产品市场份额持续提升。"
        result = assess_model_research(json.dumps(raw, ensure_ascii=False),
                                       brief, e["公告"])
        self.assertEqual(result["状态"], "条件性研究（原文事实证据不足）")
        self.assertFalse(result["直接证据主张"])
        self.assertNotIn("市场份额持续提升", result["章节"]["反面证据"])
        conditional, removed = review_research_hypotheses(
            "下期应核验归母净利润是否改善。公司已实现利润翻番。")
        self.assertIn("应核验", conditional)
        self.assertNotIn("已实现利润翻番", conditional)
        self.assertGreaterEqual(removed, 1)

    def test_conditional_research_still_requires_valid_snapshot_financials(self):
        e, f = source(), financial()
        raw = model()
        raw["source_claims"] = []
        f["披露日期"] = "2026-11-09"
        brief = build_research_brief({"代码": "000690"}, f, e, "2026-11-06")
        result = assess_model_research(json.dumps(raw, ensure_ascii=False),
                                       brief, e["公告"])
        self.assertEqual(result["状态"], "仅发布可核对事实")
        self.assertFalse(result["章节"])

    def test_unsupported_operating_achievements_filtered_without_numbers(self):
        for claim in (
            "公司核心产品市场份额持续上升。",
            "目前在手订单显著增长。",
            "企业产能已大幅提升。",
            "核心产品销量快速增长。",
        ):
            with self.subTest(claim=claim):
                self.assertFalse(review_narrative(claim))
        caveat = "当前指标不能证明公司核心产品市场份额持续上升。"
        self.assertIn("不能证明", review_narrative(caveat))

    def test_plain_legacy_text_is_never_treated_as_audited_research(self):
        f = financial()
        e = source()
        brief = build_research_brief({"代码":"000690"}, f, e, "2026-11-06")
        self.assertIsNone(decode_report("公司获利明显增加！"))
        status = assess_model_research("公司获利明显增加！", brief, e["公告"])
        self.assertEqual(status["状态"], "仅发布可核对事实")

    def test_publication_distinguishes_stale_factual_and_partial(self):
        index = '<h1>榜单</h1><p class="ai-nav">旧AI链接</p>'
        report = ('<small>行情快照：2026-11-06 23:25</small>'
                  '<meta name="research-approved-count" content="3">'
                  '<meta name="research-total-count" content="10">')
        snapshot = {"updated": "2026-11-06 23:25"}
        partial = research_freshness(snapshot, report, True, "success")
        self.assertEqual(partial["state"], "partial")
        first = update_index(index, partial, True)
        self.assertIn('data-report-state="partial"', first)
        # report_only can run repeatedly without picker rebuilding index.html
        self.assertEqual(update_index(first, partial, True), first)
        stale = research_freshness(
            {"updated":"2026-11-07 23:25"}, report, True, "success")
        self.assertEqual(stale["state"], "stale")
        failed = research_freshness(snapshot, report, True, "failure")
        self.assertEqual(failed["state"], "failed")
        factual = research_freshness(
            snapshot, report.replace('count" content="3"', 'count" content="0"'),
            True, "success")
        self.assertEqual(factual["state"], "facts-only")


if __name__ == "__main__":
    unittest.main()
