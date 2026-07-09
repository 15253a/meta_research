# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-09 ｜ 位置：步⑪ CP11.2 人类控制闭环
- 检查点状态：代码已构建，待内部复核、隔离 staged diff、外审与提交

## 正在做什么

CP10.2（`03d3ffd`）与 CP11.1（`ac53516`）已提交。当前工作树只剩 CP11.2：
HTTP 控制台把 directive confirm/reject 和 file request resolve/cancel 写 spool，run 单写进程 ingest 后落权威状态；
已解决/已取消请求的同 hash 重做会新建 attempt，不复用旧终态。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- CP10.2：`03d3ffd` + build_log 0047；最终全量 `754 passed`。
- CP11.1：`ac53516` + build_log 0048；定向 132，当前合并全量 754。
- CP11.2 未提交文件：`console_server.py` / `console_ingest.py` / `interaction.py` / `notify.py` /
  `run.py` / 控制台 HTML + 5 个测试文件。

## 下一步动作

1. 逐文件复核 CP11.2 的 HTTP→spool→ingest→权威写路、路径 containment、幂等和 provenance。
2. 跑 console/notify/run 定向测试，只 stage CP11.2 文件并跑 codex 外审，提交。
3. 写 build_log 0049，随后分拆 CP11.3 状态语义与运行边界检查点。

## 关键上下文 / 坑

- console_server 必须继续 DB mode=ro，POST 只写 spool；所有 DB 终态只能由 run 进程写。
- resolve 来源只允许 `work/uploads/` 或 `input/uploads/` 虚拟路径，必须 resolve + containment 防 symlink/逃逸。
- confirm/reject 必须绑 directive_id 与 provenance message；不得再依赖自然语言「确认」分类。
- 每个检查点必须：定向+全量测试 → staged diff codex 外审（最多两轮）→ 功能提交 → 独立 build_log 提交。
