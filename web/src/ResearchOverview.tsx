import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import type { PublicSnapshot, WritingReportView } from "./api";
import "./research-overview.css";
import { SpectrumStages, spectrumStage } from "./Spectrum";

export type OverviewStage = "idea" | "plan" | "bundle" | "reasoning";
export type OverviewSource = {
  commit_ref?: string | null;
  request_ref?: string | null;
  run_ref?: string | null;
  outcome_ref?: string | null;
  content_ref?: string | null;
  content_hash?: string | null;
  [key: string]: unknown;
};
export type OverviewFinding = {
  text: string;
  text_field?: string;
  disposition: string;
  quest_ref: string;
  question_ref: string;
  cycle_ref: string;
  cycle_ordinal: number | null;
  stage: "reasoning";
  epoch: number;
  source: OverviewSource;
};
export type OverviewStageArtifact = {
  stage: OverviewStage;
  status: "accepted" | "report_accepted" | "skipped" | "exhausted" | "unavailable";
  epoch: number;
  source: OverviewSource;
  content: Record<string, unknown> | null;
  reason: { code: string } | null;
};
export type OverviewCycle = {
  cycle_ref: string;
  question_ref: string;
  ordinal: number | null;
  stages: Partial<Record<OverviewStage, OverviewStageArtifact[]>>;
};
export type ResearchOverviewData = {
  schema_ref: "meta-research/research-overview/v1";
  projection_hash?: string;
  status: "ready" | "limited" | "idle";
  quest_ref: string | null;
  question_ref: string | null;
  cycle_ref: string | null;
  cycle_ordinal: number | null;
  foreground: PublicSnapshot["research_control"]["foreground"];
  findings: { quest: OverviewFinding[]; question: OverviewFinding[]; cycle: OverviewFinding[] };
  cycles: OverviewCycle[];
  reason: { code: string } | null;
};

export function overviewQuestRef(snapshot: PublicSnapshot | null): string | null {
  const current = snapshot?.research_space.current_quest;
  if (current?.status === "ready" && current.quest_ref) return current.quest_ref;
  return snapshot?.research_control.foreground?.quest_ref ?? snapshot?.research_control.quest_ref ?? null;
}

/** Read-only projection. Request identity is checked before exposing a result. */
export function useResearchOverview(snapshot: PublicSnapshot | null, active = true, refreshOnRevision = true) {
  const questRef = overviewQuestRef(snapshot);
  const cycleRef = snapshot?.research_control.foreground?.cycle_ref ?? null;
  const questionRef = snapshot?.research_control.foreground?.question_ref ?? null;
  // Cycle numbering is immutable for this identity; the collapsed workspace
  // reads it once. Open result/history views keep their existing live refresh.
  const revision = refreshOnRevision ? snapshot?.revision : undefined;
  const identity = JSON.stringify([questRef, cycleRef, questionRef]);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ identity: string; data: ResearchOverviewData | null; error: string | null; loading: boolean }>({ identity: "", data: null, error: null, loading: false });
  const retry = useCallback(() => setAttempt(value => value + 1), []);

  useEffect(() => {
    if (!active || !questRef) return;
    const controller = new AbortController();
    setResult(previous => ({ identity, data: previous.identity === identity ? previous.data : null, error: null, loading: true }));
    void (async () => {
      try {
        const response = await fetch(`/api/v1/research-overview?${new URLSearchParams({ quest_ref: questRef })}`, {
          credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
        });
        if (!response.ok) throw new Error(`research_overview_unavailable:${response.status}`);
        const data = await response.json() as ResearchOverviewData;
        if (data.schema_ref !== "meta-research/research-overview/v1" || !Array.isArray(data.cycles)
          || !data.findings || !Array.isArray(data.findings.quest) || !Array.isArray(data.findings.question) || !Array.isArray(data.findings.cycle)) {
          throw new Error("research_overview_invalid_response");
        }
        if (data.quest_ref !== questRef || (cycleRef && data.cycle_ref !== cycleRef) || (questionRef && data.question_ref !== questionRef)) {
          throw new Error("research_overview_scope_changed");
        }
        if (!controller.signal.aborted) setResult({ identity, data, error: null, loading: false });
      } catch (caught) {
        if (!controller.signal.aborted) setResult(previous => ({ identity, data: previous.identity === identity ? previous.data : null, error: caught instanceof Error ? caught.message : "research_overview_unavailable", loading: false }));
      }
    })();
    return () => controller.abort();
  }, [active, questRef, cycleRef, questionRef, identity, revision, attempt]);

  const current = active && questRef && result.identity === identity;
  return { data: current ? result.data : null, error: current ? result.error : null, loading: current ? result.loading : false, retry };
}

const stages: OverviewStage[] = ["idea", "plan", "bundle", "reasoning"];
const stageNames: Record<OverviewStage, string> = { idea: "研究思路", plan: "验证计划", bundle: "实验与证据", reasoning: "研究判断" };
const stageTechnicalNames: Record<OverviewStage, string> = { idea: "Idea", plan: "Plan", bundle: "Bundle", reasoning: "Reasoning" };
const dispositionNames: Record<string, string> = { affirmed: "已有证据支持", denied: "已有反证", uncertain: "尚无确定结论", insufficient_evidence: "证据不足", supported: "已有证据支持", refuted: "已有反证", inconclusive: "尚无确定结论", answered: "已形成回答", unresolved: "尚待解决", proceed: "可继续", complete: "已完成", completed: "已完成", blocked: "受阻", exhausted: "未形成可用方案", insufficient: "证据不足", partial: "部分完成" };
const acceptedStatusNames: Record<OverviewStageArtifact["status"], string> = { accepted: "结果已接纳", report_accepted: "报告已接纳", skipped: "本轮已跳过", exhausted: "未形成可用方案", unavailable: "结果暂不可用" };

export function cycleOrdinalLabel(ordinal: number | null | undefined): string {
  return typeof ordinal === "number" && Number.isInteger(ordinal) && ordinal > 0 ? `第 ${ordinal} 轮` : "轮次待确认";
}

function scopedOverview(snapshot: PublicSnapshot, overview?: ResearchOverviewData | null) {
  const foreground = snapshot.research_control.foreground;
  return overview?.quest_ref === overviewQuestRef(snapshot)
    && (!foreground || (overview?.cycle_ref === foreground.cycle_ref && overview?.question_ref === foreground.question_ref)) ? overview : null;
}

export function acceptedWritingRuns(snapshot: PublicSnapshot, questRef: string | null): WritingReportView[] {
  const unique = new Map<string, WritingReportView>();
  for (const item of snapshot.writing.runs) {
    if (!questRef || item.snapshot.quest_ref !== questRef || !item.deliverable.version_ref) continue;
    if (item.deliverable.acceptance_status !== "accepted" && item.deliverable.status !== "accepted") continue;
    unique.set(item.run?.run_ref ?? item.intent_id, item);
  }
  return [...unique.values()];
}

function latestFinding(items: OverviewFinding[]) {
  // The authenticated projection returns newest-first, retaining its source order.
  return items.find(item => typeof item.text === "string" && item.text.trim());
}

export function cycleRuntime(snapshot: PublicSnapshot): { label: string; running: boolean } {
  const foreground = snapshot.research_control.foreground;
  if (!foreground) return { label: "尚未进入研究轮次", running: false };
  if (snapshot.research_control.status !== "ready") return { label: "当前运行状态暂不可确认", running: false };
  const stageKey = foreground.stage.toLowerCase() as OverviewStage;
  const stage = stageNames[stageKey] ?? "当前阶段";
  if (["completed", "closed"].includes(foreground.status)) return { label: "本轮已结束", running: false };
  if (["paused", "suspended"].includes(foreground.status) || foreground.grant_status === "suspended") return { label: `${stage} · 已暂停`, running: false };
  if (["blocked", "waiting", "revoked", "abandoned"].includes(foreground.status) || ["revoked", "abandoned"].includes(foreground.grant_status)) return { label: `${stage} · 等待继续条件`, running: false };
  const projection = ({ idea: snapshot.idea_stage, plan: snapshot.plan_stage, bundle: snapshot.bundle_stage, reasoning: snapshot.reasoning_stage })[stageKey];
  const request = projection?.stage_run_request;
  const binding = request?.accepted_question_binding;
  // A previous stage or epoch can remain active in managed_runs. It is not evidence
  // that the currently displayed stage is executing.
  const exact = projection && (request?.cycle_ref === foreground.cycle_ref || projection.eligibility.cycle_ref === foreground.cycle_ref)
    && (!projection.eligibility.cycle_ref || projection.eligibility.cycle_ref === foreground.cycle_ref)
    && (!projection.eligibility.question_ref || projection.eligibility.question_ref === foreground.question_ref)
    && (!request?.cycle_ref || request.cycle_ref === foreground.cycle_ref)
    && (request?.epoch == null || request.epoch === foreground.epoch)
    && (!binding?.question_ref || binding.question_ref === foreground.question_ref)
    && (!binding?.quest_ref || binding.quest_ref === foreground.quest_ref);
  const run = exact ? projection.run : null;
  const current = run?.run_ref ? snapshot.research_control.managed_runs.filter(item => item.run_ref === run.run_ref && item.quest_ref === foreground.quest_ref && item.cycle_ref === foreground.cycle_ref && (item.epoch == null || item.epoch === foreground.epoch)) : [];
  const statuses = [run?.status, ...current.map(item => item.status)];
  if (statuses.some(status => status === "paused" || status === "suspended")) return { label: `${stage} · 已暂停`, running: false };
  if (run?.blocker || statuses.some(status => status === "blocked" || status === "waiting" || status?.startsWith("waiting_"))) return { label: `${stage} · 等待继续条件`, running: false };
  if (statuses.some(status => status === "active" || status === "running")) return { label: `${stage} · 执行中`, running: true };
  if (statuses.some(status => status === "completed" || status === "succeeded")) return { label: `${stage} · 本次输出已结束`, running: false };
  return { label: `${stage} · 暂无新的执行状态`, running: false };
}

function writingProgress(snapshot: PublicSnapshot, questRef: string | null): string | null {
  const runs = snapshot.writing.runs.filter(item => questRef && item.snapshot.quest_ref === questRef);
  const active = runs.filter(item => item.status === "running" && item.run?.status === "active").length;
  if (active) return `${active} 项写作正在进行`;
  const blocked = runs.filter(item => item.status === "blocked" || item.run?.status === "blocked").length;
  if (blocked) return `${blocked} 项写作等待继续条件`;
  const paused = runs.filter(item => item.status === "paused" || item.run?.status === "paused").length;
  return paused ? `${paused} 项写作已暂停` : null;
}

type OverviewProps = {
  snapshot: PublicSnapshot;
  overview?: ResearchOverviewData | null;
  error?: string | null;
  onRetry?: () => void;
  onOpenWriting: () => void;
  connected?: boolean;
};

export function ResearchOverview({ snapshot, overview, error, onRetry, onOpenWriting, connected = true }: OverviewProps) {
  const data = scopedOverview(snapshot, overview);
  const scope = JSON.stringify([overviewQuestRef(snapshot), snapshot.research_control.foreground?.cycle_ref, snapshot.research_control.foreground?.question_ref]);
  const [detailSelection, setDetailSelection] = useState<{ scope: string; kind: "quest" | "question" | "cycle" } | null>(null);
  const detail = detailSelection?.scope === scope ? detailSelection.kind : null;
  const setDetail = (kind: "quest" | "question" | "cycle" | null) => setDetailSelection(kind ? { scope, kind } : null);
  useEffect(() => setDetailSelection(null), [scope]);
  const findings = data?.findings ?? { quest: [], question: [], cycle: [] };
  const quest = latestFinding(findings.quest);
  const question = latestFinding(findings.question);
  const cycle = latestFinding(findings.cycle);
  const latestMilestone = data?.cycles.flatMap(item => stages.flatMap(stage =>
    (item.stages[stage] ?? []).filter(result => ["accepted", "report_accepted"].includes(result.status))
      .map(result => ({ cycle: item, stage: result.stage })),
  )).at(-1);
  const milestoneCopy = latestMilestone
    ? `${cycleOrdinalLabel(latestMilestone.cycle.ordinal)}已形成${stageNames[latestMilestone.stage]}结果`
    : null;
  const writing = acceptedWritingRuns(snapshot, overviewQuestRef(snapshot));
  const writingState = writingProgress(snapshot, overviewQuestRef(snapshot));
  const runtime = cycleRuntime(snapshot);
  const runtimeLabel = connected ? runtime.label : `上次确认：${runtime.label}`;
  const labels = { quest: "研究进展", question: "本题发现", cycle: "本轮进展" };
  const empty = !overviewQuestRef(snapshot) ? "尚未建立研究" : data?.status === "limited" ? "发现暂不可确认" : data ? "尚未形成研究发现" : error ? "发现暂不可用" : "正在读取研究发现";
  const selectedFindings = detail ? findings[detail] : [];
  return <>
    <section className="research-overview" aria-label="研究进展、发现与写作产物">
      <button className="research-overview-item" onClick={() => setDetail("quest")}><span>研究进展 <i aria-hidden="true">↗</i></span><strong>{quest?.text ?? milestoneCopy ?? empty}</strong><small>{quest ? "整个研究 · 最近进展" : milestoneCopy ? "阶段产物已形成 · 尚无研究结论" : "整个研究 · 最近进展"}</small></button>
      <button className="research-overview-item" onClick={() => setDetail("question")}><span>本题发现 <i aria-hidden="true">↗</i></span><strong>{question?.text ?? empty}</strong><small>当前问题 · 已接纳的研究判断</small></button>
      <button className="research-overview-item research-overview-cycle" onClick={() => setDetail("cycle")}><span><i className={`research-overview-dot${connected && runtime.running ? " is-running" : ""}`} aria-hidden="true" />{cycleOrdinalLabel(data?.cycle_ordinal)} <i aria-hidden="true">↗</i></span><strong>{runtimeLabel}</strong><small className="research-overview-finding">{cycle?.text ?? (error || data?.status === "limited" ? "本轮发现暂不可确认" : data ? "本轮尚未形成发现" : "本轮发现待载入")}</small></button>
      <button className="research-overview-item" onClick={onOpenWriting}><span>写作产物 <i aria-hidden="true">↗</i></span><strong>{writing.length ? `${writing.length} 份写作产物` : "暂无写作产物"}</strong><small>{writingState ? `${connected ? "" : "上次确认："}${writingState}` : writing.length ? writing.at(-1)?.intent.title : "查看报告、论文与演示稿"}</small></button>
    </section>
    {error && <p className="research-overview-availability" role="status">{data ? "发现更新暂不可用，保留已收到的结果。" : "研究发现暂不可用；对话与日志仍可阅读。"}{onRetry && <button onClick={onRetry}>重试</button>}</p>}
    {detail && <OverviewDialog title={labels[detail]} subtitle={detail === "quest" ? "整个研究的已接纳进展" : detail === "question" ? "当前问题的已接纳发现" : `${cycleOrdinalLabel(data?.cycle_ordinal)} · ${runtimeLabel}`} onClose={() => setDetail(null)}>
      {detail === "quest" && !quest && milestoneCopy ? <p className="overview-readable-text">{milestoneCopy}。可从顶部阶段入口查看对应产物。</p> : null}
      {detail === "cycle" && <p className="overview-readable-text">{runtimeLabel}。{!connected ? "连接中断，当前是否继续执行暂不可确认。" : "运行记录与科学发现分别展示。"}</p>}
      {selectedFindings.length ? selectedFindings.map((finding, index) => <article className="overview-finding" key={`${finding.source.commit_ref ?? finding.source.content_ref}-${index}`}><div className="overview-result-meta"><span>{cycleOrdinalLabel(finding.cycle_ordinal)}</span><span>{dispositionNames[finding.disposition] ?? "已接纳研究判断"}</span></div><p className="overview-readable-text">{finding.text}</p><SourceDetails source={{ ...finding.source, quest_ref: finding.quest_ref, question_ref: finding.question_ref, cycle_ref: finding.cycle_ref, epoch: finding.epoch, text_field: finding.text_field, disposition: finding.disposition }} /></article>) : <p className="overview-empty">{empty}。形成正式结果后，将在这里显示原文和依据。</p>}
      {error && <p className="research-overview-availability">结果更新暂不可用。{onRetry && <button onClick={onRetry}>重试</button>}</p>}
    </OverviewDialog>}
  </>;
}

type StageHistoryProps = {
  snapshot: PublicSnapshot;
  overview?: ResearchOverviewData | null;
  error?: string | null;
  loading?: boolean;
  onRetry?: () => void;
  onRequestOverview?: () => void;
  onStageResult?: (stage: OverviewStage, cycle: OverviewCycle | null, artifact: OverviewStageArtifact | null) => void;
};

export function StageHistoryStrip({ snapshot, overview, error, loading, onRetry, onStageResult, onRequestOverview }: StageHistoryProps) {
  const data = scopedOverview(snapshot, overview);
  const current = data?.cycles.find(cycle => cycle.cycle_ref === data.cycle_ref) ?? null;
  const questRef = overviewQuestRef(snapshot);
  const [selected, setSelection] = useState<{ questRef: string | null; stage: OverviewStage; cycleRef: string | null; epoch: number | null } | null>(null);
  const selection = selected?.questRef === questRef ? selected : null;
  useEffect(() => {
    const initial = spectrumStage(new URLSearchParams(window.location.search).get("stage"));
    setSelection(initial ? { questRef, stage: initial, cycleRef: null, epoch: null } : null);
  }, [questRef]);
  const cycle = selection?.cycleRef ? data?.cycles.find(item => item.cycle_ref === selection.cycleRef) ?? null : current;
  const artifacts = selection ? [...(cycle?.stages[selection.stage] ?? [])].sort((a, b) => a.epoch - b.epoch) : [];
  const artifact = artifacts.find(item => item.epoch === selection?.epoch) ?? artifacts.at(-1) ?? null;
  const open = (stage: OverviewStage) => {
    onRequestOverview?.();
    const last = [...(current?.stages[stage] ?? [])].sort((a, b) => a.epoch - b.epoch).at(-1) ?? null;
    setSelection({ questRef, stage, cycleRef: current?.cycle_ref ?? null, epoch: last?.epoch ?? null });
    onStageResult?.(stage, current, last);
  };
  return <>
    <SpectrumStages compact current={spectrumStage(snapshot.research_control.foreground?.stage)} onSelect={open} labels={Object.fromEntries((["idea", "plan", "bundle", "reasoning"] as const).map(stage => {
      const last = [...(current?.stages[stage] ?? [])].sort((a, b) => a.epoch - b.epoch).at(-1);
      const isCurrent = snapshot.research_control.foreground?.stage.toLowerCase() === stage;
      return [stage, isCurrent ? "当前阶段" : last ? acceptedStatusNames[last.status] : error || data?.status === "limited" ? "暂不可用" : data ? "尚无结果" : "查看结果"];
    }))} />
    {selection && <OverviewDialog title={stageNames[selection.stage]} subtitle={`${cycleOrdinalLabel(cycle?.ordinal)} · ${stageTechnicalNames[selection.stage]}${artifact ? ` · 记录版本 ${artifact.epoch}` : ""}`} onClose={() => setSelection(null)}>
      <div className="overview-history-controls"><label>研究轮次<select value={cycle?.cycle_ref ?? ""} onChange={event => setSelection({ ...selection, cycleRef: event.target.value, epoch: null })}>{data?.cycles.length ? data.cycles.map(item => <option key={item.cycle_ref} value={item.cycle_ref}>{cycleOrdinalLabel(item.ordinal)}{item.cycle_ref === data.cycle_ref ? " · 当前" : ""}</option>) : <option value="">轮次记录暂不可用</option>}</select></label>{artifacts.length > 1 && <label>阶段记录<select value={artifact?.epoch ?? ""} onChange={event => setSelection({ ...selection, epoch: Number(event.target.value) })}>{artifacts.map(item => <option key={item.epoch} value={item.epoch}>版本 {item.epoch} · {acceptedStatusNames[item.status]}</option>)}</select></label>}</div>
      {artifact ? <><p className="overview-result-status">{acceptedStatusNames[artifact.status]}</p><StageCoreResult artifact={artifact} cycle={cycle} /><SourceDetails source={{ ...artifact.source, cycle_ref: cycle?.cycle_ref, question_ref: cycle?.question_ref, epoch: artifact.epoch, reason: artifact.reason?.code }} />{artifact.content && <details className="overview-source-details"><summary>查看完整原始结果</summary><pre>{JSON.stringify(artifact.content, null, 2)}</pre></details>}</> : loading || (!data && !error) ? <p className="overview-empty" role="status">正在读取阶段结果与历史记录…</p> : <p className="overview-empty">{error || data?.status === "limited" ? "阶段结果暂不可用。" : "该轮次尚无此阶段的已接纳结果。"} 当前 Stage 对话仍可继续阅读。</p>}
      {error && onRetry && <button className="overview-text-button" onClick={onRetry}>重新读取阶段结果</button>}
    </OverviewDialog>}
  </>;
}

const fieldLabels: Record<string, string> = { candidate_key: "候选编号", direction: "研究方向", rationale: "依据", assumptions: "假设", risks: "风险", evidence_boundary: "证据边界", falsification_hint: "如何证伪", test: "检验方式", would_refute: "可推翻该解释的结果", note: "说明", binding: "是否约束后续选择", obligations: "回答要求", obligation_key: "要求编号", statement: "需要确认的内容", minimum_support: "最低证据要求", experiment_key: "实验编号", goal: "目标", characteristics: "设计要点", boundary_constraints: "边界条件", semantic_delta: "相较已有工作的变化", quest: "整个研究", current_question: "当前问题", cycle: "本轮", impact: "进展与影响", progress: "进展", summary: "总结", text: "内容", description: "说明", evidence_refs: "证据引用", reason: "原因", status: "状态", conclusion: "结论", claim: "判断", exploration_scope: "已探索范围", overturn_conditions: "重新考虑的条件", coverage: "覆盖范围", gap_set: "证据缺口", bundle_disposition: "执行安排", semantic_change_required: "需要调整的内容", blocker_refs: "阻碍依据", evidence: "判断依据", missing_evidence: "缺少的证据", uncertainty_basis: "不确定性", limitations: "适用限制", research_synthesis: "研究总结" };

type CoreItem = { title?: string; text?: string; details?: Record<string, unknown> };
type CoreSection = { label: string; items: CoreItem[] };
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text(value: unknown): string | undefined { return typeof value === "string" && value.trim() ? value : undefined; }
function rows(value: unknown): Record<string, unknown>[] { return Array.isArray(value) ? value.map(record) : []; }
function pick(value: Record<string, unknown>, fields: string[]): Record<string, unknown> {
  return Object.fromEntries(fields.filter(field => value[field] !== undefined && value[field] !== null).map(field => [field, value[field]]));
}

/** Only authored core fields are promoted. IDs and other structures stay folded. */
export function mapStageCoreResult(artifact: OverviewStageArtifact, cycle: OverviewCycle | null = null): { sections: CoreSection[]; supplementary: Record<string, unknown> } {
  const content = artifact.content ?? {};
  const sections: CoreSection[] = [];
  const add = (label: string, value: unknown) => { const prose = text(value); if (prose) sections.push({ label, items: [{ text: prose }] }); };
  let supplementary: Record<string, unknown> = {};
  if (artifact.stage === "idea") {
    const candidates = rows(content.candidates).flatMap(candidate => {
      const title = text(candidate.direction), rationale = text(candidate.rationale);
      return title || rationale ? [{ title, text: rationale, details: pick(candidate, ["assumptions", "risks", "evidence_boundary", "falsification_hint", "candidate_key"]) }] : [];
    });
    if (candidates.length) sections.push({ label: "候选思路", items: candidates });
    add("研究建议", record(content.recommendation).note);
    add("暂不能进入计划的原因", content.why_plan_cannot_proceed);
    supplementary = pick(content, ["exploration_scope", "overturn_conditions"]);
  } else if (artifact.stage === "plan") {
    const obligations = rows(record(content.answer_contract).obligations).flatMap(item => text(item.statement) ? [{ text: text(item.statement), details: pick(item, ["minimum_support", "obligation_key"]) }] : []);
    if (obligations.length) sections.push({ label: "需要回答什么", items: obligations });
    const experiments = rows(content.experiment_briefs).flatMap(item => text(item.goal) ? [{ text: text(item.goal), details: pick(item, ["characteristics", "boundary_constraints", "semantic_delta", "experiment_key"]) }] : []);
    if (experiments.length) sections.push({ label: "验证安排", items: experiments });
    supplementary = pick(content, ["coverage", "gap_set", "bundle_disposition"]);
  } else if (artifact.stage === "bundle") {
    const disposition = text(content.disposition);
    if (disposition) add("实验状态", dispositionNames[disposition] ?? disposition);
    const formalPlanRef = text(content.formal_plan_ref);
    const matchingPlan = formalPlanRef ? cycle?.stages.plan?.find(item => item.source.outcome_ref === formalPlanRef || item.source.formal_plan_ref === formalPlanRef) : undefined;
    const labels = new Map(rows(matchingPlan?.content?.experiment_briefs).flatMap(item => text(item.experiment_key) && text(item.goal) ? [[String(item.experiment_key), String(item.goal)]] : []));
    for (const [field, label] of [["realized_experiment_keys", "已执行的实验"], ["remaining_experiment_keys", "仍待执行的实验"]]) {
      const keys = content[field];
      if (Array.isArray(keys) && keys.length) sections.push({ label, items: keys.flatMap(key => typeof key === "string" ? [{ text: labels.get(key) ?? `实验编号：${key}`, details: labels.has(key) ? { experiment_key: key } : undefined }] : []) });
    }
    supplementary = pick(content, ["semantic_change_required", "blocker_refs"]);
  } else {
    add("研究判断", content.claim);
    const disposition = text(content.disposition);
    if (disposition) add("证据支持情况", dispositionNames[disposition] ?? disposition);
    add("本轮研究总结", record(record(content.research_synthesis).cycle).impact);
    supplementary = pick(content, ["evidence", "missing_evidence", "uncertainty_basis", "limitations", "research_synthesis"]);
  }
  return { sections, supplementary };
}

function StageCoreResult({ artifact, cycle }: { artifact: OverviewStageArtifact; cycle: OverviewCycle | null }) {
  if (!artifact.content) return <p className="overview-empty">{artifact.status === "skipped" ? "该阶段已按本轮安排跳过；来源记录保留在下方。" : "目前没有可展示的结果正文；可查看下方来源记录。"}</p>;
  const mapped = mapStageCoreResult(artifact, cycle);
  return <div className="overview-core-result">{mapped.sections.length ? mapped.sections.map(section => <section key={section.label}><h3>{section.label}</h3><ol className={`overview-core-items${section.items.length === 1 ? " is-single" : ""}`}>{section.items.map((item, index) => <li key={index}>{item.title && <h4>{item.title}</h4>}{item.text && <p className="overview-readable-text">{item.text}</p>}{item.details && Object.keys(item.details).length > 0 && <details className="overview-source-details overview-core-extra"><summary>展开条件与依据</summary><ReadableValue value={item.details} /></details>}</li>)}</ol></section>) : <p className="overview-empty">这份结果尚无可映射的核心字段，完整正文保留在下方。</p>}{Object.keys(mapped.supplementary).length > 0 && <details className="overview-source-details"><summary>展开证据、限制与其他说明</summary><ReadableValue value={mapped.supplementary} /></details>}</div>;
}

function ReadableValue({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value === null || value === undefined) return <span className="overview-muted">未提供</span>;
  if (typeof value === "string") return <p className="overview-readable-text">{dispositionNames[value] ?? value}</p>;
  if (typeof value === "number" || typeof value === "boolean") return <span>{typeof value === "boolean" ? value ? "是" : "否" : value}</span>;
  if (Array.isArray(value)) return value.length ? <ul className="overview-readable-list">{value.map((item, index) => <li key={index}><ReadableValue value={item} depth={depth + 1} /></li>)}</ul> : <p className="overview-muted">未列出项目</p>;
  if (typeof value === "object") return depth > 5 ? <pre className="overview-long-value">{JSON.stringify(value, null, 2)}</pre> : <dl className="overview-readable-fields">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{fieldLabels[key] ?? key}</dt><dd><ReadableValue value={item} depth={depth + 1} /></dd></div>)}</dl>;
  return null;
}

const sourceLabels: Record<string, string> = { quest_ref: "研究", question_ref: "问题", cycle_ref: "轮次记录", epoch: "阶段记录序号", commit_ref: "阶段提交", request_ref: "阶段请求", run_ref: "执行记录", outcome_ref: "结果", content_ref: "原文", content_hash: "原文校验值", text_field: "摘要原文字段", disposition: "结果状态原值", reason: "说明代码" };
function SourceDetails({ source }: { source: Record<string, unknown> }) {
  const entries = Object.entries(source).filter(([, value]) => value !== null && value !== undefined && value !== "");
  return <details className="overview-source-details"><summary>查看来源与核验记录</summary><dl>{entries.map(([key, value]) => <div key={key}><dt>{sourceLabels[key] ?? key}</dt><dd>{typeof value === "string" || typeof value === "number" ? String(value) : JSON.stringify(value)}</dd></div>)}</dl></details>;
}

function OverviewDialog({ title, subtitle, children, onClose }: { title: string; subtitle: string; children: ReactNode; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const subtitleId = useId();
  useEffect(() => {
    const returnTo = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    closeRef.current?.focus({ preventScroll: true });
    return () => { if (dialog?.open) dialog.close(); if (returnTo?.isConnected) returnTo.focus({ preventScroll: true }); };
  }, []);
  return <dialog ref={dialogRef} className="research-overview-dialog" aria-labelledby={titleId} aria-describedby={subtitleId} onCancel={event => { event.preventDefault(); onClose(); }} onClose={onClose}><div className="research-overview-dialog-layout"><header><div><h2 id={titleId}>{title}</h2><p id={subtitleId}>{subtitle}</p></div><button ref={closeRef} type="button" onClick={onClose} aria-label={`关闭${title}`}>×</button></header><div className="research-overview-dialog-body">{children}</div><footer><button type="button" onClick={onClose}>返回研究</button></footer></div></dialog>;
}
