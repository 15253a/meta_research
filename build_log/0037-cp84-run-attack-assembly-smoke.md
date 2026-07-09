# 0037 · CP8.4 run.py attack 全装配 + 全链 E2E + 真 Codex 冒烟（含自纠环）

- date: 2026-07-09
- commit: 4f39559 — feat: CP8.4 run.py attack 全装配 + 全链 E2E + 真 Codex 冒烟自纠环（M7）
- branch: main
- 检查点 / 步: CP8.4（属：步⑧ M7 plan 契约缺口补齐 → 全流程 real-Codex attack）

## 决策
把步⑧前三个检查点的部件装进全系统入口，端到端验证「完整流程」，并以两次真 Codex 冒烟闭环发现→修复：
1. **run.py 装配 attack 全家**：_STAGES+=bundle；PoolGate（open_gate_read_conn 判据隔离连）+ close gate
   （parser_suspect 走 open_responder_read_conn 读观测——连接分工唯一坑点，注释写明）+ JudgeProvider
   （judge SKILL 单独加载）+ AttackStages（schemas+policy 注入）。attack: bool=True（False=诊断退化）。
2. **import_defer 显式拒**：schema 允许（targets 必空）但 CP8.6 未接线——不拒会静默走空 targets 合法终态
   **丢外部导入意图零痕迹**；转 _PlanReject 留 decision（payload 带 question_id 锚）。
3. **真 Codex 冒烟自纠环**（第一次冒烟发现）：真 Codex 全自动 6 轮零楔死，但对 toy 家族连续 3 轮产 exec
   target、1 轮 eval（池空时语义非法——无 legal baseline_ref 可指）→ 全部业务拒、轮次浪费不收敛。修复：
   a) plan SKILL 补「kind 前置条件」块（检索区无该家族 legal baseline ⇒ exec/eval 非法、必须 build）；
   b) compiler plan 锚区 _plan_reject_feedback：本问题最近一次 plan_rejected 拒因回流进下一轮 pack。

## 改动文件
- `meta-research/orchestrator/run.py` — attack 全家装配 + 连接分工注释 + main 文案。
- `meta-research/orchestrator/attack_stages.py` — import_defer 显式拒 + plan_rejected payload 带 question_id。
- `meta-research/orchestrator/compiler_sqlite.py` — plan 锚区拒因回流（确定性派生 decision 表）。
- `meta-research/prompts/skills/plan/SKILL.md` — kind 前置条件块（池空必 build + 先修上轮拒因）。
- `meta-research/tests/test_run.py` — +4：test_full_attack_flow_end_to_end（run.py 装配全系统：bootstrap→
  attack[idea→plan 真 gate→bundle manifest 真子进程→JudgeProvider 真落库×2]→真证据关问→terminate）/
  attack=False 退化 / 拒因回流进 pack；旧 CLI 测试适配。
- `meta-research/tests/test_attack_advance.py` — +1 import_defer 拒留痕。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE（零 BLOCKER/SHOULD，1 可选 NIT[只读连接生命周期未显式收口——进程退出即
  释放，无害]）。实证：gate 判据 SQL 全不落 authorizer 禁集；obs 连接分离正确（生产比测试更严：只读）；
  E2E 断言面足证全链（TRAIN_OK 对 suspect 策略全不命中 → 关问真过 gate 含 suspect 谓词）；import_defer
  拒位置精确（schema allOf 保证 defer⇒空 targets，若不拒恰落静默合法终态路径）。
- codex 第1轮：**APPROVE**（连接拓扑无 BLOCKER：gate 判据/观测/写三路连接分工确认正确）。附 2 SHOULD + 3 NIT，全部采纳：①[SHOULD] 拒因永久渲染会陈旧（CP8.6 后把合法 exec/eval 引导走偏）→ 已有更晚成功 plan（同问题更晚 cycle 落过 build_target）即静默 + 标题改「最近一次」+ 回归 test_plan_reject_feedback_suppressed_after_success；②[SHOULD] import_defer 拒只留 reason 字符串（decision 是恢复/检索面）→ defer 对象整体嵌入拒因 payload；③[NIT] attack 参数 bool 破注入契约 → True|False|AttackStages 三态；④[NIT] main 文案固定 CP8.6 在 attack=False 路径误导 → 改不预设来源、由异常文本自述；⑤[NIT] ROADMAP 断行「+」→ 改「；同日追加」。
- 第2轮：无需（第1轮即 APPROVE；采纳项全部自验 613 绿后提交）。
- 未采纳意见及理由：无（全部采纳）。

## 验证
- 命令：`python -m pytest tests/ -q` → **613 passed**（基线 535 无回归）。
- **步级验证①（全链 E2E）**：test_full_attack_flow_end_to_end 通过——run.py 装配的全系统（真组件+
  脚本 runner）跑通 bootstrap→attack（协议注册/占坑/manifest 真子进程 smoke·train·eval/双评审真落库/
  注册入池）→真证据关问→terminate。
- **步级验证②（真 Codex CLI 冒烟）**：
  - 第一次（修正前）：`python -m orchestrator.run --system-root . --work-root <scratch>/smoke_m7
    --max-cycles 6` → exit 0，6 轮全自动零楔死（bootstrap→decompose→attack×4），但 exec/eval target
    全被业务拒（发现→修复见「决策」3）。
  - 第二次（修正后）：`--max-cycles 6` → exit 0，输出
    `[run] dual_mode=A 推进 3 轮：['c1','c2','c3']；停因=score_floor`。**c3 attack 轮完整走通 build 链**：
    ```
    targets: build complete；baseline `baseline.synthetic_2d_double_gaussian.single_hidden_mlp` legal
    protocol: synthetic_2d_double_gaussian_binary_classification @1（真 Codex 自声明 2 metric_def）
    run: build success；evaluation: factory success；metric_result: acc=0.9949 / seed_pass=1.0（真子进程）
    judge: bundle_code_review pass + bundle_result_review pass（JudgeProvider 真落库）
    plan_rejected: 0（SKILL 修正后一次选对 build）；停因=τ score_floor（安全网真实触发）
    ```
    reasoning 侧真 Codex 保守未关问（inconclusive 收尾合法）；关问链由 E2E 以脚本 provider 全链证。
- 结论：通过（**真 Codex 全自动完整 attack build 链端到端跑通**——步⑧核心目标达成；正式可用收口
  = CP8.5–8.7）。

## 遗留 / 回退
- 待办（用户 2026-07-09 追加「正式直接可用」→ 升格必做）：CP8.5 sidecar→file_request 桥；CP8.6 plan
  全形态受理（exec/eval/import_defer + route 特化 + ImportWorker 装配）；CP8.7 运维操作面文档 + 步级
  验证收口。
- 回退：`git revert <HASH>`。
