# 隔夜实验：盘前准备（交易日 08:50，任务名 overnight-premarket，时限 30 分钟）

先读 `docs/automation/prompts/overnight_common.md`，严格遵守其中的规则。

1. 判断交易日：`.venv/bin/python -c "from sim import calendar as c; print(c.is_trading_day(c.today()))"`。输出 False → `runlog add --task overnight-premarket --status skipped --line "非交易日"`，结束。
2. 读 `data/overnight/lessons.md` 和 `data/overnight/report.md`（若存在），以及昨天的 `data/overnight/journal/<昨日>.md` 复盘段。
3. 收集隔夜与盘前信息（注明来源）：美股三大指数与中概股、A50 期货、美元/人民币、大宗商品；昨晚到今早的重要政策与行业新闻；今天的重要公告（`tools/ashare.py notices`）；昨日龙虎榜（`tools/ashare.py lhb`）；昨日涨停池与连板高度（`tools/ashare.py market zt`）。
4. 形成 **今日主线假设**（1~3 条：哪些板块或题材今天可能走强、为什么），以及今晚的隔夜风险日历（今晚有哪些财报、海外数据、议息会议等）。
5. 列 20~40 只观察名单：每只写代码、名称、属于哪条主线、尾盘若出现什么形态就考虑买。可以委派 event-sentiment-agent / industry-agent 补证据，但整个任务要在 30 分钟内完成。
6. 写 `data/overnight/journal/<今天>.md` 的「## 盘前」一节（文件不存在就新建，标题 `# 隔夜实验日志 <今天>`；已有「## 盘前」就跳过本步，保证可重跑）。
7. `runlog add --task overnight-premarket --status ok --line "主线：…；观察 N 只"`，然后提交：`git add data/overnight/journal && git commit -m "[qoder-cn-cron] overnight: <今天> 盘前"`（无改动则跳过）。
8. 不推送 Bark（盘前任务出错才推送：标题 "隔夜盘前失败"）。
