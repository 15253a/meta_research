import { expect, test, type Locator, type Page } from "@playwright/test";

import type {
  AutonomousCreationView,
  PublicSnapshot,
  QuestCompletionView,
  ReasoningStageProjection,
} from "../src/api.js";
import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


test.describe.configure({ timeout: 150_000 });

async function openCreation(page: Page): Promise<Locator> {
  await page.getByRole("button", { name: "创建 Quest" }).click();
  const dialog = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(dialog).toBeVisible();
  return dialog;
}

async function createAcceptedQuestThroughWeb(
  page: Page,
  options: { deepfetch: boolean } = { deepfetch: false },
): Promise<void> {
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

  if (options.deepfetch) {
    const deepfetch = dialog.getByRole("button", {
      name: "先运行 DeepFetch",
      exact: true,
    });
    await deepfetch.click();
    await expect(deepfetch).toHaveAttribute("aria-pressed", "true");
    await dialog.getByLabel("文献搜索范围").selectOption("oa_only");
    await expect(dialog.getByTestId("acquisition-session-status")).toContainText(
      "ready · current",
    );
  }

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

async function readPublic<T>(page: Page, path: string): Promise<T> {
  return await page.evaluate(async (requestPath) => {
    const response = await fetch(requestPath, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`${requestPath} failed: ${response.status}`);
    }
    return response.json();
  }, path) as T;
}

async function publicSnapshot(page: Page): Promise<PublicSnapshot> {
  return readPublic(page, "/api/v1/snapshot");
}

async function publicReasoning(page: Page): Promise<ReasoningStageProjection> {
  return readPublic(page, "/api/v1/reasoning-stage/current");
}

async function publicAutonomous(
  page: Page,
): Promise<AutonomousCreationView | null> {
  return readPublic(page, "/api/v1/autonomous-creations/current");
}

async function publicCompletion(
  page: Page,
): Promise<QuestCompletionView | null> {
  return readPublic(page, "/api/v1/quest-completions/current");
}

async function assertResponsiveWithoutOverflow(
  page: Page,
  card: Locator,
): Promise<void> {
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
}

test("Chrome observes mandatory AutonomousCreation without a browser-authored effect", async ({
  page,
}) => {
  const product = await DeterministicProduct.start({
    stagePipeline: "reasoning-autonomous",
  });
  const autonomousWrites: Array<{ method: string; path: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname.includes("/autonomous-creations")
      && request.method() !== "GET"
    ) {
      autonomousWrites.push({ method: request.method(), path: url.pathname });
    }
  });

  try {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await openAuthenticatedProduct(page, product);
    await createAcceptedQuestThroughWeb(page, { deepfetch: true });

    await product.waitForReasoningProviderPhase("reasoning-primary", 30_000);
    product.releaseReasoningProviderPhase("reasoning-primary");
    await product.waitForReasoningProviderPhase("reasoning-review", 30_000);
    product.releaseReasoningProviderPhase("reasoning-review");

    await expect.poll(async () => (await publicAutonomous(page))?.status, {
      timeout: 60_000,
    }).toBe("ready_for_reasoning_resume");
    const autonomous = await publicAutonomous(page);
    expect(autonomous).not.toBeNull();
    expect(autonomous).toMatchObject({
      creation_mode: "AutonomousCreation",
      status: "ready_for_reasoning_resume",
      deepfetch: {
        required: true,
        waiver_allowed: false,
        human_authorization_required: false,
        status: "succeeded",
      },
      waiver: null,
      human_confirmation: null,
      content_acceptance: { status: "accepted" },
      graph_presence_fact: { value: "present", is_current: true },
      question_research_state_fact: { value: "open", is_current: true },
      next_cycle_proposal: null,
      successor_cycle: null,
    });
    expect(autonomous?.deepfetch.request_receipt?.issuer).toBe(
      "advancement_engine",
    );
    expect(autonomous?.content_acceptance.receipt?.issuer).toBe(
      "research_memory",
    );
    expect(autonomous?.literature_revision).not.toBeNull();

    const snapshot = await publicSnapshot(page);
    expect(snapshot.autonomous_creation).toEqual({
      status: "ready",
      creation_mode: "AutonomousCreation",
      current: autonomous,
    });

    const card = page.getByTestId("autonomous-creation-card");
    await expect(card).toHaveAttribute(
      "data-autonomous-creation-state",
      "ready_for_reasoning_resume",
    );
    await expect(card).toContainText("DeepFetch 必须执行");
    await expect(card).toContainText("无额外人工确认 · 无 waiver");
    await expect(
      card.getByRole("button", { name: /确认|waiver|豁免/i }),
    ).toHaveCount(0);
    await card.getByText("查看创建来源、DeepFetch 与 Owner receipt").click();
    await expect(card).toContainText(autonomous?.checkpoint.ref ?? "missing");
    await assertResponsiveWithoutOverflow(page, card);
    expect(autonomousWrites).toEqual([]);
    await expect.poll(async () => (
      await publicReasoning(page)
    ).stage_commit?.transition_kind, { timeout: 30_000 }).toBe(
      "NextCycleProposal",
    );
  } finally {
    await product.stop();
  }
});

test("Chrome requires the exact HC preview before RG acceptance and AE ending", async ({
  page,
}) => {
  const product = await DeterministicProduct.start({
    stagePipeline: "quest-completion",
  });
  const completionWrites: Array<{ method: string; path: string }> = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname.includes("/quest-completions")
      && request.method() !== "GET"
    ) {
      completionWrites.push({ method: request.method(), path: url.pathname });
    }
  });

  try {
    await page.setViewportSize({ width: 1440, height: 1000 });
    await openAuthenticatedProduct(page, product);
    await createAcceptedQuestThroughWeb(page);

    await product.waitForReasoningProviderPhase("reasoning-primary", 30_000);
    product.releaseReasoningProviderPhase("reasoning-primary");
    await product.waitForReasoningProviderPhase("reasoning-review", 30_000);
    product.releaseReasoningProviderPhase("reasoning-review");
    await expect.poll(async () => (
      await publicReasoning(page)
    ).stage_commit?.transition_kind, { timeout: 30_000 }).toBe(
      "CandidateCompletion",
    );

    const card = page.getByTestId("quest-completion-card");
    await expect(card).toHaveAttribute("data-quest-completion-state", "candidate");
    await card.getByRole("button", {
      name: "审阅 Quest 结束提案",
      exact: true,
    }).click();

    await expect.poll(async () => (
      await publicCompletion(page)
    )?.status, { timeout: 30_000 }).toBe("awaiting_human_confirmation");
    const awaiting = await publicCompletion(page);
    expect(awaiting?.human_confirmation.preview).toMatchObject({
      status: "current",
      candidate_completion_ref: awaiting?.candidate_completion_ref,
      candidate_completion_hash: awaiting?.candidate_completion_hash,
      quest_ref: awaiting?.quest.quest_ref,
      goal_revision_ref: awaiting?.goal_revision.goal_revision_ref,
    });
    expect(awaiting?.human_confirmation.decision).toBeNull();
    expect(awaiting?.domain_acceptance).toEqual({ status: "not_attempted" });
    expect(awaiting?.ending_transition).toBeNull();

    await expect(card.getByRole("button", {
      name: "确认结束 Quest",
      exact: true,
    })).toBeVisible();
    await card.getByRole("button", {
      name: "确认结束 Quest",
      exact: true,
    }).click();

    await expect.poll(async () => (
      await publicCompletion(page)
    )?.status, { timeout: 30_000 }).toBe("ended");
    const ended = await publicCompletion(page);
    expect(ended).toMatchObject({
      status: "ended",
      quest: { status: "ended" },
      human_confirmation: {
        status: "confirmed",
        decision: {
          decision: "confirmed",
          receipt: { issuer: "human_collaboration" },
        },
      },
      domain_acceptance: {
        status: "accepted",
        receipt: { issuer: "research_graph" },
      },
      ending_transition: {
        status: "ended",
        receipt: { issuer: "advancement_engine" },
      },
      successor_cycle: null,
    });
    expect((await publicSnapshot(page)).quest_completion).toEqual({
      status: "ready",
      current: ended,
    });

    await expect(card).toHaveAttribute("data-quest-completion-state", "ended");
    await expect(card).toContainText("Quest 已结束");
    await card.getByText("查看 Goal、里程碑与 Owner receipt").click();
    await expect(card).toContainText(ended?.candidate_completion_ref ?? "missing");
    await assertResponsiveWithoutOverflow(page, card);
    expect(completionWrites).toEqual([
      { method: "POST", path: "/api/v1/quest-completions" },
      {
        method: "POST",
        path: `/api/v1/quest-completions/${ended?.context_ref}/decision`,
      },
    ]);
  } finally {
    await product.stop();
  }
});
