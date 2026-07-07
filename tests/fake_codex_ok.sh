#!/usr/bin/env bash
# 测试替身：模拟 codex exec 的最小行为——吞掉 stdin，把合法信封写到 -o 指定文件。
# 仅供 tests/test_orchestrator.py 经 METARESEARCH_CODEX_BIN 注入（不联网、不花 token）。
set -euo pipefail
out=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
cat > /dev/null
printf '会话噪声（应被解析器忽略）\n```json\n{"files": {"probe.json": {"ok": true, "note": "替身产物"}}, "md": "中文正文"}\n```\n' > "$out"
