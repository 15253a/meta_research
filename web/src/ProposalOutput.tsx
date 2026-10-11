import { useEffect, useRef, useState } from "react";
import { proposalActivities } from "./proposalActivity";

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

export function ProposalOutput({ initializationId, generationRef, status, enabled = true, onPhase, preparation = false }: {
  initializationId: string; generationRef: string; status: string; enabled?: boolean;
  onPhase?: (phase: "understanding" | "drafting" | null) => void;
  preparation?: boolean;
}) {
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(document.visibilityState !== "hidden");
  const log = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (!enabled || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let offset = 0;
    let text = "";
    setPage(null);
    setError(false);
    async function poll() {
      try {
        const params = new URLSearchParams({ [preparation ? "request_ref" : "generation_ref"]: generationRef, after: String(offset) });
        const response = await fetch(`/api/v1/quest-initializations/${encodeURIComponent(initializationId)}/${preparation ? "preparation-output" : "proposal-output"}?${params}`, {
          credentials: "same-origin", cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error(String(response.status));
        const next: Page = await response.json();
        if (controller.signal.aborted) return;
        if (next.generation_ref !== generationRef || next.next_offset < offset) throw new Error("stale");
        text = (text + next.text).slice(-1024 * 1024);
        offset = next.next_offset;
        setPage({ ...next, text });
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
  }, [initializationId, generationRef, status, enabled, visible, retry, preparation]);

  useEffect(() => {
    if (log.current && pinned.current) log.current.scrollTop = log.current.scrollHeight;
  }, [page?.text]);
  useEffect(() => { onPhase?.(page?.phase ?? null); }, [page?.phase, onPhase]);
  const currentStatus = page?.status ?? status;
  const label = ({ queued: "排队中", running: "运行中", succeeded: "已完成", failed: "已失败", cancelled: "已取消" } as Record<string, string>)[currentStatus] ?? currentStatus;
  const activities = proposalActivities(page?.text ?? "");
  const active = ["queued", "running"].includes(currentStatus);
  return <section className="first-question-activity" data-testid="proposal-output" aria-label="第一问运行活动">
    <header><b>最近活动</b><span>{active && page?.phase === "understanding" ? "正在理解已有资料" : active && page?.phase === "drafting" ? "正在准备问题草稿" : label} · {active ? "自动刷新" : "保留运行记录"}</span></header>
    {error ? <p role="alert">日志连接暂时中断，正在重试。<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></p> : null}
    <div ref={log} className="first-question-activity-log" role="log" aria-label="第一问最近活动" aria-live="polite" aria-relevant="additions" aria-atomic="false"
      onScroll={event => { const node = event.currentTarget; pinned.current = node.scrollTop + node.clientHeight >= node.scrollHeight - 4; }}>
      {activities.map(activity => <div key={activity.key} className={`first-question-activity-row ${activity.status}`}><i aria-hidden="true" /><span>{activity.label}</span></div>)}
      {active ? <p>{page ? "等待下一条运行活动，自动刷新中。" : "正在连接本次运行…"}</p> : !activities.length ? <p>本次生成已结束，尚无可展示的运行活动。</p> : null}
    </div>
    {page?.has_more ? <p>正在读取后续运行活动…</p> : null}
  </section>;
}
