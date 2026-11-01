# 隔夜实验：盘后复盘（交易日 16:00，任务名 overnight-review，时限 30 分钟）

先读 `docs/automation/prompts/overnight_common.md`，严格遵守其中的规则。复盘时可以看结果数据。

1. 非交易日 → `runlog add --task overnight-review --status skipped --line "非交易日"`，结束。
2. 确认 15:30 的结算已完成：`.venv/bin/python -m sim on-status`，三个账户的"最近结算"应为今天。没有完成就先运行 `.venv/bin/python -m sim on-settle`；若提示数据未更新，在日志注明"结算待 20:30 补跑"，只复盘能复盘的部分。
3. 读 `data/overnight/scores.csv` 里 `exit_date` 为今天的行（即昨晚买、今早卖的结果，含 agent_veto 反事实）和 `data/overnight/report.md`。
4. 逐笔对照昨晚的预测（`orders/<昨日>/overnight_agent.json` 的 p_win / exp_gap / thesis / kill）与结果：猜对的原因、猜错的原因、kill 条件是否出现过、否决是否正确。与 quant / random 当晚的结果比较。
5. 在 `data/overnight/journal/<今天>.md` 追加「## 盘后复盘」一节（已有则跳过）：昨晚三组结果、逐笔归因、今晚（今天尾盘已买）的持仓与风险。
6. 更新 `data/overnight/lessons.md`：只记 **可证伪的规律**，格式 `- [类型] 规律描述（样本 N，胜率 x%，均值 y%，首次记录 日期，最近更新 日期）`。样本 < 20 的放在「观察中」一节，≥ 20 的才放进「已验证」；被新数据推翻的移到「已推翻」。不要写空洞的道理。
7. `runlog add --task overnight-review --status ok --line "昨晚 agent N 笔均值 x%；quant y%；random z%"`；提交 `git add data/overnight && git commit -m "[qoder-cn-cron] overnight: <今天> 复盘"`。
8. Bark 推送：标题 "隔夜实验 <今天>"，正文三组昨晚的结果各一行 + 累计进度（agent 已平仓 N/200 笔）。
