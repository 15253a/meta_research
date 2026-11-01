import { useEffect, useRef, useState } from "react";
import { fetchCompanionFiles, ProductError, type CompanionFile } from "./api";
import "./companion-files.css";

export function CompanionFiles({ scopeRef, refreshKey }: { scopeRef: string; refreshKey: string }) {
  const [open, setOpen] = useState(false);
  const [files, setFiles] = useState<CompanionFile[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const read = async (offset: number) => {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true); setError(null);
    try {
      const page = await fetchCompanionFiles(scopeRef, offset, controller.signal);
      if (controller.signal.aborted) return;
      setFiles(current => offset === 0 ? page.files : [...current, ...page.files]);
      setNextOffset(page.next_offset);
    } catch (caught) {
      if (!controller.signal.aborted) setError(caught instanceof ProductError ? caught.code : "文件暂时无法读取，可重试。");
    } finally { if (!controller.signal.aborted) setLoading(false); }
  };
  useEffect(() => {
    if (open) void read(0);
    return () => requestRef.current?.abort();
  }, [scopeRef, open, refreshKey, refresh]);

  return <details className="companion-files" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>助手文件</summary>
    {open && <div>
      <p>这里保留本研究的材料、草稿与交付文件。</p>
      <button type="button" disabled={loading} onClick={() => setRefresh(value => value + 1)}>刷新文件</button>
      {error && <p role="alert">{error}</p>}
      {loading && <p role="status">正在读取文件…</p>}
      {!loading && !error && !files.length && <p>目录中还没有文件。</p>}
      <ul>{files.map(file => <li key={`${file.workspace_ref}:${file.path}`}>
        {file.readable && file.download_url.startsWith("/api/v1/companion/files/content?")
          ? <a href={file.download_url} download>{file.path}</a>
          : <span>{file.path} · 暂不可读</span>}
        <small>{file.bytes.toLocaleString()} 字节</small>
      </li>)}</ul>
      {nextOffset !== null && <button type="button" disabled={loading} onClick={() => void read(nextOffset)}>更多文件</button>}
    </div>}
  </details>;
}
