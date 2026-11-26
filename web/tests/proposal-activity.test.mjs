import assert from "node:assert/strict";
import test from "node:test";
import * as proposalActivity from "../src/proposalActivity.ts";

const { proposalActivities } = proposalActivity;
const proposalStreamState = (...args) => {
  assert.equal(typeof proposalActivity.proposalStreamState, "function", "proposalStreamState export missing");
  return proposalActivity.proposalStreamState(...args);
};

const line = value => JSON.stringify(value);

test("coalesces item.started, item.updated, and item.completed by item.id in place", () => {
  const stream = [
    line({ type: "item.started", item: { id: "cmd-1", type: "command_execution", command: "bash -lc 'ls -la /data/project'" } }),
    line({ type: "item.completed", item: { id: "cmd-1", type: "command_execution", command: "bash -lc 'ls -la /data/project'", exit_code: 0, status: "completed" } }),
  ].join("\n");
  const rows = proposalActivities(stream);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].key, "s0:cmd-1");
  assert.equal(rows[0].status, "completed");
  assert.match(rows[0].label, /浏览目录/);
  assert.doesNotMatch(rows[0].label, /bash -lc/);
});

test("renders concise reasoning summaries from text or summary array", () => {
  const stream = [
    line({ type: "item.completed", item: { id: "r-1", type: "reasoning", text: "**Inspecting repository layout**\nChecking existing scripts and README." } }),
    line({ type: "item.completed", item: { id: "r-2", type: "reasoning", summary: [{ type: "summary_text", text: "Comparing baseline metrics across runs" }] } }),
  ].join("\n");
  const rows = proposalActivities(stream);
  assert.equal(rows.length, 2);
  assert.match(rows[0].label, /Inspecting repository layout/);
  assert.match(rows[1].label, /Comparing baseline metrics across runs/);
});

test("maps command_execution semantically and uses calm neutral copy on non-zero exit", () => {
  const stream = [
    line({ type: "item.started", item: { id: "c-ls", type: "command_execution", command: "/bin/bash -lc \"find /mnt/work -maxdepth 2\"" } }),
    line({ type: "item.completed", item: { id: "c-head", type: "command_execution", command: "bash -lc 'head -n 50 /mnt/work/notes.md'", exit_code: 0 } }),
    line({ type: "item.completed", item: { id: "c-cp", type: "command_execution", command: "cp /mnt/work/data.csv ./tmp/data.csv", exit_code: 0 } }),
    line({ type: "item.completed", item: { id: "c-py", type: "command_execution", command: "python3 inspect_metrics.py --quick", exit_code: 0 } }),
    line({ type: "item.completed", item: { id: "c-fail", type: "command_execution", command: "bash -lc 'rg missing_pattern /mnt/work/notes.md'", status: "failed", exit_code: 1 } }),
  ].join("\n");
  const rows = proposalActivities(stream);
  assert.equal(rows.length, 5);
  assert.match(rows[0].label, /浏览目录/);
  assert.match(rows[1].label, /查看文件/);
  assert.match(rows[1].label, /notes\.md/);
  assert.match(rows[2].label, /复制/);
  assert.match(rows[3].label, /运行脚本检查/);
  assert.match(rows[3].label, /inspect_metrics\.py/);
  assert.notEqual(rows[4].status, "failed");
  assert.match(rows[4].label, /非零状态（已继续）/);
  assert.doesNotMatch(rows[4].label, /资料准备操作未完成/);
});

test("unwraps tools.call mcp_tool_call semantically with neutral error copy", () => {
  const stream = [
    line({
      type: "item.completed",
      item: {
        id: "mcp-1",
        type: "mcp_tool_call",
        tool: "tools.call",
        arguments: { operation_id: "research_workspace.materials.discover", arguments: { reference_ref: "material:abc", path: "experiments" } },
      },
    }),
    line({
      type: "item.completed",
      item: {
        id: "mcp-2",
        type: "mcp_tool_call",
        tool: "tools.call",
        arguments: { operation_id: "research_workspace.materials.read", arguments: { reference_ref: "material:abc", path: "experiments/summary.md" } },
        status: "failed",
      },
    }),
  ].join("\n");
  const rows = proposalActivities(stream);
  assert.equal(rows.length, 2);
  assert.match(rows[0].label, /目录/);
  assert.match(rows[0].label, /experiments/);
  assert.notEqual(rows[1].status, "failed");
  assert.match(rows[1].label, /已继续/);
  assert.match(rows[1].label, /summary\.md/);
});

test("drops empty or JSON agent_message spam completely while preserving natural prose", () => {
  const stream = [
    line({ type: "item.completed", item: { id: "m-json", type: "agent_message", text: '{"claims_and_conditions":[],"coverage":[],"selection":[]}' } }),
    line({ type: "item.completed", item: { id: "m-arr", type: "agent_message", text: '[{"a":1}]' } }),
    line({ type: "item.completed", item: { id: "m-empty", type: "agent_message", text: "   " } }),
    line({ type: "item.completed", item: { id: "m-prose", type: "agent_message", phase: "commentary", text: "已查看实验脚本与指标日志，正在整理研究边界。" } }),
  ].join("\n");
  const rows = proposalActivities(stream);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].key, "s0:m-prose");
  assert.equal(rows[0].label, "已查看实验脚本与指标日志，正在整理研究边界。");
  assert.ok(rows.every(row => !row.label.includes("当前工作说明已更新")));
});

test("shows reconnect retry progress and clears reconnecting flag on recovery", () => {
  const during = [
    line({ type: "turn.started" }),
    line({ type: "error", message: "Reconnecting... 2/5" }),
  ].join("\n");
  const state1 = proposalStreamState(during);
  assert.ok(state1.reconnecting && /2\/5/.test(state1.reconnecting));
  assert.ok(state1.activities.some(row => /网络连接重试中（2\/5）/.test(row.label)));

  const recovered = during + "\n" + line({ type: "item.completed", item: { id: "r-ok", type: "reasoning", text: "Connection restored, continuing review." } });
  const state2 = proposalStreamState(recovered);
  assert.equal(state2.reconnecting, null);
});

test("retains more than 12 scrollable history rows up to 200", () => {
  const lines = [];
  for (let i = 1; i <= 35; i += 1) {
    lines.push(line({ type: "item.completed", item: { id: `r-${i}`, type: "reasoning", text: `Step ${i}` } }));
  }
  const rows = proposalActivities(lines.join("\n"));
  assert.equal(rows.length, 35);
  assert.equal(rows[0].key, "s0:r-1");
  assert.equal(rows[34].key, "s0:r-35");
});

test("scopes item.id per segment across concatenated understanding and drafting streams without overwriting", () => {
  const stream = [
    // Segment 1: initialization-understanding
    line({ type: "thread.started", thread_id: "t-understanding" }),
    line({ type: "turn.started" }),
    line({ type: "item.started", item: { id: "item_0", type: "command_execution", command: "ls /data/project" } }),
    line({ type: "item.completed", item: { id: "item_0", type: "command_execution", command: "ls /data/project", exit_code: 0 } }),
    line({ type: "item.completed", item: { id: "item_1", type: "reasoning", summary: ["Finished reading mounted repo"] } }),
    line({ type: "turn.completed" }),
    // Segment 2: first-question-synthesis (reuses item_0 and item_1)
    line({ type: "thread.started", thread_id: "t-drafting" }),
    line({ type: "turn.started" }),
    line({ type: "item.started", item: { id: "item_0", type: "reasoning", summary: ["Drafting candidate first question"] } }),
    line({ type: "item.completed", item: { id: "item_0", type: "reasoning", summary: ["Drafted first question proposal"] } }),
    line({ type: "item.completed", item: { id: "item_1", type: "command_execution", command: "head -n 20 /data/project/README.md", exit_code: 0 } }),
    line({ type: "turn.completed" }),
  ].join("\n");

  const rows = proposalActivities(stream);
  const itemRows = rows.filter(row => row.key.includes(":item_"));
  assert.equal(itemRows.length, 4);
  assert.deepEqual(itemRows.map(row => row.key), ["s2:item_0", "s2:item_1", "s4:item_0", "s4:item_1"]);
  assert.match(itemRows[0].label, /已浏览目录/);
  assert.match(itemRows[1].label, /Finished reading mounted repo/);
  assert.match(itemRows[2].label, /Drafted first question proposal/);
  assert.match(itemRows[3].label, /已查看文件/);
  // All keys across the entire stream must be distinct and in chronological order.
  assert.equal(new Set(rows.map(row => row.key)).size, rows.length);
});

test("extractReasoningText prefers summary over text without duplicating identical strings", () => {
  const stream = [
    line({
      type: "item.completed",
      item: {
        id: "r-dup",
        type: "reasoning",
        summary: ["Inspecting baseline logs", "Inspecting baseline logs"],
        text: "Inspecting baseline logs",
      },
    }),
    line({
      type: "item.completed",
      item: {
        id: "r-prefer",
        type: "reasoning",
        summary: [{ type: "summary_text", text: "Concise summary wins" }],
        text: "Verbose fallback text should not be appended",
      },
    }),
  ].join("\n");

  const rows = proposalActivities(stream);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].label, "思路：Inspecting baseline logs");
  assert.equal(rows[1].label, "思路：Concise summary wins");
});
