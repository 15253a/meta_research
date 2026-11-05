import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = readFileSync(resolve("src/ExecutionElapsed.tsx"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
function render(state: string, ended = false) {
  const exports: Record<string, any> = {};
  runInNewContext(code, { exports, require: (name: string) => name === "react" ? {
    ...React, useEffect: () => {}, useState: () => [1_000, () => {}],
  } : jsxRuntime, Date, Math, performance: { now: () => 1_000 } });
  return renderToStaticMarkup(React.createElement(exports.ExecutionElapsed, {
    sample: { sourceUpdatedAt: 1_700_000_000, observedAt: 1_700_000_581, receivedAt: 1_000 }, state, ended,
  }));
}
test("waiting and paused sessions show a stable last record instead of an apparent stalled clock", () => {
  for (const [state, label] of [["waiting", "等待继续"], ["paused", "已暂停"], ["failed", "执行受阻"], ["unknown", "状态待确认"]]) {
    const html = render(state);
    expect(html).toContain(label);
    expect(html).toContain("最后记录于");
    expect(html).not.toContain("581");
  }
});
test("an executing session retains the real output age even while the service is responsive", () => {
  const html = render("executing");
  expect(html).toContain("距上次执行记录");
  expect(html).toContain("581");
  expect(html).toContain("不代表进程卡住");
});
test("terminal output remains ended even if its session has moved on", () => {
  expect(render("executing", true)).toContain("输出已结束");
  expect(render("executing", true)).not.toContain("581");
});
