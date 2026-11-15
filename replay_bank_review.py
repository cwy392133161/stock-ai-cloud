"""Recover five paid answers offline and review them against the same archived evidence."""
import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch

from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
OLD_COMMIT = "ab175a85c6f1d84c4b7ecfaabed25f46b18800d0"
DAY = "2026-11-13"
ARCHIVE = Path("docs/history") / DAY
EVIDENCE = "a-bank-evidence.json.gz"
ORIGINAL = "ai-bank.html"
CST = timezone(timedelta(hours=8))


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _private(path):
    root = Path(os.getenv("RUNNER_TEMP") or tempfile.gettempdir()).resolve()
    resolved = Path(path).resolve()
    if root not in resolved.parents or (ROOT / "docs").resolve() in resolved.parents:
        raise ValueError("复审中间文件必须位于临时目录内")
    return resolved


def _forbid(*args, **kwargs):
    raise RuntimeError("离线复审不允许网络请求")


@contextmanager
def offline():
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"AI_API_KEY": "offline-replay-no-secret"}))
        for target in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex",
                       "requests.sessions.Session.request"):
            stack.enter_context(patch(target, side_effect=_forbid))
        yield


def _source(archive, stocks):
    from archive_research import _read_research
    path = archive / ORIGINAL
    raw = path.read_bytes()
    html = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    page = BeautifulSoup(html, "html.parser")
    details = _read_research(html, DAY, stocks)
    summary = json.loads((archive / "summary.json").read_text(encoding="utf-8"))["bank_research"]
    evidence_hash = _hash((archive / EVIDENCE).read_bytes())
    if (details["generation_id"] != summary["generation_id"] or summary["report"] != ORIGINAL
            or evidence_hash != summary["evidence_sha256"] or details["snapshot"] != summary["snapshot"]):
        raise ValueError("原付费报告、生成编号或原始财报归档不匹配")
    times = re.findall(r"AI 生成[：:]\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})", page.get_text())
    if len(times) != 1:
        raise ValueError("无法唯一确定原付费报告的生成时间")
    datetime.strptime(times[0], "%Y-%m-%d %H:%M")
    return {"generation_id": details["generation_id"], "generated_at": times[0],
            "snapshot": details["snapshot"], "evidence_sha256": evidence_hash,
            "report_sha256": _hash(raw)}, page.select("article")


def _same_disclosures(actual, frozen, finance):
    # The paid generation attached two derived PDF-read fields to the frozen
    # matching report. All notices, links, excerpts, periods and amounts remain exact.
    expected = dict(frozen)
    report = frozen.get("对应报告期财报")
    if report and report.get("链接"):
        period = finance.get("报告期") if finance.get("状态") in ("可用", "部分可用") else None
        match = next((n for n in frozen["公告"] if n.get("报告期匹配") == period), None)
        if match is not None:
            expected["对应报告期财报"] = dict(report, 正文状态=match.get("正文状态", "未读取"),
                                            正文摘录页数=len(match.get("正文摘录", [])))
    return actual == expected


def _cache_reader(cache, codes, recovered):
    def read(endpoint, _key, model, prompt, _cache_dir):
        inputs = json.loads(prompt.split("输入资料：", 1)[1])
        code = inputs["股票"]["代码"]
        if len(recovered) >= len(codes) or code != codes[len(recovered)]:
            raise ValueError("历史提示词缺少对应公司或顺序不符")
        identity = json.dumps([endpoint, model, prompt], ensure_ascii=False).encode("utf-8")
        digest = _hash(identity)
        file = _private(cache / (digest + ".txt"))
        if file.parent != cache or not file.is_file():
            raise ValueError(f"{code} 未命中原付费提示词缓存 {digest}")
        data = file.read_text(encoding="utf-8")
        if not data.strip():
            raise ValueError(f"{code} 原答缓存为空")
        recovered[code] = {"prompt_sha256": digest, "reply_sha256": _hash(data.encode("utf-8")),
                           "reply": data}
        return data
    return read


def recover(old, cache, packet):
    """Run in a separate Python process so imports resolve to the exact historical tree."""
    cache, packet = _private(cache), _private(packet)
    old = old.resolve()
    head = subprocess.check_output(["git", "-C", str(old), "rev-parse", "HEAD"], text=True).strip()
    if head != OLD_COMMIT:
        raise ValueError("旧版检出与原付费生成所用提交不一致")
    subprocess.run(["git", "-C", str(old), "diff", "--quiet", "HEAD", "--", "*.py", str(ARCHIVE.as_posix())],
                   check=True)
    sys.path.insert(0, str(old))
    import ai_report as ai
    from bank_research_edition import inputs
    if Path(ai.__file__).resolve().parent != old:
        raise RuntimeError("请在独立进程中运行 recover")
    with offline(), patch.object(ai, "request_deepseek", side_effect=_forbid):
        snapshot, evidence, _ = inputs(old / ARCHIVE)
        stocks = snapshot["a_shares"][:5]
        codes = [s["代码"] for s in stocks]
        source, original_articles = _source(ROOT / ARCHIVE, stocks)
        if (_hash((old / ARCHIVE / EVIDENCE).read_bytes()) != source["evidence_sha256"] or
                source["snapshot"] != snapshot["updated"]):
            raise ValueError("旧检出的行情或财报输入与原付费报告不匹配")
        replies, rows = {}, []
        render_original = ai.render_report

        def capture(data, output, _now, *, disclosures, peer_data, markets):
            original = render_original(data, output, source["generated_at"], disclosures=disclosures,
                                       peer_data=peer_data, markets=markets)
            articles = BeautifulSoup(original.replace("\r\n", "\n").replace("\r", "\n"), "html.parser").select("article")
            if len(articles) != 5 or len(output) != 5 or list(replies) != codes:
                raise ValueError("五家公司的原答尚未完整恢复")
            for code, article, genuine, item in zip(codes, articles, original_articles, output):
                market, stock, text, finance = item
                if (market != "A股" or stock["代码"] != code or text != replies[code]["reply"][:10000]
                        or finance != evidence[code]["finance"]):
                    raise ValueError(f"{code} 原答与证券或原始财务不同")
                if article != genuine:
                    raise ValueError(f"{code} 旧渲染结果与已发布原付费文章不同")
                rows.append(dict(replies[code], code=code, stock=stock, finance=finance,
                                 disclosures=disclosures[market, code], peers=peer_data[market, code]))
            return original

        with (patch.object(ai, "_request_with_checkpoint", _cache_reader(cache, codes, replies)),
              patch.object(ai, "render_report", capture),
              tempfile.TemporaryDirectory(dir=packet.parent) as temp):
            ai.generate_report(snapshot, Path(temp) / "old.html", markets=("A股",), evidence_snapshot=evidence)
    if len(rows) != 5:
        raise ValueError("五份原答未全部核对")
    ai._atomic_write(packet, json.dumps({"old_commit": head, "source": source, "rows": rows},
                                       ensure_ascii=False, allow_nan=False))
    print("已按原提示词匹配五份缓存并逐篇复现已发布文章", flush=True)


def review(archive, packet, output):
    from ai_report import _atomic_write, render_report, stamp_generated_report
    from archive_research import _read_research
    from bank_research_edition import inputs
    packet, output = _private(packet), _private(output)
    if packet == output or archive.name != DAY:
        raise ValueError("输入、输出或复审交易日不符")
    with offline():
        replay = json.loads(packet.read_text(encoding="utf-8"))
        snapshot, evidence, _ = inputs(archive)
        stocks = snapshot["a_shares"][:5]
        source, _ = _source(archive, stocks)
        rows = replay["rows"]
        if (replay["old_commit"] != OLD_COMMIT or replay["source"] != source or
                [r["code"] for r in rows] != [s["代码"] for s in stocks] or
                len({r["prompt_sha256"] for r in rows}) != 5):
            raise ValueError("复审记录与原报告或五家公司不符")
        results, disclosures, peers = [], {}, {}
        for stock, row in zip(stocks, rows):
            code = stock["代码"]
            if (row["stock"] != stock or row["finance"] != evidence[code]["finance"] or
                    not _same_disclosures(row["disclosures"], evidence[code]["disclosures"], row["finance"]) or
                    _hash(row["reply"].encode("utf-8")) != row["reply_sha256"] or
                    not re.fullmatch(r"[0-9a-f]{64}", row["prompt_sha256"])):
                raise ValueError(f"{code} 财务输入、行情或原答摘要有变化")
            results.append(("A股", stock, row["reply"], row["finance"]))
            disclosures["A股", code] = row["disclosures"]
            peers["A股", code] = row["peers"]
        html = render_report(snapshot, results, source["generated_at"], disclosures=disclosures,
                             peer_data=peers, markets=("A股",))
        html = stamp_generated_report(html, expected_count=5)
    original_id = source["generation_id"]
    reviewed = datetime.now(CST).isoformat(timespec="seconds")
    marker = (f'<meta name="research-original-generation-id" content="{original_id}">'
              f'<meta name="research-reviewed-at" content="{reviewed}">')
    note = (f'<p class="bank-review-provenance">本次沿用北京时间 {source["generated_at"]} 已付费生成的'
            f'五份原答，按新的银行研究口径重新审核。复审时间 {reviewed}。'
            f'原报告编号 <code>{original_id}</code>。</p>')
    html = html.replace("</title>", "</title>" + marker, 1).replace("</h1>", "</h1>" + note, 1)
    details = _read_research(html, DAY, stocks)
    if details["generation_id"] == original_id or details["snapshot"] != source["snapshot"]:
        raise ValueError("复审标记缺失或行情日期不一致")
    _atomic_write(output, html)
    print("银行复审完成", details["generation_id"], details["approved"], details["conditional"],
          details["facts_only"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(required=True, dest="command")
    recover_parser = commands.add_parser("recover")
    for name in ("old", "cache", "packet"):
        recover_parser.add_argument("--" + name, required=True, type=Path)
    review_parser = commands.add_parser("review")
    for name in ("archive", "packet", "output"):
        review_parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    if args.command == "recover":
        recover(args.old, args.cache, args.packet)
    else:
        review(args.archive, args.packet, args.output)
