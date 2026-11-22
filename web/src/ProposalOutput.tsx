import { useEffect, useMemo, useRef, useState } from "react";
import { proposalActivities } from "./proposalActivity";
import { useSmoothFollowScroll } from "./useSmoothFollowScroll";

type Page = {
  generation_ref: string;
  status: string;
  availability: string;
  text: string;
  next_offset: number;
  source_bytes: number;
  has_more: boolean;
  phase?: "understanding" | "drafting" | null;
};

export function ProposalOutput({ initializationId, generationRef, status, enabled = true, onPhase, preparation = false, preparationRequestRef, preparationNeedsReview = false }: {
  initializationId: string; generationRef: string; status: string; enabled?: boolean;
  onPhase?: (phase: "understanding" | "drafting" | null) => void;
  preparation?: boolean;
  preparationRequestRef?: string;
  preparationNeedsReview?: boolean;
}) {
  const streamKey = `${initializationId}|${preparation ? "prep" : "gen"}|${preparationRequestRef ?? ""}|${generationRef}`;
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(document.visibilityState !== "hidden");
  const log = useRef<HTMLDivElement>(null);
  const streamRef = useRef({ streamKey, offset: 0, text: "" });
  const seenKeysRef = useRef<{ streamKey: string; hydrated: boolean; seen: Set<string>; entering: Set<string> }>({
    streamKey,
    hydrated: false,
    seen: new Set(),
    entering: new Set(),
  });

  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (streamRef.current.streamKey !== streamKey) {
      streamRef.current = { streamKey, offset: 0, text: "" };
      seenKeysRef.current = { streamKey, hydrated: false, seen: new Set(), entering: new Set() };
      setPage(null);
      setError(false);
    }
    if (!enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const currentOffset = streamRef.current.offset;
        const params = new URLSearchParams({
          [preparation ? "request_ref" : "generation_ref"]: preparation ? preparationRequestRef ?? generationRef : generationRef,
          after: String(currentOffset),
        });
        if (preparation) params.set("preparation_ref", generationRef);
        const response = await fetch(`/api/v1/quest-initializations/${encodeURIComponent(initializationId)}/${preparation ? "preparation-output" : "proposal-output"}?${params}`, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error(String(response.status));
        const next: Page = await response.json();
        if (controller.signal.aborted) return;
        if (next.generation_ref !== generationRef || next.next_offset < currentOffset) throw new Error("stale");
        const combinedText = (streamRef.current.text + next.text).slice(-1024 * 1024);
        streamRef.current.text = combinedText;
        streamRef.current.offset = next.next_offset;
        setPage(previous => (
          previous &&
          previous.text === combinedText &&
          previous.status === next.status &&
          previous.phase === next.phase &&
          previous.has_more === next.has_more
            ? previous
            : { ...next, text: combinedText }
        ));
        setError(false);
        if (next.has_more || ["queued", "running"].includes(next.status)) {
          timer = setTimeout(poll, next.has_more ? 50 : 2000);
        }
      } catch {
        if (!controller.signal.aborted) {
          setError(true);
          timer = setTimeout(poll, 3000);
        }
      }
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [initializationId, generationRef, status, enabled, visible, retry, preparation, preparationRequestRef, streamKey]);

  const activities = useMemo(() => proposalActivities(page?.text ?? ""), [page?.text]);

  if (seenKeysRef.current.streamKey !== streamKey) {
    seenKeysRef.current = { streamKey, hydrated: false, seen: new Set(), entering: new Set() };
  }
  const tracker = seenKeysRef.current;
  if (page !== null) {
    if (!tracker.hydrated) {
      for (const item of activities) tracker.seen.add(item.key);
      tracker.hydrated = true;
    } else {
      for (const item of activities) {
        if (!tracker.seen.has(item.key)) {
          tracker.seen.add(item.key);
          tracker.entering.add(item.key);
        }
      }
    }
  }

  const lastKey = activities.at(-1)?.key ?? null;
  const { handleScroll, hasUnseen, jumpToLatest } = useSmoothFollowScroll({
    logRef: log,
    streamKey,
    itemCount: activities.length,
    lastKey,
    enabled,
  });

  useEffect(() => { onPhase?.(page?.phase ?? null); }, [page?.phase, onPhase]);
  const currentStatus = page?.status ?? status;
  const label = ({ queued: "排队中", running: "运行中", succeeded: "已完成", failed: "已失败", cancelled: "已取消" } as Record<string, string>)[currentStatus] ?? currentStatus;
  const active = ["queued", "running"].includes(currentStatus);
  const statusText = active && page?.phase === "understanding"
    ? "理解资料"
    : active && page?.phase === "drafting"
      ? "起草问题"
      : label;

  return <section className={`first-question-activity ${active ? "is-live" : ""}`} data-testid="proposal-output" aria-label="第一问运行活动">
    <header>
      <b><i className={`first-question-live-dot ${active ? "is-live" : ""}`} aria-hidden="true" />最近活动</b>
      <span>{statusText}</span>
    </header>
    {error ? <p className="first-question-activity-error" role="alert">日志连接暂时中断，正在重试。<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></p> : null}
    <div className="first-question-activity-body">
      <div
        ref={log}
        className={`first-question-activity-log ${active || activities.length ? "has-stream" : ""}`}
        role="log"
        aria-label="第一问最近活动"
        aria-live="polite"
        aria-relevant="additions"
        aria-atomic="false"
        onScroll={handleScroll}
      >
        {activities.map((activity, index) => (
          <div
            key={activity.key}
            className={`first-question-activity-row ${activity.status} ${active && index === activities.length - 1 ? "is-latest" : ""}`}
            data-entering={tracker.entering.has(activity.key) ? "true" : undefined}
          >
            <i aria-hidden="true" />
            <span>{activity.label}</span>
          </div>
        ))}
        {!activities.length ? (
          <p className="first-question-activity-empty">
            {active
              ? (page ? "正在等待第一条运行活动…" : "正在连接本次运行…")
              : (preparationNeedsReview ? "本次资料准备仍需核对，尚无可展示的运行活动。" : "本次生成已结束，尚无可展示的运行活动。")}
          </p>
        ) : null}
      </div>
      {hasUnseen ? (
        <button type="button" className="quest-activity-jump" onClick={jumpToLatest}>
          查看最新 ↓
        </button>
      ) : null}
    </div>
  </section>;
}
