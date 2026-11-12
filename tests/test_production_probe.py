"""Synthetic transition fixtures only; live recovery still requires real feeds."""
import json
import unittest
from unittest.mock import patch
import production_probe as probe
import publish_guard as guard

OLD = "2026-11-10 17:58"
NEW = "2026-11-12 17:30"
REPORT = '<h1>研究</h1><small>行情快照：' + OLD + '</small><article>不可改写正文</article>'


class ProductionProbeTests(unittest.TestCase):
    def fixture(self, successful=True):
        previous = {"snapshot": OLD, "market_state": "failed", "last_screening_outcome": "failure",
                    "state": "review-required", "review_required": True}
        data = {"updated": NEW if successful else OLD,
                **{key: [{"代码": str(i)} for i in range(20)] for key in ("a_shares", "hk_shares")}}
        old = {**data, "updated": OLD}
        state = guard.research_freshness(data, REPORT, False, "skipped", ai_skipped=True,
                                        market_state=None if successful else "failed",
                                        screening_outcome="success" if successful else "failure",
                                        previous_status=previous)
        index = '<h1>榜单</h1><p class="ai-nav">研究</p>'
        before = {"data.json": json.dumps(old), "ai.html": REPORT, "index.html": index,
                  "ai-status.json": json.dumps(previous)}
        after = {"data.json": json.dumps(data), "ai.html": guard.update_report_banner(REPORT, state),
                 "index.html": guard.update_index(index, state, True), "ai-status.json": json.dumps(state)}
        return before, after

    def test_new_market_recovers_without_unfreezing_research(self):
        before, after = self.fixture()
        state = probe.check_result(before, after, True)
        self.assertEqual(state['market_state'], 'updated')
        self.assertEqual(state['state'], 'review-required')

    def test_failed_market_stays_failed_and_keeps_raw_snapshot(self):
        before, after = self.fixture(False)
        self.assertEqual(probe.check_result(before, after, False)['market_state'], 'failed')

    def test_rejects_unfrozen_research_or_modified_report(self):
        for mutation in ('state', 'report'):
            before, after = self.fixture()
            if mutation == 'state':
                state = json.loads(after['ai-status.json']);state['review_required'] = False
                after['ai-status.json'] = json.dumps(state)
            else:
                after['ai.html'] += '<p>新正文</p>'
            with self.assertRaises(ValueError):
                probe.check_result(before, after, True)

    def test_rejects_partial_rankings_or_unsynchronized_banners(self):
        for mutation in ('count', 'duplicate', 'banner', 'snapshot'):
            before, after = self.fixture()
            data = json.loads(after['data.json'])
            if mutation == 'count':data['hk_shares'].pop()
            if mutation == 'duplicate':data['hk_shares'][0] = data['hk_shares'][1]
            if mutation == 'snapshot':data['updated'] = OLD
            if mutation == 'banner':after['index.html'] = '<h1>未同步</h1>'
            after['data.json'] = json.dumps(data)
            with self.assertRaises(ValueError):
                probe.check_result(before, after, True)

    def test_probe_uses_publication_window_without_network_before_close(self):
        with patch.object(probe.market_schedule, 'is_market_ready', return_value=False), \
             patch.object(probe.subprocess, 'run') as run:
            self.assertEqual(probe.main(), 2)
        run.assert_not_called()


if __name__ == '__main__':unittest.main()
