# Bundle 全覆盖 replan 纠错

基于 `origin/v1-test@edc8f15d96a407e98a8ab9d0a004c5c2fdc24262`，保留现有 Bundle → Reasoning 研究流程。

原问题已在该基线复现：当前 Plan 的唯一 Target 正式接纳后，普通协调入口仍接纳 `replan_required`；后续三次报告构建均失败为 `bundle_report_invalid`（缺少 remaining work），没有反馈到原 Root。

批量规划和普通协调入口现在共用 `query_bundle_remaining_work`。它核验冻结 Plan、正式 TargetCommit 和 canonical closure，并复用正式报告的 `result_sets`；空 frontier、所有已提出 Target 完成及科学问题尚未解决，都不能替代这一判断。

全覆盖 replan 通过既有 provider correction 返回同一逻辑 Root，反馈为：

> 当前 Plan 的工作已全部完成。研究问题尚未解决，不代表本轮工作未完成。请整理结果、局限和后续建议，正常结束 Bundle，交给 Reasoning 综合。

报告阶段重验剩余工作，覆盖已接纳的旧决定及最后一个 Target 晚完成的情况。纠错前核验决定 receipt、当前 Root 与原 attempt/fence 的合法恢复链、native Session、原 provider operation 和已完成的原调度 unit，随后由现有 adapter 重验该操作的保留输出和退出证据。运行时刷新后的纠错仍引用原调度操作；已纠正的同一操作不会重复纠正。旧决定、TargetCommit 和原始结果均保留，不生成替代完成决定。

历史策略可能早于 Target 实际完成封口，因此全覆盖纠错后仍须接纳 Root 的新综合。已封口策略只允许补交空 candidates、仍为 complete 且 notes 非空的更新，不能新增 Target 或重新打开策略。待综合状态从既有纠错记录推导，后续格式错误、重启或运行时刷新不会把它抹掉；只有原拒绝之后正式接纳的新说明才解除等待。

部署回退需保留这一历史读取兼容性：一旦已封口策略接纳了新的说明 append，旧版验证器会拒绝重放该历史；不能通过删除已接纳记录来降级。

专项验收位于 `tests/test_bundle_replan_completion_feedback.py`、`tests/test_bundle_remaining_work.py` 和 `tests/test_bundle_sealed_synthesis.py`。测试通过真实 composition、Owner、BundleStageWorker 和 ReasoningStageWorker，使用确定性研究选择和合成 Target 结果，验证实际接纳并运行 Reasoning Run：

- 两个入口全覆盖误提 replan，经原 Root 反馈和完整收尾进入 Reasoning。
- 未全覆盖时正常 replan 进入 Reasoning，已规划但尚未启动的 Target 保持未启动。
- Bundle 工程求助恢复及 Target 结果修复保留原恢复路径，之后正常进入 Reasoning。
- 旧批量/协调决定、晚完成 Target、已封口策略、连续纠错、调度来源损坏和重启边界。

验证在隔离 Linux 源码副本和临时研究数据中进行，没有部署本次代码，也没有改变现有研究数据。Windows 本机因生产模块依赖 `fcntl` 无法收集该测试集。

Linux 复验命令：

```bash
PYTHONPATH=src:tests python -m pytest -q tests/test_bundle_replan_completion_feedback.py tests/test_bundle_remaining_work.py tests/test_bundle_sealed_synthesis.py
```

最终源码专项验收 **32 项通过**：21 条完整路径（397.69 秒），以及 11 项覆盖判断与封口合同测试（91.24 秒）。原始运行回执位于工作区外层 `bundle-replan-feedback-evidence/test-20261115-143525.json` 和 `test-20261115-143215.json`，均记录源码归档 SHA-256、`fixture_removed=true`、`production_modified=false`。

相关报告接纳、读取、运行时刷新、兼容性和原 Root 反馈回归另有 45 项通过；源码通过 `compileall` 和 `git diff --check`。

相关旧测试中两项在原始基线和候选上同样失败，保留原样：`test_wait_preserves_batch_opportunity_after_new_inbox_generation[hr-restart]`、`test_dispatch_observes_existing_frontier_when_target_commits_after_list_read`。另一个历史运行时兼容性测试需要未随仓库提供的 cf6 源码，因此按原条件跳过。
