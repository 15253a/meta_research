import { expect, test } from "@playwright/test";
import type { CompanionMessage, HumanRequestItem } from "../src/api";
import {
  HANDOFF_DOCUMENT_PROMPT,
  buildHumanRequestHandoff,
  getHumanRequestHandoff,
  type CompletedHumanRequestHandoff,
} from "../src/humanRequestHandoff";

const request: HumanRequestItem = {
  request_ref: "research_graph:HR-52:r3",
  request_id: "HR-52",
  revision: 3,
  issuer: "research_graph",
  quest_ref: "quest_test",
  kind: "offline_action",
  status: "open",
  obligation: '先读取设备 "OmniHand" 的信息，不执行运动命令。',
  business_purpose: "取得两只手的可核查原始记录。",
  acceptance_conditions: ["包含 UTC 时间与逐手标识。", "记录停止与恢复条件。"],
};
const scopeRef = "quest:quest_test";

function message(
  interaction: string,
  role: "user" | "assistant",
  content: string,
  status: CompanionMessage["status"] = "completed",
): CompanionMessage {
  return {
    message_ref: `${interaction}:${role}`, role, content, status,
    scope_ref: scopeRef, created_at: 1_790_294_400,
    view_context: {
      kind: "human_request", quest_ref: "quest_test",
      request_ref: request.request_ref, revision: request.revision,
    },
  };
}

function completed(content = "## 现在先做这一步\n\n只读核对设备标识。") {
  const result = getHumanRequestHandoff([
    message("doc-1", "user", HANDOFF_DOCUMENT_PROMPT),
    message("doc-1", "assistant", content),
  ], request, scopeRef);
  expect(result.status).toBe("completed");
  if (result.status !== "completed") throw new Error("Expected completed handoff");
  return result;
}

test("exports the exact generation reply, never the most recent ordinary chat", () => {
  const result = getHumanRequestHandoff([
    message("doc-1", "user", HANDOFF_DOCUMENT_PROMPT),
    message("doc-1", "assistant", "完整的交接说明"),
    message("chat-2", "user", "第一步是什么意思？"),
    message("chat-2", "assistant", "只是普通聊天。"),
  ], request, scopeRef);
  expect(result).toMatchObject({
    status: "completed", content: "完整的交接说明", messageRef: "doc-1:assistant",
    requestRef: request.request_ref, revision: 3, scopeRef,
  });
  expect(getHumanRequestHandoff([message("chat-2", "assistant", "普通聊天")], request, scopeRef))
    .toEqual({ status: "idle" });
});

test("requires exact quest, request, revision, scope, and interaction pairing", () => {
  const generation = message("doc-1", "user", HANDOFF_DOCUMENT_PROMPT);
  const reply = message("doc-1", "assistant", "不能跨事项下载");
  const variants: CompanionMessage[] = [
    { ...reply, scope_ref: "quest:other" },
    { ...reply, view_context: null },
    { ...reply, view_context: { kind: "human_request", quest_ref: "other", request_ref: request.request_ref, revision: 3 } },
    { ...reply, view_context: { kind: "human_request", quest_ref: "quest_test", request_ref: "other", revision: 3 } },
    { ...reply, view_context: { kind: "human_request", quest_ref: "quest_test", request_ref: request.request_ref, revision: 2 } },
    { ...reply, message_ref: "other:assistant" },
  ];
  for (const other of variants) {
    expect(getHumanRequestHandoff([generation, other], request, scopeRef)).toEqual({ status: "pending" });
  }
  expect(getHumanRequestHandoff([generation, reply], { ...request, revision: 4 }, scopeRef)).toEqual({ status: "idle" });
  expect(getHumanRequestHandoff([generation, reply], request, null)).toEqual({ status: "idle" });
});

test("latest pending or failed generation never silently falls back to the old document", () => {
  const old = [message("old", "user", HANDOFF_DOCUMENT_PROMPT), message("old", "assistant", "旧版文档")];
  for (const status of ["queued", "processing", "running", undefined] as const) {
    const reply = { ...message("new", "assistant", "未完成的新文档"), status };
    expect(getHumanRequestHandoff([...old, message("new", "user", HANDOFF_DOCUMENT_PROMPT), reply], request, scopeRef))
      .toEqual({ status: "pending" });
  }
  expect(getHumanRequestHandoff([
    ...old, message("new", "user", HANDOFF_DOCUMENT_PROMPT), message("new", "assistant", "生成失败", "failed"),
  ], request, scopeRef)).toEqual({ status: "failed" });
  expect(getHumanRequestHandoff([
    ...old, message("new", "user", HANDOFF_DOCUMENT_PROMPT), message("new", "assistant", " \n "),
  ], request, scopeRef)).toEqual({ status: "failed" });
  expect(getHumanRequestHandoff([
    ...old, { ...message("new", "user", HANDOFF_DOCUMENT_PROMPT), message_ref: undefined },
  ], request, scopeRef)).toEqual({ status: "pending" });
});

test("document preserves assistant text and original request with provenance and a result-return reminder", () => {
  const body = '## 第一步\n\n```powershell\nWrite-Output "C:\\研究\\资料"\n```\n\n原话：\"不要移动\"；来源：https://example.org/manual?a=1&b=2';
  const result = buildHumanRequestHandoff(request, completed(body), new Date("2026-09-25T12:30:00Z"));
  expect(result.markdown).toContain(body);
  expect(result.markdown).toContain(JSON.stringify(request.obligation));
  expect(result.markdown).toContain(JSON.stringify(request.acceptance_conditions![0]));
  expect(result.markdown).toContain('"request_ref": "research_graph:HR-52:r3"');
  expect(result.markdown).toContain('"revision": 3');
  expect(result.markdown).toContain('"source_message_ref": "doc-1:assistant"');
  expect(result.markdown).toContain('"exported_at": "2026-09-25T12:30:00.000Z"');
  expect(result.markdown).toContain("事项与回应");
  expect(result.markdown).toContain("不等于已经提交正式回应");
  expect(result.filenameBase).toBe("研究协作说明-HR-52-r3-2026-09-25");
  expect(result.html).toContain("<h2>第一步</h2>");
  expect(result.html).toContain("<pre><code>Write-Output &quot;C:\\研究\\资料&quot;</code></pre>");
});

test("offline reading version cannot execute source HTML or load remote images, styles, scripts", () => {
  const body = '<script>alert("bad")</script>\n<img src="https://evil.invalid/pixel" onerror="alert(1)">\n<iframe src="https://evil.invalid"></iframe>\n\n![远程图](https://evil.invalid/image.png)\n\n[不安全链接](javascript:alert(1))\n\n```html\n</code></pre><script>bad()</script>\n```';
  const result = buildHumanRequestHandoff(request, completed(body));
  expect(result.markdown).toContain(body);
  expect(result.html).not.toMatch(/<(script|img|iframe|object|embed|link)\b/i);
  expect(result.html).not.toMatch(/<[^>]*\s(?:src|onerror|onclick)=/i);
  expect(result.html).not.toMatch(/<[^>]*href="(?!https?:)/i);
  expect(result.html).toContain("&lt;script&gt;alert(&quot;bad&quot;)&lt;/script&gt;");
  expect(result.html).toContain("&lt;/code&gt;&lt;/pre&gt;&lt;script&gt;bad()&lt;/script&gt;");
  expect(result.html).toContain("default-src 'none'");
  expect(result.html).toContain("@media print");
});

test("offline reading version formats steps and safe source links without remote embeds", () => {
  const body = "## 当前第一步\n\n请检查 **设备标识**，使用 `read_id()`。\n\n- 先取得访问地址\n- 查看 [官方文档](https://example.org/manual?a=1&b=2)\n\n1. 保存原始输出\n2. 记录时间\n\n![图片](https://example.org/image.png)";
  const html = buildHumanRequestHandoff(request, completed(body)).html;
  expect(html).toContain("<strong>设备标识</strong>");
  expect(html).toContain("<code>read_id()</code>");
  expect(html).toContain('<a href="https://example.org/manual?a=1&amp;b=2" rel="noreferrer noopener">官方文档</a>');
  expect(html).toContain("<ul><li>先取得访问地址</li><li>查看 ");
  expect(html).toContain('<ol start="1"><li>保存原始输出</li><li>记录时间</li></ol>');
  expect(html).toContain("![图片](https://example.org/image.png)");
  expect(html).not.toContain("<img");
});

test("export filenames are portable and original appendix omits unrelated internal receipts", () => {
  const unusual: HumanRequestItem = {
    ...request, request_id: '../CON:文件\\/"*?<>|\u0000\u202etest . ',
    target_assertion: { secret_internal_binding: "internal-do-not-export", condition: { safe_response: "先只读查看", impact: "缺少访问", runtime_ref: "internal-do-not-export" } },
    open_effect: undefined,
    required_authorization: { private_receipt: "internal-do-not-export" },
    responses: [{ internal_response_ref: "internal-do-not-export" }],
  };
  const result = buildHumanRequestHandoff(unusual, completed());
  expect(result.filenameBase).not.toMatch(/[<>:"/\\|?*\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/);
  expect(result.filenameBase.length).toBeLessThan(120);
  expect(result.markdown).not.toContain("internal-do-not-export");
  expect(result.markdown).toContain("先只读查看");
  expect(() => buildHumanRequestHandoff({ ...request, revision: 4 }, completed()))
    .toThrow("human_request_handoff_scope_stale");
});

test("missing timestamps are identified rather than invented", () => {
  const reply: CompletedHumanRequestHandoff = { ...completed(), createdAt: null };
  expect(buildHumanRequestHandoff(request, reply).markdown).toContain('"assistant_generated_at": "未记录"');
});
