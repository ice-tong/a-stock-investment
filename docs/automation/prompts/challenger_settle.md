# 挑战者结算（S-003）——交易日 20:50

你是 S-003 挑战者账户的结算任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
S-003 是纸面账户（`accounts/challenger/s003/`），与 main、隔夜、PEAD 完全隔离。（S-002 已于 2026-11-02 删除，不要再跑或重建它。）

1. 运行 `.venv/bin/python tools/s002.py settle`（工具文件名沿用 s002，现在只服务 S-003）。
2. 输出含「非交易日，跳过」 → `.venv/bin/python tools/runlog.py add --task challenger-settle --status skipped --line "非交易日"`，结束，不推送。
3. 输出含「已结算，跳过」 → runlog 记 `skipped`，`--line "已结算"`，结束。
4. 成功 → `runlog add --task challenger-settle --status ok --line "<输出里'结算：…净值…'那一行原文>"`。
5. 报错含「数据未更新」或「取不到价格，判定为数据源故障」 → 等 10 分钟重试一次；仍失败 runlog 记 `failed`，推送 `.venv/bin/python tools/notify.py --title "挑战者结算失败" --body "S-003：<错误摘要>"`。程序是幂等的，明天补跑 `settle --date <今天>` 即可。
6. 其他报错 → 重试一次，仍失败同第 5 步记 failed 并推送。
7. **有账本变化时**（即第 4 步走了成功分支，第 2、3 步跳过的不算）重建状态看板并尽力发布，让站点上的挑战者区块与今天结算对齐：
   a. `.venv/bin/python tools/build_dashboard.py`（同时更新 `web/index.html` 与 `dist/index.html`；挑战者区块读 `accounts/challenger/s003`，runlog 已在第 4 步写入，会一并进页面）。
   b. 若当前会话存在 sites 插件 MCP 工具（`mcp__plugin_sites_qoder_sites__prepare_site` / `get_publish_status` / `publish_site`）：生成一个新 UUID 作为 actionId，调用 `prepare_site`，参数 `projectRoot="/Users/yancong/agent-workspace/a-stock-investment"`、`webDirectory="dist"`、`projectId="01a1728a-617c-708f-99f9-03e5934f09f3"`；返回 `descriptorStatus=pending` 就保持同一 actionId 与参数原样重试，直到 `written`；读 `get_publish_status` 至 `canPublish=true` 后调用 `publish_site`（同一 actionId），再确认 `published=true`。
   c. 工具缺失或任一步失败，最多重试一次；仍不行就放弃发布，在汇报中注明「看板已本地更新，站点待同步发布」。发布失败不影响结算本身，不回滚账本。
8. 有账本变化时提交：`git add accounts/challenger/s003 data/s003 web/index.html dist/index.html .A股模拟交易状态看板.qoder.site`，`git commit -m "[qoder-cn-cron] challenger: <日期> 结算，看板已同步"`（站点未发布成功则去掉「，看板已同步」），然后 `git push`（push 失败不算任务失败，记在 runlog 的 line 里）。**只 add 这几个路径**，不要 `git add -A`。
9. 绝不手工修改 `accounts/`、`orders/`、`config/`、`data/s003/orders/`。
