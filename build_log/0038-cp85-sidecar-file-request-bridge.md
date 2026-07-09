# 0038 · CP8.5 sidecar→file_request 桥（阶段资源请求全局等待闭环）

- date: 2026-07-09
- commit: 650aace — feat: CP8.5 sidecar→file_request 桥——阶段资源请求全局等待闭环（M7）
- branch: main
- 检查点 / 步: CP8.5（属：步⑧ M7；正式可用性①——用户 2026-07-09 升格必做）

## 决策
真研究要向用户请求数据文件。把 StageProvider 对 resource_request.json sidecar 的 fail-loud 占位换成真桥，
接通 CP6.3 既有基建，形成**全等待环**：阶段产 sidecar → FileRequestService.create_checked（schema+幂等+
quota 闸）落 interaction_request(pending) → 抛 StageBlockedOnResources → run_cycles 干净停（**在途轮保持
游标、本阶段零提交**——sidecar 从 provider 调用内抛出 ⟹ persist-then-consume 产物未落盘 ⟹ 无中间态
泄漏）→ 后续 run 由 precheck 的 pending 阻断接管 → 用户 resolve（复制入 input/user_provided/ + 哈希 +
一次性迁终态）→ 续跑重做同一阶段。
失败分流：桥业务拒（FileRequestReject：sidecar 非法/quota 尽）→ 有界重试反馈（工人可修正或放弃）；
其余异常（DB 损坏等）fail loud 不吞。sidecar **有意优先于** stage 漂移/schema 校验（控制信号不得因产物
质量判定而丢）。判官（JudgeProvider）不受理 sidecar（评审材料已全量给出）→ 反馈重试。

## 改动文件
- `meta-research/orchestrator/interfaces.py` — 新增 StageBlockedOnResources（跨模块契约：provider 抛、
  advancer 捕、run.py 观测）。
- `meta-research/orchestrator/stage_provider.py` — sidecar 真桥分支 + JudgeProvider 拒 sidecar +
  file_request_bridge 注入参。
- `meta-research/orchestrator/advancer.py` — run_cycles 捕获→last_block_reason+干净 return。
- `meta-research/orchestrator/run.py` — 装配 FileRequestService + bridge 闭包（goal 最新版；在途轮
  cycle/question 挂单）。
- `meta-research/tests/test_stage_provider.py` — +4（桥接通/桥拒反馈/非业务异常 fail loud/判官拒 sidecar）。
- `meta-research/tests/test_run.py` — +1 全等待环 E2E（阻断→precheck 拦→真 resolve→同一在途轮续跑）。

## Review（codex-chatgpt gpt-5.5/xhigh）
- 内审（Opus 子代理）：APPROVE（零 BLOCKER/SHOULD）。实证：attack_stages 全部 except 面
  （_PlanReject/GateReject/_BundleReject/ManifestError）无一误吞 StageBlockedOnResources（bundle provider
  调用点在 try 外双重安全）；恢复无中间态泄漏（plan.json/reasoning.json 均在 provider 返回后才落盘）；
  E2E 走真 create_checked/resolve。采纳 NIT：except Exception 收窄为 FileRequestReject（其余 fail loud）。
- codex 第1轮：**APPROVE**（无 BLOCKER/SHOULD）。2 NIT 全采纳：①非业务异常 fail-loud 补回归钉牢
  （test_sidecar_bridge_nonbusiness_error_fails_loud：桥抛 OperationalError → 原样上抛、零重试）；
  ②sidecar 优先于 stage 漂移校验为有意设计 → 注释点明。
- 未采纳意见及理由：无（全部采纳）。

## 验证
- 命令：`python -m pytest tests/ -q` → **618 passed**（基线 535 无回归；CP8.4 后 613 + 5 新）。
- 关键输出：
  ```
  28 passed in 4.12s (test_stage_provider)
  618 passed in 96.77s (0:01:36)
  ```
- 全等待环 E2E：run1 阶段发 sidecar → 零轮完成 + 在途轮 status=created + 请求单 pending；run2 precheck
  拦（拒因含 #id）；真 resolve（条目 unavailable=合法部分解决）；run3 同一在途轮续跑完成。
- 结论：通过。

## 遗留 / 回退
- 待办：CP8.6 plan 全形态受理（exec/eval/import_defer + route 特化 + ImportWorker 装配）；CP8.7 运维
  操作面文档 + 步⑧步级验证收口。
- 回退：`git revert 650aace`。
