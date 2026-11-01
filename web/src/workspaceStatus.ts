import { useEffect, useState } from "react";
import { readStatus, type RuntimeStatus } from "./StatusHome";
import type { PublicSnapshot } from "./api";
import type { RootConversationContext } from "./RootConversations";

// Conversation observations can load while the full accepted-result projection
// is being verified. This state never supplies capabilities to Owner commands.
export function useWorkspaceStatus(active: boolean) {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!active) return;
    let stopped = false;
    let running = false;
    let failures = 0;
    let timer: number | undefined;
    const load = async () => {
      if (stopped || running) return;
      running = true;
      window.clearTimeout(timer);
      try {
        const next = await readStatus();
        if (next.foreground && [next.foreground.quest_ref, next.foreground.cycle_ref,
          next.foreground.question_ref, next.foreground.stage].some(value => typeof value !== "string" || !value)) {
          throw new Error("workspace_status_scope_invalid");
        }
        if (stopped) return;
        setStatus(current => current && current.revision_domain === next.revision_domain
          && current.revision > next.revision ? current : next);
        setError(false);
        failures = 0;
      } catch {
        if (!stopped) { setError(true); failures += 1; }
      } finally {
        running = false;
        if (!stopped) timer = window.setTimeout(load, failures ? Math.min(5_000 * 2 ** Math.min(failures - 1, 3), 30_000) : 5_000);
      }
    };
    const onVisible = () => { if (!document.hidden) void load(); };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", onVisible);
    void load();
    return () => {
      stopped = true; window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", onVisible);
    };
  }, [active]);
  return { status, error };
}

export function conversationContext(snapshot: PublicSnapshot | null, status: RuntimeStatus | null, error: boolean): RootConversationContext {
  // A newly observed data instance has its own revision baseline. Never let a
  // larger counter from the previous instance select its old conversation.
  const useStatus = status && (!snapshot || status.revision_domain !== snapshot.revision_domain || status.revision > snapshot.revision
    || status.revision === snapshot.revision && Date.parse(status.observed_at) >= Date.parse(snapshot.observed_at ?? ""));
  return useStatus
    ? { foreground: status.foreground, checks: status.health.checks, stale: error }
    : { foreground: snapshot?.research_control.foreground ?? null, checks: snapshot?.readiness.checks ?? [], stale: !snapshot };
}
