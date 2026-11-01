# 隔夜实验定时任务：交接给 Qoder CN（2026-11-01）

> 交给 Qoder CN 来建定时任务。代码、账户、提示词都已就绪，**只差建 cron**。
> 设计见 [docs/research/隔夜策略_可行性与设计.md](../research/隔夜策略_可行性与设计.md)；引擎 `sim/overnight.py`；测试 `tests/test_overnight.py`。

## 0. 现状（已完成，不需要再做）
- 三个隔夜账户已创建，各 10 万，起始日 2026-11-02：`accounts/overnight/{agent,quant,random}/`。与 main / quant / random / etf / etf_band 完全独立，`sim evening` / `sim execute` 不会碰它们。
- 参数 `config/overnight.yaml`；风控 `config/overnight_risk.yaml`（用户 2026-11-01 确认，**只有用户能改**）。
- 命令：`python -m sim on-scan | on-freeze | on-settle | on-report | on-status | on-resume <账户>`（`-h` 看说明）。
- Agent 提示词：`docs/automation/prompts/overnight_{common,premarket,midday,close,review}.md`。
- 经验库种子：`data/overnight/lessons.md`。

## 1. 要建的 8 个任务（全部本地执行、独立会话、Full Access、时区 `Asia/Shanghai`、工作目录 `/Users/yancong/agent-workspace/a-stock-investment`）

| # | UI 任务名（建议） | cron | 类型 | 指令 | 时限 |
|---|---|---|---|---|---|
| 1 | 隔夜：盘前准备 | `50 8 * * 1-5` | Agent | 读取并严格执行 `docs/automation/prompts/overnight_premarket.md` | 30 分钟 |
| 2 | 隔夜：午间验证 | `35 11 * * 1-5` | Agent | 读取并严格执行 `docs/automation/prompts/overnight_midday.md` | 40 分钟 |
| 3 | 隔夜：尾盘快照 | `38 14 * * 1-5` | 命令 | 见 2.1 | 2 分钟 |
| 4 | 隔夜：尾盘决策 | `40 14 * * 1-5` | Agent | 读取并严格执行 `docs/automation/prompts/overnight_close.md` | **12 分钟（14:52 前写完）** |
| 5 | 隔夜：冻结订单 | `54 14 * * 1-5` | 命令 | 见 2.2 | 1 分钟 |
| 6 | 隔夜：结算 | `30 15 * * 1-5` | 命令 | 见 2.3 | 2 分钟 |
| 7 | 隔夜：盘后复盘 | `0 16 * * 1-5` | Agent | 读取并严格执行 `docs/automation/prompts/overnight_review.md` | 30 分钟 |
| 8 | 隔夜：结算补跑 | `30 20 * * 1-5` | 命令 | 见 2.3（同 6，幂等） | 2 分钟 |

- 命令内部会判断交易日，节假日触发会打印"不是交易日，跳过"，无需外部过滤。
- 3、5、6、8 是确定性命令，**必须和 Agent 任务分开建**：Agent 会话卡死（2026-10-26 出现过 41 分钟卡死）不能拖累快照、冻结和结算。
- 4 号最关键：14:54 冻结程序只认 14:57 之前写好的草稿。若 Qoder 会话启动慢于 2~3 分钟，可以把 4 号提前到 `35 14`，但 3 号快照必须更早（例如 `33 14`），否则 Agent 读不到候选表。
- 与现有任务的冲突：15:10 的 `sim execute` 和 6 号 15:30 都会 `git commit`，程序带 index.lock 重试；20:30 的 8 号在 20:00 evening 之后、21:00 看门狗之前，互不影响。

## 2. 命令型任务的指令原文

### 2.1 隔夜：尾盘快照（14:38）
```
你是隔夜实验的尾盘快照任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
1. 运行 .venv/bin/python -m sim on-scan。
2. 输出含"不是交易日" → .venv/bin/python tools/runlog.py add --task overnight-scan --status skipped --line "非交易日"，结束，不推送。
3. 成功 → runlog add --task overnight-scan --status ok --line "<输出里'快照 … 炸板率 …'那一行原文>"。不提交 git（14:54 冻结时一并提交）。
4. 失败 → 重试一次；仍失败 runlog 记 failed，推送 .venv/bin/python tools/notify.py --title "隔夜快照失败" --body "<错误摘要>；quant/random 今晚将空仓"。
5. 绝不手工修改 accounts/、orders/、config/。
```

### 2.2 隔夜：冻结订单（14:54）
```
你是隔夜实验的冻结任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
1. 立即运行 .venv/bin/python -m sim on-freeze（必须在 14:57 前完成，不要先做任何别的事）。
2. 非交易日 → runlog add --task overnight-freeze --status skipped --line "非交易日"，结束。
3. 成功 → runlog add --task overnight-freeze --status ok --line "agent: <来源 draft/provisional/missed>，N 只" --line "quant: N 只；random: N 只"（取自输出原文）。
4. agent 来源是 missed 时推送 Bark：标题 "隔夜：Agent 漏跑"，正文 "今晚 agent 空仓（无草稿也无午间预案），请检查尾盘决策任务"。
5. 输出含"冻结必须在 … 之前完成" → runlog 记 failed，推送 Bark "隔夜冻结超时"。不要重试（过了 14:57 的订单按规则作废）。
6. 绝不手工修改 accounts/、orders/、config/。
```

### 2.3 隔夜：结算（15:30）/ 结算补跑（20:30）
```
你是隔夜实验的结算任务。工作目录 /Users/yancong/agent-workspace/a-stock-investment。
1. 运行 .venv/bin/python -m sim on-settle。
2. 非交易日 → runlog add --task overnight-settle --status skipped --line "非交易日"，结束。
3. 输出全部是"已结算，跳过" → runlog add --task overnight-settle --status skipped --line "已结算"，结束，不推送。
4. 输出含"中止：基准指数日线还没更新"或"暂不结算"（个股日线还没更新；20:00 前程序会等，20:00 后仍缺才按停牌处理） → runlog add --task overnight-settle --status skipped --line "数据未更新，等待补跑"；若这是 20:30 的补跑任务，则改记 failed 并推送 Bark "隔夜结算：数据仍未更新"。
5. 成功 → runlog add --task overnight-settle --status ok --line "agent: 总资产 X，平仓 N 笔 …" --line "quant: …；random: …"（取自输出原文）。
6. 提交：程序已自动提交 accounts/ orders/ data/overnight/；不需要再 git add。
7. 其他报错 → 重试一次，仍失败 runlog 记 failed 并推送 Bark "隔夜结算失败"。
8. 绝不手工修改 accounts/、orders/、config/。
```

## 3. 每日数据流

```
08:50 盘前（Agent）  → data/overnight/journal/<日>.md「盘前」：主线假设 + 20~40 只观察名单
11:35 午间（Agent）  → 名单收缩到 10~15 只 + orders/<日>/overnight_agent.provisional.json（兜底预案）
14:38 快照（命令）   → data/overnight/snap/<日>_1438.csv（不入库）、data/overnight/cand/<日>.md/.csv、随机池
14:40 尾盘（Agent）  → 14:44 前保底复制预案 → 14:52 前写 orders/<日>/overnight_agent.draft.json
14:54 冻结（命令）   → orders/<日>/overnight_{agent,quant,random}.json + MANIFEST（哈希、只读）+ git 提交
15:00               收盘集合竞价：按收盘价买入（收盘封涨停 = 未成交）
次日 09:25          开盘集合竞价：按开盘价卖出（开盘跌停 = 顺延）
15:30 结算（命令）   → 先卖前一晚持仓（今日开盘价）再买今日订单（今日收盘价），写账本、data/overnight/scores.csv、report.md
16:00 复盘（Agent）  → 日志「盘后复盘」+ 更新 lessons.md
20:30 补跑（命令）   → 15:30 数据没更新时补结算（幂等；漏几天也会按交易日逐日补齐）
```

Agent 草稿的取用优先级：尾盘 draft → 午间 provisional → 都没有记为 `missed`（报告里与"主动空仓"分开统计）。草稿的 `decided_at` 必须是当天且早于冻结时间，否则作废。

## 4. 验收清单（建完任务后的第一个交易日逐项核对）

| 时点 | 检查 | 期望 |
|---|---|---|
| 建任务后 | `.venv/bin/python -m pytest tests/test_overnight.py -q` | 8 passed |
| 建任务后 | `.venv/bin/python -m sim on-status` | 三个账户，现金 100,000，最近结算 None |
| 08:50 后 | `data/overnight/journal/<日>.md` | 有「盘前」一节 |
| 11:35 后 | `orders/<日>/overnight_agent.provisional.json` | 存在，`decided_at` 为当天上午 |
| 14:39 后 | `data/overnight/cand/<日>.md` | 存在，快照时间约 14:38 |
| 14:53 前 | `orders/<日>/overnight_agent.draft.json` | 存在，`decided_at` ≤ 14:52 |
| 14:55 后 | `orders/<日>/MANIFEST.json` | 含 overnight_agent / overnight_quant / overnight_random 三项；agent 的 `source` 为 draft |
| 15:31 后 | `on-status` | 三个账户最近结算为当天，有持仓（除非主动空仓或买不到） |
| 次日 15:31 后 | `data/overnight/scores.csv`、`data/overnight/report.md` | 出现前一晚的平仓记录 |
| 每天 | `tools/runlog.py show` | overnight-* 各任务都有当天记录 |

## 5. 交接完成后请 Qoder 更新
1. `docs/tools/qoder.md` 的定时任务表：加上述 8 个任务与任务 ID。
2. `docs/automation/README.md` 第 1 节时间线与第 3 节任务清单：加隔夜任务（或链接到本文件）。
3. `docs/进展与交接.md`：记录建任务的日期与首日验收结果。
4. 21:00 看门狗（可选）：增加一条检查——交易日当天 runlog 若缺 `overnight-settle` 的 ok 记录，就幂等跑 `python -m sim on-settle` 并推送 Bark。

## 6. 已知风险
- **会话启动延迟**：尾盘只有约 12 分钟。如果首周 `missed` 或 `provisional` 比例偏高，把尾盘任务提前，或把决策逻辑更多前移到午间预案。
- **分钟线接口不能并发**（新浪接口走 mini_racer，多线程会崩进程），`on-scan` 已改为串行，约 40 秒。
- **快照数据源**：`ak.stock_zh_a_spot`（新浪）。取数失败时 quant / random 当晚空仓，agent 仍可按草稿冻结（风控检查会缺少快照价，但日线检查照常）。
- **结算依赖新浪日线更新**：15:30 可能还没更新到当天，所以有 20:30 补跑。
