import { expect, test, type Locator, type Page } from "@playwright/test";

import type { PlanStageProjection, PublicSnapshot } from "../src/api.js";
import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 120_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start({ stagePipeline: "plan-gap" });
});

test.afterEach(async () => {
  await product?.stop();
  product = undefined;
});

function runningProduct(): DeterministicProduct {
  if (!product) throw new Error("deterministic product is not running");
  return product;
}

async function openCreation(page: Page): Promise<Locator> {
  await page.getByRole("button", { name: "创建 Quest" }).click();
  const dialog = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function createAcceptedQuestThroughWeb(page: Page): Promise<void> {
  const dialog = await openCreation(page);
  const goal = dialog.getByRole("textbox", { name: "目标", exact: true });
  await goal.fill("判断低照度显微图像去噪能否保留稀有形态");
  await dialog.getByRole("textbox", { name: "边界", exact: true }).fill(
    "形成带反例、证据边界和可执行 gap 的比较结论",
  );
  await goal.blur();
  await expect(dialog.getByText("草案已自动保存", { exact: true })).toBeVisible();

  const computeCard = dialog.getByLabel("本机计算卡");
  await computeCard.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText(
      "capability_unavailable · deterministic_probe_unavailable",
      { exact: true },
    ),
  ).toBeVisible();
  await computeCard.getByRole("button", { name: "重新检测", exact: true }).click();
  const device = computeCard.getByRole("button", {
    name: /Deterministic GPU.*GPU-deterministic-1/,
  });
  await expect(device).toBeVisible();
  await device.click();
  await expect(
    dialog.getByText("已绑定 1 张实际检测设备。", { exact: false }),
  ).toBeVisible();

  await dialog.getByRole("button", { name: "生成第一个问题" }).click();
  await expect(dialog.getByLabel("首问题标题")).toHaveValue(
    "低照度显微图像中的稀有形态保真",
    { timeout: 15_000 },
  );
  await expect(
    dialog.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();

  const confirmation = dialog.getByRole("button", {
    name: "确认创建 Quest 与第一个问题",
  });
  await expect(confirmation).toBeEnabled();
  await confirmation.click();
  await expect.poll(async () => {
    try {
      const snapshot = await publicSnapshot(page);
      return snapshot.question_tree.status === "ready"
        && snapshot.question_tree.items.length > 0
        && snapshot.research_control.foreground
        ? "completed"
        : "pending";
    } catch {
      return "snapshot_unavailable";
    }
  }, { timeout: 30_000 }).toBe("completed");
  if (await dialog.isVisible()) {
    await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  }
  await page.getByRole("button", { name: "Quest 总览", exact: true }).click();
  await expect(page.getByTestId("current-cycle-overview")).toBeVisible();
}

async function publicPlanStage(page: Page): Promise<PlanStageProjection> {
  return await page.evaluate(async () => {
    const response = await fetch("/api/v1/plan-stage/current", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`plan current failed: ${response.status}`);
    return response.json();
  }) as PlanStageProjection;
}

async function publicSnapshot(page: Page): Promise<PublicSnapshot> {
  return await page.evaluate(async () => {
    const response = await fetch("/api/v1/snapshot", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`snapshot failed: ${response.status}`);
    return response.json();
  }) as PublicSnapshot;
}

function receiptRef(value: unknown): string {
  if (!value || typeof value !== "object" || !("receipt_ref" in value)) {
    throw new Error("accepted receipt is missing its identity");
  }
  const ref = value.receipt_ref;
  if (typeof ref !== "string" || !ref) {
    throw new Error("accepted receipt identity is invalid");
  }
  return ref;
}

test("Chrome observes the daemon execute a real Plan StageRun through every Owner boundary", async ({
  page,
}) => {
  const planWrites: Array<{ method: string; path: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/plan-stage") && request.method() !== "GET") {
      planWrites.push({ method: request.method(), path: url.pathname });
    }
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await openAuthenticatedProduct(page, runningProduct());
  await createAcceptedQuestThroughWeb(page);

  // The marker comes from inside generate_draft. Nothing in this test calls a
  // Stage worker or Owner method, so the production lifespan daemon must have
  // admitted the Run and invoked the configured external Provider.
  await runningProduct().waitForPlanProviderPhase("plan-primary", 30_000);
  await expect.poll(async () => (
    await publicPlanStage(page)
  ).run?.status).toBe("admitted");
  const admitted = await publicPlanStage(page);
  expect(admitted).toMatchObject({
    eligibility: { status: "requested" },
    stage_run_request: {
      status: "current",
      stage: "Plan",
      receipt: {
        status: "accepted",
        issuer: "advancement_engine",
        kind: "stage_run_request",
      },
    },
    run: {
      status: "admitted",
      primary_draft_checkpoint: null,
      attempt_execution_receipt: null,
      completion_receipt: null,
    },
    plan_acceptance: { status: "not_attempted" },
    stage_commit: null,
  });
  const admittedSnapshot = await publicSnapshot(page);
  expect(admittedSnapshot.plan_stage).toEqual(admitted);
  expect(
    admittedSnapshot.readiness.checks.find(
      (check) => check.name === "plan_stage_worker",
    )?.status,
  ).toBe("ready");

  const card = page.getByTestId("plan-stage-card");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-plan-stage-state", "run");
  await expect(card.locator('[data-plan-slot="run"]')).toContainText(
    "实际 Plan Skill 尚未形成 Attempt 执行证据",
  );

  runningProduct().releasePlanProviderPhase("plan-primary");
  await runningProduct().waitForPlanProviderPhase("plan-review");
  await expect.poll(async () => (
    await publicPlanStage(page)
  ).run?.primary_draft_checkpoint?.status).toBe("recorded");
  const awaitingReview = await publicPlanStage(page);
  expect(awaitingReview.run).toMatchObject({
    status: "admitted",
    provider_operations: {
      primary: { status: "completed" },
      review: { status: "prepared" },
    },
    primary_draft_checkpoint: {
      status: "recorded",
      adapter_kind: "chrome_deterministic_plan",
    },
    attempt_execution_receipt: null,
  });
  await expect(card).toHaveAttribute("data-plan-stage-state", "run");

  runningProduct().releasePlanProviderPhase("plan-review");
  await expect.poll(async () => (
    await publicPlanStage(page)
  ).stage_commit?.status, { timeout: 30_000 }).toBe("Completed");

  const committed = await publicPlanStage(page);
  expect(committed).toMatchObject({
    eligibility: { status: "consumed" },
    run: {
      status: "completed",
      primary_draft_checkpoint: {
        adapter_kind: "chrome_deterministic_plan",
      },
      attempt_execution_receipt: {
        status: "accepted",
        issuer: "agent_runtime",
        kind: "plan_attempt_execution",
      },
      completion_receipt: {
        status: "accepted",
        issuer: "agent_runtime",
        kind: "run_execution_completed",
      },
      review: {
        status: "completed",
        review_mode: "advisory_unobserved",
      },
    },
    plan_acceptance: {
      status: "accepted",
      bundle_disposition: "experiments_required",
      gap_count: 1,
      experiment_brief_count: 1,
      content: {
        status: "accepted",
        receipt: {
          status: "accepted",
          issuer: "research_memory",
          kind: "plan_document_content_acceptance",
        },
      },
      domain: {
        status: "accepted",
        receipt: {
          status: "accepted",
          issuer: "research_graph",
          kind: "formal_plan_accepted",
        },
      },
    },
    stage_commit: {
      status: "Completed",
      stage: "Plan",
      outcome_kind: "FormalPlan",
      next_stage: "Bundle",
      receipt: {
        status: "accepted",
        issuer: "advancement_engine",
        kind: "stage_commit",
      },
    },
  });
  expect(committed).not.toHaveProperty("bundle_run");

  const committedSnapshot = await publicSnapshot(page);
  expect(committedSnapshot.plan_stage).toEqual(committed);
  await expect.poll(async () => (
    await publicSnapshot(page)
  ).bundle_stage?.stage_run_request?.accepted_formal_plan_binding, {
    timeout: 30_000,
  }).toMatchObject({
    formal_plan_ref: committed.plan_acceptance.formal_plan_ref,
    stage_commit_ref: committed.stage_commit?.stage_commit_ref,
  });
  const transitionedSnapshot = await publicSnapshot(page);
  expect(transitionedSnapshot.plan_stage).toEqual(committed);
  expect(
    transitionedSnapshot.bundle_stage?.stage_run_request
      ?.accepted_formal_plan_binding,
  ).toMatchObject({
    formal_plan_ref: committed.plan_acceptance.formal_plan_ref,
    stage_commit_ref: committed.stage_commit?.stage_commit_ref,
  });
  const transitionedForeground = transitionedSnapshot.research_control.foreground;
  if (!transitionedForeground) {
    throw new Error("Bundle transition has no foreground");
  }

  // The main workspace always shows the highest current Stage. Plan remains a
  // complete Snapshot fact while Bundle takes over the current Stage surface.
  await expect(card).toHaveCount(0);
  const bundleCard = page.getByTestId("bundle-stage-card");
  await expect(bundleCard).toBeVisible();
  await expect(page.getByTestId("current-question-card")).toContainText(
    transitionedForeground.question_ref,
  );
  const stageStrip = page.getByRole("list", {
    name: "当前 Cycle 的四个可能 Stage",
  });
  await expect(stageStrip.locator('[data-stage-position="plan"]'))
    .toHaveAttribute("data-stage-state", "result");
  await expect(stageStrip.locator('[data-stage-position="bundle"]'))
    .toHaveAttribute("data-stage-state", "current");

  const dynamicFacts = [
    committed.stage_run_request?.request_ref,
    receiptRef(committed.stage_run_request?.receipt),
    committed.run?.run_ref,
    receiptRef(committed.run?.attempt_execution_receipt),
    receiptRef(committed.run?.completion_receipt),
    committed.plan_acceptance.plan_document_ref,
    receiptRef(committed.plan_acceptance.content.receipt),
    committed.plan_acceptance.formal_plan_ref,
    receiptRef(committed.plan_acceptance.domain.receipt),
    committed.stage_commit?.stage_commit_ref,
    receiptRef(committed.stage_commit?.receipt),
  ];
  for (const fact of dynamicFacts) {
    expect(typeof fact).toBe("string");
  }

  await expect(
    page.getByRole("button", { name: /启动.*Plan|Plan.*启动/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /授权.*Run|Run.*授权/ }),
  ).toHaveCount(0);
  expect(planWrites).toEqual([]);
});
