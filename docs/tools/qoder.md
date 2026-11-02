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

| 任务名（UI 显示） | 计划（Asia/Shanghai） | 对应命令 / 阶段 | 任务 ID（CN） |
|---|---|---|---|
| 模拟盘：按单下单 | 交易日 15:10（`10 15 * * 1-5`） | `sim execute` | `6a4defea-34b0-473e-9dbc-b5f694678021` |
| 模拟盘：晚间选股 | **每天** 19:00（`0 19 * * *`） | research（复盘 + 候选研究 + 次日草稿） | `32e49144-4edf-496b-8630-f03165c72452` |
| 模拟盘：结算并锁明日 | **每天** 20:00（`0 20 * * *`） | `sim evening` | `5f8097de-a4be-4863-965d-184e414f54ec` |
| 模拟盘：21:00 看门狗 | **每天** 21:00（`0 21 * * *`） | runlog 巡检 + 必要时兜底 `sim evening` | `a5f577f5-5c93-4bd0-bbdf-a73a8924615f` |
| 策略进化：周六体检 | 每周六 10:00（`0 10 * * 6`） | `sim attrib` + 周度诊断 | `425fec61-963d-443a-9ece-db2869a56025` |
| 黄金：做T提醒（独立功能） | 每 3 小时：10:00 / 14:00 / 22:00（`0 10,14,22 * * 1-5`，23:00 后不推送，2026-12-31 到期） | `tools/gold_alert.py` | `5c74d6a0-c23e-42c0-b8af-e895c0837509` |
| 隔夜：盘前准备 | 交易日 08:50（`50 8 * * 1-5`） | [prompts/overnight_premarket.md](../automation/prompts/overnight_premarket.md) | `d480f682-4610-4707-a9c2-069345419d84` |
| 隔夜：午间验证 | 交易日 11:35（`35 11 * * 1-5`） | [prompts/overnight_midday.md](../automation/prompts/overnight_midday.md) | `1587a3bd-0962-4853-8903-c7ce453954f6` |
| 隔夜：尾盘快照 | 交易日 14:38（`38 14 * * 1-5`） | `sim on-scan` | `52e69518-b68a-4fb8-a5ee-ddc1ef719263` |
| 隔夜：尾盘决策 | 交易日 14:40（`40 14 * * 1-5`） | [prompts/overnight_close.md](../automation/prompts/overnight_close.md)（14:52 前写完草稿） | `e1b73392-310c-44ac-9b41-2e2105a981e4` |
| 隔夜：冻结订单 | 交易日 14:54（`54 14 * * 1-5`） | `sim on-freeze`（必须早于 14:57） | `eecbc91e-3564-4c30-8e98-24f87e9e87e7` |
| 隔夜：结算 | 交易日 15:30（`30 15 * * 1-5`） | `sim on-settle` | `be1a4772-7518-4d0d-8334-b48fd42e36a5` |
| 隔夜：盘后复盘 | 交易日 16:00（`0 16 * * 1-5`） | [prompts/overnight_review.md](../automation/prompts/overnight_review.md) | `6c699f87-6fc4-4f42-a862-81c546961d68` |
| 隔夜：结算补跑 | 交易日 20:30（`30 20 * * 1-5`） | `sim on-settle`（幂等） | `4d2ad5d2-5914-4232-895a-3f777b27b5df` |
| 隔夜：市场上下文（数据） | 交易日 20:40（`40 20 * * 1-5`） | `tools/fetch_context.py`（两融/事件/涨停/炸板） | `b491d616-6850-4363-ab59-087b994df7af` |
| PEAD：尾盘扫描 | 交易日 14:50（`50 14 * * 1-5`） | `tools/pead.py scan` | `8cd61a00-08dd-4fbe-ad56-c66897bb4075` |
| PEAD：结算 | 交易日 20:45（`45 20 * * 1-5`） | `tools/pead.py settle` | `e88f3b17-fb28-4afa-ba68-3cf03866b9be` |
| 挑战者：结算（S-003） | 交易日 20:50（`50 20 * * 1-5`） | 读取并严格执行 [prompts/challenger_settle.md](../automation/prompts/challenger_settle.md)（`tools/s002.py --profile s003 settle`；除权、月初换仓、净值；幂等） | `2bb5991b-b1ca-4105-ab87-84e834318808`（2026-11-02 建；同日 S-002 删除后改名） |
| 挑战者：月末信号（S-003） | 交易日 21:15（`15 21 * * 1-5`） | 读取并严格执行 [prompts/challenger_signal.md](../automation/prompts/challenger_signal.md)（`tools/s002.py --profile s003 signal`；非月末自动跳过；月末约 10 分钟） | `9bb89634-f9e2-4267-90b6-8137ae2fffc5`（2026-11-02 建；同日 S-002 删除后改名） |

> **任务名只是 UI 展示**（2026-10-25 按用户要求从 `execute` / `evening` / 「第二阶段」等内部叫法改为口语化）。代码、cron 指令、`docs/automation/` 与本文件正文仍用 execute / research / evening / evolution 称呼阶段，两者按上表对应；改名不影响调度与指令内容。

- 创建于 2026-10-25，全部为本地执行、独立会话、Full Access、`Asia/Shanghai`。
- **隔夜实验 8 个任务创建于 2026-11-01**（同样本机、独立会话、Full Access、`Asia/Shanghai`，模型继承为 `dfmodel`）。账户在 `accounts/overnight/`，与 main 等完全独立，`sim execute` / `sim evening` 不会碰它们；指令与首日验收见 [overnight-handoff.md](../automation/overnight-handoff.md)，时间线见 `docs/automation/README.md` 1.1 节。14:38 快照、14:54 冻结、15:30/20:30 结算是确定性命令，**必须与 Agent 任务分开**（Agent 卡死不能拖累它们）；尾盘决策只有 12 分钟（草稿 14:52 前、冻结读 14:57 前）。
- **PEAD 挑战者（2026-11-01 新增）**：14:50 扫描、20:45 结算（等日线更新，卖价仍按次日收盘），独立账户 `accounts/challenger/pead/`（10 万），只写 `data/pead/` 与自己的账本；主流程 `all_accounts()` 只扫 `accounts/*` 第一层，看不到它。规则、样本内证据与 kill 线见 `docs/research/隔夜策略_理论边界与探索方针.md` 附录 D。
- **S-002 挑战者（已删除，2026-11-02 用户决定）**：第四轮 F4 的 50 只版本，10 万分 50 只每只约 2000 元、买不起一手的多，用户认为与宽基持仓区别不大，删除账户 `accounts/challenger/s002/` 与 `data/s002/`（历史在 git 提交 7650d58 及之前）。`tools/s002.py` 只保留 s003 profile。
- **S-003 挑战者（2026-11-01 新增，Claude Code 实现，观察级）**：第五轮 R5（F4 池子 + 退市/解禁减持剔除 + 增持回购加分，取前 10 只、前 30 名缓冲），同一工具 `tools/s002.py --profile s003`。账户 `accounts/challenger/s003/`（10 万），数据 `data/s003/`，日线与财报缓存 `data/s003/cache/`（gitignored，2026-11-02 从 `data/s002/cache/` 迁来）。首个信号已冻结 `data/s003/orders/2026-11-02.json`；11-02 首次建仓已手动结算（买 10 只，现金 6,030，净值 100,173）。
- **挑战者两个任务**（2026-11-02 由四个合并，同日 S-002 删除后改名为只跑 S-003，任务 ID 不变）：结算每个交易日都要跑（净值、除权、跌停顺延卖单），信号每天触发但只在月末最后一个交易日干活（cron 写不出「月末交易日」）；信号排在结算之后（缓冲要读结算后的持仓）。结算与信号分开建：信号要全市场联网取数、较易失败，不能拖累每日结算。runlog 任务名 `challenger-settle` / `challenger-signal`。cron 指令只跑一次 `--profile s003`，不要再同时跑默认 profile（两者等价，会重复执行）。
- 迁移后国际版的任务 ID 仅作历史记录：execute `df6f6cb3…`、research `acbf75d2…`、evening `377136b3…`、黄金 `dbfeb064…`；**必须在国际版里停用/删除，否则两套 cron 会同时写账本**。
- cron 会在非交易日触发，但 `sim` 命令内部会判断交易日并空跑，无需外部过滤。
- **移交方式**：其他工具要承担模拟盘时，先在 CN 里停用上表任务，再到新工具创建，并更新 `docs/进展与交接.md` 接手规则第 2 条的归属记录。黄金提醒与模拟盘无依赖，可独立迁移或直接不迁移。
- 任务指令：execute / evening 的原文快照在 `docs/automation/prompts/`；**research 任务以 `docs/automation/prompts/research.md` 为唯一指令源**（cron 里只保存“读取并严格执行该文件”的短指令，修改文件即生效）；黄金提醒在 `docs/automation/extras/gold-alert.md`。完整 pipeline、数据流与迁移步骤见 `docs/automation/README.md`。
- 2026-10-25 起，execute / evening / research 任务成功后会：追加执行日志（`tools/runlog.py` → `reports/sim/runlog.json`）→ 重建状态看板（`tools/build_dashboard.py` → `web/`+`dist/`）→ 尽力发布站点 → 推送 Bark（`tools/notify.py`）；黄金提醒仅在触发/失败时写日志并推送 Bark，不重建看板。
- **2026-10-26 事故与看门狗**：research 会话 19:00 准点触发后跑满 **60 分钟硬上限**被判 `session-failed`（`lastError {"type":"session-failed"}`），会话被杀导致末尾的 Bark 步骤没执行——失败对用户完全静默；evening 会话 20:00 派发后卡住 41 分钟（期间无 `python -m sim` 进程、零产出），20:41 自行恢复并跑完结算与冻结。**账本没坏纯属侥幸**：它冻结时读的是 10-25 的旧空草稿，当晚补跑的结论恰好也是空 orders。为此新增「模拟盘：21:00 看门狗」（每天 21:00 巡检 runlog 是否各有 research / evening 当天记录，evening 缺失则幂等兜底跑 `sim evening`，异常才推 Bark；不写 journal、不改草稿）。**2026-11-01 revision 2 起**，交易日还会检查当天是否有 `overnight-settle` 记录，缺失则幂等补跑 `python -m sim on-settle` 并补记 runlog（隔夜账户的结算不再依赖隔夜任务自己是否卡死）。排查用：`qoder_cron list` 看 `lastRunStatus` / `consecutiveFailures` / `runningAt`（`runningAt` 停在触发时刻且长时间不更新 = 会话卡死）。
- Bark 配置在 `config/notify.local.yaml`（已 gitignore）或 `BARK_KEY` 环境变量；模板见 `config/notify.example.yaml`，未配置时静默跳过。
- 复盘与研究任务（每天 19:00，含非交易日）：① 交易复盘——交易日复盘当天、非交易日复盘最近一个已结算交易日，写 `journal/daily/<日期>.md`（已存在则跳过）；② v1.1 分诊漏斗——`python -m sim pool` 全市场多路召回 + 分诊（action/触发位/stop_prob，产物 `data/pool/<日期>.md`）→ 按 regime 配额选 6-8 只 shortlist → 深度研究 3-5 只（结论三选一：现在买/等触发/放弃）→ 组合经理决策；另有 `python -m sim pool2` 影子池（`data/pool_v2/`，只记录 5 只 shadow shortlist，不进入下单），写入 `orders/<目标交易日>/main.draft.json`，研究成果记入 `data/watchlist.json`。`data/screen/` 只服务 quant / random 对照组，不再是 main 的候选来源。

## 状态看板（Qoder Sites，CN 账号）
- 站点：`https://ashare-sim-dashboard-wv51upzp22k.qoder.zone`（私有），描述符 `.A股模拟交易状态看板.qoder.site`，projectId `01a1728a-617c-708f-99f9-03e5934f09f3`，siteId `01a1728a-617f-79a7-97af-a1db216409b8`。
- 页面由 `tools/build_dashboard.py` 生成（多账户收益对照 + 标签页详情；main 标签含候选池与观察名单；另有对照组初筛快照与执行日志），发布输出目录为 `dist/`。
- 国际版旧站点 `ashare-sim-dashboard-hnjfa46w01s.qoder.website`（projectId `01a17110-976d-72b4-bda2-73e3f45aa97a`）仍留在国际版账号，CN 会话无法发布也无法删除；迁移后看板只更新 CN 站点。
- cron 会话**可能没有 sites 插件**（qoder_cron 无法为任务挂插件）：发布失败时任务只注明"站点待同步发布"，本地 `dist/` 已是最新；任何带 sites 插件的会话用新 actionId 调 `prepare_site`（复用上述 projectId、webDirectory="dist"）+ `publish_site` 即可补发。

## 特有约束与技巧
- MCP 工具懒加载：不在工具列表里的 MCP 工具用 `mcp_list` / `mcp_get` / `mcp_call` 发现和调用。
- cron 任务的执行会话不继承任何历史对话，prompt 必须自包含；任务里的汇报和日志规则已写进各任务 prompt。
