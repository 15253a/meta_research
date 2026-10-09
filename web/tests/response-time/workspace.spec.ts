import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { performance } from "node:perf_hooks";
import { expect, test, type Browser, type Page, type TestInfo } from "@playwright/test";
import type { PublicSnapshot } from "../../src/api.js";
import type { RuntimeStatus } from "../../src/StatusHome.js";
import type { RootSession, RootSessions } from "../../src/rootSessionsApi.js";
import { DeterministicProduct } from "../support/deterministic-product.js";
import { summarizeLatency } from "../support/response-time-metrics.js";
import budgets from "./budgets.json" with { type: "json" };

type Metric = keyof typeof budgets.p95_ms;
type Samples = Partial<Record<Metric, number[]>>;
let product: DeterministicProduct;
let foreground: NonNullable<RuntimeStatus["foreground"]>;
let plan: RootSession;
let idea: RootSession;
let storageState: Awaited<ReturnType<import("@playwright/test").BrowserContext["storageState"]>>;

test.beforeAll(async ({ browser }, testInfo) => {
  test.setTimeout(90_000);
  product = await DeterministicProduct.start({ manualRoot: true, stagePipeline: "plan-gap" });
  const context = await browser.newContext();
  try {
    const page = await context.newPage();
    await product.authenticate(page);
    storageState = await context.storageState();
    try {
      await product.waitForPlanProviderPhase("plan-primary", 30_000);
    } catch (error) {
      for (const path of ["/api/v1/status", "/api/v1/snapshot", "/internal/readiness"]) {
        const name = `setup-${path.split("/").pop()}`;
        try {
          const response = await page.request.get(`${product.baseUrl}${path}`, {
            headers: { "X-Meta-Research-Control": "chrome-control-key" }, timeout: 3000,
          });
          await testInfo.attach(name, { body: await response.body(), contentType: "application/json" });
        } catch (diagnosticError) {
          await testInfo.attach(`${name}-error`, {
            body: String(diagnosticError), contentType: "text/plain",
          }).catch(() => {});
        }
      }
      throw error;
    }
    const statusResponse = await page.request.get(`${product.baseUrl}/api/v1/status`);
    expect(statusResponse.ok()).toBe(true);
    const status = await statusResponse.json() as RuntimeStatus;
    expect(status.foreground?.stage.toLowerCase()).toBe("plan");
    const { quest_ref, cycle_ref, question_ref, stage } = status.foreground!;
    foreground = { quest_ref, cycle_ref, question_ref, stage };
    const response = await page.request.get(
      `${product.baseUrl}/api/v1/quests/${encodeURIComponent(foreground.quest_ref)}/root-sessions`,
    );
    expect(response.ok()).toBe(true);
    const catalog = await response.json() as RootSessions;
    expect(catalog.quest_ref).toBe(foreground.quest_ref);
    const find = (stage: string) => catalog.sessions.find(session => session.kind === "stage"
      && session.stage?.toLowerCase() === stage && session.cycle_ref === foreground.cycle_ref
      && session.question_ref === foreground.question_ref);
    const currentPlan = find("plan"), acceptedIdea = find("idea");
    expect(currentPlan?.operations.length).toBeGreaterThan(0);
    expect(acceptedIdea?.operations.length).toBeGreaterThan(0);
    plan = currentPlan!;
    idea = acceptedIdea!;
  } finally {
    await context.close();
  }
});

test.afterAll(async () => {
  await product?.stop();
});

async function authenticatedPage(browser: Browser, viewport: { width: number; height: number }) {
  const context = await browser.newContext({ viewport, serviceWorkers: "block", storageState });
  const page = await context.newPage();
  return { context, page };
}

async function visibleSession(page: Page, session: RootSession) {
  const conversation = page.locator(".root-session-conversation");
  await expect(conversation).toBeVisible();
  await expect(conversation).toHaveAttribute("data-session-ref", session.session_ref);
  await expect(conversation.locator(".root-session-context")).toContainText(session.question_ref!);
  // The clock only exists once the real output HTTP body has been read.
  await expect(conversation.locator(".root-operation-clock").first()).toBeVisible();
  await expect(conversation.locator(".root-session-warning")).toHaveCount(0);
  await page.evaluate(() => new Promise<void>(done => requestAnimationFrame(() => {
    requestAnimationFrame(() => done());
  })));
}

async function workspaceReady(page: Page) {
  const overview = page.locator('[data-testid="workspace-live-overview"]:visible, [data-testid="current-cycle-overview"]:visible');
  await expect(overview).toBeVisible();
  await expect(overview).toHaveAttribute("data-cycle-ref", foreground.cycle_ref);
  await visibleSession(page, plan);
}

async function measure(
  samples: Samples,
  metric: Metric,
  index: number,
  action: () => Promise<void>,
) {
  const began = performance.now();
  await action();
  const elapsed = performance.now() - began;
  if (index >= budgets.warmups) samples[metric]!.push(elapsed);
}

async function report(
  samples: Samples,
  testInfo: TestInfo,
  browser: Browser,
  failure: unknown,
) {
  const metrics = Object.fromEntries(Object.entries(samples).map(([name, raw]) => [
    name, summarizeLatency(raw, budgets.samples, budgets.p95_ms[name as Metric]),
  ]));
  const payload = {
    schema: "meta-research/response-time-ci/v1",
    status: failure || Object.values(metrics).some(metric => !metric.passed) ? "failed" : "passed",
    error: failure instanceof Error ? failure.message : failure ? String(failure) : null,
    test: testInfo.title,
    generated_at: new Date().toISOString(),
    environment: {
      node: process.version,
      platform: process.platform,
      architecture: process.arch,
      browser: browser.version(),
      commit: process.env.GITHUB_SHA ?? process.env.META_RESEARCH_TEST_COMMIT ?? "local-unrecorded",
    },
    workload: { quest_count: 1, held_provider: "plan-primary", stages: ["idea", "plan"] },
    warmups: budgets.warmups,
    percentile_method: "nearest-rank",
    timer: "Monotonic elapsed time through validated UI content plus two animation frames, or through the complete API JSON body",
    boundaries: [
      "Fresh browser contexts for navigation; backend process and static files are warmed.",
      "Stage-switch samples alternate Idea and Plan in an already open workspace.",
      "Warm snapshot measures complete HTTP delivery including retained results, not fresh projection computation.",
      "Temporary deterministic product data; no production service or real model calls.",
    ],
    metrics,
  };
  const body = JSON.stringify(payload, null, 2);
  const directory = resolve("response-time-results");
  await mkdir(directory, { recursive: true });
  await writeFile(resolve(directory, `${testInfo.title.replace(/[^a-z0-9-]/gi, "-")}.json`), body);
  await testInfo.attach("response-time", { body: Buffer.from(body), contentType: "application/json" });
  for (const [name, metric] of Object.entries(metrics)) {
    console.log(`${testInfo.title} ${name}: n=${metric.count}, p95=${metric.p95_ms?.toFixed(1)}ms, max=${metric.max_ms?.toFixed(1)}ms, budget=${metric.p95_budget_ms}ms`);
  }
  return metrics;
}

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`workspace-and-stage-switch-${viewport.width}`, async ({ browser }, testInfo) => {
    const samples: Samples = { workspace: [], stage_switch: [] };
    let failure: unknown;
    try {
      for (let index = 0; index < budgets.warmups + budgets.samples; index++) {
        const { context, page } = await authenticatedPage(browser, viewport);
        try {
          await measure(samples, "workspace", index, async () => {
            await page.goto(`${product.baseUrl}/?workspace=1`, { waitUntil: "domcontentloaded" });
            await workspaceReady(page);
          });
        } finally {
          await context.close();
        }
      }
      const { context, page } = await authenticatedPage(browser, viewport);
      try {
        await page.goto(`${product.baseUrl}/?workspace=1`, { waitUntil: "domcontentloaded" });
        await workspaceReady(page);
        for (let index = 0; index < budgets.warmups + budgets.samples; index++) {
          const destination = index % 2 === 0 ? idea : plan;
          const stage = destination.stage!.toLowerCase();
          await measure(samples, "stage_switch", index, async () => {
            await page.locator(`[data-testid="workspace-live-overview"] [data-stage="${stage}"] .spectrum-stage-open, [data-testid="current-cycle-overview"] [data-stage="${stage}"] .spectrum-stage-open`).click();
            await visibleSession(page, destination);
          });
        }
      } finally {
        await context.close();
      }
    } catch (error) {
      failure = error;
      throw error;
    } finally {
      const metrics = await report(samples, testInfo, browser, failure);
      if (!failure) expect(Object.values(metrics).every(metric => metric.passed), JSON.stringify(metrics)).toBe(true);
    }
  });
}

test("warm-public-api-response", async ({ browser }, testInfo) => {
  const samples: Samples = { status: [], snapshot: [] };
  let failure: unknown;
  const { context, page } = await authenticatedPage(browser, { width: 1440, height: 900 });
  try {
    for (const metric of ["status", "snapshot"] as const) {
      for (let index = 0; index < budgets.warmups + budgets.samples; index++) {
        await measure(samples, metric, index, async () => {
          const response = await page.request.get(`${product.baseUrl}/api/v1/${metric}`);
          expect(response.status()).toBe(200);
          if (metric === "status") {
            const body = await response.json() as RuntimeStatus;
            expect(body.schema_ref).toBe("meta-research/runtime-status/v1");
            expect(body.foreground).toMatchObject(foreground);
          } else {
            const body = await response.json() as PublicSnapshot;
            expect(body.research_control.foreground).toMatchObject(foreground);
            expect(body.question_tree.status).toBe("ready");
          }
        });
      }
    }
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    await context.close();
    const metrics = await report(samples, testInfo, browser, failure);
    if (!failure) expect(Object.values(metrics).every(metric => metric.passed), JSON.stringify(metrics)).toBe(true);
  }
});
