# -*- coding: utf-8 -*-
"""
A 股 + 港股多因子选股（免费云端版）

每天收盘后由 GitHub Actions 定时运行：
1. 拉取东方财富原始字段行情；A 股限流即停，不使用字段不全的新浪榜单
2. 规则化多因子打分（趋势 / 活跃度 / 估值 / 规模），各市场分别取 Top 20
3. 生成中文网页 docs/index.html（供 GitHub Pages 发布）

注意：这是规则化评分，不是大语言模型分析；结果仅供学习参考，不构成投资建议。
东方财富接口会对云端共享 IP 限流，本脚本直调其行情接口：慢速分页、
A 股使用有请求预算的单次新连接，疑似限流停止本轮所有同源请求；失败则
本次不覆盖旧页面（保留上一次结果和更新时间），并在日志中写明原因。
"""

import json
import os
import sys
import time
from html import escape as escape_html

from market_transport import EastmoneyBudget, ProviderCooldown
from market_quality import MarketDataQualityError, validate_market_data
from peer_valuation import industry_benchmarks, safe_number
from peer_fundamentals import candidate_pool
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

try:
    import akshare as ak
except ImportError:  # pragma: no cover
    ak = None

# 北京时间
CST = timezone(timedelta(hours=8))

# ---------- 可调参数 ----------
TOP_N = 20                 # 每个市场取前 N 只
MIN_FLOAT_CAP_A = 20e8     # A 股：流通市值不低于 20 亿人民币
MIN_AMOUNT_A = 5e7          # A 股：当日成交额不低于 5000 万人民币
MIN_FLOAT_CAP_HK = 20e8    # 港股：流通市值不低于 20 亿港币
MIN_AMOUNT_HK = 1e7         # 港股：当日成交额不低于 1000 万港币
RETRY_WAITS = [120, 300]   # 换数据源前的退避等待（秒），可用环境变量覆盖
PAGE_DELAY = 1.5           # 东财分页请求间隔（秒），可用环境变量覆盖
PAGE_SIZE = 100            # 东财接口每页最多 100 行
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# 东财行情接口字段映射：f12 为代码，港股 8xxxx 是真实的人民币交易柜台代码，
# 例如 89988 与港币柜台 09988 均有效，必须保留为两个不同代码；f5 成交量单位为手。
EM_COLS = {
    "f2": "最新价", "f3": "涨跌幅", "f4": "涨跌额", "f5": "成交量",
    "f6": "成交额", "f7": "振幅", "f8": "换手率", "f9": "市盈率-动态",
    "f10": "量比", "f11": "5分钟涨跌", "f12": "代码", "f14": "名称",
    "f15": "最高", "f16": "最低", "f17": "今开", "f18": "昨收",
    "f20": "总市值", "f21": "流通市值", "f22": "涨速", "f23": "市净率",
    "f24": "60日涨跌幅", "f25": "年初至今涨跌幅", "f100": "行业",
}
EM_FIELDS = ",".join(["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9",
                      "f10", "f12", "f13", "f14", "f15", "f16", "f17", "f18",
                      "f20", "f21", "f23", "f24", "f25", "f22", "f11", "f62",
                      "f128", "f136", "f115", "f152", "f100"])
EM_FS_A = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"   # 沪深京 A 股
EM_FS_HK = "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2"           # 港股
EM_HOSTS_A = ["82", "7", "28", ""]    # 空字符串 = 不带数字的 push2.eastmoney.com
EM_HOSTS_HK = ["72", "81", "7", ""]


def _retry_waits():
    raw = os.environ.get("RETRY_WAITS", "")
    if raw:
        try:
            return [int(x) for x in raw.split(",") if x.strip()]
        except ValueError:
            pass
    return RETRY_WAITS


def _num(series):
    """把 AKShare 可能返回的 '-'、空值等转成数字，失败为 NaN。"""
    return pd.to_numeric(series, errors="coerce")


def _col(d, name):
    """取一列转数字；列不存在时返回全 NaN。
    A 股和港股接口的字段并不完全一致（如港股没有换手率），缺失列按中性处理，
    不能直接按列名取值，否则会 KeyError。"""
    if name in d.columns:
        return _num(d[name])
    return pd.Series(float("nan"), index=d.index)


def _page_delay():
    try:
        return float(os.environ.get("PAGE_DELAY", PAGE_DELAY))
    except ValueError:
        return PAGE_DELAY


def _norm_hk_code(code):
    """Preserve actual HKEX counter codes, including 8xxxx RMB dual counters.

    09988 (HKD) and 89988 (RMB) are distinct listings/trading counters.
    Never strip the leading 8: it merges two different counter codes and
    makes a complete Eastmoney response fail duplicate-code validation.
    """
    s = str(code).strip()
    if not s.isascii() or not s.isdigit() or len(s) > 5:
        raise ValueError(f"无效港股交易柜台代码：{s!r}")
    return s.zfill(5)


def _ak_em_get(url, **kwargs):
    """Use AKShare's bounded, fresh-connection transport, not its column projection.

    AKShare 1.20.34 stock_hk_spot_em discards f8/f9/f10/f21/f23/f24/f25.
    Keep the same Eastmoney fields and units; never substitute f115 PE or f20 cap.
    """
    from akshare.utils import http
    return http.request_with_retry(
        "get", url, fresh_session=True, max_retries=3, **kwargs)


def fetch_em_clist(host, fs, market, get=None):
    """直调东财行情接口，慢速分页拉全量。host 如 '82'，空串表示不带数字的 push2。"""
    get = get or requests.get
    base = f"https://{host + '.' if host else ''}push2.eastmoney.com/api/qt/clist/get"
    params = {
        "pn": "1", "pz": str(PAGE_SIZE), "po": "1", "np": "1",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fltt": "2", "invt": "2",
        "fid": "f12", "fs": fs, "fields": EM_FIELDS, "_": "1700000000000",
    }
    def page_data():
        r = get(base, params=params.copy(), headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json().get("data") or {}
        diff = data.get("diff") or []
        rows = list(diff.values()) if isinstance(diff, dict) else diff
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ValueError("东财分页格式异常（非证券对象）")
        return int(data.get("total") or 0), rows

    total, rows = page_data()
    if total <= 0 or not rows:
        raise ValueError("东财接口返回空数据")
    # Some servers cap pages below the requested pz. Use the observed size.
    page_size = len(rows)
    pages = (total + page_size - 1) // page_size
    if pages > 200:
        raise ValueError("东财分页不完整或异常：页数超过安全上限")
    for page in range(2, pages + 1):
        time.sleep(_page_delay())
        params["pn"] = str(page)
        page_total, diff = page_data()
        if page_total != total or len(diff) != min(page_size, total - len(rows)):
            raise ValueError(f"东财分页不完整或总数变化：第 {page} 页")
        rows.extend(diff)
    if len(rows) != total:
        raise ValueError(f"东财分页不完整：预期 {total} 行，实际 {len(rows)} 行")
    df = pd.DataFrame(rows)
    keep = [c for c in EM_COLS if c in df.columns]
    df = df[keep].rename(columns=EM_COLS)
    if "代码" not in df or df["代码"].isna().any():
        raise ValueError("东财证券代码缺失")
    df["代码"] = df["代码"].astype(str)
    if market == "HK":
        df["代码"] = df["代码"].map(_norm_hk_code)
    if df["代码"].eq("").any() or df["代码"].duplicated().any():
        raise ValueError("东财分页证券代码缺失或重复")
    for col in ("最新价", "涨跌幅", "涨跌额", "成交量", "成交额", "振幅", "换手率",
                "市盈率-动态", "量比", "5分钟涨跌", "最高", "最低", "今开", "昨收",
                "总市值", "流通市值", "涨速", "市净率", "60日涨跌幅", "年初至今涨跌幅"):
        if col in df.columns:
            df[col] = _num(df[col])
    print(f"  东财直连（{host or 'push2'}）：{len(df)} 行", flush=True)
    return df


def fetch_em_market(label, hosts, fs, market, get=None):
    """轮换多台东财行情服务器拉取，全部失败才抛错。"""
    last_err = None
    for j, host in enumerate(hosts):
        if j > 0:
            time.sleep(10)
        try:
            df = fetch_em_clist(host, fs, market, get=get)
            source = f"东财原始字段/{host or 'push2'}" + ("/AKShare传输" if get else "")
            df.attrs["quality"] = validate_market_data(df, label, source)
            return df
        except ProviderCooldown:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[{label}] 服务器 {host or 'push2'} 失败：{str(e)[:150]}", flush=True)
    raise RuntimeError(f"东财直连全部服务器失败：{last_err}")


def _ak_em_single_get(url, **kwargs):
    # One physical request per budgeted call. Never hide nested retries.
    from akshare.utils import http
    return http.request_with_retry(
        "get", url, fresh_session=True, max_retries=1, **kwargs)


def fetch_a_market(label, hosts, fs):
    """A股全字段、限流即停；AKShare只作传输，不是独立备用数据源。"""
    get = _ak_em_single_get if ak is not None else requests.get
    budget = EastmoneyBudget(get)
    try:
        frame = fetch_em_market(label, hosts, fs, "A", get=budget)
        frame = _standardize(frame)
        source = "东财A股原始字段/" + ("AKShare单次新连接" if ak is not None else "requests")
        frame.attrs["quality"] = validate_market_data(frame, label, source)
        frame.attrs["quality"]["请求次数"] = budget.requests
        return frame
    except Exception as exc:
        raise RuntimeError(
            "A股 未找到字段完整且合格的行情源；"
            "不重试同源AKShare全市场/分市场；新浪缺少必要评分字段，不能作为等价备用源。"
            f"本轮东财请求 {budget.requests} 次：{exc}"
        ) from exc


def fetch_market(label, hosts, fs, market, ak_primary, ak_fallback):
    """逐源尝试完整行情；字段质量不达标则继续备用源，全部失败保留旧榜单。"""
    if market == "A":
        return fetch_a_market(label, hosts, fs)
    waits = _retry_waits() or [0]
    attempts = [("东财直连", lambda: fetch_em_market(label, hosts, fs, market))]
    if ak is not None:
        attempts.append(("东财港股原始字段（AKShare传输）", lambda: fetch_em_market(
            label, hosts, fs, market, get=_ak_em_get)))
        if ak_fallback is not None:
            attempts.append(("AKShare 新浪（需字段质量核验）", ak_fallback))
    last_err = None
    for i, (name, fn) in enumerate(attempts):
        if i > 0:
            # Switching from Eastmoney to Sina changes the upstream provider.
            # Eastmoney's IP cooldown does not apply to Sina, so don't waste
            # another five minutes waiting before checking its real fields.
            wait = 0 if "新浪" in name else waits[min(i - 1, len(waits) - 1)]
            if wait > 0:
                print(f"[{label}] 改试{name}前等待 {wait} 秒（接口限流退避）...", flush=True)
                time.sleep(wait)
        try:
            df = fn()
            if df is None or len(df) == 0:
                raise ValueError("接口返回空数据")
            df = _standardize(df)
            quality = validate_market_data(df, label, df.attrs.get("quality", {}).get("行情来源", name))
            df.attrs["quality"] = quality
            print(f"[{label}] {name}质量合格：{len(df)} 行；"
                  f"字段有效覆盖 {quality['字段有效覆盖率%']}", flush=True)
            return df
        except Exception as e:  # noqa: BLE001 - 任何接口异常都按限流/故障处理
            last_err = e
            print(f"[{label}] {name}失败：{str(e)[:200]}", flush=True)
    raise RuntimeError(f"{label} 未找到字段完整且合格的行情源；最后一次失败：{last_err}")


def _standardize(df):
    """统一不同数据源的列名：新浪港股用 '中文名称'，没有名称时用代码代替。
    保证下游打分和榜单总能读到 代码 / 名称 两列。"""
    d = df.copy()
    if "名称" not in d.columns and "中文名称" in d.columns:
        d = d.rename(columns={"中文名称": "名称"})
    if "名称" not in d.columns and "代码" in d.columns:
        d["名称"] = d["代码"].astype(str)
    if "代码" not in d.columns:
        d["代码"] = ""
    d["代码"] = d["代码"].astype(str)
    if "名称" in d.columns:
        d["名称"] = d["名称"].astype(str)
    return d


def _rank_pct(s):
    """百分位排名（0~100），NaN 视为中位数 50。"""
    r = s.rank(pct=True) * 100
    return r.fillna(50.0)


def score_a(df):
    """A 股打分：趋势 40 / 活跃度 25 / 估值 20 / 规模 15。"""
    d = df.copy()
    d["最新价"] = _col(d, "最新价")
    d["涨跌幅"] = _col(d, "涨跌幅")
    d["60日涨跌幅"] = _col(d, "60日涨跌幅")
    d["年初至今涨跌幅"] = _col(d, "年初至今涨跌幅")
    d["换手率"] = _col(d, "换手率")
    d["量比"] = _col(d, "量比")
    d["市盈率-动态"] = _col(d, "市盈率-动态")
    d["市净率"] = _col(d, "市净率")
    d["流通市值"] = _col(d, "流通市值")
    d["成交额"] = _col(d, "成交额")
    d["振幅"] = _col(d, "振幅")

    # 过滤：ST / 退市、停牌（无价）、市值与成交额过小；缺失数据不得跳过门槛
    if "名称" in d.columns:
        d = d[~d["名称"].astype(str).str.contains("ST|退", na=False)]
    d = d[d["最新价"].notna() & (d["最新价"] > 0)]
    # 未知市值/成交额不能视为通过原有门槛；覆盖不足已由全市场质量检查拦截。
    d = d[(d["流通市值"] >= MIN_FLOAT_CAP_A) &
          (d["成交额"] >= MIN_AMOUNT_A)]
    d = d.dropna(subset=["涨跌幅"])

    trend = 0.5 * _rank_pct(d["60日涨跌幅"]) + 0.3 * _rank_pct(d["年初至今涨跌幅"]) + 0.2 * _rank_pct(d["涨跌幅"])
    active = 0.5 * _rank_pct(d["换手率"]) + 0.5 * _rank_pct(d["量比"])
    pe = d["市盈率-动态"].where((d["市盈率-动态"] > 0) & (d["市盈率-动态"] < 100))
    pb = d["市净率"].where((d["市净率"] > 0) & (d["市净率"] < 10))
    value = 0.6 * (100 - _rank_pct(pe)) + 0.4 * (100 - _rank_pct(pb))
    size = _rank_pct(d["流通市值"])
    d["评分"] = (0.40 * trend + 0.25 * active + 0.20 * value + 0.15 * size).round(1)
    d["趋势分"] = trend.round(1)
    d["活跃分"] = active.round(1)
    d["估值分"] = value.round(1)
    return d.sort_values("评分", ascending=False)


def score_hk(df):
    """港股打分：趋势 40 / 活跃度 25 / 估值 20 / 规模 15。
    直连接口的港股数据包含 60 日涨跌幅、换手率、市盈率等全字段，与 A 股用
    同一套打分；所有来源必须先通过字段覆盖检查，整列缺失的来源不得入榜。"""
    d = df.copy()
    d["最新价"] = _col(d, "最新价")
    d["涨跌幅"] = _col(d, "涨跌幅")
    d["60日涨跌幅"] = _col(d, "60日涨跌幅")
    d["年初至今涨跌幅"] = _col(d, "年初至今涨跌幅")
    d["换手率"] = _col(d, "换手率")
    d["量比"] = _col(d, "量比")
    d["市盈率-动态"] = _col(d, "市盈率-动态")
    d["市净率"] = _col(d, "市净率")
    d["流通市值"] = _col(d, "流通市值")
    d["成交额"] = _col(d, "成交额")
    d["振幅"] = _col(d, "振幅")

    if "名称" in d.columns:
        d = d[~d["名称"].astype(str).str.contains("ST|退", na=False)]
    d = d[d["最新价"].notna() & (d["最新价"] > 0)]
    d = d[(d["流通市值"] >= MIN_FLOAT_CAP_HK) &
          (d["成交额"] >= MIN_AMOUNT_HK)]
    d = d.dropna(subset=["涨跌幅"])

    if d["60日涨跌幅"].notna().any():
        trend = 0.5 * _rank_pct(d["60日涨跌幅"]) + 0.3 * _rank_pct(d["年初至今涨跌幅"]) + 0.2 * _rank_pct(d["涨跌幅"])
    else:
        trend = _rank_pct(d["涨跌幅"])
    active = 0.5 * _rank_pct(d["换手率"]) + 0.5 * _rank_pct(d["量比"])
    pe = d["市盈率-动态"].where((d["市盈率-动态"] > 0) & (d["市盈率-动态"] < 100))
    pb = d["市净率"].where((d["市净率"] > 0) & (d["市净率"] < 10))
    value = 0.6 * (100 - _rank_pct(pe)) + 0.4 * (100 - _rank_pct(pb))
    size = _rank_pct(d["流通市值"])
    d["评分"] = (0.40 * trend + 0.25 * active + 0.20 * value + 0.15 * size).round(1)
    d["趋势分"] = trend.round(1)
    d["活跃分"] = active.round(1)
    d["估值分"] = value.round(1)
    return d.sort_values("评分", ascending=False)


def fmt_money(v, unit):
    if pd.isna(v):
        return "-"
    if v >= 1e8:
        return f"{v / 1e8:.1f} 亿{unit}"
    return f"{v / 1e4:.0f} 万{unit}"


def reasons(row, market):
    """按得分贡献生成中文入选原因。"""
    rs = []
    if pd.notna(row.get("60日涨跌幅")):
        rs.append(f"60日涨 {row['60日涨跌幅']:+.1f}%")
    if pd.notna(row.get("年初至今涨跌幅")):
        rs.append(f"今年以来 {row['年初至今涨跌幅']:+.1f}%")
    rs.append(f"今日 {row['涨跌幅']:+.1f}%")
    if pd.notna(row.get("换手率")):
        rs.append(f"换手率 {row['换手率']:.1f}%")
    pe_col = "市盈率-动态" if pd.notna(row.get("市盈率-动态")) else "市盈率"
    if pd.notna(row.get(pe_col)) and 0 < row[pe_col] < 100:
        rs.append(f"市盈率 {row[pe_col]:.0f} 倍")
    return "，".join(rs)


def risks(row):
    """按阈值生成风险提示。"""
    out = []
    if pd.notna(row.get("涨跌幅")) and row["涨跌幅"] > 7:
        out.append("今日涨幅过大，追高风险")
    if pd.notna(row.get("换手率")) and row["换手率"] > 15:
        out.append("换手率过高，情绪过热")
    if pd.notna(row.get("振幅")) and row["振幅"] > 8:
        out.append("日内波动大")
    pe_col = "市盈率-动态" if "市盈率-动态" in row else "市盈率"
    if pd.notna(row.get(pe_col)) and row[pe_col] > 60:
        out.append("估值偏高")
    if pd.notna(row.get(pe_col)) and row[pe_col] <= 0:
        out.append("暂无盈利/市盈率为负")
    return "；".join(out) if out else "无明显异常信号"


def pick_rows(d, market, n, industry_reference=None):
    rows = []
    industry_reference = industry_reference or {}
    for _, r in d.head(n).iterrows():
        industry = (str(r.get("行业")).strip()
                    if pd.notna(r.get("行业")) and str(r.get("行业")).strip()
                    else None)
        rows.append({
            "代码": str(r.get("代码", "")),
            "名称": str(r.get("名称", r.get("代码", ""))),
            "最新价": float(r["最新价"]),
            "涨跌幅": float(r["涨跌幅"]),
            "评分": float(r["评分"]),
            "趋势分": float(r["趋势分"]),
            "活跃分": float(r["活跃分"]),
            "估值分": float(r["估值分"]),
            "换手率": float(r["换手率"]) if pd.notna(r.get("换手率")) else None,
            # Keep quote valuation as structured AS-OF data, never extract a
            # PE number from the explanatory ranking paragraph.
            "市盈率-动态": safe_number(r.get("市盈率-动态")),
            "市净率": safe_number(r.get("市净率")),
            "60日涨跌幅": safe_number(r.get("60日涨跌幅")),
            "年初至今涨跌幅": safe_number(r.get("年初至今涨跌幅")),
            "行业": industry,
            "流通市值数值": safe_number(r.get("流通市值")),
            "行业估值参考": industry_reference.get(industry) if market == "A" else None,
            "成交额": fmt_money(r["成交额"], "元" if market == "A" else "港币"),
            "流通市值": fmt_money(r["流通市值"], "元" if market == "A" else "港币"),
            "入选原因": reasons(r, market),
            "风险提示": risks(r),
        })
    return rows


HTML_HEAD = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A 股 + 港股 AI 选股榜</title>
<style>
body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f6f7f9;color:#222}
.wrap{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:22px;margin:8px 0 4px}
.sub{color:#666;font-size:13px;line-height:1.6}
h2{font-size:18px;margin:28px 0 8px}
table{width:100%;border-collapse:collapse;background:#fff;font-size:14px}
th,td{padding:8px 6px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{background:#fafafa;font-size:13px;color:#555}
.up{color:#d43d3d}.down{color:#1a9e54}
.score{font-weight:700}
.note{font-size:12px;color:#888;line-height:1.7;margin-top:24px}
.ai-nav{background:#eaf2ff;border-radius:10px;padding:12px 14px;line-height:1.8}.ai-nav a{font-weight:700;color:#2455c7;text-decoration:none}.ai-nav span{color:#667085;font-size:12px}
.tag{display:inline-block;background:#eef3ff;color:#2b5fd9;border-radius:4px;padding:1px 6px;font-size:12px}
</style>
</head>
<body><div class="wrap">
<h1>📈 A 股 + 港股 AI 选股榜</h1>
<p class="ai-nav"><a href="./ai.html">🧠 查看 DeepSeek AI 中文研究报告</a> <span>（AI 报告可能不是最新交易日，请核对报告时间）</span></p>
<p class="sub">更新时间：<b>{updated}</b>（北京时间）<br>
规则化多因子评分：趋势 40% / 活跃度 25% / 估值 20% / 规模 15%。每天工作日收盘后自动更新。数据来源：AKShare 免费接口（东方财富 / 新浪）。</p>
"""

HTML_FOOT = """
<p class="note">⚠️ 本页面由程序按公开行情数据自动生成，仅供学习参考，不构成任何投资建议。股市有风险，入市需谨慎。<br>
选股逻辑：过滤 ST / 退市、流通市值与成交额过小的股票后，按趋势、活跃度、估值、规模综合打分，各市场取前 20 名。数据来源：东方财富免费行情接口（直连 + AKShare / 新浪兜底）。<br>
遇到节假日、行情接口故障或 GitHub 定时任务延迟，页面可能显示上一次的结果，请以页面上的更新时间为准。</p>
</div></body></html>
"""


def table_html(title, rows, market):
    lines = [f"<h2>{title}</h2>", "<table><tr><th>#</th><th>股票</th><th>最新价</th><th>涨跌幅</th><th>评分</th><th>入选原因</th><th>风险提示</th></tr>"]
    for i, r in enumerate(rows, 1):
        chg = f"{r['涨跌幅']:+.2f}%"
        cls = "up" if r["涨跌幅"] >= 0 else "down"
        lines.append(
            f"<tr><td>{i}</td><td><b>{r['名称']}</b><br><span style='color:#888;font-size:12px'>{r['代码']}</span></td>"
            f"<td>{r['最新价']}</td><td class='{cls}'>{chg}</td>"
            f"<td class='score'>{r['评分']}</td><td>{r['入选原因']}</td><td>{r['风险提示']}</td></tr>"
        )
    lines.append("</table>")
    return "\n".join(lines)


def main():
    if ak is None:
        print("未安装 akshare，请先 pip install -r requirements.txt", file=sys.stderr)
        return 2
    now = datetime.now(CST)
    updated = now.strftime("%Y-%m-%d %H:%M")

    df_a = fetch_market("A股", EM_HOSTS_A, EM_FS_A, "A",
                        ak.stock_zh_a_spot_em, ak.stock_zh_a_spot)
    df_hk = fetch_market("港股", EM_HOSTS_HK, EM_FS_HK, "HK",
                         ak.stock_hk_spot_em, ak.stock_hk_spot)

    ranked_a = score_a(df_a)
    ranked_hk = score_hk(df_hk)
    for market_name, ranked in (("A股", ranked_a), ("港股", ranked_hk)):
        if len(ranked) >= TOP_N and ranked.head(TOP_N)["评分"].nunique() <= 1:
            raise MarketDataQualityError(
                f"{market_name} 前{TOP_N}名评分全部相同，疑似多因子评分退化；拒绝覆盖旧榜单"
            )
    # Full eligible A-share universe provides a less cherry-picked valuation
    # context than comparing only the top-ranked names. No extra network call.
    sector_reference = industry_benchmarks(ranked_a)
    top_a = pick_rows(ranked_a, "A", TOP_N, sector_reference)
    top_hk = pick_rows(ranked_hk, "HK", TOP_N)
    # Persist a bounded reproducible, similarity-sized peer candidate pool
    # without changing scores, Top20 or the original price snapshot.
    a_peer_pool = candidate_pool(ranked_a, top_a[:5])
    print(f"A 股候选 {len(df_a)} 只，入选 {len(top_a)} 只；港股候选 {len(df_hk)} 只，入选 {len(top_hk)} 只", flush=True)
    if len(top_a) != TOP_N or len(top_hk) != TOP_N:
        raise MarketDataQualityError("通过有效市值、成交额门槛的股票不足 Top 20，保留已有榜单")
    quality = {"A股": df_a.attrs["quality"], "港股": df_hk.attrs["quality"]}

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    os.makedirs(out_dir, exist_ok=True)
    # AI 报告页由 ai_report.py 在选股之后生成；榜单页只在 ai.html 已存在时才放链接，
    # 否则显示待开启提示（首次开启 AI 的那次运行，链接从下一次运行开始出现）。
    if os.path.exists(os.path.join(out_dir, "ai.html")):
        ai_link = ''
    else:
        ai_link = '<p class="sub">📝 AI 深度分析待配置 DeepSeek 密钥后开启。</p>'
    html = HTML_HEAD.replace("{updated}", updated)
    html += ai_link
    for m in ("A股", "港股"):
        q = quality[m]
        cov = q["字段有效覆盖率%"]
        text = ("行情质量核验：" + m + " · " + q["行情来源"]
                + " · 有效股票 " + str(q["有效股票数"])
                + " · 60日涨幅 " + str(cov["60日涨跌幅"]) + "%"
                + " · 换手率 " + str(cov["换手率"]) + "%"
                + " · 流通市值 " + str(cov["流通市值"]) + "%")
        html += '<p class="sub">' + escape_html(text) + '</p>'
    html += table_html("🇨🇳 A 股 Top 20", top_a, "A")
    html += table_html("🇭🇰 港股 Top 20", top_hk, "HK")
    html += HTML_FOOT
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "quality": quality, "a_shares": top_a,
                   "hk_shares": top_hk, "a_peer_pool": a_peer_pool},
                  f, ensure_ascii=False, indent=2)
    print(f"已生成 docs/index.html（更新时间 {updated}）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

