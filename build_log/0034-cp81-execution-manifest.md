# 0034 · CP8.1 execution_manifest 契约 + harness manifest 适配层（步⑧开工）

- date: 2026-07-09
- commit: 8b4f59a — feat: CP8.1 execution_manifest 契约 + harness manifest 适配层（M7）
- branch: main
- 检查点 / 步: CP8.1（属：步⑧ M7 plan 契约缺口补齐 → 全流程 real-Codex attack）

## 决策
步⑦遗留「plan 制品契约二分」缺口——attack_stages 把 toy TARGET_SPEC（train_cmd 等）走私进
build_target.plan_ref，绕过冻结 plan.schema（抽象层）。方案（与用户 + codex-chatgpt 联合设计，见
ROADMAP 步⑧ + scratchpad plan_contract_design_out.md）：**不解冻任何冻结件**；plan 保持抽象；bundle
阶段 Codex 产「代码文件 + identity.md + execution_manifest.json」；manifest = 机器可验证执行契约，编排器
交叉核 plan 切片防旁路，harness 只机械执行。真 git worktree 隔离 = 后续硬化步（本步为 operational
canary，诚实边界写入 docstring/schema）。

CP8.1 = 纯新增契约层，**不改任何既有行为**（冻结 plan.schema / DDL / MIGRATION_SHA256 未动，578 测中
既有 535 无回归为证）。

## 改动文件
- `meta-research/schemas/execution_manifest.schema.json` — 新增：机器执行契约 schema（additive）。
  argv 数组禁 shell 字符串 / target_ref 回引 plan_slice_hash / {src}{ckpt} 占位符 / 路径负向前瞻拒 `.`·`..`
  段 / build-exec 须 smoke+train+eval+checkpoint、eval 只须 eval（allOf if/then，if 内加 target_kind
  required 防 vacuous 触发）/ code_files uniqueItems + 保留名（含 _staged.ok）。
- `meta-research/orchestrator/manifest.py` — 新增：契约唯一执法点（零 DB/零事务）。
  validate_manifest（schema）/ cross_check（目标三元组 + plan_slice_hash 内容寻址 + 协议绑定 + config
  服从计划，防「自立目标/换协议/改配置」）/ stage_bundle_files（净土物化=物化前整目录清空 + 逐文件
  .partial→原子改名 + 哨兵 sha256 记账）/ staged_hashes（哨兵解析→ManifestError + ledger 键值校验 +
  正向 is_file 核 + 反向对账 + symlink 目录审计）/ checkpoint_dest（checkpoint 相对路径解析进 run 目录）/
  resolve_command（{src}{ckpt} 替换 + _check_no_shell + 路径/env/超时围栏）/ run_manifest_command（委托
  既有 harness.run_staged）。
- `meta-research/policies/policy.yaml` — 修改：+execution 节（default_timeout_s=600/max_timeout_s=86400/
  path_allowlist=[]）。
- `meta-research/schemas/policy.schema.json` — 修改：+execution 节校验（required 同步 + 封闭属性）。
- `meta-research/orchestrator/schemas.py` — 修改：ARTIFACT_SCHEMA_MAP 注册 execution_manifest.json。
- `meta-research/tests/test_manifest.py` — 新增：39 测（schema 条件必填/交叉核防旁路/物化正反对账/
  禁 shell 参数化/checkpoint 围栏/symlink/哨兵损坏/ledger 内容/`.`段/dotfile 豁免/env overlay/真子进程 e2e）。
- `meta-research/tests/fixtures/{valid,invalid}/execution_manifest/*` — 新增：正例 1 + 负例 2（钉扎）。
- `meta-research/tests/test_schemas.py` `tests/test_orchestrator.py` — 修改：两清单锁登记 execution_manifest。
- `ROADMAP.md` — 记账：步⑧登记（方案 + CP8.1–8.6 切分 + CP8.2 硬约束）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE（无 BLOCKER）。采纳 SHOULD——净土物化清孤儿 + staged_hashes 反向
  对账；NIT-3 schema 报错噪声（allOf if 内加 target_kind required）。未采纳 NIT-4（eval 不禁冗余
  expected_outputs/repro_cmd_md——过约束徒增工人重试）。
- codex 第1轮：REQUEST_CHANGES——2 BLOCKER + 2 SHOULD + 1 NIT + 1 open question，全部采纳修复：
  ①[BLOCKER] 禁 shell 未执法（bash -c / env sh 绕过）→ _check_no_shell（argv[0] basename ∈ shell∪env 拒）；
  ②[BLOCKER] checkpoint 允许 ../x → schema 负向前瞻 + checkpoint_dest 解析入口；
  ③[SHOULD] symlink 目录漏判 → staged_hashes 审计 dirs 拒 symlink + 文件 is_file 核；
  ④[SHOULD] 哨兵损坏抛原生异常 → 包 ManifestError；
  ⑤[NIT] schema/runtime 分裂 → code_files uniqueItems + _staged.ok 保留名 + pattern 拒 .. 段；
  ⑥[open] env overlay？→ 确认 harness `{**os.environ,**env}` overlay + 加 test_env_overlay_preserves_inherited。
- codex 第2轮：REQUEST_CHANGES（**无新 BLOCKER**；确认第1轮 BLOCKER 全解）——2 SHOULD，均采纳修复：
  ①`.` 段（`./train.py`/`pkg/./util.py`/裸 `.`/`./identity.md` 别名）→ _check_rel_path + schema pattern
  拒 `.` 段（`\.{1,2}`），dotfile `.gitignore` 不误伤；②哨兵 ledger 内容未校验（hash 整数泄 TypeError、
  key `../x` 拿去拼路径）→ 逐 ledger 键过 _check_rel_path(allow_reserved) + hash 校 `^[0-9a-f]{64}$`。
  **第2轮为末轮**（CLAUDE.md §2.2：至多 2 轮）：两 SHOULD 确有理、均已自行修复，不再回送 codex。
- 未采纳意见及理由：内审 NIT-4（eval 冗余字段）——冗余无害，过约束会徒增真 Codex 重试。

## 验证
- 命令：`python -m pytest tests/test_manifest.py -q` → 39 passed；`python -m pytest tests/ -q` → 578 passed。
- 关键输出：
  ```
  39 passed in 0.21s
  578 passed in 84.12s (0:01:24)
  ```
  基线 535 + 43 新（含 test_manifest 39、fixtures 正1负2、清单锁 2 更新），既有全绿=无回归。
- 结论：通过（纯新增契约层，冻结件字节未动；CP8.2 将消费本契约驱动真执行）。

## 遗留 / 回退
- 待办：CP8.2 attack_stages 真契约化（idea/plan 消费冻结 schema + plan 走 gate_new_protocol/
  gate_claim_baseline + bundle manifest 驱动 + toy TARGET_SPEC 清理）。**硬约束**（ROADMAP CP8.2）：
  每目标 staging 物化目录唯一 / 命令序列按 target_kind 静态定 / manifest 记账对值(canon_hash)勿对字节。
- 回退：`git revert 8b4f59a`（纯新增，无既有行为依赖本模块——回退零影响 535 基线）。
