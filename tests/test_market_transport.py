"""Offline request-budget regressions; these never prove upstream recovery."""
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import requests
import picker
from market_transport import CollectionDeadline, EastmoneyBudget, ProviderCooldown
from test_market_quality import quotes


class BudgetTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(picker, "fetch_tencent_a", side_effect=RuntimeError("offline source unavailable"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_429_stops_all_followup_requests(self):
        response = Mock(status_code=429)
        get = Mock(return_value=response)
        budget = EastmoneyBudget(get)
        for _ in range(2):
            with self.assertRaises(ProviderCooldown):
                budget("https://example.test")
        self.assertEqual(get.call_count, 1)

    def test_disconnect_stops_same_provider_rotation(self):
        get = Mock(side_effect=requests.ConnectionError(
            "Remote end closed connection without response"))
        with patch.object(picker, "ak", None), patch.object(
            picker.requests, "get", get), patch.object(picker.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "本轮东财请求 1 次"):
                picker.fetch_market("A股", ["82", "7", "28", ""], "", "A",
                                    Mock(), Mock())
        self.assertEqual(get.call_count, 1)

    def test_http_failure_budget_is_shared(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("500")
        get = Mock(return_value=response)
        budget = EastmoneyBudget(get, max_failures=2)
        with self.assertRaises(requests.HTTPError):
            budget("url")
        for _ in range(2):
            with self.assertRaises(ProviderCooldown):
                budget("url")
        self.assertEqual(get.call_count, 2)

    def test_502_503_504_and_access_denials_stop_even_if_transport_raises(self):
        for status in (401, 403, 429, 502, 503, 504):
            for raised in (False, True):
                with self.subTest(status=status, raised=raised):
                    response = requests.Response()
                    response.status_code = status
                    get = Mock(return_value=response)
                    if raised:
                        get.side_effect = requests.HTTPError("unsafe response", response=response)
                    budget = EastmoneyBudget(get)
                    with self.assertRaisesRegex(ProviderCooldown, f"HTTP {status}"):
                        budget("url")
                    with self.assertRaises(ProviderCooldown):
                        budget("another-host")
                    get.assert_called_once()

    def test_gateway_failure_switches_once_without_same_source_wait(self):
        response = Mock(status_code=503)
        with patch.object(picker, "ak", None), patch.object(
                picker.requests, "get", return_value=response) as get, patch.object(
                picker.time, "sleep") as sleep, patch.object(
                picker, "fetch_tencent_a", return_value=quotes()) as fallback:
            result = picker.fetch_a_market("A股", ["82", "7", "28", ""], "")
        self.assertEqual(len(result), 3500)
        get.assert_called_once()
        sleep.assert_not_called()
        fallback.assert_called_once()

    def test_gateway_failure_during_pagination_stops_and_discards_partial_data(self):
        first = Mock(status_code=200)
        first.json.return_value = {"data": {"total": 3, "diff": [{"f12": "000001"}]}}
        gateway = Mock(status_code=502)
        with patch.object(picker, "ak", None), patch.object(
                picker.requests, "get", side_effect=[first, gateway]) as get, patch.object(
                picker.time, "sleep") as sleep, patch.object(picker, "_page_delay", return_value=1.5), patch.object(
                picker, "fetch_tencent_a", return_value=quotes()) as fallback:
            result = picker.fetch_a_market("A股", ["82", "7"], "")
        self.assertEqual(len(result), 3500)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["params"]["pn"], "2")
        sleep.assert_called_once_with(1.5)
        fallback.assert_called_once()

    def test_eastmoney_bounds_request_and_stops_before_exhausted_wait(self):
        clock = Mock(side_effect=[0, 297, 298, 299, 300])
        deadline = CollectionDeadline("东财", 300, clock=clock)
        get = Mock(return_value=Mock(status_code=200))
        budget = EastmoneyBudget(get, deadline=deadline)
        budget("page", timeout=20)
        self.assertEqual(get.call_args.kwargs["timeout"], 3)
        sleep = Mock()
        with self.assertRaisesRegex(ProviderCooldown, "剩余"):
            budget.wait(sleep, 1)
        sleep.assert_not_called()
        with self.assertRaisesRegex(ProviderCooldown, "上限"):
            budget("page2", timeout=20)
        get.assert_called_once()

    def test_request_budget_includes_successful_pages(self):
        get = Mock(return_value=Mock(status_code=200))
        budget = EastmoneyBudget(get, max_requests=2)
        budget("page1")
        budget("page2")
        with self.assertRaises(ProviderCooldown):
            budget("page3")
        self.assertEqual(get.call_count, 2)

    def test_single_transport_has_no_hidden_retries(self):
        with patch("akshare.utils.http.request_with_retry") as request:
            picker._ak_em_single_get("url", timeout=20)
        self.assertEqual(request.call_args.kwargs["max_retries"], 1)
        self.assertTrue(request.call_args.kwargs["fresh_session"])

    def test_a_share_never_calls_incomplete_sina_or_same_source_projection(self):
        primary, sina = Mock(), Mock()
        with patch.object(picker, "fetch_em_market", return_value=quotes()):
            result = picker.fetch_market("A股", [], "", "A", primary, sina)
        self.assertEqual(len(result), 3500)
        primary.assert_not_called()
        sina.assert_not_called()

    def test_a_share_still_rejects_missing_factors(self):
        with patch.object(picker, "fetch_em_market",
                          return_value=quotes().drop(columns=["流通市值"])):
            with self.assertRaisesRegex(RuntimeError, "覆盖不足"):
                picker.fetch_a_market("A股", [], "")

    def test_quality_failure_can_try_another_host(self):
        good = quotes()
        bad = good.drop(columns=["流通市值"])
        with patch.object(picker, "fetch_em_clist", side_effect=[bad, good]) as fetch, \
             patch.object(picker.time, "sleep"):
            result = picker.fetch_a_market("A股", ["82", "7"], "")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(result), 3500)

    def test_wrong_quote_day_or_midnight_collection_stops_before_snapshot_write(self):
        start = datetime(2026, 11, 23, 23, 59, tzinfo=picker.CST)
        for end, day in ((start + timedelta(minutes=2), "2026-11-24"),
                         (start, "2026-11-22")):
            frame = quotes()
            frame.attrs["quality"] = {"报价日期": day}
            with self.subTest(end=end,day=day), patch.object(
                    picker, "datetime") as clock, patch.object(
                    picker, "fetch_a_market", return_value=frame), patch.object(
                    picker, "build_a_snapshot") as build:
                clock.now.side_effect = [start, end]
                with self.assertRaisesRegex(ValueError, "日期不一致"):
                    picker.main()
                build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
