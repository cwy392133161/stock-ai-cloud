"""Tencent A-share fallback with explicit units, dynamic PE, and quote dates.

Mappings verified against Tencent's own adaptHS frontend (2026-11-10).
Never rename pe_ttm to dynamic PE. All downloads fail closed on partial data.
"""
import math
import re
import time
from datetime import datetime, timezone, timedelta
import pandas as pd
import requests
from market_freshness import a_quote_cutoff
from market_quality import validate_market_data
from market_transport import CollectionDeadline

CST = timezone(timedelta(hours=8))
RANK_URL = "https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList"
QUOTE_URL = "https://qt.gtimg.cn/q="
CODE = re.compile(r"(?:sh|sz|bj)\d{6}\Z")
PAGE_SIZE = 200
BATCH_SIZE = 100


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else float("nan")
    except (ValueError, TypeError):
        return float("nan")


def get_rank_rows(get, pause=time.sleep):
    params = {"_appver": "11.17.0", "board_code": "aStock",
              "sort_type": "price", "direct": "down", "offset": "0",
              "count": str(PAGE_SIZE)}
    rows, total = {}, None
    for sweep in range(2):
        for page in range(40):
            if page or sweep:
                pause(1)
            params["offset"] = str(page * PAGE_SIZE)
            r = get(RANK_URL, params=params.copy(), timeout=(5, 20))
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data") or {}
            if payload.get("code") != 0 or not isinstance(data.get("rank_list"), list):
                raise ValueError("腾讯 A 股分页业务错误或格式异常")
            count = int(data.get("total", 0))
            if total is None:
                total = count
                if not 3000 <= total <= 8000:
                    raise ValueError("腾讯 A 股证券总数异常")
            expected = min(PAGE_SIZE, total-page*PAGE_SIZE)
            if count != total or len(data["rank_list"]) != expected:
                raise ValueError("腾讯 A 股分页数量变化或缺页")
            page_codes = [r.get("code", "") for r in data["rank_list"]]
            if len(set(page_codes)) != len(page_codes):
                raise ValueError("腾讯单页出现重复代码")
            for row in data["rank_list"]:
                code = row.get("code", "")
                if not CODE.fullmatch(code):
                    raise ValueError("腾讯 A 股代码格式异常")
                rows[code] = row
            if len(rows) > total:
                raise ValueError("腾讯证券集合在采集中发生变化")
            if (page+1)*PAGE_SIZE >= total:
                break
        print(f"腾讯证券列表第{sweep+1}遍：唯一代码 {len(rows)}/{total}", flush=True)
        if len(rows) == total:
            return list(rows.values())
    raise ValueError("腾讯 A 股两遍后仍有缺失代码；禁止部分市场评分")


def quote_is_fresh(stamp, observed):
    observed = observed.astimezone(CST)
    return (stamp.date() == observed.date()
            and a_quote_cutoff(observed) <= stamp <= observed + timedelta(seconds=60))


def parse_quotes(text, expected, observed):
    matches = re.findall(r'v_([a-z0-9]+)="([^"]*)";', text)
    result = {}
    for code, content in matches:
        if code not in expected or code in result:
            raise ValueError("腾讯批量行情返回非预期或重复代码")
        f = content.split("~")
        if len(f) <= 57 or f[2] != code[2:]:
            raise ValueError("腾讯批量行情字段不完整或代码不匹配")
        stamp = f[30]
        if len(stamp) != 14 or not stamp.isdigit():
            raise ValueError("腾讯行情缺少可验证报价时间")
        quote_time = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=CST)
        # Old suspended listings are retained as missing observations, not re-dated.
        fresh = quote_is_fresh(quote_time, observed)
        row = {"代码": code[2:], "名称": f[1],
               "最新价": number(f[3]), "涨跌幅": number(f[32]),
               "昨收": number(f[4]), "今开": number(f[5]),
               "最高": number(f[33]), "最低": number(f[34]),
               "涨停价": number(f[47]), "跌停价": number(f[48]),
               "成交量": number(f[36]), "成交额": number(f[57])*1e4,
               "换手率": number(f[38]), "量比": number(f[49]),
               "流通市值": number(f[44])*1e8, "总市值": number(f[45])*1e8,
               "市盈率-动态": number(f[52]), "市净率": number(f[46]),
               "振幅": number(f[43]), "报价时间": stamp, "报价当日": fresh}
        if not fresh:
            row["最新价"] = float("nan")
        result[code] = row
    if set(result) != set(expected):
        raise ValueError("腾讯批量行情缺失证券，禁止以部分市场评分")
    return result


def fetch_tencent_a(get=None, pause=time.sleep, now=None, *, clock=time.monotonic):
    started = now or datetime.now(CST)
    if started.utcoffset() is None:
        raise ValueError("采集时间必须包含时区")
    deadline = CollectionDeadline("腾讯", 600, clock)
    if get is not None:
        def checked_get(url, *, timeout, **kwargs):
            response = get(url, timeout=deadline.timeout(timeout), **kwargs)
            deadline.remaining()
            return response
        return _collect_tencent_a(checked_get, pause, now, started, deadline)

    with requests.Session() as session:
        requests_used = 0

        def bounded_get(url, *, timeout, **kwargs):
            nonlocal requests_used
            for attempt in range(2):
                if requests_used >= 240:
                    raise RuntimeError("腾讯请求预算已耗尽")
                bounded_timeout = deadline.timeout(timeout)
                requests_used += 1
                try:
                    response = session.get(url, timeout=bounded_timeout, **kwargs)
                    status = response.status_code
                    if 400 <= status < 500 and status != 408:
                        raise RuntimeError(f"腾讯 HTTP {status}，停止本轮请求")
                    response.raise_for_status()
                    deadline.remaining()
                    return response
                except (requests.Timeout, requests.ConnectionError, requests.HTTPError):
                    if attempt:
                        raise
                    deadline.wait(pause, 3)

        return _collect_tencent_a(bounded_get, pause, now, started, deadline)


def _collect_tencent_a(get, pause, now, started, deadline):
    day = started.astimezone(CST).date()

    def paced_wait(seconds):
        deadline.wait(pause, seconds)

    rank = get_rank_rows(get, paced_wait)
    rows = []
    stale_active = 0
    for offset in range(0, len(rank), BATCH_SIZE):
        paced_wait(1)
        observed = now or datetime.now(CST)
        if observed.astimezone(CST).date() != day:
            raise ValueError("腾讯行情采集跨日，拒绝混合报价")
        batch = rank[offset:offset+BATCH_SIZE]
        codes = [r["code"] for r in batch]
        response = get(QUOTE_URL + ",".join(codes), timeout=(5, 20))
        response.raise_for_status()
        # Tencent's quote text uses GBK, not the HTTP default ISO-8859-1.
        response.encoding = "gbk"
        observed = now or datetime.now(CST)
        if observed.astimezone(CST).date() != day:
            raise ValueError("腾讯行情采集跨日，拒绝混合报价")
        quotes = parse_quotes(response.text, codes, observed)
        stale_active += sum(row["成交额"] > 0 and not row["报价当日"] for row in quotes.values())
        # Even if every remaining security is active and fresh, the final
        # active denominator can never exceed the full universe.
        if 20 * stale_active > len(rank):
            raise ValueError("腾讯新鲜报价已无法达到95%门槛，停止剩余批次")
        print(f"腾讯批量行情：{min(offset+BATCH_SIZE, len(rank))}/{len(rank)}", flush=True)
        for original in batch:
            row = quotes[original["code"]]
            row["60日涨跌幅"] = number(original.get("zdf_d60"))
            row["年初至今涨跌幅"] = number(original.get("zdf_y"))
            rows.append(row)
    deadline.remaining()
    frame = pd.DataFrame(rows)
    quality = validate_market_data(frame, "A股", "腾讯全市场+批量原始行情/动态PE")
    active = frame["成交额"] > 0
    if not active.any() or frame.loc[active, "报价当日"].mean() < .95:
        raise ValueError("腾讯有成交证券的新鲜报价不足95%，拒绝旧行情")
    quality.update({"证券总数": len(frame), "当日报价数": int(frame["报价当日"].sum()),
                    "报价日期": day.isoformat(), "采集开始": started.isoformat(),
                    "时效说明": "价格/估值报价日期与交易时段新鲜度已核对；60日/YTD来自实时排名接口，该接口无独立时间戳"})
    frame.attrs["quality"] = quality
    return frame

