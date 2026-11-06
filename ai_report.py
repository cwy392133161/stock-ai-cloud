# Optional LLM research layer. Uses only the existing ranked data; no news or filings are fetched.
import os, json, html, pathlib, requests, re
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"
def esc(v): return html.escape(str(v), quote=True)

def fetch_financial(market, code):
    """Best-effort AKShare finance indicators; never infer missing figures."""
    try:
        import akshare as ak
        if market == "A股":
            prefix = "SH" if str(code).startswith(("6","9")) else "BJ" if str(code).startswith(("4","8")) else "SZ"
            df = ak.stock_financial_analysis_indicator_em(symbol=prefix+str(code).zfill(6), indicator="按报告期")
        else:
            df = ak.stock_financial_hk_analysis_indicator_em(symbol=str(code).zfill(5), indicator="报告期")
        if df is None or df.empty:
            return {"状态":"未获取到财务数据"}
        date_cols = [c for c in df.columns if any(t in str(c).upper() for t in ("REPORT_DATE","REPORTDATE","日期","周期","报告期"))]
        if not date_cols:
            return {"状态":"未识别报告期，财务数据不采用"}
        date_col = date_cols[0]
        import pandas as pd
        dates = pd.to_datetime(df[date_col], errors="coerce")
        valid = df.loc[dates.notna()].copy()
        if valid.empty:
            return {"状态":"无有效报告期"}
        valid["_period"] = dates.loc[valid.index]
        valid = valid.sort_values("_period", ascending=False)
        row = valid.iloc[0]
        if row["_period"].date() > datetime.now(timezone(timedelta(hours=8))).date():
            return {"状态":"报告期晚于当前日期，拒绝使用"}
        result = {"来源":"东方财富（经 AKShare 获取，非交易所原始披露）","报告期":str(row["_period"].date()),"说明":"指标可能为累计值、单季值或特定币种，未核实口径时不得跨期比较"}
        aliases = {
            "营业收入同比增长率(%)":["TOTALOPERATEREVETZ","营业收入同比增长率","营业总收入同比增长率"],
            "归母净利润同比增长率(%)":["PARENTNETPROFITTZ","归母净利润同比增长率","归属母公司净利润同比增长率"],
            "净资产收益率(%)":["ROEJQ","净资产收益率(%)","加权净资产收益率(%)"],
            "资产负债率(%)":["ZCFZL","资产负债率(%)"],
            "每股收益":["基本每股收益(元)","基本每股收益","EPSJB"]
        }
        for label, names in aliases.items():
            for name in names:
                if name in row.index:
                    value = row[name]
                    if pd.notna(value) and str(value).strip() not in ("","--","-","nan"):
                        result[label] = str(value)[:40]
                    break
        if len(result) == 4:
            result["状态"] = "未识别可用财务指标"
        return result
    except Exception as exc:
        print(f"财务数据不可用 {market} {code}: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return {"状态":"财务接口暂不可用，未使用未经核实的数字"}

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
    # 仅展示可核对的官方财务信息入口，不把未经验证的接口数据当作事实。
    for market, s in stocks:
        payload = {k:s.get(k) for k in ("代码","名称","最新价","涨跌幅","评分","趋势分","活跃分","估值分","换手率","入选原因","风险提示")}
        finance = fetch_financial(market, s.get("代码", ""))
        prompt = ("你只能使用下列快照数据，不得补充未提供的财务数字、行业事实、新闻、公告、财报或市盈率。提供的财务指标仅来自第三方数据接口，必须标明报告期，不能当作最新实时数据；缺失则明确未知。"
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
        if not content:
            raise ValueError("AI 返回空内容")
        output.append((market,s,content[:1600],finance))
    if len(output) != 10: raise ValueError("AI 报告数量不足10只，保留已有报告")
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI 股票研究</title>',
             '<style>body{font-family:system-ui,-apple-system,sans-serif;background:#f7f8fa;color:#222;max-width:850px;margin:auto;padding:18px}article{background:white;padding:16px;border-radius:12px;margin:14px 0;white-space:pre-wrap;line-height:1.7}small{color:#666}</style>',
             '<h1>AI 股票研究（实验版）</h1><p><a href="./">返回选股榜</a></p>',
             '<small>行情快照：'+esc(data.get("updated","未知"))+'；AI 生成：'+esc(updated)+'（北京时间）</small>',
             '<p>每天分析 A 股前5只、港股前5只，共10只；已尝试接入东方财富财务指标（第三方，按报告期展示）；未接入公告原文，所有财务数字仍需以正式披露核实。AI 可能出错，研究内容不构成投资建议。</p>']
    for market,s,content,finance in output:
        code = str(s.get("代码", ""))
        # 可直接核对的交易所披露网站，不假定已取得公司财报。
        if market == "A股":
            source = "https://www.cninfo.com.cn/new/index"
            source_name = "巨潮资讯：检索公司公告和财报"
        else:
            source = "https://www.hkexnews.hk/index_c.htm"
            source_name = "港交所披露易：检索公司公告和财报"
        parts.append('<p><a href="'+esc(source)+'" target="_blank" rel="noopener noreferrer">'+esc(source_name)+'</a>（请按股票代码 '+esc(code)+' 查询，核对报告期和披露日期）</p>')
        finance_info = "；".join(esc(k)+": "+esc(v) for k,v in finance.items())
        parts.append('<small>财务数据：'+finance_info+'</small>')
        parts.append('<article><b>'+esc(market)+' · '+esc(s.get("名称",""))+' ('+esc(s.get("代码",""))+')</b><p>'+esc(content)+'</p></article>')
    parts.append('</html>')
    (DOCS/"ai.html").write_text("\n".join(parts),encoding="utf-8")
    print("AI 报告生成完成，股票数：",len(output))
if __name__ == "__main__":
    main()
