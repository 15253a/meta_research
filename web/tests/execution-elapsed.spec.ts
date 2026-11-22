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
function render(state: string, ended = false, extra: Record<string, unknown> = {}) {
  const exports: Record<string, any> = {};
  runInNewContext(code, { exports, require: (name: string) => name === "react" ? {
    ...React, useEffect: () => {}, useState: () => [1_000, () => {}],
  } : jsxRuntime, Date, Math, performance: { now: () => 1_000 } });
  return renderToStaticMarkup(React.createElement(exports.ExecutionElapsed, {
    sample: { sourceUpdatedAt: 1_700_000_000, observedAt: 1_700_000_581, receivedAt: 1_000 }, state, ended, ...extra,
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
test("ended and executing runs show factual run duration when start/end bounds exist", () => {
  const endedHtml = render("completed", true, { startedAt: 1_699_999_246, endedAt: 1_700_000_000 });
  expect(endedHtml).toContain("输出已结束");
  expect(endedHtml).toContain("上次运行 12 分 34 秒");
  expect(endedHtml).toContain("最后记录于");
  expect(endedHtml).not.toContain("581");
  const liveHtml = render("executing", false, { startedAt: 1_699_999_900 });
  expect(liveHtml).toContain("已运行 11 分 21 秒");
  expect(liveHtml).toContain("距上次执行记录 <b>581</b> 秒");
});
