import { expect, test, type Page } from "@playwright/test";

import type { BundleStageProjection, PublicSnapshot } from "../src/api.js";
import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


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
        spec_hash: "d".repeat(64),
        dependency_refs: [],
        target_run_ref: "target-run-007-a",
        status: "committed",
        blocker: null,
      },
      {
        target_ref: "target-007-b",
        target_key: "gap-transfer",
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
        research_control: {
          status: "ready",
          quest_ref: QUESTION.quest_ref,
          foreground: {
            quest_ref: QUESTION.quest_ref,
            cycle_ref: "cycle-bundle-007",
            question_ref: QUESTION.question_ref,
            stage: "bundle",
            epoch: 1,
            status: "active",
            grant_ref: "bundle-grant-007",
            grant_status: "active",
            safe_point_ref: null,
            pending_operation_ref: null,
            owner_revision: 311,
          },
          managed_runs: [],
          recovery_records: [],
          actions: [],
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

async function publicBundleStage(page: Page): Promise<BundleStageProjection> {
  return await page.evaluate(async () => {
    const response = await fetch("/api/v1/bundle-stage/current", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error(`bundle current failed: ${response.status}`);
    }
    return response.json();
  }) as BundleStageProjection;
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

function acceptedReceiptRef(value: unknown): string {
  if (!value || typeof value !== "object" || !("receipt_ref" in value)) {
    throw new Error("accepted receipt is missing its identity");
  }
  const ref = value.receipt_ref;
  if (typeof ref !== "string" || !ref) {
    throw new Error("accepted receipt identity is invalid");
  }
  return ref;
}

test("a real BundleExhaustion exposes its public basis and remains a Cycle result", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await product?.stop();
  product = await DeterministicProduct.start({
    manualRoot: true,
    stagePipeline: "bundle-exhaustion",
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await openAuthenticatedProduct(page, runningProduct());
  await runningProduct().waitForPlanProviderPhase("plan-primary", 30_000);
  runningProduct().releasePlanProviderPhase("plan-primary");
  await runningProduct().waitForPlanProviderPhase("plan-review", 30_000);
  runningProduct().releasePlanProviderPhase("plan-review");

  await expect.poll(async () => (
    await publicBundleStage(page)
  ).disposition.report_disposition, { timeout: 45_000 }).toBe("exhausted");

  const bundleStage = await publicBundleStage(page);
  expect(bundleStage).toMatchObject({
    disposition: {
      status: "completed",
      report_disposition: "exhausted",
    },
    bundle_exhaustion: {
      kind: "BundleExhaustion",
      status: "accepted",
    },
    stage_commit: {
      status: "Completed",
      outcome_kind: "BundleExhaustion",
      disposition: "exhausted",
    },
  });
  expect(bundleStage.target_graph.targets).toEqual([]);
  expect(bundleStage.target_commits).toEqual([]);
  expect(bundleStage.run).toMatchObject({ status: "completed" });

  const snapshot = await publicSnapshot(page);
  expect(snapshot.bundle_stage).toEqual(bundleStage);
  const exhaustion = bundleStage.bundle_exhaustion;
  if (!exhaustion || !bundleStage.stage_commit) {
    throw new Error("real exhaustion closure is incomplete");
  }
  const basisRef = exhaustion.basis_ref;
  const basisReceiptRef = acceptedReceiptRef(exhaustion.basis_receipt);
  const decisionReceiptRef = acceptedReceiptRef(exhaustion.decision_receipt);
  expect(basisRef).toBe(bundleStage.stage_commit.basis_ref);
  expect(basisReceiptRef).toBe(
    acceptedReceiptRef(bundleStage.stage_commit.basis_receipt),
  );
  expect(decisionReceiptRef).toBeTruthy();

  const bundlePosition = page.getByRole("list", {
    name: "当前 Cycle 的四个可能 Stage",
  }).locator('[data-stage-position="bundle"]');
  await expect(bundlePosition).toHaveAttribute("data-stage-state", "result");
});

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

test("Target raw output is bounded, explicit, quiescent when hidden, and isolated per Target", async ({
  page,
}) => {
  type RawOutputRequest = {
    method: string;
    targetRef: string;
    after: number;
    limit: number;
  };
  const requests: RawOutputRequest[] = [];
  const unsafeRequests: string[] = [];
  const firstA = JSON.stringify({
    type: "item.completed",
    item: { type: "agent_message", text: "target A · first bounded page" },
  }) + "\n";
  const secondA = JSON.stringify({
    type: "item.started",
    item: { type: "mcp_tool_call", tool: "experiment.run" },
  }) + "\n";
  const firstB = JSON.stringify({
    type: "turn.started",
    target: "target B · independent live page",
  }) + "\n";
  const bytes = (value: string) => new TextEncoder().encode(value).byteLength;
  const firstABytes = bytes(firstA);
  const totalABytes = firstABytes + bytes(secondA);

  await page.route("**/api/v1/bundle/targets/*/raw-output?*", async (route) => {
    const url = new URL(route.request().url());
    const match = url.pathname.match(/\/targets\/([^/]+)\/raw-output$/);
    const targetRef = decodeURIComponent(match?.[1] ?? "");
    const after = Number(url.searchParams.get("after") ?? "0");
    const limit = Number(url.searchParams.get("limit") ?? "0");
    requests.push({ method: route.request().method(), targetRef, after, limit });
    expect(limit).toBe(65_536);

    const targetA = targetRef === "target-007-a";
    const suffix = targetA ? "a" : "b";
    const text = targetA
      ? after === 0 ? firstA : after === firstABytes ? secondA : ""
      : after === 0 ? firstB : "";
    const nextOffset = after + bytes(text);
    const invocationHash = (targetA ? "a" : "b").repeat(64);
    const sourceCaughtUp = true;
    const mappedBytes = targetA ? totalABytes : nextOffset;
    const status = targetA ? "complete" : "live";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        schema_ref: "meta-research/target-raw-output-page/v1",
        target_ref: targetRef,
        target_run_ref: `target-run-007-${suffix}`,
        attempt_ref: `attempt-${suffix}`,
        attempt_generation: 1,
        root_session_ref: `root-${suffix}`,
        native_session_ref: `native-${suffix}`,
        root_native_session_ref: `native-${suffix}`,
        fence_ref: `fence-${suffix}`,
        operation_ref: `operation-${suffix}`,
        operation_generation: 1,
        operation_status: targetA ? "executed" : "running",
        operation_outcome_code: null,
        transport_invocation_hash: invocationHash,
        stream_ref: `target-raw-output:${invocationHash}`,
        status,
        text,
        offset: after,
        next_offset: nextOffset,
        mapped_bytes: mappedBytes,
        source_bytes: mappedBytes,
        has_more: nextOffset < mappedBytes,
        source_caught_up: sourceCaughtUp,
        exact: true,
        unredacted: true,
      }),
    });
  });

  await page.setViewportSize({ width: 800, height: 900 });
  await freezeBundleProjection(page);
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      unsafeRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });

  const targetList = page.getByTestId("bundle-target-list");
  const targetA = targetList.locator('[data-target-ref="target-007-a"]');
  const targetB = targetList.locator('[data-target-ref="target-007-b"]');
  await expect(targetA).toContainText("gap-structure");
  await expect(targetA.getByText("target-007-a", { exact: true })).toBeHidden();
  await expect(targetA.getByText("target-run-007-a", { exact: true })).toBeHidden();
  await targetA.getByRole("button", { name: "查看原始输出" }).click();
  let terminal = page.getByRole("dialog", { name: "gap-structure 实验原始输出" });
  let log = terminal.getByRole("log", { name: /gap-structure.*Provider.*stdout/ });
  await expect(terminal).toBeVisible();
  await expect(log).toContainText(firstA.trim());
  await page.waitForTimeout(150);

  const targetARequests = () => requests.filter(
    (request) => request.targetRef === "target-007-a",
  );
  await expect.soft(targetARequests()).toEqual([{
    method: "GET",
    targetRef: "target-007-a",
    after: 0,
    limit: 65_536,
  }]);
  await expect.soft(log).not.toContainText(secondA.trim());
  const nextPage = terminal.getByRole("button", { name: "读取下一页" });
  await expect.soft(nextPage).toBeVisible();
  if (await nextPage.count()) {
    await nextPage.click();
    await expect(log).toContainText(secondA.trim());
    await expect(log).not.toContainText(firstA.trim());
    const previousPage = terminal.getByRole("button", { name: "返回上一页" });
    await expect(previousPage).toBeVisible();
    await previousPage.click();
    await expect(log).toContainText(firstA.trim());
    await expect(log).not.toContainText(secondA.trim());
  }
  expect(requests.filter((request) => request.targetRef === "target-007-b"))
    .toEqual([]);

  await terminal.getByRole("button", { name: "关闭原始输出窗口" }).click();
  await expect(terminal).toHaveCount(0);
  const targetACountAfterClose = targetARequests().length;
  await page.waitForTimeout(850);
  expect(targetARequests()).toHaveLength(targetACountAfterClose);

  await targetB.getByRole("button", { name: "查看原始输出" }).click();
  terminal = page.getByRole("dialog", { name: "gap-transfer 实验原始输出" });
  log = terminal.getByRole("log", { name: /gap-transfer.*Provider.*stdout/ });
  await expect(log).toContainText(firstB.trim());
  await expect(log).not.toContainText(/target A/);
  await terminal.getByRole("button", { name: "最小化" }).click();
  const targetBCountWhenMinimized = requests.filter(
    (request) => request.targetRef === "target-007-b",
  ).length;
  await page.waitForTimeout(900);
  expect.soft(requests.filter(
    (request) => request.targetRef === "target-007-b",
  )).toHaveLength(targetBCountWhenMinimized);

  await terminal.getByRole("button", { name: "展开" }).click();
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  await expect(page.locator("main.lumen-main")).toBeHidden();
  const targetBCountWhenMainHidden = requests.filter(
    (request) => request.targetRef === "target-007-b",
  ).length;
  await page.waitForTimeout(900);
  expect.soft(requests.filter(
    (request) => request.targetRef === "target-007-b",
  )).toHaveLength(targetBCountWhenMainHidden);

  await terminal.getByRole("button", { name: "关闭原始输出窗口" }).click();
  const targetBCountAfterClose = requests.filter(
    (request) => request.targetRef === "target-007-b",
  ).length;
  await page.waitForTimeout(900);
  expect(requests.filter(
    (request) => request.targetRef === "target-007-b",
  )).toHaveLength(targetBCountAfterClose);
  expect(requests.every((request) => request.method === "GET")).toBeTruthy();
  expect(unsafeRequests).toEqual([]);
});
