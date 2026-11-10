import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch
import picker
from test_market_quality import quotes
import tencent_market as tx

NOW = datetime(2026, 11, 10, 16, tzinfo=timezone(timedelta(hours=8)))


def quote(code, date="20261110150000"):
    fields = ["0"] * 80
    for index, value in {1:"测试",2:code[2:],3:"10",30:date,32:"2",36:"100000",
                         38:"1.5",39:"99",43:"3",44:"40",45:"50",46:"2",
                         49:"1.2",52:"12",57:"10000"}.items():
        fields[index] = value
    return 'v_' + code + '="' + "~".join(fields) + '";'


class TencentTests(unittest.TestCase):
    def test_raw_units_and_dynamic_pe_not_ttm(self):
        row = tx.parse_quotes(quote("sz000001"), ["sz000001"], NOW)["sz000001"]
        self.assertEqual(row["流通市值"], 4e9)
        self.assertEqual(row["成交额"], 1e8)
        self.assertEqual(row["市盈率-动态"], 12)
        self.assertEqual(row["量比"], 1.2)

    def test_stale_quotes_are_not_relabelled(self):
        row = tx.parse_quotes(quote("sz000001","20261109150000"),
                              ["sz000001"], NOW)["sz000001"]
        self.assertFalse(row["报价当日"])
        self.assertNotEqual(row["最新价"], row["最新价"])

    def test_missing_duplicate_or_unexpected_quote_rejected(self):
        for text in ["", quote("sz000002"), quote("sz000001")*2]:
            with self.subTest(text=text[:20]), self.assertRaises(ValueError):
                tx.parse_quotes(text, ["sz000001"], NOW)

    def test_bad_timestamp_rejected(self):
        with self.assertRaises(ValueError):
            tx.parse_quotes(quote("sz000001","--"), ["sz000001"], NOW)

    def test_same_day_stale_and_future_rejected(self):
        for stamp in ("20261110093000", "20261110170000"):
            row = tx.parse_quotes(quote("sz000001", stamp), ["sz000001"], NOW)["sz000001"]
            self.assertFalse(row["报价当日"])

    def make_get(self, stale=False, duplicate=False, missing=False):
        rank = [{"code":f"sz{i:06d}", "zdf_d60":"15","zdf_y":"20"}
                for i in range(1,3501)]
        if duplicate:
            rank[-1] = rank[0]
        def get(url, **kwargs):
            r = Mock()
            if url == tx.RANK_URL:
                offset = int(kwargs["params"]["offset"])
                page = rank[offset:offset+200]
                if missing and offset == 200:
                    page = page[:-1]
                r.json.return_value = {"code":0,"data":{"total":3500,"rank_list":page}}
            else:
                codes = url.split("q=")[1].split(",")
                r.text = "".join(quote(c, "20261109150000" if stale else "20261110150000")
                                 for c in codes)
            return r
        return get

    def test_full_market_merges_trends_and_passes_unchanged_quality_gate(self):
        frame = tx.fetch_tencent_a(get=self.make_get(),pause=lambda _:None, now=NOW)
        self.assertEqual(len(frame),3500)
        self.assertEqual(frame["60日涨跌幅"].iloc[0],15)
        self.assertEqual(frame["年初至今涨跌幅"].iloc[0],20)
        self.assertEqual(frame.attrs["quality"]["当日报价数"],3500)

    def test_stale_full_market_is_rejected(self):
        with self.assertRaises(ValueError):
            tx.fetch_tencent_a(get=self.make_get(stale=True),pause=lambda _:None,now=NOW)

    def test_duplicate_or_missing_rank_pages_rejected(self):
        for options in [{"duplicate":True},{"missing":True}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                tx.get_rank_rows(self.make_get(**options),pause=lambda _:None)

    def test_second_sweep_recovers_drift_but_requires_exact_total(self):
        original = self.make_get()
        calls = 0
        def get(url, **kwargs):
            nonlocal calls
            calls += 1
            r = original(url, **kwargs)
            if calls == 18:
                d = r.json()
                d["data"]["rank_list"][-1] = {"code":"sz000001"}
                r.json.return_value = d
            return r
        rows = tx.get_rank_rows(get,pause=lambda _:None)
        self.assertEqual(len(rows),3500)
        self.assertEqual(calls,36)

    def test_second_sweep_extra_code_rejected(self):
        original = self.make_get()
        calls = 0
        def get(url, **kwargs):
            nonlocal calls
            calls += 1
            r = original(url, **kwargs);d=r.json()
            if calls == 18:
                d["data"]["rank_list"][-1] = {"code":"sz000001"}
            if calls == 19:
                d["data"]["rank_list"][0] = {"code":"sz999999"}
            r.json.return_value=d
            return r
        with self.assertRaisesRegex(ValueError,"集合"):
            tx.get_rank_rows(get,pause=lambda _:None)

    def test_changed_total_rejected(self):
        original = self.make_get()
        def get(url, **kwargs):
            r = original(url, **kwargs)
            if kwargs["params"]["offset"] == "200":
                d=r.json();d["data"]["total"]=3501;r.json.return_value=d
            return r
        with self.assertRaises(ValueError):
            tx.get_rank_rows(get,pause=lambda _:None)

    def test_intra_page_duplicate_rejected(self):
        original = self.make_get()
        def get(url, **kwargs):
            r = original(url, **kwargs);d=r.json()
            d["data"]["rank_list"][1]=d["data"]["rank_list"][0]
            r.json.return_value=d
            return r
        with self.assertRaisesRegex(ValueError,"单页"):
            tx.get_rank_rows(get,pause=lambda _:None)

    def test_lunch_and_live_freshness_windows(self):
        observed=NOW.replace(hour=12)
        self.assertTrue(tx.quote_is_fresh(observed.replace(hour=11,minute=30),observed))
        self.assertFalse(tx.quote_is_fresh(observed.replace(hour=10),observed))
        observed=NOW.replace(hour=10)
        self.assertFalse(tx.quote_is_fresh(observed.replace(hour=9,minute=30),observed))

    def test_picker_uses_independent_complete_fallback(self):
        with patch.object(picker, "_fetch_a_em_market", side_effect=RuntimeError("429")), \
             patch.object(picker, "fetch_tencent_a", return_value=quotes()):
            frame = picker.fetch_a_market("A股", [], "")
        self.assertEqual(len(frame),3500)
        self.assertIn("腾讯",frame.attrs["quality"]["行情来源"])

    def test_picker_rejects_incomplete_independent_fallback(self):
        with patch.object(picker, "_fetch_a_em_market", side_effect=RuntimeError("429")), \
             patch.object(picker, "fetch_tencent_a", return_value=quotes().drop(columns=["量比"])):
            with self.assertRaisesRegex(RuntimeError,"覆盖不足"):
                picker.fetch_a_market("A股", [], "")

    def test_business_error_not_silent_empty_dataframe(self):
        response = Mock()
        response.json.return_value={"code":1619377101}
        with self.assertRaises(ValueError):
            tx.get_rank_rows(Mock(return_value=response),pause=lambda _:None)


if __name__ == "__main__":
    unittest.main()

