# Agent 选股策略两年回测 · 口径规格（2026-10-27）

> **注（2026-10-27 23:00 追加）**：用户同日批准新收益风险目标（年化 ≥20%、回撤 ≤30%）并把 `config/risk.yaml` 的 drawdown 阈值改为 15/22/27%、个股上限改为 95/80/50%。主结果已按新参数重跑（`data/backtest_agent/newrules/`），旧参数结果保留在 `data/backtest_agent/` 作对照；本规格的技术口径（数据/候选池/执行/指标）不变。

> 目的：回答用户「按现在 Agent 选股的策略，目标年化收益多少？最大回撤多少？能否用过去 1-2 年数据回测？」
> 现状：main 账户 2026-10-23 开户、10-26 首笔成交，实盘样本 3 个交易日；LLM 历史判断不可复现（无决策快照）。
> 因此**回测对象 = 策略的确定性内核代理**：候选池（`sim/pool.py` v1.1 六通道 + 分诊）→ 可行动名单 → 机械执行 + `config/risk.yaml` 风控。
> 明确不包含：LLM 的深度研究判断、逻辑证伪卖出、事件驱动择时、加仓/减仓。报告必须对此单独声明。

## 0. 文件与纪律
- 新增脚本：`tools/bt_agent_data.py`（Lead）、`tools/bt_agent_engine.py`（engineer）、`tools/bt_agent_verify.py`（verifier）。
- 中间数据：`data/cache/backtest/`、`data/backtest_agent/`（被 `.gitignore` 忽略，不入库）。
- **禁止修改**：`accounts/`、`orders/`、`config/risk.yaml`、`sim/` 内任何文件（只允许 import 复用 `sim.pool` 的 `channels()` / `triage()` / `features()` 口径，不得改其代码）。
- 只读 `config/strategy.yaml`、`config/broker.yaml`、`config/risk.yaml`。
- 不写任何交易账户；回测在内存/独立目录完成。

## 1. 数据
- 股票池：`data/cache/pool/2026-10-23_features.csv` 的 2,473 只（sh/sz 前缀；当日 `sim pool` 全市场快照）。**幸存者偏差**：只含 2026-10-23 仍可交易且流动性达标的股票，不含已退市/长期停牌/当日落后 1,250 名之外的股票 → 收益偏乐观，报告需定量讨论方向与量级。
- 日线：新浪 `ak.stock_zh_a_daily(adjust="qfq")`，2024-01-02 ~ 2026-10-23（前复权，含分红再投资近似）。缓存到 `data/cache/backtest/daily/<code>.csv`，不覆盖既有 `data/cache/daily/`（那是 2025-06 起的口径）。
- 指数：`sh000300` 日线（收盘即可）；ETF：`sh510300`（底仓）日线。
- 长表面板：`data/cache/backtest/panel.pkl`，列 `date,code,open,high,low,close,volume,amount`（前复权），另有 `meta.pkl`（code→name/industry/pe_snapshot/eps_implied）。

## 2. 回测区间
- 预热：2024-01-02 起（≥170 个交易日，满足 120 日均线与 130 观测）。
- 首信号日 = 2024-10-24；末信号日 = 2026-10-23 之前可完成 20 日持有与次日执行（实际末结算日 2026-10-23）。
- 主报告期：2024-10-24 → 2026-10-23（约 490 个交易日，2.0 年）。子区间：2024-10-24~2025-09-30、2025-10-08~2026-10-23（后段含策略设计所处行情，需标注 in-sample 成分）。

## 3. 每日资格（风险规则的时间点重算，等价 `risk.yaml::universe`）
- 代码排除：688/689/8/4/92 前缀、ST/退（用当前名称近似，历史 ST 不可得）。
- `listed_days ≥ 60`（自身交易日序列观测数）、`avg_amount20 ≥ 1e8`（元）、`chg_1d ≤ 7%`、`chg_5d ≤ 25%`、`close > 0`。
- 资格逐日重算（不是用 2026-10-23 的 `eligible` 快照）。

## 4. 候选池与"可行动名单"（确定性代理）
- 每个交易日 T 收盘后：按 `sim/pool.py::features()` 同款字段逐股重算（≥130 观测），再调 `sim/pool.py::channels(feat, cfg)`，cfg 取 `config/strategy.yaml::pool`（per_channel=25、breakout_amt_ratio=1.5、sector_top=5、sector_per=4、sector_min_size=8）。
- `regime` 用 `sim/settle.py::regime` 同款规则（沪深 300 收盘 > MA120 且 MA20 > 5 日前 MA20 → offense；收盘 < MA120 且 < MA60 → defense；其余 neutral）。
- 分诊：调 `sim/pool.py::triage(pool, regime)`。
- **可行动名单** = `action == "ready"`，按 `priority` 降序；triage 已把不合规/极端涨幅/波动/估值/高触损概率的名字标为 `avoid`，自然剔除。
- defensive 通道无历史 PE：主口径用 `lowvol`（close>MA120，vol_60 升序）替代（与 2026-10-25 通道回测同口径）；敏感性口径用 PE 近似（`eps = close_2026-10-23 / pe_2026-10-23`，`pe_t = close_t / eps`）——该口径含盈利预期前视，只作敏感性检查。
- 核心代理假设（报告必须写明）：LLM 深度研究不会系统性偏离 `ready` 集合；它主要做的是"从可行动名单里再挑 3-5 只 + 决定买点"，本回测用 priority/等权代替。

## 5. 组合与交易规则（main 账户口径）
- 初始 100,000 元；T 日收盘决策 → T+1 收盘成交（近似实盘 14:45）。
- 成交价：T+1 前复权收盘价；整手 100 股。
- 滑点（按 T 日 `avg_amount20` 分档，见 `broker.yaml::slippage`）：≥10 亿 0.10%；1-10 亿 0.20%；<1 亿 0.50%；ETF 0.05%。股票佣金 0.025%（最低 5 元）、卖出印花税 0.05%、过户费 0.001%（双向）；ETF 免印花税/过户费。
- 三种仓位口径，**必须都跑**：
  - **P1 实盘口径（主口径）**：个股单只目标 = 总资产 5%，最多 6 只；底仓 ETF `sh510300` 补到 `band_max − 个股权重`（defense .50 / neutral .80 / offense .95），ETF 权重偏离目标 >5pp 才调；个股 + 底仓 ETF 合计 ≤ band_max。
  - **P2 风控上限口径**：个股目标 = `band stock_max ÷ 6`（defense .20/6、neutral .50/6、offense .80/6），无底仓 ETF，其余现金。
  - **P3 纯选股口径**：6 只等权（各 1/6）100% 股票，忽略 band 与 ETF，只度量选股 alpha。
- 买入约束：当周新开个股 ≤3（自然周）、单只 ≤20%、单行业 ≤35%（用当前行业标签）、ETF 不计入个股数、`max_single` 与 `band stock_max` 均生效；T+1 无法成交（停牌/一字涨停/成交量不足）→ 顺延或跳过并在 trades 里标 `no_fill`。
- 卖出（机械，只保留代码可执行部分）：止损 −8%（T 收盘价 ≤ 成本×0.92 → T+1 收盘卖，不受最短持有期限制）；时间止损（持有 ≥20 交易日且市值 < 成本 → T+1 收盘卖）；regime 降档导致个股权重 > stock_max → 按 20 日动量最差顺序卖至不超限；最短持有 3 个交易日（仅对非强制卖出）。
- 熔断：账户回撤 ≥12% → 次日 10 个交易日暂停新开个股；≥18% → 暂停新开（机械口径，不设人工 resume；报告标注偏差）。
- 不实现：LLM 逻辑证伪卖出、事件加减仓、主观择时、底仓 ETF 主动择时（P1 只按 band 机械补仓）。

## 6. 指标与基准
- 指标：累计收益、CAGR（250 交易日/年）、最大回撤（日净值）、回撤最长持续、年化波动、Sharpe（rf=0）、Calmar = CAGR / |MaxDD|、已平仓胜率、平均持有天数、年换手、年度/子区间分解、按 regime 分解。
- 基准：`sh000300` 价格指数、`sh510300` 买入持有、**等权合格池**（每月首个交易日调仓、等权持有、含成本）、`P3` 对照。
- 报告必须回答用户原问：**「目标年化收益」「最大回撤」**——先说明设计文档没有预设年化目标（目标是 Calmar/回撤 ≤15%，见 `docs/模拟交易系统设计.md` 第 7 节、`evolution/roadmap.md`），再给回测估计区间 + 情景。

## 7. 交付物
- `data/backtest_agent/nav_P{1,2,3}.csv`、`trades_P{1,2,3}.csv`、`metrics.json`、`selection_forward.csv`（ready 名单前向 5/20/60 日收益与横截面超额）。
- `data/backtest_agent/verify/`：敏感性（执行价 open/close、滑点 0/2x、止损 5/8/10%、持股 3/6/9、lowvol vs PE-defensive、区间截断）与独立复算结果。
- 报告：`reports/20261027_Agent选股策略_两年回测.md`（Lead 汇总，含方法、结果表、净值/回撤 SVG、偏差与证伪条件）。
