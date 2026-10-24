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
| 模拟盘 evening | 交易日 20:00（`0 20 * * 1-5`） | `80c52643-5358-4edb-8f4a-8c9a6391a56b` |
| 黄金做T条件提醒（与 sim 无关） | 交易时段每 15 分钟 | `dbfeb064-b2bf-4ae9-8170-7cafc1888cb6` |

- 创建时间：2026-10-24。cron 会在非交易日触发，但 `sim` 命令内部会判断交易日并空跑，无需外部过滤。
- **移交方式**：其他工具要承担时，先在本工具里 `qoder_cron disable/remove` 上表两个 sim 任务，再到新工具创建，并更新 `docs/进展与交接.md` 接手规则第 2 条的归属记录。

## 特有约束与技巧
- MCP 工具懒加载：不在工具列表里的 MCP 工具用 `mcp_list` / `mcp_get` / `mcp_call` 发现和调用。
- cron 任务的执行会话不继承任何历史对话，prompt 必须自包含；任务里的汇报和日志规则已写进各任务 prompt。
