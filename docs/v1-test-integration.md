# 将运行分支 v1-test 持续同步到 v1-enginer

`v1-test` 保存真实运行环境持续修正后的代码；`v1-enginer` 在吸收这些修正的同时，保留自己的系统 MCP 管理、配置隔离、人机协作和运行恢复能力。实际分支名为 `v1-enginer`。

## 本次同步

本次合并以 `b499ef58636b640959b0d1b353828a9bd75f64bb` 为目标，吸收 `829b701cc31f04ee75fd5a2aa6f8af371bf746c8` 及其祖先。共同基点是 `c7ee62546c15cfd668b2ddb799cb189b523b3e6d`。采用保留两个父提交的 merge commit，源分支不重写。

主要变化包括 Bundle 长备注与错误诊断修复、输出时间、研究交接、数据库读取快照和证明复用、历史成果展示、Target 输入与 checkpoint 归属、暂停恢复和搜索改进。融合中保留 enginer 的系统 MCP 全链路和新版人机协作界面，并补齐带冻结 MCP 快照的历史输出恢复。

前端从合并后的源码统一构建；两支旧的带哈希构建文件不能混选来代表融合后的产品。

## 后续同步流程

1. 获取两个远端分支，记录各自完整 SHA。以当时最新 `origin/v1-enginer` 建立干净、独立的集成分支。
2. 检查 `git log origin/v1-enginer..origin/v1-test`，确认这次新增的运行修复；同时检查双方自 merge-base 的变化，避免把 enginer 独有功能当成应删除内容。
3. 使用 `git merge --no-ff --no-commit origin/v1-test`。保留真实父子关系，不用 squash、整目录覆盖或将已合并提交逐个重复 cherry-pick。
4. 逐处解决冲突；除文本冲突，还检查 runtime binding、冻结操作身份、读取证明边界和前端 API。阶段源码、提示词、schema、能力或模型发生变化时，现有精确兼容映射不能自动推广。
5. 在 Linux 独立环境按锁文件准备 Python 依赖，运行受影响后端测试及系统 MCP/历史恢复回归；前端运行类型检查、相关浏览器回归，再重新构建静态资源。
6. 记录验证结果并完成 merge commit。发布前再次获取两支：若 test 有新提交，继续合入新增量；若 enginer 有新提交，先合入并复验受到影响的部分。只做普通快进推送，不强制改写任一远端分支。

由于上一次 test 提交已经是 enginer 合并提交的祖先，下次 merge 会自然只引入后续新增变化。此文档不创建自动监控或自动合并；后续同步仍按明确发起的一次工作执行。

## 源码同步与运行部署

这次 Git 融合不修改运行中的数据库、原生会话、签名操作文件、transport keys 或服务配置，也不代表完成部署。

旧操作的签名身份必须保持精确：MCP 配置变更可以沿原冻结快照恢复；真实的 root profile、提示词或 output schema 变更不能借 MCP 兼容映射放行。历史成果的只读兼容也不能当作新执行的许可。

实际部署前应读取当时的冻结绑定、运行中操作和会话状态，单独验证恢复或迁移路径。仅存在源码级兼容 fixture，不能宣称所有线上旧会话都能无缝执行新版本。

## 本次验证记录

验证使用独立 Linux 临时目录和锁定的应用依赖，没有访问生产数据库或调用实际研究模型。以下为分批结果，存在重复用例，不应直接相加为全量通过数：

| 范围 | 结果 |
|---|---|
| Bundle notes、文件依赖缓存、搜索和读取边界首组 | 21 passed |
| 50 个受影响后端模块，四组并行 | 初次 341 passed / 9 failed / 20 errors / 2 skipped；20 个 errors 来自本次新增测试漏填注册名，已修正，新增历史恢复模块单独复验 20 passed |
| 精确绑定桥、Bundle MCP、原 Reasoning/root prompt 兼容 | 276 passed |
| Idea MCP、已签名 usage-limit 分类和重放 | 6 passed |
| 5 类 provider 实际调用和系统 MCP execution | 13 passed |
| 前端应用与测试 TypeScript 检查、Vite 构建 | passed |
| Node 实验日志回退和 Target facts | 18 passed |
| 选定 Playwright 界面回归 | 78 passed / 5 failed；5 项均在原 enginer 构建产物上同位置复现 |
| SSE 刷新、长文本窄屏、资料库脚本 | passed |
| 最终暂存 Git tree 导出的兼容、恢复、MCP 执行及 Bundle notes 复验 | 307 passed |

后端 9 个失败逐项在原始 `829b701` 复现：4 个 Bundle 历史测试仍断言旧验证调用路径，3 个 Target 输入测试桩缺少新增 `read_snapshot`，1 个 Bundle 暂停和 1 个额度恢复测试遇到既有 `stage_provider_hard_ceiling_stale`。原分支对照另一次运行中两个暂停参数均失败；没有将这类不稳定结果计作融合引入的新回归。2 个跳过项需要指定版本的真实 Codex 原生可执行文件。

前端 5 个既有失败是旧人机协作入口/折叠内容预期。另有两类 test 分支旧断言已在本次修正：时间线摘要保存时间文案，以及读取折叠详情前的展开动作；产品行为未为测试改写。

未运行全仓库所有旧测试，也不声称全量测试通过。原 enginer 的更广泛历史审计边界继续保留在 `system-mcp-registration-acceptance.md`。
