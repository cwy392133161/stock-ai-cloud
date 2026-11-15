"""Offline fixtures only; no historical model replies or credentials are bundled."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
import requests

import replay_bank_review as replay
from ai_report import stamp_generated_report


class ReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict(os.environ, {"RUNNER_TEMP": tmp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_cache_requires_exact_old_prompt_model_and_order_without_network(self):
        endpoint, model = "https://example.invalid/v1", "offline-model"
        prompt = '原提示词输入资料：{"股票":{"代码":"000001"}}'
        identity = json.dumps([endpoint, model, prompt], ensure_ascii=False).encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        file = self.root / (digest + ".txt")
        file.write_text("离线测试答复", encoding="utf-8")
        rows = {}
        read = replay._cache_reader(self.root, ["000001", "000002"], rows)
        with replay.offline():
            with self.assertRaisesRegex(RuntimeError, "离线"):
                requests.get(endpoint)
            with self.assertRaisesRegex(ValueError, "未命中"):
                read(endpoint, None, model + "x", prompt, None)
            self.assertEqual(read(endpoint, None, model, prompt, None), "离线测试答复")
            with self.assertRaisesRegex(ValueError, "顺序"):
                read(endpoint, None, model, prompt, None)
        self.assertEqual(rows["000001"]["prompt_sha256"], digest)
        self.assertEqual(file.read_text(encoding="utf-8"), "离线测试答复")

    def test_review_preserves_original_generation_time_and_rejects_changed_evidence(self):
        archive = self.root / replay.DAY
        archive.mkdir()
        evidence_bytes = b"offline fixture"
        (archive / replay.EVIDENCE).write_bytes(evidence_bytes)
        stocks = [{"代码": f"{i:06d}", "名称": f"银行{i}"} for i in range(1, 6)]
        snapshot = {"updated": replay.DAY + " 16:15", "a_shares": stocks}
        generated = "2026-11-16 00:16"
        body = ('<title>离线测试</title><h1>离线测试</h1>'
                '<meta name="research-approved-count" content="0">'
                '<meta name="research-limited-count" content="0">'
                '<meta name="research-total-count" content="5">'
                f'<small>行情快照：{snapshot["updated"]}；AI 生成：{generated}（北京时间）</small>')
        for s in stocks:
            body += f'<article><b id="stock-{s["代码"]}">A股 · {s["名称"]} ({s["代码"]})</b></article>'
        original = stamp_generated_report(body)
        old_id = BeautifulSoup(original, "html.parser").select_one(
            'meta[name="research-generation-id"]')["content"]
        (archive / replay.ORIGINAL).write_text(original, encoding="utf-8")
        summary = {"bank_research": {"report": replay.ORIGINAL, "generation_id": old_id,
                                     "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                                     "snapshot": snapshot["updated"]}}
        (archive / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        source, _ = replay._source(archive, stocks)
        rows, evidence = [], {}
        for stock in stocks:
            code = stock["代码"]
            finance = {"报告期": "2026-09-30", "状态": "可用", "指标": {"营业收入": code}}
            notices = {"公告": [{"编号": "D1", "报告期匹配": "2026-09-30", "正文摘录": []}],
                       "对应报告期财报": {"链接": "offline"}}
            processed = {**notices, "对应报告期财报": {"链接": "offline", "正文状态": "未读取", "正文摘录页数": 0}}
            evidence[code] = {"finance": finance, "disclosures": notices}
            reply = "仅供离线测试" + code
            rows.append({"code": code, "stock": stock, "finance": finance, "disclosures": processed,
                         "reply": reply, "reply_sha256": hashlib.sha256(reply.encode()).hexdigest(),
                         "prompt_sha256": hashlib.sha256(code.encode()).hexdigest(), "peers": {}})
        payload = {"source": source, "old_commit": replay.OLD_COMMIT, "rows": rows}
        packet, output = self.root / "private.json", self.root / "reviewed.html"
        packet.write_text(json.dumps(payload), encoding="utf-8")
        with (patch("bank_research_edition.inputs", return_value=(snapshot, evidence, {})),
              patch("ai_report.render_report", return_value=body) as render,
              patch("ai_report.request_deepseek", side_effect=AssertionError("unexpected paid call"))):
            replay.review(archive, packet, output)
            args, kwargs = render.call_args
            self.assertEqual(args[2], generated)
            self.assertEqual([item[2] for item in args[1]], [r["reply"] for r in rows])
            self.assertEqual(len(kwargs["disclosures"]), 5)
            audited = output.read_text(encoding="utf-8")
            soup = BeautifulSoup(audited, "html.parser")
            self.assertEqual(soup.select_one('meta[name="research-original-generation-id"]')["content"], old_id)
            self.assertNotEqual(soup.select_one('meta[name="research-generation-id"]')["content"], old_id)
            self.assertIn("重新审核", soup.get_text())
            self.assertNotIn("仅供离线测试", audited)
            rows[0]["disclosures"] = {"公告": []}
            packet.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "000001"):
                replay.review(archive, packet, output)
            self.assertEqual(audited, output.read_text(encoding="utf-8"))
