# A 股 + 港股 AI 选股榜（免费云端版）

每天工作日北京时间 17:10 自动跑一次：GitHub Actions 用 AKShare 免费接口拉取
A 股 + 港股行情，按趋势 / 活跃度 / 估值 / 规模多因子打分，各市场取 Top 20，
生成中文网页，通过 GitHub Pages 发布，iPhone 用 Safari 就能看。

> ⚠️ 这是**规则化多因子评分**，不是大语言模型分析；结果仅供学习参考，
> **不构成任何投资建议**。股市有风险，入市需谨慎。

## 部署步骤（5 步）

### 第 1 步：创建 GitHub 仓库

打开 https://github.com/new ，仓库名填 `stock-ai-cloud`，选 **Public**（免费
Pages 要求公开仓库；选股结果将公开可见），点 **Create repository**。

### 第 2 步：上传项目文件

把本项目所有文件上传到仓库，保留文件夹结构：

```
stock-ai-cloud/
├── picker.py                        选股脚本
├── ai_report.py                     AI 深度分析（可选，需 DeepSeek 密钥）
├── requirements.txt                 依赖（akshare）
├── README.md                        本说明
├── docs/
│   ├── index.html                   榜单网页（首次运行后自动覆盖）
│   ├── data.json                    榜单数据（供 AI 报告读取）
│   └── ai.html                      AI 深度分析页（开启后自动生成）
└── .github/
    └── workflows/
        └── daily.yml                每天 17:10 自动运行
```

网页上传：仓库页面点 **Add file → Upload files**，把文件拖进去，点
**Commit changes**。注意 `.github` 是隐藏文件夹，上传时要把整个
`.github/workflows/daily.yml` 路径保留好。

### 第 3 步：启用 GitHub Pages

仓库 **Settings → Pages → Build and deployment**，Source 选
**Deploy from a branch**，Branch 选 `main`，文件夹选 `/docs`，点 Save。

### 第 4 步：首次手动运行

仓库 **Actions → Daily stock screening and Pages → Run workflow**，
等几分钟。出现绿色 ✅ 表示成功；红色 ❌ 点进去看失败步骤的日志。

### 第 5 步：iPhone 查看

成功后访问：`https://你的GitHub用户名.github.io/stock-ai-cloud/`

iPhone Safari 打开后点 **分享 → 添加到主屏幕**，以后像 App 一样打开。

## 选股逻辑

- 过滤：ST / 退市、流通市值过小（A 股 < 20 亿、港股 < 20 亿港币）、
  当日成交额过小（A 股 < 5000 万、港股 < 1000 万港币）、停牌无价的股票
- 打分：趋势 40% / 活跃度 25% / 估值 20% / 规模 15%，用百分位排名，
  各市场分别取前 20 名；港股与 A 股用同一套打分
- 数据源：直调东方财富免费行情接口（慢速分页、多台服务器轮换），
  失败时依次兜底 AKShare 东财源、新浪源；免费接口对云端共享 IP
  偶有限流，单次运行失败属于正常现象，重跑通常能恢复
- 每只入选股票附中文入选原因和风险提示（如追高、情绪过热、估值偏高）

## AI 深度分析（可选，二期）

选股榜本身仍是规则化打分（Top 20 不变）。开启后，每天在选股结束时，
`ai_report.py` 只取榜单每边前 5 只（共 10 只），调用 DeepSeek 大模型，
按"优势 / 风险 / 待核实事项"各写一段约 120 字的中文短评，生成
`docs/ai.html`，榜单页顶部会出现"查看 AI 深度分析"链接。

注意：AI 只看得到当天的行情快照（价格、涨跌幅、评分、换手率、入选原因），
**不读新闻、财报和公告**，也不会预测涨跌、给出买卖指令；内容可能出错，
仅供参考，不构成投资建议。

### 开启方法（4 项配置）

去仓库 **Settings → Secrets and variables → Actions**：

| 配置项 | 放在哪里 | 值 |
| --- | --- | --- |
| `AI_API_KEY` | Secrets（点 New repository secret） | 去 platform.deepseek.com 注册并充值后创建的 API 密钥 |
| `AI_ENABLED` | Variables（点 New repository variable） | `true` |
| `AI_BASE_URL` | Variables | `https://api.deepseek.com` |
| `AI_MODEL` | Variables | `deepseek-flash`（最便宜，约 ¥0.033 / 百万 Tokens 起） |

配好后去 **Actions → Daily stock screening and Pages → Run workflow**
手动跑一次，等几分钟，打开榜单页看链接，或直接访问
`https://你的GitHub用户名.github.io/stock-ai-cloud/ai.html`。

费用：每天 10 次调用，按 deepseek-flash 计价，每月约 ¥5–10 元；
请在 DeepSeek 后台自行设置用量上限，避免超支。
关闭：把 `AI_ENABLED` 改回 `false` 再跑一次工作流即可；
关闭后榜单不受影响，旧的 ai.html 会保留到下一次运行。

## 常见问题

**定时任务没跑 / 延迟？**
GitHub 官方说明：定时工作流可能延迟；公开仓库长期没有活动时，定时
工作流可能被停用。本项目每天成功运行都会提交一次结果，本身就是活动，
一般不会被停用。节假日无行情时，页面会保留上一次的结果，请看页面上的
更新时间。

**运行失败，日志说接口限流 / 拉不到数据？**
免费行情接口（东方财富 / 新浪）会对云端共享 IP 限流或拦截。脚本已内置
重试（东方财富失败后等待 120 秒、300 秒再试，仍失败改试新浪源）。
如果还是失败，等一段时间后在 Actions 页面点 **Re-run jobs** 重新跑一次。
当天多次失败是正常的，第二天通常会恢复。

**页面显示的还是旧结果？**
先看页面顶部的更新时间。工作日 17:10 运行，若失败则保留上一次结果，
属于正常现象。
