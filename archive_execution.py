"""Recover daily execution fields from the original Tencent quote-response bundle."""
import argparse
from collections import Counter
from datetime import date, datetime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from zipfile import ZipFile

import pandas as pd

from tencent_market import parse_quotes


FILENAME = "a-execution-quotes.json.gz"
FIELDS = ["昨收", "今开", "最高", "最低", "涨停价", "跌停价"]
COLUMNS = ["代码", "报价时间", "最新价", *FIELDS, "响应文件"]
CSV = "verified-friday-quotes.csv"


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def recover(archive, bundle):
    day = date.fromisoformat(archive.name)
    # Sunday retrieval preserved Friday's actual exchange timestamps. Compare each
    # security against Friday's original mapped close, never the request date.
    eod = datetime.fromisoformat(f"{day}T23:59:59+08:00")
    with ZipFile(archive / "a-quotes.zip") as original, ZipFile(bundle) as raw:
        for filename in (CSV, "quality.json"):
            if raw.read("A股/" + filename) != original.read(filename):
                raise ValueError(f"原始采集包与已发布归档不匹配 {filename}")
        base = pd.read_csv(original.open(CSV), dtype={"代码": str, "报价时间": str},
                           usecols=["代码", "报价时间", "最新价"], converters={"最新价": float}).set_index("代码")
        if not base.index.is_unique:
            raise ValueError("原始行情包含重复证券")
        csv_hash = _sha(original.read(CSV))
        requests_file = raw.read("A股/requests.jsonl")
        logs = [json.loads(line) for line in requests_file.splitlines() if line.strip()]
        rows, sources, seen = [], {}, set()
        for log in logs:
            filename = log["file"]
            if not filename.endswith(".gbk"):
                continue
            uri = urlsplit(log["url"])
            if (not re.fullmatch(r"[0-9]+\.gbk", filename) or filename in sources or
                    uri.scheme != "https" or uri.netloc != "qt.gtimg.cn" or
                    not uri.path.startswith("/q=") or log["http_status"] != 200):
                raise ValueError("腾讯原始行情请求记录不符")
            symbols = uri.path.removeprefix("/q=").split(",")
            if len(symbols) != len(set(symbols)) or not all(re.fullmatch(r"(sh|sz|bj)[0-9]{6}", s) for s in symbols):
                raise ValueError("证券代码与原始请求不符")
            payload = raw.read("A股/raw/" + filename)
            observed = datetime.fromisoformat(log["observed"])
            if observed.utcoffset() is None:
                raise ValueError("原始响应记录缺少带时区的采集时间")
            source = {"member": "A股/raw/" + filename, "sha256": _sha(payload),
                      "url": log["url"], "observed": log["observed"],
                      "http_date": log.get("provider_http_date"), "count": len(symbols)}
            sources[filename] = source
            for symbol, quote in parse_quotes(payload.decode("gbk"), symbols, eod).items():
                code = symbol[2:]
                quote_time = datetime.strptime(quote["报价时间"], "%Y%m%d%H%M%S").replace(tzinfo=eod.tzinfo)
                if (code in seen or code not in base.index or not quote["报价当日"] or
                        quote_time > observed or
                        base.at[code, "报价时间"] != quote["报价时间"] or
                        base.at[code, "最新价"] != quote["最新价"]):
                    raise ValueError(f"{code} 原始响应与已发布证券、收盘时间或价格不符")
                seen.add(code)
                values = [quote[k] if math.isfinite(quote[k]) else None for k in FIELDS]
                rows.append([code, quote["报价时间"], quote["最新价"], *values, filename])
        members = [n for n in raw.namelist() if n.startswith("A股/raw/") and n.endswith(".gbk")]
        if (len(members) != len(sources) or set(members) != {s["member"] for s in sources.values()}
                or seen != set(base.index)):
            raise ValueError("原始响应与收盘行情覆盖范围不一致")
    rows.sort()
    return {"schema": 1, "trade_date": day.isoformat(), "original_csv_sha256": csv_hash,
            "source_bundle": bundle.name, "source_bundle_sha256": _sha(bundle.read_bytes()),
            "requests_member": "A股/requests.jsonl", "requests_sha256": _sha(requests_file),
            "sources": sources, "columns": COLUMNS, "rows": rows,
            "positive_counts": {key: sum(r[i] is not None and r[i] > 0 for r in rows)
                                for i, key in enumerate(COLUMNS) if key in FIELDS}}


def merge_execution(archive, frame):
    path = archive / FILENAME
    if not path.is_file():
        return frame
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        saved = json.load(stream)
    with ZipFile(archive / "a-quotes.zip") as frozen:
        matches = saved.get("original_csv_sha256") == _sha(frozen.read(CSV))
    if (not matches or saved.get("schema") != 1 or saved.get("trade_date") != archive.name or
            saved.get("columns") != COLUMNS or set(FIELDS) & set(frame.columns)):
        raise ValueError("交易价格补充记录与原始归档不符")
    values = pd.DataFrame(saved["rows"], columns=COLUMNS).set_index("代码")
    counts = Counter(values["响应文件"])
    if (counts != {k: v["count"] for k, v in saved["sources"].items()} or
            any(not re.fullmatch(r"[0-9a-f]{64}", v["sha256"]) for v in saved["sources"].values()) or
            values[FIELDS].gt(0).sum().to_dict() != saved["positive_counts"]):
        raise ValueError("原始响应来源或有效价格覆盖记录不符")
    codes = frame["代码"]
    if set(codes) != set(values.index) or not codes.is_unique or not values.index.is_unique:
        raise ValueError("交易价格补充记录与原始证券范围不符")
    ordered = values.loc[codes]
    for key in ("报价时间", "最新价"):
        if ordered[key].tolist() != frame[key].tolist():
            raise ValueError(f"交易价格补充记录的{key}与原始归档不符")
    return frame.join(values[FIELDS], on="代码", validate="one_to_one")


def publish(saved, path):
    data = json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        staged = Path(tmp.name)
    try:
        staged.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
        try:
            os.link(staged, path)
        except FileExistsError:
            if gzip.decompress(path.read_bytes()) != data:
                raise ValueError("已有交易价格补充档与本次原始响应不同")
    finally:
        staged.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    saved = recover(args.archive, args.bundle)
    publish(saved, args.archive / FILENAME)
    print(len(saved["sources"]), len(saved["rows"]), saved["positive_counts"], flush=True)
