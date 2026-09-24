import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

test("DeepFetch follows a sliding event window only while the reader stays at the bottom", async ({ page }) => {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  let lastSequence = 24;
  const current = () => ({
    initialization_id: "init-scroll", creation_context: "quest_initialization", route: "deepfetch", status: "proposal_generating",
    quest_draft: { revision: 1, hash: "a".repeat(64), schema_ref: "meta-research/quest-initialization-draft/v2", value: {
      goal: "Read ongoing research", completion_criteria: "Keep the event view stable", time_budget: "30d", route: "deepfetch",
      resource_envelope_ref: null, resource_envelope_hash: null, background_and_initial_direction: "",
      literature: { mode: "oa_only", library_entry_url: "", scope_exclusions: "", accepted_material_bindings: [] },
    } },
    compute: null, resource_envelope: null, proposal_generation: null, proposal: null, confirmation_preview: null,
    intent_session: null, acquisition_session: null, recovery: null, canonical_empty_advancement: false,
    capabilities: { direct: { status: "ready" }, first_question_deepfetch: { status: "ready" }, accepted_material_basis: { status: "ready" } },
    receipts: {},
    deepfetch: {
      request_ref: "request-scroll", correlation_ref: "correlation-scroll", basis_revision: 1, basis_hash: "a".repeat(64), scope_hash: "b".repeat(64),
      status: "running", activity: "web_research", progress: { completed: 2, total: 5 }, freshness: "current", authorization_receipt: null,
      literature_snapshot: null, failure: null,
      recent_events: Array.from({ length: 24 }, (_, index) => ({ sequence: lastSequence - 23 + index, category: "analysis", status: "completed", label: `Research event ${lastSequence - 23 + index}` })),
      run: { run_ref: "run-scroll", status: "running", attempt_ref: "attempt-scroll", attempt_generation: 1,
        attempt_started_at: Date.now() / 1000 - 60, attempt_completed_at: null, provider_operation_retry_permitted: true,
        root_session_ref: "root-scroll", native_session_ref: null, fence_ref: "fence-scroll", runtime_binding_hash: "c".repeat(64), execution_receipt: null, failure: null },
    },
  });
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json({ ...snapshot, quest_creation: { ...snapshot.quest_creation, current: current() } });
    if (url.pathname === "/api/v1/quest-initializations/init-scroll") return json(current());
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?panel=create-quest", { waitUntil: "domcontentloaded" });
  const log = page.getByTestId("deepfetch-live-events").getByRole("log");
  await expect(log).toBeVisible();
  await expect(log.locator("[data-sequence='24']")).toBeVisible();
  await log.evaluate(node => { node.scrollTop = node.scrollHeight; node.dispatchEvent(new Event("scroll")); });
  const bottomGap = () => log.evaluate(node => node.scrollHeight - node.clientHeight - node.scrollTop);
  await expect.poll(bottomGap).toBeLessThanOrEqual(4);
  lastSequence = 25;
  await expect(log.locator("[data-sequence='25']")).toBeVisible();
  await expect.poll(bottomGap).toBeLessThanOrEqual(4);
  await log.evaluate(node => { node.scrollTop = 0; node.dispatchEvent(new Event("scroll")); });
  expect(await bottomGap()).toBeGreaterThan(40);
  lastSequence = 26;
  await expect(log.locator("[data-sequence='26']")).toHaveCount(1);
  expect(await bottomGap()).toBeGreaterThan(40);
  await log.evaluate(node => { node.scrollTop = node.scrollHeight; node.dispatchEvent(new Event("scroll")); });
  lastSequence = 27;
  await expect(log.locator("[data-sequence='27']")).toBeVisible();
  await expect.poll(bottomGap).toBeLessThanOrEqual(4);
  expect(errors).toEqual([]);
});
