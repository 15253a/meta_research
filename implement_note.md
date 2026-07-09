# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-09 ｜ 位置：步⑪ CP11.2b 人类控制面
- 检查点状态：CP11.2a 已提交并记账；CP11.2b 待逐文件复核与自验

## 正在做什么

CP11.2a 功能提交 `c077cd2` 已闭合用户文件原子接纳、goal-wide 有界回执、
稳定 fd 真消费与生成时最小授权；staged-only 全量 `844 passed`，记录见 build_log 0049。
当前转入 CP11.2b：复核已隔离的 console confirm/reject、file request resolve/cancel 和生产 notifier 接线。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP10.2 `03d3ffd` + build_log 0047；CP11.1 `ac53516` + build_log 0048；
  CP11.2a `c077cd2` + build_log 0049。
- CP11.2b 未提交：`console.py`、`console_ingest.py`、`console_server.py`、`run.py`、
  `test_console_e2e.py`、`test_console_frontend.py`、`test_console_ingest.py`、`test_console_server.py`、
  `test_run.py`、`views/console/index.html`。
- CP11.2a 外审两轮均 `REQUEST_CHANGES`；第 2 轮上限后全部问题已本地修复，
  三路内部最终复核均 `APPROVE`，未开第 3 轮。

## 下一步动作

1. 逐文件复核 CP11.2b 的 HTTP→spool→ingest→权威 DB 终态链，重点核对 DB mode=ro、路径 containment、幂等、provenance 和 inbox 崩溃恢复。
2. 跑 console/ingest/run 定向测试，确认与已提交 CP11.2a 的 FileRequestService 新约束兼容。
3. 只 stage CP11.2b 文件，跑 codex 边界外审（最多两轮），然后落功能提交与 build_log 0050。

## 关键上下文 / 坑

- console_server 必须继续用 SQLite `mode=ro`；POST 只能追加 spool，不得直写主库。
- 只有 run 进程内的 ConsoleInboxIngest/FileRequestService 能把 confirm/reject/resolve/cancel 迁入权威状态。
- resolve 来源必须受虚拟根白名单和 resolve+containment 约束，不得跟随 symlink 逃出运行根。
- confirm/reject 必须绑 directive_id 与 provenance message，不得依赖自然语言「确认」分类。
- CP11.4 仍负责 prompt/code 强隔离、容器/VM 与内容寻址 artifact store；CP11.2 不得宣称这些已完成。
