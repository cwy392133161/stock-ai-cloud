"""Synthetic transport fixtures only; never used as market observations."""
import unittest
from unittest.mock import patch, Mock
import pandas as pd
import requests
import picker
from test_market_quality import quotes


def response(rows, total):
    r = Mock()
    r.json.return_value = {'data': {'total': total, 'diff': rows}}
    return r


class RawQuoteTests(unittest.TestCase):
    def test_key_mapping_keeps_all_factors_without_substitution(self):
        row = {'f12': '9988', 'f2': '10.5', 'f3': '0', 'f8': '1.2',
               'f9': '12', 'f10': '1.3', 'f21': '3000000000', 'f23': '2',
               'f24': '-3.1', 'f25': '15', 'f20': '9999999999', 'f115': '99'}
        with patch.object(picker.requests, 'get', return_value=response([row], 1)):
            d = picker.fetch_em_clist('72', picker.EM_FS_HK, 'HK')
        self.assertEqual(d.iloc[0]['代码'], '09988')
        for key in ('f8', 'f9', 'f10', 'f21', 'f23', 'f24', 'f25'):
            self.assertEqual(d.iloc[0][picker.EM_COLS[key]], float(row[key]))

    def test_missing_fields_remain_missing_not_filled_from_other_metrics(self):
        with patch.object(picker.requests, 'get', return_value=response([
            {'f12': '09988', 'f20': 1e10, 'f115': 10, 'f9': '-'}], 1)):
            d = picker.fetch_em_clist('72', '', 'HK')
        self.assertNotIn('流通市值', d)
        self.assertTrue(pd.isna(d.iloc[0]['市盈率-动态']))

    def test_observed_page_size_and_dict_shape(self):
        get = Mock(side_effect=[response({'0': {'f12': '1'}, '1': {'f12': '2'}}, 3),
                                response([{'f12': '3'}], 3)])
        with patch.object(picker.time, 'sleep'):
            d = picker.fetch_em_clist('72', '', 'HK', get=get)
        self.assertEqual(d['代码'].tolist(), ['00001', '00002', '00003'])
        self.assertEqual([c.kwargs['params']['pn'] for c in get.call_args_list], ['1', '2'])

    def test_duplicate_or_changed_total_is_rejected(self):
        for second, total in [([{'f12': '1'}], 2), ([{'f12': '2'}], 3)]:
            with self.subTest(total=total), patch.object(picker.time, 'sleep'):
                get = Mock(side_effect=[response([{'f12': '1'}], 2), response(second, total)])
                with self.assertRaises(ValueError):
                    picker.fetch_em_clist('72', '', 'HK', get=get)

    def test_quality_failure_rotates_server(self):
        good = quotes(1500)
        with patch.object(picker, 'fetch_em_clist', side_effect=[
            good.drop(columns=['流通市值']), good]), patch.object(picker.time, 'sleep'):
            d = picker.fetch_em_market('港股', ['72', '81'], '', 'HK')
        self.assertEqual(d.attrs['quality']['行情来源'], '东财原始字段/81')

    def test_hk_uses_one_budget_and_never_lossy_projection(self):
        good = quotes(1500)
        lossy = Mock(side_effect=AssertionError('must not call projected API'))
        with patch.object(picker, 'ak', object()), patch.object(picker, 'fetch_em_market',
            return_value=good) as fetch:
            d = picker.fetch_market('港股', ['72'], '', 'HK', lossy, lossy)
        self.assertIsInstance(fetch.call_args.kwargs['get'], picker.EastmoneyBudget)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(d), 1500)
        lossy.assert_not_called()

    def test_hk_raw_failure_never_restarts_transport(self):
        projected, sina = Mock(), Mock()
        with patch.object(picker, 'fetch_em_market', side_effect=requests.Timeout()) as fetch:
            with self.assertRaisesRegex(RuntimeError, '不是等价备用源'):
                picker.fetch_market('港股', ['delay', '72'], '', 'HK', projected, sina)
        self.assertEqual(fetch.call_count, 1)
        projected.assert_not_called()
        sina.assert_not_called()
