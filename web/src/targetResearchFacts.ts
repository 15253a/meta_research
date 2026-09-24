import type { BundleTargetCommitProjection, BundleTargetProjection, HumanRequestItem } from "./api";

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const records = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const ref = (value: unknown): string | null => typeof value === "string" && value.length > 0 ? value : null;
const refs = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

export function targetResearchFacts(target: BundleTargetProjection, candidate?: BundleTargetCommitProjection, requests: readonly HumanRequestItem[] = []) {
  // A historical or mismatched Commit must never supply facts for the current Run.
  const commit = candidate?.target_ref === target.target_ref && candidate.target_spec_hash === target.spec_hash
    && (!target.target_run_ref || candidate.target_run_ref === target.target_run_ref) ? candidate : undefined;
  const closure = record(commit?.closure);
  const measurement = record(closure.root_measurement);
  const inventory = records(measurement.formal_entities);
  const manifest = record(closure.target_root_manifest);
  const notes = records(closure.research_notes);
  const inputs = record(measurement.variant_input_binding);
  const runRefs = [...new Set(inventory.length ? inventory.map(item => ref(item.variant_run_ref)).filter((item): item is string => Boolean(item)) : refs([measurement.variant_run_ref]))];
  const attemptRefs = [...new Set(inventory.length ? inventory.map(item => ref(item.evaluation_attempt_ref)).filter((item): item is string => Boolean(item)) : refs([measurement.evaluation_attempt_ref]))];
  const failedAttempts = new Set(inventory.filter(item => item.evaluation_status === "failed").map(item => item.evaluation_attempt_ref)).size
    || (measurement.evaluation_status === "failed" ? 1 : 0);
  const failedRuns = new Set(inventory.filter(item => item.run_status === "failed").map(item => item.variant_run_ref)).size
    || (measurement.execution_status === "failed" ? 1 : 0);
  const pendingEvaluation = measurement.evaluation_status === "pending" || inventory.some(item => item.variant_run_ref && !item.evaluation_attempt_ref);
  const evaluation = failedAttempts ? `${failedAttempts} 项评价执行失败 · 需复盘或重试${pendingEvaluation ? "；另有评价待办" : ""}`
    : pendingEvaluation ? attemptRefs.length ? "部分已评价 · 仍有待评价执行" : "执行已记录 · 评价待办"
    : measurement.evaluation_status === "completed" || attemptRefs.length ? "已形成评价记录" : "尚无可确认的评价记录";
  const related = requests.filter(request => request.target_assertion?.target_ref === target.target_ref
    || request.direct_waiters?.some(waiter => waiter.target_assertion?.target_ref === target.target_ref)
    || request.open_effect?.operation_binding.task_ref === target.target_ref
    || Boolean(target.target_run_ref && request.open_effect?.operation_binding.task_ref === target.target_run_ref));
  const openRequests = related.filter(request => request.status === "open" || request.status === "unsatisfied");
  const answered = related.filter(request => request.responses?.length);
  const blockerCode = ref(target.blocker?.code);
  const coordination = blockerCode === "target_coordination_required" || blockerCode === "target_semantic_change_required";
  const technicalFailure = !commit && (["failed", "fenced"].includes(target.status) || Boolean(blockerCode && !coordination && !blockerCode.startsWith("target_high_risk_authorization")));
  const dispositions: Record<string, string> = { positive: "正面结果", negative: "负面结果", uncertain: "结果不确定", rejected: "方案已否定", inconclusive: "尚无确定结论", insufficient_evidence: "证据不足" };
  const result = commit ? dispositions[commit.result_disposition] ?? commit.result_disposition : "研究结果尚未接纳";
  const summary = openRequests.length ? "有待人类答复的求助" : technicalFailure ? "技术执行受阻" : coordination ? "等待研究调整" : commit ? `研究记录已接纳 · ${result}` : target.status === "running" ? "Target 正在开展工作" : "等待研究工作接续";
  const resultAsset = record(measurement.result_asset);
  const artifactRefs = [...new Set([...refs(measurement.checkpoint_refs), ...records(manifest.entries).map(item => ref(record(item.binding).version_ref)).filter((item): item is string => Boolean(item)), ...refs([resultAsset.version_ref])])];
  const readableAssets = [...new Map([
    ...(ref(resultAsset.version_ref) ? [{ versionRef: String(resultAsset.version_ref), label: "读取结果原文" }] : []),
    ...notes.filter(note => ref(note.version_ref)).map(note => ({ versionRef: String(note.version_ref), label: "读取交接说明" })),
    ...records(manifest.entries).filter(item => ref(record(item.binding).version_ref)).map(item => ({ versionRef: String(record(item.binding).version_ref), label: "读取产物" })),
  ].map(item => [item.versionRef, item])).values()];
  return {
    summary, result, technicalFailure, pendingEvaluation, failedRuns, failedAttempts, runRefs, attemptRefs, artifactRefs, notes, inputs, readableAssets,
    facts: [
      { label: "输入", text: refs(inputs.input_refs).length ? `${refs(inputs.input_refs).length} 项精确输入已绑定` : `${target.dependency_refs.length} 项上游依赖 · 资产见执行记录` },
      { label: "实际执行", text: runRefs.length ? `${runRefs.length} 项方法执行已记录${failedRuns ? ` · ${failedRuns} 项执行失败` : ""}` : target.target_run_ref ? "Target 工作已建立 · 方法执行待记录" : "尚未建立执行" },
      { label: "产物", text: artifactRefs.length ? `${artifactRefs.length} 项已记录资产引用` : manifest.manifest_ref ? "已有精确产物清单 · 可按引用读取" : "尚无可确认的产物清单" },
      { label: "评价", text: evaluation },
      { label: "接纳", text: commit ? `研究记录已接纳 · ${result}` : "尚未接纳研究结果" },
      { label: "交接", text: notes.length ? `${notes.length} 项版本化交接说明` : commit ? "结果与精确来源可供后续读取" : "工作记录中保留尝试与未决事项" },
      { label: "求助", text: openRequests.length ? `${openRequests.length} 项待人类答复 · 独立工作可继续` : answered.length ? `${answered.length} 项已有人类回复 · 见协作记录` : "当前未显示关联的待答复请求" },
    ],
    sources: { target_ref: target.target_ref, target_run_ref: target.target_run_ref, target_spec_hash: target.spec_hash, dependency_refs: target.dependency_refs,
      input_bindings: inputs, variant_run_refs: runRefs, evaluation_attempt_refs: attemptRefs, result_asset: resultAsset, artifact_refs: artifactRefs,
      manifest, research_notes: notes, commit_ref: commit?.commit_ref, closure_hash: commit?.closure_hash,
      human_requests: related.map(request => ({ request_ref: request.request_ref, status: request.status, obligation: request.obligation, responses: request.responses })) },
  };
}
