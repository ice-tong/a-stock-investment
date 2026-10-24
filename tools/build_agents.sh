#!/usr/bin/env bash
# 把 prompts/通用规则.md 拼进 prompts/agents/*.md，同时生成 Claude Code（.claude/agents/）和 Qoder（.qoder/agents/）的子代理。
# 两者的子代理格式相同：YAML frontmatter（name / description / tools）+ Markdown 正文作为系统提示词。
# 修改提示词后重新运行：bash tools/build_agents.sh（改的是 prompts/ 下的源文件，不要直接改生成目录）
set -euo pipefail
cd "$(dirname "$0")/.."

for dir in .claude/agents .qoder/agents; do
mkdir -p "$dir"
for src in prompts/agents/*.md; do
  out="$dir/$(basename "$src")"
  # frontmatter（第一个和第二个 --- 之间）原样保留，其后插入通用规则，再接角色正文
  awk 'BEGIN{n=0} /^---$/{n++; print; if(n==2) exit; next} {print}' "$src" > "$out"
  { echo; echo "<!-- 由 tools/build_agents.sh 生成，请修改 prompts/ 下的源文件 -->"; echo
    cat prompts/通用规则.md; echo; echo "---"
    awk 'BEGIN{n=0} n>=2{print; next} /^---$/{n++}' "$src"; } >> "$out"
  echo "生成 $out"
done
done
