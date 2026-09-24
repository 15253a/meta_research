import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "@playwright/test";

const dist = path.resolve(process.argv[2]);
const browser = await chromium.launch({ headless: true, executablePath: process.env.META_RESEARCH_CHROME || "/usr/bin/google-chrome" });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  let indexReads = 0;
  let indexUnavailable = false;
  const status = {
    schema_ref: "meta-research/runtime-status/v1", revision: 1, observed_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    state: "waiting", pending_requests: 0, waiting_reason: "等待 Target 完成",
    current_task: { kind: "stage", title: "实验与证据", run_ref: "bundle:1", target_ref: null, status: "awaiting_acceptance" },
    foreground: { quest_ref: "quest:1", cycle_ref: "cycle:1", question_ref: "question:1", stage: "bundle", status: "active" },
    health: { status: "ready", checks: [{ name: "bundle_stage_worker", status: "ready" }] },
  };
  await page.clock.install({ time: new Date("2026-09-14T12:00:00Z") });
  await page.clock.pauseAt(new Date("2026-09-14T12:00:01Z"));
  // Let the host fulfill each intercepted response before advancing its
  // browser-side request deadline; one large jump can fabricate timeouts.
  const advance = async milliseconds => {
    for (let remaining = milliseconds; remaining > 0; remaining -= 1_000) {
      await page.clock.runFor(Math.min(1_000, remaining));
      await new Promise(resolve => setTimeout(resolve, 20));
    }
  };
  await page.route("http://meta-research.test/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/v1/status") return route.fulfill({ json: structuredClone(status) });
    if (url.pathname === "/api/v1/quests/quest%3A1/root-sessions") {
      indexReads += 1;
      if (indexUnavailable) return route.fulfill({ status: 503, json: { detail: { code: "root_session_observation_unavailable" } } });
      const observedAt = await page.evaluate(() => Date.now() / 1_000);
      return route.fulfill({ json: { schema_ref: "meta-research/root-sessions/v1", quest_ref: "quest:1", observed_at: observedAt,
        limited: false, reasons: [], active_session_refs: ["session:target"], sessions: [{
          session_ref: "session:target", root_session_ref: "session:target", kind: "target", title: "Target T1 · live trial", stage: "bundle",
          cycle_ref: "cycle:1", question_ref: "question:1", is_current: true, is_executing: true, status: "executing",
          target_ref: "target:1", run_ref: "target-run:1", updated_at: observedAt, operations: [],
        }] } });
    }
    assert.ok(!url.pathname.startsWith("/api/"), `unexpected API request: ${url.pathname}`);
    const file = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    const contentType = file.endsWith(".js") ? "application/javascript" : file.endsWith(".css") ? "text/css" : "text/html";
    return route.fulfill({ body: await readFile(path.join(dist, file)), contentType });
  });
  await page.goto("http://meta-research.test/");
  await page.getByRole("heading", { name: "Target T1 · live trial", exact: true }).waitFor();
  assert.equal(indexReads, 1, "StrictMode must share the first request");
  assert.equal(await page.locator(".status-state").getAttribute("data-state"), "running");
  await page.getByText("执行观测时间", { exact: true }).waitFor();
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await advance(29_000);
  assert.equal(indexReads, 1, "manual refresh must respect the 30-second cooldown");
  indexUnavailable = true;
  await advance(1_100);
  await page.getByRole("heading", { name: "实验与证据", exact: true }).waitFor();
  assert.equal(indexReads, 2, "the next observation begins after 30 seconds");
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await advance(100);
  assert.equal(await page.getByText("执行观测时间", { exact: true }).count(), 0, "failed refresh must invalidate its cached execution evidence");
  indexUnavailable = false;
  await advance(30_000);
  await page.getByRole("heading", { name: "Target T1 · live trial", exact: true }).waitFor();
  assert.equal(indexReads, 3);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await advance(31_000);
  assert.equal(indexReads, 3, "hidden pages must not refresh the index");
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.getByRole("heading", { name: "Target T1 · live trial", exact: true }).waitFor();
  assert.equal(indexReads, 4, "a visible page may refresh after the cooldown");
  status.foreground.cycle_ref = "cycle:2";
  await advance(5_100);
  await page.getByRole("heading", { name: "实验与证据", exact: true }).waitFor();
  assert.equal(await page.getByText("执行观测时间", { exact: true }).count(), 0, "scope changes must clear old execution evidence");
  status.health = { status: "unavailable", checks: [{ name: "bundle_stage_worker", status: "unavailable", reason: { code: "real_integrity_failure" } }] };
  status.state = "failed";
  status.waiting_reason = "真实故障";
  await advance(5_100);
  await page.getByRole("heading", { name: "实验与证据", exact: true }).waitFor();
  assert.equal(await page.locator(".status-state").getAttribute("data-state"), "failed");
  assert.equal(await page.getByText("执行观测时间", { exact: true }).count(), 0);
  await advance(31_000);
  assert.equal(indexReads, 4, "failed status must stop supplemental reads");
  assert.deepEqual(errors, []);
  console.log("PASS: real Target display, observation time, StrictMode singleflight, 30-second throttle, failed cache invalidation, visibility, scope reset, failure preservation, no full snapshot");
} finally {
  await browser.close();
}
