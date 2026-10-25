# 模拟交易自动化 Pipeline 与迁移手册

> 最后核对：2026-10-25（Asia/Shanghai）。`prompts/` 下是当时 cron 指令的原文快照。
> 迁移前请用本工具的任务列表核对一次实际配置；改过指令就把对应文件一起更新。

## 范围

本手册只覆盖**模拟交易主 pipeline**：execute / research / evening 三个任务加状态看板。整条链路**不依赖云端状态**，所有事实都在本仓库文件里，因此可以整体搬到任何能跑本地 cron + shell 的 Agent 工具。

黄金做T提醒是**独立分叉功能**，与模拟盘没有任何数据或执行依赖，只额外借用同一个 cron 与执行日志展示。它不属于本 pipeline，单独说明见[第 9 节](#9-附独立分叉功能黄金做t提醒)，迁移时可独立决定去留。

## 一句话

三个任务共享一套本地账本：`execute` / `evening` 只能通过 `python -m sim` 写账本，`research` 只写复盘与订单草稿；看板由任务重建后尽力发布到 Qoder Sites。

## 1. 时间线

| 时间（Asia/Shanghai） | 任务 | 触发日 | 典型耗时 | 一句话 |
|---|---|---|---|---|
| 15:10 | execute | 交易日（周一至周五，日历内部再判休市） | < 1 分钟 | 按当日冻结订单、以 14:45 分钟价成交 |
| 19:00 | research | **每天** | 5–25 分钟 | 交易复盘（写 journal）+ 下一交易日订单草稿（写 main.draft.json） |
| 20:00 | evening | 交易日 | 1–2 分钟 | 收盘结算 → 读草稿跑风控 → 冻结次日订单 → 日报/资金看板 |

配套 cron 表达式：

| 任务 | 表达式 | 任务 ID（Qoder） |
|---|---|---|
| execute | `10 15 * * 1-5` | `df6f6cb3-9585-4b22-a93e-b8c812769567` |
| research | `0 19 * * *` | `acbf75d2-160c-4377-9296-df43b078d61e` |
| evening | `0 20 * * 1-5` | `377136b3-c3d5-4955-92c3-6a129f5b4e0c`（2026-10-25 重建） |

## 2. 数据流

```
每天 19:00 research
  ├─ 复盘 → journal/daily/<L>.md（幂等：存在即跳过）
  └─ 研究 → orders/<目标日>/main.draft.json        （只是草稿，随时可被覆盖）

交易日 20:00 evening（python -m sim evening）
  ├─ settle：收盘价结算 → accounts/*/nav.csv、state.json
  ├─ plan：读 main.draft.json + 各组策略 → risk.review 一票否决/缩量
  │        → orders.freeze：orders/<T+1>/{main,quant,random,etf}.json
  │                         + MANIFEST.json（SHA256）
  ├─ report / dashboard：reports/sim/latest.html、reports/sim/dashboard.html
  └─ git：自动提交 orders/ accounts/ data/screen/

交易日 15:10 execute（python -m sim execute）
  ├─ 校验 orders/<当日>/MANIFEST.json 的 SHA256
  ├─ 以 14:45 分钟价成交（取不到则收盘价 + 额外滑点）
  ├─ 写 accounts/*/trades.csv、state.json
  └─ git：自动提交 orders/ accounts/ data/screen/
```

**草稿目标日规则**：计划日 = 今天（交易日）或下一个交易日；目标日 = 计划日的下一个交易日。周末生成的草稿目标是下周二，周一 19:00 会用新数据覆盖它，属正常设计。

## 3. 任务清单（迁移时逐条照抄）

| 任务 | Prompt 原文 | 命令 / 动作 | 产出 |
|---|---|---|---|
| execute | [prompts/execute.md](prompts/execute.md) | `python -m sim execute` | 成交、trades.csv、runlog、看板、git 提交 |
| research | [prompts/research.md](prompts/research.md) | 复盘 + 子代理研究 + 写草稿；必要时刷新初筛 | journal、main.draft.json、runlog、看板、git 提交 |
| evening | [prompts/evening.md](prompts/evening.md) | `python -m sim evening` | 结算、冻结订单、日报、runlog、看板、git 提交 |

> 这三份文件和 cron 里的指令应保持一致。迁移时直接整份粘贴，不要改写路径（`/Users/yancong/agent-workspace/a-stock-investment`）与任务 ID；换机器时先把路径批量替换成新工作区。

## 4. 各任务要点

### execute（交易日 15:10）
- 只读当日已冻结订单，不会临时生成订单。
- 命令自带两道闸：非交易日跳过；当天 14:45 前运行会被拒绝。
- 无冻结订单时记为 skipped，不重建看板。
- 失败最多重试一次，再失败只汇报一次。

### research（每天 19:00）
- **复盘**：交易日复盘当天（净值以 20:00 结算为准，文中注明）；非交易日复盘 `state.json` 的 `last_settled`。`journal/daily/<L>.md` 已存在就跳过，因此手动重跑安全。
- **归因**：仅当 main 当日有成交或任一持仓浮亏 >5% 时才委派 technical-flow / event-sentiment 子代理；否则不烧 token。
- **草稿**：初筛前 12 只里至多研究 3 只新候选，每只并行委派 fundamental + event-sentiment（必要时 technical-flow）；持仓逐只复核。组合经理输出 `code / name / target_weight / reason（含置信度）/ 可选 stop_price、industry`；卖出用 `reason_type: "exit"`。
- 没值得买的就写空 `orders` 列表——代码会自动补底仓停泊，**不要为了交易而交易**。
- 只允许写 `orders/<目标日>/main.draft.json` 与 `journal/`；该日订单已冻结则跳过。
- 指令第 12 步负责重建看板、尽力发布、git 提交（`web/ dist/ journal/ orders/`）。

### evening（交易日 20:00）
- 一条命令完成结算、冻结、日报、资金看板；命令内部幂等，同一天重复跑不会重复结算/冻结。
- **职责分工**：evening 负责每日结算与订单冻结，execute 只按冻结订单写成交；两者是账本的唯一写入路径，都只能通过 `python -m sim` 触发。
- 严格来说 evening 必须与 research 分开跑：research 是 LLM + 网络 + 子代理，失败/超时不能拖累结算。
- 订单冻结后不可修改；SHA256 写进 `orders/<日期>/MANIFEST.json`，execute 前会校验。
- 若项目是 git 仓库，`sim execute` / `sim evening` 结束时会自动 `git add orders accounts data/screen` 并提交，作为账本的不可抵赖时间记录；任务 prompt 只需再提交 `web/`、`dist/` 等衍生文件。

## 5. 文件地图

| 路径 | 作用 | 是否入库 |
|---|---|---|
| `sim/` | 确定性内核（日历、撮合、账本、风控、初筛、报告） | 是 |
| `tools/ashare.py` | AKShare 取数 | 是 |
| `tools/runlog.py` | 执行日志追加/查看（`reports/sim/runlog.json`） | 是（日志本身不入库） |
| `tools/build_dashboard.py` | 生成 `web/index.html` 与 `dist/index.html` | 是 |
| `config/broker.yaml` | 账户、费率、执行时点、滑点 | 是 |
| `config/risk.yaml` | 风控规则，**只有用户能改** | 是 |
| `config/strategy.yaml` | quant / random 参数 | 是 |
| `accounts/<账户>/` | `state.json` / `nav.csv` / `trades.csv`，**只能由 `python -m sim` 写** | 是 |
| `orders/<交易日>/` | 冻结订单 + MANIFEST，**冻结后不可改** | 是 |
| `orders/<交易日>/main.draft.json` | Agent 草稿（未冻结，可覆盖） | 是 |
| `journal/daily/<日期>.md` | 每日复盘 | 是 |
| `data/screen/<日期>.csv` | 初筛快照 | 是 |
| `reports/sim/` | 日报、资金看板、`runlog.json` | 否（.gitignore） |
| `web/`、`dist/` | 看板源码与发布目录 | 是 |
| `docs/automation/` | 本手册与 prompt 原文 | 是 |

## 6. 状态看板与发布

- 生成：`python tools/build_dashboard.py`，读账户、订单、runlog、初筛快照，输出主账户视角 + 对照组对比（含可折叠的持仓/成交明细）+ 执行日志 + 冻结订单 + 策略依据。
- 发布：Qoder Sites，projectId `01a17110-976d-72b4-bda2-73e3f45aa97a`，站点 `ashare-sim-dashboard-hnjfa46w01s.qoder.website`（私有），描述符 `.A股模拟交易状态看板.qoder.site`，输出目录 `dist/`。
- 流程：`prepare_site`（新 actionId）→ `get_publish_status`（等 canPublish）→ 同参数重试 `prepare_site` 写描述符 → `publish_site` → 确认 `published: true`。
- 降级：cron 会话可能没有 sites 插件。此时任务只注明“看板已本地更新，站点待同步发布”，`dist/` 已是最新；任何带 sites 插件的会话用新 actionId 补发即可。
- `runlog.json` 只保留最近 300 条，由 `tools/runlog.py` 维护；看板每次重建时把当前日志内嵌进 HTML，所以日志刷新依赖重建。
- 看板的执行日志会同时显示黄金提醒的记录（若触发）；那是独立功能的旁路展示，不参与模拟盘流程。

## 7. 迁移到其他 Agent 工具

### 7.1 先核对能力

| 能力 | 用途 | 没有时 |
|---|---|---|
| 本地 cron + IANA 时区 | 三个定时任务 | 不能用云端任务替代：云任务没有本机文件与 `.venv` |
| 工作区 shell | 跑 `python -m sim`、git | 不可迁移 |
| 会话重启后仍在的本地文件 | 账本、订单、journal | 不可迁移 |
| 子代理（或按角色提示词执行） | research 的多角色研究 | 降级为单会话按 `prompts/agents/*.md` 依次执行，会慢 |
| Qoder Sites 插件（可选） | 看板发布 | 只更新本地 `web/`、`dist/` |
| git | 每次运行留痕 | 可跳过提交，但审计能力下降 |

### 7.2 迁移步骤

1. **准备环境**：克隆本仓库到新工具的工作区，装依赖并自检。
   ```bash
   uv venv .venv --python 3.11
   uv pip install --python .venv/bin/python akshare tabulate pyyaml pytest
   .venv/bin/python -m pytest -q
   .venv/bin/python -m sim status
   ```
2. **先建账本链路**：创建 execute（`10 15 * * 1-5`）与 evening（`0 20 * * 1-5`），时区 `Asia/Shanghai`，工作目录指向新工作区，prompt 用 `prompts/execute.md`、`prompts/evening.md`。此时 main 只会做底仓与强制止损，安全。
3. **再建 research**（`0 19 * * *`）：prompt 用 `prompts/research.md`；确认新工具能调用子代理（或改走角色提示词）。第一晚先手动 run 一次，检查 journal 与草稿。
4. **避免双跑**：先在新工具建好但不要启用，等当天任务跑完后再停用旧工具的两个账本任务（execute/evening 错峰 30 分钟以上也不安全，最好直接停旧启新）。research 双跑只会覆盖草稿，风险较小，但同样建议停旧。
5. **登记归属**：更新 `docs/tools/<新工具>.md` 的任务表，并把 `docs/进展与交接.md` 接手规则第 2 条改成新工具的归属与任务 ID。
6. **验收**：按 7.3 逐项打勾，再删旧任务。

> 黄金提醒不在上述步骤内。要保留就按第 9 节单独迁移；不保留就不建。

### 7.3 验收清单

| 检查项 | 做法 | 期望 |
|---|---|---|
| 环境 | `python -m pytest -q`、`python -m sim status` | 测试全绿；四个账户存在，`last_settled` 为最近已结算日 |
| 任务配置 | 工具的任务列表 | 3 个模拟盘任务时间/时区/工作目录正确 |
| research 手动跑 | 非交易日触发一次 | 新增/跳过 journal、写 `orders/<目标日>/main.draft.json`、runlog 有 research 行 |
| 看板 | 看 `dist/index.html` mtime 与页面 | 执行日志出现本次运行；站点已发布或注明待同步 |
| execute | 交易日 15:10 后看 runlog | `ok`，trades.csv 有成交，现金/持仓变化 |
| evening | 交易日 20:00 后看 runlog | `ok`，nav.csv 新增一行，`orders/<次日>/MANIFEST.json` 存在 |
| 幂等 | 同日重复跑 evening | 不重复结算、不重复冻结 |
| 失败隔离 | 手动制造 research 失败（如断网） | evening 仍照常结算并冻结底仓订单 |

### 7.4 已知坑

- **不要合并 research 与 evening**：账本路径必须与 LLM 路径故障隔离（用户已在 2026-10-25 确认保持分开）。
- research 每天跑，但复盘幂等；非交易日复盘最近一个已结算日，净值以 20:00 结算为准。
- 周末草稿目标下周二，周一晚会被新草稿覆盖，不要当成 bug。
- AKShare 部分接口可能因代理失败（`info/hist/finance/valuation/notices`），研究需退到 WebSearch/WebFetch，并在复盘里注明数据缺口。
- `reports/sim/` 被 gitignore：`runlog.json` 不入库，看板重建时才把它写进 HTML；迁移后历史日志不会带过去，属预期。
- 测试/演练用 `SIM_HOME=/tmp/simtest` 指向临时目录，绝不在正式账户上试。
- `config/risk.yaml`、已冻结 `orders/`、`accounts/` 都是用户资产：任何自动化都不得改写（账本只能 `python -m sim` 写）。
- 迁到新工具后，原工具的 cron 必须停用，否则两个账本写入口会互相覆盖。

## 8. Prompt 同步约定

- `prompts/*.md` 是模拟盘任务指令原文；cron 里保存的是运行副本。黄金提醒的原文单独放在 `extras/gold-alert.md`。
- 改任务指令时：先改 cron，再把同一段文本覆盖到对应文件，并更新本页顶部的“最后核对”日期。
- 迁移时如需核对，逐字比对任务列表里的 `instruction.text` 与文件内容；不要只凭本手册的任务表重建 prompt。

## 9. 附：独立分叉功能（黄金做T提醒）

**定位**：个人附加的条件提醒，不是模拟交易 pipeline 的一部分。它与 `sim/` 零依赖：不读账户、不写订单、不结算、不参与风控，也不决定看板里的任何数字。

| 项目 | 内容 |
|---|---|
| 脚本 | `tools/gold_alert.py` |
| cron | `*/15 9-11,13-15,21-22 * * 1-5`（Asia/Shanghai），2026-12-31 到期 |
| 任务 ID（Qoder） | `dbfeb064-b2bf-4ae9-8170-7cafc1888cb6` |
| 指令原文 | [extras/gold-alert.md](extras/gold-alert.md) |
| 行为 | `NO_ALERT` 静默结束；`ALERT` 才发消息并写一条 runlog；不重建看板、不发布站点 |
| 冷却 | 脚本内置 2 小时，避免同一信号重复提醒 |

**与 pipeline 的唯一交集**：共用执行日志的展示位置（看板“定时任务执行日志”里会出现 gold 行），以及同一个 cron 调度器。迁移时：

- 想保留 → 在新工具单独建一个任务，粘贴 `extras/gold-alert.md`，与模拟盘三个任务没有先后顺序要求；
- 不想保留 → 直接不建，模拟盘不受任何影响；
- 停用黄金提醒也**不需要**改动 `docs/进展与交接.md` 里的账本归属记录。
