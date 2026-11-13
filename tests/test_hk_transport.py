"""Offline HK outage regressions; synthetic fixtures do not prove live recovery."""
import unittest
from unittest.mock import Mock, patch
import requests
import picker
from market_transport import EastmoneyBudget, ProviderCooldown
from test_market_quality import quotes


class HKTransportTests(unittest.TestCase):
    def test_hk_429_stops_all_hosts_and_legacy_fallbacks(self):
        get = Mock(return_value=Mock(status_code=429))
        legacy = Mock()
        with patch.object(picker, "ak", None), patch.object(picker.requests, "get", get), \
             patch.object(picker.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "累计请求 1 次"):
                picker.fetch_market("港股", picker.EM_HOSTS_HK, "", "HK", legacy, legacy)
        self.assertEqual(get.call_count, 1)
        legacy.assert_not_called()

    def test_hk_502_then_disconnect_stops_without_second_host_sweep(self):
        response = Mock(status_code=502)
        response.raise_for_status.side_effect = requests.HTTPError("502 Bad Gateway")
        get = Mock(side_effect=[response, requests.ConnectionError(
            "Remote end closed connection without response")])
        with patch.object(picker, "ak", None), patch.object(picker.requests, "get", get), \
             patch.object(picker.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "累计请求 2 次"):
                picker.fetch_market("港股", picker.EM_HOSTS_HK, "", "HK", Mock(), Mock())
        self.assertEqual(get.call_count, 2)
        self.assertNotIn(120, [call.args[0] for call in sleep.call_args_list])

    def test_transport_failure_budget_cannot_reset_on_new_market(self):
        get = Mock(return_value=Mock(status_code=429))
        budget = EastmoneyBudget(get)
        with self.assertRaises(RuntimeError):
            picker.fetch_market("港股", ["delay"], "", "HK", em_budget=budget)
        with patch.object(picker, "fetch_tencent_a", return_value=quotes()) as tencent:
            result = picker.fetch_market("A股", ["82"], "", "A", em_budget=budget)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(result), 3500)
        tencent.assert_called_once()

    def test_standalone_delayed_probe_also_has_no_hidden_retry(self):
        with patch.object(picker, "_ak_em_single_get", side_effect=requests.ConnectionError(
                "Remote end closed connection without response")) as get, \
             patch.object(picker, "ak", object()):
            with self.assertRaises(ProviderCooldown):
                picker.fetch_em_market("港股", ["delay"], "", "HK")
        self.assertEqual(get.call_count, 1)

    def test_fetch_order_and_shared_budget(self):
        hk, a = quotes(1500), quotes()
        with patch.object(picker, "fetch_market", side_effect=[hk, a]) as fetch:
            result_a, result_hk = picker.fetch_markets()
        calls = fetch.call_args_list
        self.assertEqual([call.args[3] for call in calls], ["HK", "A"])
        self.assertIs(calls[0].kwargs["em_budget"], calls[1].kwargs["em_budget"])
        self.assertIs(result_a, a)
        self.assertIs(result_hk, hk)

    def test_failed_hk_stops_before_spending_a_share_requests(self):
        with patch.object(picker, "fetch_market", side_effect=RuntimeError("HK unavailable")) as fetch:
            with self.assertRaises(RuntimeError):
                picker.fetch_markets()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.args[3], "HK")

    def test_transient_page_retry_consumes_shared_budget(self):
        good = Mock(status_code=200)
        get = Mock(side_effect=[requests.Timeout(), good])
        budget = EastmoneyBudget(get)
        self.assertIs(budget.request_page("page", pause=lambda _: None), good)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(budget.failures, 1)

    def test_gateway_page_retry_is_bounded_and_keeps_same_parameters(self):
        failure = requests.Response()
        failure.status_code = 502
        good = Mock(status_code=200)
        get = Mock(side_effect=[failure, good])
        budget = EastmoneyBudget(get)
        budget.request_page("page", pause=lambda _: None, params={"pn": "23"})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0], get.call_args_list[1])

    def test_page_retry_never_retries_limit_disconnect_or_403(self):
        for status in (429, 401, 403):
            response = requests.Response()
            response.status_code = status
            get = Mock(return_value=response)
            with self.assertRaises((ProviderCooldown, requests.HTTPError)):
                EastmoneyBudget(get).request_page("page", pause=lambda _: None)
            self.assertEqual(get.call_count, 1)
        get = Mock(side_effect=requests.ConnectionError(
            "Remote end closed connection without response"))
        with self.assertRaises(ProviderCooldown):
            EastmoneyBudget(get).request_page("page", pause=lambda _: None)
        self.assertEqual(get.call_count, 1)

    def test_page_retry_cannot_exceed_failure_or_request_budget(self):
        for limits in ({"max_requests": 1}, {"max_failures": 1}):
            get = Mock(side_effect=requests.Timeout())
            with self.assertRaises(ProviderCooldown):
                EastmoneyBudget(get, **limits).request_page("page", pause=lambda _: None)
            self.assertEqual(get.call_count, 1)

    def test_hk_fields_and_scores_unchanged(self):
        good = quotes(1500)
        before = picker.score_hk(good)
        with patch.object(picker, "fetch_em_market", return_value=good):
            after = picker.score_hk(picker.fetch_market("港股", [], "", "HK"))
        self.assertEqual(before["代码"].tolist(), after["代码"].tolist())
        self.assertEqual(before["评分"].tolist(), after["评分"].tolist())


if __name__ == "__main__":
    unittest.main()
