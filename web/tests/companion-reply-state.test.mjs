import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require("typescript");
const path = new URL("../src/HumanCollaboration.tsx", import.meta.url);
const source = readFileSync(path, "utf8");
const parsed = ts.createSourceFile(path.pathname, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = new Set(["messageText", "CompanionReplyContent"]);
const nodes = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.has(node.name?.text));
assert.equal(nodes.length, names.size);
const javascript = ts.transpileModule(nodes.map(node => node.getText(parsed)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText;

function render(message, preview = null) {
  const context = { React, useRef: React.useRef, useEffect: React.useEffect, useState: React.useState,
    useReplyStream: () => preview, scrollToConversationMessage: () => {} };
  runInNewContext(javascript, context, { filename: path.pathname });
  return renderToStaticMarkup(React.createElement(context.CompanionReplyContent, {
    message: { message_ref: "original-interaction:assistant", role: "assistant", content: "", ...message },
    scopeRef: "quest:original-quest", onChanged: () => {},
  }));
}

test("a durable failed Companion reply with empty content displays failure and its reason", () => {
  const html = render({ status: "failed", reason: { code: "codex_operation_failed" } });
  assert.match(html, /本次回复未完成/);
  assert.match(html, /查看原因代码/);
  assert.match(html, /aria-expanded="false"/);
  assert.doesNotMatch(html, /正在思考|正在回复/);
});

test("a failed stream remains visible while the durable snapshot is still processing", () => {
  const html = render({ status: "processing" }, { text: "已读取第一份材料。", status: "failed" });
  assert.match(html, /已读取第一份材料/);
  assert.match(html, /本次回复未完成/);
  assert.doesNotMatch(html, /正在思考|正在回复/);
});

test("a normal pending reply continues to show progress", () => {
  assert.match(render({ status: "processing" }), /Codex 正在思考/);
  assert.match(render({ status: "processing" }, { text: "正在核对材料。", status: "processing" }), /正在回复/);
});

test("a completed reply replaces the preview without a failure notice", () => {
  const html = render({ status: "completed", content: "已核对，仍缺性能数据。" }, { text: "旧片段", status: "failed" });
  assert.match(html, /已核对，仍缺性能数据/);
  assert.doesNotMatch(html, /未完成|正在思考|正在回复|旧片段/);
});
