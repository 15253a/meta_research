# 0025 · CP5.6 语义判据 5 判例显式命名验收——收尾 M4

- date: 2026-07-08
- commit: eb5e7d9 — test: CP5.6 §7.1 M4 语义判据 5 判例显式命名验收（收尾 M4）
- branch: main
- 检查点 / 步: CP5.6（属：步⑤ M4 真执行 + 真 log + import 物化）——**M4 收尾检查点**

## 决策
把 §7.1 M4 行的五判例写成**显式命名**的端到端验收（每判例独立起真 SQLite + 真子进程场景）。CP5.1–5.5 的
细粒度回归各自覆盖崩溃缝隙；本套是**归属验收**。两处微妙归因经内审实证：⑤ 拒因反事实（不 ingest 时同
close 真通过）、③ env 非空由 hit 断言本身钉住。验收诚实化：③「零重训」按可证面收窄口径（selector 级
只读复用；编排器级 reuse_only 路由=plan 特化 M6）；②「经 harness 出 factory evidence」以真执行指纹自证
（source 是注册期字面）；「report 带 provenance」的实质=join 可达性（M4 无 report 制品，report=M6）。

## 改动文件
- `meta-research/tests/test_m4_semantic_cases.py` — 新增：五判例 + 证据回溯全链 join（含 protocol_metric
  声明与 baseline 锚定）。

## Review
- **内审（Opus）**：APPROVE。实证核可两处归因（⑤ 反事实/③ env）；2 SHOULD 修（③ 零重训重言断言 → 收窄
  口径 + 命中即既有 metric_result id + 三表计数不变；② source 字面 → 补真执行指纹）；3 NIT 修（连接关闭/
  importmode 依赖注释/EOF）。勾兑判定：import 失败负例与 provenance 五件套在 test_import_worker，M4 步级
  验证按两文件联合勾兑成立。
- **外审（codex gpt-5.5/xhigh 第1轮）**：**APPROVE**。1 SHOULD（protocol_metric 纳入证据链 join——防残留
  声明行误过）+ 2 NIT（join 锚定 baseline/最近 run 锚定）均已加。评语：「五个判例整体不是空转……两文件联合
  勾兑作为 M4 步级验证也成立」。
- 未采纳意见：无。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  436 passed in 61.51s
  ```

### 步级验证（本检查点收尾步⑤ M4）——跑 §7.1 M4「验证方法」
- 命令：`pytest -q tests/test_m4_semantic_cases.py tests/test_import_worker.py tests/test_attack_advance.py tests/test_obs_parser.py`
- 关键输出：`47 passed in 15.81s`
- 逐条映射（§7.1 M4 行）：
  1. **语义判据 5 判例确定归属**：test_m4_semantic_cases 五测（①自建 ②import factory ③复用零重训
     ④失败入账不入树 ⑤log suspect 不成证据）→ 通过。
  2. **证据可回溯到一次真实 evaluation**：判例① 的 answer→evidence→metric_result→attempt→evaluation
     (success,factory)→protocol_metric→baseline 全链 join（值 0.93 端到端一致）→ 通过。
  3. **imported 经本系统 harness 出 factory evidence、report 带 provenance**：判例②（origin+manifest 互链
     + 真执行指纹）+ test_import_worker.test_materialize_full_chain（provenance 五件套 join：origin/
     source_uri/revision/manifest_hash/imported 事件；evaluation.source='factory'）→ 通过。
  4. **import 失败路径负例全拒**：test_import_worker 三测（scope 缺→不物化 / smoke 失败→不 target_ready
     [零 run] / factory eval 失败→不 pool_publish）+ judge FAIL settling → 通过。
- **结论：步⑤（M4）步级验证通过。** 全量 436 绿。**M0–M4 全部完成**；OPEN #1–#6 中 #1/#2（M1 裁）、
  #3（M3 无阻塞）、#5/#6（M4 落地）已闭，#4（paper-gap 谓词）留 M6 目标书定稿时。

## 遗留 / 回退
- 待办：**步⑥（M5）人类控制台 + query 只读应答器**（§7.1 M5 行）；步⑦（M6）长跑 + 验收剧本（含 M6 硬化
  清单：注册段合一事务、route plan 后特化、共享注册骨架、完整供应链 manifest、修复重评轮数、report 制品）。
- 回退：`git revert eb5e7d9`（纯测试文件）。
