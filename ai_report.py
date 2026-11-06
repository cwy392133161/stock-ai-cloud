# Optional research layer for the existing ranked stocks.
import os, json, html, pathlib, requests
import re
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from finance import fetch_financial
from hk_products import stock_directory

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"
def esc(v): return html.escape(str(v), quote=True)

def display_metric(value):
    """Group digits for display without rounding the source value or changing units."""
    number, separator, unit = str(value).partition(" ")
    if separator and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", number):
        return f"{Decimal(number):,f} {unit}"
    return str(value)


def finance_html(finance):
    lines = []
    for key, value in finance.items():
        if isinstance(value, dict):
            for label, detail in value.items():
                prefix = "缺失 · " if key == "缺失" else ""
                text = display_metric(detail) if key == "指标" else detail
                lines.append(esc(prefix + label) + "：" + esc(text))
        else:
            lines.append(esc(key) + "：" + esc(value))
    return '<small>财务数据<br>' + '<br>'.join(lines) + '</small>'


def finance_summary(market, reports):
    products = sum(f.get("状态") == "公司财务分析不适用" for f in reports)
    applicable = len(reports) - products
    covered = [f for f in reports if f.get("指标") and f.get("状态") != "公司财务分析不适用"]
    partial = sum(bool(f.get("缺失")) or f.get("状态") == "部分可用" for f in covered)
    message = (f"{market}：{len(covered)}/{applicable} 家公司取得指标"
               f"（完整 {len(covered) - partial}，部分 {partial}，无可用指标 {applicable - len(covered)}），"
               f"产品不适用 {products} 只")
    return message, bool(applicable and not covered)


def render_report(data, output, updated, archived_codes=()):
    summaries = [finance_summary(market, [f for m, _, _, f in output if m == market])[0]
                 for market in ("A股", "港股")]
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI 股票研究</title>',
             '<style>body{font-family:system-ui,-apple-system,sans-serif;background:#f7f8fa;color:#222;max-width:850px;margin:auto;padding:18px}article{background:white;padding:16px;border-radius:12px;margin:14px 0;white-space:pre-wrap;line-height:1.7}small{color:#666}</style>',
             '<h1>AI 股票研究（实验版）</h1><p><a href="./">返回选股榜</a></p>',
             '<small>行情快照：'+esc(data.get("updated","未知"))+'；AI 生成：'+esc(updated)+'（北京时间）</small>',
             '<p>每天分析 A 股前5只、港股前5只，共10只；已尝试接入东方财富财务指标（第三方，按报告期展示）；未接入公告原文，所有财务数字仍需以正式披露核实。AI 可能出错，研究内容不构成投资建议。</p>']
    parts.append('<section><h2>财务数据覆盖</h2><ul>' +
                 ''.join('<li>' + esc(summary) + '</li>' for summary in summaries) + '</ul></section>')
    for market,s,content,finance in output:
        code = str(s.get("代码", ""))
        # 可直接核对的交易所披露网站，不假定已取得公司财报。
        if market == "A股":
            source = "https://www.cninfo.com.cn/new/index"
            source_name = "巨潮资讯：检索公司公告和财报"
        else:
            source = "https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=zh"
            source_name = "港交所披露易：检索公司公告和财报"
        parts.append('<article><b>'+esc(market)+' · '+esc(s.get("名称",""))+' ('+esc(code)+')</b>')
        query_code = re.sub(r"^(?:sh|sz|bj)(?=[0-9]{6}$)", "", code, flags=re.I) if market == "A股" else code
        parts.append('<p><a href="'+esc(source)+'" target="_blank" rel="noopener noreferrer">'+esc(source_name)+'</a>（请按股票代码 '+esc(query_code)+' 查询，核对报告期和披露日期）</p>')
        parts.append(finance_html(finance))
        narrative = '<p>' + esc(content) + '</p>'
        if (market, code) in archived_codes:
            narrative = ('<details><summary>原 AI 分析（生成后已更新财务数据）</summary>'
                         + narrative + '</details>')
        parts.append(narrative + '</article>')
    parts.append('</html>')
    return "\n".join(parts)


def main():
    key = os.getenv("AI_API_KEY", "").strip()
    model = os.getenv("AI_MODEL", "").strip()
    endpoint = os.getenv("AI_BASE_URL", "").strip().rstrip("/")
    if not (key and model and endpoint):
        print("AI 未配置：跳过 AI 报告，原有选股不受影响")
        return
    if not endpoint.startswith("https://") or "@" in endpoint:
        raise ValueError("AI_BASE_URL 必须是可信的 HTTPS API 地址")
    data = json.loads((DOCS / "data.json").read_text(encoding="utf-8"))
    stocks = [(market, x) for market, field in (("A股","a_shares"),("港股","hk_shares")) for x in data.get(field, [])[:5]]
    if len(stocks) != 10: raise ValueError(f"预期 A 股和港股各5只，共10只；实际只有 {len(stocks)} 只，拒绝发布不完整报告")
    output = []
    snapshots = []
    directory = stock_directory()
    for market, s in stocks:
        finance = fetch_financial(market, s.get("代码", ""), s.get("名称", ""), data["updated"][:10],
                                  directory=directory)
        snapshots.append((market, s, finance))
        print(f"财务查询 {market} {s.get('代码', '')}：{finance['状态']}，"
              f"{len(finance.get('指标', {}))} 项，报告期 {finance.get('报告期', '—')}", flush=True)
    for market in ("A股", "港股"):
        summary, all_missing = finance_summary(market, [f for m, _, f in snapshots if m == market])
        # Fixed market names and counts only in Actions annotations.
        prefix = "::warning::" if all_missing and os.getenv("GITHUB_ACTIONS") == "true" else ""
        print(prefix + summary, flush=True)
    for market, s, finance in snapshots:
        payload = {k:s.get(k) for k in ("代码","名称","最新价","涨跌幅","评分","趋势分","活跃分","估值分","换手率","入选原因","风险提示")}
        prompt = ("你只能使用下列快照数据，不得补充未提供的财务数字、行业事实、新闻、公告、财报或市盈率。提供的财务指标仅来自第三方数据接口，必须标明报告期，不能当作最新实时数据；缺失则明确未知。"
                  "标明公司财务不适用的产品只分析产品行情，勿讨论公司财务。只采用标为可用的指标并保留币种和期间，勿解读异常 ROE 为经营改善。若财务指标已可用，必须引用对应报告期和至少一个真实财务数值，不得沿用旧结论或声称财务接口无效。归母净利润为负必须明确写亏损，即使同比变化为正也不得称已盈利；利润与经营现金流方向相反、现金流金额明显大于营收时必须提醒核查原因，不得臆测原因。"
                  "请用120字以内，分为优势、风险、待核实事项三个部分。"
                  "必须明确：评分是程序的相对排名分数，不代表收益概率或预测。禁止将估值分当成市盈率。"
                  "明确不预测确定涨跌、不给出买卖指令。数据缺失时说未知。"
                  "股票数据：" + json.dumps({"市场":market,**payload,"财务指标":finance},ensure_ascii=False))
        # deepseek-flash 默认开思考模式：先写推理再给正文。max_tokens 太小会被推理
        # 占满（实测 350 个 token 里推理占了 290 个），正文被截断甚至为空而报错。
        # 给足 2000 token，空内容时重试一次，仍为空则用推理文本兜底。
        content = ""
        for attempt in range(2):
            response = requests.post(endpoint + "/chat/completions",
                headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
                json={"model":model,"temperature":0.2,"max_tokens":2000,
                      "messages":[{"role":"system","content":"你是审慎的股票研究助手，只依据提供的数据，不得编造事实。"},{"role":"user","content":prompt}]},
                timeout=120)
            response.raise_for_status()
            msg = response.json()["choices"][0]["message"]
            content = msg.get("content") or ""
            if content.strip():
                break
            # Only publish final model response, not reasoning.
            print(f"AI 返回空内容，重试 {attempt+1}/2：{market} {s.get('名称','')}", flush=True)
        if not content.strip():
            raise ValueError("AI 返回空内容")
        output.append((market,s,content[:1600],finance))
    if len(output) != 10: raise ValueError("AI 报告数量不足10只，保留已有报告")
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    (DOCS/"ai.html").write_text(render_report(data, output, updated), encoding="utf-8")
    print("AI 报告生成完成，股票数：",len(output))
if __name__ == "__main__":
    main()
