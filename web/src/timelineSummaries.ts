import { useEffect, useState } from "react";
import type { OverviewCycle, OverviewStageArtifact } from "./ResearchOverview";
import type { RootSession } from "./rootSessionsApi";

export type TimelineSummaryNode = {
  node_key: string;
  kind: "question" | "cycle" | "stage" | "target";
  question_ref: string;
  cycle_ref: string | null;
  stage: string | null;
  target_ref: string | null;
  summary: string | null;
  summary_kind?: "process" | "tentative_finding" | "accepted_conclusion" | "insufficient_evidence";
  status: "pending" | "updating" | "ready" | "failed";
  source_hash: string | null;
  summarized_source_hash: string | null;
  updated_at: number | null;
  sources: Array<{ ref: string; label?: string }>;
};

export type TimelineSummaries = {
  schema_ref: "meta-research/timeline-summaries/v1";
  quest_ref: string;
  revision: number;
  observed_at: number;
  nodes: TimelineSummaryNode[];
};

export function summaryNeedsAcceptedResultReview(node: TimelineSummaryNode | undefined, cycle: OverviewCycle,
  artifacts: OverviewStageArtifact[], sessions: RootSession[], sourcesObservedAt: number): boolean {
  if (!node?.summary?.trim() || !Number.isFinite(sourcesObservedAt) || sourcesObservedAt <= 0
    || node.cycle_ref !== cycle.cycle_ref || node.question_ref !== cycle.question_ref
    || (node.kind !== "cycle" && node.kind !== "stage")) return false;
  // observed_at marks the start of this Quest's source scan, not its WAL cut.
  // A later completed run warrants checking the saved prose against the
  // accepted result; it does not prove that the scan omitted that result.
  return artifacts.some(artifact => artifact.status === "accepted" && artifact.source.commit_ref
    && artifact.source.run_ref && (node.kind !== "stage" || node.stage === artifact.stage)
    && sessions.some(session => session.kind === "stage" && session.status === "completed" && !session.is_executing
      && session.run_ref === artifact.source.run_ref && session.stage === artifact.stage
      && session.cycle_ref === cycle.cycle_ref && session.question_ref === cycle.question_ref
      && typeof session.updated_at === "number" && Number.isFinite(session.updated_at)
      && session.updated_at > sourcesObservedAt));
}

const nullableText = (value: unknown) => value === null || typeof value === "string";

// Near-current and tree regions read the same recorder cut. Share only an
// in-flight request; each visible region retains its own display and polling.
const pendingReads = new Map<string, Promise<TimelineSummaries>>();
function readTimelineSummaries(questRef: string): Promise<TimelineSummaries> {
  const pending = pendingReads.get(questRef);
  if (pending) return pending;
  const controller = new AbortController();
  const deadline = window.setTimeout(() => controller.abort(), 8_000);
  const request = (async () => {
    const response = await fetch(`/api/v1/quests/${encodeURIComponent(questRef)}/timeline-summaries`, {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    });
    if (!response.ok) throw new Error("timeline_summaries_unavailable");
    return await response.json() as TimelineSummaries;
  })().finally(() => { window.clearTimeout(deadline); pendingReads.delete(questRef); });
  pendingReads.set(questRef, request);
  return request;
}

function validNode(node: TimelineSummaryNode): boolean {
  if (!node || typeof node.node_key !== "string" || typeof node.question_ref !== "string"
    || !["pending", "updating", "ready", "failed"].includes(node.status)
    || ![node.cycle_ref, node.stage, node.target_ref, node.summary, node.source_hash, node.summarized_source_hash].every(nullableText)
    || !(node.updated_at === null || typeof node.updated_at === "number" && Number.isFinite(node.updated_at))
    || !Array.isArray(node.sources) || node.sources.some(source => !source || typeof source.ref !== "string"
      || source.label !== undefined && typeof source.label !== "string")
    || node.summary_kind !== undefined && !["process", "tentative_finding", "accepted_conclusion", "insufficient_evidence"].includes(node.summary_kind)) return false;
  const key = node.kind === "question" ? `question:${node.question_ref}`
    : node.kind === "cycle" && node.cycle_ref ? `cycle:${node.cycle_ref}`
      : node.kind === "stage" && node.cycle_ref && node.stage ? `stage:${node.cycle_ref}:${node.stage}`
        : node.kind === "target" && node.cycle_ref && node.target_ref ? `target:${node.cycle_ref}:${node.target_ref}` : null;
  return key !== null && key === node.node_key;
}

/** The recorder writes independently; this interface only reads its saved summaries. */
export function useTimelineSummaries(questRef: string | null, active = true) {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [result, setResult] = useState<{
    questRef: string | null;
    revision: number;
    observedAt: number;
    nodes: Record<string, TimelineSummaryNode>;
    error: boolean;
  }>({ questRef: null, revision: -1, observedAt: 0, nodes: {}, error: false });
  useEffect(() => {
    const changed = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!questRef || !visible || !active) return;
    let stopped = false;
    let timer: number | undefined;
    const read = async () => {
      try {
        const data = await readTimelineSummaries(questRef);
        if (data.schema_ref !== "meta-research/timeline-summaries/v1" || data.quest_ref !== questRef
          || !Number.isSafeInteger(data.revision) || data.revision < 0 || !Number.isFinite(data.observed_at)
          || !Array.isArray(data.nodes) || data.nodes.some(node => !validNode(node))
          || new Set(data.nodes.map(node => node.node_key)).size !== data.nodes.length) throw new Error("timeline_summaries_identity_invalid");
        if (stopped) return;
        setResult(previous => {
          const sameQuest = previous.questRef === questRef;
          if (sameQuest && previous.revision > data.revision) return previous;
          const nodes = sameQuest ? { ...previous.nodes } : {};
          for (const node of data.nodes) {
            const saved = nodes[node.node_key];
            // A new generation or a transient failure never erases the last saved sentence.
            nodes[node.node_key] = !node.summary?.trim() && saved?.summary?.trim()
              ? { ...node, summary: saved.summary, summarized_source_hash: saved.summarized_source_hash,
                summary_kind: saved.summary_kind, updated_at: saved.updated_at, sources: saved.sources }
              : node;
          }
          return { questRef, revision: data.revision, observedAt: data.observed_at, nodes, error: false };
        });
      } catch {
        if (!stopped) setResult(previous => previous.questRef === questRef ? { ...previous, error: true }
          : { questRef, revision: -1, observedAt: 0, nodes: {}, error: true });
      } finally {
        if (!stopped) timer = window.setTimeout(read, 5_000);
      }
    };
    void read();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [questRef, visible, active]);
  return result.questRef === questRef ? result : { questRef, revision: -1, observedAt: 0, nodes: {}, error: false };
}
