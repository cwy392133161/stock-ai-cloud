from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import trend_audit as audit
import trend_bars as bars


class TrendAuditTests(unittest.TestCase):
    def test_calendar_and_session_returns_do_not_fill_missing_anchors(self):
        end = date(2026, 11, 13)
        dates = [(end - timedelta(days=i)).isoformat() for i in range(62)]
        anchors = audit.factor_anchors(["2025-12-31"] + dates, end.isoformat())
        self.assertEqual(anchors, {"60日涨跌幅": dates[60], "年初至今涨跌幅": "2025-12-31"})
        row = {"代码": "600001", "名称": "样本", "最新价": 11, "60日涨跌幅": 10.1, "年初至今涨跌幅": 0.}
        report = audit.check_row(row, dates[0], anchors, {dates[0]: 11., dates[60]: 10.}, source="sina")
        self.assertEqual(report["factors"]["60日涨跌幅"]["status"], "一致")
        self.assertEqual(report["factors"]["年初至今涨跌幅"]["status"], "缺少基准日复权收盘")
        different = audit.check_row(row, dates[0], anchors, {dates[0]: 11, dates[60]: 9.})
        self.assertEqual(different["factors"]["60日涨跌幅"]["status"], "有差异")
        unaligned = audit.check_row(row, dates[0], anchors, {dates[0]: 11.05, dates[60]: 10.})
        self.assertEqual(unaligned["factors"]["60日涨跌幅"]["status"], "期末收盘未对齐")

    def test_sina_qfq_uses_effective_factor_and_rebases_to_signal_not_later_dividend(self):
        body = ('var sh600001qfq={"total":3,"data":[{"d":"2026-11-20","f":"1"},'
                '{"d":"2026-07-16","f":"1.1"},{"d":"1900-01-01","f":"1.21"}]}\n/* ABCwaf */')
        result = bars.sina_adjust({"2025-12-31": 11., "2026-11-13": 12.}, body, "sh600001", date(2026, 11, 13))
        self.assertAlmostEqual(result["2025-12-31"], 10.)
        self.assertAlmostEqual(result["2026-11-13"], 12.)
        with self.assertRaises(ValueError):
            bars.sina_adjust({}, body, "sz000002", date(2026, 11, 13))

    def test_calendar_outage_yields_an_explicit_audit_without_any_quote_request(self):
        row = {"代码": "600001", "名称": "样本", "最新价": 9.}
        with (patch.object(audit, "trading_sessions", side_effect=bars.ProviderUnavailable()),
              patch.object(bars, "tencent", side_effect=AssertionError("unexpected request"))):
            result = audit.audit([row], "2026-11-13")
        self.assertEqual(result["counts"]["60日涨跌幅"], {"交易日历不可用": 1})
        self.assertEqual(result["calendar_sessions"], [])
        with TemporaryDirectory() as directory:
            audit.write_audit(Path(directory), result, {"legacy": ["600001"]})
            html = (Path(directory)/"trend-audit.html").read_text(encoding="utf8")
            self.assertIn("交易日历不可用", html)
            self.assertIn("原榜第 1 名", html)

    def test_fallback_skips_blocked_sources_and_identifies_daily_close_provider(self):
        row = {"代码": "000001", "最新价": 12}
        with (patch.object(audit, "trading_sessions", return_value=["2025-12-31", "2026-11-13"]),
              patch.object(bars, "tencent", side_effect=AssertionError("skip WAF")),
              patch.object(bars, "eastmoney", side_effect=bars.ProviderUnavailable()),
              patch.object(bars, "sina", return_value={"2026-11-13": 12., "2025-12-31": 10.}),
              patch.object(audit.time, "sleep")):
            result = audit.audit([row], "2026-11-13", skip_sources=["tencent"])
        self.assertEqual(result["rows"][0]["source"], "sina")
        self.assertEqual(result["source_counts"], {"sina": 1})


if __name__ == "__main__":
    unittest.main()
