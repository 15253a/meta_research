import { expect, test, type Page, type Route } from "@playwright/test";

import {
  DeterministicProduct,
} from "./support/deterministic-product.js";


type IdeaStageFixture = Record<string, unknown>;

const QUESTION = {
  quest_ref: "quest-021",
  question_ref: "question-038",
  graph_revision: 184,
  title: "必要结构信息",
  unknown_statement: "压缩推理轨迹后，哪些结构信息是保持长链一致性的必要条件？",
  answer_shape: "形成带反例和证据边界的比较结论。",
  applicability_scope: "覆盖 1.5B–3B 模型；不包含必须调用外部工具的任务。",
};

const RECEIPT = {
  status: "accepted",
  issuer: "advancement_engine",
  kind: "stage_commit",
  receipt_ref: "ae-stage-commit-receipt-001",
  subject_ref: "stage-commit-001",
  payload_hash: "c".repeat(64),
};

const ELIGIBLE: IdeaStageFixture = {
  eligibility: {
    status: "eligible",
    cycle_ref: "cycle-007",
    reason: null,
  },
  stage_run_request: null,
  run: null,
  outcome_acceptance: {
    status: "not_attempted",
    content: { status: "not_attempted" },
    domain: { status: "not_attempted" },
  },
  stage_commit: null,
};

const REQUEST = {
  status: "current",
  request_ref: "stage-run-request-idea-001",
  cycle_ref: "cycle-007",
  stage: "Idea",
  epoch: 1,
  accepted_question_binding: {
    initialization_id: "quest-initialization-021",
    quest_ref: QUESTION.quest_ref,
    question_ref: QUESTION.question_ref,
    content_ref: "question-content-038",
    content_hash: "a".repeat(64),
    schema_ref: "meta-research/question-proposal/v1",
    content_receipt: {
      status: "accepted",
      issuer: "research_memory",
      kind: "question_content_acceptance",
      receipt_ref: "rm-question-content-receipt-038",
      subject_ref: "question-content-038",
      payload_hash: "1".repeat(64),
    },
    question_receipt: {
      status: "accepted",
      issuer: "research_graph",
      kind: "root_question_acceptance",
      receipt_ref: "rg-root-question-receipt-038",
      subject_ref: QUESTION.question_ref,
      payload_hash: "2".repeat(64),
    },
  },
  context_pack_ref: "idea-context-pack-001",
  context_pack_hash: "b".repeat(64),
  receipt: {
    status: "accepted",
    issuer: "advancement_engine",
    kind: "stage_run_request",
    receipt_ref: "ae-stage-run-request-receipt-001",
    subject_ref: "stage-run-request-idea-001",
    payload_hash: "3".repeat(64),
  },
};

const REQUESTED: IdeaStageFixture = {
  ...ELIGIBLE,
  eligibility: { ...ELIGIBLE.eligibility as object, status: "requested" },
  stage_run_request: REQUEST,
};

const RUN = {
  status: "admitted",
  run_ref: "run-idea-001",
  attempt_ref: "attempt-idea-001",
  attempt_generation: 1,
  submission_ref: null,
  root_session_ref: "session-idea-primary-001",
  fence_ref: "fence-idea-001",
  fence_status: "current",
  attempt_execution_receipt: null,
  completion_receipt: null,
  native_session_ref: null,
  provider_operations: {
    primary: {
      invocation_ref: "idea-primary-invocation-001",
      status: "prepared",
      request_hash: "6".repeat(64),
      response_hash: null,
    },
    review: {
      invocation_ref: "idea-review-invocation-001",
      status: "prepared",
      request_hash: "7".repeat(64),
      response_hash: null,
    },
  },
  primary_draft_checkpoint: null,
  review: null,
};

const ADMITTED: IdeaStageFixture = {
  ...REQUESTED,
  run: RUN,
};

const AWAITING: IdeaStageFixture = {
  ...ADMITTED,
  run: {
    ...RUN,
    status: "awaiting_acceptance",
    native_session_ref: "session-idea-native-primary-001",
    provider_operations: {
      primary: {
        invocation_ref: "idea-primary-invocation-001",
        status: "completed",
        request_hash: "6".repeat(64),
        response_hash: "8".repeat(64),
      },
      review: {
        invocation_ref: "idea-review-invocation-001",
        status: "completed",
        request_hash: "7".repeat(64),
        response_hash: "9".repeat(64),
      },
    },
    primary_draft_checkpoint: {
      status: "recorded",
      draft_hash: "8".repeat(64),
      adapter_kind: "test_deterministic",
    },
    submission_ref: "idea-submission-001",
    attempt_execution_receipt: {
      status: "accepted",
      issuer: "agent_runtime",
      kind: "idea_attempt_execution",
      receipt_ref: "ar-attempt-execution-receipt-001",
      subject_ref: "idea-submission-001",
      payload_hash: "d".repeat(64),
    },
    completion_receipt: null,
    fence_status: "submitted",
    review: {
      status: "completed",
      review_mode: "harness_child_agent",
      reviewer_agent_ref: "agent-idea-reviewer-001",
      finding_count: 1,
      disposition_count: 1,
    },
  },
  outcome_acceptance: {
    status: "awaiting_domain",
    outcome_kind: "IdeaSet",
    content: {
      status: "accepted",
      content_ref: "idea-content-001",
      receipt: {
        issuer: "research_memory",
        kind: "idea_outcome_content_acceptance",
        receipt_ref: "rm-idea-content-receipt-001",
        subject_ref: "idea-content-001",
        payload_hash: "e".repeat(64),
      },
    },
    domain: { status: "not_attempted" },
  },
};

const REJECTED: IdeaStageFixture = {
  ...AWAITING,
  outcome_acceptance: {
    ...AWAITING.outcome_acceptance as object,
    status: "rejected",
    outcome_ref: null,
    rejection: { code: "idea_outcome_requires_revision" },
    domain: {
      status: "rejected",
      reason: { code: "evidence_boundary_incomplete" },
      receipt: {
        status: "accepted",
        issuer: "research_graph",
        kind: "idea_outcome_rejected",
        receipt_ref: "rg-idea-rejection-receipt-001",
        subject_ref: "idea-decision-rejected-001",
        payload_hash: "8".repeat(64),
      },
    },
  },
  stage_commit: null,
};

const COMMITTED: IdeaStageFixture = {
  ...AWAITING,
  eligibility: { ...AWAITING.eligibility as object, status: "consumed" },
  run: {
    ...AWAITING.run as object,
    status: "completed",
    fence_status: "completed",
    completion_receipt: {
      status: "accepted",
      issuer: "agent_runtime",
      kind: "run_execution_completed",
      receipt_ref: "ar-run-completion-receipt-001",
      subject_ref: RUN.run_ref,
      payload_hash: "9".repeat(64),
    },
  },
  outcome_acceptance: {
    ...AWAITING.outcome_acceptance as object,
    status: "accepted",
    outcome_ref: "idea-outcome-001",
    domain: {
      status: "accepted",
      receipt: {
        issuer: "research_graph",
        kind: "idea_outcome_accepted",
        receipt_ref: "rg-idea-outcome-receipt-001",
        subject_ref: "idea-outcome-001",
        payload_hash: "f".repeat(64),
      },
    },
  },
  stage_commit: {
    status: "Completed",
    commit_ref: "stage-commit-001",
    stage_commit_ref: "stage-commit-001",
    request_ref: REQUEST.request_ref,
    cycle_ref: REQUEST.cycle_ref,
    stage: "Idea",
    epoch: 1,
    run_ref: RUN.run_ref,
    outcome_ref: "idea-outcome-001",
    outcome_kind: "IdeaSet",
    run_completion_receipt: {
      status: "accepted",
      issuer: "agent_runtime",
      kind: "run_execution_completed",
      receipt_ref: "ar-run-completion-receipt-001",
      subject_ref: RUN.run_ref,
      payload_hash: "9".repeat(64),
    },
    outcome_receipt: {
      status: "accepted",
      issuer: "research_graph",
      kind: "idea_outcome_accepted",
      receipt_ref: "rg-idea-outcome-receipt-001",
      subject_ref: "idea-outcome-001",
      payload_hash: "f".repeat(64),
    },
    receipt: RECEIPT,
    next_stage: "Plan",
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

async function freezeIdeaProjection(
  page: Page,
  initial: IdeaStageFixture,
  installEvents?: (route: Route) => Promise<void>,
  readiness?: Record<string, unknown>,
) {
  const running = runningProduct();
  await running.authenticate(page);
  const response = await page.request.get(`${running.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as Record<string, unknown>;
  let ideaStage = initial;
  let revision = Number(base.revision) + 10;

  await page.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...base,
        revision,
        ...(readiness ? { readiness } : {}),
        research_space: {
          status: "active",
          quest_count: 1,
          question_count: 1,
          foreground_cycle_count: 1,
          current_question: QUESTION,
        },
        idea_stage: ideaStage,
      }),
    });
  });
  if (installEvents) {
    await page.route("**/api/v1/events*", installEvents);
  }
  await page.goto(running.baseUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("product-shell")).toHaveAttribute(
    "data-shell-state",
    readiness ? "readiness-unavailable" : "ready-active",
  );
  return {
    update(next: IdeaStageFixture) {
      ideaStage = next;
      revision += 1;
    },
  };
}

test("readiness failure keeps durable Idea facts visible and names the worker blocker", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await freezeIdeaProjection(page, ADMITTED, undefined, {
    status: "unavailable",
    checks: [
      { name: "database", status: "ready" },
      { name: "projection", status: "ready", revision: 501 },
      {
        name: "idea_stage_worker",
        status: "unavailable",
        reason: { code: "provider_temporarily_unavailable" },
      },
    ],
  });

  await expect(page.getByTestId("product-shell")).toHaveAttribute(
    "data-shell-state",
    "readiness-unavailable",
  );
  await expect(page.getByTestId("current-question-card")).toContainText(
    QUESTION.unknown_statement,
  );
  const stageCard = page.getByTestId("idea-stage-card");
  await expect(stageCard.getByRole("listitem")).toHaveCount(5);
  await expect(stageCard.locator('[data-idea-slot="stage-run-request"]')).toContainText(
    "已冻结 AcceptedQuestionBinding 与 Idea ContextPack",
  );
  await expect(stageCard.locator('[data-idea-slot="run"]')).toContainText("admitted");
  await stageCard.getByText("查看 Idea 运行身份与 receipt", { exact: true }).click();
  await expect(stageCard.getByText(REQUEST.request_ref, { exact: true })).toBeVisible();
  await expect(stageCard.getByText(RUN.run_ref, { exact: true })).toBeVisible();

  const blocker = page.getByTestId("idea-stage-health-blocker");
  await expect(blocker).toContainText("Idea 自动推进暂时不可用");
  await expect(blocker).toContainText("已完成的请求和运行记录仍在");
  await expect(blocker).toContainText("provider_temporarily_unavailable");

  await expect(page.getByRole("banner")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(page.getByRole("button", { name: "创建 Quest" })).toBeEnabled();
  await expect(page.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByTestId("current-question-card")).toBeVisible();
  await expect(stageCard.getByRole("listitem")).toHaveCount(5);
  expect(
    await page.evaluate(() => ({
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    })),
  ).toEqual({ pageWidth: 390, viewportWidth: 390 });
});

test("the active Lumen shell keeps all five Idea facts separate", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const frozen = await freezeIdeaProjection(page, ELIGIBLE);
  const shell = page.getByTestId("product-shell");
  const stageCard = page.getByTestId("idea-stage-card");

  await expect(shell.getByRole("banner")).toBeVisible();
  await expect(shell.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(shell.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();
  expect(
    await page
      .getByRole("list", { name: "当前研究周期的四个 Stage" })
      .locator("b")
      .allTextContents(),
  ).toEqual(["Idea", "Plan", "Bundle", "Reasoning"]);
  await expect(page.getByTestId("current-question-card")).toContainText(
    QUESTION.unknown_statement,
  );
  await expect(stageCard.getByRole("listitem")).toHaveCount(5);
  await expect(stageCard.getByRole("listitem").allTextContents()).resolves.toEqual([
    expect.stringContaining("Idea eligibility"),
    expect.stringContaining("StageRunRequest"),
    expect.stringContaining("Run"),
    expect.stringContaining("awaiting acceptance"),
    expect.stringContaining("StageCommit"),
  ]);

  for (const scenario of [
    {
      projection: ELIGIBLE,
      state: "eligibility",
      slot: "eligibility",
      text: "首个 Idea Stage 已具备启动资格",
    },
    {
      projection: REQUESTED,
      state: "stage-run-request",
      slot: "stage-run-request",
      text: "已冻结 AcceptedQuestionBinding 与 Idea ContextPack",
    },
    {
      projection: ADMITTED,
      state: "run",
      slot: "run",
      text: "Agent Runtime 已 admission；实际 Idea Skill 尚未形成 Attempt 执行证据",
    },
    {
      projection: AWAITING,
      state: "awaiting-acceptance",
      slot: "outcome-acceptance",
      text: "Attempt 执行证据已形成；IdeaSet 正等待 Research Graph 接纳",
    },
    {
      projection: REJECTED,
      state: "awaiting-acceptance",
      slot: "outcome-acceptance",
      text: "IdeaSet 已被退回；current Session 将依据反馈修订重提",
      factState: "blocked",
    },
    {
      projection: COMMITTED,
      state: "stage-commit",
      slot: "stage-commit",
      text: "StageCommit(Completed) 已形成",
    },
  ]) {
    frozen.update(scenario.projection);
    await page.reload({ waitUntil: "domcontentloaded" });
    await expect(stageCard).toHaveAttribute("data-idea-stage-state", scenario.state);
    const current = stageCard.locator(
      `[data-idea-slot="${scenario.slot}"]`,
    );
    await expect(current).toContainText(scenario.text);
    await expect(current).toHaveAttribute(
      "data-state",
      scenario.factState ?? /current|done/,
    );

    if (scenario.projection === AWAITING) {
      const runRow = stageCard.locator('[data-idea-slot="run"]');
      await expect(runRow).toContainText(
        "Attempt 执行证据已形成；Run 等待 Owner 接纳后完成",
      );
      await expect(runRow).toContainText("awaiting_acceptance");
      await expect(runRow).not.toContainText("Run 已正式完成");

      const awaitingDetails = stageCard.getByText(
        "查看 Idea 运行身份与 receipt",
        { exact: true },
      );
      await awaitingDetails.click();
      await expect(
        stageCard.getByText("ar-attempt-execution-receipt-001", { exact: true }),
      ).toBeVisible();
      await expect(
        stageCard.getByText("idea_attempt_execution", { exact: true }),
      ).toBeVisible();
      await expect(
        stageCard.getByText("ar-run-completion-receipt-001", { exact: true }),
      ).toHaveCount(0);
      await expect(
        stageCard.getByText("run_execution_completed", { exact: true }),
      ).toHaveCount(0);
      await awaitingDetails.click();
    }

    if (scenario.projection === REJECTED) {
      const commitRow = stageCard.locator('[data-idea-slot="stage-commit"]');
      await expect(commitRow).toHaveAttribute("data-state", "pending");
      await expect(commitRow).toContainText("not_committed");
      const rejectedDetails = stageCard.getByText(
        "查看 Idea 运行身份与 receipt",
        { exact: true },
      );
      await rejectedDetails.click();
      await expect(
        stageCard.getByText("idea_outcome_requires_revision", { exact: true }),
      ).toBeVisible();
      await expect(
        stageCard.getByText("evidence_boundary_incomplete", { exact: true }),
      ).toBeVisible();
      await expect(
        stageCard.getByText("ae-stage-commit-receipt-001", { exact: true }),
      ).toHaveCount(0);
      await rejectedDetails.click();
    }
  }

  const committedRows = stageCard.getByRole("listitem");
  await expect(committedRows.nth(2)).toContainText("completed");
  await expect(committedRows.nth(2)).toContainText(
    "Owner 接纳已验证，Run 已正式完成",
  );
  await expect(committedRows.nth(3)).toContainText("accepted");
  await expect(committedRows.nth(4)).toContainText("Completed");
  await expect(stageCard.getByText("success", { exact: true })).toHaveCount(0);

  const details = stageCard.getByText("查看 Idea 运行身份与 receipt", {
    exact: true,
  });
  await details.click();
  await expect(stageCard.getByText(REQUEST.request_ref, { exact: true })).toBeVisible();
  await expect(stageCard.getByText("stage_run_request", { exact: true })).toBeVisible();
  await expect(stageCard.getByText(RUN.root_session_ref, { exact: true })).toBeVisible();
  await expect(
    stageCard
      .getByText("Submission", { exact: true })
      .locator("..").getByRole("definition"),
  ).toHaveText("idea-submission-001");
  await expect(
    stageCard.getByText("run_execution_completed", { exact: true }),
  ).toBeVisible();
  await expect(
    stageCard.getByText("idea_outcome_accepted", { exact: true }),
  ).toBeVisible();
  await expect(stageCard.getByText("stage_commit", { exact: true })).toBeVisible();
  await expect(stageCard.getByText(RECEIPT.receipt_ref, { exact: true })).toBeVisible();
  await expect(
    stageCard
      .getByText("Child reviewer agent", { exact: true })
      .locator("..").getByRole("definition"),
  ).toHaveText("agent-idea-reviewer-001");
  await expect(
    stageCard.getByText("Child-review provider turn", { exact: true }),
  ).toBeVisible();
  await expect(
    stageCard
      .getByText("Review mode", { exact: true })
      .locator("..").getByRole("definition"),
  ).toHaveText("harness_child_agent");
  await expect(
    stageCard.getByText("Independent reviewer", { exact: true }),
  ).toHaveCount(0);
  await expect(
    stageCard.getByText("Review provider operation", { exact: true }),
  ).toHaveCount(0);
});

test("Idea projection updates preserve focus and the fixed responsive order", async ({
  page,
}) => {
  let releaseCommit!: () => void;
  const commitGate = new Promise<void>((resolve) => {
    releaseCommit = resolve;
  });
  let eventRequest = 0;
  const frozen = await freezeIdeaProjection(page, ELIGIBLE, async (route) => {
    eventRequest += 1;
    if (eventRequest === 1) {
      await commitGate;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: "id: 500\nevent: advancement_engine.stage_committed\ndata: {\"stage\":\"Idea\"}\n\n",
      });
      return;
    }
    await route.abort("connectionrefused");
  });

  const details = page.getByText("查看 Idea 运行身份与 receipt", {
    exact: true,
  });
  await details.focus();
  await expect(details).toBeFocused();
  frozen.update(COMMITTED);
  releaseCommit();
  await expect(page.getByTestId("idea-stage-card")).toHaveAttribute(
    "data-idea-stage-state",
    "stage-commit",
  );
  await expect(details).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(details.locator("xpath=..")).toHaveAttribute("open", "");

  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 800, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const geometry = await page.evaluate(() => {
      const box = (selector: string) => {
        const rect = document.querySelector(selector)?.getBoundingClientRect();
        if (!rect) throw new Error(`missing ${selector}`);
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      };
      const strip = document.querySelector(".lumen-stage-strip");
      if (!(strip instanceof HTMLElement)) throw new Error("missing Stage strip");
      return {
        question: box("[data-testid=current-question-card]"),
        idea: box("[data-testid=idea-stage-card]"),
        companion: box("[data-shell-region=companion]"),
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        stageClientWidth: strip.clientWidth,
        stageScrollWidth: strip.scrollWidth,
      };
    });
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    if (viewport.width === 1440) {
      expect(geometry.question.x + geometry.question.width).toBeLessThanOrEqual(
        geometry.idea.x,
      );
      expect(geometry.idea.x + geometry.idea.width).toBeLessThanOrEqual(
        geometry.companion.x,
      );
    } else {
      expect(geometry.idea.y).toBeGreaterThanOrEqual(
        geometry.question.y + geometry.question.height - 1,
      );
      expect(geometry.companion.y).toBeGreaterThanOrEqual(
        geometry.idea.y + geometry.idea.height - 1,
      );
    }
    if (viewport.width === 390) {
      expect(geometry.stageScrollWidth).toBeGreaterThan(geometry.stageClientWidth);
    }
  }

  const summaryBox = await details.boundingBox();
  expect(summaryBox?.width ?? 0).toBeGreaterThanOrEqual(44);
  expect(summaryBox?.height ?? 0).toBeGreaterThanOrEqual(44);
});
