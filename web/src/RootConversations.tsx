import { useCallback, useEffect, useRef, useState } from "react";
import { StageReadableOutput } from "./ReadableOutput";
import { ExecutionElapsed, type ExecutionClockSample } from "./ExecutionElapsed";
import { fetchRootOutput, fetchRootSessions, RootSessionError, type RootOperation, type RootOutput, type RootSession, type RootSessions } from "./rootSessionsApi";
import { spectrumStage, spectrumStages, type SpectrumStage } from "./Spectrum";
import { BoundedDetails, PageWindow } from "./BoundedDetails";
import { useOutputLanguage } from "./OutputLanguage";
import "./root-conversations.css";

const statusLabels = { executing: "正在执行", waiting: "等待继续", completed: "已完成", failed: "执行受阻", paused: "已暂停", pending: "等待启动" };
const reasoningRoles = ["reasoning", "deepfetch", "acquisition"] as const;
type ReasoningRole = typeof reasoningRoles[number];
const reasoningRoleLabels = { reasoning: "Reasoning", deepfetch: "DeepFetch", acquisition: "Acquisition" };
const reasoningRole = (session: RootSession): ReasoningRole => session.kind === "stage" ? "reasoning" : session.kind === "deepfetch" ? "deepfetch" : "acquisition";

const kindLabels = { stage: "阶段会话", target: "实验会话", deepfetch: "DeepFetch", acquisition: "Acquisition" };
const isHistoricalWaitingSession = (session: RootSession) => session.is_current === false && session.is_executing === false && session.status === "waiting";
export const rootSessionStatus = (session: RootSession, language: "zh" | "en" = "zh") => isHistoricalWaitingSession(session)
  ? language === "en" ? "Historical session" : "历史会话"
  : session.activity_label || statusLabels[session.status];
const timeText = (value: number | null) => value ? new Date(value * 1_000).toLocaleString("zh-CN", { hour12: false }) : "时间待确认";

export type RootConversationContext = {
  questRef?: string | null;
  foreground: { quest_ref: string; cycle_ref: string; question_ref: string; stage: string; status?: string; grant_status?: string } | null;
  checks: readonly { name: string; status: string; reason?: { code?: string } | null }[];
  stale: boolean;
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
  const visible = usePageVisible();
  const [result, setResult] = useState<{ questRef: string; data: RootSessions } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const [selection, setSelection] = useState<{ questRef: string; cycleRef: string | null; stage: SpectrumStage; ref: string | null; role?: ReasoningRole } | null>(null);
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
        setResult({ questRef, data }); setError(null); failures = 0;
      } catch (caught) {
        if (controller.signal.aborted) return;
        failures += 1;
        setError(caught instanceof Error ? caught.message : "root_sessions_unavailable");
      }
      if (!controller.signal.aborted) timer = window.setTimeout(load, Math.min(10_000, 2_500 * 2 ** failures));
    };
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [questRef, active, visible, retry]);
  const data = result?.questRef === questRef ? result.data : null;
  const sessions = data?.sessions ?? [];
  const cycleRef = context.foreground?.cycle_ref ?? null;
  const currentStage = spectrumStage(context.foreground?.stage);
  const chosen = selection?.questRef === questRef && selection.cycleRef === cycleRef ? selection : null;
  const selectedStage = chosen?.stage ?? currentStage;
  const reasoningDeepFetch = (session: RootSession) => {
    if (session.creation_context_kind !== "autonomous_question_creation" || spectrumStage(session.stage) !== "reasoning"
      || !session.cycle_ref || !session.owner_session_ref) return false;
    const owner = sessions.find(item => item.session_ref === session.owner_session_ref);
    return owner?.kind === "stage" && spectrumStage(owner.stage) === "reasoning" && owner.cycle_ref === session.cycle_ref
      && (!session.question_ref || !owner.question_ref || owner.question_ref === session.question_ref);
  };
  const forStage = (stage: SpectrumStage, historical = false) => sessions.filter(session => {
    const belongs = session.kind === "acquisition" || (session.kind === "deepfetch" ? stage === "reasoning" && reasoningDeepFetch(session)
      : session.kind === "target" ? stage === "bundle" : spectrumStage(session.stage) === stage);
    const isHistory = Boolean(session.cycle_ref && session.cycle_ref !== cycleRef);
    return belongs && isHistory === historical;
  }).sort((a, b) => {
    const rank = (session: RootSession) => ({ stage: 0, target: 1, deepfetch: 1, acquisition: 2 }[session.kind]);
    return rank(a) - rank(b) || (a.short_title || a.title).localeCompare(b.short_title || b.title, undefined, { numeric: true })
      || (a.created_at ?? 0) - (b.created_at ?? 0) || a.session_ref.localeCompare(b.session_ref);
  });
  const defaultRoot = (stage: SpectrumStage | null) => {
    if (!stage) return null;
    const candidates = forStage(stage);
    const own = candidates.filter(item => item.kind === "stage").sort((a, b) =>
      Number(b.is_current === true) - Number(a.is_current === true) || Number(b.is_executing) - Number(a.is_executing)
      || (b.created_at ?? 0) - (a.created_at ?? 0) || b.session_ref.localeCompare(a.session_ref));
    if (stage === "reasoning") return own[0] ?? null;
    return own[0] ?? candidates.find(item => item.is_executing) ?? candidates[0] ?? null;
  };
  const current = defaultRoot(currentStage);
  const roleRoot = (role: ReasoningRole) => role === "reasoning" ? defaultRoot("reasoning") : forStage("reasoning").find(item => reasoningRole(item) === role) ?? null;
  const selectedRole = selectedStage === "reasoning" ? chosen?.role ?? "reasoning" : null;
  const pendingRoleRoot = chosen?.role && !chosen.ref ? roleRoot(chosen.role) : null;
  const selected = chosen?.ref ? sessions.find(item => item.session_ref === chosen.ref) ?? null
    : selectedRole ? roleRoot(selectedRole) : defaultRoot(selectedStage);
  // A pending role has no invented session identity. Bind it once its real root appears,
  // then keep that exact root through subsequent index refreshes and new roots.
  useEffect(() => {
    if (!pendingRoleRoot || !chosen) return;
    setSelection(previous => previous === chosen ? { ...previous, ref: pendingRoleRoot.session_ref } : previous);
  }, [chosen, pendingRoleRoot?.session_ref]);
  const selectStage = (stage: SpectrumStage, ref: string | null = null) => setSelection(questRef ? { questRef, cycleRef, stage, ref } : null);
  const selectReasoningRole = (role: ReasoningRole) => setSelection(questRef ? { questRef, cycleRef, stage: "reasoning", ref: null, role } : null);
  const returnCurrent = () => setSelection(null);
  return { context, questRef, data, sessions, error, refresh, current, selected, selectedStage, currentStage, selectedRole,
    selectedRef: chosen?.ref ?? null, forStage, selectStage, selectReasoningRole, returnCurrent, active: active && visible };

}

export type RootConversationsModel = ReturnType<typeof useRootConversations>;
type Model = RootConversationsModel;

export function StageRootSessions({ model, stage }: { model: Model; stage: SpectrumStage }) {
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
      onClick={() => model.selectStage(stage, session.session_ref)}>
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
          aria-label={`${reasoningRoleLabels[role]} · ${roleStatus}`} onClick={() => model.selectReasoningRole(role)}>
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

export function RootConversations({ model, connected, polling = false }: { model: Model; connected: boolean; polling?: boolean }) {
  const foreground = model.context.foreground;
  const failure = model.context.checks.find(check => check.status === "unavailable" && (
    check.name === `${foreground?.stage.toLowerCase()}_stage_worker`
    || (foreground?.stage.toLowerCase() === "bundle" && check.name === "target_run_worker")
  ));
  return <section className="research-flow root-conversations" id="research-activity" tabIndex={-1} aria-labelledby="research-trace-title">
    <header className="research-flow-heading"><h2 id="research-trace-title">研究过程</h2>
      {(model.selectedStage !== model.currentStage || model.selected?.session_ref !== model.current?.session_ref || model.selectedRole && model.selectedRole !== "reasoning") ? <button className="root-return-current" onClick={model.returnCurrent}>返回当前阶段 ↗</button> : null}
      <span className="research-connection" data-connected={connected || polling}><i />{model.context.stale ? "状态待确认 · 保留记录" : connected ? "实时连接" : polling ? "定时更新" : "连接中断 · 保留记录"}</span>
    </header>
    {failure ? <div className="lumen-idea-health-blocker" data-testid="research-current-worker-blocker" role="status"><span aria-hidden="true">!</span><div><b>{failure.name === "target_run_worker" ? "Target 启动受阻" : "当前阶段推进受阻"}</b><small>已形成的会话与研究记录仍可查看。</small></div><code title={failure.reason?.code}>{failure.reason?.code ?? "worker_unavailable"}</code></div> : null}
    {model.error || model.data?.limited ? <div className="root-session-warning" role="status">{model.data ? "会话状态更新不完整，保留已读取记录。" : "暂时无法读取研究会话。"}<button onClick={model.refresh}>重新读取</button>{model.error ? <details><summary>状态详情</summary><code>{model.error}</code></details> : null}</div> : null}
    {model.selected && model.questRef ? <RootSessionTimeline key={model.selected.session_ref} questRef={model.questRef} session={model.selected} scope={sessionScope(model.selected, model.context)} active={model.active} stale={Boolean(model.context.stale || model.error || model.data?.limited)} />
      : model.selectedStage === "reasoning" && model.selectedRole && !model.selectedRef ? <section className="research-conversation root-pending-role" aria-label={`${reasoningRoleLabels[model.selectedRole]} 待启动会话`} data-root-role={model.selectedRole}>
        <h3>{reasoningRoleLabels[model.selectedRole]}</h3>
        <p>{!model.data || model.error || model.data.limited ? "正在确认此入口的根会话状态。" : model.selectedRole === "deepfetch" ? "Reasoning 尚未建立由本阶段发起的 DeepFetch 根会话。" : model.selectedRole === "reasoning" ? "当前轮尚未建立 Reasoning 根会话。" : "此 Quest 尚未建立共享 Acquisition 根会话。"}</p>
        <small>{model.data && !model.error && !model.data.limited ? "待启动 · 建立后会自动显示该根会话的对话流。" : "确认后会显示对应的真实会话。"}</small>
      </section> : <p className="research-output-empty">{model.selectedRef ? "所选根会话暂不可读取，正在重新获取；也可在上方选择其他会话。" : model.data ? "所选阶段尚未建立根会话。产生后会自动显示；可在上方切换阶段。" : "正在读取研究会话…"}</p>}
  </section>;
}

function RootSessionTimeline({ questRef, session, scope, active, stale }: { questRef: string; session: RootSession; scope: string; active: boolean; stale: boolean }) {
  const { language } = useOutputLanguage();
  const [historyEnd, setHistoryEnd] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const operations = [...session.operations].sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0) || a.operation_ref.localeCompare(b.operation_ref));
  const end = Math.min(historyEnd ?? operations.length, operations.length);
  const start = Math.max(0, end - 4);
  const onOutput = useCallback(() => {
    if (follow) requestAnimationFrame(() => { const node = scrollRef.current; if (node) node.scrollTop = node.scrollHeight; });
  }, [follow]);
  useEffect(onOutput, [onOutput, operations.length]);
  return <section className="research-conversation root-session-conversation" aria-label={`${session.title} 会话`} data-session-ref={session.session_ref}>
    <header className="root-session-heading"><div><b>{session.title}</b><small>{scope} · {kindLabels[session.kind]}{session.stage ? ` · ${session.stage}` : ""}</small></div><span data-executing={!stale && session.is_executing}>{stale ? `状态待确认 · 上次${rootSessionStatus(session, language)}` : rootSessionStatus(session, language)}</span></header>
    <div className="root-session-context"><span>{stale ? "当前会话状态暂不可确认，以下为已读取的工作记录。" : isHistoricalWaitingSession(session) ? language === "en" ? "View recorded content." : "查看已记录内容。" : session.status === "waiting" ? "当前会话等待继续条件，暂无模型调用在执行。" : session.status === "completed" ? "本会话已完成，以下保留已产生的公开工作记录。" : "同一会话的连续工作记录"}</span><details><summary>归属</summary><p>会话 {session.session_ref}</p><p>研究轮次 {session.cycle_ref ?? "Quest 资料范围"}</p><p>研究问题 {session.question_ref ?? "未绑定单个问题"}</p>{session.owner_session_ref ? <p>发起会话 {session.owner_session_ref}</p> : null}</details></div>
    <div className="root-session-scroll" ref={scrollRef} role="log" aria-live="off" aria-label={`${session.title} 连续工作记录`} onScroll={event => {
      const node = event.currentTarget;
      if (follow && node.scrollHeight - node.clientHeight - node.scrollTop > 48) { setFollow(false); setHistoryEnd(end); }
    }}>
      {start > 0 ? <button className="root-load-earlier" onClick={() => { setFollow(false); setHistoryEnd(start); }}>读取更早工作 · 还有 {start} 次</button> : null}
      {operations.slice(start, end).map((operation, index) => <RootOperationOutput key={operation.operation_ref} questRef={questRef} sessionRef={session.session_ref}
        operation={operation} ordinal={start + index + 1} active={active} onOutput={onOutput} />)}
      {end < operations.length ? <button className="root-load-earlier" onClick={() => setHistoryEnd(Math.min(operations.length, end + 4))}>读取后续工作 · 还有 {operations.length - end} 次</button> : null}
      {!operations.length ? <p className="research-output-empty">会话已建立，等待第一条公开工作记录。</p> : null}
    </div>
    <footer className="research-output-footer"><button type="button" aria-pressed={follow} onClick={() => { setFollow(value => !value); if (follow) setHistoryEnd(end); else { setHistoryEnd(null); requestAnimationFrame(() => { const node = scrollRef.current; if (node) node.scrollTop = node.scrollHeight; }); } }}>{follow ? "跟随最新 ✓" : "继续跟随 ↓"}</button><span>{follow ? "默认显示最近 4 次工作；原始历史可分页读取" : "已暂停滚动，可以继续回看"}</span></footer>
  </section>;
}

function RootOperationOutput({ questRef, sessionRef, operation, ordinal, active, onOutput }: { questRef: string; sessionRef: string; operation: RootOperation; ordinal: number; active: boolean; onOutput: () => void }) {
  const [page, setPage] = useState<RootOutput | null>(null);
  const [chunks, setChunks] = useState<{ offset: number; text: string }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [clock, setClock] = useState<ExecutionClockSample | null>(null);
  const [retry, setRetry] = useState(0);
  const [browseOffset, setBrowseOffset] = useState<number | null>(null);
  const last = useRef<RootOutput | null>(null);
  const tailOffset = useRef(0);
  const chunksRef = useRef<{ offset: number; text: string }[]>([]);
  const browse = (offset: number | null) => {
    if (browseOffset === null) tailOffset.current = last.current?.offset ?? 0;
    if (offset === null || offset !== last.current?.next_offset) { chunksRef.current = []; setChunks([]); }
    last.current = null; setPage(null);
    setBrowseOffset(offset); setRetry(value => value + 1);
  };
  useEffect(() => {
    if (!active) return;
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
        const collected = [...(sameStream ? chunksRef.current.filter(chunk => chunk.offset < next.offset) : []), ...(next.text ? [{ offset: next.offset, text: next.text }] : [])];
        // Bound browser memory per call while retaining adjacent UTF-8 pages.
        let bytes = collected.reduce((sum, chunk) => sum + new TextEncoder().encode(chunk.text).length, 0);
        while (bytes > 512 * 1024 && collected.length > 1) bytes -= new TextEncoder().encode(collected.shift()!.text).length;
        chunksRef.current = collected; last.current = next;
        setChunks(collected); setPage(next); setError(null); failures = 0;
        if (next.source_updated_at && Number.isFinite(next.observed_at)) setClock(previousClock => previousClock?.sourceUpdatedAt === next.source_updated_at ? previousClock : { sourceUpdatedAt: next.source_updated_at!, observedAt: next.observed_at, receivedAt: performance.now() });
        unchangedReads = next.text ? 0 : unchangedReads + 1;
        if (browseOffset === null && (next.has_more || next.status !== "terminal")) timer = window.setTimeout(load, next.has_more ? 30 : Math.min(8_000, 1_000 * 2 ** Math.min(unchangedReads, 3)));
      } catch (caught) {
        if (controller.signal.aborted) return;
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
  }, [questRef, sessionRef, operation.operation_ref, operation.status, active, retry, browseOffset]);
  useEffect(onOutput, [page?.next_offset, page?.stream_ref, onOutput]);
  return <article className="root-operation" data-operation-ref={operation.operation_ref}>
    <div className="root-operation-time"><span>工作记录 {ordinal}{operation.label ? ` · ${operation.label}` : ""}</span><time>{timeText(operation.created_at)}</time></div>
    {error ? <div className="root-session-warning" role="status">记录暂时无法更新，已保留上次内容。<button onClick={() => setRetry(value => value + 1)}>重试</button><details><summary>读取详情</summary><code>{error}</code></details></div> : null}
    {page ? <>{(chunks[0]?.offset ?? 0) > 0 && browseOffset === null ? <p className="root-output-truncated">这次调用较早的记录已收起，可通过分页回看。</p> : null}
      <StageReadableOutput rawText={page.text} chunks={chunks} streamKey={page.stream_ref} rootNativeSessionRef={page.native_session_ref} isTerminal={page.status === "terminal"} publicOnly />
      <div className="root-operation-clock"><ExecutionElapsed sample={clock} ended={page.status === "terminal"} />{page.has_more && browseOffset === null ? <small>正在补读后续记录…</small> : null}</div>
      <details className="root-output-pages" open={browseOffset !== null}><summary>查看这次调用的分页记录</summary><nav aria-label={`工作记录 ${ordinal} 分页`}>
        <button disabled={page.offset === 0} onClick={() => browse(0)}>最早记录</button>
        <button disabled={page.offset === 0} onClick={() => browse(Math.max(0, page.offset - 65536))}>上一页</button>
        <span>{page.offset}–{page.next_offset} / {page.source_bytes} 字节</span>
        <button disabled={!page.has_more} onClick={() => browse(page.next_offset)}>下一页</button>
        {browseOffset !== null ? <button onClick={() => browse(null)}>返回最新记录</button> : null}
      </nav></details></>
      : <p className="research-output-empty">正在读取公开工作记录…</p>}
  </article>;
}
