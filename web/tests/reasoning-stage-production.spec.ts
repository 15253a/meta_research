import { expect, test, type Locator, type Page } from "@playwright/test";

import type { PublicSnapshot, ReasoningStageProjection } from "../src/api.js";
import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 120_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start({
    stagePipeline: "reasoning-no-evidence",
  });
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
    "形成带反例、证据边界和可执行后继的比较结论",
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
  await dialog.getByRole("button", {
    name: /Deterministic GPU.*GPU-deterministic-1/,
  }).click();

  await dialog.getByRole("button", { name: "生成第一个问题" }).click();
  await expect(dialog.getByLabel("首问题标题")).toHaveValue(
    "低照度显微图像中的稀有形态保真",
    { timeout: 15_000 },
  );
  await expect(
    dialog.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", {
    name: "确认创建 Quest 与第一个问题",
  }).click();
  await expect(
    dialog.getByText("Quest 与第一个问题已就绪。", { exact: false }),
  ).toBeVisible({ timeout: 30_000 });
  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
}

async function publicReasoningStage(page: Page): Promise<ReasoningStageProjection> {
  return await page.evaluate(async () => {
    const response = await fetch("/api/v1/reasoning-stage/current", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`reasoning current failed: ${response.status}`);
    }
    return response.json();
  }) as ReasoningStageProjection;
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

test("Chrome observes the real Reasoning chain and keeps every Owner boundary separate", async ({
  page,
}) => {
  const reasoningWrites: Array<{ method: string; path: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/reasoning-stage") && request.method() !== "GET") {
      reasoningWrites.push({ method: request.method(), path: url.pathname });
    }
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await openAuthenticatedProduct(page, runningProduct());
  await createAcceptedQuestThroughWeb(page);

  await runningProduct().waitForReasoningProviderPhase(
    "reasoning-primary",
    30_000,
  );
  await expect.poll(async () => (
    await publicReasoningStage(page)
  ).run?.status).toBe("admitted");
  const admitted = await publicReasoningStage(page);
  expect(admitted).toMatchObject({
    eligibility: { status: "requested", next_stage: "Reasoning" },
    stage_run_request: {
      status: "current",
      stage: "Reasoning",
      receipt: {
        status: "accepted",
        issuer: "advancement_engine",
        kind: "stage_run_request",
      },
      context_pack: {
        question_literature_input: { kind: "none" },
        upstream_stage_closure: [
          { stage: "idea", disposition: "completed" },
          { stage: "plan", disposition: "skipped" },
          { stage: "bundle", disposition: "skipped" },
        ],
        accepted_target_commit_closures: [],
      },
    },
    run: {
      status: "admitted",
      primary_draft_checkpoint: null,
      attempt_execution_receipt: null,
      completion_receipt: null,
    },
    reasoning_acceptance: { status: "not_attempted" },
    transition: { status: "not_attempted" },
    stage_commit: null,
  });
  expect((await publicSnapshot(page)).reasoning_stage).toEqual(admitted);

  const card = page.getByTestId("reasoning-stage-card");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-reasoning-stage-state", "run");
  await expect(card.getByRole("listitem")).toHaveCount(7);
  await expect(card.locator('[data-reasoning-slot="run"]')).toContainText(
    "实际 Reasoning Skill 尚未形成 Attempt 执行证据",
  );
  await expect(page.getByTestId("current-question-card")).toContainText(
    "Current StageReasoning",
  );
  await expect(card.getByText(admitted.run?.run_ref ?? "missing")).toBeHidden();
  await card.getByText("查看 Reasoning closure、运行身份与 receipt").click();
  await expect(card.getByText(admitted.run?.run_ref ?? "missing")).toBeVisible();

  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(card).toBeVisible();
    expect(await page.evaluate(() => ({
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    }))).toEqual({ pageWidth: viewport.width, viewportWidth: viewport.width });
  }

  runningProduct().releaseReasoningProviderPhase("reasoning-primary");
  await runningProduct().waitForReasoningProviderPhase("reasoning-review");
  await expect.poll(async () => (
    await publicReasoningStage(page)
  ).run?.primary_draft_checkpoint?.status).toBe("recorded");

  runningProduct().releaseReasoningProviderPhase("reasoning-review");
  await expect.poll(async () => (
    await publicReasoningStage(page)
  ).stage_commit?.status, { timeout: 30_000 }).toBe("Completed");

  const committed = await publicReasoningStage(page);
  expect(committed).toMatchObject({
    eligibility: { status: "consumed" },
    run: {
      status: "completed",
      primary_draft_checkpoint: {
        adapter_kind: "chrome_deterministic_reasoning",
      },
      attempt_execution_receipt: {
        status: "accepted",
        issuer: "agent_runtime",
        kind: "reasoning_attempt_execution",
      },
      completion_receipt: {
        status: "accepted",
        issuer: "agent_runtime",
        kind: "run_execution_completed",
      },
      review: {
        status: "completed",
        review_mode: "harness_child_agent",
        reviewer_agent_ref: "chrome-reasoning-child-reviewer",
      },
    },
    reasoning_acceptance: {
      status: "accepted",
      disposition: "insufficient_evidence",
      content: {
        status: "accepted",
        receipt: {
          status: "accepted",
          issuer: "research_memory",
          kind: "reasoning_content_acceptance",
        },
      },
      domain: {
        status: "accepted",
        receipt: {
          status: "accepted",
          issuer: "research_graph",
          kind: "reasoning_outcome_accepted",
        },
      },
    },
    transition: {
      status: "proposed",
      kind: "CandidateCompletion",
      is_authoritative: false,
    },
    stage_commit: {
      status: "Completed",
      stage: "Reasoning",
      outcome_kind: "ScientificOutcome",
      disposition: "insufficient_evidence",
      transition_kind: "CandidateCompletion",
      receipt: {
        status: "accepted",
        issuer: "advancement_engine",
        kind: "stage_commit",
      },
    },
  });

  await expect(card).toHaveAttribute(
    "data-reasoning-stage-state",
    "stage-commit",
  );
  await expect(page.getByTestId("reasoning-outcome")).toContainText(
    "insufficient_evidence",
  );
  await expect(page.getByTestId("reasoning-transition")).toContainText(
    "CandidateCompletion",
  );
  await expect(
    page.getByRole("button", { name: /启动.*Reasoning|Reasoning.*启动/ }),
  ).toHaveCount(0);
  expect(reasoningWrites).toEqual([]);
});
