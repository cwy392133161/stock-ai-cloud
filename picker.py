# -*- coding: utf-8 -*-
"""
A 股 + 港股多因子选股（免费云端版）

每天收盘后由 GitHub Actions 定时运行：
1. 用 AKShare 免费接口拉取 A 股 / 港股实时行情（东方财富源优先，新浪源兜底）
2. 规则化多因子打分（趋势 / 活跃度 / 估值 / 规模），各市场分别取 Top 20
3. 生成中文网页 docs/index.html（供 GitHub Pages 发布）

注意：这是规则化评分，不是大语言模型分析；结果仅供学习参考，不构成投资建议。
东方财富接口会对云端共享 IP 限流，拉取失败时按退避间隔重试，仍失败则
本次不覆盖旧页面（保留上一次结果和更新时间），并在日志中写明原因。
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import pandas as pd

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
RETRY_WAITS = [120, 300]   # 东方财富限流后的重试等待（秒），可用环境变量覆盖


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


def fetch_with_retry(label, primary, fallback):
    """先跑主接口，失败按退避重试；仍失败再试备用接口；都失败则抛错。"""
    last_err = None
    for attempt, wait in enumerate([0] + _retry_waits()):
        if wait:
            print(f"[{label}] 第 {attempt} 次重试前等待 {wait} 秒（接口限流退避）...", flush=True)
            time.sleep(wait)
        try:
            df = primary()
            if df is None or len(df) == 0:
                raise ValueError("接口返回空数据")
            print(f"[{label}] 成功：{len(df)} 行", flush=True)
            return df
        except Exception as e:  # noqa: BLE001 - 任何接口异常都按限流/故障处理
            last_err = e
            print(f"[{label}] 第 {attempt + 1} 次失败：{str(e)[:200]}", flush=True)
    if fallback is not None:
        print(f"[{label}] 改试备用接口...", flush=True)
        try:
            df = fallback()
            if df is None or len(df) == 0:
                raise ValueError("备用接口返回空数据")
            print(f"[{label}] 备用接口成功：{len(df)} 行", flush=True)
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[{label}] 备用接口失败：{str(e)[:200]}", flush=True)
    raise RuntimeError(f"{label} 数据拉取失败：{last_err}")


def _rank_pct(s):
    """百分位排名（0~100），NaN 视为中位数 50。"""
    r = s.rank(pct=True) * 100
    return r.fillna(50.0)


def score_a(df):
    """A 股打分：趋势 40 / 活跃度 25 / 估值 20 / 规模 15。"""
    d = df.copy()
    d["涨跌幅"] = _num(d["涨跌幅"])
    d["60日涨跌幅"] = _num(d["60日涨跌幅"]) if "60日涨跌幅" in d else pd.Series(float("nan"), index=d.index)
    d["年初至今涨跌幅"] = _num(d["年初至今涨跌幅"]) if "年初至今涨跌幅" in d else pd.Series(float("nan"), index=d.index)
    d["换手率"] = _num(d["换手率"])
    d["量比"] = _num(d["量比"])
    d["市盈率-动态"] = _num(d["市盈率-动态"])
    d["市净率"] = _num(d["市净率"])
    d["流通市值"] = _num(d["流通市值"])
    d["成交额"] = _num(d["成交额"])
    d["振幅"] = _num(d["振幅"])

    # 过滤：ST / 退市、停牌（无价）、市值与成交额过小
    d = d[~d["名称"].astype(str).str.contains("ST|退", na=False)]
    d = d[d["最新价"].notna() & (d["最新价"] > 0)]
    d = d[d["流通市值"] >= MIN_FLOAT_CAP_A]
    d = d[d["成交额"] >= MIN_AMOUNT_A]
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
    港股实时接口没有 60 日涨跌幅，趋势分用当日涨跌幅。"""
    d = df.copy()
    d["涨跌幅"] = _num(d["涨跌幅"])
    d["换手率"] = _num(d["换手率"])
    d["量比"] = _num(d["量比"])
    d["市盈率"] = _num(d["市盈率"])
    d["市净率"] = _num(d["市净率"])
    d["流通市值"] = _num(d["流通市值"])
    d["成交额"] = _num(d["成交额"])
    d["振幅"] = _num(d["振幅"])

    d = d[~d["名称"].astype(str).str.contains("ST|退", na=False)]
    d = d[d["最新价"].notna() & (d["最新价"] > 0)]
    d = d[d["流通市值"] >= MIN_FLOAT_CAP_HK]
    d = d[d["成交额"] >= MIN_AMOUNT_HK]
    d = d.dropna(subset=["涨跌幅"])

    trend = _rank_pct(d["涨跌幅"])
    active = 0.5 * _rank_pct(d["换手率"]) + 0.5 * _rank_pct(d["量比"])
    pe = d["市盈率"].where((d["市盈率"] > 0) & (d["市盈率"] < 100))
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
    if market == "A":
        if pd.notna(row.get("60日涨跌幅")):
            rs.append(f"60日涨 {row['60日涨跌幅']:+.1f}%")
        if pd.notna(row.get("年初至今涨跌幅")):
            rs.append(f"今年以来 {row['年初至今涨跌幅']:+.1f}%")
    rs.append(f"今日 {row['涨跌幅']:+.1f}%")
    if pd.notna(row.get("换手率")):
        rs.append(f"换手率 {row['换手率']:.1f}%")
    pe_col = "市盈率-动态" if market == "A" else "市盈率"
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


def pick_rows(d, market, n):
    rows = []
    for _, r in d.head(n).iterrows():
        rows.append({
            "代码": str(r["代码"]),
            "名称": str(r["名称"]),
            "最新价": float(r["最新价"]),
            "涨跌幅": float(r["涨跌幅"]),
            "评分": float(r["评分"]),
            "趋势分": float(r["趋势分"]),
            "活跃分": float(r["活跃分"]),
            "估值分": float(r["估值分"]),
            "换手率": float(r["换手率"]) if pd.notna(r.get("换手率")) else None,
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
.tag{display:inline-block;background:#eef3ff;color:#2b5fd9;border-radius:4px;padding:1px 6px;font-size:12px}
</style>
</head>
<body><div class="wrap">
<h1>📈 A 股 + 港股 AI 选股榜</h1>
<p class="sub">更新时间：<b>{updated}</b>（北京时间）<br>
规则化多因子评分：趋势 40% / 活跃度 25% / 估值 20% / 规模 15%。每天工作日收盘后自动更新。数据来源：AKShare 免费接口（东方财富 / 新浪）。</p>
"""

HTML_FOOT = """
<p class="note">⚠️ 本页面由程序按公开行情数据自动生成，仅供学习参考，不构成任何投资建议。股市有风险，入市需谨慎。<br>
选股逻辑：过滤 ST / 退市、流通市值与成交额过小的股票后，按趋势、活跃度、估值、规模综合打分，各市场取前 20 名。港股实时接口无 60 日涨跌幅，趋势分仅用当日涨跌幅。<br>
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

    df_a = fetch_with_retry("A股", ak.stock_zh_a_spot_em, ak.stock_zh_a_spot)
    df_hk = fetch_with_retry("港股", ak.stock_hk_spot_em, ak.stock_hk_spot)

    top_a = pick_rows(score_a(df_a), "A", TOP_N)
    top_hk = pick_rows(score_hk(df_hk), "HK", TOP_N)
    print(f"A 股候选 {len(df_a)} 只，入选 {len(top_a)} 只；港股候选 {len(df_hk)} 只，入选 {len(top_hk)} 只", flush=True)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
    os.makedirs(out_dir, exist_ok=True)
    html = HTML_HEAD.format(updated=updated)
    html += table_html("🇨🇳 A 股 Top 20", top_a, "A")
    html += table_html("🇭🇰 港股 Top 20", top_hk, "HK")
    html += HTML_FOOT
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "a_shares": top_a, "hk_shares": top_hk},
                  f, ensure_ascii=False, indent=2)
    print(f"已生成 docs/index.html（更新时间 {updated}）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
