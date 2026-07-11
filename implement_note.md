# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c 敌对隔离与最终长程验收
- 检查点状态：CP11.4b 功能已提交 `e236487`；唯一全量 1332 passed；build_log 0063 已记账；CP11.4b 达成

## 刚完成什么

CP11.4b 已把 source tree、checkpoint 与 imported artifact 从“hash 后再按 path open”改为 fd capability：同一
`O_NOFOLLOW` fd 上取得 device/inode/size/hash 并读取，source tree 从稳定 root dirfd 相对遍历，外部进程通过
`/proc/self/fd` + `pass_fds` 消费，使用后复核 capability、路径绑定或整棵树。manifest、attack、harness 与 import worker
已接入，path swap、symlink 和内容变化均 fail-closed。

所有 Codex 调用现在有本地 invocation identity；guardian 将 stdout/stderr 写入 0600 capture，terminal receipt 锚定
inode/size/hash。owner 在 provider 返回与 DB 提交之间死亡时，可从 capture 重建严格 `provider-invocation-v1` receipt，
再把 runner_call、usage 与本地 policy cost ledger 在一个 WriteDaemon 事务中只对账一次。usage 冲突、capture 失败或无法
确认时记 unknown 并 fail-closed；regular/query 恢复不伪造业务成功。

## 验证 / Review

- 开发期相关组：runner 32、supervisor 22、execution/reconcile 36、cost/query 76、artifact/import/manifest 87、run 47、
  attack 76、manifest/import/stage 123 均通过；compile/check 通过。
- 检查点末唯一一次全量：`pytest -q` → `1332 passed in 313.81s`，无失败、无第二次全量。
- 外审第 1 轮独立账号 401，无 verdict；第 2 轮 `REQUEST_CHANGES`。成立的 capture 无限 running、README overclaim、
  fd rewind 与 str→bytes 均已修；specialized owner 与 unique-index 两项经冻结架构/单写事务核实不成立，增加注释和
  active-transaction 机械断言。两轮上限后未发第 3 轮。
- 功能提交：`e236487e31c4f30de1ae2d344afc4100e8faebca`；尚未 push。

## 当前关键边界

- CP11.4b 完成不等于 provider 侧 exactly-once：只保证每个已观察 invocation 在本地入账一次；policy cost projection
  不是供应商 invoice。
- 同 UID 敌手仍可能攻击嵌套目录/进程证据；container/cgroup/VM 等强隔离、guardian/receipt 跨信任域防篡改、VEPFS
  跨节点 owner/fd 语义和含真实外调/故障注入的 100+ 轮 soak 尚未完成。
- CP11.3c 的 120 轮是控制面/状态投影回归，不是 100+ 轮真实 provider 工作负载验收。
- 不得 push；继续按用户节奏：开发期只跑相关验证，检查点末只跑一次全量，再提交功能和记账。

## 下一步动作

1. 逐条映射 `reference/` 对敌对代码执行、资源限制、owner/receipt 信任域、跨节点共享文件系统和长程验收的原始要求，
   划定 CP11.4c 可在当前环境机械证明与必须诚实标注为部署验收的部分。
2. 设计最小强隔离边界：优先 rootless container/user+mount+pid+network namespace、cgroup v2 与只读/显式 capability mount；
   环境不支持时 fail-closed，不以普通 subprocess/same-UID 权限冒充沙箱。
3. 把 guardian/capture/provider receipt 放入隔离执行者不可写的信任域，并补篡改、owner-death、资源耗尽与跨节点 canary。
4. 设计 100+ 轮真实 provider/失败注入 soak 的可恢复证据包；开发期只跑相关验证，检查点边界外审后只跑一次全量。
