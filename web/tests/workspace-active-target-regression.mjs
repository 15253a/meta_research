import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { chromium, expect } from "@playwright/test";

const dist = path.resolve(process.argv[2]);
const fixture = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
const browser = await chromium.launch({ headless: true, executablePath: process.env.META_RESEARCH_CHROME || "/usr/bin/google-chrome" });
try {
  for (const snapshotState of ["paused", "active"]) {
    const page = await browser.newPage();
    const snapshot = structuredClone(fixture);
    snapshot.revision = 200;
    snapshot.observed_at = new Date().toISOString();
    snapshot.research_control.foreground.status = snapshotState;
    if (snapshotState === "paused") snapshot.research_control.foreground.grant_status = "suspended";
    const scope = fixture.research_control.foreground;
    let statusUnavailable = false;
    let rootReads = 0;
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.clock.install();
    await page.route("http://meta-research.test/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/v1/status") {
        if (statusUnavailable) return route.fulfill({ status: 503, json: {} });
        return route.fulfill({ json: {
          schema_ref: "meta-research/runtime-status/v1", revision: snapshotState === "paused" ? 199 : 201,
          observed_at: new Date().toISOString(), updated_at: new Date().toISOString(), foreground: scope,
          state: "waiting", waiting_reason: "等待 Target", pending_requests: 0,
          current_task: { kind: "stage", title: "实验与证据", run_ref: "bundle:1", target_ref: null, status: "awaiting_acceptance" },
          health: { status: "ready", checks: [{ name: "bundle_stage_worker", status: "ready" }] },
        } });
      }
      if (url.pathname === "/api/v1/snapshot") return route.fulfill({ json: snapshot });
      if (url.pathname.endsWith("/root-sessions")) {
        rootReads += 1;
        return route.fulfill({ json: {
          schema_ref: "meta-research/root-sessions/v1", quest_ref: scope.quest_ref,
          observed_at: await page.evaluate(() => Date.now() / 1_000), limited: false, reasons: [], active_session_refs: ["session:target"],
          sessions: [{ session_ref: "session:target", root_session_ref: "session:target", kind: "target", title: "Target T1 · observed trial",
            stage: "bundle", related_stages: ["bundle"], scope_label: "当前轮", owner_session_ref: null,
            cycle_ref: scope.cycle_ref, question_ref: scope.question_ref, is_current: true, is_executing: true, status: "executing",
            target_ref: "target:1", run_ref: "target-run:1", updated_at: 100, created_at: 100, operations: [] }],
        } });
      }
      if (url.pathname === "/api/v1/research-overview") return route.fulfill({ json: {
        schema_ref: "meta-research/research-overview/v1", status: "ready", ...scope, foreground: scope,
        cycle_ordinal: 1, findings: { quest: [], question: [], cycle: [] }, cycles: [], reason: null,
      } });
      if (url.pathname.startsWith("/api/")) return route.fulfill({ status: 404, json: {} });
      const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
      return route.fulfill({ body: await readFile(path.join(dist, file)), contentType: file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html" });
    });
    await page.goto("http://meta-research.test/?workspace=1");
    await expect(page.getByTestId("product-shell")).toHaveAttribute("data-shell-state", "ready-active");
    await expect(page.locator(".root-session-conversation")).toBeVisible();
    assert.ok(rootReads > 0);
    if (snapshotState === "paused") {
      assert.equal(await page.getByTestId("current-target-activity").count(), 0, "a newer paused snapshot must beat the old waiting status");
    } else {
      await expect(page.getByTestId("current-target-activity")).toContainText("执行中");
      statusUnavailable = true;
      await page.clock.runFor(5_100);
      await expect(page.getByTestId("current-target-activity")).toHaveCount(0);
    }
    assert.deepEqual(errors, []);
    await page.close();
  }
  console.log("PASS: actual WorkspaceMain respects the selected newer snapshot and failed runtime status refresh");
} finally {
  await browser.close();
}
