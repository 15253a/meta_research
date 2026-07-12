# implement_note.md · 施工现场（活文档，只写当下）

- 更新：2026-07-11 ｜ 位置：步⑪ CP11.4c.2b.3 deployment trust contract
- 检查点状态：CP11.4c.2b.2c 功能提交 `c0ba5ed` 已完成，CP11.4c.2b.2 repository closure 收口；
  当前转入最小部署可用性，CP11.4c.2/CP11.4c 父项仍未达成

## 刚完成什么

缺 adapter 时，系统从已验证 repository ledger/tree 构造有界投影，只做一次 tool-free 生成和一次独立 tool-free
评审；通过后仍由既有 schema、机械 adapter 编译器、exact dependency image 和 adversarial sandbox smoke 决定能否入
snapshot。sidecar 不修改 Git tree；生成调用、成本、decision、prompt/model/config、projection/candidate/adapter/verdict hash
复用现有审计链。科学 target identity 不含 DB-local ID 或自由文本，显式 adapter 和 legacy import 路径保持兼容。

CP11.4c.2b.2 的三部分现已齐备：exact Git LFS objects、canonical wheel lock → exact/restorable dependency image、
以及缺 adapter 的受审生成。没有加入隐式在线 pip、仓库脚本执行、多轮 agent 状态机或第二套 receipt store。

## 验证 / Review

- 相关验证：`325 passed`；修复后 focused `185 passed`；projection/service/schema/SKILL `115 passed`；最终
  service `12 passed`；materializer identity/provenance、compile 和 diff check 通过。
- 外审按两轮上限：第 1 轮隔离 codexro token 失效返回 401；第 2 轮服务 reconnect、无输出；均无 verdict。
  内部并行审计的 provenance/identity、错误分类和预算问题均已在功能提交前修复。
- 检查点末唯一全量：`1443 passed, 1 skipped, 1 failed in 1051.15s`。唯一失败是无关 dependency image
  `docker save` 写 `/ebs/docker/tmp` 时 `no space left on device`；已清精确测试 image，未重跑第二次全量。
- 真实 missing-adapter Codex canary 在生成首调用被隔离 `/home/codexro/.codex` 的 revoked refresh token 阻断；系统正确
  fail-closed 并记录未知成本失败，未伪造通过。恢复认证需用户/运维授权。
- 功能提交：`c0ba5ed`；本记录随独立文档提交收口；未 push。

## 当前可用边界

- 显式 adapter v2/v3 路径可用；缺 adapter 生成路径代码闭环已具备，但此机器必须先恢复隔离 Codex 凭证才能实际启用。
- canonical Python wheel lock 项目可获得 exact/restorable project image；普通 requirements/Poetry/uv lock 只作证据，
  不会隐式在线安装。因而并非任意 ML/SOTA repository 都能自动运行。
- 当前已有 pinned Docker、guardian、quarantine、seccomp/rlimit，但生产 service account/VM、Docker socket、
  cgroup/device/GPU 与 VEPFS hard byte+inode quota 尚未通过目标环境预检，开发模式不得冒充生产通过。
- 两节点 exact archive/owner/lease/fd 与真实 100+ 轮 soak 属 CP11.4c.3；CP11.3c 的控制面 120 轮不能替代。
- 不得 push；开发期只跑相关验证，下一个检查点冻结后再做一次全量。

## 下一步动作

1. 先恢复隔离 Codex 凭证，并只跑一条真实 generation→independent review→Docker smoke canary，确认新路径可操作。
2. CP11.4c.2b.3 只实现一个薄的 fail-closed deployment preflight/receipt：核对 service identity、Docker daemon/socket、
   cgroup/device/GPU 与配置的 VEPFS hard byte+inode quota；不建设新的部署编排器。
3. 常见科学运行时覆盖优先采用 pinned scientific bootstrap image 或 verified-hash 标准 lock 转换，绝不退回无 hash 在线 pip。
4. 部署合同完成后，再准备 CP11.4c.3 两节点、真实 100+ 轮和故障注入证据包。
