import type { RuntimeStatus } from "./StatusHome";
import type { RootSession, RootSessions } from "./rootSessionsApi";

export const TARGET_OBSERVATION_INTERVAL_MS = 30_000;
const OBSERVATION_MAX_AGE_MS = 45_000;

export function canObserveActiveTarget(status: RuntimeStatus | null): boolean {
  return Boolean(status && status.state === "waiting" && status.current_task?.kind === "stage"
    && status.foreground?.stage === "bundle" && status.foreground.status === "active"
    && status.health.status === "ready" && status.health.checks.every(check => check.status === "ready"));
}

// A live execution observation supplements only the ambiguous Bundle wait.
// It never supplies execution authority or replaces a failure/pause indication.
export function observedActiveTarget(status: RuntimeStatus | null, roots: RootSessions | null, now = Date.now()): {
  status: RuntimeStatus; target: RootSession; observedAt: string;
} | null {
  if (!canObserveActiveTarget(status) || !status?.foreground || !roots || roots.limited !== false
    || roots.quest_ref !== status.foreground.quest_ref || !Number.isFinite(roots.observed_at)) return null;
  const observedMs = roots.observed_at * 1_000;
  if (observedMs > now + 5_000 || now - observedMs > OBSERVATION_MAX_AGE_MS) return null;
  const foreground = status.foreground;
  const target = roots.sessions.filter(session => session.kind === "target" && session.stage === "bundle"
    && session.cycle_ref === foreground.cycle_ref && session.question_ref === foreground.question_ref
    && session.is_current === true && session.is_executing === true && session.status === "executing"
    && session.target_ref && session.run_ref && roots.active_session_refs.includes(session.session_ref))
    .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || a.session_ref.localeCompare(b.session_ref))[0];
  if (!target) return null;
  return {
    status: { ...status, state: "running", waiting_reason: null, current_task: {
      kind: "target", title: target.title, run_ref: target.run_ref, target_ref: target.target_ref, status: "running",
    } },
    target, observedAt: new Date(observedMs).toISOString(),
  };
}
