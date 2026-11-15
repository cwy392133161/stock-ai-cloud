import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pandas as pd
import picker
from market_freshness import validate_delayed_hk_times, validate_eastmoney_a_times
from market_quality import MarketDataQualityError
from test_market_quality import quotes

CST=timezone(timedelta(hours=8))
NOW=datetime(2026,11,10,16,45,tzinfo=CST)

def frame(stamp=None):
    df=quotes(1500)
    stamp=stamp or NOW.replace(hour=16,minute=5)
    df['f124']=stamp.timestamp()
    return df


class AShareFreshnessTests(unittest.TestCase):
    def test_missing_stale_or_future_quotes_cannot_be_used_after_close(self):
        df = quotes()
        with self.assertRaisesRegex(MarketDataQualityError, "f124"):
            validate_eastmoney_a_times(df.drop(columns=["f124"]), NOW)
        for stamp in (NOW - timedelta(days=1), NOW.replace(hour=14, minute=59),
                      NOW + timedelta(seconds=61)):
            with self.subTest(stamp=stamp), self.assertRaisesRegex(MarketDataQualityError, "95%"):
                validate_eastmoney_a_times(df.assign(f124=stamp.timestamp()), NOW)

    def test_market_passes_with_95_percent_fresh_but_only_fresh_securities_rank(self):
        df = quotes()
        df["f124"] = NOW.replace(hour=15, minute=0).timestamp()
        df.loc[:174, "f124"] = (NOW - timedelta(days=1)).timestamp()
        result = validate_eastmoney_a_times(df, NOW.astimezone(timezone.utc))
        self.assertEqual(result.attrs["freshness"]["新鲜有成交证券数"], 3325)
        self.assertEqual(result.attrs["freshness"]["报价日期"], "2026-11-10")
        self.assertTrue(result.loc[:174, "最新价"].isna().all())
        self.assertEqual(len(picker.score_a(result)), 3325)
        df.loc[175, "f124"] = float("nan")
        with self.assertRaisesRegex(MarketDataQualityError, "95%"):
            validate_eastmoney_a_times(df, NOW)

    def test_picker_moves_to_independent_provider_when_eastmoney_is_stale(self):
        stale = quotes().assign(f124=0)
        with patch.object(picker, "fetch_em_clist", return_value=stale), \
             patch.object(picker, "fetch_tencent_a", return_value=quotes()) as tencent:
            result = picker.fetch_a_market("A股", ["82"], "")
        tencent.assert_called_once()
        self.assertIn("腾讯", result.attrs["quality"]["行情来源"])

    def test_picker_keeps_eastmoney_timestamp_diagnostics(self):
        df = quotes().assign(f124=NOW.replace(hour=15, minute=0).timestamp())
        with patch("market_freshness.datetime") as clock, \
             patch.object(picker, "fetch_em_clist", return_value=df):
            clock.now.return_value = NOW
            result = picker.fetch_a_market("A股", ["82"], "")
        self.assertEqual(result.attrs["quality"]["新鲜有成交证券数"], 3500)
        self.assertIn("15:00", result.attrs["quality"]["行情时效"])


class HKFreshnessTests(unittest.TestCase):
    def test_same_day_close_quotes_pass(self):
        df=validate_delayed_hk_times(frame(),NOW)
        self.assertEqual(df.attrs['freshness']['新鲜有成交证券数'],1500)

    def test_same_day_opening_previous_date_and_future_fail(self):
        for stamp in (NOW.replace(hour=9),NOW-timedelta(days=1),NOW+timedelta(minutes=3)):
            with self.subTest(stamp=stamp), self.assertRaises(MarketDataQualityError):
                validate_delayed_hk_times(frame(stamp),NOW)

    def test_missing_timestamp_fails(self):
        with self.assertRaises(MarketDataQualityError):
            validate_delayed_hk_times(quotes(1500),NOW)

    def test_small_stale_remainder_cannot_rank(self):
        df=frame();df.loc[:9,'f124']=NOW.replace(hour=9).timestamp()
        result=validate_delayed_hk_times(df,NOW)
        self.assertTrue(result.loc[:9,'最新价'].isna().all())
        self.assertEqual(len(picker.score_hk(result)),1490)

    def test_delayed_lunch_and_reopening_quotes(self):
        for hour,minute,quotehour,quoteminute in ((12,30,11,55),(13,10,12,0),(10,0,9,45)):
            now=NOW.replace(hour=hour,minute=minute)
            result=validate_delayed_hk_times(frame(now.replace(hour=quotehour,minute=quoteminute)),now)
            self.assertEqual(result.attrs['freshness']['新鲜有成交证券数'],1500)

    def test_delay_host_uses_official_redirect_destination(self):
        response=type('Response',(),{'raise_for_status':lambda self:None,
            'json':lambda self:{'data':{'total':1,'diff':[{'f12':'09988','f2':10,'f124':1794275225}]}}})()
        with patch.object(picker.requests,'get',return_value=response) as get:
            frame=picker.fetch_em_clist('delay','','HK')
        self.assertEqual(frame['f124'].iloc[0],1794275225)
        self.assertEqual(get.call_args.args[0],'https://push2delay.eastmoney.com/api/qt/clist/get')
        self.assertIn('f124',get.call_args.kwargs['params']['fields'])

    def test_all_hk_hosts_require_timestamp_not_only_delay(self):
        with patch.object(picker,'fetch_em_clist',return_value=quotes(1500).drop(columns=['f124'])), patch.object(picker.time,'sleep'):
            with self.assertRaisesRegex(RuntimeError,'f124'):
                picker.fetch_em_market('港股',['72'],'','HK')

    def test_fetch_market_preserves_delay_freshness_diagnostics(self):
        with patch.object(picker,'fetch_em_clist',return_value=quotes(1500)):
            result=picker.fetch_market('港股',['delay'],'','HK',None,None)
        self.assertIn('行情时效',result.attrs['quality'])
        self.assertIn('30',result.attrs['quality']['行情时效'])

    def test_delay_prefers_bounded_fresh_connection_page_transport(self):
        with patch.object(picker,'ak',object()), patch.object(picker,'fetch_em_clist',return_value=quotes(1500)) as fetch:
            picker.fetch_em_market('港股',['delay'],'','HK')
        self.assertIs(fetch.call_args.kwargs['get'],picker._ak_em_get)

    def test_old_same_day_illiquid_quotes_do_not_disqualify_fresh_candidates(self):
        df=frame()
        df.loc[:999,'成交额']=1000
        df.loc[:999,'f124']=NOW.replace(hour=9).timestamp()
        result=validate_delayed_hk_times(df,NOW)
        self.assertEqual(result.attrs['freshness']['可入榜证券数'],500)
        self.assertEqual(len(picker.score_hk(result)),500)

    def test_previous_day_illiquid_active_quotes_still_reject_snapshot(self):
        df=frame()
        df.loc[:999,'成交额']=1000
        df.loc[:999,'f124']=(NOW-timedelta(days=1)).timestamp()
        with self.assertRaisesRegex(MarketDataQualityError,'当日报价'):
            validate_delayed_hk_times(df,NOW)

if __name__=='__main__': unittest.main()
