#!/usr/bin/env bash
# 测试替身：输出不含 json 代码块的回复（触发 RunnerError 信封不可解析路径）。
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat > /dev/null
printf '这是一段没有任何代码块的自由文本回复。\n' > "$out"
