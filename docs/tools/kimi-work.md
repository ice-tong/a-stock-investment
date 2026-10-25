# Kimi Work 专属上下文

> 通用规则见 [AGENTS.md](../../AGENTS.md)，接力流程见 [跨工具接力准则](../跨工具接力准则.md)。本文件只写 Kimi Work 与其他工具不同的地方。

## 规则入口
工作区自动注入 `AGENTS.md`，无需额外配置。

## 子代理调度
**不可用。** 按 [跨工具接力准则](../跨工具接力准则.md) 第五节降级执行：依次读取 `prompts/agents/<名称>.md` 按角色串行分析，输出中注明"本报告由单线程降级流程产出"。

## 定时任务
有 Automation（cron 任务）能力，适合承担定时工作。
**当前不承担模拟盘定时任务**（归属见 `docs/进展与交接.md` 接手规则第 2 条）；如需接管，先在现承担工具里删除/禁用旧任务，再在本工具创建，并更新归属记录。

## 金融数据插件（Kimi Work 特有优势）

宿主预装官方金融插件，经 agent-gw 网关调用、凭证由环境提供。这些插件**只在 Kimi Work 会话内可用**（Qoder / Claude Code 没有），调用消耗账号额度——批量、高频、pipeline 取数仍优先 `tools/ashare.py`；插件用于 AKShare 失败时的替代、单点核实与交互式研究。

| 插件 | 能力 | 对应项目环节 |
|---|---|---|
| `ifind`（同花顺） | A 股实时/历史行情、实时技术指标、三大报表、公告、股东、盈利预测、智能选股 | fundamental / valuation / technical-flow / 快速问答 |
| `wind-allskill` | Wind：A 股行情财务、基金、指数板块、债券、宏观 EDB、公告与财经新闻 | macro / fundamental / valuation |
| `dongcai-mx-data` | 东财妙想：行情财务、宏观、智能选股、资讯搜索、专项诊股、各类研究报告生成 | industry / event-sentiment / 报告生成 |
| `cls-news` | 财联社实时快讯流（A 股盘面最快中文源）+ 栏目深度报道 | event-sentiment / 盘后复盘 |
| `imf` / `world_bank_open_data` / `igo_open_data` | IMF WEO、世行、FRED/WHO/OECD 等官方宏观序列 | macro |
| `yahoo_finance` / `sp_data` / `sec_edgar` | 美股与海外基本面、SEC  filings | 海外对照（本项目 A 股为主） |
| `financial-market-terminal` | 预置市场日报看板 + 定时任务（与 Qoder 看板/定时任务功能重叠，**不要重复搭建**，仅作参考） | 不启用 |
| `dynamic-finance-charts` | 一句话出金融图表（K 线、PE band、对比图等） | 报告配图 |

调用方式（以 `ifind` 为例，其他插件结构相同）：

```bash
python3 -c "import agent_gw"                       # SDK 已在宿主 Python 就绪
python3 "<插件根目录>/scripts/ifind_tool.py" describe          # 查看接口说明
python3 "<插件根目录>/scripts/ifind_tool.py" call \
  --api-name "<接口名>" --params-json '{"ticker":"600887.SH","file_path":"data/xxx.csv"}'
```

插件根目录：`~/Library/Application Support/kimi-desktop/daimon-share/daimon/runtime/kimi-code/home/plugins/managed/<插件名>/`。

口径注意：
- 代码后缀 `.SH/.SZ/.BJ/.HK/.US`（`tools/ashare.py` 用 6 位数字，切换时记得转换）。
- `ifind_get_stock_realtime_price` 单次最多 3 个代码；`realtime_tech` 不支持 ETF 与科创板 688xxx。
- 数据落地为 CSV 后用 pandas 计算，不要心算（与 `prompts/通用规则.md` 一致）。

已验证（2026-10-25）：AKShare 代理故障期间，`ifind_get_stock_realtime_price` 成功返回 600887.SH 行情（冒烟样例 `data/ifind_smoke_600887.csv`，接口说明快照 `data/ifind_desc.md`，均为临时文件不入库）。

## 特有约束与技巧
- 模拟盘定时任务归 Qoder（见上手规则），Kimi Work 内**不要**为 execute / evening / research 再建 Automation。
- 会话内临时展示可用 `AutomationControl.runPluginWidget` 的 iFinD 预设卡片（行情卡 / 公司卡），但每次运行消耗额度，单轮最多 5 次；不要主动提议把卡片常驻看板。
