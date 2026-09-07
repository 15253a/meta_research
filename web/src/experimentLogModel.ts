export type ExperimentLogFile = {
  log_ref: string;
  name: string;
  relative_path: string;
  kind: "train" | "eval";
  source_bytes: number;
  modified_at: number;
  stream_ref?: string;
};
export type ExperimentLogList = {
  schema_ref: "meta-research/experiment-log-list/v1";
  target_ref: string;
  target_run_ref: string | null;
  workspace_ref: string | null;
  status: "ready" | "empty" | "unavailable";
  logs: ExperimentLogFile[];
  default_log_ref: string | null;
  truncated: boolean;
  reason: { code: string } | null;
};
export type ExperimentLogPage = ExperimentLogFile & {
  schema_ref: "meta-research/experiment-log-page/v1";
  target_ref: string;
  target_run_ref: string;
  workspace_ref: string;
  stream_ref: string;
  text: string;
  offset: number;
  next_offset: number;
  has_more: boolean;
  source_caught_up: boolean;
  pending_utf8_bytes: number;
  decode_replacements?: boolean;
};
export type ExperimentLogWindow = {
  targetRef: string;
  targetRunRef: string;
  logRef: string;
  streamRef: string;
  text: string;
  startOffset: number;
  nextOffset: number;
  sourceBytes: number;
  modifiedAt: number;
  pendingUtf8Bytes: number;
  trimmed: boolean;
  startOffsetExact: boolean;
  decodeReplacements: boolean;
};

export const LOG_PAGE_BYTES = 64 * 1024;
export const LOG_WINDOW_BYTES = 512 * 1024;
const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });
const validOffset = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

export function validateExperimentLogList(
  value: ExperimentLogList, targetRef: string, targetRunRef: string | null | undefined,
): ExperimentLogList {
  if (value.schema_ref !== "meta-research/experiment-log-list/v1"
    || value.target_ref !== targetRef || value.target_run_ref !== targetRunRef
    || !["ready", "empty", "unavailable"].includes(value.status) || !Array.isArray(value.logs)
    || value.logs.some(log => !log || typeof log.log_ref !== "string" || !log.log_ref
      || typeof log.name !== "string" || typeof log.relative_path !== "string"
      || !["train", "eval"].includes(log.kind) || !validOffset(log.source_bytes)
      || typeof log.modified_at !== "number" || !Number.isFinite(log.modified_at))) {
    throw new Error("experiment_log_identity_invalid");
  }
  return value;
}

export function validateExperimentLogPage(
  value: ExperimentLogPage, targetRef: string, targetRunRef: string | null | undefined, logRef: string,
): ExperimentLogPage {
  if (value.schema_ref !== "meta-research/experiment-log-page/v1"
    || value.target_ref !== targetRef || value.target_run_ref !== targetRunRef || value.log_ref !== logRef
    || typeof value.stream_ref !== "string" || !value.stream_ref || typeof value.text !== "string"
    || !validOffset(value.offset) || !validOffset(value.next_offset) || !validOffset(value.source_bytes)
    || value.next_offset < value.offset || value.next_offset > value.source_bytes
    || !validOffset(value.pending_utf8_bytes) || value.pending_utf8_bytes > 3
    || typeof value.modified_at !== "number" || !Number.isFinite(value.modified_at)
    || encoder.encode(value.text).length > 256 * 1024) {
    throw new Error("experiment_log_identity_invalid");
  }
  return value;
}

/** Only a contiguous page of this exact file generation may append to a window.
 * Prefix eviction is byte bounded and avoids splitting UTF-8 characters.
 */
export function mergeExperimentLogPage(
  current: ExperimentLogWindow | null, page: ExperimentLogPage,
  maximumBytes = LOG_WINDOW_BYTES,
): ExperimentLogWindow {
  if (current && (current.targetRef !== page.target_ref || current.targetRunRef !== page.target_run_ref
    || current.logRef !== page.log_ref || current.streamRef !== page.stream_ref
    || current.nextOffset !== page.offset)) {
    throw new Error("experiment_log_reset_required");
  }
  let text = (current?.text ?? "") + page.text;
  let startOffset = current?.startOffset ?? page.offset;
  let trimmed = current?.trimmed ?? false;
  let startOffsetExact = current?.startOffsetExact ?? true;
  let decodeReplacements = Boolean(current?.decodeReplacements || page.decode_replacements);
  const bytes = encoder.encode(text);
  if (bytes.length > maximumBytes) {
    if (decodeReplacements || !startOffsetExact) {
      // Replacement characters do not have the same byte length as their
      // source. Keep a whole validated page rather than inventing a cut offset.
      text = page.text;
      startOffset = page.offset;
      startOffsetExact = true;
      decodeReplacements = Boolean(page.decode_replacements);
    } else {
      let cut = bytes.length - maximumBytes;
      while (cut < bytes.length && (bytes[cut] & 0xc0) === 0x80) cut += 1;
      text = decoder.decode(bytes.subarray(cut));
      startOffset += cut;
    }
    trimmed = true;
  }
  return {
    targetRef: page.target_ref, targetRunRef: page.target_run_ref, logRef: page.log_ref,
    streamRef: page.stream_ref, text, startOffset, nextOffset: page.next_offset,
    sourceBytes: page.source_bytes, modifiedAt: page.modified_at,
    pendingUtf8Bytes: page.pending_utf8_bytes, trimmed, startOffsetExact, decodeReplacements,
  };
}

/** Plain text terminal presentation only: no HTML, links, styles, or control
 * sequences are executed. Original bytes/text remain available in raw view.
 * CR rewrites the current line, as used by tqdm; common SGR is presentation-only.
 */
export function formatExperimentLogText(raw: string): string {
  const text = raw.replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "");
  const completed: string[] = [];
  let line: string[] = [];
  let cursor = 0;
  for (let index = 0; index < text.length;) {
    if (text[index] === "\x1b") {
      const control = /^\x1b\[([\d;?]*)([A-Za-z])/.exec(text.slice(index));
      if (control && control[2] === "m") { index += control[0].length; continue; }
      if (control && control[2] === "K") {
        const mode = Number(control[1] || "0");
        if (mode === 2) line = [];
        else if (mode === 0) line.length = cursor;
        else if (mode === 1) for (let col = 0; col < cursor; col += 1) line[col] = " ";
        index += control[0].length;
        continue;
      }
      line[cursor++] = "␛";
      index += 1;
      continue;
    }
    const point = text.codePointAt(index)!;
    const character = String.fromCodePoint(point);
    index += character.length;
    if (character === "\r") cursor = 0;
    else if (character === "\n") { completed.push(line.join("")); line = []; cursor = 0; }
    else if (character === "\b") cursor = Math.max(0, cursor - 1);
    else line[cursor++] = character;
  }
  completed.push(line.join(""));
  return completed.join("\n");
}
