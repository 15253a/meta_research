#!/usr/bin/env bash
# 测试替身：进程非零退出（触发 RunnerError 进程失败路径）。
cat > /dev/null
echo "模拟崩溃" >&2
exit 3
