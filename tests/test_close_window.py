"""Pre-close manual daily runs preserve the old snapshot, not fake an outage."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import market_schedule
import publish_guard

CST=timezone(timedelta(hours=8))

class CloseWindowTests(unittest.TestCase):
    def test_before_at_and_after_beijing_window(self):
        for hour,minute,ready in [(12,45,False),(16,59,False),(17,0,True),(23,59,True)]:
            self.assertEqual(market_schedule.is_market_ready(datetime(2026,11,10,hour,minute,tzinfo=CST)),ready)
        self.assertTrue(market_schedule.is_market_ready(datetime(2026,11,10,9,0,tzinfo=timezone.utc)))
        self.assertFalse(market_schedule.is_market_ready(datetime(2026,11,7,18,tzinfo=CST)))

    def test_missing_timezone_rejected(self):
        with self.assertRaises(ValueError):
            market_schedule.is_market_ready(datetime(2026,11,10,17))

    def test_pre_close_preserves_data_and_reports_waiting_not_failure(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            data={'updated':'2026-11-06 23:25','a_shares':[{'代码':'000001'}]}
            (root/'data.json').write_text(json.dumps(data))
            original=(root/'data.json').read_bytes()
            (root/'index.html').write_text('<h1>旧榜</h1><p class="ai-nav">旧</p>')
            (root/'ai.html').write_text('<h1>旧报告</h1><small>行情快照：2026-11-06 23:25</small>')
            with patch.object(publish_guard,'DOCS',root),patch.dict('os.environ',{
                'REPORT_ONLY':'false','TRADING_WEEKDAY':'true','MARKET_READY':'false',
                'SCREENING_OUTCOME':'skipped','AI_JOB_STATUS':'skipped','AI_ENABLED':'true'}):
                self.assertEqual(publish_guard.main(),0)
            status=json.loads((root/'ai-status.json').read_text())
            self.assertEqual(status['state'],'market-awaiting-close')
            self.assertEqual((root/'data.json').read_bytes(),original)
            self.assertIn('本次未检查行情源是否可用',status['description'])
            self.assertNotIn('这不是行情源故障',status['description'])

    def test_requested_no_ai_is_skipped_not_generation_failure(self):
        status=publish_guard.research_freshness({'updated':'2026-11-10 17:30'},'',True,'skipped',ai_skipped=True)
        self.assertEqual(status['state'],'skipped')
        self.assertIn('按要求跳过',status['description'])

    def test_workflow_requires_close_window_for_screen_and_preserves_outage_failure(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/daily.yml').read_text()
        self.assertEqual(text.count("steps.trading_day.outputs.market_ready == 'true'"),1)
        self.assertIn('MARKET_READY: ${{ steps.trading_day.outputs.market_ready }}',text)
        self.assertIn('if status.get("market_state") == "failed":',text)
        self.assertIn('raise SystemExit(1)',text)

if __name__=='__main__': unittest.main()
