import { useCallback, useEffect, useId, useRef, useState, type CSSProperties, type ReactNode } from "react";
import type { PublicSnapshot, WritingReportView } from "./api";
import "./research-overview.css";
import { SpectrumStages, spectrumStage } from "./Spectrum";
import { StageRootSessions, RootConversations, useRootConversations, rootSessionStatus, type RootConversationsModel } from "./RootConversations";
import { BoundedDetails, PageWindow } from "./BoundedDetails";
import type { RootSession } from "./rootSessionsApi";
import { summaryNeedsAcceptedResultReview, useTimelineSummaries, type TimelineSummaryNode } from "./timelineSummaries";
import { MetaTrace } from "./MetaTrace";
import { useResearchMotion } from "./ResearchMotion";
import { useTimelineHistory } from "./timelineHistoryApi";

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
  // Visible summaries refresh when a stage/result changes, not for every
  // execution heartbeat. Expanded history retains revision-based refresh.
  const revision = refreshOnRevision ? snapshot?.revision : undefined;
  const resultVersion = JSON.stringify([
    snapshot?.research_control.foreground?.stage, snapshot?.research_control.foreground?.epoch,
    snapshot?.idea_stage?.stage_commit, snapshot?.idea_stage?.outcome_acceptance?.outcome_ref,
    snapshot?.plan_stage?.stage_commit, snapshot?.plan_stage?.plan_acceptance?.outcome_ref,
    snapshot?.bundle_stage?.stage_commit, snapshot?.bundle_stage?.bundle_report,
    snapshot?.reasoning_stage?.stage_commit, snapshot?.reasoning_stage?.reasoning_acceptance?.outcome_ref,
  ]);
  const identity = JSON.stringify([questRef, cycleRef, questionRef]);
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState<{ identity: string; data: ResearchOverviewData | null; error: string | null; loading: boolean }>({ identity: "", data: null, error: null, loading: false });
  const retry = useCallback(() => setAttempt(value => value + 1), []);
  const queryVersion = JSON.stringify([resultVersion, revision, attempt]);
  const latestQueryVersion = useRef(queryVersion);
  latestQueryVersion.current = queryVersion;
  const refresh = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!active || !questRef) return;
    const controller = new AbortController();
    let running = false;
    let requestedVersion: string | null = null;
    const read = async () => {
      if (controller.signal.aborted || running || requestedVersion === latestQueryVersion.current) return;
      running = true;
      requestedVersion = latestQueryVersion.current;
      setResult(previous => ({ identity, data: previous.identity === identity ? previous.data : null, error: null, loading: true }));
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
      } finally {
        running = false;
        // Same-scope revisions must not cancel a slow history read. Deliver
        // that cut first, then coalesce intervening updates into one new read.
        if (!controller.signal.aborted && requestedVersion !== latestQueryVersion.current) void read();
      }
    };
    refresh.current = read;
    void read();
    return () => { controller.abort(); if (refresh.current === read) refresh.current = null; };
  }, [active, questRef, cycleRef, questionRef, identity]);
  useEffect(() => { refresh.current?.(); }, [queryVersion]);

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

type ResearchRuntimeState = "absent" | "unknown" | "paused" | "waiting" | "running" | "completed" | "idle";

export function cycleRuntime(snapshot: PublicSnapshot): { state: ResearchRuntimeState; label: string; running: boolean; executionPrevented?: boolean } {
  const foreground = snapshot.research_control.foreground;
  if (!foreground) return { state: "absent", label: "尚未进入研究轮次", running: false };
  if (snapshot.research_control.status !== "ready") return { state: "unknown", label: "当前运行状态暂不可确认", running: false, executionPrevented: true };
  const stageKey = foreground.stage.toLowerCase() as OverviewStage;
  const stage = stageNames[stageKey] ?? "当前阶段";
  if (["completed", "closed"].includes(foreground.status)) return { state: "completed", label: "本轮已结束", running: false, executionPrevented: true };
  if (["paused", "suspended"].includes(foreground.status) || foreground.grant_status === "suspended") return { state: "paused", label: `${stage} · 已暂停`, running: false, executionPrevented: true };
  if (["blocked", "waiting", "revoked", "abandoned"].includes(foreground.status) || ["revoked", "abandoned"].includes(foreground.grant_status)) return { state: "waiting", label: `${stage} · 等待继续条件`, running: false, executionPrevented: true };
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
  if (statuses.some(status => status === "paused" || status === "suspended")) return { state: "paused", label: `${stage} · 已暂停`, running: false, executionPrevented: true };
  if (run?.blocker || statuses.some(status => status === "blocked" || status === "waiting" || status?.startsWith("waiting_"))) return { state: "waiting", label: `${stage} · 等待继续条件`, running: false };
  if (statuses.some(status => status === "active" || status === "running")) return { state: "running", label: `${stage} · 执行中`, running: true };
  if (statuses.some(status => status === "completed" || status === "succeeded")) return { state: "completed", label: `${stage} · 本次输出已结束`, running: false };
  return { state: "idle", label: `${stage} · 暂无新的执行状态`, running: false };
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
  rootConversations?: RootConversationsModel;
};

export function ResearchOverview({ snapshot, overview, error, onRetry, onOpenWriting, connected = true, rootConversations }: OverviewProps) {
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
  const currentStage = spectrumStage(snapshot.research_control.foreground?.stage);
  const runtime = currentStage ? currentStageStatus(currentStage, snapshot, rootConversations) : cycleRuntime(snapshot);
  const motion = useResearchMotion<HTMLElement>(connected && runtime.state === "running");
  const runtimeLabel = connected ? runtime.label : `上次确认：${runtime.label}`;
  const labels = { quest: "研究进展", question: "本题发现", cycle: "本轮进展" };
  const empty = !overviewQuestRef(snapshot) ? "尚未建立研究" : data?.status === "limited" ? "发现暂不可确认" : data ? "尚未形成研究发现" : error ? "发现暂不可用" : "正在读取研究发现";
  const selectedFindings = detail ? findings[detail] : [];
  return <>
    <section className="research-overview" aria-label="研究进展、发现与写作产物" ref={motion.ref} data-motion-active={motion.active}>
      <button className="research-overview-item" onClick={() => setDetail("quest")}><span>研究进展 <i aria-hidden="true">↗</i></span><strong>{quest?.text ?? milestoneCopy ?? empty}</strong><small>{quest ? "整个研究 · 最近进展" : milestoneCopy ? "阶段产物已形成 · 尚无研究结论" : "整个研究 · 最近进展"}</small></button>
      <button className="research-overview-item" onClick={() => setDetail("question")}><span>本题发现 <i aria-hidden="true">↗</i></span><strong>{question?.text ?? empty}</strong><small>当前问题 · 已接纳的研究判断</small></button>
      <button className="research-overview-item research-overview-cycle" onClick={() => setDetail("cycle")}><span><i className={`research-overview-dot${motion.active ? " is-running" : ""}`} aria-hidden="true" />{cycleOrdinalLabel(data?.cycle_ordinal)} <i aria-hidden="true">↗</i></span><strong>{runtimeLabel}</strong><small className="research-overview-finding">{cycle?.text ?? (error || data?.status === "limited" ? "本轮发现暂不可确认" : data ? "本轮发现尚未形成" : "本轮发现待载入")}</small></button>
      <button className="research-overview-item" onClick={onOpenWriting}><span>写作产物 <i aria-hidden="true">↗</i></span><strong>{writing.length ? `${writing.length} 份写作产物` : "暂无写作产物"}</strong><small>{writingState ? `${connected ? "" : "上次确认："}${writingState}` : writing.length ? writing.at(-1)?.intent.title : "查看报告、论文与演示稿"}</small></button>
    </section>
    {error && <p className="research-overview-availability" role="status">{data ? "发现更新暂不可用，保留已收到的结果。" : "研究发现暂不可用；对话与日志仍可阅读。"}{onRetry && <button onClick={onRetry}>重试</button>}</p>}
    {detail && <OverviewDialog title={labels[detail]} subtitle={detail === "quest" ? "整个研究的已接纳进展" : detail === "question" ? "当前问题的已接纳发现" : `${cycleOrdinalLabel(data?.cycle_ordinal)} · ${runtimeLabel}`} onClose={() => setDetail(null)}>
      {detail === "quest" && !quest && milestoneCopy ? <p className="overview-readable-text">{milestoneCopy}。可从顶部阶段入口查看对应产物。</p> : null}
      {detail === "cycle" && <p className="overview-readable-text">{runtimeLabel}。{!connected ? "连接中断，当前是否继续执行暂不可确认。" : "运行记录与科学发现分别展示。"}</p>}
      {selectedFindings.length ? <PageWindow key={detail} items={selectedFindings} size={8} label="研究判断分页" render={(finding, index) => <article className="overview-finding" key={`${finding.source.commit_ref ?? finding.source.content_ref}-${index}`}><div className="overview-result-meta"><span>{cycleOrdinalLabel(finding.cycle_ordinal)}</span><span>{dispositionNames[finding.disposition] ?? "已接纳研究判断"}</span></div><p className="overview-readable-text">{finding.text}</p><SourceDetails source={{ ...finding.source, quest_ref: finding.quest_ref, question_ref: finding.question_ref, cycle_ref: finding.cycle_ref, epoch: finding.epoch, text_field: finding.text_field, disposition: finding.disposition }} /></article>} /> : <p className="overview-empty">{empty}。形成正式结果后，将在这里显示原文和依据。</p>}
      {error && <p className="research-overview-availability">结果更新暂不可用。{onRetry && <button onClick={onRetry}>重试</button>}</p>}
    </OverviewDialog>}
  </>;
}

type StageHistoryProps = {
  rootConversations?: RootConversationsModel;
  snapshot: PublicSnapshot;
  overview?: ResearchOverviewData | null;
  error?: string | null;
  loading?: boolean;
  onRetry?: () => void;
  onRequestOverview?: () => void;
  onStageResult?: (stage: OverviewStage, cycle: OverviewCycle | null, artifact: OverviewStageArtifact | null) => void;
};

/** Read the current work separately from the recorder's interpretation and scientific findings. */
export function ResearchBrief({ snapshot, overview, error, rootConversations }: {
  snapshot: PublicSnapshot; overview?: ResearchOverviewData | null; error?: string | null; rootConversations?: RootConversationsModel;
}) {
  const data = scopedOverview(snapshot, overview);
  const foreground = snapshot.research_control.foreground;
  const stage = spectrumStage(foreground?.stage);
  const stageState = stage ? currentStageStatus(stage, snapshot, rootConversations) : cycleRuntime(snapshot);
  const running = stageState.state === "running";
  const motion = useResearchMotion<HTMLElement>(running);
  const summaries = useTimelineSummaries(overviewQuestRef(snapshot), motion.visible);
  const nodeKey = `cycle:${foreground?.cycle_ref}`;
  const node = summaries.nodes[nodeKey];
  const requests = snapshot.human_collaboration?.human_requests;
  const ownRequests = foreground && requests?.status === "ready" ? requests.items.filter(request => request.status === "open"
    && request.quest_ref?.replace(/^quest:/, "") === foreground?.quest_ref.replace(/^quest:/, "")) : [];
  const paused = ["paused", "suspended"].includes(foreground?.status ?? "") || foreground?.grant_status === "suspended" || stageState.state === "paused";
  const projection = stage ? ({ idea: snapshot.idea_stage, plan: snapshot.plan_stage, bundle: snapshot.bundle_stage, reasoning: snapshot.reasoning_stage })[stage] : null;
  const exactCycle = (projection?.stage_run_request?.cycle_ref ?? projection?.eligibility.cycle_ref) === foreground?.cycle_ref;
  const failed = !rootConversations?.error && !rootConversations?.context.stale && !rootConversations?.data?.limited && exactCycle
    && rootConversations?.sessions.some(session => session.cycle_ref === foreground?.cycle_ref
      && session.question_ref === foreground?.question_ref && session.is_current !== false && spectrumStage(session.stage) === stage && session.status === "failed"
      && (session.kind === "target" ? snapshot.bundle_stage?.target_graph.targets.some(target => target.target_ref === session.target_ref && target.target_run_ref === session.run_ref && target.status === "failed")
        : session.kind === "stage" && projection?.run?.run_ref === session.run_ref));
  const wait = rootConversations?.error || rootConversations?.context.stale ? "当前等待状态暂不可确认。"
    : paused ? "等待继续研究。"
      : ownRequests.length ? `还有 ${ownRequests.length} 项求助等你处理；可从求助入口查看。`
        : failed ? "有工作遇到阻碍，需查看对应会话的说明。"
          : stageState.state === "waiting" ? "还在等待继续条件，可查看当前工作说明。"
            : running ? "等待这次工作完成，再核对结果。"
              : foreground ? "暂无已确认的等待事项；执行状态见当前工作。" : "等待进入研究轮次。";
  const finding = latestFinding(data?.findings.cycle ?? []) ?? latestFinding((data?.findings.question ?? []).filter(item => item.question_ref === foreground?.question_ref));
  const confirmed = Boolean(finding && ["affirmed", "supported", "denied", "refuted", "answered"].includes(finding.disposition));
  const [findingOpen, setFindingOpen] = useState(false);
  const scope = JSON.stringify([foreground?.quest_ref, foreground?.question_ref, foreground?.cycle_ref]);
  useEffect(() => setFindingOpen(false), [scope]);
  const currentCycle = data?.cycles.find(cycle => cycle.cycle_ref === data.cycle_ref);
  const acceptedResultNeedsReview = currentCycle ? summaryNeedsAcceptedResultReview(node, currentCycle,
    stages.flatMap(item => currentCycle.stages[item] ?? []), rootConversations?.sessions ?? [], summaries.observedAt) : false;
  const openCurrent = () => {
    if (stage) rootConversations?.selectStage(stage);
    requestAnimationFrame(() => document.getElementById("research-activity")?.focus({ preventScroll: true }));
    document.getElementById("research-activity")?.scrollIntoView({ block: "start" });
  };
  return <section className="research-brief" aria-label="研究近况" ref={motion.ref} data-motion-active={motion.active}>
    <header><h2><MetaTrace variant="brief" active={motion.active} />研究近况</h2><span>{cycleOrdinalLabel(data?.cycle_ordinal)}</span></header>
    <div className="research-brief-facts">
      <div><h3>正在做什么</h3><p>{running && stage ? `正在开展${stageNames[stage]}。` : stageState.label}</p></div>
      <div><h3>还在等什么</h3><p>{wait}</p></div>
    </div>
    <div className="research-brief-process"><h3>本轮工作记录</h3><TimelineSummary nodeKey={nodeKey} node={node} className="research-brief-summary" unavailable={summaries.error} acceptedResultNeedsReview={acceptedResultNeedsReview} /></div>
    <div className="research-brief-finding"><h3>{finding ? confirmed ? "已确认科研结论" : "待确认发现" : "目前知道什么"}</h3><p>{finding?.text ?? (error ? "研究发现暂不可用，已读取的工作记录仍可查看。" : data ? "本轮尚无可确认的科研结论。" : "正在读取发现；暂不作科研判断。")}</p>{finding ? <small>{dispositionNames[finding.disposition] ?? "尚待核对"} · {cycleOrdinalLabel(finding.cycle_ordinal)}</small> : null}</div>
    <footer>{rootConversations && stage ? <button type="button" onClick={openCurrent}>查看当前工作 ↗</button> : null}{finding ? <button type="button" onClick={() => setFindingOpen(true)}>查看发现依据</button> : null}</footer>
    {summaries.error ? <p className="research-brief-availability" role="status">近况摘要读取暂不可用，保留已读取内容；稍后自动重试。</p> : null}
    {error && finding ? <p className="research-brief-availability" role="status">发现更新暂不可用，保留上次读取的发现与依据。</p> : null}
    {findingOpen && finding ? <OverviewDialog title="发现依据" subtitle={`${confirmed ? "已确认科研结论" : "待确认发现"} · ${cycleOrdinalLabel(finding.cycle_ordinal)}`} onClose={() => setFindingOpen(false)}><div className="overview-result-meta">{dispositionNames[finding.disposition] ?? "尚待核对"}</div><p className="overview-readable-text">{finding.text}</p><SourceDetails source={{ ...finding.source, quest_ref: finding.quest_ref, question_ref: finding.question_ref, cycle_ref: finding.cycle_ref, epoch: finding.epoch, disposition: finding.disposition }} /></OverviewDialog> : null}
  </section>;
}

export function StageHistoryStrip({ snapshot, overview, error, loading, onRetry, onStageResult, onRequestOverview, rootConversations }: StageHistoryProps) {
  const data = scopedOverview(snapshot, overview);
  const current = data?.cycles.find(cycle => cycle.cycle_ref === data.cycle_ref) ?? null;
  const foregroundStage = spectrumStage(snapshot.research_control.foreground?.stage);
  const questRef = overviewQuestRef(snapshot);
  const [selected, setSelection] = useState<{ questRef: string | null; stage: OverviewStage; cycleRef: string | null; epoch: number | null } | null>(null);
  const selection = selected?.questRef === questRef ? selected : null;
  useEffect(() => {
    const initial = spectrumStage(new URLSearchParams(window.location.search).get("stage"));
    setSelection(initial ? { questRef, stage: initial, cycleRef: null, epoch: null } : null);
  }, [questRef]);
  const open = (stage: OverviewStage) => {
    onRequestOverview?.();
    const last = [...(current?.stages[stage] ?? [])].sort((a, b) => a.epoch - b.epoch).at(-1) ?? null;
    setSelection({ questRef, stage, cycleRef: current?.cycle_ref ?? null, epoch: last?.epoch ?? null });
    onStageResult?.(stage, current, last);
  };
  return <>
    {loading && data ? <p className="overview-empty" role="status">历史成果正在更新，以下保留已读取记录；当前运行状态见阶段会话。</p> : null}
    <SpectrumStages compact running={foregroundStage !== null && currentStageStatus(foregroundStage, snapshot, rootConversations).state === "running"} stageContent={rootConversations ? stage => <StageRootSessions model={rootConversations} stage={stage} /> : undefined} selected={rootConversations?.selectedStage} current={foregroundStage} onSelect={rootConversations ? stage => rootConversations.selectStage(stage) : open} onResult={open} labels={Object.fromEntries((["idea", "plan", "bundle", "reasoning"] as const).map(stage => {
      const last = [...(current?.stages[stage] ?? [])].sort((a, b) => a.epoch - b.epoch).at(-1);
      const isCurrent = snapshot.research_control.foreground?.stage.toLowerCase() === stage;
      return [stage, isCurrent ? "当前阶段" : last ? acceptedStatusNames[last.status] : error || data?.status === "limited" ? "暂不可用" : data ? "尚无结果" : "查看结果"];
    }))} />
    {selection && <StageResultDialog data={data} selection={selection} onSelect={next => setSelection({ questRef, ...next })} onClose={() => setSelection(null)} error={error} loading={loading} onRetry={onRetry} />}
  </>;
}

function currentStageStatus(stage: OverviewStage, snapshot: PublicSnapshot, model?: RootConversationsModel): { state: ResearchRuntimeState; label: string } {
  const foreground = snapshot.research_control.foreground;
  if (model?.context.stale || model?.error || model?.data?.limited) return { state: "unknown", label: "状态待确认" };
  const observedControl = model?.context.foreground ?? foreground;
  if (["paused", "suspended"].includes(observedControl?.status ?? "") || observedControl?.grant_status === "suspended") return { state: "paused", label: "已暂停" };
  if (["completed", "closed"].includes(observedControl?.status ?? "")) return { state: "completed", label: "本轮已结束" };
  if (["blocked", "waiting", "revoked", "abandoned"].includes(observedControl?.status ?? "")
    || ["revoked", "abandoned"].includes(observedControl?.grant_status ?? "")) return { state: "waiting", label: "等待继续条件" };
  const sessions = model?.sessions.filter(session => session.cycle_ref === foreground?.cycle_ref
    && (!session.question_ref || session.question_ref === foreground?.question_ref) && session.is_current !== false) ?? [];
  const projection = ({ idea: snapshot.idea_stage, plan: snapshot.plan_stage, bundle: snapshot.bundle_stage, reasoning: snapshot.reasoning_stage })[stage];
  const exactCycle = (projection?.stage_run_request?.cycle_ref ?? projection?.eligibility.cycle_ref) === foreground?.cycle_ref;
  const active = sessions.find(session => (session.kind === "stage" || stage === "bundle" && session.kind === "target")
    && exactCycle && session.is_executing && spectrumStage(session.stage) === stage
    && (session.kind === "target" ? snapshot.bundle_stage?.target_graph.targets.some(target => target.target_ref === session.target_ref
      && target.target_run_ref === session.run_ref && target.status === "running") : projection?.run?.run_ref === session.run_ref));
  const runtime = cycleRuntime(snapshot);
  const controlAllowsExecution = foreground?.status === "active" && foreground.grant_status === "active";
  if (active && controlAllowsExecution && !runtime.executionPrevented && (stage === "bundle" || runtime.running)) return { state: "running", label: "执行中" };
  if (exactCycle && sessions.some(session => session.kind === "stage" && spectrumStage(session.stage) === stage
    && !session.is_executing && session.status === "waiting")) return { state: "waiting", label: "等待继续条件" };
  // A managed lease can remain running while its root has durably waited.
  // Animation requires a positively matched executing root or Target.
  if (runtime.state === "running") return { state: "unknown", label: "执行状态待确认" };
  return { state: runtime.state, label: runtime.label };
}

type StageSelection = { cycleRef: string | null; stage: OverviewStage; epoch: number | null };

/** One dialog serves both the spectrum strip and the research timeline; selection stays controlled. */
function StageResultDialog({ data, selection, onSelect, onClose, error, loading, onRetry }: {
  data: ResearchOverviewData | null;
  selection: StageSelection;
  onSelect: (selection: StageSelection) => void;
  onClose: () => void;
  error?: string | null;
  loading?: boolean;
  onRetry?: () => void;
}) {
  const current = data?.cycles.find(item => item.cycle_ref === data.cycle_ref) ?? null;
  const cycle = selection.cycleRef ? data?.cycles.find(item => item.cycle_ref === selection.cycleRef) ?? current : current;
  const artifacts = [...(cycle?.stages[selection.stage] ?? [])].sort((a, b) => a.epoch - b.epoch);
  const artifact = artifacts.find(item => item.epoch === selection.epoch) ?? artifacts.at(-1) ?? null;
  return <OverviewDialog title={stageNames[selection.stage]} subtitle={`${cycleOrdinalLabel(cycle?.ordinal)} · ${stageTechnicalNames[selection.stage]}${artifact ? ` · 记录版本 ${artifact.epoch}` : ""}`} onClose={onClose}>
    {loading && data ? <p className="overview-empty" role="status">历史成果正在更新，以下保留已读取记录。</p> : null}
    <div className="overview-history-controls"><label>研究轮次<select value={cycle?.cycle_ref ?? ""} onChange={event => onSelect({ ...selection, cycleRef: event.target.value, epoch: null })}>{data?.cycles.length ? data.cycles.map(item => <option key={item.cycle_ref} value={item.cycle_ref}>{cycleOrdinalLabel(item.ordinal)}{item.cycle_ref === data.cycle_ref ? " · 当前" : ""}</option>) : <option value="">轮次记录暂不可用</option>}</select></label>{artifacts.length > 1 && <label>阶段记录<select value={artifact?.epoch ?? ""} onChange={event => onSelect({ ...selection, epoch: Number(event.target.value) })}>{artifacts.map(item => <option key={item.epoch} value={item.epoch}>版本 {item.epoch} · {acceptedStatusNames[item.status]}</option>)}</select></label>}</div>
    {artifact ? <><p className="overview-result-status">{acceptedStatusNames[artifact.status]}</p><StageCoreResult artifact={artifact} cycle={cycle} /><SourceDetails source={{ ...artifact.source, cycle_ref: cycle?.cycle_ref, question_ref: cycle?.question_ref, epoch: artifact.epoch, reason: artifact.reason?.code }} />{artifact.content && <details className="overview-source-details"><summary>查看完整原始结果</summary><pre>{JSON.stringify(artifact.content, null, 2)}</pre></details>}</> : loading || (!data && !error) ? <p className="overview-empty" role="status">正在读取阶段结果与历史记录…</p> : <p className="overview-empty">{error || data?.status === "limited" ? "阶段结果暂不可用。" : "该轮次尚无此阶段的已接纳结果。"} 当前 Stage 对话仍可继续阅读。</p>}
    {error && onRetry && <button className="overview-text-button" onClick={onRetry}>重新读取阶段结果</button>}
  </OverviewDialog>;
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
  return <div className="overview-core-result">{mapped.sections.length ? mapped.sections.map(section => <section key={section.label}><h3>{section.label}</h3><div className="overview-core-items"><PageWindow items={section.items} label={`${section.label}分页`} render={(item, index) => <article key={index}>{item.title && <h4>{item.title}</h4>}{item.text && <p className="overview-readable-text">{item.text}</p>}{item.details && Object.keys(item.details).length > 0 && <BoundedDetails className="overview-source-details overview-core-extra" summary="展开条件与依据">{() => <ReadableValue value={item.details} />}</BoundedDetails>}</article>} /></div></section>) : <p className="overview-empty">这份结果尚无可映射的核心字段，完整正文保留在下方。</p>}{Object.keys(mapped.supplementary).length > 0 && <BoundedDetails className="overview-source-details" summary="展开证据、限制与其他说明">{() => <ReadableValue value={mapped.supplementary} />}</BoundedDetails>}</div>;
}

function ReadableValue({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  if (value === null || value === undefined) return <span className="overview-muted">未提供</span>;
  if (typeof value === "string") return <p className="overview-readable-text">{dispositionNames[value] ?? value}</p>;
  if (typeof value === "number" || typeof value === "boolean") return <span>{typeof value === "boolean" ? value ? "是" : "否" : value}</span>;
  if (Array.isArray(value)) return value.length ? <div className="overview-readable-list"><PageWindow items={value} label="详细记录分页" render={(item, index) => <div key={index}><ReadableValue value={item} depth={depth + 1} /></div>} /></div> : <p className="overview-muted">未列出项目</p>;
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

const stageAccents: Record<OverviewStage, string> = { idea: "#257c70", plan: "#3b67ab", bundle: "#6656a6", reasoning: "#995a32" };
const summaryKindNames = { process: "过程摘要", tentative_finding: "待确认发现", accepted_conclusion: "已确认科研结论", insufficient_evidence: "证据不足" };

function TimelineSummary({ nodeKey, node, className, unavailable, acceptedResultNeedsReview = false }: {
  nodeKey: string; node?: TimelineSummaryNode; className: string; unavailable: boolean; acceptedResultNeedsReview?: boolean;
}) {
  const sentence = node?.summary?.trim() ? node.summary : null;
  const stale = Boolean(node?.source_hash && node.source_hash !== node.summarized_source_hash);
  const status = node?.status ?? "pending";
  const note = unavailable && sentence ? "连接暂不可用"
    : !sentence ? null : status === "failed" ? "更新暂不可用"
      : status === "updating" ? "正在更新总结"
        : status === "pending" || stale ? "总结待更新" : null;
  const sourceTitle = node?.sources.map(source => source.label ? `${source.label} · ${source.ref}` : source.ref).join("\n");
  const savedAt = node?.updated_at != null ? `总结保存于 ${new Date(node.updated_at * 1_000).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  })}` : "既有总结（保存时间未知）";
  const title = [sentence ? `记录员独立摘要 · ${savedAt}` : null,
    sourceTitle ? `依据：\n${sourceTitle}` : null].filter(Boolean).join("\n");
  return <span className={`${className} timeline-summary`} data-summary-key={nodeKey} data-summary-kind={node?.summary_kind ?? "process"} data-summary-status={status} data-summary-stale={stale}>
    {sentence ? <small className="timeline-summary-kind">{summaryKindNames[node?.summary_kind ?? "process"]}</small> : null}
    <span className="timeline-summary-text" title={title || undefined}>{sentence ?? (status === "failed" ? "总结暂未生成" : "记录员正在整理…")}</span>
    {sentence && acceptedResultNeedsReview ? <small className="timeline-summary-status timeline-summary-result-review">已有正式阶段成果，记录员摘要待核对；请查看正式成果。</small> : null}
    {sentence ? <small className="timeline-summary-saved-at">{savedAt}</small> : null}
    {note ? <small className="timeline-summary-status">{note}</small> : null}
  </span>;
}

type TimelineProps = {
  snapshot: PublicSnapshot;
  overview?: ResearchOverviewData | null;
  error?: string | null;
  onRetry?: () => void;
  rootConversations?: RootConversationsModel;
};

function timelineTargets(snapshot: PublicSnapshot, cycle: OverviewCycle, sessions: RootSession[], recordedNodes: TimelineSummaryNode[], unavailable: boolean, paused: boolean) {
  const unique = new Map<string, RootSession>();
  const prefer = (candidate: RootSession, previous: RootSession) =>
    Number(previous.activity_label === "已由新会话接续") - Number(candidate.activity_label === "已由新会话接续")
    || Number(candidate.is_current === true) - Number(previous.is_current === true)
    || Number(candidate.is_executing) - Number(previous.is_executing)
    || (candidate.updated_at ?? candidate.created_at ?? 0) - (previous.updated_at ?? previous.created_at ?? 0);
  for (const session of [...sessions].sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0))) {
    if (session.kind === "target" && session.target_ref && session.cycle_ref === cycle.cycle_ref
      && (!session.question_ref || session.question_ref === cycle.question_ref)) {
      const previous = unique.get(session.target_ref);
      if (!previous || prefer(session, previous) >= 0) unique.set(session.target_ref, session);
    }
  }
  const bundle = snapshot.bundle_stage;
  const exact = (bundle?.stage_run_request?.cycle_ref ?? bundle?.eligibility.cycle_ref) === cycle.cycle_ref
    && (!bundle?.eligibility.question_ref || bundle.eligibility.question_ref === cycle.question_ref);
  const graph = exact ? bundle?.target_graph.targets ?? [] : [];
  const recorded = [...unique.values()].sort((a, b) => (a.short_title || a.title).localeCompare(b.short_title || b.title, undefined, { numeric: true }));
  // Suspending the foreground can withdraw the live graph. The recorder's
  // source-bound historical identities remain readable, without a live status.
  const savedRefs = recordedNodes.filter(node => node.kind === "target" && node.cycle_ref === cycle.cycle_ref
    && node.question_ref === cycle.question_ref && node.target_ref).map(node => node.target_ref!);
  // Starting a later Target first must not reorder or renumber pending work.
  const refs = [...new Set([...graph.map(target => target.target_ref), ...recorded.map(session => session.target_ref!), ...savedRefs])];
  return refs.map((ref, index) => {
    const target = graph.find(item => item.target_ref === ref);
    const session = unique.get(ref);
    const currentSession = session && (!target?.target_run_ref || target.target_run_ref === session.run_ref) ? session : undefined;
    const label = session?.short_title || `Target ${index + 1}`;
    const commit = target && bundle?.target_commits.find(item => item.target_ref === ref && item.target_spec_hash === target.spec_hash
      && (!target.target_run_ref || item.target_run_ref === target.target_run_ref));
    const status = commit && target ? "结果已接纳"
      : unavailable ? "状态待确认"
      : paused && currentSession?.is_executing ? "已暂停"
      : currentSession ? rootSessionStatus(currentSession)
      : !target ? "状态待确认"
      : target?.status === "running" ? "等待执行记录"
      : target?.status === "failed" || target?.blocker ? "执行受阻"
      : "等待启动";
    return { ref, label, status, session: currentSession,
      executing: !unavailable && !paused && !commit && Boolean(currentSession?.is_executing) };
  });
}

/** Keep consecutive Question visits in time order: revisiting a Question never moves its new Cycle into the past. */
export function ResearchTimeline({ snapshot, overview, error, onRetry, rootConversations }: TimelineProps) {
  const liveQuestRef = overviewQuestRef(snapshot);
  const [browsingQuestRef, setBrowsingQuestRef] = useState<string | null>(null);
  const questRef = browsingQuestRef ?? liveQuestRef;
  const isLiveQuest = questRef === liveQuestRef;
  const questionVersion = isLiveQuest ? JSON.stringify(snapshot.question_tree.items.map(item => item.question_ref)) : "history";
  const history = useTimelineHistory(questRef, liveQuestRef, questionVersion);
  const availableData = isLiveQuest ? scopedOverview(snapshot, overview) : history.overview;
  const retainedData = useRef<{ questRef: string | null; data: ResearchOverviewData } | null>(null);
  if (availableData) retainedData.current = { questRef, data: availableData };
  // A new live scope starts an overview read. Keep this Quest's existing tree
  // mounted so that its expanded rows and the reader's scroll survive that gap.
  const data = availableData ?? (retainedData.current?.questRef === questRef ? retainedData.current.data : null);
  const readError = isLiveQuest ? error : history.overviewError;
  const retryHistory = () => { history.retry(); if (isLiveQuest) onRetry?.(); };
  const historicalRoots = useRootConversations({ questRef, foreground: null, checks: [], stale: false }, !isLiveQuest);
  const roots = isLiveQuest ? rootConversations : historicalRoots;
  const [historicalConversation, setHistoricalConversation] = useState(false);
  const cycles = data?.cycles ?? [];
  const foreground = snapshot.research_control.foreground;
  const sessions = roots?.sessions ?? [];
  const sessionsUnavailable = Boolean(roots?.error || roots?.data?.limited || roots?.context.stale);
  const stage = spectrumStage(foreground?.stage);
  const motion = useResearchMotion<HTMLElement>(stage !== null && currentStageStatus(stage, snapshot, rootConversations).state === "running");
  const summaries = useTimelineSummaries(questRef, motion.visible);
  // A limited history can still contain this exact completed run. Only use
  // positive matched rows, and do not infer from a stale/failed Quest read.
  const summarySessions = !roots?.error && !roots?.context.stale
    && roots?.data?.quest_ref === questRef ? sessions : [];
  const groups: { key: string; questionRef: string; ordinal: number; revisit: boolean; cycles: OverviewCycle[] }[] = [];
  const questionOrdinals = new Map<string, number>();
  for (const cycle of cycles) {
    const previous = groups.at(-1);
    if (previous?.questionRef === cycle.question_ref) { previous.cycles.push(cycle); continue; }
    const revisit = questionOrdinals.has(cycle.question_ref);
    if (!revisit) questionOrdinals.set(cycle.question_ref, questionOrdinals.size + 1);
    groups.push({ key: cycle.cycle_ref, questionRef: cycle.question_ref, ordinal: questionOrdinals.get(cycle.question_ref)!, revisit, cycles: [cycle] });
  }
  const questionItems = new Map(history.questions.map(item => [String(item.question_ref), item]));
  for (const item of history.questions) {
    const questionRef = String(item.question_ref);
    if (questionOrdinals.has(questionRef)) continue;
    questionOrdinals.set(questionRef, questionOrdinals.size + 1);
    groups.push({ key: questionRef, questionRef, ordinal: questionOrdinals.get(questionRef)!, revisit: false, cycles: [] });
  }
  const [selection, setSelection] = useState<StageSelection | null>(null);
  const treeRef = useRef<HTMLDivElement>(null);
  const [returnRequest, setReturnRequest] = useState(0);
  const returnedRequest = useRef(0);
  useEffect(() => {
    // Only an explicit return moves the reader; live updates retain their place.
    if (!isLiveQuest || !returnRequest || returnedRequest.current === returnRequest || !data) return;
    returnedRequest.current = returnRequest;
    const tree = treeRef.current;
    tree?.querySelectorAll<HTMLDetailsElement>('[data-current="true"] > details').forEach(details => { details.open = true; });
    requestAnimationFrame(() => {
    const current = tree?.querySelector<HTMLElement>('.research-timeline-cycle[data-current="true"]');
    if (tree && current) { tree.scrollTop += current.getBoundingClientRect().top - tree.getBoundingClientRect().top - 12; tree.focus({ preventScroll: true }); }
    });
  }, [isLiveQuest, returnRequest, data]);
  useEffect(() => { setSelection(null); setHistoricalConversation(false); }, [questRef]);
  const observedControl = rootConversations?.context.foreground ?? foreground;
  const paused = ["paused", "suspended"].includes(observedControl?.status ?? "") || observedControl?.grant_status === "suspended";
  const sourcesObservedAt = summaries.observedAt > 0 ? new Date(summaries.observedAt * 1_000).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }) : null;
  return <section className="research-timeline" aria-label="研究时间线" ref={motion.ref} data-motion-active={motion.active}>
    <header className="research-timeline-heading"><div><h2>研究时间线</h2><details className="research-timeline-note"><summary>摘要说明</summary><p>摘要按扫描资料独立整理；过程摘要、待确认发现与科研结论分别标明。阶段状态与正式成果请查看详情。</p></details>{sourcesObservedAt ? <p>资料最近扫描开始于 {sourcesObservedAt}</p> : null}</div><span>{groups.length ? `${questionOrdinals.size} 个问题 · ${cycles.length} 轮探索` : "随研究更新"}</span></header>
    <div className="research-timeline-browse"><label>浏览 Quest<select value={questRef ?? ""} onChange={event => { setBrowsingQuestRef(event.target.value === liveQuestRef ? null : event.target.value); }}>
      {questRef && !history.quests.some(item => item.quest_ref === questRef) ? <option value={questRef}>{isLiveQuest ? snapshot.research_space.current_quest.goal || questRef : questRef}</option> : null}
      {history.quests.map(item => <option key={item.quest_ref} value={item.quest_ref}>{item.goal || item.quest_ref}{item.quest_ref === liveQuestRef ? " · 当前工作" : ""}</option>)}
    </select></label><button type="button" className="overview-text-button" onClick={() => { setBrowsingQuestRef(null); setReturnRequest(value => value + 1); }}>返回当前工作</button></div>
    {history.questLoading || history.questionsLoading ? <p className="overview-empty" role="status">正在读取{history.questLoading ? "Quest 列表" : "全部正式问题"}…</p> : null}
    {history.questError || history.questionsError ? <p className="overview-empty" role="status">{history.questError ? "Quest 列表" : "问题历史"}读取失败，已显示的记录仍可阅读；列表可能不完整。<button className="overview-text-button" onClick={retryHistory}>重新读取</button></p> : null}
    {readError && data ? <p className="overview-empty" role="status">时间线更新暂不可用，保留已读取记录。<button className="overview-text-button" onClick={retryHistory}>重新读取</button></p> : null}
    {sessionsUnavailable ? <p className="overview-empty" role="status">部分工作会话暂不可确认，以下保留已读取记录。</p> : null}
    {summaries.error ? <p className="timeline-summary-availability" role="status">总结读取暂不可用，保留已读取内容；稍后自动重试。</p> : null}
    {!data && !readError ? <p className="overview-empty" role="status">正在读取研究时间线…</p>
      : !data ? <p className="overview-empty">研究时间线暂不可用。<button className="overview-text-button" onClick={retryHistory}>重新读取</button></p>
      : !groups.length ? <p className="overview-empty">{history.questionsLoading ? "正在读取正式问题…" : history.questionsError ? "问题历史暂不可确认。" : "尚无正式问题或轮次记录。"}</p>
      : <div className="research-timeline-scroll" ref={treeRef} tabIndex={0} aria-label="按时间排列的研究记录"><ol key={questRef} className="research-timeline-questions">{groups.map(group => {
        const active = isLiveQuest && group.cycles.some(cycle => cycle.cycle_ref === foreground?.cycle_ref);
        const questionItem = questionItems.get(group.questionRef);
        const lifecycle = typeof questionItem?.status === "string" ? questionItem.status : null;
        const lifecycleLabel = lifecycle ? ({ retired: "已退役", superseded: "已由新问题接续", completed: "已完成", closed: "已关闭" } as Record<string, string>)[lifecycle] ?? lifecycle : null;
        const questionKey = `question:${group.questionRef}`;
        return <li key={group.key} className="research-timeline-question" data-question-ref={group.questionRef} data-current={active}>
          <BoundedDetails className="research-timeline-question-details" defaultOpen={active || groups.length === 1 || !isLiveQuest} summary={<>
            <span className="research-timeline-question-name">Question {group.ordinal}<i className="research-timeline-chevron" aria-hidden="true">▸</i></span>
            <span className="research-timeline-question-copy">{questionItem?.name ? <span className="research-timeline-question-title">{questionItem.name}{lifecycle && lifecycle !== "active" ? <small>{lifecycleLabel}</small> : null}{!group.cycles.length ? <small>尚无 Cycle</small> : null}</span> : null}<TimelineSummary className="research-timeline-question-summary" nodeKey={questionKey} node={summaries.nodes[questionKey]} unavailable={summaries.error} /></span>
            {active || group.revisit ? <i className="research-timeline-badge">{active ? "当前问题" : "继续研究"}{active && group.revisit ? " · 再次进入" : ""}</i> : null}
          </>}>{() => group.cycles.length ? <ol className="research-timeline-cycles">{group.cycles.map(cycle => {
        const isCurrent = isLiveQuest && cycle.cycle_ref === foreground?.cycle_ref;
        const entries = stages.map(stage => {
          const artifacts = [...(cycle.stages[stage] ?? [])].sort((a, b) => a.epoch - b.epoch);
          return { stage, artifacts, latest: artifacts.at(-1) ?? null };
        });
        const targets = timelineTargets(snapshot, cycle, sessions, Object.values(summaries.nodes), sessionsUnavailable, isCurrent && paused);
        return <li key={cycle.cycle_ref} className="research-timeline-cycle" data-cycle-ref={cycle.cycle_ref} data-current={isCurrent}>
          <BoundedDetails className="research-timeline-cycle-details" defaultOpen={isCurrent} summary={<>
            <span className="research-timeline-cycle-name">{isCurrent ? <MetaTrace variant="timeline" /> : null}Cycle {cycle.ordinal ?? "?"}<i className="research-timeline-chevron" aria-hidden="true">▸</i></span>
            <TimelineSummary className="research-timeline-cycle-meta" nodeKey={`cycle:${cycle.cycle_ref}`} node={summaries.nodes[`cycle:${cycle.cycle_ref}`]} unavailable={summaries.error}
              acceptedResultNeedsReview={summaryNeedsAcceptedResultReview(summaries.nodes[`cycle:${cycle.cycle_ref}`], cycle, entries.flatMap(entry => entry.artifacts), summarySessions, summaries.observedAt)} />
            {isCurrent ? <i className="research-timeline-badge">当前轮</i> : null}
          </>}>{() => <ol className="research-timeline-stages">{entries.map(({ stage, artifacts, latest }) => <li key={stage} className="research-timeline-stage" data-stage={stage} style={{ "--stage-accent": stageAccents[stage] } as CSSProperties}>
              <button type="button" className="research-timeline-stage-open" aria-label={`查看${stageNames[stage]}结果与历史`} onClick={() => setSelection({ cycleRef: cycle.cycle_ref, stage, epoch: latest?.epoch ?? null })}>
                <span className="research-timeline-stage-head"><b>{stageTechnicalNames[stage]}</b></span>
                <TimelineSummary className="research-timeline-stage-summary" nodeKey={`stage:${cycle.cycle_ref}:${stage}`} node={summaries.nodes[`stage:${cycle.cycle_ref}:${stage}`]} unavailable={summaries.error}
                  acceptedResultNeedsReview={summaryNeedsAcceptedResultReview(summaries.nodes[`stage:${cycle.cycle_ref}:${stage}`], cycle, artifacts, summarySessions, summaries.observedAt)} />
                <small className="research-timeline-stage-state">{isCurrent && foreground?.stage.toLowerCase() === stage ? <><span>当前阶段</span><span>{currentStageStatus(stage, snapshot, rootConversations).label}</span></> : latest ? acceptedStatusNames[latest.status] : "尚无记录"}</small>
              </button>
              {stage === "bundle" && (targets.length > 0 || isCurrent) ? <ul className="research-timeline-targets">
                {targets.map(target => <li key={target.ref} className="research-timeline-target" data-target-ref={target.ref} data-executing={target.executing}>
                  <button className="research-timeline-target-open" type="button" onClick={() => {
                    if (!target.session || !roots) {
                      setSelection({ cycleRef: cycle.cycle_ref, stage: "bundle", epoch: latest?.epoch ?? null });
                      return;
                    }
                    roots.selectStage("bundle", target.session.session_ref);
                    if (!isLiveQuest) { setHistoricalConversation(true); return; }
                    requestAnimationFrame(() => {
                      const activity = document.getElementById("research-activity");
                      activity?.focus({ preventScroll: true });
                      activity?.scrollIntoView({ block: "start", behavior: motion.active ? "smooth" : "auto" });
                    });
                  }}><b>{target.label}</b><TimelineSummary className="research-timeline-target-summary" nodeKey={`target:${cycle.cycle_ref}:${target.ref}`} node={summaries.nodes[`target:${cycle.cycle_ref}:${target.ref}`]} unavailable={summaries.error} /><small className="research-timeline-target-state">{target.status}</small></button>
                </li>)}
                {!targets.length ? <li className="research-timeline-target"><span className="research-timeline-target-summary">{sessionsUnavailable ? "Target 工作记录暂不可确认" : "尚无可展示的 Target 工作记录"}</span></li> : null}
              </ul> : null}
            </li>)}</ol>}</BoundedDetails>
        </li>;
      })}</ol> : <p className="overview-readable-text">{questionItem?.summary || "正式问题已建立，尚未进入 Cycle。"}</p>}</BoundedDetails></li>;
      })}</ol></div>}
    {selection && data && <StageResultDialog data={data} selection={selection} onSelect={setSelection} onClose={() => setSelection(null)} />}
    {historicalConversation && !isLiveQuest ? <OverviewDialog title="历史工作会话" subtitle="只读浏览 · 当前研究继续执行" onClose={() => setHistoricalConversation(false)}><RootConversations model={historicalRoots} connected={true} /></OverviewDialog> : null}
  </section>;
}
