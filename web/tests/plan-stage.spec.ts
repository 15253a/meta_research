import { expect, test, type Page } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";


type PlanStageFixture = Record<string, unknown>;

const QUESTION = {
  quest_ref: "quest-plan-021",
  question_ref: "question-plan-038",
  graph_revision: 208,
  title: "必要结构信息",
  unknown_statement: "压缩推理轨迹后，哪些结构信息是保持长链一致性的必要条件？",
  answer_shape: "形成带反例和证据边界的比较结论。",
  applicability_scope: "覆盖 1.5B–3B 模型；不包含必须调用外部工具的任务。",
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

const ELIGIBLE: PlanStageFixture = {
  eligibility: {
    status: "eligible",
    cycle_ref: "cycle-plan-007",
    question_ref: QUESTION.question_ref,
    idea_outcome_ref: "idea-set-accepted-001",
    reason: null,
  },
  stage_run_request: null,
  run: null,
  plan_acceptance: {
    status: "not_attempted",
    content: { status: "not_attempted" },
    domain: { status: "not_attempted" },
    bundle_disposition: "experiments_required",
    gap_count: 2,
    experiment_brief_count: 2,
  },
  stage_commit: null,
};

const REQUEST = {
  status: "current",
  request_ref: "stage-run-request-plan-001",
  cycle_ref: "cycle-plan-007",
  stage: "Plan",
  epoch: 2,
  accepted_question_binding: {
    quest_ref: QUESTION.quest_ref,
    question_ref: QUESTION.question_ref,
    content_ref: "question-content-038",
    content_hash: "b".repeat(64),
    schema_ref: "meta-research/question-proposal/v1",
    content_receipt: receipt(
      "research_memory",
      "question_content_acceptance",
      "rm-question-content-receipt-038",
      "question-content-038",
    ),
    question_receipt: receipt(
      "research_graph",
      "root_question_acceptance",
      "rg-question-receipt-038",
      QUESTION.question_ref,
    ),
  },
  accepted_idea_set_binding: {
    binding_ref: "accepted-idea-set-binding-001",
    idea_set_ref: "idea-set-accepted-001",
    content_ref: "idea-set-content-001",
    content_hash: "c".repeat(64),
    schema_ref: "meta-research/idea-set/v1",
    candidate_count: 3,
    content_receipt: receipt(
      "research_memory",
      "idea_outcome_content_acceptance",
      "rm-idea-content-receipt-001",
      "idea-set-content-001",
    ),
    domain_receipt: receipt(
      "research_graph",
      "idea_outcome_accepted",
      "rg-idea-set-receipt-001",
      "idea-set-accepted-001",
    ),
    stage_commit_receipt: receipt(
      "advancement_engine",
      "stage_commit",
      "ae-idea-stage-commit-receipt-001",
      "idea-stage-commit-001",
    ),
  },
  context_pack_ref: "plan-context-pack-001",
  context_pack_hash: "d".repeat(64),
  receipt: receipt(
    "advancement_engine",
    "stage_run_request",
    "ae-plan-request-receipt-001",
    "stage-run-request-plan-001",
  ),
};

const REQUESTED: PlanStageFixture = {
  ...ELIGIBLE,
  eligibility: { ...(ELIGIBLE.eligibility as object), status: "requested" },
  stage_run_request: REQUEST,
};

const RUN = {
  status: "awaiting_acceptance",
  run_ref: "run-plan-001",
  attempt_ref: "attempt-plan-001",
  attempt_generation: 1,
  submission_ref: "plan-submission-001",
  root_session_ref: "session-plan-root-001",
  native_session_ref: "session-plan-native-001",
  fence_ref: "fence-plan-001",
  fence_status: "submitted",
  attempt_execution_receipt: receipt(
    "agent_runtime",
    "plan_attempt_execution",
    "ar-plan-execution-receipt-001",
    "plan-submission-001",
  ),
  completion_receipt: null,
  review: {
    status: "completed",
    review_mode: "harness_child_agent",
    reviewer_agent_ref: "agent-plan-reviewer-001",
    finding_count: 1,
    disposition_count: 1,
  },
};

const AWAITING_CONTENT: PlanStageFixture = {
  ...REQUESTED,
  run: RUN,
  plan_acceptance: {
    status: "awaiting_content",
    content: { status: "not_attempted" },
    domain: { status: "not_attempted" },
    bundle_disposition: "experiments_required",
    answer_contract_hash: "e".repeat(64),
    gap_count: 2,
    experiment_brief_count: 2,
  },
};

const AWAITING_DOMAIN: PlanStageFixture = {
  ...AWAITING_CONTENT,
  plan_acceptance: {
    ...(AWAITING_CONTENT.plan_acceptance as object),
    status: "awaiting_domain",
    plan_document_ref: "plan-document-001",
    content: {
      status: "accepted",
      content_ref: "plan-document-001",
      receipt: receipt(
        "research_memory",
        "plan_document_acceptance",
        "rm-plan-document-receipt-001",
        "plan-document-001",
      ),
    },
  },
};

const COMMITTED_NO_GAP: PlanStageFixture = {
  ...AWAITING_DOMAIN,
  eligibility: { ...(ELIGIBLE.eligibility as object), status: "consumed" },
  run: {
    ...RUN,
    status: "completed",
    fence_status: "completed",
    completion_receipt: receipt(
      "agent_runtime",
      "run_execution_completed",
      "ar-plan-completion-receipt-001",
      RUN.run_ref,
    ),
  },
  plan_acceptance: {
    status: "accepted",
    plan_document_ref: "plan-document-001",
    formal_plan_ref: "formal-plan-001",
    outcome_ref: "formal-plan-001",
    content: {
      status: "accepted",
      content_ref: "plan-document-001",
      receipt: receipt(
        "research_memory",
        "plan_document_acceptance",
        "rm-plan-document-receipt-001",
        "plan-document-001",
      ),
    },
    domain: {
      status: "accepted",
      formal_plan_ref: "formal-plan-001",
      receipt: receipt(
        "research_graph",
        "formal_plan_acceptance",
        "rg-formal-plan-receipt-001",
        "formal-plan-001",
      ),
    },
    bundle_disposition: "no_new_experiment_required",
    answer_contract_hash: "f".repeat(64),
    gap_count: 0,
    experiment_brief_count: 0,
  },
  stage_commit: {
    status: "Completed",
    stage_commit_ref: "plan-stage-commit-001",
    request_ref: REQUEST.request_ref,
    cycle_ref: REQUEST.cycle_ref,
    stage: "Plan",
    epoch: 2,
    run_ref: RUN.run_ref,
    outcome_ref: "formal-plan-001",
    outcome_kind: "FormalPlan",
    receipt: receipt(
      "advancement_engine",
      "stage_commit",
      "ae-plan-stage-commit-receipt-001",
      "plan-stage-commit-001",
    ),
    next_stage: "Bundle",
  },
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

async function freezePlanProjection(page: Page, initial: PlanStageFixture) {
  const running = runningProduct();
  await running.authenticate(page);
  const response = await page.request.get(`${running.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as Record<string, unknown>;
  let planStage = initial;
  let revision = Number(base.revision) + 20;

  await page.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...base,
        revision,
        research_space: {
          status: "active",
          quest_count: 1,
          question_count: 1,
          foreground_cycle_count: 1,
          current_question: QUESTION,
        },
        idea_stage: {
          eligibility: { status: "consumed", cycle_ref: "cycle-plan-007" },
          stage_run_request: null,
          run: null,
          outcome_acceptance: {
            status: "accepted",
            content: { status: "accepted" },
            domain: { status: "accepted" },
          },
          stage_commit: { status: "Completed", next_stage: "Plan" },
        },
        plan_stage: planStage,
      }),
    });
  });
  await page.goto(running.baseUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("product-shell")).toHaveAttribute(
    "data-shell-state",
    "ready-active",
  );
  return {
    update(next: PlanStageFixture) {
      planStage = next;
      revision += 1;
    },
  };
}

test("Plan projection keeps execution, asset, domain and advancement visibly separate", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const frozen = await freezePlanProjection(page, ELIGIBLE);
  const stageCard = page.getByTestId("plan-stage-card");

  await expect(page.getByRole("heading", { name: /从已接纳的 IdeaSet 出发/ })).toBeVisible();
  await expect(page.getByTestId("idea-stage-card")).toHaveCount(0);
  await expect(page.getByTestId("current-question-card")).toContainText("Current StagePlan");
  await expect(stageCard.getByRole("listitem")).toHaveCount(5);
  await expect(stageCard.getByRole("listitem").allTextContents()).resolves.toEqual([
    expect.stringContaining("Plan eligibility"),
    expect.stringContaining("StageRunRequest"),
    expect.stringContaining("Run"),
    expect.stringContaining("Plan acceptance"),
    expect.stringContaining("StageCommit"),
  ]);

  for (const scenario of [
    {
      projection: ELIGIBLE,
      state: "eligibility",
      slot: "eligibility",
      text: "已接纳完整 IdeaSet，Plan Stage 具备启动资格",
    },
    {
      projection: REQUESTED,
      state: "stage-run-request",
      slot: "stage-run-request",
      text: "AcceptedQuestionBinding、AcceptedIdeaSetBinding 与 Plan ContextPack",
    },
    {
      projection: AWAITING_CONTENT,
      state: "awaiting-acceptance",
      slot: "plan-acceptance",
      text: "PlanDocument 正等待 Research Memory 接纳",
    },
    {
      projection: AWAITING_DOMAIN,
      state: "awaiting-acceptance",
      slot: "plan-acceptance",
      text: "FormalPlan 正等待 Research Graph 接纳",
    },
    {
      projection: COMMITTED_NO_GAP,
      state: "stage-commit",
      slot: "stage-commit",
      text: "StageCommit(Completed) 已形成",
    },
  ]) {
    frozen.update(scenario.projection);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(stageCard).toHaveAttribute("data-plan-stage-state", scenario.state);
    await expect(stageCard.locator(`[data-plan-slot="${scenario.slot}"]`)).toContainText(
      scenario.text,
    );
  }

  const acceptance = stageCard.locator('[data-plan-slot="plan-acceptance"]');
  await expect(acceptance.locator('[data-plan-owner-layer="content"]')).toContainText(
    "PlanDocument · RMaccepted",
  );
  await expect(acceptance.locator('[data-plan-owner-layer="domain"]')).toContainText(
    "FormalPlan · RGaccepted",
  );
  await stageCard.getByText("查看 Plan 运行身份与 receipt", { exact: true }).click();
  await expect(stageCard.getByText("plan-document-001", { exact: true })).toBeVisible();
  await expect(stageCard.getByText("formal-plan-001", { exact: true })).toBeVisible();
  await expect(stageCard.getByText("ae-plan-stage-commit-receipt-001", { exact: true })).toBeVisible();
});

test("no-gap Plan explains the Bundle skip path without per-Run controls", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await freezePlanProjection(page, COMMITTED_NO_GAP);

  await expect(page.getByTestId("plan-no-gap-disposition")).toContainText(
    "0 gap · 0 ExperimentBrief；不会创建伪造的 Bundle Run",
  );
  await expect(page.getByText(/Advancement Engine 显式处理 Bundle skip/)).toBeVisible();
  await expect(
    page.getByRole("list", { name: "当前研究周期的四个 Stage" }),
  ).toContainText("SKIP PATH");
  await expect(page.getByRole("button", { name: /启动.*Plan|Plan.*启动/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /授权.*Run|Run.*授权/ })).toHaveCount(0);

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("current-question-card")).toBeVisible();
  await expect(page.getByTestId("plan-stage-card")).toBeVisible();
  expect(
    await page.evaluate(() => ({
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    })),
  ).toEqual({ pageWidth: 390, viewportWidth: 390 });
});
