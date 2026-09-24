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
    let timer: number | undefined;
    const load = async () => {
      try {
        const next = await readStatus();
        if (next.foreground && [next.foreground.quest_ref, next.foreground.cycle_ref,
          next.foreground.question_ref, next.foreground.stage].some(value => typeof value !== "string" || !value)) {
          throw new Error("workspace_status_scope_invalid");
        }
        if (stopped) return;
        setStatus(current => current && current.revision > next.revision ? current : next);
        setError(false);
      } catch {
        if (!stopped) setError(true);
      }
      if (!stopped) timer = window.setTimeout(load, 5_000);
    };
    void load();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [active]);
  return { status, error };
}

export function conversationContext(snapshot: PublicSnapshot | null, status: RuntimeStatus | null, error: boolean): RootConversationContext {
  const useStatus = status && (!snapshot || status.revision > snapshot.revision
    || status.revision === snapshot.revision && Date.parse(status.observed_at) >= Date.parse(snapshot.observed_at ?? ""));
  return useStatus
    ? { foreground: status.foreground, checks: status.health.checks, stale: error }
    : { foreground: snapshot?.research_control.foreground ?? null, checks: snapshot?.readiness.checks ?? [], stale: !snapshot };
}
