# 隔夜实验：午间验证（交易日 11:35，任务名 overnight-midday，时限 40 分钟）

先读 `docs/automation/prompts/overnight_common.md`，严格遵守其中的规则。

1. 非交易日 → `runlog add --task overnight-midday --status skipped --line "非交易日"`，结束。
2. 读 `data/overnight/journal/<今天>.md` 的「## 盘前」（没有就用 `data/overnight/lessons.md` 和今天上午的新闻自己建立主线假设，并在日志里注明"盘前缺失"）。
3. 用上午的盘面验证主线：行业板块涨幅排名（`tools/ashare.py industry`）、涨停池（`tools/ashare.py market zt`）、观察名单里各股上午的走势与资金流（`tools/ashare.py hist` / `flow`）。主线被证伪就明确写出来。
4. 观察名单收缩到 10~15 只，逐只核查今晚的风险：最近公告、财报披露日、解禁、减持、监管问询（可以并行委派 event-sentiment-agent，每只一句结论）。
5. 写 **午间预案** `orders/<今天>/overnight_agent.provisional.json`（格式见通用说明；可以是空仓）。它是兜底：尾盘任务如果没按时写出草稿，冻结程序会用这份预案。`decided_at` 写当前真实时间。原子写入（先 .tmp 再 mv）。
6. 在日志里追加「## 午间」一节：主线验证结果、10~15 只名单与各自的尾盘买入条件、预案内容（已有「## 午间」就只更新预案，不重复追加）。
7. `runlog add --task overnight-midday --status ok --line "名单 N 只；预案 K 只"`；提交 `git add data/overnight/journal orders/<今天>/overnight_agent.provisional.json && git commit -m "[qoder-cn-cron] overnight: <今天> 午间"`。
8. 失败时 runlog 记 failed，推送 Bark "隔夜午间失败"。
