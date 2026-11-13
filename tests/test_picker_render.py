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
        with TemporaryDirectory() as directory, patch.object(picker,'__file__',str(Path(directory)/'picker.py')), patch.object(picker,'fetch_market',side_effect=[hk,a]):
            self.assertEqual(picker.main(),0)
            data=json.loads((Path(directory)/'docs/data.json').read_text())
            html=(Path(directory)/'docs/index.html').read_text()
            self.assertEqual(len(data['a_shares']),20)
            self.assertEqual(len(data['hk_shares']),20)
            self.assertIn('腾讯测试源',html)
            self.assertIn('最多30交易分钟延迟',html)
            self.assertIn('行情时效',data['quality']['港股'])

if __name__=='__main__': unittest.main()
