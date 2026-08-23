import { expect, test, type Page } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";


const QUESTION = {
  quest_ref: "quest-bundle-021",
  question_ref: "question-bundle-038",
  graph_revision: 311,
  title: "必要结构信息",
  unknown_statement: "哪些冻结的结构信息足以复现长链推理结果？",
  answer_shape: "形成带负面结果与证据边界的比较结论。",
  applicability_scope: "覆盖本地可执行微实验。",
};

function receipt(
  issuer: string,
  kind: string,
  receiptRef: string,
  subjectRef: string,
) {
  return {
    status: "accepted",
    issuer,
    kind,
    receipt_ref: receiptRef,
    subject_ref: subjectRef,
    payload_hash: "a".repeat(64),
  };
}

const PARTIAL_BUNDLE = {
  eligibility: {
    status: "eligible",
    cycle_ref: "cycle-bundle-007",
    question_ref: QUESTION.question_ref,
    formal_plan_ref: "formal-plan-007",
    reason: null,
    next_stage: "Bundle",
  },
  stage_run_request: {
    status: "current",
    request_ref: "stage-request-bundle-007",
    cycle_ref: "cycle-bundle-007",
    stage: "Bundle",
    epoch: 1,
    accepted_formal_plan_binding: {
      formal_plan_ref: "formal-plan-007",
      stage_commit_ref: "stage-commit-plan-007",
    },
    context_pack_ref: "context-pack-bundle-007",
    context_pack_hash: "b".repeat(64),
    receipt: receipt(
      "advancement_engine",
      "stage_run_request",
      "ae-bundle-request-receipt-007",
      "stage-request-bundle-007",
    ),
  },
  run: {
    status: "executed",
    run_ref: "run-bundle-007",
    attempt_ref: "attempt-bundle-007",
    root_session_ref: "session-bundle-root-007",
    native_session_ref: "session-bundle-native-007",
    fence_ref: "fence-bundle-007",
    review: {
      status: "completed",
      reviewer_agent_ref: "agent-bundle-reviewer-007",
    },
  },
  target_graph: {
    status: "accepted",
    graph_ref: "target-graph-007",
    formal_plan_ref: "formal-plan-007",
    target_plan_hash: "c".repeat(64),
    receipt: receipt(
      "research_graph",
      "target_graph_acceptance",
      "rg-target-graph-receipt-007",
      "target-graph-007",
    ),
    targets: [
      {
        target_ref: "target-007-a",
        target_key: "gap-structure",
        target_type: "micro_experiment",
        spec_hash: "d".repeat(64),
        dependency_refs: [],
        target_run_ref: "target-run-007-a",
        status: "committed",
        blocker: null,
      },
      {
        target_ref: "target-007-b",
        target_key: "gap-transfer",
        target_type: "micro_experiment",
        spec_hash: "e".repeat(64),
        dependency_refs: ["target-007-a"],
        target_run_ref: "target-run-007-b",
        status: "blocked",
        blocker: { code: "target_run_resource_exhausted" },
      },
    ],
    frontier: [],
  },
  target_commits: [
    {
      status: "realized",
      commit_ref: "target-commit-007-a",
      target_ref: "target-007-a",
      target_run_ref: "target-run-007-a",
      evaluation_attempt_ref: "evaluation-attempt-007-a",
      target_spec_hash: "d".repeat(64),
      closure_hash: "f".repeat(64),
      closure: {},
      result_disposition: "negative",
      receipt: receipt(
        "research_graph",
        "target_commit_acceptance",
        "rg-target-commit-receipt-007-a",
        "target-commit-007-a",
      ),
    },
  ],
  baseline_pool: [
    {
      target_commit_ref: "target-commit-007-a",
      target_ref: "target-007-a",
      result_disposition: "negative",
    },
  ],
  disposition: {
    status: "partial_blocked",
    target_count: 2,
    target_commit_count: 1,
    blocked_targets: [
      {
        target_ref: "target-007-b",
        reason: { code: "target_run_resource_exhausted" },
      },
    ],
  },
  stage_commit: null,
};

let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 90_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start();
});

test.afterEach(async () => {
  await product?.stop();
  product = undefined;
});

function runningProduct(): DeterministicProduct {
  if (!product) throw new Error("deterministic product is not running");
  return product;
}

async function freezeBundleProjection(page: Page) {
  const running = runningProduct();
  await running.authenticate(page);
  const response = await page.request.get(`${running.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as Record<string, unknown>;

  await page.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...base,
        revision: Number(base.revision) + 30,
        research_space: {
          status: "active",
          quest_count: 1,
          question_count: 1,
          foreground_cycle_count: 1,
          current_question: QUESTION,
        },
        idea_stage: undefined,
        plan_stage: undefined,
        bundle_stage: PARTIAL_BUNDLE,
      }),
    });
  });
  await page.goto(running.baseUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("product-shell")).toHaveAttribute(
    "data-shell-state",
    "ready-active",
  );
}

test("Bundle keeps partial TargetCommit truth and blockers visible", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await freezeBundleProjection(page);

  const stageCard = page.getByTestId("bundle-stage-card");
  await expect(page.getByRole("heading", { name: /从 FormalPlan 的 GapSet 出发/ })).toBeVisible();
  await expect(page.getByTestId("current-question-card")).toContainText("Current StageBundle");
  await expect(stageCard.getByRole("listitem")).toHaveCount(6);
  await expect(stageCard).toHaveAttribute("data-bundle-stage-state", "target-work");
  await expect(stageCard.locator('[data-bundle-slot="target-closure"]')).toContainText(
    "1/2 closure 已冻结",
  );

  const targets = page.getByTestId("bundle-target-list");
  await expect(targets.locator('[data-target-status="committed"]')).toContainText(
    "negative",
  );
  await expect(targets.locator('[data-target-status="blocked"]')).toContainText(
    "target_run_resource_exhausted",
  );
  await expect(page.getByRole("button", { name: /启动.*Bundle|Bundle.*启动/ })).toHaveCount(0);

  for (const viewport of [
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(stageCard).toBeVisible();
    expect(
      await page.evaluate(() => ({
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
      })),
    ).toEqual({ pageWidth: viewport.width, viewportWidth: viewport.width });
  }
});
