import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

async function workspace(page: Page) {
  const snapshot = JSON.parse(await readFile(resolve("tests/snapshot-before.json"), "utf8"));
  const foreground = { ...snapshot.research_control.foreground, stage: "bundle", status: "active" };
  snapshot.research_control.foreground = foreground;
  snapshot.human_collaboration.human_requests.items = [];
  snapshot.readiness.checks = [{ name: "bundle_stage_worker", status: "ready" }];
  let domain = "root-data-a", hold = false, rootReads = 0;
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve("../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (data: unknown) => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (path === "/api/v1/status") return json({ schema_ref: "meta-research/runtime-status/v1",
      revision: domain === "root-data-a" ? 10 : 1, revision_domain: domain,
      observed_at: new Date().toISOString(), updated_at: new Date().toISOString(), state: "waiting",
      current_task: { kind: "stage", title: domain === "root-data-a" ? "实例 A 等待" : "实例 B 等待", run_ref: "same-run", target_ref: null, status: "waiting" },
      waiting_reason: "等待当前条件", pending_requests: 0, foreground,
      health: { status: "ready", checks: snapshot.readiness.checks } });
    if (path === "/api/v1/snapshot") return json({ ...snapshot, revision: domain === "root-data-a" ? 10 : 1,
      revision_domain: domain, observed_at: new Date().toISOString() });
    if (path.endsWith("/root-sessions")) {
      rootReads++;
      const captured = domain;
      if (hold) await gate;
      return json({ schema_ref: "meta-research/root-sessions/v1", quest_ref: foreground.quest_ref,
        observed_at: Date.now() / 1000, limited: false, reasons: [], active_session_refs: captured === "root-data-a" ? ["same-session"] : [],
        sessions: [{ session_ref: "same-session", root_session_ref: "same-root", kind: "target", title: captured === "root-data-a" ? "实例 A 执行中的 Target" : "实例 B 等待中的 Target",
          short_title: "T1", stage: "bundle", related_stages: ["bundle"], scope_label: "当前轮", owner_session_ref: null,
          status: captured === "root-data-a" ? "executing" : "waiting", is_current: true, is_executing: captured === "root-data-a",
          run_ref: "same-target-run", target_ref: "same-target", cycle_ref: foreground.cycle_ref, question_ref: foreground.question_ref,
          created_at: 1, updated_at: Date.now() / 1000, operations: [] }] });
    }
    if (path === "/api/v1/preferences") return json({ output_language: "zh" });
    if (path === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "" });
    if (path === "/api/v1/research-overview") return json({ schema_ref: "meta-research/research-overview/v1", status: "ready",
      ...foreground, foreground, cycle_ordinal: 1, findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null });
    if (path.startsWith("/api/")) return json({ status: "ready", items: [], sessions: [], cycles: [] });
    const file = resolve(webRoot, path === "/" ? "index.html" : `.${path}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  return {
    switchInstance() { domain = "root-data-b"; hold = true; },
    release, get rootReads() { return rootReads; },
  };
}

for (const detailed of [false, true]) test(`${detailed ? "workspace" : "home"} discards roots from a different instance with identical references`, async ({ page }) => {
  const app = await workspace(page);
  await page.goto(`http://localhost:18774/${detailed ? "?workspace=1" : ""}`);
  const observation = detailed ? page.getByTestId("current-target-activity") : page.locator("#current-task-title");
  await expect(observation).toContainText("实例 A 执行中的 Target");
  const reads = app.rootReads;
  app.switchInstance();
  await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => app.rootReads, { timeout: 1200 }).toBeGreaterThan(reads);
  if (detailed) await expect(page.getByTestId("current-target-activity")).toHaveCount(0);
  else await expect(observation).toHaveText("实例 B 等待");
  app.release();
  if (detailed) await expect(page.locator(".root-conversations")).toContainText("实例 B 等待中的 Target");
  else await expect(observation).toHaveText("实例 B 等待");
});
