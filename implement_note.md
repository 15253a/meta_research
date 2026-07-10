# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-10 ｜ 位置：步⑪ CP11.2b 人类控制面
- 检查点状态：在建；先按可独立回退边界继续拆分控制面，而不是一次提交全部 WIP

## 正在做什么

CP11.2a 功能提交 `c077cd2` 已闭合用户文件原子接纳、goal-wide 有界回执、
稳定 fd 真消费与生成时最小授权；CP11.2a.1 `3c4c9b4` 又把逐组件上传树身份、同 fd 复制复验和
resolve 级遍历预算拆成独立检查点，记录见 build_log 0049/0050。

当前继续 CP11.2b：HTTP 进程只把已鉴权输入耐久追加到 spool，run 单写进程负责分类、确认/拒绝、
文件 resolve/cancel 与通知扫描。提交前还要闭合结构化动作的跨域幂等、所有公开指令的真实状态语义、
abort 后 active question 释放、生产通知投递、失败回执可见性以及前端只展示权威数据等问题。

## 工作区状态

- 分支：`fix/architecture-hardening-20260709`。
- 已提交：CP10.2 `03d3ffd` + build_log 0047；CP11.1 `ac53516` + build_log 0048；
  CP11.2a `c077cd2` + build_log 0049；CP11.2a.1 `3c4c9b4` + build_log 0050。
- CP11.2b 未提交：console/run/frontend/README 改动、`console_spool.py`、浏览器 auth smoke，
  以及 `test_notify.py` 中结构化指令 provenance 适配。
- CP11.2b 工作区上次全量：`946 passed, 2 failed`；两项已知失败分别是前端静态测试误扫 demo mock，
  以及 auth smoke 尚未把 snapshot 标为 fresh。它们不是唯一剩余问题，不能只修到测试变绿就宣称完成。
- CP11.2a 外审两轮均 `REQUEST_CHANGES`；第 2 轮上限后全部问题已本地修复，
  三路内部最终复核均 `APPROVE`，未开第 3 轮。
- CP11.2a.1 外审第 1 轮 `APPROVE` 后修正 2 SHOULD + 1 NIT，第 2 轮 `APPROVE` 且无遗留；
  staged-only 定向 `80 passed`、全量 `857 passed`。

## 下一步动作

1. 盘点 CP11.2b 的依赖关系并拆成能独立验证/回退的子检查点，避免一次提交数千行交叉改动。
2. 优先修复结构化动作跨域幂等、无真实语义的公开指令、abort 遗留 active question、坏动作终态收敛。
3. 闭合生产 notifier/回执与前端权威投影，恢复定向和全量测试绿色，再按每个子检查点执行 staged-only 外审和提交。

## 关键上下文 / 坑

- console_server 必须继续用 SQLite `mode=ro`；POST 只能追加 spool，不得直写主库。
- 只有 run 进程内的 ConsoleInboxIngest/FileRequestService 能把 confirm/reject/resolve/cancel 迁入权威状态。
- resolve 来源和 HTTP 文件读取必须持有逐组件 `openat(O_NOFOLLOW)` 得到的 fd capability，不能只做 Path containment 后再打开。
- spool/token 必须拒绝 symlink、hardlink、FIFO/设备，身份不能依赖可复用的行号；首次创建须 fsync 父目录链。
- 默认 CLI 必须在 pause/file block 时继续消费控制动作与扫描提醒；一次性批处理只能由显式 `--once` 请求。
- 所有 `/api/*` 必须同时满足 loopback/Host/Origin 与持久 Bearer capability；token 只经 URL fragment 引导并存 sessionStorage。
- confirm/reject 必须绑 directive_id 与 provenance message，不得依赖自然语言「确认」分类。
- CP11.4 仍负责 prompt/code 强隔离、容器/VM 与内容寻址 artifact store；CP11.2 不得宣称这些已完成。
