# 8768 基线核验

2026-09-24 的保存提交是 `863320550b1c6d7ab104946844eddc9810a3f152`（`baseline/v0.0.0`）。包内 286 文件与活跃运行环境逐字节 SHA-256 一致，759 项必要源码/测试/依赖/文档被保存；另外记录了 38 项排除的临时运行捕获和依赖特定部署的手工脚本。正式视觉测试基线保持保留。

## 隔离回归

123 项中 **121 通过、2 失败**。两项失败单独重跑均复现。本轮没有修改测试断言或生产实现，不把该保存点称为全绿发行版。

| 范围 | 通过 | 失败 |
|---|---:|---:|
| public research assets | 35 | 1 |
| asset roles | 1 | 1 |
| research datasets / derivations / effect scope recovery | 28 | 0 |
| asset content pages / public asset HTTP | 18 | 0 |
| public HumanRequests / material intake | 39 | 0 |

解释器为当前运行环境的 Python 3.12.13；当前环境没有 pytest，runner 使用已有 pytest 8.4.2（仓库锁定 8.4.1），没有安装依赖。测试数据位于独立临时目录；禁用真实 provider/auth 模板、插件自动加载和源目录缓存；审计 guard 禁止网络、子进程及隔离根以外 SQLite。两轮 guard 拦截数均为零。

### 快照断言与动态字段

`tests/test_public_research_assets.py::test_healthy_managed_handoff_alias_keeps_projection_revision_atomic` 已确认 revision 未变化，随后全对象比较包含 `observed_at` / `query_diagnostics` 动态字段而失败。当前证据支持断言未适配动态诊断字段，不能据此推断资产损坏。

### 写锁内的证据深检

`tests/test_public_research_asset_roles.py::test_rg_accepts_precise_asset_roles_and_revalidates_current_evidence` 在 Idea v4 分支复现 writer lock 内调用 `verify_evidence_refs`。`owners/advancement_engine.py:3983` 的现有约定要求 hash 深检在锁外，`:4000` 进入事务后 `:4043` 再次深检，形成实际边界偏离。尚未测量大文件负载下的延迟；该测试后续还保留旧 v2 schema 预期，应在后续设计/修复时一起核对合同。

## 验证限度

没有运行完整前端、真实模型、全部测试、平台安装或灾难恢复。运行数据反馈是只读聚合；数据库最近验证状态不等于本轮重新核验全部研究字节。8768 进程未重启，数据未迁移。
