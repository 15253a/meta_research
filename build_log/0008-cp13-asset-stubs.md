# 0008 · CP1.3 资产层接口桩：Gate/StateStore/Compiler/Runner/goalbrief

- date: 2026-07-07
- commit: 7deb14a — feat: CP1.3 资产层接口桩
- branch: main
- 检查点 / 步: CP1.3（属：步① M0）

## 决策
orchestrator 六模块（桩与真实现共用 §6.10 签名；M0 边界内真做该真做的）：
- `schemas.py`：SchemaSet（$ref Registry）+ ARTIFACT_SCHEMA_MAP（Gate 校验对象清单；sidecar 故意不入）。
- `gate.py`：StubGate 三级校验——①schema ②引用完整性真做（answer/tree_ops/selection/plan/reuse_evidence/bundle target 一致性，含同批 local_key）③业务门禁放过留缝；ArtifactIndex；逐文件原子写（阶段级原子=M1 phase_commit）；sidecar 显式拒。
- `statestore.py`：InMemoryStateStore——七 op **事务语义**（快照回滚）、轮型护栏（create_root/add_children/goal_retarget）、树规模护栏（depth/children/open）、调度可见性（inconclusive 对 attack 限次）、local_key 同事务解析内化（§6.10）、close/release、applicability 全量绑定判据+每轮回看配额（校验过后才计数）。
- `compiler.py`：StubCompiler 确定性四区包（稳定排序遍历）+ manifest 溯源落盘（验收②证据链）+ normalized selected（不带 wildidea_extra）+ plan 切片；StubCtx/StubRecall。
- `runner.py`：CodexRunner（codex exec ephemeral、单 json 块信封解析、prompt/输出快照归档、fd 不泄漏、工程配置走 METARESEARCH_* env——模型/二进制是工程事实、不入 policy）。
- `goalbrief.py`：启动契约唯一实现（allow_nan=False），tests 反向 import。
- `interfaces.py` 增补：Cycle.next_question_id/next_intent（持久停机位）；close_question/release_question 入 StateStore Protocol（M0 归属注记：业务判据 M1 落 gate_close_question，状态迁移在 StateStore——两者共用写服务 §4.1.1）。

## 改动文件
- `meta-research/orchestrator/{schemas,gate,statestore,compiler,runner,goalbrief}.py` — 新增
- `meta-research/orchestrator/{__init__,interfaces}.py` — 修改（模块地图/Cycle 字段/Protocol 增补）
- `meta-research/tests/test_orchestrator.py` — 新增（32 用例：gate 两级/状态机链路/回滚/护栏/确定性/信封/启动契约）
- `meta-research/tests/fake_codex_{ok,bad,fail}.sh` — 新增（Runner 测试替身，不花 token）
- `meta-research/tests/test_schemas.py` — 修改（goal_brief 解析迁出注记）
- `implement_note.md` — 记账随提交

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内部（Opus 子代理）：With fixes——Critical：apply_tree_ops 返回类型背离冻结签名+local_key 解析归属与 §6.10 相悖（按规范收口：解析内化 StateStore、->None）；Important：Cycle 缺持久停机字段（setattr 移除）、dep 目标存在性、runner fd 泄漏、applicability 半实现、close/release 不在 Protocol、测试缺口；Minor 8 条。全部采纳。
- codex 第 1 轮：REQUEST_CHANGES——BLOCKER：①apply_tree_ops 非事务（半写不回滚）②缺轮型/树规模护栏（§4.2.4 拒因）③引用完整性覆盖不全；SHOULD：sidecar 不应走 Gate、渲染顺序依赖登记序、goalbrief NaN。全部核实采纳修复（详见提交信息）。
- codex 第 2 轮：REQUEST_CHANGES（达 2 轮上限）——BLOCKER：create_root 误放行 goal_amend 轮（goal 改版新 root 应走 goal_retarget）；SHOULD：max_spawn_from_goal_amend 未执行、add_children 子问题 source 应为 decompose（DDL 枚举核实）；NIT：test_schemas 注释。全部核实采纳修复（负例各补），按 §2.2 不再送第 3 轮
- 未采纳意见及理由：无。

## 验证
- 命令：`cd meta-research && /root/miniconda3/bin/python -m pytest tests/ -q`
- 关键输出：
  ```
  101 passed
  ```
- 覆盖：Gate 两级校验正反（含 sidecar 拒/target_key 一致性/悬空引用四类）；StateStore bootstrap→decompose→聚合解锁全链路、事务回滚、轮型/树规模/回看护栏、终态冻结；Compiler 双跑字节一致+manifest 溯源+切片；Runner 三替身（成功/坏信封/非零退出）；goalbrief 启动契约正反。
- 结论：通过（步①未收尾，步级验证待 CP1.4）。

## 遗留 / 回退
- 待办→CP1.4：driver.py + run_m0_acceptance.py（草稿已在 scratchpad cp14/，已同步收口后 API）；driver 需实现 sidecar 摘出路径（Gate 现拒 sidecar）。
- M0 简化注记（M1 换真时消化）：阶段级原子提交（phase_commit）、business 门禁 I1–I6、baseline dep 目标校验、amend_goal 重打分归 R3。
- 回退：`git revert 7deb14a`。
