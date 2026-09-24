import { useEffect, useState } from "react";

export type TimelineSummaryNode = {
  node_key: string;
  kind: "question" | "cycle" | "stage" | "target";
  question_ref: string;
  cycle_ref: string | null;
  stage: string | null;
  target_ref: string | null;
  summary: string | null;
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

const nullableText = (value: unknown) => value === null || typeof value === "string";

function validNode(node: TimelineSummaryNode): boolean {
  if (!node || typeof node.node_key !== "string" || typeof node.question_ref !== "string"
    || !["pending", "updating", "ready", "failed"].includes(node.status)
    || ![node.cycle_ref, node.stage, node.target_ref, node.summary, node.source_hash, node.summarized_source_hash].every(nullableText)
    || !(node.updated_at === null || typeof node.updated_at === "number" && Number.isFinite(node.updated_at))
    || !Array.isArray(node.sources) || node.sources.some(source => !source || typeof source.ref !== "string"
      || source.label !== undefined && typeof source.label !== "string")) return false;
  const key = node.kind === "question" ? `question:${node.question_ref}`
    : node.kind === "cycle" && node.cycle_ref ? `cycle:${node.cycle_ref}`
      : node.kind === "stage" && node.cycle_ref && node.stage ? `stage:${node.cycle_ref}:${node.stage}`
        : node.kind === "target" && node.cycle_ref && node.target_ref ? `target:${node.cycle_ref}:${node.target_ref}` : null;
  return key !== null && key === node.node_key;
}

/** The recorder writes independently; this interface only reads its saved summaries. */
export function useTimelineSummaries(questRef: string | null) {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [result, setResult] = useState<{
    questRef: string | null;
    revision: number;
    nodes: Record<string, TimelineSummaryNode>;
    error: boolean;
  }>({ questRef: null, revision: -1, nodes: {}, error: false });
  useEffect(() => {
    const changed = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", changed);
    return () => document.removeEventListener("visibilitychange", changed);
  }, []);
  useEffect(() => {
    if (!questRef || !visible) return;
    let stopped = false;
    let timer: number | undefined;
    let request: AbortController | undefined;
    const read = async () => {
      request = new AbortController();
      const deadline = window.setTimeout(() => request?.abort(), 8_000);
      try {
        const response = await fetch(`/api/v1/quests/${encodeURIComponent(questRef)}/timeline-summaries`, {
          credentials: "same-origin", headers: { Accept: "application/json" }, signal: request.signal,
        });
        if (!response.ok) throw new Error("timeline_summaries_unavailable");
        const data = await response.json() as TimelineSummaries;
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
                updated_at: saved.updated_at, sources: saved.sources }
              : node;
          }
          return { questRef, revision: data.revision, nodes, error: false };
        });
      } catch {
        if (!stopped) setResult(previous => previous.questRef === questRef ? { ...previous, error: true }
          : { questRef, revision: -1, nodes: {}, error: true });
      } finally {
        window.clearTimeout(deadline);
        if (!stopped) timer = window.setTimeout(read, 5_000);
      }
    };
    void read();
    return () => { stopped = true; request?.abort(); window.clearTimeout(timer); };
  }, [questRef, visible]);
  return result.questRef === questRef ? result : { questRef, revision: -1, nodes: {}, error: false };
}
