# 挑战者月末信号（S-003）——交易日 21:15

你是 S-003 挑战者账户的月末信号任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
信号只在每月最后一个交易日真正运行，其余日子程序自己打印「不是月末最后一个交易日，跳过」。
必须排在 20:50 的结算之后（前 30 名缓冲要读结算后的持仓）。（S-002 已于 2026-11-02 删除，不要再跑或重建它。）

1. 先看 `accounts/challenger/s003/state.json`：若今天是交易日而 `last_settled` 不是今天，先按 `docs/automation/prompts/challenger_settle.md` 补跑结算，再继续。
2. 运行 `.venv/bin/python tools/s002.py signal`。月末这一步要全市场取数（日线缓存 `data/s003/cache/`），约 5–10 分钟，不要中途打断。
3. 输出含「不是月末最后一个交易日，跳过」或「订单已冻结，跳过」 → `runlog add --task challenger-signal --status skipped --line "非月末"`，结束，不推送、不提交。
4. 成功 → `runlog add --task challenger-signal --status ok --line "<输出里'信号：可投资格 … 入选 N 只'那一行原文>"`；推送 `.venv/bin/python tools/notify.py --title "挑战者月末信号" --body "S-003 入选 N 只，次日开盘换仓"`。
5. 报错（取数失败、日线失败超过 5%、数据未更新等） → 等 10 分钟重试一次；仍失败 runlog 记 `failed`，推送 Bark「挑战者信号失败：<错误摘要>」。**当晚 23:30 前**任何会话都可以手动补跑同一命令；过了次日 09:15 就不要再补，本月按不换仓处理并在 runlog 注明。
6. 成功后重建看板并尽力发布（月末才冻结新订单，页面上的「最近订单」要跟上当天）：
   a. `.venv/bin/python tools/build_dashboard.py`。
   b. 若当前会话存在 sites 插件 MCP 工具：新 UUID 作 actionId，`prepare_site` 传 `projectRoot="/Users/yancong/agent-workspace/a-stock-investment"`、`webDirectory="dist"`、`projectId="01a1728a-617c-708f-99f9-03e5934f09f3"`；`descriptorStatus=pending` 就原样重试到 `written`；`get_publish_status` 至 `canPublish=true` 后 `publish_site`（同一 actionId），确认 `published=true`。工具缺失或失败最多重试一次，放弃时在汇报里注明「看板已本地更新，站点待同步发布」。
7. 成功后提交：`git add data/s003 web/index.html dist/index.html .A股模拟交易状态看板.qoder.site`，`git commit -m "[qoder-cn-cron] challenger: <日期> 月末信号，看板已同步"`（站点未发布成功则去掉「，看板已同步」），`git push`。只 add 这几个路径。
8. 已冻结的 `data/s003/orders/*.json` 不得修改，**不要加 `--force`**。绝不手工修改 `accounts/`、`orders/`、`config/`。
