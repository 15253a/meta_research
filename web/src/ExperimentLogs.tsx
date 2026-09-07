import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { BundleTargetProjection } from "./api";
import {
  LOG_PAGE_BYTES, formatExperimentLogText, mergeExperimentLogPage, validateExperimentLogList, validateExperimentLogPage,
} from "./experimentLogModel";
import type { ExperimentLogList, ExperimentLogPage, ExperimentLogWindow } from "./experimentLogModel";
import "./experiment-logs.css";

export type ExperimentLogsProps = {
  target: BundleTargetProjection;
  blockedByHumanRequest: boolean;
  activityPaused: boolean;
  minimized: boolean;
  onMinimize: () => void;
  onClose: () => void;
};

const POLL_MILLISECONDS = 2000;
const fileSize = (bytes: number) => bytes < 1024 ? `${bytes} B`
  : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB`
    : bytes < 1024 * 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
      : `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
const timeLabel = (seconds: number) => new Date(seconds * 1000).toLocaleString();
function errorCode(error: unknown) { return error instanceof Error ? error.message : "experiment_log_unavailable"; }
function friendlyError(code: string) {
  if (code === "experiment_log_identity_invalid") return "日志所属任务已变化，正在等待当前任务信息。";
  if (code === "experiment_log_not_found") return "这个日志文件暂时不可读，正在重新查找。";
  return "暂时无法读取日志，保留最后一次读取的内容，稍后自动重试。";
}
function targetStatus(target: BundleTargetProjection) {
  if (target.blocker) return "等待处理";
  return ({ running: "执行中", in_progress: "执行中", committed: "结果已接纳", failed: "执行失败",
    cancelled: "已取消", blocked: "等待处理", queued: "等待执行", ready: "等待执行", succeeded: "已结束",
    completed: "已结束", fenced: "已停止" } as Record<string, string>)[target.status] ?? target.status;
}

async function readJson<T>(url: string, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, signal });
  if (!response.ok) {
    let code = `request_failed:${response.status}`;
    try {
      const body = await response.json() as { detail?: { code?: string }; error?: { code?: string } };
      code = body.detail?.code ?? body.error?.code ?? code;
    } catch { /* Preserve the HTTP status when there is no structured error. */ }
    throw new Error(code);
  }
  return await response.json() as T;
}

export function ExperimentLogs({ target, blockedByHumanRequest, activityPaused, minimized, onMinimize, onClose }: ExperimentLogsProps) {
  const scope = `${target.target_ref}:${target.target_run_ref ?? ""}`;
  const [catalog, setCatalog] = useState<ExperimentLogList | null>(null);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [content, setContent] = useState<ExperimentLogWindow | null>(null);
  const [following, setFollowing] = useState(true);
  const [wrap, setWrap] = useState(true);
  const [rawCharacters, setRawCharacters] = useState(false);
  const [readingEarlier, setReadingEarlier] = useState(false);
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  const [connection, setConnection] = useState<"connecting" | "connected" | "error">("connecting");
  const [lastRead, setLastRead] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const selected = useRef<string | null>(null);
  const contentRef = useRef<ExperimentLogWindow | null>(null);
  const followingRef = useRef(true);
  const forceTail = useRef(false);
  const earlierRequest = useRef<{ before: number; streamRef: string } | null>(null);
  const showEarlierBottom = useRef(false);
  const logRef = useRef<HTMLPreElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const windowRef = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const paused = minimized || blockedByHumanRequest || activityPaused || !visible;

  useEffect(() => {
    const listener = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", listener);
    return () => document.removeEventListener("visibilitychange", listener);
  }, []);

  useEffect(() => {
    opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus({ preventScroll: true });
    return () => { if (opener.current?.isConnected) opener.current.focus({ preventScroll: true }); };
  }, []);

  useEffect(() => {
    selected.current = null;
    contentRef.current = null;
    followingRef.current = true;
    forceTail.current = false;
    earlierRequest.current = null;
    setReadingEarlier(false);
    setCatalog(null);
    setContent(null);
    setSelectedRef(null);
    setFollowing(true);
    setConnection("connecting");
    setLastRead(null);
    setError(null);
    setNotice(null);
  }, [scope]);

  useEffect(() => {
    if (paused || !target.target_run_ref) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    const base = `/api/v1/bundle/targets/${encodeURIComponent(target.target_ref)}/experiment-logs`;
    const scopeParameters = new URLSearchParams({ target_run_ref: target.target_run_ref });
    let consecutiveResets = 0;
    const poll = async () => {
      let delay = POLL_MILLISECONDS;
      try {
        const listed = validateExperimentLogList(
          await readJson<ExperimentLogList>(`${base}?${scopeParameters}`, controller.signal), target.target_ref, target.target_run_ref,
        );
        if (disposed) return;
        if (listed.status === "unavailable") {
          // An unavailable catalog is not evidence that files disappeared.
          // Keep the last confirmed same-run selection/text until reads recover.
          setCatalog(value => value?.target_ref === target.target_ref && value.target_run_ref === target.target_run_ref
            ? value : listed);
          setConnection("error");
          setError(listed.reason?.code ?? "experiment_log_unavailable");
          setReadingEarlier(false);
          delay = 4000;
          return;
        }
        setCatalog(listed);
        setConnection("connected");
        setLastRead(Date.now());
        setError(null);
        const active = listed.logs.find(log => log.log_ref === selected.current)
          ?? listed.logs.find(log => log.log_ref === listed.default_log_ref) ?? listed.logs[0];
        if (!active) {
          selected.current = null;
          contentRef.current = null;
          earlierRequest.current = null;
          setReadingEarlier(false);
          setSelectedRef(null);
          setContent(null);
          if (listed.reason?.code) setError(listed.reason.code);
        } else {
          if (selected.current !== active.log_ref) {
            const hadSelection = selected.current !== null;
            selected.current = active.log_ref;
            contentRef.current = null;
            earlierRequest.current = null;
            setReadingEarlier(false);
            setSelectedRef(active.log_ref);
            setContent(null);
            if (hadSelection) setNotice("原日志已变化，已切换到当前可用的日志文件。");
          }
          const existing = contentRef.current;
          const earlier = earlierRequest.current;
          if (!followingRef.current && existing && !earlier) {
            // While someone reads history, only refresh metadata. New writes do
            // not mutate the visible text or evict the lines being read.
            if (active.stream_ref && active.stream_ref !== existing.streamRef) {
              setNotice("日志文件已轮换；当前保留已读取片段。继续跟随时读取新文件末尾。");
              forceTail.current = true;
            } else if (active.source_bytes < existing.nextOffset) {
              setNotice("日志文件已被截断；当前保留已读取片段。继续跟随时重新读取文件末尾。");
              forceTail.current = true;
            }
          } else {
            const requestedTail = forceTail.current;
            const current = requestedTail || earlier ? null : existing;
            const parameters = new URLSearchParams({ limit: String(LOG_PAGE_BYTES), target_run_ref: target.target_run_ref! });
            if (earlier) {
              parameters.set("before", String(earlier.before));
              parameters.set("stream_ref", earlier.streamRef);
            } else if (current) {
              parameters.set("after", String(current.nextOffset));
              parameters.set("stream_ref", current.streamRef);
            }
            const page = validateExperimentLogPage(await readJson<ExperimentLogPage>(
              `${base}/${encodeURIComponent(active.log_ref)}?${parameters}`, controller.signal,
            ), target.target_ref, target.target_run_ref, active.log_ref);
            if (disposed || selected.current !== active.log_ref) return;
            if (earlier && (page.stream_ref !== earlier.streamRef || page.next_offset !== earlier.before)) {
              throw new Error("experiment_log_reset_required");
            }
            const next = mergeExperimentLogPage(current, page);
            contentRef.current = next;
            forceTail.current = false;
            earlierRequest.current = null;
            consecutiveResets = 0;
            if (earlier) showEarlierBottom.current = true;
            setReadingEarlier(false);
            setContent(next);
            if (requestedTail) setNotice("已从当前日志文件末尾重新读取；较早的内容保留在原文件中。");
            setCatalog(value => value ? { ...value, logs: value.logs.map(log => log.log_ref === active.log_ref
              ? { ...log, source_bytes: page.source_bytes, modified_at: page.modified_at } : log) } : value);
            setConnection("connected");
            setLastRead(Date.now());
            if (followingRef.current && page.has_more && page.next_offset > (current?.nextOffset ?? page.offset)) delay = 150;
          }
        }
      } catch (caught) {
        if (disposed || controller.signal.aborted) return;
        const code = errorCode(caught);
        if (code === "experiment_log_reset_required" || code === "experiment_log_not_found") {
          contentRef.current = null;
          earlierRequest.current = null;
          setReadingEarlier(false);
          forceTail.current = true;
          setContent(null);
          setConnection("connecting");
          setNotice(code === "experiment_log_reset_required"
            ? "日志文件已轮换、截断或重新绑定，正在从新文件末尾读取。"
            : "日志文件已变化，正在重新查找当前文件。");
          consecutiveResets += 1;
          delay = consecutiveResets === 1 ? 300 : Math.min(consecutiveResets * 2000, 8000);
        } else {
          setError(code);
          setReadingEarlier(false);
          setConnection("error");
          delay = 4000;
          if (code === "experiment_log_identity_invalid") {
            contentRef.current = null;
            setContent(null);
            setCatalog(null);
          }
        }
      } finally {
        if (!disposed) timer = setTimeout(() => void poll(), delay);
      }
    };
    void poll();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [scope, paused, refresh, target.target_ref, target.target_run_ref]);

  useEffect(() => {
    if ((following || showEarlierBottom.current) && !paused && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
      showEarlierBottom.current = false;
    }
  }, [content, following, paused]);

  const currentCatalog = catalog?.target_ref === target.target_ref && catalog.target_run_ref === target.target_run_ref ? catalog : null;
  const current = content?.targetRef === target.target_ref && content.targetRunRef === target.target_run_ref ? content : null;
  const displayText = useMemo(() => rawCharacters ? current?.text ?? "" : formatExperimentLogText(current?.text ?? ""), [current?.text, rawCharacters]);
  const file = currentCatalog?.logs.find(log => log.log_ref === selectedRef) ?? null;
  const sourceBytes = file?.source_bytes ?? current?.sourceBytes ?? 0;
  const unreadBytes = current ? Math.max(0, sourceBytes - current.nextOffset - current.pendingUtf8Bytes) : 0;
  const taskLabel = targetStatus(target);
  const connectionLabel = paused ? "已暂停读取" : connection === "connected" ? "日志读取正常"
    : connection === "error" ? "正在重连" : "正在连接日志";

  const resume = () => {
    earlierRequest.current = null;
    setReadingEarlier(false);
    followingRef.current = true;
    setFollowing(true);
    // An explicit return-to-live action may skip a very large unread interval,
    // but it never silently stitches that interval into existing text.
    if (unreadBytes > 4 * LOG_PAGE_BYTES) {
      forceTail.current = true;
      setNotice("正在定位到文件末尾；中间未加载的内容保留在原文件中。");
    }
    setRefresh(value => value + 1);
  };

  return createPortal(<section id="experiment-log-dialog" ref={windowRef}
    className="experiment-log-window" role="dialog" aria-modal="false"
    aria-label={`${target.target_key} 训练与评估日志`} aria-hidden={blockedByHumanRequest ? true : undefined}
    inert={blockedByHumanRequest} data-hc-background data-hc-inert-owner="experiment-log"
    data-minimized={minimized ? "true" : "false"}
    onKeyDown={event => {
      if (event.key === "Escape" && !blockedByHumanRequest) {
        event.preventDefault(); event.stopPropagation(); onClose();
      }
    }}>
    <header className="experiment-log-header">
      <div><small>训练与评估 · 文件实时输出</small><b>{target.target_key}</b></div>
      <div className="experiment-log-window-actions">
        <button type="button" onClick={onMinimize} aria-label={minimized ? "展开实验日志" : "最小化实验日志"}>{minimized ? "展开" : "最小化"}</button>
        <button ref={closeRef} type="button" onClick={onClose} aria-label="关闭实验日志">×</button>
      </div>
    </header>
    {minimized ? <p className="experiment-log-minimized">{file?.name ?? "等待日志文件"} · 已暂停读取</p> : <div className="experiment-log-body">
      <div className="experiment-log-toolbar">
        <label className="experiment-log-select">日志文件
          <select aria-label="选择训练或评估日志文件" value={selectedRef ?? ""} disabled={!currentCatalog?.logs.length}
            onChange={event => {
              selected.current = event.currentTarget.value;
              setSelectedRef(selected.current);
              contentRef.current = null;
              earlierRequest.current = null;
              setReadingEarlier(false);
              setContent(null);
              forceTail.current = false;
              followingRef.current = true;
              setFollowing(true);
              setNotice(null);
              setError(null);
              setRefresh(value => value + 1);
            }}>
            {!currentCatalog?.logs.length ? <option value="">尚未发现日志文件</option> : null}
            {currentCatalog?.logs.map(log => <option key={log.log_ref} value={log.log_ref}>
              {log.kind === "train" ? "训练" : "评估"} · {log.relative_path}
            </option>)}
          </select>
        </label>
        <label className="experiment-log-wrap"><input type="checkbox" checked={wrap} onChange={event => setWrap(event.currentTarget.checked)} />自动换行</label>
        <label className="experiment-log-wrap"><input type="checkbox" checked={rawCharacters} onChange={event => setRawCharacters(event.currentTarget.checked)} />控制字符原样</label>
      </div>
      <div className="experiment-log-states"><span data-connection={paused ? "paused" : connection}><i aria-hidden="true" />{connectionLabel}</span>
        <span>任务：{taskLabel}</span>
      </div>
      {file ? <div className="experiment-log-file-facts"><code title={file.relative_path}>{file.relative_path}</code>
        <span>{fileSize(sourceBytes)} · 最近写入 {timeLabel(file.modified_at)}</span></div> : null}
      {error ? <div className="experiment-log-notice is-error" role="status"><span>{friendlyError(error)}</span>
        <button type="button" onClick={() => setRefresh(value => value + 1)}>重试</button>
        <details><summary>详情</summary><code>{error}</code></details>
      </div> : null}
      {notice ? <div className="experiment-log-notice" role="status"><span>{notice}</span>
        <button type="button" onClick={() => setNotice(null)} aria-label="关闭日志更新提示">×</button></div> : null}
      {current ? <pre ref={logRef} className="experiment-log-text" data-wrap={wrap ? "true" : "false"}
        role="log" aria-live="off" aria-label="日志文件原始内容" tabIndex={0} onScroll={event => {
          const node = event.currentTarget;
          if (followingRef.current && node.scrollHeight - node.clientHeight - node.scrollTop > 32) {
            followingRef.current = false;
            setFollowing(false);
          }
        }}>{displayText}</pre> : <div className="experiment-log-empty" role="status">
        <span aria-hidden="true">⌘</span>
        <b>{connection === "connecting" ? "正在查找训练与评估日志…"
          : currentCatalog?.status === "unavailable" ? "当前实验的日志文件暂不可读"
            : file ? "正在读取文件末尾…" : "尚未生成 train.log / eval.log"}</b>
        <p>{file ? "首次只读取末尾一段，后续增量刷新。"
          : "实际训练或评估开始并写入日志后，这里会自动更新。"}</p>
      </div>}
      {current && !current.text ? <p className="experiment-log-empty-file">文件已创建，暂时没有完整可读取的内容。</p> : null}
      <footer className="experiment-log-footer">
        <div className="experiment-log-history"><button type="button" disabled={paused || !current || current.startOffset === 0 || !current.startOffsetExact || readingEarlier}
          onClick={() => {
            if (!current || current.startOffset <= 0) return;
            followingRef.current = false;
            setFollowing(false);
            earlierRequest.current = { before: current.startOffset, streamRef: current.streamRef };
            setReadingEarlier(true);
            setNotice(null);
            setRefresh(value => value + 1);
          }}>{readingEarlier ? "正在读取…" : "↑ 读取更早日志"}</button>
          <small>{rawCharacters ? "控制字符按读取文本显示" : "终端显示 · 安全呈现 ANSI / 回车进度"}</small>
        </div>
        <div className="experiment-log-follow-state">{following
          ? <span>{paused ? "恢复窗口后继续读取" : file ? "跟随文件最新写入" : "等待日志文件生成"}</span>
          : <><span>正在阅读已加载内容{unreadBytes ? ` · 新增 ${fileSize(unreadBytes)}` : ""}</span>
            <button type="button" onClick={resume}>{unreadBytes ? "有新日志 · 继续跟随 ↓" : "继续跟随 ↓"}</button></>}
        </div>
        <small>{current ? current.startOffsetExact ? `已载入字节 ${current.startOffset}–${current.nextOffset}` : `已读取至字节 ${current.nextOffset} · 含非 UTF-8 字符` : "每次最多读取 64 KiB"}
          {current?.trimmed ? " · 窗口保留最近 512 KiB" : current && current.startOffset > 0 ? " · 从文件末尾开始读取" : ""}
          {lastRead ? ` · 最近读取 ${new Date(lastRead).toLocaleTimeString()}` : ""}</small>
        {currentCatalog?.truncated ? <small>目录扫描达到显示上限，仅列出本次发现的日志文件。</small> : null}
        {current?.decodeReplacements ? <small>文件含无法按 UTF-8 解码的字节，已用替代字符显示；控制字符视图也使用读取后的文本。</small> : null}
      </footer>
    </div>}
  </section>, document.body);
}
