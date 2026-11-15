"""Synthetic offline render fixture, not a real published market observation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import picker
from market_quality import validate_market_data
from test_market_quality import quotes


class PickerRenderTests(unittest.TestCase):
    def test_complete_two_market_render_keeps_source_and_delay_notice(self):
        a,hk=quotes(3500),quotes(1500)
        a.attrs['quality']=validate_market_data(a,'A股','腾讯测试源')
        hk.attrs['quality']=validate_market_data(hk,'港股','东财原始字段/delay')
        hk.attrs['quality']['行情时效']='港股官方延迟源，最多30交易分钟延迟'
        with TemporaryDirectory() as directory, patch.object(picker,'__file__',str(Path(directory)/'picker.py')), patch.object(picker,'fetch_market',side_effect=[a,hk]):
            self.assertEqual(picker.main(),0)
            data=json.loads((Path(directory)/'docs/data.json').read_text())
            html=(Path(directory)/'docs/index.html').read_text()
            self.assertEqual(len(data['a_shares']),20)
            self.assertEqual(len(data['hk_shares']),20)
            self.assertIn('腾讯测试源',html)
            self.assertIn('最多30交易分钟延迟',html)
            self.assertIn('行情时效',data['quality']['港股'])
            self.assertNotIn('href="./ai.html"', html)
            self.assertIn('<p class="ai-nav">', html)

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
