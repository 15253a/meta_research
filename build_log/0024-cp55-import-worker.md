# 0024 · CP5.5 外部 import 物化 ImportWorker（OPEN #6 落地 + 失败路径全拒）

- date: 2026-07-08
- commit: d9de442 — feat: CP5.5 外部 import 物化 ImportWorker——OPEN #6 落地 + 失败路径全拒（M4）
- branch: main
- 检查点 / 步: CP5.5（属：步⑤ M4 真执行 + 真 log + import 物化）

## 决策
M1c 的 deferred 三写入（占位 baseline + pending dep）到此接上真物化（§3.6.3）。两项 OPEN 裁决全部闭环
（#5 于 CP5.3、#6 于本检查点）。

- **OPEN #6 落地**：worker cycle = route 终身 NULL（七研究形态不扩）+ 开轮同事务 DECISION(orchestrator,
  import_worker_cycle) 权威标记 + 收尾 mark done/failed 不产 cycle_report + 研究驱动循环**无条件**标记探测
  （标记在而 worker 未装配 → fail loud，绝不被 _setup_cycle 误派研究 route——内审实证三种静默损坏子情形后
  收紧）。配套：last_done_cycle 滤 route IS NOT NULL（worker 轮 done 后无 selection，混入会污染研究交接）。
- **物化链**：scope 消费点（allow_eval+allow_publish_pool；license 表 authorizer 对 gate 拒读 → scope 核
  在 worker 普通连接，§3.6.3 的钦定位置）→ clone（路径卫生 + 幂等）→ 供应链 manifest（条目+revision 规范
  哈希；完整闭包=M6 硬化）→ 真 smoke → 适配评审 → import run + checkpoint(origin='external_import' 携
  source_uri/revision/manifest_hash，DDL CHECK 焊) → 出厂评估（**source 仍 'factory'**——外部性只在
  checkpoint provenance，不新增 metric source，§3.6.3 证据归属）→ register →占位 planned→legal → imported
  事件（双 hash）→ resolve_deps 机械 satisfied → 原问题回可调度。
- **失败路径全拒（§7.1 M4 负例）**：scope 缺→不物化不开工（占位 planned）；smoke 败→不 target_ready（零
  run）；factory eval 败→不 pool_publish；**judge FAIL→failed(review_failed) 收尾**——均 settling
  （materialize_failed）+ 占位连坐 build_failed + worker failed；dep 保持 pending、问题继续不可调度。
- **崩溃缝隙**（本检查点两层评审主战场）：worker 收尾+resolve_deps 同一 atomic；judge FAIL 不得闯 gate
  （replay 复用 fail 裁决=确定性死循环）；终败/终态短路幂等补记 settling+pc；clone 路径卫生。

## 改动文件
- `meta-research/orchestrator/import_worker.py` — 新增（~330 行）。
- `meta-research/orchestrator/advancer.py` — 修改：无条件 worker 标记探测 + 未装配 fail-loud。
- `meta-research/orchestrator/statestore_sqlite.py` — 修改：last_done_cycle 滤 route IS NOT NULL。
- `meta-research/orchestrator/attack_stages.py` — 修改：judge_once 抽模块级；judge FAIL 收尾 lockstep 同修；
  _run_and_register 双向 lockstep 注释。
- `meta-research/orchestrator/gate_exec.py` — 修改：import kind 接入（start 连动/finish 连坐）。
- `meta-research/orchestrator/gate_pool.py` — 修改：register_evaluation 放行 import；register_baseline
  expect_kind 按 provenance。
- `meta-research/orchestrator/__init__.py` — 修改：模块地图。
- `meta-research/tests/test_import_worker.py` — 新增：9 测。`test_attack_advance.py` +1（lockstep）。
  `test_gate_exec.py` import start 语义测替换 defer 测。

## Review（两层评审共 2 BLOCKER + 6 SHOULD + 2 NIT，全采纳）
- **内审（Opus）**：REQUEST_CHANGES → 全修。**BLOCKER**（实证）：崩在「worker done ↔ resolve_deps」两提交
  之间 → baseline legal+imported 已记但 dep 永 pending、问题永不可调度且**无自愈**（done 不再 resume、
  settled 跳过不再扫）→ mark_cycle_done+resolve_deps 合一 state.atomic()。SHOULD×4：① 未装配 worker 时
  在途 worker 轮被 _setup_cycle 误派研究 route（实证三子情形）→ 无条件探测+fail loud ② finish-failed↔
  record 缝隙 → 终败短路幂等补记（自愈收敛）③ 裸 UPDATE 漏 finished_at → 并入 ① ④ 与 attack 注册阶梯
  同构拷贝 → 双向 lockstep 注释（CP5.4 修法已逐条镜像核可；共享骨架=M6）。
- **外审（codex gpt-5.5/xhigh）**：
  - 第1轮 REQUEST_CHANGES → 全修。**BLOCKER**：judge FAIL 直接闯 gate 被拒 → 重启 judge_once 复用同 fail
    裁决 → 确定性重试死循环、worker 永悬置（「全拒」退化）→ review_passed 核 + failed(review_failed) 收尾
    +settling+pc；**attack 侧同洞 lockstep 同修**（result review FAIL 保留 run+checkpoint 不注册 =
    §4.2.5「第(ii)段不发生」口径）。SHOULD×2：终态短路补 pc；clone 路径卫生（父目录/绝对/越界拒 + 干净
    settling）。NIT：worker 标记查询加 actor='orchestrator'（×4 处）。
  - 第2轮 **APPROVE**（零 BLOCKER/SHOULD；symlink containment 注记为 fetch-bytes 契约外、后续归档展开时加）。
- 未采纳意见：无。

## 验证
- 命令：`pytest tests/ -q`
- 关键输出：
  ```
  431 passed in 60.53s
  ```
- 10 新测：全链 provenance 五件套 join（origin/source_uri/revision/manifest_hash + imported 事件）/scope 拒/
  smoke 拒/eval 拒/judge FAIL 双侧 settling/worker 轮识别续跑/resolve_deps 缝隙自愈/未装配 fail-loud/
  failed-record 缝隙自愈。
- 结论：通过。（CP5.5 未收尾步⑤；M4 步级验证在 CP5.6。）

## 遗留 / 回退
- 待办：CP5.6 语义判据 5 判例 + §7.1 M4 步级验证收尾。M6 硬化：完整供应链闭包 manifest、attack/import
  注册阶梯共享骨架、修复重评轮数、symlink containment（归档展开时）、重试策略（materialize_failed 后
  按 selection_key 取下一候选）。
- 回退：`git revert d9de442`（新模块+gate 接入+advancer 探测；不影响 attack/reasoning-only 路径基线绿）。
