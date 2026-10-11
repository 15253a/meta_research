import { useCallback, useEffect, useState, type ReactNode } from "react";
import type { QuestCreationView } from "./api";
import { ProposalOutput } from "./ProposalOutput";
import "./first-question-progress.css";

export function currentDeepFetch(creation: QuestCreationView) {
  const value = creation.deepfetch;
  return creation.route === "deepfetch" && value?.basis_revision === creation.quest_draft.revision
    && value.basis_hash === creation.quest_draft.hash ? value : null;
}

export function currentProposalGeneration(creation: QuestCreationView) {
  const value = creation.proposal_generation;
  return value?.basis_revision === creation.quest_draft.revision
    && value.basis_hash === creation.quest_draft.hash ? value : null;
}

export function FirstQuestionProgress({ creation, pending, enabled, children }: {
  creation: QuestCreationView; pending: boolean; enabled: boolean; children: ReactNode;
}) {
  const generation = currentProposalGeneration(creation);
  const deepfetch = currentDeepFetch(creation);
  const outputRef = generation?.ref ?? deepfetch?.request_ref;
  const [observation, setObservation] = useState<{ ref?: string; phase: "understanding" | "drafting" | null }>({ phase: null });
  const phase = observation.ref === outputRef ? observation.phase : null;
  const observePhase = useCallback((next: "understanding" | "drafting" | null) => {
    setObservation({ ref: outputRef, phase: next });
  }, [outputRef]);
  const basis = creation.creation_basis;
  const currentBasis = basis?.freshness === "current";
  const ready = Boolean(creation.proposal?.status === "current" && generation?.status === "succeeded");
  const failed = generation?.status === "failed" || generation?.status === "capability_unavailable" || deepfetch?.status === "failed";
  const cancelled = deepfetch?.status === "cancelled" || creation.status === "cancelled";
  const stale = creation.status === "proposal_stale" || creation.proposal?.status === "stale" || basis?.freshness === "stale" || deepfetch?.freshness === "stale";
  const active = !failed && !cancelled && !ready && !stale && (pending || creation.status === "proposal_generating" || ["queued", "running"].includes(generation?.status ?? "") || ["queued", "running", "accepting"].includes(deepfetch?.status ?? ""));
  const hasMaterials = Boolean(creation.work_materials?.length || ("material_manifest" in creation.quest_draft.value && creation.quest_draft.value.material_manifest?.entries.length) || ("literature" in creation.quest_draft.value && creation.quest_draft.value.literature.accepted_material_bindings.length));
  const direct = creation.route === "direct";
  const searchDone = deepfetch?.status === "succeeded" && Boolean(deepfetch.literature_snapshot);
  const steps = [
    { label: "理解已有资料", done: !hasMaterials || Boolean(currentBasis) || phase === "drafting", skipped: !hasMaterials },
    { label: "补充检索", done: direct || searchDone, skipped: direct },
    { label: "准备研究依据", done: ready || Boolean(currentBasis && (direct || searchDone && basis?.kind === "literature_revised")) || direct && phase === "drafting", skipped: false },
    { label: "起草第一问", done: ready, skipped: false },
  ];
  const completed = steps.filter(step => step.done).length;
  const firstGap = steps.findIndex(step => !step.done);
  const starts = [generation?.started_at, deepfetch?.run?.attempt_started_at]
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const completions = [generation?.completed_at, deepfetch?.run?.attempt_completed_at]
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const started = starts.length ? Math.min(...starts) : null;
  const ended = !active && completions.length ? Math.max(...completions) : null;
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, started, generation?.ref]);
  const seconds = started === null ? null : Math.max(0, Math.floor((ended ?? now / 1000) - started));
  const elapsed = seconds === null ? null : `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  const title = stale ? "研究依据已变化，需要重新准备" : failed ? "本次准备未完成" : cancelled ? "本次准备已停止" : ready ? "第一个问题准备好了" : generation?.status === "succeeded" ? "问题草稿仍需补全" : pending && !generation && !deepfetch ? "正在保存目标并准备运行" : generation?.status === "queued" ? "等待开始准备第一问" : phase === "understanding" && !deepfetch?.run ? "正在理解已有资料" : deepfetch && !searchDone ? ({ queued: "正在准备检索", running: "正在检索与阅读资料", accepting: "正在保留可用文献" } as Record<string, string>)[deepfetch.status] ?? "正在准备第一问" : hasMaterials && !currentBasis && phase !== "drafting" ? "正在理解资料并准备第一问" : direct ? "正在起草第一个研究问题" : "正在整理研究依据与起草第一问";
  if (!pending && !generation && !deepfetch) return null;
  return <section className={`first-question-progress ${active ? "is-active" : ""} ${failed ? "is-failed" : ""}`}
    data-testid="first-question-progress" aria-label="第一问准备进度">
    <header><div><small>第一问 · 研究现场</small><h3 role="status">{title}</h3></div>
      <span>{elapsed ? `${active ? "已用时" : "本轮耗时"} ${elapsed}` : active ? "状态自动更新" : "本次运行记录"}</span></header>
    <div className="first-question-stage-meter" role="progressbar" aria-label="第一问流程位置" aria-valuemin={0} aria-valuemax={4}
      aria-valuenow={completed} aria-valuetext={failed || stale || cancelled ? title : ready ? "四个阶段已完成" : steps[firstGap]?.label}>
      {steps.map((step, index) => <div key={step.label} data-state={step.done ? step.skipped ? "skipped" : "done" : active && index === firstGap ? "current" : "pending"}
        aria-current={active && index === firstGap ? "step" : undefined}><i aria-hidden="true" /><b>{step.label}</b><small>{step.skipped ? "已跳过" : step.done ? "已就绪" : index === firstGap && active ? "进行中" : "待完成"}</small></div>)}
    </div>
    <p>{failed ? "本次运行已结束，目标、材料和已有草稿仍保留。可以重新生成第一问。" : stale ? "保留此前的活动记录；更新依据后重新生成第一问。" : ready ? "检查下面的问题草稿，确认前仍可修改。" : "下面持续显示实际运行活动；没有新事件时会等待下一次输出。"}</p>
    {children}
    {generation ? <ProposalOutput key={generation.ref} initializationId={creation.initialization_id} generationRef={generation.ref}
      status={generation.status} enabled={enabled} onPhase={observePhase} /> : null}
    {!generation && deepfetch && !deepfetch.run ? <ProposalOutput key={deepfetch.request_ref} initializationId={creation.initialization_id}
      generationRef={deepfetch.request_ref} status={deepfetch.status} enabled={enabled} onPhase={observePhase} preparation /> : null}
  </section>;
}
