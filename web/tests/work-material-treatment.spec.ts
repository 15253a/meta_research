import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

test("guidance materials separate receipt and byte reads from root treatment and selected custody", async ({ page }, testInfo) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const companion = snapshot.human_collaboration.companion;
  const questRef = companion.scope_ref.replace(/^quest:/, "");
  const receiver = { kind: "current", quest_ref: questRef, run_ref: "target-run-183", root_session_ref: "target-root-183", workspace_ref: "workspace-183" };
  const processedBy = { root_kind: "target", run_ref: "target-run-183", root_session_ref: "target-root-183", workspace_ref: "workspace-183" };
  const reference = (name: string, read: boolean, actor: string) => ({
    reference_ref: `work-material-${name}`, submission_ref: "submission-183", receiver,
    source: {
      server: { server_ref: "fixture-server", hostname: "research-host", platform: "linux", permission_context: "researcher" },
      absolute_path: `/materials/${name}.txt`, kind: "file", description: name === "news" ? "新闻材料：尚待独立核实。" : "原始研究材料。",
      observation: { device: "1", inode: "2", kind: "file", size: "128", modified_ns: "1", changed_ns: "1", observation_ref: `observation-${name}` },
      availability: "available",
    },
    description: "用于当前研究判断", availability: "available", read_state: read ? "read" : "not_read",
    read_ranges: read ? [{ path: `/materials/${name}.txt`, offset: 0, bytes: 128, actor }] : [],
    failures: [], unexpanded: false, treatments: [] as Record<string, unknown>[],
  });
  const adopted = reference("news", true, "target-run-183");
  adopted.treatments = [{
    feedback_ref: "feedback-adopted-183", reference_ref: adopted.reference_ref, receiver, processed_by: processedBy,
    understanding: "这是新闻中的线索，不能单独证明实验结论。", disposition: "adopted",
    changes: "在当前安排中加入独立来源复核。", continuing_work: "后继先读校核笔记，再决定是否投入实验。",
    reasons: "线索与当前问题相关，保留可复核依据。", limitations: "报道缺少原始数据，目标尚未完成。",
    selections: [{
      source: { kind: "original_file", path: "/materials/news.txt" }, custody: "managed", purpose: "保留新闻线索来源",
      asset_binding: { asset_ref: "asset-news-183", version_ref: "asset-news-version-183" }, role_ref: "role-human-input-183",
      reader: { operation: "research_memory.content.read", source_ref: "asset-news-version-183", version_ref: "asset-news-version-183" },
    }, {
      source: { kind: "workspace_file", workspace_file_ref: "workspace-notes-183", path: "/workspace/check-notes.txt" }, custody: "linked_local", purpose: "接续校核笔记",
      asset_binding: { asset_ref: "asset-notes-183", version_ref: "asset-notes-version-183" }, role_ref: "role-notes-183",
      reader: { operation: "research_memory.content.read", source_ref: "asset-notes-version-183", version_ref: "asset-notes-version-183" },
    }], declared_by_root: true, created_at: 1791529200,
  }];
  const unused = reference("unrelated", true, "target-run-183");
  unused.treatments = [{
    feedback_ref: "feedback-unused-183", reference_ref: unused.reference_ref, receiver, processed_by: processedBy,
    understanding: "内容对应其他研究对象。", disposition: "not_used", changes: "维持当前研究安排。",
    continuing_work: "继续现有基线核验。", reasons: "与当前问题无关，因此未采用。", limitations: "没有据此作出结论。",
    selections: [], declared_by_root: true, created_at: 1791529201,
  }];
  const browserOnly = reference("browser-only", true, "browser");
  const unread = reference("unread", false, "");
  companion.messages = [];
  companion.agent_proposals = [];
  companion.soft_constraints = [{
    constraint_ref: "guidance-material-183", scope_ref: companion.scope_ref, revision: 1,
    guidance: { text: "请先复核新闻线索，并说明对当前研究的影响。" }, strength: 3, status: "active",
    work_materials: [{ submission_ref: "submission-183", receiver, references: [adopted, unused, browserOnly, unread] }],
  }];
  snapshot.human_collaboration.human_requests.items = [];
  const errors: string[] = [];
  const writes: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://material-treatment.test") return route.abort();
    if (request.method() !== "GET") { writes.push(`${request.method()} ${url.pathname}`); return route.abort(); }
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("http://material-treatment.test/?workspace=1&companion=1", { waitUntil: "domcontentloaded" });
  const guidance = page.getByRole("complementary", { name: "研究助手", exact: true }).getByRole("article").filter({ hasText: "请先复核新闻线索，并说明对当前研究的影响。" }).first();
  const material = (path: string) => guidance.getByRole("article").filter({ has: page.getByText(path, { exact: true }) });
  const adoptedCard = material("/materials/news.txt");
  await expect(adoptedCard).toContainText("已保存，存在成功读取的字节范围");
  await expect(material("/materials/unread.txt")).toContainText("已保存，尚未读取");
  const feedback = adoptedCard.getByRole("region", { name: "材料处理反馈", exact: true });
  await expect(feedback).toBeVisible();
  await expect(feedback).toContainText("根声明：已采用");
  await expect(feedback).toContainText("在当前安排中加入独立来源复核。");
  await expect(feedback).toContainText("后继先读校核笔记，再决定是否投入实验。");
  await expect(feedback).toContainText("报道缺少原始数据，目标尚未完成。");
  await expect(feedback).toContainText("保留新闻线索来源");
  await expect(feedback).toContainText("接续校核笔记");
  await expect(feedback).toContainText(/原件/);
  await expect(feedback).toContainText(/处理结果/);
  await expect(feedback).toContainText("独立保管");
  await expect(feedback).toContainText("原位链接");
  await adoptedCard.getByText("成功读取范围", { exact: true }).click();
  await expect(adoptedCard).toContainText("从字节 0 起读取 128 字节");
  await expect(adoptedCard).toContainText("读取记录不代表理解或研究采用。");

  const unusedFeedback = material("/materials/unrelated.txt").getByRole("region", { name: "材料处理反馈", exact: true });
  await expect(unusedFeedback).toContainText("根声明：未采用");
  await expect(unusedFeedback).toContainText("与当前问题无关，因此未采用。");
  await expect(unusedFeedback).toContainText(/未选.*保管/);
  await expect(material("/materials/browser-only.txt")).not.toContainText("根声明：已采用");
  await expect(material("/materials/unread.txt")).not.toContainText("根声明：已采用");
  await expect(feedback.getByText("target-root-183", { exact: false })).toBeHidden();
  await expect(feedback.getByText("asset-news-version-183", { exact: false })).toBeHidden();
  await adoptedCard.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("material-treatment-1440.png"), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await adoptedCard.scrollIntoViewIfNeeded();
  await expect(feedback).toBeVisible();
  await expect(feedback).toContainText("根声明：已采用");
  await page.screenshot({ path: testInfo.outputPath("material-treatment-390.png"), fullPage: true });
  await feedback.locator("details").filter({ hasText: "target-root-183" }).locator("summary").click();
  await expect(feedback.getByText("target-root-183", { exact: false })).toBeVisible();
  await expect(feedback.getByText("asset-news-version-183", { exact: false })).toBeVisible();
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});
