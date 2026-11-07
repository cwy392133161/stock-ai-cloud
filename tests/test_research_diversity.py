"""Individual research priority and cross-report duplicate suppression."""
import unittest
from unittest.mock import patch

import ai_report
from research_differentiation import focal_question, prose_similarity, duplicate_reports


def financial(rev, profit):
    return {"报告期": "2026-09-30", "状态": "可用",
            "指标": {
                "营业总收入同比": f"{rev} %（累计同比）",
                "归母净利润同比": f"{profit} %（累计同比）",
                "加权 ROE": "7.0 %（报告期）",
            }}


class DiversityTests(unittest.TestCase):
    def test_focus_uses_real_company_sign_divergence(self):
        stock={"代码": "000690", "风险提示": "短期波动"}
        self.assertEqual(focal_question(stock, financial(12, -4))["研究主题"], "增收不增利")
        self.assertEqual(focal_question(stock, financial(-12, 4))["研究主题"], "收入与利润方向分化")
        self.assertEqual(focal_question(stock, financial(-12, -4))["研究主题"], "双指标承压")
        self.assertEqual(focal_question(stock, financial(12, 4))["研究主题"], "增长质量与可持续性")

    def test_near_identical_wording_across_codes_is_rejected(self):
        paragraphs={
            "入选逻辑": "市场短期关注很强，走势与业绩增长存在明显不一致，不能单凭价格说明经营改善。",
            "经营和估值": "本报告的经营资料尚未反映稳定现金转化，估值差异仍需要进一步同行质量验证。",
            "反面证据": "经营指标的当前证据仍不足以认定竞争优势，未来公告可能改变此前研究判断。",
            "跟踪条件": "应观察下一份报告是否披露真实利润持续性和现金回收变化，并重新评估结论。",
        }
        indices = duplicate_reports([
            ("A股","000690",paragraphs),("港股","00585",paragraphs),
            ("A股","000001",{**paragraphs,"经营和估值":"现金流出现新的已披露证据，仍需结合营收核对。"}),
        ])
        self.assertIn(1, indices)
        self.assertGreaterEqual(prose_similarity("相同的研究内容", "相同的研究内容"), 0.99)
        self.assertLess(prose_similarity("营收变动", "负债结构"), 0.1)

    def test_three_cloned_sections_rejected_even_if_one_is_changed(self):
        common = {
            "入选逻辑": "市场近期关注度提高，但这不能说明企业未来经营会改善，后续还须取得可靠财务证据。",
            "经营和估值": "当前估值样本不能调整业务模式差异，因此需要关注利润质量与经营现金流表现。",
            "反面证据": "如果同报告期财务指标发生变化，当前研究假设可能被反证，不能仅凭走势得出结论。",
            "跟踪条件": "核对下一期收入同比和利润绝对额，验证是否存在真实主营业务变化。",
        }
        changed = {**common, "跟踪条件": "请在新一期财报中重新核对官方PDF来源、披露日期与币种，并取得新的现金流量表。"}
        blocked = duplicate_reports([
            ("A股", "000690", common), ("A股", "000001", changed),
        ], threshold=0.995)
        self.assertEqual(blocked[1]["重复章节数"], 3)

    def test_render_page_downgrades_second_duplicated_report(self):
        sections = {"入选逻辑":"相同的论证内容支持进一步观察",
                    "经营和估值":"同一组经营证据需要在下期再次验证",
                    "反面证据":"数据不足以证明公司的竞争优势",
                    "跟踪条件":"继续观察新财务披露的真实变化"}
        approved = {"状态":"解释性研究已通过基础发布门槛", "章节":sections,
                    "直接证据主张":[], "原文短引":[],"原因":[]}
        stocks=[
            ("A股", {"代码":"000690","名称":"A公司"}, "{}", {"状态":"接口无记录"}),
            ("港股", {"代码":"00585","名称":"B公司"}, "{}", {"状态":"接口无记录"}),
        ]
        with patch.object(ai_report, "assess_model_research", return_value=approved):
            html=ai_report.render_report({"updated":"2026-11-06 23:25"}, stocks,
                                         "2026-11-07")
        self.assertIn('name="research-approved-count" content="1"', html)
        self.assertIn("与另一家公司研究正文高度重复", html)


if __name__ == "__main__":
    unittest.main()
