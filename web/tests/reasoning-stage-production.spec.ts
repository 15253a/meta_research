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
  const goal = dialog.getByRole("textbox", { name: "目标", exact: true });
  await goal.fill("判断低照度显微图像去噪能否保留稀有形态");
  await dialog.getByRole("textbox", { name: "边界", exact: true }).fill(
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
  const observationWrites: Array<{ method: string; path: string }> = [];
  let observingPublicState = false;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.includes("/reasoning-stage") && request.method() !== "GET") {
      reasoningWrites.push({ method: request.method(), path: url.pathname });
    }
    if (
      observingPublicState
      && !["GET", "HEAD", "OPTIONS"].includes(request.method())
    ) {
      observationWrites.push({ method: request.method(), path: url.pathname });
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
  const admittedSnapshot = await publicSnapshot(page);
  expect(admittedSnapshot.idea_stage).toMatchObject({
    outcome_acceptance: {
      status: "accepted",
      outcome_kind: "NoViableCandidate",
    },
    stage_commit: {
      status: "Completed",
      outcome_kind: "NoViableCandidate",
      next_stage: "Reasoning",
    },
  });
  const foreground = admittedSnapshot.research_control.foreground;
  if (!foreground) throw new Error("real Reasoning chain has no foreground");
  expect(foreground).toMatchObject({
    quest_ref: admitted.stage_run_request?.accepted_question_binding?.quest_ref,
    cycle_ref: admitted.eligibility.cycle_ref,
    question_ref: admitted.eligibility.question_ref,
    stage: "reasoning",
    epoch: admitted.stage_run_request?.epoch,
    status: "active",
  });
  expect(admittedSnapshot.question_tree.status).toBe("ready");
  const currentQuestion = admittedSnapshot.question_tree.items.find(
    (item) => item.question_ref === foreground.question_ref,
  );
  if (!currentQuestion) throw new Error("foreground Question is not in the tree");
  expect(currentQuestion).toMatchObject({
    quest_ref: foreground.quest_ref,
    lifecycle_status: "active",
    cycle_binding: {
      status: "bound",
      cycle_ref: foreground.cycle_ref,
      foreground: {
        question_ref: foreground.question_ref,
        stage: "reasoning",
        epoch: foreground.epoch,
        status: "active",
      },
    },
    related_human_requests: { status: "ready", items: [] },
    furthest_accepted_stage_result: {
      status: "accepted",
      stage: "Idea",
      kind: "NoViableCandidate",
    },
  });
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

  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  const currentTreeItem = tree.locator(
    `[data-question-ref="${foreground.question_ref}"]`,
  );
  await currentTreeItem.hover();
  await tree.getByRole("button", {
    name: `在 ${foreground.question_ref} 下创建子问题`,
  }).click();
  const manualDialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await manualDialog.locator("#manual-seed-intent").fill(
    "追问稀有形态保真结论在哪些边界下会失效。",
  );
  await manualDialog.getByLabel("问题标题，可选", { exact: true }).fill(
    "稀有形态保真的失效边界",
  );
  await manualDialog.getByLabel("要解决的未知，可选", { exact: true }).fill(
    "哪些噪声与形态组合会推翻当前结论？",
  );
  await manualDialog.getByLabel("合格答案的形状，可选", { exact: true }).fill(
    "给出失效边界、反例与可验证指标。",
  );
  await manualDialog.getByLabel("适用范围与排除项，可选", { exact: true }).fill(
    "仅用于后续分支，不改变当前 Reasoning Cycle。",
  );
  await manualDialog.getByRole("button", {
    name: "确认当前 Seed，开始讨论",
  }).click();
  await expect(manualDialog.getByText("CreationSeed 已冻结", { exact: true }))
    .toBeVisible();
  await manualDialog.getByRole("button", {
    name: "确认本次不运行 DeepFetch",
  }).click();
  await expect(manualDialog.getByText(/explicit waiver · accepted/)).toBeVisible();
  await manualDialog.getByRole("button", { name: "确认最终问题" }).click();
  await expect.poll(async () => {
    const snapshot = await publicSnapshot(page);
    return snapshot.question_tree.items.some(
      (item) => item.parent_question_ref === foreground.question_ref,
    );
  }, { timeout: 30_000 }).toBe(true);
  const branchedSnapshot = await publicSnapshot(page);
  expect(branchedSnapshot.question_tree.items).toHaveLength(2);
  expect(branchedSnapshot.research_control.foreground).toEqual(foreground);
  const childQuestion = branchedSnapshot.question_tree.items.find(
    (item) => item.parent_question_ref === foreground.question_ref,
  );
  if (!childQuestion) throw new Error("ManualCreation child is not in the tree");
  expect(childQuestion).toMatchObject({
    quest_ref: foreground.quest_ref,
    lifecycle_status: "active",
    cycle_binding: {
      status: "not_bound",
      cycle_ref: null,
      foreground: null,
      reason: { code: "current_foreground_not_bound" },
    },
  });
  expect(childQuestion.question_ref).not.toBe(currentQuestion.question_ref);
  expect(childQuestion).not.toHaveProperty("furthest_accepted_stage_result");
  await expect(manualDialog.getByText(/稳定 QuestionAnchor/)).toBeVisible();
  await manualDialog.getByRole("button", { name: "关闭创建 Question 窗口" }).click();

  observingPublicState = true;

  const childTreeItem = tree.locator(
    `[data-question-ref="${childQuestion.question_ref}"]`,
  );
  await expect(currentTreeItem).toHaveAttribute("data-current-question", "true");
  await expect(childTreeItem).not.toHaveAttribute("data-current-question", "true");
  await childTreeItem.click();
  const inspector = tree.getByLabel("选中问题详情");
  await expect(inspector).toContainText("未绑定当前 Cycle");
  await expect(inspector).toContainText("Projection 未提供");
  await expect(inspector.getByText("当前攻克", { exact: true })).toHaveCount(0);
  await currentTreeItem.click();
  await expect(inspector).toContainText(
    `${foreground.cycle_ref} · reasoning · epoch ${foreground.epoch} · active`,
  );
  await expect(inspector.getByText("当前攻克", { exact: true })).toBeVisible();
  await expect(inspector).toContainText("当前没有关联 HumanRequest");
  await expect(inspector).toContainText("最远已接纳 Stage 结果");
  await expect(inspector).toContainText("NoViableCandidate · accepted");
  await page.getByRole("button", { name: "回到总览" }).click();

  const overview = page.getByTestId("current-cycle-overview");
  await expect(overview).toHaveAttribute("data-cycle-ref", foreground.cycle_ref);
  await expect(overview).toHaveAttribute(
    "data-question-ref",
    foreground.question_ref,
  );
  const stages = page.getByRole("list", {
    name: "当前 Cycle 的四个可能 Stage",
  });
  await expect(stages.getByRole("listitem")).toHaveCount(4);
  for (const [position, state] of Object.entries({
    idea: "result",
    plan: "skipped",
    bundle: "skipped",
    reasoning: "current",
  })) {
    await expect(stages.locator(`[data-stage-position="${position}"]`))
      .toHaveAttribute("data-stage-state", state);
  }
  await expect(stages).not.toContainText(/Writing.*Stage|Companion.*Stage/);

  const card = page.getByTestId("reasoning-stage-card");
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute("data-reasoning-stage-state", "run");
  await card.getByText("系统如何核验这段研究", { exact: true }).click();
  await expect(card.getByRole("listitem")).toHaveCount(7);
  await expect(card.locator('[data-reasoning-slot="run"]')).toContainText(
    "实际 Reasoning Skill 尚未形成 Attempt 执行证据",
  );
  await expect(page.getByTestId("current-question-card")).toContainText(
    foreground.question_ref,
  );
  await expect(card.getByText(admitted.run?.run_ref ?? "missing")).toBeHidden();
  await card.getByText("技术身份与核验记录", { exact: true }).click();
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
        review_mode: "advisory_unobserved",
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
    "已经形成可审阅判断",
  );
  await expect(page.getByTestId("reasoning-transition")).toContainText(
    "审阅是否结束当前研究",
  );
  await expect(
    page.getByRole("button", { name: /启动.*Reasoning|Reasoning.*启动/ }),
  ).toHaveCount(0);
  expect(reasoningWrites).toEqual([]);
  expect(observationWrites).toEqual([]);
});
