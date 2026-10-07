import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

test("creating a Quest defaults to balanced and restores a saved research style with its boundaries", async ({ page }) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  let created = false;
  let revision = 1;
  let value: Record<string, unknown> = {
    goal: "核对已有结果的适用范围", completion_criteria: "保留原始数据；只在获准范围内研究。",
    time_budget: "30d", route: "direct", resource_envelope_ref: null, resource_envelope_hash: null,
    background_and_initial_direction: "已有数据不能改写。",
    literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
  };
  const current = () => ({
    initialization_id: "init-style", creation_context: "quest_initialization", route: "direct", status: "draft",
    quest_draft: { revision, hash: String(revision).repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value },
    compute: null, resource_envelope: null, proposal_generation: null, proposal: null, confirmation_preview: null,
    intent_session: null, acquisition_session: null, deepfetch: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: {},
  });
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current: created ? current() : null } });
    if (url.pathname === "/api/v1/quest-initializations" && request.method() === "POST") {
      created = true;
      return json(current());
    }
    if (url.pathname === "/api/v1/quest-initializations/init-style/draft" && request.method() === "PUT") {
      const body = request.postDataJSON();
      expect(body.expected_draft_revision).toBe(revision);
      value = body.draft;
      revision += 1;
      return json(current());
    }
    if (url.pathname === "/api/v1/quest-initializations/init-style") return json(current());
    if (url.pathname === "/api/v1/quest-initializations/current") return json(created ? current() : null);
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const style = dialog.getByRole("combobox", { name: "研究风格", exact: true });
  await expect(style).toHaveValue("balanced");
  await expect(style.locator("option")).toHaveText(["聚焦攻关", "均衡探索", "开放探索"]);
  await style.selectOption("focus");
  await style.blur();
  await expect.poll(async () => page.evaluate(async () => {
    const response = await fetch("/api/v1/quest-initializations/init-style");
    return (await response.json()).quest_draft.value;
  })).toMatchObject({
    research_style: "focus", goal: "核对已有结果的适用范围", completion_criteria: "保留原始数据；只在获准范围内研究。",
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" }).getByRole("combobox", { name: "研究风格", exact: true })).toHaveValue("focus");
});
