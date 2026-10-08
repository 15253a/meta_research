import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { PublicSnapshot } from "../src/api.js";
import type { ResearchOverviewData } from "../src/ResearchOverview.js";
import type { TimelineSummaries } from "../src/timelineSummaries.js";

test.setTimeout(90_000);

async function workspace(page: Page, finding = false) {
  const snapshot: PublicSnapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  const foreground = snapshot.research_control.foreground!;
  snapshot.human_collaboration!.human_requests.items = [];
  const overview: ResearchOverviewData = {
    schema_ref: "meta-research/research-overview/v1", ...foreground, status: "ready",
    cycle_ordinal: 1, foreground, findings: { quest: [], question: [], cycle: [] }, reason: null,
    cycles: [{ cycle_ref: foreground.cycle_ref, question_ref: foreground.question_ref, ordinal: 1, stages: {} }],
  };
  if (finding) overview.findings.cycle.push({ text: "一组测试出现改善，尚待复测。", disposition: "uncertain",
    quest_ref: foreground.quest_ref, question_ref: foreground.question_ref, cycle_ref: foreground.cycle_ref,
    cycle_ordinal: 1, stage: "reasoning", epoch: 1, source: { commit_ref: "accepted-reasoning-1", outcome_ref: "uncertain-outcome-1" } });
  const summaries: TimelineSummaries = {
    schema_ref: "meta-research/timeline-summaries/v1", quest_ref: foreground.quest_ref, revision: 1,
    observed_at: 1_790_000_000, nodes: [{
      node_key: `cycle:${foreground.cycle_ref}`, kind: "cycle", question_ref: foreground.question_ref,
      cycle_ref: foreground.cycle_ref, stage: null, target_ref: null,
      summary: "正在核对实验结果，还需要更多证据。", status: "ready",
      source_hash: "source-1", summarized_source_hash: "source-1", updated_at: 1_790_000_010,
      sources: [{ ref: "record-cycle-1", label: "本轮工作记录" }],
    }],
  };
  const errors: string[] = [];
  const reads = { summaries: 0 };
  page.on("pageerror", error => errors.push(error.message));
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://spectrum-overview.test" || request.method() !== "GET") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname === "/api/v1/research-overview") return json(overview);
    if (url.pathname.endsWith("/timeline-summaries")) { reads.summaries += 1; return json(summaries); }
    if (url.pathname.endsWith("/root-sessions")) return json({ schema_ref: "meta-research/root-sessions/v1", quest_ref: foreground.quest_ref, observed_at: 1_790_000_000, sessions: [], active_session_refs: [], limited: false, reasons: [] });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://spectrum-overview.test/?workspace=1", { waitUntil: "domcontentloaded" });
  return { snapshot, overview, summaries, errors, reads };
}

test("saved recorder prose is identified as a process summary with its sources", async ({ page }) => {
  const { errors } = await workspace(page);
  const timeline = page.getByRole("region", { name: "研究时间线", exact: true });
  await timeline.scrollIntoViewIfNeeded();
  await expect(timeline).toContainText("正在核对实验结果，还需要更多证据。");
  await expect(timeline).toContainText("过程摘要");
  await expect(timeline.getByText("正在核对实验结果，还需要更多证据。", { exact: true })).toHaveAttribute("title", /本轮工作记录.*record-cycle-1/);
  expect(errors).toEqual([]);
});

test("research brief keeps an uncertain finding separate from confirmed conclusions", async ({ page }) => {
  const { errors } = await workspace(page, true);
  const brief = page.getByRole("region", { name: "研究近况", exact: true });
  await brief.scrollIntoViewIfNeeded();
  await expect(brief).toBeVisible();
  await expect(brief).toContainText("正在做什么");
  await expect(brief).toContainText("还在等什么");
  await expect(brief).toContainText("过程摘要");
  await expect(brief.getByText("待确认发现", { exact: true })).toBeVisible();
  await expect(brief).toContainText("一组测试出现改善，尚待复测。");
  await expect(brief.getByText("已确认科研结论", { exact: true })).toHaveCount(0);
  await brief.getByRole("button", { name: "查看发现依据" }).click();
  await expect(page.getByRole("dialog")).toContainText("尚无确定结论");
  expect(errors).toEqual([]);
});

test("a recorder's tentative finding remains distinct from a scientific conclusion", async ({ page }) => {
  const { summaries, errors } = await workspace(page);
  const timeline = page.getByRole("region", { name: "研究时间线", exact: true });
  await timeline.scrollIntoViewIfNeeded();
  await expect(timeline.getByText("过程摘要", { exact: true })).toBeVisible();
  summaries.nodes[0].summary_kind = "tentative_finding";
  summaries.revision += 1;
  await expect(timeline.getByText("待确认发现", { exact: true })).toBeVisible();
  await expect(timeline.getByText("已确认科研结论", { exact: true })).toHaveCount(0);
  await expect(timeline).toContainText("正在核对实验结果，还需要更多证据。");
  expect(errors).toEqual([]);
});

test("a paused projection keeps recorded Target summaries when the live graph is unavailable", async ({ page }) => {
  const { snapshot, summaries, errors } = await workspace(page);
  const foreground = snapshot.research_control.foreground!;
  foreground.status = "suspended";
  foreground.grant_status = "suspended";
  snapshot.bundle_stage!.target_graph.targets = [];
  summaries.nodes.push({
    node_key: `target:${foreground.cycle_ref}:recorded-target-1`, kind: "target",
    question_ref: foreground.question_ref, cycle_ref: foreground.cycle_ref,
    stage: "bundle", target_ref: "recorded-target-1", summary: "实验方案已记录，当前执行状态待确认。",
    summary_kind: "process", status: "failed", source_hash: "next-target-source",
    summarized_source_hash: "saved-target-source", updated_at: 1_790_000_010,
    sources: [{ ref: "accepted-target-graph-1", label: "正式实验方案" }],
  });
  summaries.revision += 1;
  await page.reload({ waitUntil: "domcontentloaded" });
  const timeline = page.getByRole("region", { name: "研究时间线", exact: true });
  await timeline.scrollIntoViewIfNeeded();
  const target = timeline.locator('[data-target-ref="recorded-target-1"]');
  await expect(target).toContainText("实验方案已记录，当前执行状态待确认。");
  await expect(target).toContainText("更新暂不可用");
  await expect(target).toContainText("状态待确认");
  await expect(target).toHaveAttribute("data-executing", "false");
  await target.getByRole("button").click();
  await expect(page.getByRole("dialog")).toContainText("实验与证据");
  await expect(page.getByRole("dialog")).toContainText("结果");
  expect(errors).toEqual([]);
});

test("visible summary regions share reads while still pictures keep updating and hidden regions stop reading", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1700 });
  const { reads, errors } = await workspace(page);
  const brief = page.getByRole("region", { name: "研究近况", exact: true });
  const timeline = page.getByRole("region", { name: "研究时间线", exact: true });
  await expect(brief).toContainText("正在核对实验结果，还需要更多证据。");
  await expect(timeline).toContainText("正在核对实验结果，还需要更多证据。");
  expect(reads.summaries).toBe(1);
  await page.getByRole("button", { name: "静止画面", exact: true }).click();
  const stillReads = reads.summaries;
  await expect.poll(() => reads.summaries, { timeout: 8_000 }).toBeGreaterThan(stillReads);

  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const backgroundReads = reads.summaries;
  await page.waitForTimeout(5_300);
  expect(reads.summaries).toBe(backgroundReads);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => reads.summaries).toBeGreaterThan(backgroundReads);

  await page.setViewportSize({ width: 1440, height: 390 });
  await brief.getByRole("button", { name: "查看当前工作 ↗", exact: true }).click();
  await page.locator(".lumen-main").evaluate(main => { main.scrollTop = main.scrollHeight; });
  await expect(brief).not.toBeInViewport();
  await expect(timeline).not.toBeInViewport();
  const awayReads = reads.summaries;
  await page.waitForTimeout(5_300);
  expect(reads.summaries).toBe(awayReads);
  await timeline.scrollIntoViewIfNeeded();
  await expect.poll(() => reads.summaries).toBeGreaterThan(awayReads);
  expect(errors).toEqual([]);
});
