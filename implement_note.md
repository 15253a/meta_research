# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-12 ｜ 位置：步⑪ CP11.4c.3b.2b registered asset closure
- 检查点状态：CP11.4c.3b.2a 已提交；转入 b.2b 最小 raw-log/import-object 闭包

## 上一检查点结果

- 功能提交 `011c98b`：exact lease 下离线验证 snapshot chain、默认深验/保护最近 3 代、SQLite-only
  no-clobber restore、canonical plan + 显式 hash 的 applied-plan backup GC，以及 cycle backup 前 bytes/inodes
  headroom 门；没有 daemon、第二 DB 或自动删除器。
- 相关 **118 passed**；内部两路最终 APPROVE。外审第 1 轮凭证 401、第 2 轮接收完整 diff 后 5 分钟无
  verdict，按两轮上限终止。全量依用户要求只在最终检查点做；当前 overlay 余量也不足已知 191MB basetemp。

## 当前可用边界

- 百轮运行已有同步逐轮 recovery-point 主干；离线 verify 的昂贵 SQLite 深验固定在最近 3 代，GC 触及时
  才单次读取旧 backup。restore 能在 storage subtree 尚存时恢复 SQLite 真相并诚实 adoption。
- 当前不是完整 DR：不复制原 views/storage timeline，不恢复 checkpoint/log/import objects；同一 VEPFS
  failure domain 也不防 fileset/站点丢失。GC 目前只作用于 backup CAS。
- production preflight、T1/T2 firewall、两节点 canary、≥200 轮真实 fault soak 与 evidence pack 仍未完成。

## 当前动作

1. 复用现有 `execution_log`/checkpoint/import-materialization receipt 与 CAS，不加新数据库或后台服务。
2. 只补两件事：raw log 的确定性压缩镜像（冻结原件保留）与 import indexes/objects 的离线可达闭包校验/恢复。
3. 先跑各自相关测试；b.2b 收口后再做一次检查点提交，全量仍留到最终验收边界。
