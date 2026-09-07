import assert from "node:assert/strict";
import test from "node:test";
import {
  formatExperimentLogText, mergeExperimentLogPage, validateExperimentLogList, validateExperimentLogPage,
} from "../src/experimentLogModel.ts";

const encoder = new TextEncoder();
const fixture = (text, offset = 0, extra = {}) => ({
  schema_ref: "meta-research/experiment-log-page/v1", target_ref: "target-1", target_run_ref: "run-1",
  workspace_ref: "workspace-1", log_ref: "log-1", name: "train.log", relative_path: "logs/train.log", kind: "train",
  stream_ref: "file-generation-1", text, offset, next_offset: offset + encoder.encode(text).length,
  source_bytes: offset + encoder.encode(text).length, modified_at: 100, has_more: false,
  source_caught_up: true, pending_utf8_bytes: 0, decode_replacements: false, ...extra,
});

test("a tail response remains at its true large-file source offset", () => {
  const page = fixture("epoch 900\n", 1_000_000_000);
  const window = mergeExperimentLogPage(null, validateExperimentLogPage(page, "target-1", "run-1", "log-1"));
  assert.equal(window.text, "epoch 900\n");
  assert.equal(window.startOffset, 1_000_000_000);
  assert.equal(window.nextOffset, page.next_offset);
});

test("contiguous growth retains exact Unicode, CR, ANSI, blank lines and source cursor", () => {
  const first = fixture("中文\n\x1b[32m10%\r", 200);
  const second = fixture("20%\x1b[0m\n\n", first.next_offset);
  const window = mergeExperimentLogPage(mergeExperimentLogPage(null, first), second);
  assert.equal(window.text, first.text + second.text);
  assert.equal(window.startOffset, 200);
  assert.equal(window.nextOffset, second.next_offset);
});

test("rotation, target/run change, file switch and a byte gap cannot mix windows", () => {
  const current = mergeExperimentLogPage(null, fixture("old\n"));
  for (const change of [
    { stream_ref: "generation-2" }, { target_ref: "target-2" }, { target_run_ref: "run-2" },
    { log_ref: "eval-log" }, { offset: current.nextOffset + 1 },
  ]) {
    assert.throws(() => mergeExperimentLogPage(current, fixture("new\n", current.nextOffset, change)), /experiment_log_reset_required/);
  }
  const replacement = mergeExperimentLogPage(null, fixture("new file\n", 0, { stream_ref: "generation-2" }));
  assert.equal(replacement.text, "new file\n");
  assert.ok(!replacement.text.includes("old"));
});

test("window eviction is bounded and never splits UTF-8 characters", () => {
  const first = fixture("甲乙丙丁", 300);
  const current = mergeExperimentLogPage(null, first, 8);
  assert.equal(current.text, "丙丁");
  assert.equal(current.startOffset, 306);
  assert.equal(current.trimmed, true);
  assert.equal(encoder.encode(current.text).length, 6);
  assert.equal(current.startOffset + encoder.encode(current.text).length, current.nextOffset);
});

test("invalid UTF-8 replacement does not create a fake source offset on eviction", () => {
  const first = fixture("a�b", 100, { next_offset: 103, source_bytes: 103, decode_replacements: true });
  const current = mergeExperimentLogPage(null, first);
  const next = fixture("中间", 103);
  const window = mergeExperimentLogPage(current, next, 8);
  assert.equal(window.text, next.text);
  assert.equal(window.startOffset, 103);
  assert.equal(window.startOffsetExact, true);
  assert.equal(window.decodeReplacements, false);
  assert.equal(window.trimmed, true);
});

test("incomplete UTF-8 at EOF waits at the returned cursor without adding fake text", () => {
  const first = fixture("epoch=1\n", 0, { source_bytes: 10, pending_utf8_bytes: 2 });
  const window = mergeExperimentLogPage(null, first);
  assert.equal(window.pendingUtf8Bytes, 2);
  assert.equal(window.nextOffset, 8);
  const second = fixture("中", 8, { source_bytes: 11 });
  assert.equal(mergeExperimentLogPage(window, second).text, "epoch=1\n中");
});

test("list/page target binding and unknown file identity are rejected", () => {
  const list = {
    schema_ref: "meta-research/experiment-log-list/v1", target_ref: "target-1", target_run_ref: "run-1",
    workspace_ref: "workspace-1", status: "empty", logs: [], default_log_ref: null, truncated: false, reason: null,
  };
  assert.equal(validateExperimentLogList(list, "target-1", "run-1"), list);
  assert.throws(() => validateExperimentLogList(list, "target-1", "run-2"), /identity_invalid/);
  assert.throws(() => validateExperimentLogPage(fixture("x"), "target-1", "run-1", "other-log"), /identity_invalid/);
});

test("safe text terminal rendering supports tqdm CR and ANSI without emitting active HTML or links", () => {
  assert.equal(formatExperimentLogText("\x1b[32m10%\r20%\x1b[0m\n"), "20%\n");
  assert.equal(formatExperimentLogText("long progress\r\x1b[2Kdone\n"), "done\n");
  assert.equal(formatExperimentLogText("a\r\nb\n\n"), "a\nb\n\n");
  assert.equal(formatExperimentLogText("\x1b]8;;javascript:bad\x07label\x1b]8;;\x07"), "label");
  assert.equal(formatExperimentLogText("<script>plain text</script>"), "<script>plain text</script>");
  assert.equal(formatExperimentLogText("中文\r甲"), "甲文");
});
