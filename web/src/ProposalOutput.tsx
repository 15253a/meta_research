import { useEffect, useRef, useState } from "react";

type Page = {
  generation_ref: string;
  status: string;
  availability: string;
  text: string;
  next_offset: number;
  source_bytes: number;
  has_more: boolean;
};

export function ProposalOutput({ initializationId, generationRef, status }: {
  initializationId: string; generationRef: string; status: string;
}) {
  const [open, setOpen] = useState(false);
  const [page, setPage] = useState<Page | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(document.visibilityState !== "hidden");
  const log = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const update = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", update);
    return () => document.removeEventListener("visibilitychange", update);
  }, []);

  useEffect(() => {
    if (!open || !visible) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let offset = 0;
    let text = "";
    setPage(null);
    setError(false);
    async function poll() {
      try {
        const params = new URLSearchParams({ generation_ref: generationRef, after: String(offset) });
        const response = await fetch(`/api/v1/quest-initializations/${encodeURIComponent(initializationId)}/proposal-output?${params}`, {
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
  }, [initializationId, generationRef, status, open, visible, retry]);

  useEffect(() => { if (log.current) log.current.scrollTop = log.current.scrollHeight; }, [page]);
  const currentStatus = page?.status ?? status;
  const label = ({ queued: "排队中", running: "运行中", succeeded: "已完成", failed: "已失败", cancelled: "已取消" } as Record<string, string>)[currentStatus] ?? currentStatus;
  return <details onToggle={event => setOpen(event.currentTarget.open)} data-testid="proposal-output">
    <summary>查看提案生成日志 · {label}</summary>
    <p>私有输出 · 当前提案的原始 JSONL；运行时自动刷新，完成后保留。</p>
    {error ? <p role="alert">日志连接暂时中断，正在重试。<button type="button" onClick={() => setRetry(value => value + 1)}>重试</button></p> : null}
    {!page && !error ? <p>正在读取日志…</p> : null}
    {page?.text ? <pre ref={log} role="log" aria-label="提案生成原始 stdout" style={{ maxHeight: 320, overflow: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{page.text}</pre> : null}
    {page && !page.text && !error ? <p>{["queued", "running"].includes(page.status) ? "等待 Provider 输出，自动刷新中。" : "该次生成已结束，没有可读取的 stdout。"}</p> : null}
    {page ? <small>{label} · {page.next_offset} / {page.source_bytes} bytes{page.has_more ? " · 正在读取后续输出" : " · 已同步到最新输出"}{page.next_offset > 1024 * 1024 ? " · 仅显示最近 1 MiB 文本" : ""}</small> : null}
  </details>;
}
