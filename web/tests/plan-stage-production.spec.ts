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
  const goal = dialog.getByLabel("这个 Quest 最终要完成什么？");
  await goal.fill("判断低照度显微图像去噪能否保留稀有形态");
  await dialog.getByLabel("什么情况算完成？").fill(
    "形成带反例、证据边界和可执行 gap 的比较结论",
  );
  await goal.blur();
  await expect(dialog.getByText("草案已自动保存", { exact: true })).toBeVisible();

  await dialog.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText(
      "capability_unavailable · deterministic_probe_unavailable",
      { exact: true },
    ),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "重新检测", exact: true }).click();
  const device = dialog.getByRole("button", {
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
  await expect(
    dialog.getByText("Quest 与第一个问题已就绪。", { exact: false }),
  ).toBeVisible({ timeout: 30_000 });
  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
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
        review_mode: "harness_child_agent",
        reviewer_agent_ref: "chrome-plan-child-reviewer",
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
  await expect(card).toHaveAttribute("data-plan-stage-state", "stage-commit");
  await expect(card.locator('[data-state="done"]')).toHaveCount(5);
  await expect(
    card.locator('[data-plan-owner-layer="content"]'),
  ).toContainText("accepted");
  await expect(
    card.locator('[data-plan-owner-layer="domain"]'),
  ).toContainText("accepted");

  await card.getByText("查看 Plan 运行身份与 receipt", { exact: true }).click();
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
    await expect(card.getByText(fact as string, { exact: true })).toBeVisible();
  }

  await expect(
    page.getByRole("button", { name: /启动.*Plan|Plan.*启动/ }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /授权.*Run|Run.*授权/ }),
  ).toHaveCount(0);
  expect(planWrites).toEqual([]);
});
