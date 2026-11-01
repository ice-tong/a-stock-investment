# 隔夜实验：尾盘决策（交易日 14:40，任务名 overnight-close，**必须在 14:52 前写完草稿**）

先读 `docs/automation/prompts/overnight_common.md`，严格遵守其中的规则。时间是本任务最大的约束：14:54 冻结程序会读你的草稿，读不到就用午间预案，再没有就记为"漏跑"。

1. 非交易日 → `runlog add --task overnight-close --status skipped --line "非交易日"`，结束。
2. **先保底（14:44 前完成）**：读 `orders/<今天>/overnight_agent.provisional.json`；若存在，把它复制成 `overnight_agent.draft.json`，并把 `decided_at` 改成当前时间（原子写入）。这样后面就算超时，也有一份尾盘草稿。
3. 读 `data/overnight/cand/<今天>.md`（14:38 的快照、量化前 40 与市场情绪）。如果文件不存在，运行一次 `.venv/bin/python -m sim on-scan`（约 40 秒）。再读日志「## 午间」的名单与尾盘条件、`data/overnight/lessons.md` 的要点。
4. 快速决策（不委派子代理）：
   - 午间名单里，哪些尾盘条件已经满足；量化前 10 里有没有新冒出来、逻辑说得通的票。
   - 对拟买的每只，最多用 1~2 条命令核查（如 `tools/ashare.py notices <代码>`、`news`），确认今晚没有已知利空。
   - 结合市场情绪（炸板率、跌停家数）决定今晚整体做不做：情绪差可以空仓或减量。
5. **14:50~14:52** 写最终 `orders/<今天>/overnight_agent.draft.json`（覆盖保底版，原子写入），`decided_at` 写当前真实时间。量化前 10 里不选的票，挑出你有明确理由的写进 `vetoed`。
6. 在日志里追加「## 尾盘决策」一节：最终选择、每只的逻辑与 p_win、否决理由、今晚的隔夜风险。
7. `runlog add --task overnight-close --status ok --line "选 N 只：…；否决 K 只"`；提交 `git add data/overnight/journal orders/<今天>/overnight_agent.draft.json && git commit -m "[qoder-cn-cron] overnight: <今天> 尾盘决策"`。
8. 如果写草稿时已经过了 14:53：不要再写，runlog 记 failed（"尾盘超时，冻结将用午间预案"），推送 Bark "隔夜尾盘超时"。
