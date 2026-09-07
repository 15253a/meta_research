/** Deterministic presentation of an authenticated native Codex stdout stream.
 * The API remains responsible for run/attempt/operation authorization and paging.
 * This module never synthesizes messages, elapsed time, or research outcomes.
 */
export type OutputChunk = {
  text: string;
  offset: number;
  streamKey?: string;
  isTerminal?: boolean;
};

export type OutputSpan = { offset: number; endOffset: number; raw: string };
export type ReadableMessage = {
  kind: "message";
  id: string;
  text: string;
  completed: boolean;
  spans: OutputSpan[];
};
export type ReadableCommand = {
  kind: "command";
  id: string;
  itemId: string | null;
  command: string;
  output: string;
  status: string;
  exitCode: number | null;
  outputReplaced: boolean;
  spans: OutputSpan[];
};
export type ReadableTool = {
  kind: "tool";
  id: string;
  name: string;
  status: string;
  spans: OutputSpan[];
};
export type ReadableEntry = ReadableMessage | ReadableCommand | ReadableTool;
export type SourceDetail = OutputSpan & {
  kind: "partial" | "unknown" | "lifecycle" | "other-actor" | "unattributed" | "reasoning";
  label: string;
};
export type ParsedOutput = {
  entries: ReadableEntry[];
  details: SourceDetail[];
  chunks: OutputChunk[];
  rootNativeSessionRef: string | null;
  byteLength: number;
};
export type ParseOutputOptions = {
  startOffset?: number;
  streamKey?: string;
  isTerminal?: boolean;
  rootNativeSessionRef?: string | null;
};

const utf8 = new TextEncoder();
const byteLength = (text: string) => utf8.encode(text).byteLength;
const object = (value: unknown): Record<string, unknown> | null => (
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown> : null
);
const string = (value: unknown): string | null => typeof value === "string" ? value : null;

/** Same actor fields/conflict rule as harness_adapters._codex_item_actor. */
function actor(event: Record<string, unknown>, item: Record<string, unknown>) {
  const actors = new Set<string>();
  for (const owner of [event, item]) {
    for (const field of ["thread_id", "sender_thread_id"]) {
      const value = owner[field];
      if (value === undefined || value === null) continue;
      if (typeof value !== "string" || !value) return { conflict: true, ref: null };
      actors.add(value);
    }
  }
  return { conflict: actors.size > 1, ref: actors.values().next().value ?? null };
}

/** Replace refreshed pages, join only byte-contiguous pages from the same stream.
 * Gaps remain separate: neither a JSON record nor an actor can cross a gap.
 */
export function joinOutputChunks(chunks: readonly OutputChunk[]): OutputChunk[] {
  const latest = new Map<string, OutputChunk>();
  for (const chunk of chunks) {
    if (!Number.isSafeInteger(chunk.offset) || chunk.offset < 0) continue;
    latest.set(`${chunk.streamKey ?? ""}:${chunk.offset}`, { ...chunk });
  }
  const sorted = [...latest.values()].sort((left, right) => (
    (left.streamKey ?? "").localeCompare(right.streamKey ?? "") || left.offset - right.offset
  ));
  const groups: OutputChunk[] = [];
  for (const chunk of sorted) {
    const previous = groups.at(-1);
    if (previous && previous.streamKey === chunk.streamKey
      && previous.offset + byteLength(previous.text) === chunk.offset) {
      previous.text += chunk.text;
      previous.isTerminal = chunk.isTerminal;
    } else groups.push({ ...chunk });
  }
  return groups;
}

export function parseProviderOutput(rawText: string, options: ParseOutputOptions = {}): ParsedOutput {
  return parseOutputChunks([{
    text: rawText,
    offset: options.startOffset ?? 0,
    streamKey: options.streamKey,
    isTerminal: options.isTerminal,
  }], options);
}

/** Render native public messages only. Unscoped native items use the root learned
 * from a verified transport binding/thread.started, matching _root_agent_message
 * and _root_output. Explicit children and conflicting actors never enter the feed.
 */
export function parseOutputChunks(
  sourceChunks: readonly OutputChunk[],
  options: ParseOutputOptions = {},
): ParsedOutput {
  const chunks = joinOutputChunks(sourceChunks);
  const entries: ReadableEntry[] = [];
  const details: SourceDetail[] = [];
  const indexed = new Map<string, ReadableEntry>();
  let discoveredRoot = options.rootNativeSessionRef ?? null;

  for (const chunk of chunks) {
    let root = options.rootNativeSessionRef ?? null;
    let rootConflict = false;
    let turn = 0;
    let offset = chunk.offset;
    const lines = chunk.text.match(/[^\n]*\n|[^\n]+$/g) ?? [];
    const detail = (span: OutputSpan, kind: SourceDetail["kind"], label: string) => {
      details.push({ ...span, kind, label });
    };
    for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
      const raw = lines[lineIndex];
      const span = { offset, endOffset: offset + byteLength(raw), raw };
      offset = span.endOffset;
      if (!raw.trim()) continue;
      let event: Record<string, unknown> | null;
      try { event = object(JSON.parse(raw)); } catch {
        const atBoundary = (lineIndex === 0 && chunk.offset > 0)
          || (lineIndex === lines.length - 1 && !raw.endsWith("\n") && !chunk.isTerminal);
        detail(span, atBoundary ? "partial" : "unknown", atBoundary
          ? "跨页或尚未写完的记录 · 原文保留" : "暂未识别的原始输出");
        continue;
      }
      if (!event) {
        detail(span, "unknown", "未识别的记录格式");
        continue;
      }
      const type = string(event.type) ?? "未知事件";
      if (type === "thread.started") {
        const ref = string(event.thread_id);
        if (event.parent_thread_id != null && event.parent_thread_id !== "") {
          detail(span, "other-actor", "子智能体的会话记录");
        } else if (!ref || (root && root !== ref)) {
          rootConflict = true;
          detail(span, "unattributed", "会话身份不一致 · 不归入主智能体");
        } else {
          root = ref;
          discoveredRoot ??= ref;
          detail(span, "lifecycle", "输出会话已建立");
        }
        continue;
      }
      if (type === "turn.started") {
        turn += 1;
        detail(span, "lifecycle", "本次输出开始");
        continue;
      }
      if (type === "turn.completed" || type === "turn.failed" || type === "error") {
        detail(span, "lifecycle", type === "turn.completed" ? "本次输出结束" : "输出报告了错误");
        continue;
      }
      const item = object(event.item);
      if (!item || !["item.started", "item.updated", "item.completed"].includes(type)) {
        detail(span, "unknown", `其他记录 · ${type}`);
        continue;
      }
      const owner = actor(event, item);
      if (rootConflict || owner.conflict) {
        detail(span, "unattributed", "来源身份不一致 · 不归入主智能体");
        continue;
      }
      if (!root) {
        detail(span, "unattributed", "尚未取得本段主智能体身份 · 原文保留");
        continue;
      }
      if ((owner.ref !== null && owner.ref !== root)
        || (event.parent_thread_id != null && event.parent_thread_id !== "")
        || (item.parent_thread_id != null && item.parent_thread_id !== "")) {
        detail(span, "other-actor", "其他智能体的记录 · 未混入主输出");
        continue;
      }
      const itemType = string(item.type) ?? "未知类型";
      if (itemType === "reasoning" || item.channel === "analysis" || item.channel === "reasoning"
        || event.channel === "analysis" || item.visibility === "hidden") {
        detail(span, "reasoning", "非公开过程记录 · 未加入对话");
        continue;
      }
      const itemId = string(item.id);
      const id = `${chunk.streamKey ?? options.streamKey ?? "source"}:${chunk.offset}:${turn}:${itemType}:${itemId ?? span.offset}`;
      const existing = indexed.get(id);
      const completed = type === "item.completed";
      if (itemType === "agent_message") {
        const text = string(item.text) ?? string(item.content);
        if (text === null) {
          detail(span, "unknown", "消息未提供可显示的正文");
          continue;
        }
        if (existing?.kind === "message") {
          // Native message updates are snapshots, not deltas. Prefer a longer
          // cumulative snapshot; retain a changed final body exactly as received.
          if (!existing.text.startsWith(text) || completed) existing.text = text;
          existing.completed ||= completed;
          existing.spans.push(span);
        } else {
          const message: ReadableMessage = { kind: "message", id, text, completed, spans: [span] };
          entries.push(message);
          indexed.set(id, message);
        }
        continue;
      }
      if (itemType === "command_execution") {
        const output = string(item.aggregated_output) ?? string(item.output) ?? string(item.stdout);
        const command = string(item.command) ?? "";
        const status = string(item.status) ?? (completed ? "completed" : "in_progress");
        const exitCode = typeof item.exit_code === "number" && Number.isInteger(item.exit_code)
          ? item.exit_code : null;
        if (existing?.kind === "command") {
          if (command) existing.command = command;
          if (output !== null && !existing.output.startsWith(output)) {
            existing.outputReplaced ||= Boolean(existing.output && !output.startsWith(existing.output));
            existing.output = output;
          }
          existing.status = status;
          if (exitCode !== null) existing.exitCode = exitCode;
          existing.spans.push(span);
        } else {
          const record: ReadableCommand = {
            kind: "command", id, itemId, command, output: output ?? "", status, exitCode,
            outputReplaced: false, spans: [span],
          };
          entries.push(record);
          indexed.set(id, record);
        }
        continue;
      }
      if (["mcp_tool_call", "web_search", "file_change", "collab_tool_call", "tool_call", "todo_list"].includes(itemType)) {
        const name = string(item.tool) ?? string(item.name) ?? string(item.query) ?? itemType;
        const status = string(item.status) ?? (completed ? "completed" : "in_progress");
        if (existing?.kind === "tool") {
          existing.name = name;
          existing.status = status;
          existing.spans.push(span);
        } else {
          const tool: ReadableTool = { kind: "tool", id, name, status, spans: [span] };
          entries.push(tool);
          indexed.set(id, tool);
        }
      } else detail(span, "unknown", `其他记录 · ${itemType}`);
    }
  }
  return {
    entries, details, chunks, rootNativeSessionRef: discoveredRoot,
    byteLength: chunks.reduce((total, chunk) => total + byteLength(chunk.text), 0),
  };
}

/** Public structured messages are labels + exact supplied fields, never an LLM summary. */
export function publicMessageContent(text: string): {
  body: string | null;
  action: string | null;
  structured: boolean;
} {
  let value: Record<string, unknown> | null;
  try { value = object(JSON.parse(text)); } catch {
    return { body: text, action: null, structured: false };
  }
  if (!value) return { body: text, action: null, structured: false };
  const body = ["reply", "rationale", "message", "summary", "conclusion", "text"]
    .map((key) => string(value[key])).find((candidate) => Boolean(candidate?.trim())) ?? null;
  return { body, action: string(value.action), structured: true };
}

export function outputStatusLabel(status: string): string {
  return ({ in_progress: "执行中", running: "执行中", completed: "已结束", failed: "失败",
    cancelled: "已取消", pending: "等待执行", queued: "已排队" } as Record<string, string>)[status] ?? status;
}
