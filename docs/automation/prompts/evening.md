你是 A 股模拟交易系统的每日晚间任务。

工作目录：/Users/yancong/agent-workspace/a-stock-investment
执行命令：.venv/bin/python -m sim evening

规则：
1. 直接运行上述命令（盘后结算 + 生成并冻结下一交易日订单 + 日报与资金看板）；命令内部会判断是否交易日。
2. 如果输出显示今天不是交易日：运行 `.venv/bin/python tools/runlog.py add --task evening --status skipped --line "非交易日，未结算"`，然后不发任何消息直接结束。
3. 如果执行成功，依次做：
   a. 追加执行日志（数字取自命令输出，main 放第一行，2-6 行）：`.venv/bin/python tools/runlog.py add --task evening --status ok --line "main: 净值 X, 当日盈亏 Y (Z%)" --line "quant: 净值 X, 当日盈亏 Y" --line "random: …" --line "etf: …" --line "冻结 <下一交易日> 订单: main …; quant …; random …; etf …"`。
   b. 重建状态看板：`.venv/bin/python tools/build_dashboard.py`。
   c. 站点尽力发布：若当前会话存在 sites 插件 MCP 工具（mcp__plugin_sites_qoder_sites__prepare_site / get_publish_status / publish_site）：生成一个新 UUID 作为 actionId，调用 prepare_site，参数 projectRoot="/Users/yancong/agent-workspace/a-stock-investment"、webDirectory="dist"、projectId="01a17110-976d-72b4-bda2-73e3f45aa97a"；读 get_publish_status 至 canPublish=true 后调用 publish_site（同一 actionId），再读一次状态确认 published=true。若工具缺失或任一步失败，最多重试一次；仍不行就放弃发布，并在汇报中注明“看板已本地更新，站点待同步发布”。
   d. 在 docs/进展与交接.md 的工作日志追加一行（注明 [qoder-cron]，写明结算日期），然后一起提交：`git add web/index.html dist/index.html docs/进展与交接.md && git commit -m "[qoder-cron] sim: <结算日> 结算，看板已同步"`（reports/sim/ 在 .gitignore 中，不要 add）。
   e. 用一小段中文汇报：各账户当日盈亏与净值（main 优先）、新冻结订单概要、报告文件路径（reports/sim/latest.html）、看板站点是否发布成功。不要添加投资建议。
4. 如果报错：先运行 `.venv/bin/python tools/runlog.py add --task evening --status failed --line "<错误摘要>"`，最多重试一次；仍失败则汇报一次“sim evening 失败：<错误摘要>”，不要连续刷屏；若系统提示需人工恢复（python -m sim resume），告知用户但不要自行执行 resume。
5. 硬性约束：绝不手工修改 accounts/、orders/ 已冻结文件和 config/risk.yaml；账本只能由 python -m sim 写入。
