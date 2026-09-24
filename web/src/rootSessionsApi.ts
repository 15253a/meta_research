export type RootSessionStatus = "executing" | "waiting" | "completed" | "failed" | "paused" | "pending";
export type RootOperation = {
  operation_ref: string; label: string; phase: string | null; status: RootSessionStatus;
  created_at: number | null; updated_at: number | null;
};
export type RootSession = {
  session_ref: string; root_session_ref: string; kind: "stage" | "target" | "deepfetch" | "acquisition";
  title: string; stage: string | null; related_stages: string[]; scope_label: string;
  short_title?: string | null;
  activity_label?: string;
  creation_context_kind?: "quest_initialization" | "manual_question_creation" | "autonomous_question_creation";
  creation_context_ref?: string | null;
  owner_session_ref: string | null; status: RootSessionStatus; is_executing: boolean; is_current: boolean | null;
  run_ref: string | null; target_ref: string | null; cycle_ref: string | null; question_ref: string | null;
  created_at: number | null; updated_at: number | null; operations: RootOperation[];
};
export type RootSessions = {
  schema_ref: "meta-research/root-sessions/v1"; quest_ref: string; observed_at: number;
  sessions: RootSession[]; active_session_refs: string[]; limited: boolean; reasons: unknown[];
};
export type RootOutput = {
  schema_ref: "meta-research/root-session-output/v1"; quest_ref: string; session_ref: string; operation_ref: string;
  stream_ref: string; text: string; offset: number; next_offset: number; source_bytes: number;
  has_more: boolean; source_caught_up: boolean; source_updated_at: number | null; observed_at: number;
  native_session_ref: string | null; status: "live" | "terminal" | "waiting";
};

export class RootSessionError extends Error {
  constructor(readonly code: string) { super(code); }
}

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const deadline = new AbortController();
  const abort = () => deadline.abort();
  signal.addEventListener("abort", abort, { once: true });
  const timer = window.setTimeout(abort, 8_000);
  try {
    if (signal.aborted) deadline.abort();
    const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" }, signal: deadline.signal });
    if (!response.ok) {
      const body = await response.json().catch(() => null) as { detail?: { code?: string } } | null;
      throw new RootSessionError(body?.detail?.code ?? `root_session_unavailable:${response.status}`);
    }
    return await response.json() as T;
  } catch (error) {
    if (deadline.signal.aborted && !signal.aborted) throw new Error("root_session_timeout");
    throw error;
  } finally {
    clearTimeout(timer);
    signal.removeEventListener("abort", abort);
  }
}

export async function fetchRootSessions(questRef: string, signal: AbortSignal): Promise<RootSessions> {
  const data = await read<RootSessions>(`/api/v1/quests/${encodeURIComponent(questRef)}/root-sessions`, signal);
  if (data.schema_ref !== "meta-research/root-sessions/v1" || data.quest_ref !== questRef
    || !Array.isArray(data.sessions) || !Array.isArray(data.active_session_refs)
    || data.sessions.some(session => !session.session_ref || !["stage", "target", "deepfetch", "acquisition"].includes(session.kind)
      || !Array.isArray(session.operations))) throw new Error("root_sessions_identity_invalid");
  return data;
}

export async function fetchRootOutput(questRef: string, sessionRef: string, operationRef: string, after: number, signal: AbortSignal): Promise<RootOutput> {
  const query = new URLSearchParams({ operation_ref: operationRef, after: String(after), limit: "65536" });
  const data = await read<RootOutput>(`/api/v1/quests/${encodeURIComponent(questRef)}/root-sessions/${encodeURIComponent(sessionRef)}/output?${query}`, signal);
  if (data.schema_ref !== "meta-research/root-session-output/v1" || data.quest_ref !== questRef || data.session_ref !== sessionRef
    || data.operation_ref !== operationRef || typeof data.stream_ref !== "string" || typeof data.text !== "string"
    || data.offset !== after || !Number.isSafeInteger(data.next_offset) || !Number.isSafeInteger(data.source_bytes)
    || data.next_offset - data.offset !== new TextEncoder().encode(data.text).length
    || data.source_bytes < data.next_offset || data.has_more !== (data.next_offset < data.source_bytes)
    || data.source_caught_up !== !data.has_more || !["live", "terminal", "waiting"].includes(data.status)) {
    throw new Error("root_session_output_identity_invalid");
  }
  return data;
}
