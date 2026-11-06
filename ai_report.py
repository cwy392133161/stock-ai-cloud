# Optional LLM research layer. Uses only the existing ranked data; no news or filings are fetched.
import os, json, html, pathlib, requests
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parent
DOCS = ROOT / "docs"
def esc(v): return html.escape(str(v), quote=True)
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
    if not stocks: raise ValueError("没有有效选股数据")
    output = []
    for market, s in stocks:
        payload = {k:s.get(k) for k in ("代码","名称","最新价","涨跌幅","评分","趋势分","活跃分","估值分","换手率","入选原因","风险提示")}
        prompt = ("仅根据以下快照数据撰写中文研究，不要声称已读取实时新闻、财报或公司公告。"
                  "请用120字以内，分为优势、风险、待核实事项三个部分。"
                  "明确不预测确定涨跌、不给出买卖指令。数据缺失时说未知。"
                  "股票数据：" + json.dumps({"市场":market,**payload},ensure_ascii=False))
        response = requests.post(endpoint + "/chat/completions",
            headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"},
            json={"model":model,"temperature":0.2,"max_tokens":350,
                  "messages":[{"role":"system","content":"你是审慎的股票研究助手，只依据提供的数据，不得编造事实。"},{"role":"user","content":prompt}]},
            timeout=45)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content,str) or not content.strip(): raise ValueError("AI 返回空内容")
        output.append((market,s,content[:1600]))
    updated = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI 股票研究</title>',
             '<style>body{font-family:system-ui,-apple-system,sans-serif;background:#f7f8fa;color:#222;max-width:850px;margin:auto;padding:18px}article{background:white;padding:16px;border-radius:12px;margin:14px 0;white-space:pre-wrap;line-height:1.7}small{color:#666}</style>',
             '<h1>AI 股票研究（实验版）</h1><p><a href="./">返回选股榜</a></p>',
             '<small>行情快照：'+esc(data.get("updated","未知"))+'；AI 生成：'+esc(updated)+'（北京时间）</small>',
             '<p>仅分析原有榜单每个市场前5只；未接入新闻、财报和公告，AI 可能出错。研究内容不构成投资建议。</p>']
    for market,s,content in output:
        parts.append('<article><b>'+esc(market)+' · '+esc(s.get("名称",""))+' ('+esc(s.get("代码",""))+')</b><p>'+esc(content)+'</p></article>')
    parts.append('</html>')
    (DOCS/"ai.html").write_text("\n".join(parts),encoding="utf-8")
    print("AI 报告生成完成，股票数：",len(output))
if __name__ == "__main__":
    main()
