import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import type { RootOperation, RootOutput } from "../src/rootSessionsApi.js";

// Render the real operation header with the public-output read at its React
// state seam. No browser, network request, or research runtime is started.
const sourcePath = resolve(process.cwd(), "src/RootConversations.tsx");
const source = readFileSync(sourcePath, "utf8");
const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const nodes = parsed.statements.filter(node =>
  ts.isFunctionDeclaration(node) && node.name?.text === "RootOperationOutput"
  || ts.isVariableStatement(node) && node.declarationList.declarations.some(declaration => declaration.name.getText(parsed) === "timeText"));
expect(nodes).toHaveLength(2);
const javascript = ts.transpileModule(nodes.map(node => node.getText(parsed)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText;

const outputTime = Date.parse("2026-09-29T06:51:28+08:00") / 1_000;
const rescheduledTime = Date.parse("2026-09-29T12:41:30+08:00") / 1_000;
const formatted = (time: number) => new Date(time * 1_000).toLocaleString("zh-CN", { hour12: false });
function header(sourceUpdatedAt: number | null | undefined, createdAt = rescheduledTime) {
  const page: RootOutput | null = sourceUpdatedAt === undefined ? null : {
    schema_ref: "meta-research/root-session-output/v1", quest_ref: "quest", session_ref: "session", operation_ref: "operation",
    stream_ref: "stream", text: "已保存的公开输出", offset: 0, next_offset: 0, source_bytes: 0,
    has_more: false, source_caught_up: true, source_updated_at: sourceUpdatedAt, observed_at: rescheduledTime,
    native_session_ref: null, status: "terminal",
  };
  let slot = 0;
  const sandbox = {
    React, Date, Number,
    useState: (initial: unknown) => [slot++ === 1 ? page : typeof initial === "function" ? initial() : initial, () => {}],
    useRef: (initial: unknown) => ({ current: initial }), useEffect: () => {},
    StageReadableOutput: () => null, ExecutionElapsed: () => null,
    RootOperationOutput: null as unknown as (props: { questRef: string; sessionRef: string; operation: RootOperation; ordinal: number; active: boolean; onOutput: () => void; readingCache: Map<string, unknown> }) => React.ReactElement<{ children: React.ReactNode[] }>,
  };
  runInNewContext(javascript, sandbox, { filename: sourcePath });
  const operation: RootOperation = { operation_ref: "operation", label: "Bundle", phase: null, status: "completed", created_at: createdAt, updated_at: createdAt };
  const article = sandbox.RootOperationOutput({ questRef: "quest", sessionRef: "session", operation, ordinal: 5, active: true, onOutput: () => {}, readingCache: new Map() });
  return renderToStaticMarkup(article.props.children[0]);
}

test("a saved output shows its source update, not the later Owner reschedule time", () => {
  const rendered = header(outputTime);
  expect(rendered).toContain("工作记录 5 · Bundle");
  expect(rendered).toContain(`输出更新 · ${formatted(outputTime)}`);
  expect(rendered).not.toContain(formatted(rescheduledTime));
});

test("later Owner reschedules do not make the same output appear newer", () => {
  expect(header(outputTime, rescheduledTime + 3_600)).toBe(header(outputTime));
});

test("before output loading there is no invented call or output timestamp", () => {
  expect(header(undefined)).toContain("输出时间待确认");
  expect(header(undefined)).not.toContain(formatted(rescheduledTime));
});

test("missing or invalid output times never fall back to Owner scheduling", () => {
  for (const time of [null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
    expect(header(time)).toContain("输出时间待确认");
    expect(header(time)).not.toContain(formatted(rescheduledTime));
  }
});
