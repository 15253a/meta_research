import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject, UIEvent } from "react";
import {
  outputStatusLabel,
  parseOutputChunks,
  parseProviderOutput,
  publicMessageContent,
} from "./readableOutputModel";
import type {
  OutputChunk,
  OutputSpan,
  ParsedOutput,
  ReadableCommand,
  ReadableEntry,
} from "./readableOutputModel";
import "./readable-output.css";

export type ReadableOutputProps = {
  rawText: string;
  startOffset?: number;
  streamKey?: string;
  isTerminal?: boolean;
  rootNativeSessionRef?: string | null;
  chunks?: readonly OutputChunk[];
  followLive?: boolean;
  onPauseFollow?: () => void;
  publicOnly?: boolean;
};

function useParsedOutput(props: ReadableOutputProps): ParsedOutput {
  return useMemo(() => props.chunks?.length
    ? parseOutputChunks(props.chunks, props)
    : parseProviderOutput(props.rawText, props), [
    props.rawText, props.startOffset, props.streamKey, props.isTerminal,
    props.rootNativeSessionRef, props.chunks,
  ]);
}

function inlineText(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => (
    part.startsWith("**") && part.endsWith("**")
      ? <strong key={index}>{part.slice(2, -2)}</strong>
      : part.startsWith("`") && part.endsWith("`")
        ? <code key={index}>{part.slice(1, -1)}</code>
        : <Fragment key={index}>{part}</Fragment>
  ));
}

/** A small text-only Markdown renderer: React escaping is never bypassed. */
function PublishedText({ text }: { text: string }) {
  const blocks = text.split(/(```[^\n]*\n[\s\S]*?```)/g);
  return <div className="ro-prose">{blocks.map((block, index) => {
    if (block.startsWith("```")) {
      const firstLine = block.indexOf("\n");
      return <pre key={index} className="ro-code"><code>{block.slice(firstLine + 1, -3)}</code></pre>;
    }
    return <Fragment key={index}>{block.split(/\n\s*\n/).filter(Boolean).map((paragraph, part) => {
      const lines = paragraph.split("\n");
      if (lines.every(line => /^\s*[-*]\s+/.test(line))) {
        return <ul key={part}>{lines.map((line, row) => <li key={row}>{inlineText(line.replace(/^\s*[-*]\s+/, ""))}</li>)}</ul>;
      }
      if (lines.every(line => /^\s*\d+[.)]\s+/.test(line))) {
        return <ol key={part}>{lines.map((line, row) => <li key={row}>{inlineText(line.replace(/^\s*\d+[.)]\s+/, ""))}</li>)}</ol>;
      }
      if (lines.length === 1 && /^#{1,6}\s/.test(paragraph)) {
        return <h4 key={part}>{inlineText(paragraph.replace(/^#{1,6}\s+/, ""))}</h4>;
      }
      return <p key={part}>{inlineText(paragraph)}</p>;
    })}</Fragment>;
  })}</div>;
}

function SourceSpans({ spans, label = "查看原始记录" }: { spans: readonly OutputSpan[]; label?: string }) {
  const [open, setOpen] = useState(false);
  return <details className="ro-source" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{label}</summary>
    {open ? spans.map((span, index) => <div className="ro-source-record" key={`${span.offset}:${index}`}>
      <small>来源字节 {span.offset}–{span.endOffset}</small>
      <pre>{span.raw}</pre>
    </div>) : null}
  </details>;
}

const actionLabels: Record<string, string> = {
  wait: "等待", dispatch: "安排执行", complete: "提交结果", stop: "停止", continue: "继续",
};

function CommandContents({ command }: { command: ReadableCommand }) {
  return <>
    {command.command ? <pre className="ro-command-line">{command.command}</pre> : <p>这条记录未提供命令文本。</p>}
    {command.output ? <pre className="ro-command-output">{command.output}</pre>
      : <p className="ro-empty-output">这条命令尚未提供可读取的输出。</p>}
    {command.exitCode !== null ? <small>退出码 {command.exitCode}</small> : null}
    {command.outputReplaced ? <p className="ro-source-note">命令更新提供了新的完整输出；较早版本保留在原始记录中。</p> : null}
    <SourceSpans spans={command.spans} />
  </>;
}

function FeedEntry({ entry }: { entry: ReadableEntry }) {
  if (entry.kind === "message") {
    const message = publicMessageContent(entry.text);
    return <article className="ro-message" data-output-kind="message" data-source-offset={entry.spans[0].offset}>
      <div className="ro-message-meta"><span>主智能体 · 公开说明</span>
        {message.action ? <span className="ro-action">{actionLabels[message.action] ?? message.action}</span> : null}
        {!entry.completed ? <span>正在输出</span> : null}
      </div>
      {message.body ? <PublishedText text={message.body} />
        : <p className="ro-structured-note">主智能体提交了一份结构化记录，可展开查看完整内容。</p>}
      <SourceSpans spans={entry.spans} label={message.structured ? "查看完整结构化记录" : "查看这条说明的原文"} />
    </article>;
  }
  if (entry.kind === "command") {
    return <details className="ro-operation" data-output-kind="command">
      <summary><span className="ro-operation-mark" aria-hidden="true">⌘</span>
        <span className="ro-operation-name" title={entry.command}>{entry.command || "执行命令"}</span>
        <span className="ro-operation-status" data-failed={entry.status === "failed" || (entry.exitCode !== null && entry.exitCode !== 0)}>
          {outputStatusLabel(entry.status)}{entry.exitCode !== null && entry.exitCode !== 0 ? ` · ${entry.exitCode}` : ""}
        </span>
      </summary>
      <div className="ro-operation-content"><CommandContents command={entry} /></div>
    </details>;
  }
  return <details className="ro-operation" data-output-kind="tool">
    <summary><span className="ro-operation-mark" aria-hidden="true">↗</span>
      <span className="ro-operation-name">{entry.name}</span>
      <span className="ro-operation-status">{outputStatusLabel(entry.status)}</span>
    </summary>
    <div className="ro-operation-content"><SourceSpans spans={entry.spans} label="查看工具记录" /></div>
  </details>;
}

type OperationEntry = Exclude<ReadableEntry, { kind: "message" }>;
type StageFeedGroup =
  | { kind: "message"; id: string; entry: Extract<ReadableEntry, { kind: "message" }> }
  | { kind: "operations"; id: string; entries: OperationEntry[] };

function groupStageEntries(entries: readonly ReadableEntry[]): StageFeedGroup[] {
  const groups: StageFeedGroup[] = [];
  for (const entry of entries) {
    if (entry.kind === "message") {
      groups.push({ kind: "message", id: entry.id, entry });
      continue;
    }
    const previous = groups.at(-1);
    if (previous?.kind === "operations") previous.entries.push(entry);
    else groups.push({ kind: "operations", id: entry.id, entries: [entry] });
  }
  return groups;
}

function OperationGroup({ entries }: { entries: readonly OperationEntry[] }) {
  const [open, setOpen] = useState(false);
  const failedCount = entries.filter(entry => entry.status === "failed"
    || (entry.kind === "command" && entry.exitCode !== null && entry.exitCode !== 0)).length;
  return <details className="ro-operation-group" data-output-kind="operation-group"
    onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span className="ro-operation-mark" aria-hidden="true">⌘</span>
      <span>执行了 {entries.length} 项操作</span>
      {failedCount ? <span className="ro-operation-group-failed">{failedCount} 项失败</span> : null}
    </summary>
    {open ? <div className="ro-operation-group-entries">{entries.map(entry => <FeedEntry key={entry.id} entry={entry} />)}</div> : null}
  </details>;
}

function OtherRecords({ parsed }: { parsed: ParsedOutput }) {
  const [open, setOpen] = useState(false);
  if (!parsed.details.length) return null;
  const partialCount = parsed.details.filter(detail => detail.kind === "partial").length;
  return <details className="ro-other-records" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>其他原始记录 · {parsed.details.length} 条{partialCount ? ` · ${partialCount} 段跨页片段` : ""}</summary>
    {open ? parsed.details.map((detail, index) => <div className="ro-other-record" key={`${detail.offset}:${index}`}>
      <b>{detail.label}</b>
      <SourceSpans spans={[detail]} label={`查看原文 · 字节 ${detail.offset}–${detail.endOffset}`} />
    </div>) : null}
  </details>;
}

function pauseIfReadingHistory(event: UIEvent<HTMLElement>, props: ReadableOutputProps) {
  const node = event.currentTarget;
  if (props.followLive && node.scrollHeight - node.clientHeight - node.scrollTop > 32) {
    props.onPauseFollow?.();
  }
}

export function StageReadableOutput(props: ReadableOutputProps & {
  scrollRef?: RefObject<HTMLDivElement | null>;
}) {
  const parsed = useParsedOutput(props);
  const localRef = useRef<HTMLDivElement>(null);
  const scrollRef = props.scrollRef ?? localRef;
  const [visibleCount, setVisibleCount] = useState(80);
  const groups = useMemo(() => groupStageEntries(parsed.entries), [parsed.entries]);
  const hiddenCount = groups.slice(0, Math.max(0, groups.length - visibleCount))
    .reduce((count, group) => count + (group.kind === "message" ? 1 : group.entries.length), 0);
  useEffect(() => {
    setVisibleCount(80);
  }, [props.streamKey]);
  useEffect(() => {
    if (props.followLive && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [props.followLive, parsed, scrollRef]);
  return <section className="ro-stage" aria-label="Stage 主智能体输出">
    <div className="ro-stage-scroll" ref={scrollRef} tabIndex={0}
      aria-label="主智能体输出，可滚动阅读" onScroll={event => pauseIfReadingHistory(event, props)}>
      {hiddenCount ? <button type="button" className="ro-load-earlier" onClick={() => {
        props.onPauseFollow?.();
        setVisibleCount(count => count + 80);
      }}>显示更早的输出 · 还有 {hiddenCount} 条</button> : null}
      {groups.slice(-visibleCount).map(group => group.kind === "message"
        ? <FeedEntry key={group.id} entry={group.entry} />
        : <OperationGroup key={`operations:${group.id}`} entries={group.entries} />)}
      {!parsed.entries.length ? <div className="ro-empty">
        <b>{props.rawText || parsed.byteLength ? "这一段还没有可直接阅读的主智能体说明" : "等待主智能体输出"}</b>
        <p>{parsed.details.some(detail => detail.kind === "partial")
          ? "记录横跨分页；读取相邻页后会合并显示，原始片段已保留。"
          : "已读取的会话记录、其他来源和暂未识别的内容可在下方查看。"}</p>
      </div> : null}
      {!props.publicOnly && <OtherRecords parsed={parsed} />}
    </div>
  </section>;
}

export function TargetCommandOutput(props: ReadableOutputProps & {
  logRef?: RefObject<HTMLPreElement | null>;
}) {
  const parsed = useParsedOutput(props);
  const commands = parsed.entries.filter((entry): entry is ReadableCommand => entry.kind === "command");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [wrap, setWrap] = useState(true);
  const id = useId();
  const localRef = useRef<HTMLPreElement>(null);
  const logRef = props.logRef ?? localRef;
  const selected = commands.find(command => command.id === selectedId) ?? commands.at(-1) ?? null;
  const nonCommandSpans = parsed.entries.filter(entry => entry.kind !== "command")
    .flatMap(entry => entry.spans).sort((left, right) => left.offset - right.offset);
  useEffect(() => setSelectedId(null), [props.streamKey]);
  useEffect(() => {
    if (props.followLive && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [props.followLive, selected?.id, selected?.output, logRef]);
  return <section className="ro-target" aria-label="任务命令的真实输出">
    <div className="ro-target-toolbar">
      <label htmlFor={`${id}-command`}>执行命令</label>
      <select id={`${id}-command`} value={selected?.id ?? ""} disabled={!commands.length}
        onChange={event => setSelectedId(event.currentTarget.value)}>
        {!commands.length ? <option value="">当前已读取记录中没有命令</option> : null}
        {commands.map((command, index) => <option key={command.id} value={command.id}>
          {index + 1}. {command.command || `命令 ${command.itemId ?? "未提供名称"}`}
        </option>)}
      </select>
      <label className="ro-wrap"><input type="checkbox" checked={wrap} onChange={event => setWrap(event.currentTarget.checked)} />换行</label>
    </div>
    {selected ? <>
      <div className="ro-target-command"><code>{selected.command || "命令文本未提供"}</code>
        <span>{outputStatusLabel(selected.status)}{selected.exitCode !== null ? ` · 退出码 ${selected.exitCode}` : ""}</span>
      </div>
      <pre ref={logRef} className="ro-target-log" data-wrap={wrap ? "true" : "false"} tabIndex={0}
        role="log" aria-live="off" aria-label="所选命令的实际输出" onScroll={event => pauseIfReadingHistory(event, props)}>{selected.output}</pre>
      {!selected.output ? <p className="ro-target-empty">这条命令尚未提供可读取的输出。</p> : null}
      {selected.outputReplaced ? <p className="ro-target-empty">这里显示最新完整输出，先前版本保留在来源记录中。</p> : null}
      <SourceSpans spans={selected.spans} label="查看所选命令的来源记录" />
    </> : <div className="ro-target-empty">
      <b>当前已读取记录中还没有命令输出</b>
      <p>命令产生输出后会显示在这里。也可以读取相邻页，查看已记录的其他命令。</p>
    </div>}
    {nonCommandSpans.length ? <SourceSpans spans={nonCommandSpans} label="查看智能体说明与工具原始记录" /> : null}
    <OtherRecords parsed={parsed} />
  </section>;
}
