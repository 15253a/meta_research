import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { fetchSnapshot, SnapshotReadError, type PublicSnapshot } from "./api";
import type { RuntimeStatus } from "./StatusHome";

type Sections = { includeAssets: boolean; includeHistory: boolean };

// One scheduler owns event coalescing, stale-cut catch-up and failed reads.
// Delivery is acknowledged after React commits the current scope, never by HTTP.
export function useProjectionRecovery(snapshot: PublicSnapshot | null,
  setSnapshot: Dispatch<SetStateAction<PublicSnapshot | null>>, status: RuntimeStatus | null,
  sections: Sections) {
  const domain = status?.revision_domain ?? snapshot?.revision_domain ?? "";
  const scope = JSON.stringify([domain, status?.foreground?.quest_ref, status?.foreground?.cycle_ref,
    status?.foreground?.question_ref, status?.foreground?.stage, sections.includeAssets, sections.includeHistory]);
  const latest = useRef({ scope, domain, sections });
  latest.current = { scope, domain, sections };
  const target = useRef({ domain, revision: 0 });
  if (target.current.domain !== domain) target.current = { domain, revision: 0 };
  if (status && (!status.revision_domain || status.revision_domain === domain)) {
    target.current.revision = Math.max(target.current.revision, status.revision);
  }
  const [delivered, setDelivered] = useState<{ scope: string; value: PublicSnapshot } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updating, setUpdating] = useState(true);
  const [cursor, setCursor] = useState<number | null>(null);
  const cursorDomain = useRef(domain);
  const acknowledged = useRef({ scope: "", revision: -1 });
  const request = useRef<(() => Promise<void>) | null>(null);
  const schedule = useRef<(delay: number) => void>(() => undefined);
  const reload = useCallback(() => request.current?.() ?? Promise.resolve(), []);
  const observe = useCallback((revision?: number) => {
    if (revision !== undefined && Number.isSafeInteger(revision)) {
      target.current.revision = Math.max(target.current.revision, revision);
    }
    setUpdating(true);
    schedule.current(2_000);
  }, []);

  useEffect(() => {
    const lifecycle = new AbortController();
    let inFlight: Promise<void> | null = null;
    let timer: number | undefined;
    let due = Infinity;
    let failures = 0;
    let retryAt = 0;
    let queued = false;
    setUpdating(true);
    setError(null);
    if (cursorDomain.current !== domain) { cursorDomain.current = domain; setCursor(null); }
    setSnapshot(current => current && domain && current.revision_domain
      && current.revision_domain !== domain ? null : current);
    const later = (delay: number) => {
      if (lifecycle.signal.aborted) return;
      const next = Math.max(Date.now() + delay, retryAt);
      if (timer !== undefined && due <= next) return;
      window.clearTimeout(timer);
      due = next;
      timer = window.setTimeout(() => { timer = undefined; due = Infinity; void read(); }, Math.max(0, next - Date.now()));
    };
    const read = (): Promise<void> => {
      if (lifecycle.signal.aborted) return Promise.resolve();
      if (inFlight) { queued = true; return inFlight; }
      if (Date.now() < retryAt) { later(retryAt - Date.now()); return Promise.resolve(); }
      window.clearTimeout(timer); timer = undefined; due = Infinity;
      const captured = latest.current;
      const flight = (async () => {
        try {
          const next = await fetchSnapshot(lifecycle.signal, captured.sections);
          if (lifecycle.signal.aborted || latest.current.scope !== captured.scope) return;
          if (captured.domain && next.revision_domain && next.revision_domain !== captured.domain) {
            later(2_000); return;
          }
          failures = 0; retryAt = 0; setError(null);
          target.current.revision = Math.max(target.current.revision, next.revision);
          setSnapshot(current => current && current.revision_domain === next.revision_domain
            && current.revision > next.revision ? current : next);
          setDelivered({ scope: captured.scope, value: next });
          setCursor(current => Math.max(current ?? 0, next.revision));
        } catch (caught) {
          if (lifecycle.signal.aborted) return;
          failures += 1;
          setError("暂时无法更新详情，将自动重试");
          setUpdating(true);
          const backoff = Math.min(2_000 * 2 ** Math.min(failures - 1, 4), 30_000);
          retryAt = Date.now() + Math.max(backoff, caught instanceof SnapshotReadError ? caught.retryAfterMs : 0);
        } finally {
          inFlight = null;
          if (!lifecycle.signal.aborted) {
            const behind = acknowledged.current.scope !== scope || acknowledged.current.revision < target.current.revision;
            later(failures ? Math.max(0, retryAt - Date.now()) : behind || queued ? 2_000 : 30_000);
            queued = false;
          }
        }
      })();
      inFlight = flight;
      return flight;
    };
    request.current = read;
    schedule.current = later;
    const wake = () => { if (!document.hidden) { setUpdating(true); void read(); } };
    window.addEventListener("online", wake);
    document.addEventListener("visibilitychange", wake);
    void read();
    return () => {
      lifecycle.abort(); window.clearTimeout(timer);
      window.removeEventListener("online", wake);
      document.removeEventListener("visibilitychange", wake);
      if (request.current === read) request.current = null;
    };
  }, [scope, setSnapshot]);

  useEffect(() => {
    if (!delivered || delivered.scope !== scope || delivered.value !== snapshot) return;
    const foreground = snapshot.research_control.foreground;
    const expected = status?.foreground;
    const matchesScope = !status || (foreground?.quest_ref === expected?.quest_ref
      && foreground?.cycle_ref === expected?.cycle_ref && foreground?.question_ref === expected?.question_ref
      && foreground?.stage.toLowerCase() === expected?.stage.toLowerCase());
    const stage = foreground?.stage.toLowerCase();
    const stageData = { idea: snapshot.idea_stage, plan: snapshot.plan_stage,
      bundle: snapshot.bundle_stage, reasoning: snapshot.reasoning_stage };
    const failedStage = stage && stage in stageData && !stageData[stage as keyof typeof stageData]
      && snapshot.query_warnings?.some(warning => warning.section === `${stage}_stage`);
    const loaded = matchesScope && !failedStage && (!sections.includeAssets || snapshot.research_assets.loaded !== false)
      && (!sections.includeHistory || (snapshot.question_tree.loaded !== false
        && snapshot.question_tree.status === "ready" && snapshot.human_collaboration?.companion.loaded !== false));
    if (loaded) acknowledged.current = { scope, revision: snapshot.revision };
    const behind = !loaded || snapshot.revision < target.current.revision;
    setUpdating(behind);
    if (behind) schedule.current(2_000);
  }, [delivered, snapshot, scope, sections.includeAssets, sections.includeHistory]);

  const health = JSON.stringify(status?.health);
  useEffect(() => { observe(status?.revision); }, [status?.revision, health, observe]);
  return { reload, observe, error, updating, cursor, domain };
}
