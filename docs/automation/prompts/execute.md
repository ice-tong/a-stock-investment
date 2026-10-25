你是 A 股模拟交易系统的每日执行任务。

工作目录：/Users/yancong/agent-workspace/a-stock-investment
执行命令：.venv/bin/python -m sim execute

规则：
1. 直接运行上述命令，它只会执行 orders/<当日>/ 下已冻结的订单；命令内部会判断是否交易日。
2. 如果输出显示今天不是交易日或无冻结订单：运行 `.venv/bin/python tools/runlog.py add --task execute --status skipped --line "非交易日或无冻结订单，未执行"`，然后不发任何消息直接结束（不重建看板、不发布）。
3. 如果执行成功，依次做：
   a. 追加执行日志（行内容取自命令输出，不得编造；main 账户放第一行）：`.venv/bin/python tools/runlog.py add --task execute --status ok --line "main: 成交 N 笔, 现金 X" --line "quant: …; random: …; etf: …"`。
   b. 重建状态看板：`.venv/bin/python tools/build_dashboard.py`（同时更新 web/index.html 与 dist/index.html）。
   c. 站点尽力发布：若当前会话存在 sites 插件 MCP 工具（mcp__plugin_sites_qoder_sites__prepare_site / get_publish_status / publish_site）：生成一个新 UUID 作为 actionId，调用 prepare_site，参数 projectRoot="/Users/yancong/agent-workspace/a-stock-investment"、webDirectory="dist"、projectId="01a1728a-617c-708f-99f9-03e5934f09f3"；读 get_publish_status 至 canPublish=true 后调用 publish_site（同一 actionId），再读一次状态确认 published=true。若工具缺失或任一步失败，最多重试一次；仍不行就放弃发布，并在汇报中注明“看板已本地更新，站点待同步发布”。
   d. 提交：`git add web/index.html dist/index.html && git commit -m "[qoder-cn-cron] sim: 同步 execute 执行日志与看板"`（reports/sim/ 在 .gitignore 中，不要 add；无改动则跳过）。
   e. 用一小段中文汇报：main 账户成交笔数与现金、其余账户各一句、看板站点是否发布成功。不要添加投资建议。
4. 如果报错：先运行 `.venv/bin/python tools/runlog.py add --task execute --status failed --line "<错误摘要>"`，最多重试一次；仍失败则汇报一次“sim execute 失败：<错误摘要>”，不要连续刷屏。
5. 硬性约束：绝不手工修改 accounts/、orders/ 已冻结文件和 config/risk.yaml；账本只能由 python -m sim 写入。
