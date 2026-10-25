# Qoder 专属上下文

> 通用规则见 [AGENTS.md](../../AGENTS.md)，接力流程见 [跨工具接力准则](../跨工具接力准则.md)。本文件只写 Qoder 与其他工具不同的地方。

## 规则入口
Qoder 自动注入工作区根目录的 `AGENTS.md`，无需额外配置。

## 子代理调度
- 定义在 `.qoder/agents/`，由 `bash tools/build_agents.sh` 从 `prompts/` 生成，不要直接改。
- 用 Agent 工具按 `subagent_type` 点名委派（如 `subagent_type: fundamental-agent`），支持同一条消息并行调用多个。
- 委派 prompt 必须自包含（标的、日期、问题），子代理看不到主对话。

## 定时任务

### 当前归属：Qoder CN（2026-10-25 迁移后）
> Qoder 国际版与 CN 版是两套账号体系（各自的 cron 与 Sites 相互独立），但共用本机同一个工作区。**账本任务只能在一套账号里启用**。

| 任务 | 计划（Asia/Shanghai） | 任务 ID（CN） |
|---|---|---|
| 模拟盘 execute | 交易日 15:10（`10 15 * * 1-5`） | `6a4defea-34b0-473e-9dbc-b5f694678021` |
| 模拟盘晚间复盘与研究（第二阶段） | **每天** 19:00（`0 19 * * *`） | `32e49144-4edf-496b-8630-f03165c72452` |
| 模拟盘 evening | 交易日 20:00（`0 20 * * 1-5`） | `5f8097de-a4be-4863-965d-184e414f54ec` |
| 黄金做T条件提醒（独立功能） | 交易时段每 15 分钟（`*/15 9-11,13-15,21-22 * * 1-5`，2026-12-31 到期） | `5c74d6a0-c23e-42c0-b8af-e895c0837509` |

- 创建于 2026-10-25，全部为本地执行、独立会话、Full Access、`Asia/Shanghai`。
- 迁移后国际版的任务 ID 仅作历史记录：execute `df6f6cb3…`、research `acbf75d2…`、evening `377136b3…`、黄金 `dbfeb064…`；**必须在国际版里停用/删除，否则两套 cron 会同时写账本**。
- cron 会在非交易日触发，但 `sim` 命令内部会判断交易日并空跑，无需外部过滤。
- **移交方式**：其他工具要承担模拟盘时，先在 CN 里停用上表任务，再到新工具创建，并更新 `docs/进展与交接.md` 接手规则第 2 条的归属记录。黄金提醒与模拟盘无依赖，可独立迁移或直接不迁移。
- 任务指令：execute / evening 的原文快照在 `docs/automation/prompts/`；**research 任务以 `docs/automation/prompts/research.md` 为唯一指令源**（cron 里只保存“读取并严格执行该文件”的短指令，修改文件即生效）；黄金提醒在 `docs/automation/extras/gold-alert.md`。完整 pipeline、数据流与迁移步骤见 `docs/automation/README.md`。
- 2026-10-25 起，execute / evening / research 任务成功后会：追加执行日志（`tools/runlog.py` → `reports/sim/runlog.json`）→ 重建状态看板（`tools/build_dashboard.py` → `web/`+`dist/`）→ 尽力发布站点；黄金提醒仅在触发/失败时写日志，不重建看板。
- 复盘与研究任务（每天 19:00，含非交易日）：① 交易复盘——交易日复盘当天、非交易日复盘最近一个已结算交易日，写 `journal/daily/<日期>.md`（已存在则跳过）；② v1.1 分诊漏斗——`python -m sim pool` 全市场多路召回 + 分诊（action/触发位/stop_prob，产物 `data/pool/<日期>.md`）→ 按 regime 配额选 6-8 只 shortlist → 深度研究 3-5 只（结论三选一：现在买/等触发/放弃）→ 组合经理决策；另有 `python -m sim pool2` 影子池（`data/pool_v2/`，只记录 5 只 shadow shortlist，不进入下单），写入 `orders/<目标交易日>/main.draft.json`，研究成果记入 `data/watchlist.json`。`data/screen/` 只服务 quant / random 对照组，不再是 main 的候选来源。

## 状态看板（Qoder Sites，CN 账号）
- 站点：`https://ashare-sim-dashboard-wv51upzp22k.qoder.zone`（私有），描述符 `.A股模拟交易状态看板.qoder.site`，projectId `01a1728a-617c-708f-99f9-03e5934f09f3`，siteId `01a1728a-617f-79a7-97af-a1db216409b8`。
- 页面由 `tools/build_dashboard.py` 生成（四策略收益对照 + 标签页详情；main 标签含候选池与观察名单；另有对照组初筛快照与执行日志），发布输出目录为 `dist/`。
- 国际版旧站点 `ashare-sim-dashboard-hnjfa46w01s.qoder.website`（projectId `01a17110-976d-72b4-bda2-73e3f45aa97a`）仍留在国际版账号，CN 会话无法发布也无法删除；迁移后看板只更新 CN 站点。
- cron 会话**可能没有 sites 插件**（qoder_cron 无法为任务挂插件）：发布失败时任务只注明"站点待同步发布"，本地 `dist/` 已是最新；任何带 sites 插件的会话用新 actionId 调 `prepare_site`（复用上述 projectId、webDirectory="dist"）+ `publish_site` 即可补发。

## 特有约束与技巧
- MCP 工具懒加载：不在工具列表里的 MCP 工具用 `mcp_list` / `mcp_get` / `mcp_call` 发现和调用。
- cron 任务的执行会话不继承任何历史对话，prompt 必须自包含；任务里的汇报和日志规则已写进各任务 prompt。
