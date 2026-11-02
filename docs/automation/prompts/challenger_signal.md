# 挑战者月末信号（S-002 / S-003）——交易日 21:15

你是 S-002 / S-003 挑战者账户的月末信号任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
信号只在每月最后一个交易日真正运行，其余日子程序自己打印「不是月末最后一个交易日，跳过」。
必须排在 20:50 的结算之后（S-003 的前 30 名缓冲要读结算后的持仓）。

1. 先检查 `.venv/bin/python tools/runlog.py` 记录或两个账户的 `state.json`：若今天是交易日而 `last_settled` 不是今天，先按 `docs/automation/prompts/challenger_settle.md` 补跑结算，再继续。
2. 依次运行（**S-003 必须在 S-002 之后**，它复用 S-002 当天下载的日线缓存 `data/s002/cache/`）：
   - `.venv/bin/python tools/s002.py signal`
   - `.venv/bin/python tools/s002.py --profile s003 signal`

   S-002 这一步要全市场取数，约 5–10 分钟，不要中途打断。
3. 两条输出都含「不是月末最后一个交易日，跳过」或「订单已冻结，跳过」 → `runlog add --task challenger-signal --status skipped --line "非月末"`，结束，不推送、不提交。
4. 成功 → `runlog add --task challenger-signal --status ok --line "<s002 那行'信号：可投资格 … 入选 N 只'原文>" --line "<s003 那行原文>"`；推送 `.venv/bin/python tools/notify.py --title "挑战者月末信号" --body "S-002 入选 N 只，S-003 入选 N 只，次日开盘换仓"`。
5. 报错（取数失败、日线失败超过 5%、数据未更新等） → 等 10 分钟重试一次；仍失败 runlog 记 `failed`，推送 Bark「挑战者信号失败：<账户>：<错误摘要>」。**当晚 23:30 前**任何会话都可以手动补跑同一命令；过了次日 09:15 就不要再补，本月按不换仓处理并在 runlog 注明。
6. 成功后提交：`git add data/s002 data/s003`，`git commit -m "[qoder-cn-cron] challenger: <日期> 月末信号"`，`git push`。只 add 这两个路径。
7. 已冻结的 `data/s002/orders/*.json`、`data/s003/orders/*.json` 不得修改，**不要加 `--force`**。绝不手工修改 `accounts/`、`orders/`、`config/`。
