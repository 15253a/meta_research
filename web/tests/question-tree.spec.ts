import { expect, test, type Page, type Route } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";

type JsonRecord = Record<string, any>;

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

async function openQuestionTreeFixture(page: Page) {
  product = await DeterministicProduct.start();
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as JsonRecord;
  const snapshot: JsonRecord = {
    ...base,
    research_space: {
      ...base.research_space,
      status: "active",
      quest_count: 1,
      question_count: 3,
      foreground_cycle_count: 1,
      current_quest: {
        status: "ready",
        quest_ref: "quest_tree_1",
        goal_revision_ref: "quest-goal-revision-tree-1",
        draft_revision: 3,
        draft_hash: "9".repeat(64),
        goal: "验证问题树只读上下文与当前研究目标保持一致",
        completion_criteria: "三个宽度均可导航，并且选择不写 Owner",
        projection_digest: "8".repeat(64),
        reason: null,
      },
    },
    question_tree: {
      status: "ready",
      reason: null,
      items: [{
        question_ref: "question_tree_root",
        quest_ref: "quest_tree_1",
        parent_question_ref: null,
        title: "Root question",
        unknown_statement: "Root unknown",
        content_ref: "content-tree-root",
        content_hash: "1".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-root",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "bound",
          cycle_ref: "cycle_tree_1",
          foreground: {
            quest_ref: "quest_tree_1",
            question_ref: "question_tree_root",
            cycle_ref: "cycle_tree_1",
            epoch: 4,
            stage: "idea",
            status: "active",
          },
          reason: null,
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }, {
        question_ref: "question_tree_formal",
        quest_ref: "quest_tree_1",
        parent_question_ref: "question_tree_root",
        title: "Formal question",
        unknown_statement: "Formal unknown",
        content_ref: "content-tree-formal",
        content_hash: "2".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-formal",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }, {
        question_ref: "question_tree_branch",
        quest_ref: "quest_tree_1",
        parent_question_ref: "question_tree_formal",
        title: "Branch question",
        unknown_statement: "Branch unknown",
        content_ref: "content-tree-branch",
        content_hash: "3".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-branch",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: {
          status: "ready",
          items: [{
            request_ref: "human-request-tree-1",
            issuer: "agent_runtime",
            kind: "offline_action",
            status: "open",
            revision: 1,
            bindings: [{
              source: "target_assertion",
              field: "question_ref",
              ref: "question_tree_branch",
            }],
          }],
          reason: null,
        },
      }],
    },
    human_collaboration: {
      companion: {
        status: "ready",
        scope_ref: "quest:quest_tree_1",
        messages: [],
        soft_constraints: [],
        agent_proposals: [],
      },
      human_requests: {
        status: "ready",
        waiting: {
          scope: "none",
          safe_meaningful_runnable_exists: true,
          other_blockers: [],
        },
        items: [],
      },
      commands: { status: "ready", items: [], authorizations: [] },
    },
    experiment: {
      status: "active",
      current: {
        intent: {
          quest_ref: "quest_tree_1",
          title: "Question tree experiment",
        },
        identities: {
          evaluation_attempt_ref: "evaluation-attempt-tree-1",
        },
        execution: {
          status: "executed",
          managed_status: "completed",
          run_ref: "run-tree-1",
          attempt_ref: "attempt-tree-1",
          attempt_generation: 1,
          root_session_ref: "session-tree-1",
          fence_ref: "fence-tree-1",
          fence_status: "released",
          events: [{
            event_ref: "event-tree-stdout-1",
            sequence: 1,
            attempt_ref: "attempt-tree-1",
            fence_ref: "fence-tree-1",
            kind: "stdout",
            payload: { stream: "stdout", line: "tree experiment ready" },
            observed_at: 1_720_000_000,
          }],
          stdout_observation: {
            mode: "complete",
            complete: true,
            truncated: false,
            dropped: 0,
            first_sequence: 1,
            last_sequence: 1,
            observed_at: 1_720_000_000,
          },
        },
        assets: { status: "not_attempted" },
        formal_measurement: { status: "not_attempted" },
      },
    },
  };

  await page.route("**/api/v1/snapshot", (route) => fulfill(route, snapshot));
  await page.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
  await page.addInitScript((fenceKey) => {
    window.sessionStorage.setItem(
      "meta-research:execution-observer:auto-presented-fence",
      fenceKey,
    );
  }, "run-tree-1:g1:session-tree-1");
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("return-summary")).toContainText(
    "验证问题树只读上下文与当前研究目标保持一致",
  );
  await expect(page.getByTestId("return-summary")).toContainText(
    "三个宽度均可导航，并且选择不写 Owner",
  );
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  await expect(tree).toBeVisible();
  return tree;
}

test("QuestionTree keeps canvas at 800/1440 and reflows the same keyboard tree into a 390 outline", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tree = await openQuestionTreeFixture(page);
  const canvas = tree.locator(".question-tree-canvas");
  const root = tree.locator('[data-question-ref="question_tree_root"]');
  const formal = tree.locator('[data-question-ref="question_tree_formal"]');
  const branch = tree.locator('[data-question-ref="question_tree_branch"]');

  await expect(canvas).toHaveAttribute("data-layout-mode", "canvas");
  await expect(tree.getByLabel("问题树小地图")).toBeVisible();
  await root.focus();
  await root.press("ArrowDown");
  await expect(formal).toBeFocused();
  await expect(formal).toHaveAttribute("aria-selected", "true");
  await formal.press("End");
  await expect(branch).toBeFocused();
  await expect(branch).toHaveAttribute("aria-selected", "true");
  await expect(branch.getByLabel("关联 1 个 HumanRequest")).toBeVisible();
  await expect(tree.getByLabel("选中问题详情")).toContainText(
    "human-request-tree-1 · offline_action · open",
  );

  await page.setViewportSize({ width: 800, height: 900 });
  await expect(canvas).toHaveAttribute("data-layout-mode", "canvas");
  await expect(tree.getByLabel("问题树小地图")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(canvas).toHaveAttribute("data-layout-mode", "outline");
  await expect(tree.getByLabel("Quest 问题树缩进大纲")).toBeVisible();
  await expect(tree.getByLabel("问题树小地图")).toBeHidden();
  await expect(tree.getByRole("button", { name: "适配全图" })).toBeHidden();
  const geometry = await tree.locator(".question-tree-canvas-world").evaluate((world) => {
    const nodes = Array.from(world.querySelectorAll<HTMLElement>(".question-canvas-node"));
    return {
      transform: getComputedStyle(world).transform,
      positions: nodes.map((node) => ({
        x: node.getBoundingClientRect().x,
        position: getComputedStyle(node).position,
      })),
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  });
  expect(geometry.transform).toBe("none");
  expect(geometry.positions.map((item) => item.position)).toEqual([
    "relative",
    "relative",
    "relative",
  ]);
  expect(geometry.positions[1].x).toBeGreaterThan(geometry.positions[0].x);
  expect(geometry.positions[2].x).toBeGreaterThan(geometry.positions[1].x);
  expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
});

test("QuestionTree reopens the current Experiment observation and routes discussion without owner writes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tree = await openQuestionTreeFixture(page);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });

  const stdout = tree.getByRole("button", { name: "当前实验 · stdout" });
  await expect(stdout).toBeEnabled();
  await stdout.click();
  const observer = page.getByTestId("execution-observer");
  await expect(observer).toHaveAttribute("data-open", "true");
  await expect(observer).toContainText("run-tree-1:g1:session-tree-1");
  await observer.getByRole("button", { name: "关闭当前实验观测窗" }).click();
  await expect(stdout).toBeFocused();

  const branch = tree.locator('[data-question-ref="question_tree_branch"]');
  await branch.click();
  expect(new URL(page.url()).searchParams.get("node")).toBe("question_tree_branch");
  await expect(page.getByTestId("companion-question-context")).toContainText(
    "Branch question",
  );
  const discuss = tree.getByRole("button", { name: "与 Companion 讨论此题" });
  await expect(discuss).toBeEnabled();
  await discuss.click();
  await expect(page.getByLabel("给 Quest Companion 发消息")).toBeFocused();
  expect(writes).toEqual([]);
});
