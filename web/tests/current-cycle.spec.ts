import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { RootOperation, RootSession, RootSessions } from "../src/rootSessionsApi.js";
import type { ResearchOverviewData } from "../src/ResearchOverview.js";

type JsonRecord = Record<string, any>;
const QUEST_REF = "quest-143";
const CYCLE_REF = "cycle-143";
const QUESTION_REF = "question-143-current";
const SOURCE_TIME = Date.parse("2026-09-09T18:47:15+08:00") / 1_000;
test.describe.configure({ timeout: 120_000 });
function currentCycleSnapshot(base: JsonRecord): JsonRecord {
  const foreground = {
    quest_ref: QUEST_REF,
    cycle_ref: CYCLE_REF,
    question_ref: QUESTION_REF,
    stage: "reasoning",
    epoch: 4,
    status: "active",
    grant_ref: "foreground-grant-143",
    grant_status: "active",
    safe_point_ref: null,
    pending_operation_ref: null,
    owner_revision: 143,
  };
  return {
    ...base,
    revision: Number(base.revision) + 143,
    owners: {
      ...base.owners,
      research_graph: {
        ...base.owners.research_graph,
        status: "ready",
        revision: 143,
      },
    },
    research_space: {
      ...base.research_space,
      status: "active",
      quest_count: 1,
      question_count: 2,
      foreground_cycle_count: 1,
      current_quest: {
        status: "ready",
        quest_ref: QUEST_REF,
        goal_revision_ref: "goal-revision-143",
        draft_revision: 2,
        draft_hash: "1".repeat(64),
        goal: "把当前 Cycle 的真实研究事实放在同一个界面",
        completion_criteria: "四个可能 Stage 与当前 Question 保持精确绑定",
        projection_digest: "2".repeat(64),
        reason: null,
      },
      current_question: {
        quest_ref: QUEST_REF,
        question_ref: QUESTION_REF,
        graph_revision: 143,
        title: "非完整路径的可信研究问题",
        unknown_statement: "非完整 Stage 路径下，哪些事实已经被正式接纳？",
        answer_shape: "按来源分离事实、活动和不可用边界。",
        applicability_scope: "仅覆盖当前 foreground Cycle。",
      },
    },
    research_control: {
      status: "ready",
      quest_ref: QUEST_REF,
      foreground,
      managed_runs: [{
        run_ref: "reasoning-run-143",
        run_kind: "reasoning_stage",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "running",
        attempt_ref: "reasoning-attempt-143",
        root_session_ref: "reasoning-root-session-143",
        fence_ref: "reasoning-fence-143",
        control_revision: 3,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_788_200_100,
      }, {
        run_ref: "acquisition-run-143",
        run_kind: "acquisition",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "completed",
        attempt_ref: "acquisition-attempt-143",
        root_session_ref: "acquisition-root-session-143",
        fence_ref: "acquisition-fence-143",
        control_revision: 1,
        safe_point_ref: null,
        terminal_reason: "completed",
        cleanup_status: "completed",
        updated_at: 1_788_200_000,
      }, {
        run_ref: "deepfetch-run-143",
        run_kind: "deepfetch",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "running",
        attempt_ref: "deepfetch-attempt-143",
        root_session_ref: "deepfetch-root-session-143",
        fence_ref: "deepfetch-fence-143",
        control_revision: 2,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_788_200_025,
      }, {
        run_ref: "bundle-run-143",
        run_kind: "bundle_stage",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 3,
        status: "completed",
        attempt_ref: "bundle-attempt-143",
        root_session_ref: "bundle-root-session-143",
        fence_ref: "bundle-fence-143",
        control_revision: 2,
        safe_point_ref: null,
        terminal_reason: "completed",
        cleanup_status: "completed",
        updated_at: 1_788_200_075,
      }],
      recovery_records: [],
      actions: [],
    },
    question_tree: {
      status: "ready",
      reason: null,
      items: [{
        question_ref: QUESTION_REF,
        quest_ref: QUEST_REF,
        parent_question_ref: null,
        title: "非完整路径的可信研究问题",
        unknown_statement: "非完整 Stage 路径下，哪些事实已经被正式接纳？",
        content_ref: "question-content-143-current",
        content_hash: "3".repeat(64),
        schema_ref: "meta-research/question/v1",
        question_receipt_ref: "question-receipt-143-current",
        lifecycle_status: "active",
        lifecycle_revision: 7,
        furthest_accepted_stage_result: {
          status: "accepted",
          source: "stage_projection",
          stage: "Bundle",
          kind: "BundleReport",
          result_ref: "bundle-report-143",
          disposition: "realized",
        },
        cycle_binding: {
          status: "bound",
          cycle_ref: CYCLE_REF,
          foreground,
          reason: null,
        },
        related_human_requests: {
          status: "ready",
          items: [{
            request_ref: "human-request-143-current",
            issuer: "agent_runtime",
            kind: "offline_action",
            status: "open",
            revision: 1,
            bindings: [{
              source: "direct_waiter",
              waiter_ref: "waiter-143-current",
              field: "question_ref",
              ref: QUESTION_REF,
            }],
          }],
          reason: null,
        },
      }, {
        question_ref: "question-143-sibling",
        quest_ref: QUEST_REF,
        parent_question_ref: QUESTION_REF,
        title: "尚未绑定的旁支问题",
        unknown_statement: "这个旁支不应被误标为当前攻克。",
        content_ref: "question-content-143-sibling",
        content_hash: "4".repeat(64),
        schema_ref: "meta-research/question/v1",
        question_receipt_ref: "question-receipt-143-sibling",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }],
    },
    human_collaboration: {
      ...base.human_collaboration,
      companion: {
        ...base.human_collaboration.companion,
        status: "ready",
        scope_ref: `quest:${QUEST_REF}`,
      },
      human_requests: {
        ...base.human_collaboration.human_requests,
        status: "ready",
        waiting: {
          scope: "local",
          safe_meaningful_runnable_exists: true,
          other_blockers: [],
        },
        items: [],
      },
    },
    idea_stage: {
      eligibility: {
        status: "not_eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: { code: "idea_route_unavailable" },
      },
      stage_run_request: null,
      run: null,
      outcome_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      stage_commit: null,
      typed_skip: { status: "skipped", basis_refs: ["idea-set-143"] },
    },
    plan_stage: {
      eligibility: {
        status: "not_eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: { code: "plan_route_unavailable" },
      },
      stage_run_request: null,
      run: null,
      plan_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      stage_commit: null,
      typed_skip: { status: "skipped", basis_refs: ["formal-plan-143"] },
    },
    bundle_stage: {
      eligibility: {
        status: "consumed",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        formal_plan_ref: "formal-plan-143",
        reason: null,
      },
      stage_run_request: null,
      run: null,
      target_graph: {
        status: "accepted",
        targets: [{
          target_ref: "target-143-a",
          target_key: "检验关键假设 A",
          spec_hash: "4".repeat(64),
          dependency_refs: [],
          target_run_ref: "target-run-143-a",
          status: "running",
        }],
        frontier: ["target-143-a"],
      },
      target_commits: [],
      baseline_pool: [],
      disposition: {
        status: "realized",
        target_count: 1,
        target_commit_count: 0,
        reason: null,
      },
      bundle_report: {
        status: "accepted",
        report_ref: "bundle-report-143",
        disposition: "realized",
      },
      stage_commit: {
        status: "committed",
        commit_ref: "bundle-stage-commit-143",
        cycle_ref: CYCLE_REF,
        stage: "Bundle",
        disposition: "completed",
        next_stage: "Reasoning",
      },
    },
    reasoning_stage: {
      eligibility: {
        status: "eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: null,
      },
      stage_run_request: null,
      run: null,
      reasoning_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      transition: { status: "not_attempted" },
      stage_commit: null,
    },
  };
}

// Exercise the built UI with read-only public contracts; no research service is started.
async function openCurrentCycle(page: Page, activeBundle = false) {
  const snapshot = currentCycleSnapshot(JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8")));
  const foreground = snapshot.research_control.foreground;
  const operation = (ref: string, label: string, phase: string, status: RootOperation["status"] = "completed", order = 0): RootOperation => ({
    operation_ref: ref, label, phase, status, created_at: SOURCE_TIME + order, updated_at: SOURCE_TIME + order,
  });
  const session = (ref: string, kind: RootSession["kind"], title: string, stage: string | null, status: RootSession["status"], operations: RootOperation[]): RootSession => ({
    session_ref: ref, root_session_ref: ref, kind, title, stage, related_stages: stage ? [stage] : [], scope_label: "当前轮",
    status, is_executing: status === "executing", is_current: kind === "stage" ? stage === foreground.stage : null,
    owner_session_ref: null, run_ref: `run-${ref}`, target_ref: null, cycle_ref: CYCLE_REF, question_ref: QUESTION_REF,
    created_at: SOURCE_TIME, updated_at: SOURCE_TIME, operations,
  });
  const reasoning = session("reasoning-root-session-143", "stage", "Reasoning", "reasoning", "waiting", [operation("reasoning-primary-143", "形成研究判断", "primary")]);
  const bundle = session("bundle-root-session-143", "stage", "Bundle", "bundle", "completed", [operation("bundle-primary-143", "初始实验安排", "primary")]);
  const target = session("target-root-session-143-a", "target", "Target T1 · 检验关键假设 A", "bundle", "completed", [operation("target-turn-143-a", "检验关键假设 A", "harness_turn:1")]);
  target.short_title = "Target T1"; target.target_ref = "target-143-a"; target.owner_session_ref = bundle.session_ref;
  const deepfetch = session("deepfetch-root-session-143", "deepfetch", "DeepFetch · 文献检索", "reasoning", "executing", [operation("deepfetch-turn-143", "检索资料", "turn:1", "executing")]);
  deepfetch.owner_session_ref = reasoning.session_ref; deepfetch.creation_context_kind = "autonomous_question_creation";
  const acquisition = session("acquisition-root-session-143", "acquisition", "Acquisition · 获取资料", null, "waiting", []);
  acquisition.cycle_ref = null; acquisition.question_ref = null; acquisition.scope_label = "Quest 共享";
  const history = session("bundle-root-session-142", "stage", "Bundle", "bundle", "completed", [operation("bundle-primary-142", "上一轮实验安排", "primary", "completed", -100)]);
  history.cycle_ref = "cycle-142"; history.question_ref = "question-142-history"; history.is_current = false;
  const overview: ResearchOverviewData = {
    schema_ref: "meta-research/research-overview/v1", status: "ready", quest_ref: QUEST_REF, question_ref: QUESTION_REF, cycle_ref: CYCLE_REF,
    cycle_ordinal: 143, foreground, findings: { quest: [], question: [], cycle: [] }, reason: null,
    cycles: [{ cycle_ref: CYCLE_REF, question_ref: QUESTION_REF, ordinal: 143, stages: {
      idea: [{ stage: "idea", status: "skipped", epoch: 1, source: { outcome_ref: "idea-set-143" }, content: null, reason: null }],
      plan: [{ stage: "plan", status: "skipped", epoch: 2, source: { outcome_ref: "formal-plan-143" }, content: null, reason: null }],
      bundle: [{ stage: "bundle", status: "accepted", epoch: 3, source: { outcome_ref: "bundle-report-143" }, content: { disposition: "realized", realized_experiment_keys: ["当前轮实验 A"] }, reason: null }],
    } }, { cycle_ref: "cycle-142", question_ref: "question-142-history", ordinal: 142, stages: {
      bundle: [{ stage: "bundle", status: "accepted", epoch: 3, source: { outcome_ref: "bundle-report-142" }, content: { disposition: "realized", realized_experiment_keys: ["上一轮历史实验 B"] }, reason: null }],
    } }],
  };
  if (activeBundle) {
    foreground.stage = "bundle"; foreground.epoch = 3;
    bundle.status = "waiting"; bundle.is_current = true;
    target.status = "executing"; target.is_executing = true; target.is_current = true; target.operations[0].status = "executing";
    deepfetch.status = "completed"; deepfetch.is_executing = false; deepfetch.operations[0].status = "completed";
    snapshot.bundle_stage.stage_commit = null; snapshot.bundle_stage.bundle_report = { status: "not_attempted" };
    overview.cycles[0].stages.bundle = [];
  }
  const catalog: RootSessions = {
    schema_ref: "meta-research/root-sessions/v1", quest_ref: QUEST_REF, observed_at: SOURCE_TIME + 60,
    sessions: [history, ...(activeBundle ? [] : [reasoning]), bundle, target, deepfetch, acquisition],
    active_session_refs: [activeBundle ? target.session_ref : deepfetch.session_ref], limited: false, reasons: [],
  };
  const message = (sessionRef: string, text: string) => JSON.stringify({ type: "item.completed", thread_id: `native-${sessionRef}`, item: { type: "agent_message", text } }) + "\n";
  const output = new Map<string, string>([
    ["reasoning-primary-143", message(reasoning.session_ref, "正在核对当前研究证据。")],
    ["bundle-primary-143", message(bundle.session_ref, "当前轮 Bundle 已形成初始实验安排。")],
    ["target-turn-143-a", message(target.session_ref, "T1 独立根会话正在检验关键假设 A。")],
    ["bundle-primary-142", message(history.session_ref, "上一轮 Bundle 的历史执行记录。")],
  ]);
  const nonGetRequests: string[] = [], errors: string[] = [];
  const state = { listReads: 0, outputReads: [] as { session: string; operation: string; after: number }[] };
  page.on("pageerror", error => errors.push(error.message));
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== "GET") { nonGetRequests.push(`${request.method()} ${url.pathname}`); return route.abort(); }
    if (url.origin !== "http://current-cycle.test") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname === "/api/v1/research-overview") return json(overview);
    if (url.pathname === `/api/v1/quests/${QUEST_REF}/root-sessions`) { state.listReads += 1; return json(catalog); }
    const raw = url.pathname.match(/^\/api\/v1\/quests\/quest-143\/root-sessions\/([^/]+)\/output$/);
    if (raw) {
      const item = catalog.sessions.find(item => item.session_ref === raw[1]);
      const ref = url.searchParams.get("operation_ref") ?? "", op = item?.operations.find(op => op.operation_ref === ref);
      if (!item || !op) return json({ detail: { code: "root_session_operation_not_found" } }, 404);
      const start = Number(url.searchParams.get("after") ?? 0), bytes = Buffer.from(output.get(ref) ?? "");
      state.outputReads.push({ session: item.session_ref, operation: ref, after: start });
      let end = Math.min(bytes.length, start + 65536);
      while (end < bytes.length && (bytes[end] & 0xc0) === 0x80) end -= 1;
      return json({ schema_ref: "meta-research/root-session-output/v1", quest_ref: QUEST_REF, session_ref: item.session_ref, operation_ref: ref,
        stream_ref: `root-output:${item.session_ref}:${ref}`, native_session_ref: `native-${item.session_ref}`,
        status: ["completed", "failed"].includes(op.status) ? "terminal" : op.status === "executing" ? "live" : "waiting",
        text: bytes.subarray(start, end).toString(), offset: start, next_offset: end, source_bytes: bytes.length, has_more: end < bytes.length,
        source_caught_up: end === bytes.length, source_updated_at: SOURCE_TIME, observed_at: SOURCE_TIME + 60 });
    }
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://current-cycle.test/?workspace=1", { waitUntil: "domcontentloaded" });
  return { nonGetRequests, errors, state, snapshot, catalog, bundle, target, deepfetch, acquisition, history, output, operation, message };
}

test("the foreground Cycle keeps one exact Question and four Stage states at 1440/800/390", async ({ page }) => {
  const f = await openCurrentCycle(page);
  for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.reload({ waitUntil: "domcontentloaded" });
    const overview = page.getByTestId("current-cycle-overview");
    await expect(overview).toBeVisible();
    await expect(overview).toHaveAttribute("data-cycle-ref", CYCLE_REF);
    await expect(overview).toHaveAttribute("data-question-ref", QUESTION_REF);
    await expect(overview).toContainText("非完整路径的可信研究问题");
    await expect(overview.locator(".research-cycle-badge")).toContainText("143");
    const stages = overview.getByRole("navigation", { name: "本轮阶段与历史结果" });
    await expect(stages.locator(".spectrum-stage")).toHaveCount(4);
    for (const [stage, label] of Object.entries({ idea: "本轮已跳过", plan: "本轮已跳过", bundle: "结果已接纳", reasoning: "当前阶段" })) {
      const position = stages.locator(`[data-stage="${stage}"]`);
      await expect(position).toHaveAttribute("data-current", String(stage === "reasoning"));
      await expect(position).toContainText(new RegExp(stage, "i"));
      await expect(position.locator(".spectrum-stage-state")).toHaveText(label);
    }
    await expect(stages.locator('[aria-current="step"]')).toHaveCount(1);
    await expect(stages).not.toContainText(/Writing.*Stage|Companion.*Stage/);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
  }
  // Opening a previous result must not replace the foreground Cycle or Question.
  await page.locator('.spectrum-stage[data-stage="bundle"] .spectrum-stage-result').click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("当前轮实验 A");
  await dialog.getByLabel("研究轮次").selectOption("cycle-142");
  await expect(dialog).toContainText("上一轮历史实验 B");
  await expect(dialog).not.toContainText("当前轮实验 A");
  await expect(page.getByTestId("current-cycle-overview")).toHaveAttribute("data-cycle-ref", CYCLE_REF);
  await expect(page.getByTestId("current-cycle-overview")).toHaveAttribute("data-question-ref", QUESTION_REF);
  expect(f.nonGetRequests).toEqual([]); expect(f.errors).toEqual([]);
});

test("QuestionTree marks only the foreground Question and keeps its facts separate", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const f = await openCurrentCycle(page);
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree"), inspector = tree.getByRole("region", { name: "选中问题详情" });
  const current = tree.locator(`[data-question-ref="${QUESTION_REF}"]`), sibling = tree.locator('[data-question-ref="question-143-sibling"]');
  await expect(tree).toBeVisible();
  await expect(current).toHaveAttribute("data-current-question", "true"); await expect(current).toContainText("当前攻克");
  await expect(sibling).not.toHaveAttribute("data-current-question", "true");
  await current.click();
  await expect(inspector).toContainText("active r7");
  await expect(inspector).toContainText(`${CYCLE_REF} · reasoning · epoch 4 · active`);
  await expect(inspector).toContainText("最远已接纳 Stage 结果");
  await inspector.locator(".question-tree-technical-details > summary").click();
  await expect(inspector).toContainText("human-request-143-current"); await expect(inspector).toContainText("bundle-report-143");
  await sibling.click();
  await expect(inspector).toContainText("active r1"); await expect(inspector).toContainText("未绑定当前 Cycle");
  await expect(inspector).not.toContainText("human-request-143-current"); await expect(inspector).not.toContainText("bundle-report-143");
  await expect(current).toHaveAttribute("data-current-question", "true");
  for (const viewport of [{ width: 800, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport); await expect(current).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(viewport.width);
  }
  expect(f.nonGetRequests).toEqual([]); expect(f.errors).toEqual([]);
});

test("root activity preserves source and Cycle scope, reports empty output honestly, and follows the main view", async ({ page }) => {
  const f = await openCurrentCycle(page), activity = page.locator("#research-activity");
  await expect(activity).toBeVisible();
  const spectrum = page.getByRole("region", { name: "研究光谱" });
  const currentRoots = spectrum.locator('.stage-root-links[aria-label$="本轮根会话"]');
  for (const session of ["reasoning-root-session-143", f.bundle.session_ref, f.target.session_ref, f.deepfetch.session_ref, f.acquisition.session_ref]) {
    await expect(currentRoots.locator(`button[data-session-ref="${session}"]`)).toHaveCount(session === f.acquisition.session_ref ? 4 : 1);
  }
  await expect(currentRoots.locator(`button[data-session-ref="${f.history.session_ref}"]`)).toHaveCount(0);
  await currentRoots.locator('button[data-session-ref="reasoning-root-session-143"]').click();
  const reasoning = activity.getByRole("region", { name: "Reasoning 会话", exact: true });
  await expect(reasoning).toContainText("正在核对当前研究证据。"); await expect(reasoning).toContainText("暂无模型调用在执行");
  await currentRoots.locator(`button[data-session-ref="${f.deepfetch.session_ref}"]`).click();
  const deepfetch = activity.getByRole("region", { name: "DeepFetch · 文献检索 会话", exact: true });
  await expect(deepfetch.locator(".root-session-heading")).toContainText("正在执行");
  await expect(deepfetch).not.toContainText("正在核对当前研究证据。");
  await spectrum.locator('.spectrum-stage[data-stage="reasoning"]').locator(`button[data-session-ref="${f.acquisition.session_ref}"]`).click();
  const acquisition = activity.getByRole("region", { name: "Acquisition · 获取资料 会话", exact: true });
  await expect(acquisition.locator(".root-session-heading")).toContainText("Quest 共享");
  await expect(acquisition).toContainText("暂无模型调用在执行"); await expect(acquisition).toContainText("会话已建立，等待第一条公开工作记录。");
  await expect(acquisition.locator(".root-session-heading")).not.toContainText("当前轮");
  await spectrum.locator('.spectrum-stage[data-stage="bundle"] .stage-root-history > summary').click();
  await spectrum.locator(`button[data-session-ref="${f.history.session_ref}"]`).click();
  await expect(activity.locator(".root-session-heading")).toContainText("历史轮次");
  await expect(activity).toContainText("上一轮 Bundle 的历史执行记录。");
  await expect(activity).not.toContainText("当前轮 Bundle 已形成初始实验安排。");
  await page.getByRole("button", { name: "问题树", exact: true }).click(); await expect(activity).toBeHidden();
  await page.getByRole("button", { name: "研究总览", exact: true }).click();
  await expect(page.locator("main.lumen-main")).toBeVisible(); await expect(activity).toBeVisible();
  await expect(page.getByTestId("current-cycle-overview")).toHaveAttribute("data-cycle-ref", CYCLE_REF);
  expect(f.nonGetRequests).toEqual([]); expect(f.errors).toEqual([]);
});

test("active Bundle selects its exact root and keeps later calls continuous without mixing Target or previous Cycle output", async ({ page }) => {
  const f = await openCurrentCycle(page, true), activity = page.locator("#research-activity");
  const current = page.getByRole("region", { name: "研究光谱" }).locator('.spectrum-stage[data-stage="bundle"]');
  await expect(page.locator('.spectrum-stage[data-stage="bundle"]')).toHaveAttribute("aria-current", "step");
  await expect(current.locator(".stage-root-count")).toHaveText("3 个根 session");
  await expect(activity).toContainText("当前轮 Bundle 已形成初始实验安排。");
  await current.locator(`button[data-session-ref="${f.bundle.session_ref}"]`).click();
  const timeline = activity.getByRole("region", { name: "Bundle 会话", exact: true });
  await expect(timeline).toHaveAttribute("data-session-ref", f.bundle.session_ref);
  await expect(timeline).toContainText("当前轮 Bundle 已形成初始实验安排。");
  await expect(timeline).not.toContainText("上一轮 Bundle 的历史执行记录。");
  await expect(timeline).not.toContainText("T1 独立根会话正在检验关键假设 A。");
  const reads = f.state.listReads;
  f.bundle.operations.push(f.operation("bundle-review-143", "复核实验安排", "review", "executing", 10));
  f.bundle.status = "executing"; f.bundle.is_executing = true;
  await expect.poll(() => f.state.listReads).toBeGreaterThan(reads);
  await expect(timeline.locator(".root-operation-time")).toHaveText([/初始实验安排/, /复核实验安排/]);
  await expect(timeline).toContainText("当前轮 Bundle 已形成初始实验安排。");
  f.output.set("bundle-review-143", f.message(f.bundle.session_ref, "新的复核输出接续在当前 Bundle 根会话。"));
  await expect(timeline).toContainText("新的复核输出接续在当前 Bundle 根会话。", { timeout: 10_000 });
  await expect(timeline).toContainText("当前轮 Bundle 已形成初始实验安排。");
  expect(f.state.outputReads.filter(item => item.operation === "bundle-review-143").every(item => item.session === f.bundle.session_ref)).toBe(true);
  await current.locator(`button[data-session-ref="${f.target.session_ref}"]`).click();
  await expect(activity).toContainText("T1 独立根会话正在检验关键假设 A。");
  await expect(activity).not.toContainText("新的复核输出接续在当前 Bundle 根会话。");
  await current.locator(`button[data-session-ref="${f.bundle.session_ref}"]`).click();
  await expect(timeline).toHaveAttribute("data-session-ref", f.bundle.session_ref);
  await expect(timeline).toContainText("新的复核输出接续在当前 Bundle 根会话。");
  await expect(timeline).not.toContainText("上一轮 Bundle 的历史执行记录。");
  await expect(timeline.getByRole("alert")).toHaveCount(0);
  expect(f.nonGetRequests).toEqual([]); expect(f.errors).toEqual([]);
});
