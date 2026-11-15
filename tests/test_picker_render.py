"""Synthetic offline render fixture, not a real published market observation."""
import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import picker
import publish_guard
from market_quality import validate_market_data
from test_market_quality import quotes


class PickerRenderTests(unittest.TestCase):
    def test_daily_screen_uses_only_a_shares_and_keeps_history_navigation(self):
        a = quotes(3500)
        close = datetime(2026, 11, 13, 16, 15, tzinfo=picker.CST)
        a['f124'] = int(close.timestamp())
        a.attrs['quality']=validate_market_data(a,'A股','腾讯测试源')
        a.attrs['quality']['报价日期'] = close.date().isoformat()
        a.attrs['quality']['行情时效'] = 'A 股收盘报价已核对'
        with (TemporaryDirectory() as directory,
              patch.object(picker, 'datetime', wraps=datetime) as clock,
              patch.object(picker, '__file__', str(Path(directory)/'picker.py')),
              patch.object(picker, 'fetch_a_market', return_value=a) as fetch_a,
              patch.object(picker, 'fetch_market', side_effect=AssertionError('意外查询其他市场')),
              patch.object(picker, 'score_hk', side_effect=AssertionError('意外计算港股评分'))):
            clock.now.return_value = close
            self.assertEqual(picker.main(), 0)
            fetch_a.assert_called_once_with("A股", picker.EM_HOSTS_A, picker.EM_FS_A)
            data=json.loads((Path(directory)/'docs/data.json').read_text(encoding='utf-8'))
            html=(Path(directory)/'docs/index.html').read_text(encoding='utf-8')
            self.assertEqual(len(data['a_shares']),20)
            self.assertNotIn('hk_shares', data)
            self.assertEqual(set(data['quality']), {'A股'})
            self.assertEqual(html.count('<table class="comparison">'), 4)
            self.assertTrue((Path(directory)/'docs/history/2026-11-13/a-snapshot.json.gz').is_file())
            self.assertNotIn("港股", html)
            self.assertIn('腾讯测试源',html)
            self.assertIn('A 股收盘报价已核对', html)
            self.assertNotIn('href="./ai.html"', html)
            self.assertIn('<p class="ai-nav">', html)
            self.assertEqual(html.count('href="./history/"'), 1)
            status = publish_guard.research_freshness(data, "", False, "skipped")
            for has_report in (False, True):
                with self.subTest(has_report=has_report):
                    guarded = publish_guard.update_index(html, status, has_report)
                    self.assertEqual(guarded.count('href="./history/"'), 1)

    def test_provider_text_is_escaped_in_ranking(self):
        row = {"名称": "<img src=x onerror=alert(1)>", "代码": "A&B", "最新价": 4.0,
               "涨跌幅": 1.0, "评分": 75.0, "入选原因": "A < B", "风险提示": "B > A"}
        html = picker.table_html("A股", [row], "A")
        self.assertIn("&lt;img", html)
        self.assertNotIn("<img", html)
        self.assertIn("A&amp;B", html)
        self.assertIn("A &lt; B", html)
        self.assertIn("B &gt; A", html)

if __name__=='__main__': unittest.main()
