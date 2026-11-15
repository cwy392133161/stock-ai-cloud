"""Public daily closes for the trend audit. Every series carries its provider."""
from bisect import bisect_right
from datetime import date
import json
import math
import re
from threading import local

import requests


TENCENT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
EASTMONEY = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SINA_FACTORS = "https://finance.sina.com.cn/realstock/company/{}/qfq.js"
THREAD = local()


class ProviderUnavailable(Exception):
    """Stop querying this provider for the rest of this run."""


def fetch(url, params=None):
    if not hasattr(THREAD, "session"):
        THREAD.session = requests.Session()
    try:
        r = THREAD.session.get(url, params=params, timeout=(4, 9))
    except (requests.ConnectionError, requests.Timeout) as exc:
        raise ProviderUnavailable("连接中断") from exc
    if r.status_code in (401, 403, 418, 429, 456, 501, 502, 503, 504):
        raise ProviderUnavailable(f"HTTP {r.status_code}")
    r.raise_for_status()
    prefix = r.content.lstrip()[:8192].lower()
    if prefix.startswith(b"<") and any(t in prefix for t in (b"captcha", b"waf", b"access denied")):
        raise ProviderUnavailable("访问限制")
    return r


def tencent(symbol, start, end, adjustment="qfq"):
    query = f"{symbol},day,{start},{end},640,{adjustment}"
    payload = fetch(TENCENT, {"param": query}).json()
    if payload.get("code") != 0:
        raise ValueError("日线接口未返回成功状态")
    data = (payload.get("data") or {}).get(symbol) or {}
    rows = data.get(f"{adjustment}day")
    if not isinstance(rows, list) or not rows:
        raise ValueError("缺少指定复权口径或代码的日线")
    return closes(((r[0], r[2]) for r in rows), start, end)


def closes(items, start, end):
    result = {}
    for stamp, raw in items:
        day = date.fromisoformat(stamp)
        if start <= day <= end:
            value = float(raw)
            if stamp in result or not math.isfinite(value):
                raise ValueError("重复交易日或收盘数值异常")
            result[stamp] = value
    return result


def eastmoney(symbol, start, end):
    data = fetch(EASTMONEY, {"secid": ("1." if symbol.startswith("sh") else "0.") + symbol[2:],
                             "klt": 101, "fqt": 1, "beg": start.strftime("%Y%m%d"),
                             "end": end.strftime("%Y%m%d"), "fields1": "f1,f2,f3",
                             "fields2": "f51,f52,f53,f54,f55,f56"}).json().get("data") or {}
    if data.get("code") != symbol[2:] or not data.get("klines"):
        raise ValueError("东方财富日线缺少目标代码或明确复权序列")
    return closes(((d[0], d[2]) for d in (line.split(",") for line in data["klines"])), start, end)


def sina_raw(symbol, start, end):
    # Sina accepts a length measured backwards from today, including when an
    # older snapshot is replayed. Filter further by the exact dates requested.
    n = min(1023, (date.today() - start).days + 15)
    rows = fetch(SINA, {"symbol": symbol, "scale": 240, "ma": "no", "datalen": n}).json()
    if not isinstance(rows, list) or not rows:
        raise ValueError("新浪缺少目标日线")
    return closes(((r["day"], r["close"]) for r in rows), start, end)


def sina_adjust(raw, body, symbol, end):
    match = re.match(r"\s*var\s+" + re.escape(symbol) + r"qfq\s*=\s*", body)
    if not match:
        raise ValueError("缺少新浪前复权因子文件或证券代码不符")
    payload, _ = json.JSONDecoder().raw_decode(body[match.end():])
    rows = payload["data"]
    if not rows or len(rows) != int(payload["total"]):
        raise ValueError("复权因子行数不符")
    factors = {}
    for item in rows:
        day = date.fromisoformat(item["d"]).isoformat()
        value = float(item["f"])
        if day in factors or not math.isfinite(value) or value <= 0:
            raise ValueError("复权因子日期或系数异常")
        factors[day] = value
    dates = sorted(factors)
    def factor(day):
        idx = bisect_right(dates, day) - 1
        return factors[dates[idx]] if idx >= 0 else None
    # Anchor to the signal close, including for replays after newer corporate
    # actions. The common terminal factor cancels in both holding-period returns.
    terminal = factor(end.isoformat())
    if terminal is None:
        raise ValueError("缺少截止日有效复权因子")
    return {day: amount * terminal / f for day, amount in raw.items() if (f := factor(day))}


def sina(symbol, start, end):
    raw = sina_raw(symbol, start, end)
    return sina_adjust(raw, fetch(SINA_FACTORS.format(symbol)).text, symbol, end)
