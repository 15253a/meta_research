# PR #297 CI 修复复验

本轮受验提交为 `568da247b30389b180571774b25516544d0c9ddb`，增量起点为 `8fe94e5`，目标基线仍为 `7746456`。此记录只补充 CI 修复与证据，不替代原有稳定性审核。业务源码、依赖、workflow 和性能预算没有改变。

## 复现与修复

1. 原始 `npm test` 得到 40 通过、5 失败。五例均报 `MetaTrace is not defined`。定向复现为 0 通过、5 失败。测试从 main.tsx 抽取函数，未带入实际页面已有的组件 import。VM context 加入与已有子组件相同的字符串绑定，原五例业务断言不变。
2. 完整 CI 随后运行到 response-time，两个页面用例失败，API 用例通过。页面用例等待已经删除的 `.root-session-context`，计时样本均为零，不能解释为性能超标。追溯到基线祖先 `1e0a9e8` 的 UI 改版；相关测试和生产组件在 `7746456..8fe94e5` 完全相同。
3. Question 校验改从现有 `current-cycle-overview` 读取精确 `data-question-ref`，继续匹配精确 Session。输出就绪检查改为会话内 `.root-session-live-time .research-execution-clock`，该节点在公开输出响应读入后渲染。旧 `.root-operation-clock` 现仅代表后续记录补读。错误断言、失败重抛、双动画帧、样本数及阈值全部保留。

只修改两个测试文件的三行。没有删除或跳过测试，没有新增 catch，没有修改业务逻辑来迁就测试。等待完整当前问题概览比允许先展示 live overview 更严格。

## 验证

- 本地原五例 5/5 通过，完整 npm test 45/45 通过，无跳过。应用与 E2E 类型检查、Vite 构建通过。
- [完整 PR CI](https://github.com/15253a/meta_research/actions/runs/42967795536) 在 Ubuntu 24.04、Node 22.23.1、Python 3.12 上通过依赖安装、45 个逻辑测试、两套类型检查、构建及三个 response-time 用例。无跳过或 flaky。
- CI 的合并提交 `d0e8860f` 父提交为 `7746456` 和 `568da24`。其 Git tree 与受验 head 相同，未包含未验证的目标分支变化。
- 六组计时各有 20 个样本，均通过原 p95 与最大值限制。原始样本、环境和边界见 [ci-response-time.json](ci-response-time.json)。失败日志与成功日志均保留在工作区外，哈希见 [ci-verification.json](ci-verification.json)。

## Claude Code 与独立审核

实际本机 Claude Code 2.1.285 使用 `claude-opus-5-5`、effort max，对最终两文件增量、相关组件实现、CI 配置、失败与成功证据和既有审计限制进行深度审核。原文见 [ci-claude-review.txt](ci-claude-review.txt)，运行元数据见 [ci-verification.json](ci-verification.json)。最终结论为 PASS，blocking_findings 为空。两轮进程均退出成功，审核期间源码不变。首轮范围说明中的无关措辞与目标分支证据疑问已在同一实际会话复核中纠正，原始记录保留在工作区外。

本轮采用有限源码与证据包输入，Claude 未亲自执行测试、访问 GitHub 或复算文件哈希。父任务执行测试与 Git 树等价核验，独立非作者代理复跑 npm 测试并复算 CI 原始样本的 p95/max。合并前的最终 head、CI、base 与审核确认另记录到 PR，不能把本记录当作已合并声明。

## 边界

response-time 使用临时数据目录、随机本地端口与确定性 provider。计时不包含启动、认证、构建或真实模型推理。Snapshot 测量 warm HTTP 交付；阶段切换可复用已读输出，不证明每次都重新下载。这里报告原预算通过，不声称生产性能提升或全科研链验收。

此前后端 249 例中的 27 个基线复现失败仍按原审计保留，未将其写成全绿。本轮未部署、未操作研究数据、未关闭 #295/#274，也未推进其他 ticket。

采用 Laziness Protocol，将变更限于三个必要测试绑定或选择器。采用 Prove It Works，保留两阶段失败复现并完成实际 CI。采用 Explain the Number，将计时结果限定为临时确定性产品上的工程预算检查。
