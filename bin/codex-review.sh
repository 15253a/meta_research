#!/usr/bin/env bash
# codex-review.sh — pre-commit 评审入口（模式 A：codexro-review 直接读仓库）
#
# 评审【已 git add 的暂存改动】：把 diff 内联进 prompt，并允许 reviewer 自行
# rg/读仓库其余文件核实跨文件假设。reviewer = 无写权限的 codexro 用户（见
# CLAUDE.md §2.1）。“最多反复 2 轮”由调用方按 CLAUDE.md §2.2 控制。
#
# 用法：
#   bin/codex-review.sh "<一句话决策>" [追加 codex 参数...]
#   例：bin/codex-review.sh "M0a 冻结四阶段 JSON Schema" -c model_reasoning_effort=high
#
# 退出码：0 = 已产出评审（去读结论）；2 = 暂存区无可评审改动。
set -euo pipefail

decision="${1:?用法: bin/codex-review.sh \"<一句话决策>\" [追加 codex 参数...]}"
shift || true

repo="$(git rev-parse --show-toplevel)"
work="$(mktemp -d /tmp/codexrev.XXXXXX)"    # 随机目录名，避免可预测/并发覆盖
chown codexro:codexro "$work"                # 700 归 codexro：评审进程可写输出、他人不可篡改送审内容/伪造结论；root（本脚本）仍可写
diff_file="$work/staged.patch"
prompt="$work/prompt.txt"
out="$work/verdict.md"
# 注：$work（/tmp/codexrev.*）故意保留供审计/排障；内含 staged diff，介意可手动清理

# 评审暂存区改动；排除 build_log/（施工记录不参与评审）。不吞 git 错误：失败即 set -e 退出
git -C "$repo" --no-pager diff --staged -- . ':(exclude)build_log/**' > "$diff_file"
if [ ! -s "$diff_file" ]; then
  echo "！暂存区无可评审改动（git diff --staged 为空，已排除 build_log/）。" >&2
  echo "  先 git add 本次决策相关文件再评审。" >&2
  exit 2
fi

{
  echo "你是严格的只读 code reviewer。下面是一次「$decision」的暂存改动 diff。"
  echo "审查重点：正确性 bug、契约破坏、并发/错误处理隐患、可回退性。"
  echo "你可以自行用 rg / 读取 $repo 下的其它文件来核实跨文件假设（调用方、类型/接口定义、契约波及面）。"
  echo "按 [BLOCKER] / [SHOULD] / [NIT] 分级列出问题；最后单独一行给结论："
  echo "  VERDICT: APPROVE         （无 BLOCKER，可提交）"
  echo "  VERDICT: REQUEST_CHANGES （有 BLOCKER，需改）"
  echo
  echo "=== 决策 ==="
  echo "$decision"
  echo
  echo "=== DIFF（staged，已排除 build_log/）==="
  cat "$diff_file"
} > "$prompt"

echo ">> codexro-review 评审中（gpt-5.5/xhigh，全新 ephemeral 会话）…"
echo "   仓库=$repo"
echo "   输出=$out"
codexro-review "$repo" "$out" "$prompt" "$@"

echo
echo "================ 评审结论（$out）================"
cat "$out"
echo "================================================="
echo "（CLAUDE.md §2.2：APPROVE→可 commit；REQUEST_CHANGES→改后最多再评 1 轮，"
echo "  第 2 轮仍不过则凭反馈自行改、不再送 codex。commit 后务必写 build_log/。）"
