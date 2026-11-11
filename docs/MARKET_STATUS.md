# 行情失败与跳过运行的区别

2026-11-11 的行情更新失败后，仍保留 2026-11-10 17:58 北京时间行情。
后续定时任务在 UTC 2026-11-11 17:05（北京时间 11 月 12 日 01:05）执行，
因未到当地 17:00 发布窗口跳过选股及AI。原代码把 `market_state` 从 `failed`
覆盖为 `pre_close`，而AI待复核提示又优先覆盖了可见的行情故障信息。

核验来源：
- 实际失败任务：https://github.com/cwy392133161/stock-ai-cloud/actions/runs/43037179164
- 失败状态记录：https://github.com/cwy392133161/stock-ai-cloud/commit/c0305c7031b7165473a96f3bd137f5aa90b7ca4d
- 隔夜跳过任务：https://github.com/cwy392133161/stock-ai-cloud/actions/runs/43064627290
- 状态覆盖提交：https://github.com/cwy392133161/stock-ai-cloud/commit/c3eac5012b9c6990521e8859482877f0776deba7

修复后：
- `market_check_state` 记录本次时间窗口检查，`screening_outcome` 记录本次选股步骤。
- `market_state` 保留尚未恢复的失败；`last_screening_outcome` 与
  `last_screening_recorded_utc` 不因跳过运行而冒充新检查。
- `market_failure_snapshot` 保留失败恢复的快照基准；仅更晚的成功选股可以清除故障。
- 首页与研究页独立显示行情状态和AI复审状态，旧行情时间与旧研究正文不改写。
- 普通日常任务在故障未恢复时保持失败；仅生成报告的任务独立判断报告结果。
- `recorded_utc` 只是状态写入时间，不代表行情刷新或AI重新生成。

本次发布仅恢复有日志证据的故障记录与警告，不证明行情接口已恢复，不产生新行情，
也不生成新的AI报告。定时任务可能延迟；工作日/17:00检查仍不是完整交易所假日日历。
