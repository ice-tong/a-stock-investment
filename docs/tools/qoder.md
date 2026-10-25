# Qoder 专属上下文

> 通用规则见 [AGENTS.md](../../AGENTS.md)，接力流程见 [跨工具接力准则](../跨工具接力准则.md)。本文件只写 Qoder 与其他工具不同的地方。

## 规则入口
Qoder 自动注入工作区根目录的 `AGENTS.md`，无需额外配置。

## 子代理调度
- 定义在 `.qoder/agents/`，由 `bash tools/build_agents.sh` 从 `prompts/` 生成，不要直接改。
- 用 Agent 工具按 `subagent_type` 点名委派（如 `subagent_type: fundamental-agent`），支持同一条消息并行调用多个。
- 委派 prompt 必须自包含（标的、日期、问题），子代理看不到主对话。

## 定时任务
Qoder 内建 cron（MCP `builtin` 服务器的 `qoder_cron` 工具），**当前承担全局模拟盘定时任务**：

| 任务 | 计划（Asia/Shanghai） | 任务 ID |
|---|---|---|
| 模拟盘 execute | 交易日 15:10（`10 15 * * 1-5`） | `df6f6cb3-9585-4b22-a93e-b8c812769567` |
| 模拟盘晚间复盘与研究（第二阶段） | **每天** 19:00（`0 19 * * *`） | `acbf75d2-160c-4377-9296-df43b078d61e` |
| 模拟盘 evening | 交易日 20:00（`0 20 * * 1-5`） | `377136b3-c3d5-4955-92c3-6a129f5b4e0c`（2026-10-25 重建，原 `80c52643…` 已删除） |
| 黄金做T条件提醒（与 sim 无关） | 交易时段每 15 分钟 | `dbfeb064-b2bf-4ae9-8170-7cafc1888cb6` |

- 创建时间：2026-10-24。cron 会在非交易日触发，但 `sim` 命令内部会判断交易日并空跑，无需外部过滤。
- **移交方式**：其他工具要承担时，先在本工具里 `qoder_cron disable/remove` 上表两个 sim 任务，再到新工具创建，并更新 `docs/进展与交接.md` 接手规则第 2 条的归属记录。
- 任务指令原文快照在 `docs/automation/prompts/`，完整 pipeline、数据流与迁移步骤见 `docs/automation/README.md`（迁移到其他工具时照它执行）。
- 2026-10-25 起，execute / evening / research 任务成功后会：追加执行日志（`tools/runlog.py` → `reports/sim/runlog.json`）→ 重建状态看板（`tools/build_dashboard.py` → `web/`+`dist/`）→ 尽力发布站点；gold 任务仅在触发/失败时写日志。
- 复盘与研究任务（每天 19:00，含非交易日）分两步：① 交易复盘——交易日复盘当天、非交易日复盘最近一个已结算交易日，写 `journal/daily/<日期>.md`（已存在则跳过，保证幂等）；② 交易计划草稿——初筛 + 子代理研究 + 组合经理决策，写入 `orders/<目标交易日>/main.draft.json`（计划日 = 今天为交易日则今天、否则下一个交易日；目标日 = 计划日的下一个交易日，即当晚 20:00 evening 将冻结订单的那一天；已冻结则跳过）。只写草稿与 journal，不碰账本。

## 状态看板（Qoder Sites）
- 站点：`ashare-sim-dashboard-hnjfa46w01s.qoder.website`（私有），描述符 `.A股模拟交易状态看板.qoder.site`，projectId `01a17110-976d-72b4-bda2-73e3f45aa97a`。
- 页面由 `tools/build_dashboard.py` 生成（主账户视角 + 执行日志 + 冻结订单 + 风控参数），发布输出目录为 `dist/`。
- cron 会话**可能没有 sites 插件**（qoder_cron 无法为任务挂插件）：发布失败时任务只注明"站点待同步发布"，本地 `dist/` 已是最新；任何带 sites 插件的会话用新 actionId 调 `prepare_site`（复用上述 projectId、webDirectory="dist"）+ `publish_site` 即可补发。

## 特有约束与技巧
- MCP 工具懒加载：不在工具列表里的 MCP 工具用 `mcp_list` / `mcp_get` / `mcp_call` 发现和调用。
- cron 任务的执行会话不继承任何历史对话，prompt 必须自包含；任务里的汇报和日志规则已写进各任务 prompt。
