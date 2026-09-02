import { expect, test, type Page, type Route } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";


type JsonRecord = Record<string, any>;

const QUEST_REF = "quest-143";
const CYCLE_REF = "cycle-143";
const QUESTION_REF = "question-143-current";

let product: DeterministicProduct | null = null;

test.describe.configure({ timeout: 120_000 });

test.afterEach(async () => {
  const running = product;
  product = null;
  if (running) await running.stop();
});

async function fulfill(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function stageResult(stage: string, outcomeRef: string, nextStage: string) {
  return {
    eligibility: {
      status: "consumed",
      cycle_ref: CYCLE_REF,
      question_ref: QUESTION_REF,
      reason: null,
    },
    stage_run_request: null,
    run: null,
    ...(stage === "idea" ? {
      outcome_acceptance: {
        status: "accepted",
        outcome_kind: "IdeaSet",
        outcome_ref: outcomeRef,
        content: { status: "accepted" },
        domain: { status: "accepted" },
      },
    } : {
      plan_acceptance: {
        status: "accepted",
        plan_document_ref: "plan-document-143",
        formal_plan_ref: outcomeRef,
        outcome_ref: outcomeRef,
        content: { status: "accepted" },
        domain: { status: "accepted" },
        bundle_disposition: "no_new_experiment_required",
        gap_count: 0,
        experiment_brief_count: 0,
      },
    }),
    stage_commit: {
      status: "committed",
      commit_ref: `${stage}-stage-commit-143`,
      cycle_ref: CYCLE_REF,
      stage,
      outcome_ref: outcomeRef,
      next_stage: nextStage,
    },
  };
}

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
        run_ref: "target-run-143-a",
        run_kind: "target",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "running",
        attempt_ref: "target-attempt-143-a",
        root_session_ref: "target-root-session-143-a",
        fence_ref: "target-fence-143-a",
        control_revision: 2,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_788_200_050,
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
        recent_accepted_result: {
          status: "ready",
          stage: "Idea",
          result_ref: "idea-set-143",
          accepted_at: 1_788_199_900,
          reason: null,
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
    idea_stage: stageResult("idea", "idea-set-143", "Plan"),
    plan_stage: stageResult("plan", "formal-plan-143", "Bundle"),
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
      target_graph: { status: "not_attempted", targets: [], frontier: [] },
      target_commits: [],
      baseline_pool: [],
      disposition: {
        status: "skipped",
        target_count: 0,
        target_commit_count: 0,
        reason: { code: "no_new_experiment_required" },
      },
      stage_commit: {
        status: "committed",
        commit_ref: "bundle-stage-commit-143",
        cycle_ref: CYCLE_REF,
        stage: "Bundle",
        disposition: "skipped",
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

async function openCurrentCycle(page: Page) {
  product = await DeterministicProduct.start();
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const snapshot = currentCycleSnapshot(await response.json() as JsonRecord);
  const nonGetRequests: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      nonGetRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  await page.route("**/api/v1/snapshot", (route) => fulfill(route, snapshot));
  await page.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  return { nonGetRequests };
}

test("the foreground Cycle keeps one exact Question and four possible Stage positions at 1440/800/390", async ({
  page,
}) => {
  const { nonGetRequests } = await openCurrentCycle(page);

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.reload({ waitUntil: "domcontentloaded" });
    const overview = page.getByTestId("current-cycle-overview");
    await expect.soft(overview).toBeVisible();
    if (await overview.count()) {
      await expect(overview).toContainText(CYCLE_REF);
      await expect(overview).toContainText(QUESTION_REF);
      await expect(overview).toContainText("非完整路径的可信研究问题");
    }
    expect.soft(await page.evaluate(() => document.documentElement.scrollWidth))
      .toBe(viewport.width);
  }

  const stages = page.getByRole("list", { name: "当前 Cycle 的四个可能 Stage" });
  await expect.soft(stages).toBeVisible();
  if (await stages.count()) {
    const expected = {
      idea: "result",
      plan: "result",
      bundle: "skipped",
      reasoning: "current",
    };
    await expect(stages.getByRole("listitem")).toHaveCount(4);
    for (const [position, state] of Object.entries(expected)) {
      const item = stages.locator(`[data-stage-position="${position}"]`);
      await expect(item).toHaveAttribute("data-stage-state", state);
      await expect(item).toContainText(new RegExp(position, "i"));
    }
    await expect(stages).not.toContainText(/Writing.*Stage|Companion.*Stage/);
  }
  expect(nonGetRequests).toEqual([]);
});

test("QuestionTree marks only the foreground Question and keeps its facts separate", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { nonGetRequests } = await openCurrentCycle(page);
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  const current = tree.locator(`[data-question-ref="${QUESTION_REF}"]`);
  const sibling = tree.locator('[data-question-ref="question-143-sibling"]');

  await expect(tree).toBeVisible();
  await expect.soft(current).toHaveAttribute("data-current-question", "true");
  await expect.soft(current).toContainText("当前攻克");
  await expect(sibling).not.toHaveAttribute("data-current-question", "true");
  await current.click();
  await expect(tree).toContainText("active r7");
  await expect(tree).toContainText(`${CYCLE_REF} · reasoning · epoch 4 · active`);
  await expect(tree).toContainText("human-request-143-current");
  await expect.soft(tree).toContainText("最近接纳结果");
  await expect.soft(tree).toContainText("idea-set-143");
  for (const viewport of [
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(current).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth))
      .toBeLessThanOrEqual(viewport.width);
  }
  expect(nonGetRequests).toEqual([]);
});

test("research activity stays source-separated, names silence honestly, and disappears with the main view", async ({
  page,
}) => {
  const { nonGetRequests } = await openCurrentCycle(page);
  const activity = page.getByRole("region", { name: "研究活动来源" });

  await expect.soft(activity).toBeVisible();
  if (await activity.count()) {
    const currentStage = activity.getByRole("region", { name: "当前阶段" });
    const acquisition = activity.getByRole("region", { name: "资料获取" });
    const targets = activity.getByRole("region", { name: "实验任务" });
    await expect(currentStage).toContainText("reasoning-run-143");
    await expect(currentStage).toContainText("running");
    await expect(currentStage).toContainText(/更新|fresh/i);
    await expect(acquisition).toContainText("acquisition-run-143");
    await expect(acquisition).toContainText("completed");
    await expect(acquisition).toContainText("deepfetch-run-143");
    await expect(acquisition).toContainText("running");
    await expect(acquisition).toContainText("暂无可观察输出");
    await expect(targets).toContainText("target-run-143-a");
    await expect(activity).toContainText("静默不等于根 Agent 已停止");
  }

  await page.getByRole("button", { name: "问题树", exact: true }).click();
  await expect(activity).toBeHidden();
  await page.waitForTimeout(850);
  expect(nonGetRequests).toEqual([]);

  await page.getByRole("button", { name: "Quest 总览", exact: true }).click();
  const overviewVisibleByNextPaint = await page.evaluate(() => (
    new Promise<boolean>((resolve) => window.requestAnimationFrame(() => {
      resolve(!document.querySelector("main.lumen-main")?.hasAttribute("hidden"));
    }))
  ));
  expect(overviewVisibleByNextPaint).toBe(true);
  await expect(activity).toBeVisible();
});
