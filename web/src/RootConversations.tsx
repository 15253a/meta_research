import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { StageReadableOutput } from "./ReadableOutput";
import { ExecutionElapsed, type ExecutionClockSample } from "./ExecutionElapsed";
import { fetchRootOutput, fetchRootSessions, RootSessionError, type RootOperation, type RootOutput, type RootSession, type RootSessions } from "./rootSessionsApi";
import { spectrumStage, spectrumStages, type SpectrumStage } from "./Spectrum";
import { BoundedDetails, PageWindow } from "./BoundedDetails";
import { useOutputLanguage } from "./OutputLanguage";
import type { TargetRetryObservation } from "./targetResearchFacts";
import "./root-conversations.css";

const statusLabels = { executing: "正在执行", waiting: "等待继续", completed: "已完成", failed: "执行受阻", paused: "已暂停", pending: "等待启动" };
const reasoningRoles = ["reasoning", "deepfetch", "acquisition"] as const;
type ReasoningRole = typeof reasoningRoles[number];
const reasoningRoleLabels = { reasoning: "Reasoning", deepfetch: "DeepFetch", acquisition: "Acquisition" };
const reasoningRole = (session: RootSession): ReasoningRole => session.kind === "stage" ? "reasoning" : session.kind === "deepfetch" ? "deepfetch" : "acquisition";

const kindLabels = { stage: "阶段会话", target: "独立 Target 根", deepfetch: "DeepFetch", acquisition: "Acquisition" };
const sessionStage = (session: RootSession): SpectrumStage | null => session.kind === "target" ? "bundle" : spectrumStage(session.stage);
const childStatusLabels = { starting: "正在启动", running: "正在运行", completed: "已完成", failed: "已失败", cancelled: "已取消", unknown: "未知 · 待核实" };
const isHistoricalWaitingSession = (session: RootSession) => session.is_current === false && session.is_executing === false && session.status === "waiting";
export const rootSessionStatus = (session: RootSession, language: "zh" | "en" = "zh") => isHistoricalWaitingSession(session)
  ? language === "en" ? "Historical session" : "历史会话"
  : session.activity_label || statusLabels[session.status];
const timeText = (value: number | null) => value ? new Date(value * 1_000).toLocaleString("zh-CN", { hour12: false }) : "时间待确认";

function RootOutputTimestamp({ sourceUpdatedAt }: { sourceUpdatedAt: number | null | undefined }) {
  return typeof sourceUpdatedAt === "number" && Number.isFinite(sourceUpdatedAt) && sourceUpdatedAt > 0
    ? <time>输出更新 · {timeText(sourceUpdatedAt)}</time>
    : <span>输出时间待确认</span>;
}

export type RootConversationContext = {
  questRef?: string | null;
  revisionDomain?: string;
  foreground: { quest_ref: string; cycle_ref: string; question_ref: string; stage: string; status?: string; grant_status?: string } | null;
  checks: readonly { name: string; status: string; reason?: { code?: string } | null }[];
  stale: boolean;
};

type OperationReadingState = {
  page: RootOutput | null; chunks: { offset: number; text: string }[]; browseOffset: number | null;
  tailOffset: number; clock: ExecutionClockSample | null; visibleCount: number;
};
type SessionReadingState = {
  historyEnd: number | null; follow: boolean; scrollTop: number | null; operations: Map<string, OperationReadingState>; detailsOpen: boolean;
};

function usePageVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const change = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  return visible;
}

export function useRootConversations(context: RootConversationContext, active: boolean) {
  const questRef = context.foreground?.quest_ref ?? context.questRef ?? null;
  const scope = JSON.stringify([context.revisionDomain, questRef]);
  const visible = usePageVisible();
  const [result, setResult] = useState<{ scope: string; data: RootSessions } | null>(null);
  const [failure, setFailure] = useState<{ scope: string; message: string } | null>(null);
  const error = failure?.scope === scope ? failure.message : null;
  const [retry, setRetry] = useState(0);
  const [selection, setSelection] = useState<{ scope: string; cycleRef: string | null; stage: SpectrumStage | null; ref: string | null; role?: ReasoningRole;
    timelineKind?: "stage" | "target"; questionRef?: string; targetRef?: string; runRef?: string | null } | null>(null);
  const retainedSelection = useRef<{ scope: string; session: RootSession } | null>(null);
  const readingCache = useMemo(() => new Map<string, SessionReadingState>(), [scope]);
  const refresh = useCallback(() => setRetry(value => value + 1), []);
  useEffect(() => {
    if (!active || !visible || !questRef) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let failures = 0;
    const load = async () => {
      try {
        const data = await fetchRootSessions(questRef, controller.signal);
        if (controller.signal.aborted) return;
        setResult({ scope, data }); setFailure(null); failures = 0;
      } catch (caught) {
        if (controller.signal.aborted) return;
        failures += 1;
        setFailure({ scope, message: caught instanceof Error ? caught.message : "root_sessions_unavailable" });
      }
      if (!controller.signal.aborted) timer = window.setTimeout(load, Math.min(10_000, 2_500 * 2 ** failures));
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [questRef, scope, active, visible, retry]);
  const data = result?.scope === scope ? result.data : null;
  const sessions = data?.sessions ?? [];
  const cycleRef = context.foreground?.cycle_ref ?? null;
  const currentStage = spectrumStage(context.foreground?.stage);
  const chosen = selection?.scope === scope ? selection : null;
  const reasoningDeepFetch = (session: RootSession) => {
    if (session.creation_context_kind !== "autonomous_question_creation" || spectrumStage(session.stage) !== "reasoning"
      || !session.cycle_ref || !session.owner_session_ref) return false;
    const owner = sessions.find(item => item.session_ref === session.owner_session_ref);
    return owner?.kind === "stage" && spectrumStage(owner.stage) === "reasoning" && owner.cycle_ref === session.cycle_ref
      && (!session.question_ref || !owner.question_ref || owner.question_ref === session.question_ref);
  };
  const forStage = (stage: SpectrumStage, historical = false, selectedCycle = cycleRef) => sessions.filter(session => {
    const belongs = session.kind === "acquisition" || (session.kind === "deepfetch" ? stage === "reasoning" && reasoningDeepFetch(session)
      : session.kind === "target" ? stage === "bundle" : spectrumStage(session.stage) === stage);
    const isHistory = Boolean(session.cycle_ref && session.cycle_ref !== selectedCycle);
    return belongs && isHistory === historical;
  }).sort((a, b) => {
    const rank = (session: RootSession) => ({ stage: 0, target: 1, deepfetch: 1, acquisition: 2 }[session.kind]);
    return rank(a) - rank(b) || (a.short_title || a.title).localeCompare(b.short_title || b.title, undefined, { numeric: true })
      || (a.created_at ?? 0) - (b.created_at ?? 0) || a.session_ref.localeCompare(b.session_ref);
  });
  const defaultRoot = (stage: SpectrumStage | null, selectedCycle = cycleRef) => {
    if (!stage) return null;
    const candidates = forStage(stage, false, selectedCycle);
    const own = candidates.filter(item => item.kind === "stage").sort((a, b) =>
      Number(b.is_current === true) - Number(a.is_current === true) || Number(b.is_executing) - Number(a.is_executing)
      || (b.created_at ?? 0) - (a.created_at ?? 0) || b.session_ref.localeCompare(a.session_ref));
    if (stage === "reasoning") return own[0] ?? null;
    return own[0] ?? candidates.find(item => item.is_executing) ?? candidates[0] ?? null;
  };
  const executing = sessions.filter(session => session.is_executing).sort((a, b) =>
    Number(Boolean(cycleRef) && b.cycle_ref === cycleRef) - Number(Boolean(cycleRef) && a.cycle_ref === cycleRef)
    || Number(Boolean(currentStage) && sessionStage(b) === currentStage) - Number(Boolean(currentStage) && sessionStage(a) === currentStage)
    || Number(b.kind === "stage") - Number(a.kind === "stage")
    || (a.created_at ?? 0) - (b.created_at ?? 0) || a.session_ref.localeCompare(b.session_ref));
  const current = executing[0]
    ?? defaultRoot(currentStage);
  const selectedStage = chosen?.stage ?? (current ? sessionStage(current) : null) ?? currentStage;
  const roleRoot = (role: ReasoningRole, selectedCycle = cycleRef) => role === "reasoning" ? defaultRoot("reasoning", selectedCycle) : forStage("reasoning", false, selectedCycle).find(item => reasoningRole(item) === role) ?? null;
  const selectedRole = selectedStage === "reasoning" ? chosen?.role ?? "reasoning" : null;
  const timelineCandidates = chosen?.timelineKind ? sessions.filter(session => session.cycle_ref === chosen.cycleRef
    && (!session.question_ref || session.question_ref === chosen.questionRef)
    && (chosen.timelineKind === "target" ? session.kind === "target" && session.target_ref === chosen.targetRef
      && (!chosen.runRef || session.run_ref === chosen.runRef) : session.kind === "stage" && spectrumStage(session.stage) === chosen.stage))
    .sort((a, b) => Number(b.is_current === true) - Number(a.is_current === true)
      || Number(b.is_executing) - Number(a.is_executing) || (b.created_at ?? 0) - (a.created_at ?? 0)
      || b.session_ref.localeCompare(a.session_ref)) : [];
  // A timeline click binds its exact work, including an empty stage or Target.
  // Never substitute the foreground's current root for that selection.
  const observedSelection = chosen?.timelineKind ? (chosen.ref ? timelineCandidates.find(item => item.session_ref === chosen.ref) : timelineCandidates[0]) ?? null
    : chosen?.ref ? sessions.find(item => item.session_ref === chosen.ref) ?? null
    : !chosen ? current : selectedRole ? roleRoot(selectedRole, chosen.cycleRef) : defaultRoot(selectedStage, chosen.cycleRef);
  const pendingRoot = chosen && !chosen.ref ? observedSelection : null;
  const selected = observedSelection ?? (chosen?.ref && retainedSelection.current?.scope === scope
    && retainedSelection.current.session.session_ref === chosen.ref ? retainedSelection.current.session : null);
  const selectedUnavailable = Boolean(chosen?.ref && !observedSelection);
  useEffect(() => {
    if (questRef && chosen?.ref && observedSelection) retainedSelection.current = { scope, session: observedSelection };
  }, [questRef, scope, chosen?.ref, observedSelection]);
  // A pending role has no invented session identity. Bind it once its real root appears,
  // then keep that exact root through subsequent index refreshes and new roots.
  useEffect(() => {
    if (!pendingRoot || !chosen) return;
    setSelection(previous => previous === chosen ? { ...previous, ref: pendingRoot.session_ref } : previous);
  }, [chosen, pendingRoot?.session_ref]);
  const selectStage = (stage: SpectrumStage, ref: string | null = null) => setSelection(questRef ? { scope, cycleRef, stage, ref: ref ?? defaultRoot(stage)?.session_ref ?? null } : null);
  const selectTimelineStage = (stage: SpectrumStage, selectedCycle: string, questionRef: string) => setSelection(questRef
    ? { scope, cycleRef: selectedCycle, questionRef, stage, ref: null, timelineKind: "stage" } : null);
  const selectTimelineTarget = (targetRef: string, selectedCycle: string, questionRef: string, ref: string | null = null, runRef: string | null = null) => setSelection(questRef
    ? { scope, cycleRef: selectedCycle, questionRef, stage: "bundle", ref, targetRef, runRef, timelineKind: "target" } : null);
  const selectReasoningRole = (role: ReasoningRole) => setSelection(questRef ? { scope, cycleRef, stage: "reasoning", ref: null, role } : null);
  const returnCurrent = () => setSelection(null);
  const pinReading = () => {
    if (!chosen && questRef && selected) setSelection({ scope, cycleRef: selected.cycle_ref, stage: selectedStage, ref: selected.session_ref });
  };
  return { context, questRef, scope, data, sessions, error, refresh, current, selected, selectedStage, currentStage, selectedRole,
    selectedRef: chosen?.ref ?? null, selectedCycleRef: chosen?.cycleRef ?? selected?.cycle_ref ?? null,
    selectedTimelineKind: chosen?.timelineKind ?? null, selectedTargetRef: chosen?.targetRef ?? selected?.target_ref ?? null,
    selectedUnavailable, manualSelection: Boolean(chosen), readingCache, pinReading, forStage, selectStage, selectTimelineStage, selectTimelineTarget, selectReasoningRole, returnCurrent, active: active && visible };

}

export type RootConversationsModel = ReturnType<typeof useRootConversations>;
type Model = RootConversationsModel;

export function focusResearchOutput(): void {
  requestAnimationFrame(() => {
    const output = document.getElementById("research-activity");
    output?.focus({ preventScroll: true });
    output?.scrollIntoView({ block: "start" });
  });
}

export function StageRootSessions({ model, stage, onOpenOutput }: { model: Model; stage: SpectrumStage; onOpenOutput?: () => void }) {
  const { language } = useOutputLanguage();
  const sessions = model.forStage(stage), historical = model.forStage(stage, true);
  const stageName = spectrumStages.find(item => item.id === stage)!.name;
  const choices = (items: RootSession[], history = false) => items.map(session => {
    const label = session.short_title || (session.kind === "stage" ? stageName : kindLabels[session.kind]);
    const sameLabel = (history ? historical : sessions).filter(item => (item.short_title || (item.kind === "stage" ? stageName : kindLabels[item.kind])) === label);
    const ordinal = sameLabel.length > 1 ? ` ${sameLabel.findIndex(item => item.session_ref === session.session_ref) + 1}` : "";
    const scope = sessionScope(session, model.context);
    return <button type="button" key={session.session_ref} data-session-ref={session.session_ref}
      data-executing={!model.error && !model.data?.limited && session.is_executing}
      aria-pressed={model.selectedStage === stage && model.selected?.session_ref === session.session_ref}
      aria-label={`${label}${ordinal} · ${scope} · ${model.error ? "上次记录" : rootSessionStatus(session, language)}`}
      title={`${session.title} · ${scope} · ${rootSessionStatus(session, language)}`}
      onClick={() => { model.selectStage(stage, session.session_ref); onOpenOutput?.(); }}>
      <span>{label}{ordinal}</span><small>{session.kind === "acquisition" ? "共享" : session.kind === "deepfetch" && !session.cycle_ref ? scope : history ? "历史" : ""}</small>
    </button>;
  });
  const missingRoles = stage === "reasoning" ? reasoningRoles.filter(role => !sessions.some(session => reasoningRole(session) === role)) : [];
  const uncertain = Boolean(model.error || model.data?.limited);
  const roleStatus = !model.data ? uncertain ? "状态待确认" : "正在读取" : uncertain ? "状态待确认" : "待启动";
  return <div className="stage-root-sessions">
    <p className="stage-root-count">{stage === "reasoning" ? `3 个根 session 入口 · ${model.data ? `${sessions.length} 个已建立 · ${uncertain ? "其余状态待确认" : `${missingRoles.length} 个待启动`}` : roleStatus}`
      : model.data ? `${sessions.length} 个根 session${uncertain ? " · 上次记录" : ""}` : model.error ? "会话暂不可用" : "正在读取会话…"}</p>
    <div className="stage-root-links" role="group" aria-label={`${stageName} 本轮根会话`}>
      {stage === "reasoning" ? reasoningRoles.map(role => {
        const actual = sessions.filter(session => reasoningRole(session) === role);
        return actual.length ? choices(actual) : <button type="button" key={`pending-${role}`} data-root-role={role}
          aria-pressed={model.selectedStage === "reasoning" && model.selectedRole === role && !model.selected && !model.selectedRef}
          aria-label={`${reasoningRoleLabels[role]} · ${roleStatus}`} onClick={() => { model.selectReasoningRole(role); onOpenOutput?.(); }}>
          <span>{reasoningRoleLabels[role]}</span><small>{roleStatus}</small>
        </button>;
      }) : <PageWindow items={sessions} label={`${stageName} 本轮会话分页`} render={session => choices([session])} />}
    </div>
    {stage !== "reasoning" && model.data && !sessions.length ? <small className="stage-root-empty">尚未建立</small> : null}
    {historical.length ? <BoundedDetails className="stage-root-history" summary={`历史会话 · ${historical.length}`}>{() => <div className="stage-root-links" role="group" aria-label={`${stageName} 历史根会话`}><PageWindow items={[...historical].sort((a, b) => (b.created_at ?? 0) - (a.created_at ?? 0))} label={`${stageName} 历史会话分页`} render={session => choices([session], true)} /></div>}</BoundedDetails> : null}
  </div>;
}

function sessionScope(session: RootSession, context: RootConversationContext) {
  const foreground = context.foreground;
  if (session.cycle_ref && session.cycle_ref === foreground?.cycle_ref) return "当前轮";
  if (session.cycle_ref) return "历史轮次";
  return session.scope_label || "Quest 资料会话";
}

export function RootConversations({ model, connected, polling = false, targetRetry, onHide, onReturnCurrent }: {
  model: Model; connected: boolean; polling?: boolean; targetRetry?: TargetRetryObservation | null; onHide?: () => void; onReturnCurrent?: () => void;
}) {
  const foreground = model.context.foreground;
  const failure = model.context.checks.find(check => check.status === "unavailable" && (
    check.name === `${foreground?.stage.toLowerCase()}_stage_worker`
    || (foreground?.stage.toLowerCase() === "bundle" && check.name === "target_run_worker")
  ));
  const usageLimit = failure?.reason?.code === "codex_usage_limit";
  const retrySummary = failure && targetRetry?.checkName === failure.name && targetRetry.reasonCode === failure.reason?.code ? targetRetry.summary : null;
  const choices = model.selected && !model.sessions.some(session => session.session_ref === model.selected?.session_ref)
    ? [...model.sessions, model.selected] : model.sessions;
  const select = (session: RootSession) => model.selectStage(sessionStage(session) ?? model.selectedStage ?? model.currentStage ?? "idea", session.session_ref);
  const targets = model.forStage("bundle").filter(session => session.kind === "target");
  const statusKnown = !model.context.stale && !model.error && !model.data?.limited;
  return <section className="research-flow root-conversations" id="research-activity" tabIndex={-1} aria-labelledby="research-trace-title">
    <header className="research-flow-heading"><div className="research-flow-title"><h2 id="research-trace-title">研究过程</h2>
      <span className="research-connection" data-connected={connected || polling}><i />{model.context.stale ? "状态待确认 · 保留记录" : connected ? "实时连接" : polling ? "定时更新" : "连接中断 · 保留记录"}</span></div>
      {onHide ? <button type="button" className="research-panel-hide" aria-label="隐藏研究过程" aria-controls="research-output-panel" onClick={onHide}>−</button> : null}
    </header>
    <div className="root-session-controls" tabIndex={0} aria-label="会话选择与状态">
    <div className="root-view-toolbar">
      <label>查看会话<select aria-label="选择查看的会话" value={model.selected?.session_ref ?? ""} onChange={event => {
        const session = choices.find(item => item.session_ref === event.target.value);
        if (session) select(session);
      }}>
        {!model.selected ? <option value="">{model.data ? "暂无会话" : "正在读取…"}</option> : null}
        {choices.map(session => {
          const name = session.short_title || session.title;
          const sameName = choices.filter(choice => (choice.short_title || choice.title) === name);
          return <option key={session.session_ref} value={session.session_ref}>{name}{sameName.length > 1 ? ` ${sameName.findIndex(choice => choice.session_ref === session.session_ref) + 1}` : ""} · {sessionScope(session, model.context)} · {rootSessionStatus(session)}</option>;
        })}
      </select></label>
      <div className="root-follow-controls"><span>{model.manualSelection ? "正在回看" : statusKnown && model.current?.is_executing ? "跟随当前执行" : "当前工作"}</span>
        {model.manualSelection ? <button className="root-return-current" onClick={() => { model.returnCurrent(); onReturnCurrent?.(); }}>返回当前执行</button> : null}
      </div>
    </div>
    {targets.length ? <nav className="root-parallel-targets" aria-label="并行 Target 会话"><span>Target</span>{targets.map(session => {
      const name = session.short_title || session.title;
      const sameName = targets.filter(target => (target.short_title || target.title) === name);
      const executing = statusKnown && session.is_executing;
      return <button key={session.session_ref} type="button" data-session-ref={session.session_ref} data-executing={executing}
        title={`${session.title} · ${statusKnown ? rootSessionStatus(session) : "状态待确认"}`}
        aria-label={`${name} · ${executing ? "正在执行" : statusKnown ? rootSessionStatus(session) : "状态待确认"}`}
        aria-pressed={model.selected?.session_ref === session.session_ref} onClick={() => select(session)}>
        {executing ? <i className="root-running-spinner" aria-hidden="true" /> : null}{name}{sameName.length > 1 ? ` · ${sameName.findIndex(target => target.session_ref === session.session_ref) + 1}` : ""}{!session.is_executing && session.status === "completed" ? " · 已完成" : ""}</button>;
    })}</nav> : null}
    {failure ? <div className="lumen-idea-health-blocker" data-testid="research-current-worker-blocker" role="status"><span aria-hidden="true">!</span><div><b>{retrySummary ?? (usageLimit ? "模型额度已用尽" : failure.name === "target_run_worker" ? "Target 推进受阻" : "当前阶段推进受阻")}</b><small>{retrySummary ? "原会话正在继续；上次错误与研究记录仍保留。" : usageLimit ? "原会话和研究记录已保留；额度恢复后可继续研究。" : "已形成的会话与研究记录仍可查看。"}</small></div><code title={failure.reason?.code}>{failure.reason?.code ?? "worker_unavailable"}</code></div> : null}
    {model.error || model.data?.limited ? <div className="root-session-warning" role="status">{model.data ? "会话状态更新不完整，保留已读取记录。" : "暂时无法读取研究会话。"}<button onClick={model.refresh}>重新读取</button>{model.error ? <details><summary>状态详情</summary><code>{model.error}</code></details> : null}</div> : null}
    {model.selectedUnavailable && model.selected ? <div className="root-session-warning" role="status">所选根会话暂不可读取，保留已读取内容和阅读位置。<button onClick={model.refresh}>重新读取所选会话</button></div> : null}
    </div>
    {model.selected && model.questRef ? <RootSessionTimeline key={`${model.scope}:${model.selected.session_ref}:${model.manualSelection ? "selected" : "current"}`} questRef={model.questRef} session={model.selected} scope={sessionScope(model.selected, model.context)} active={model.active} stale={Boolean(model.context.stale || model.error || model.data?.limited || model.selectedUnavailable)} readingCache={model.readingCache} followCurrent={!model.manualSelection} onReadHistory={model.pinReading} />
      : model.selectedTimelineKind ? <p className="research-output-empty">{model.selectedRef ? "所选会话暂不可读取。" : !model.data ? "正在读取所选工作会话…" : model.error || model.data.limited ? "所选工作会话暂不可确认，请重新读取。" : model.selectedTimelineKind === "target" ? "所选实验任务尚无会话。" : "所选轮次的此阶段尚无会话。"}</p>
      : model.selectedStage === "reasoning" && model.selectedRole && !model.selectedRef ? <section className="research-conversation root-pending-role" aria-label={`${reasoningRoleLabels[model.selectedRole]} 待启动会话`} data-root-role={model.selectedRole}>
        <h3>{reasoningRoleLabels[model.selectedRole]}</h3>
        <p>{!model.data || model.error || model.data.limited ? "正在确认此入口的根会话状态。" : model.selectedRole === "deepfetch" ? "Reasoning 尚未建立由本阶段发起的 DeepFetch 根会话。" : model.selectedRole === "reasoning" ? "当前轮尚未建立 Reasoning 根会话。" : "此 Quest 尚未建立共享 Acquisition 根会话。"}</p>
        <small>{model.data && !model.error && !model.data.limited ? "待启动 · 建立后会自动显示该根会话的对话流。" : "确认后会显示对应的真实会话。"}</small>
      </section> : <p className="research-output-empty">{model.selectedRef ? "所选根会话暂不可读取，正在重新获取；也可在上方选择其他会话。" : model.data ? "所选阶段尚未建立根会话。产生后会自动显示；可在上方切换阶段。" : "正在读取研究会话…"}</p>}
  </section>;
}

function RootSessionTimeline({ questRef, session, scope, active, stale, readingCache, followCurrent, onReadHistory }: { questRef: string; session: RootSession; scope: string; active: boolean; stale: boolean; readingCache: Map<string, SessionReadingState>; followCurrent: boolean; onReadHistory: () => void }) {
  const { language } = useOutputLanguage();
  const [reading] = useState(() => {
    const state = readingCache.get(session.session_ref) ?? { historyEnd: null, follow: true, scrollTop: null, operations: new Map<string, OperationReadingState>(), detailsOpen: false };
    if (followCurrent) {
      state.historyEnd = null; state.follow = true; state.scrollTop = null; state.detailsOpen = false;
      for (const output of state.operations.values()) { output.browseOffset = null; output.visibleCount = 80; }
    }
    readingCache.set(session.session_ref, state);
    return state;
  });
  const [historyEnd, setHistoryEnd] = useState<number | null>(reading.historyEnd);
  const [follow, setFollow] = useState(reading.follow);
  const [detailsOpen, setDetailsOpen] = useState(reading.detailsOpen);
  const [detailsHost, setDetailsHost] = useState<HTMLDivElement | null>(null);
  const [clockHost, setClockHost] = useState<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScroll = useRef<number | null>(null);
  const intentTimeout = useRef<number | null>(null);
  const readingIntent = useRef<{ top: number; pointerId?: number } | null>(null);
  const touchY = useRef<number | null>(null);
  const cancelAutoScroll = useCallback(() => {
    if (autoScroll.current !== null) cancelAnimationFrame(autoScroll.current);
    autoScroll.current = null;
  }, []);
  const clearReadingIntent = useCallback(() => {
    if (intentTimeout.current !== null) clearTimeout(intentTimeout.current);
    intentTimeout.current = null; readingIntent.current = null;
  }, []);
  const onOutput = useCallback(() => {
    cancelAutoScroll();
    if (reading.follow) autoScroll.current = requestAnimationFrame(() => {
      autoScroll.current = null;
      const node = scrollRef.current;
      if (node && reading.follow && !readingIntent.current) node.scrollTop = node.scrollHeight;
    });
  }, [reading, cancelAutoScroll]);
  const readIntent = (node: HTMLDivElement, pointerId?: number) => {
    // Passive wheel/touch handlers can run after the browser has already moved.
    const top = Math.max(node.scrollTop, reading.scrollTop ?? node.scrollTop);
    if (!reading.follow || top <= 0) return;
    cancelAutoScroll(); clearReadingIntent();
    readingIntent.current = { top, pointerId };
    // An input that never scrolls must not turn a later layout change into a pause.
    if (pointerId === undefined) intentTimeout.current = window.setTimeout(() => {
      clearReadingIntent(); onOutput();
    }, 300);
  };
  const children = (session.children ?? []).filter(child => child.parent_session_ref === session.session_ref);
  const operations = [...session.operations].sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0) || a.operation_ref.localeCompare(b.operation_ref));
  const end = Math.min(historyEnd ?? operations.length, operations.length);
  const start = Math.max(0, end - 4);
  const pauseReading = (readingEnd = end) => {
    cancelAutoScroll(); clearReadingIntent();
    reading.follow = false; reading.historyEnd = readingEnd;
    reading.scrollTop = scrollRef.current?.scrollTop ?? null;
    setFollow(false); setHistoryEnd(readingEnd); onReadHistory();
  };
  useEffect(() => {
    reading.historyEnd = historyEnd; reading.follow = follow;
    const visibleOperations = new Set(operations.slice(start, end).map(operation => operation.operation_ref));
    // Keep each remembered session bounded to the four calls in its reading window.
    for (const ref of reading.operations.keys()) if (!visibleOperations.has(ref)) reading.operations.delete(ref);
  }, [reading, historyEnd, follow, start, end, session.operations]);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (!node) return;
    if (!followCurrent && reading.scrollTop !== null) node.scrollTop = reading.scrollTop;
    return () => { reading.scrollTop = node.scrollTop; };
  }, [reading, followCurrent]);
  useEffect(() => {
    const releasePointer = (event: PointerEvent) => {
      if (readingIntent.current?.pointerId === event.pointerId) { clearReadingIntent(); onOutput(); }
    };
    const blur = () => { clearReadingIntent(); touchY.current = null; };
    document.addEventListener("pointerup", releasePointer, true);
    document.addEventListener("pointercancel", releasePointer, true);
    window.addEventListener("blur", blur);
    return () => {
      cancelAutoScroll(); clearReadingIntent();
      document.removeEventListener("pointerup", releasePointer, true);
      document.removeEventListener("pointercancel", releasePointer, true);
      window.removeEventListener("blur", blur);
    };
  }, [cancelAutoScroll, clearReadingIntent, onOutput]);
  useEffect(onOutput, [onOutput, operations.length, follow]);
  return <section className="research-conversation root-session-conversation" aria-label={`${session.title} 会话`} data-session-ref={session.session_ref}>
    <header className="root-session-heading"><div><b>{session.title}</b><small>{scope}{session.kind === "acquisition" ? " · 共享 Acquisition" : ""}</small></div><span data-executing={!stale && session.is_executing}>{stale ? `状态待确认 · 上次${rootSessionStatus(session, language)}` : rootSessionStatus(session, language)}</span></header>
    {operations.length ? <div className="root-session-live-time" ref={setClockHost} /> : null}
    {children.length ? <section className="root-delegated-children" aria-label="委派子智能体">
      <h4>委派子智能体 <small>{children.length}</small></h4>
      <PageWindow items={children} label="子智能体分页" wrap={rows => <ul>{rows}</ul>} render={child => <li key={child.session_ref} data-child-session-ref={child.session_ref} data-child-status={stale ? "unknown" : child.status}>
        <div><b>{child.label || "子智能体"}</b></div>
        <span>{stale ? `待核实 · 上次${childStatusLabels[child.status]}` : childStatusLabels[child.status]}</span>
      </li>} />
    </section> : null}
    <div className="root-session-scroll" ref={scrollRef} role="log" tabIndex={0} aria-live="off" aria-label={`${session.title} 连续工作记录`}
      onWheel={event => {
        if (!event.defaultPrevented && event.deltaY < 0) readIntent(event.currentTarget);
        else clearReadingIntent();
      }}
      onKeyDown={event => {
        if (event.defaultPrevented || event.target instanceof HTMLElement && event.target.closest("input, textarea, select, [contenteditable=true]")) return;
        if (["ArrowUp", "PageUp", "Home"].includes(event.key) || event.key === " " && event.shiftKey) readIntent(event.currentTarget);
        else clearReadingIntent();
      }}
      onPointerDown={event => {
        const node = event.currentTarget;
        const bounds = node.getBoundingClientRect();
        if (event.button === 0 && event.clientX >= bounds.left + node.clientLeft + node.clientWidth) readIntent(node, event.pointerId);
        else clearReadingIntent();
      }}
      onTouchStart={event => { touchY.current = event.touches[0]?.clientY ?? null; }}
      onTouchMove={event => {
        const nextY = event.touches[0]?.clientY;
        if (nextY !== undefined && touchY.current !== null && nextY > touchY.current) readIntent(event.currentTarget);
        touchY.current = nextY ?? null;
      }}
      onTouchEnd={() => { touchY.current = null; }}
      onTouchCancel={() => { touchY.current = null; clearReadingIntent(); }}
      onScroll={event => {
      const node = event.currentTarget;
      reading.scrollTop = node.scrollTop;
      if (follow && readingIntent.current && node.scrollTop < readingIntent.current.top && node.scrollHeight - node.clientHeight - node.scrollTop > 48) pauseReading();
    }}>
      {start > 0 ? <button className="root-load-earlier" onClick={() => pauseReading(start)}>读取更早工作 · 还有 {start} 次</button> : null}
      {operations.slice(start, end).map((operation, index) => <RootOperationOutput key={operation.operation_ref} questRef={questRef} sessionRef={session.session_ref}
        operation={operation} ordinal={start + index + 1} active={active} followLive={follow} onOutput={onOutput} readingCache={reading.operations} onReadHistory={() => pauseReading()}
        detailsHost={detailsHost} clockHost={index === end - start - 1 ? clockHost : null}
        onShowHistory={() => { reading.detailsOpen = true; setDetailsOpen(true); pauseReading(); }} />)}
      {end < operations.length ? <button className="root-load-earlier" onClick={() => setHistoryEnd(Math.min(operations.length, end + 4))}>读取后续工作 · 还有 {operations.length - end} 次</button> : null}
      {!operations.length ? <p className="research-output-empty">会话已建立，等待第一条公开工作记录。</p> : null}
    </div>
    {operations.length ? <details className="root-session-details" open={detailsOpen} onToggle={event => {
      reading.detailsOpen = event.currentTarget.open; setDetailsOpen(reading.detailsOpen); if (reading.detailsOpen) pauseReading();
    }}>
      <summary>执行详情与历史分页</summary><div ref={setDetailsHost} />
    </details> : null}
    <footer className="research-output-footer"><span>{follow ? "" : "正在回看"}</span><button type="button" aria-pressed={follow} onClick={() => { clearReadingIntent(); reading.follow = true; setFollow(true); setHistoryEnd(null); onOutput(); }}>{follow ? "跟随最新输出 ✓" : "回到最新输出 ↓"}</button></footer>
  </section>;
}

function RootOperationOutput({ questRef, sessionRef, operation, ordinal, active, followLive = true, onOutput, readingCache, onReadHistory, detailsHost, clockHost, onShowHistory }: { questRef: string; sessionRef: string; operation: RootOperation; ordinal: number; active: boolean; followLive?: boolean; onOutput: () => void; readingCache: Map<string, OperationReadingState>; onReadHistory?: () => void; detailsHost: HTMLDivElement | null; clockHost: HTMLDivElement | null; onShowHistory: () => void }) {
  const [reading] = useState(() => {
    const state = readingCache.get(operation.operation_ref) ?? { page: null, chunks: [], browseOffset: null, tailOffset: 0, clock: null, visibleCount: 80 };
    readingCache.set(operation.operation_ref, state);
    return state;
  });
  const [page, setPage] = useState<RootOutput | null>(reading.page);
  const [chunks, setChunks] = useState<{ offset: number; text: string }[]>(reading.chunks);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState<ExecutionClockSample | null>(reading.clock);
  const [retry, setRetry] = useState(0);
  const [browseOffset, setBrowseOffset] = useState<number | null>(reading.browseOffset);
  const [visibleCount, setVisibleCount] = useState(reading.visibleCount);
  const last = useRef<RootOutput | null>(reading.page);
  const tailOffset = useRef(reading.tailOffset);
  const chunksRef = useRef<{ offset: number; text: string }[]>(reading.chunks);
  const retryRequested = useRef(false);
  useEffect(() => {
    reading.page = page; reading.chunks = chunks; reading.browseOffset = browseOffset;
    reading.tailOffset = tailOffset.current; reading.clock = clock; reading.visibleCount = visibleCount;
  }, [reading, page, chunks, browseOffset, clock, visibleCount]);
  const browse = (offset: number | null) => {
    if (browseOffset === null) tailOffset.current = last.current?.offset ?? 0;
    if (offset === null || offset !== last.current?.next_offset) { chunksRef.current = []; setChunks([]); }
    last.current = null; setPage(null);
    setBrowseOffset(offset); setRetry(value => value + 1);
    reading.page = null; reading.chunks = chunksRef.current; reading.browseOffset = offset; reading.tailOffset = tailOffset.current;
    if (offset !== null) onReadHistory?.();
  };
  useEffect(() => {
    if (!active) return;
    const frozenRetry = !followLive && last.current && browseOffset === null && retryRequested.current;
    if (!followLive && last.current && browseOffset === null && !frozenRetry) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let failures = 0;
    let unchangedReads = 0;
    const readAt = async (offset: number) => {
      for (let adjustment = 0; ; adjustment += 1) {
        try { return await fetchRootOutput(questRef, sessionRef, operation.operation_ref, offset + adjustment, controller.signal); }
        catch (caught) {
          if (!(caught instanceof RootSessionError) || caught.code !== "root_session_output_cursor_invalid" || offset === 0 || adjustment >= 3) throw caught;
        }
      }
    };
    const load = async () => {
      try {
        if (frozenRetry) {
          // Check recovery without replacing the reader's retained page.
          await readAt(0);
          if (controller.signal.aborted) return;
          retryRequested.current = false; setError(null);
          return;
        }
        const previous = last.current;
        const after = browseOffset ?? previous?.next_offset ?? tailOffset.current;
        let next = await readAt(after);
        if (!previous && browseOffset === null && after === 0 && next.source_bytes > 512 * 1024) {
          next = await readAt(next.source_bytes - 256 * 1024);
        }
        if (controller.signal.aborted) return;
        if (previous && previous.stream_ref !== next.stream_ref && after !== 0) {
          last.current = null;
          tailOffset.current = 0;
          timer = window.setTimeout(load, 0);
          return;
        }
        const sameStream = !previous || previous.stream_ref === next.stream_ref;
        if (reading.page && reading.page.stream_ref !== next.stream_ref) { reading.visibleCount = 80; setVisibleCount(80); }
        const collected = [...(sameStream ? chunksRef.current.filter(chunk => chunk.offset < next.offset) : []), ...(next.text ? [{ offset: next.offset, text: next.text }] : [])];
        // Bound browser memory per call while retaining adjacent UTF-8 pages.
        let bytes = collected.reduce((sum, chunk) => sum + new TextEncoder().encode(chunk.text).length, 0);
        while (bytes > 512 * 1024 && collected.length > 1) bytes -= new TextEncoder().encode(collected.shift()!.text).length;
        chunksRef.current = collected; last.current = next;
        setChunks(collected); setPage(next); setError(null); failures = 0; retryRequested.current = false;
        if (next.source_updated_at && Number.isFinite(next.observed_at)) setClock(previousClock => previousClock?.sourceUpdatedAt === next.source_updated_at ? previousClock : { sourceUpdatedAt: next.source_updated_at!, observedAt: next.observed_at, receivedAt: performance.now() });
        unchangedReads = next.text ? 0 : unchangedReads + 1;
        if (browseOffset === null && (next.has_more || followLive && next.status !== "terminal")) timer = window.setTimeout(load, next.has_more ? 30 : Math.min(8_000, 1_000 * 2 ** Math.min(unchangedReads, 3)));
      } catch (caught) {
        if (controller.signal.aborted) return;
        retryRequested.current = false;
        if (frozenRetry) { setError(caught instanceof Error ? caught.message : "root_output_unavailable"); return; }
        if (caught instanceof RootSessionError && ["root_session_output_cursor_stale", "root_session_output_cursor_invalid"].includes(caught.code)) {
          last.current = null; tailOffset.current = 0;
          if (browseOffset !== null && browseOffset !== 0) { setBrowseOffset(0); return; }
          timer = window.setTimeout(load, 100);
          return;
        }
        failures += 1; setError(caught instanceof Error ? caught.message : "root_output_unavailable");
        timer = window.setTimeout(load, Math.min(10_000, 1_000 * 2 ** failures));
      }
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [questRef, sessionRef, operation.operation_ref, operation.status, active, followLive, retry, browseOffset]);
  useEffect(onOutput, [page?.next_offset, page?.stream_ref, onOutput]);
  return <article className="root-operation" data-operation-ref={operation.operation_ref}>
    {clockHost ? createPortal(<><RootOutputTimestamp sourceUpdatedAt={page?.source_updated_at} />
      {page ? <ExecutionElapsed sample={clock} ended={page.status === "terminal"} /> : null}</>, clockHost) : null}
    {error ? <div className="root-session-warning" role="status">{page ? "记录暂时无法更新，已保留上次内容。" : "部分记录暂时无法读取，后续内容仍可继续查看。"}<button onClick={() => { retryRequested.current = true; setRetry(value => value + 1); }}>重试</button><details><summary>读取详情</summary><code>{error}</code></details></div> : null}
    {page ? <>{(chunks[0]?.offset ?? 0) > 0 && browseOffset === null ? <button type="button" className="root-load-earlier root-output-older" onClick={onShowHistory}>前文已收起 · 查看更早内容</button> : null}
      <StageReadableOutput rawText={page.text} chunks={chunks} streamKey={page.stream_ref} rootNativeSessionRef={page.native_session_ref} isTerminal={page.status === "terminal"} publicOnly
        visibleCount={visibleCount} onVisibleCountChange={count => { reading.visibleCount = count; setVisibleCount(count); }} onPauseFollow={onReadHistory} />
      {page.has_more && browseOffset === null ? <small className="root-operation-clock">正在读取后续记录…</small> : null}
      </> : !error ? <p className="root-output-loading" role="status">正在读取公开工作记录…</p> : null}
    {detailsHost ? createPortal(<section className="root-operation-details" data-operation-details-ref={operation.operation_ref}>
      <div className="root-operation-time"><span>执行 {ordinal}{operation.label ? ` · ${operation.label}` : ""}</span><RootOutputTimestamp sourceUpdatedAt={page?.source_updated_at} /></div>
      {page ? <>{(chunks[0]?.offset ?? 0) > 0 && browseOffset === null ? <p className="root-output-truncated">较早记录可分页回看。</p> : null}
      <nav className="root-output-pages" aria-label={`执行 ${ordinal} 历史分页`}>
        <button disabled={page.offset === 0} onClick={() => browse(0)}>最早记录</button>
        <button disabled={page.offset === 0} onClick={() => browse(Math.max(0, page.offset - 65536))}>上一页</button>
        <button disabled={!page.has_more} onClick={() => browse(page.next_offset)}>下一页</button>
        {browseOffset !== null ? <button onClick={() => browse(null)}>返回最新记录</button> : null}
      </nav></> : null}
    </section>, detailsHost) : null}
  </article>;
}
