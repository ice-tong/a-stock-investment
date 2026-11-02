# 挑战者结算（S-003）——交易日 20:50

你是 S-003 挑战者账户的结算任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
S-003 是纸面账户（`accounts/challenger/s003/`），与 main、隔夜、PEAD 完全隔离。（S-002 已于 2026-11-02 删除，不要再跑或重建它。）

1. 运行 `.venv/bin/python tools/s002.py settle`（工具文件名沿用 s002，现在只服务 S-003）。
2. 输出含「非交易日，跳过」 → `.venv/bin/python tools/runlog.py add --task challenger-settle --status skipped --line "非交易日"`，结束，不推送。
3. 输出含「已结算，跳过」 → runlog 记 `skipped`，`--line "已结算"`，结束。
4. 成功 → `runlog add --task challenger-settle --status ok --line "<输出里'结算：…净值…'那一行原文>"`。
5. 报错含「数据未更新」或「取不到价格，判定为数据源故障」 → 等 10 分钟重试一次；仍失败 runlog 记 `failed`，推送 `.venv/bin/python tools/notify.py --title "挑战者结算失败" --body "S-003：<错误摘要>"`。程序是幂等的，明天补跑 `settle --date <今天>` 即可。
6. 其他报错 → 重试一次，仍失败同第 5 步记 failed 并推送。
7. 有账本变化时提交：`git add accounts/challenger/s003 data/s003`，`git commit -m "[qoder-cn-cron] challenger: <日期> 结算"`，然后 `git push`（push 失败不算任务失败，记在 runlog 的 line 里）。**只 add 这两个路径**，不要 `git add -A`。
8. 绝不手工修改 `accounts/`、`orders/`、`config/`、`data/s003/orders/`。
