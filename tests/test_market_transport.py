"""Offline request-budget regressions; these never prove upstream recovery."""
import unittest
from unittest.mock import Mock, patch
import requests
import picker
from market_transport import EastmoneyBudget, ProviderCooldown
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
        response = Mock(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError("503")
        get = Mock(return_value=response)
        budget = EastmoneyBudget(get, max_failures=2)
        with self.assertRaises(requests.HTTPError):
            budget("url")
        for _ in range(2):
            with self.assertRaises(ProviderCooldown):
                budget("url")
        self.assertEqual(get.call_count, 2)

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


if __name__ == "__main__":
    unittest.main()
