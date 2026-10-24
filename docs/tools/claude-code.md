# Claude Code 专属上下文

> 通用规则见 [AGENTS.md](../../AGENTS.md)，接力流程见 [跨工具接力准则](../跨工具接力准则.md)。本文件只写 Claude Code 与其他工具不同的地方。

## 规则入口
`CLAUDE.md`（仓库根目录）内容为 `@AGENTS.md`，Claude Code 启动时自动导入。

## 子代理调度
- 定义在 `.claude/agents/`，由 `bash tools/build_agents.sh` 从 `prompts/` 生成，不要直接改。
- 用 Task 工具按子代理名称点名委派，支持并行。
- 委派 prompt 必须自包含（标的、日期、问题），子代理看不到主对话。

## 定时任务
Claude Code 无内建定时能力，需要外部 cron（crontab / launchd）配合。
**当前不承担模拟盘定时任务**（归属见 `docs/进展与交接.md` 接手规则第 2 条）；如需接管，先在现承担工具里删除/禁用旧任务，再配置外部 cron，并更新归属记录。

## 特有约束与技巧
（暂无，遇到后补记到这里）
