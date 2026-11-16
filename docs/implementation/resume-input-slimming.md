# 同会话 resume 输入精简验证

本次修改以远端 `v1-test` 的 `060b2fbef440c7570ea8dd0a4cf4b0c5226d1946` 为基线，在独立 worktree 的 `codex/resume-input-slim-v1` 分支完成。开始时该 checkout 干净；其他 checkout 的未提交工作未触碰。交付代码、测试和输入样本，不合并、不部署。

范围按当前 [#262](https://github.com/15253a/meta_research/issues/262)、[#295](https://github.com/15253a/meta_research/issues/295) 与产品要求 [#169](https://github.com/15253a/meta_research/issues/169) 核查。旧调研只作为查找线索；特别是 Bundle 按本次基线源码重新确认。

## 最终行为

| 路径 | 首次或替换会话 | 同原生会话后续 turn |
|---|---|---|
| Target | 研究正文、执行合同、候选和工作目录正常初始化 | 当前修正反馈与 Target/run 指代；不重发旧科学正文 |
| Idea、Plan | 原问题、冻结上下文及 IdeaSet 正常到达 | 本次反馈和准确上下文引用；材料缺失时定向补读 |
| Reasoning | 正常初始化；summary、facts、creation_result 首次出现时提供正文 | 同阶段纠错仅本次反馈及实际改变的字段；无法验证前序证据时保守保留正文 |
| Bundle | 保留当前 primary、多 Target dispatch、汇齐结果后决策的执行方式 | 已验证送达且有准确 reader 的旧正文不重发；新候选、新结果、当前调度状态和权限保持完整 |
| 共享包装 | 原运行条件、目录和必要工具说明 | 条件、目录授权、外部服务配置改变时送达；运行时间观察仍更新；固定流程不逐轮重发 |
| 人类输入 | 本次原文、附件、适用范围及必要关联 | 活跃 turn 使用 `turn/steer`；结束或暂停后在原 thread 启动新 turn；按宿主确认的原文身份去重 |

原生 system/base instructions、developer 配置、工具授权、完整输出 schema 和业务接纳校验均保留。没有把业务正文搬进 developer，也没有新增持久化表、manifest、交付账本或通用逐轮 diff 系统。

精简依据来自已有签名 operation 的输入/完成证据。Target 使用已有 operation generation；共享包装使用宿主已有 provider unit 顺序，避免条件 A→B→A 后继续误发旧条件。人类输入还检查签名 native start/steer 接受证据。结果不明的同一 operation 使用原封存输入对账，不重写输入或再次发送。新 native session 不继承旧 session 的送达证据。

Bundle 的旧正文省略需要实际旧 prompt 中存在对应全文，而非仅在业务请求中存在。只有一个科学版本的准确 ref 可复用全文送达证明；存在互相冲突版本、损坏证据或缺少 reader 时保守保留正文。四次连续 dispatch 的回归证明三个 resume 都没有再次追加旧候选。

没有假设 compact 保留首轮 user 全文。续轮保留必要准确引用并沿既有文件、上下文和材料 reader 补读；没有新增周期性全文补发。

## 可重复样本

`tests/resume_input_samples.py` 运行 33 个确定性场景。基线使用上述提交的原始生产源码，只覆盖六个列在 capture manifest 中的测试采样文件；候选使用当前工作树。每侧每场景运行一次，完整 prompt、argv、output schema、native 协议请求、原始字符/UTF-8 大小和源码 SHA-256 均保存在本地 `.scratch/resume-slim/comparison/`。

下表仅将生成的测试目录和源码目录替换为 `<fixture>`、`<source>`，以排除不同临时路径的长度差。原始值仍保留在每侧 `sizes.json`。

| 场景 | 修改前字符 | 修改后字符 |
|---|---:|---:|
| Target 同会话继续 | 27,977 | 89 |
| Target 当前纠错 | 5,122 | 454 |
| Idea 同会话继续 | 7,154 | 131 |
| Plan 同会话继续 | 12,116 | 131 |
| Reasoning 同会话继续 | 3,363 | 151 |
| Reasoning summary 同阶段纠错 | 4,124 | 83 |
| Reasoning creation 同阶段纠错 | 4,913 | 85 |
| Bundle 后续 dispatch | 11,143 | 1,999 |
| Bundle 新结果决策 | 20,774 | 7,931 |
| 原生人类指导 steer | 1,350 | 550 |

Idea、Plan、Reasoning 首次和替换输入的字符数均与基线相同。Target 样本 `replacement-session-continue` 模拟保存的 native id 不存在，因此再次完整初始化是预期行为。新科学事实的完整到达由具体正文断言验证，不以字符大小推定。

这些是应用实际 process runner 边界的输入；native-human 样本由真实子进程中的 app-server 协议 fixture 收到 `turn/start` / `turn/steer` 后记录。响应和终止证明由测试 fixture 控制。另有生产 driver → adapter → supervisor → native bridge 的端到端协议测试，覆盖活跃/结束 turn、替换会话和未知操作。这不是线上模型效果验收。字符和 UTF-8 字节的减少不能据此宣称为 token、费用或延迟节省。

Linux 环境安装本项目及测试依赖后，可直接运行：

```bash
PYTHONPATH=src:tests python tests/resume_input_samples.py /tmp/resume-fixture-unique /tmp/resume-capture
python -m pytest -q tests/test_target_root_runtime.py tests/test_target_root_runtime_context.py tests/test_target_delivery.py tests/test_target_contract_reset.py tests/test_target_admitted_prompt_context.py tests/test_target_runtime_conditions.py tests/test_harness_frozen_terminal_recovery.py tests/test_target_system_completion_pipeline.py
python -m pytest -q tests/test_resume_wrapper_inputs.py tests/test_runtime_conditions.py tests/test_external_mcp.py tests/test_bundle_resume_inputs.py tests/test_stage_resume_inputs.py
python -m pytest -q tests/test_resume_human_input.py tests/test_native_codex_guidance.py
```

本次 Windows 控制端的完整复现入口为 `.scratch/resume-slim/run_linux_tests.py` 和 `capture_comparison.py`；日志、JUnit XML、每份测试快照的源码散列保存在同目录。采样命令为 `python .scratch/resume-slim/capture_comparison.py baseline --name final-01`、`candidate --name final-03`、`compare`。脚本保存 exact 命令和基线测试覆盖文件清单。

实际执行使用隔离的 Linux `/tmp` 源码副本及每份快照独立的解释器，未运行或修改研究服务。特别验证了子进程清理 `PYTHONPATH` 后仍从本次快照加载源码：独立 venv 的 `.pth` 固定快照源码路径，依赖只读复用既有安装。较早仅设置 `PYTHONPATH` 的真实子进程结果不作为最终验收依据。

## 测试结果

各分组存在重叠，不把重复执行相加作为通过总数。

| 分组 / 本地日志名 | 结果 |
|---|---|
| Idea / Plan / Reasoning：`stages-07` | 138 passed |
| 共享包装、外部 MCP、Bundle 与阶段新增边界：`acceptance-wrapper-final` | 87 passed |
| Target 八个相关文件：`acceptance-target-shared` 中的 Target 部分 | 51 passed；同次运行唯一失败为 legacy external wrapper 字段兼容，修正后包含于上述 87 项 |
| 人类输入、生产 supervisor 和 native 协议：`human-03` | 30 passed |
| 最后一处 malformed legacy guidance 解析 guard：`acceptance-parser-final` | 1 passed |
| 通用调用同操作重入、拒绝篡改 caller 输入、运行条件 provider 边界：`acceptance-generic-final-02` | 29 passed |
| Bundle 广泛 adapter / owner / replacement / dispatch 回归：`bundle-fixtures-green` | 132/134 passed；剩余两个 fixture 已分别修正并重测通过；新增连续四次 dispatch 也通过 |

部分旧测试 fixture 没有提供基线已要求的 process-tree receipt，或缺少既有输出 schema 必填字段；本次补齐 fixture，没有放宽生产校验。另将旧清理/未知操作测试调整为基线现有语义：缺少终止证明时保持 pending，不再次发起操作。Target 科学结果字段的既有演进规则也未修改。相应基线复现及具体命令记录在 `stages/VALIDATION.md`、`bundle-implementation.md` 和测试日志。

广泛人类指导测试中另有 **12 项基线已存在的失败未修复**：9 项缺少 Target 历史工作区 authority，1 项缺少 Target 签名终止证明，2 项断言过时静态说明文字。分别在 `human-baseline-broad`（10 failed / 3 passed）和 `human-baseline`（2 failed）上用原始基线复现；完整 node ID 见 `human-validation.md`。不能将这些范围声明为全绿。

基线对照还找出并修复了一处本次引入的真实回归：不传显式业务输入的通用 `_invoke_root_operation` 调用，在新增指导后重复同一 operation，会因准备阶段不再重渲染指导而无法匹配输入。新通用调用现在将原始 caller prompt 存入既有 business-input 封存，重试沿原输入对账，修改 caller prompt 仍被拒绝；正式阶段已有显式业务输入的路径不变。已有无业务身份的旧 seal 不补造身份或改写。

最后的 29 项复验命令：

```text
python .scratch/resume-slim/run_linux_tests.py acceptance-generic-final-02 tests/test_resume_wrapper_inputs.py tests/test_human_guidance_providers.py::test_actual_empty_operation_retry_keeps_original_cut_after_late_submission tests/test_runtime_conditions_provider_prompts.py
```

`python -m compileall -q src tests`、`git diff --cached --check` 通过。测试临时文件曾填满 Linux `/tmp` 所在盘；仅清理了本任务五份已完成、无活跃进程使用的旧测试副本，保留本地证据后完成复验，未清理其他目录。

## 审阅与边界

非作者代理分别审阅了共享包装、Target、人类输入路径。已修正无序历史比较导致条件改变后反复重发、历史扫描读取无关作业、坏形状记录解析及人类指导范围关联等问题。新的注释经过独立检查，删除两行内部实现叙述。

Claude Code 完成了聚焦代码交叉审阅。采纳了无序条件历史、解析容错和未知人类输入证据的有效检查；其跨 operation 去重、Bundle compact hash 两个异议经源码与四次 dispatch 样本复核后已明确撤回。最早一次无限制读取审阅因工具额度中断，没有把它计作审阅通过。完整本地审阅记录见 `.scratch/resume-slim/cc-*-stream.jsonl` 与 `independent-review.md`。

仍有边界：现有本根 operation 历史读取量随历史增长，准备一次 turn 时可能读取多次；本次不宣称改善宿主读取开销。Bundle 同一准确 ref 出现多个科学版本时保守发送全文。历史通用私有调用若没有业务身份且其旧包装无法匹配，仍严格拒绝不一致输入，不自动重发；正式阶段使用已有业务身份对账。没有运行真实模型研究任务、compact 压力实测、完整仓库测试或部署验收。
