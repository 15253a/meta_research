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

test("an open Target terminal pages redacted root output through the one Projection stream", async ({
  page,
}) => {
  await page.addInitScript(() => {
    class ProjectionEventSource {
      static instances: ProjectionEventSource[] = [];
      readonly url: string;
      closed = false;
      onopen: ((event: Event) => void) | null = null;
      onerror: ((event: Event) => void) | null = null;
      private readonly listeners = new Map<
        string,
        Array<(event: Event) => void>
      >();

      constructor(url: string | URL) {
        this.url = String(url);
        ProjectionEventSource.instances.push(this);
        queueMicrotask(() => {
          if (!this.closed) this.onopen?.(new Event("open"));
        });
      }

      addEventListener(type: string, listener: (event: Event) => void) {
        const listeners = this.listeners.get(type) ?? [];
        listeners.push(listener);
        this.listeners.set(type, listeners);
      }

      close() {
        this.closed = true;
      }

      emit(type: string, payload: object, lastEventId = "") {
        const event = new MessageEvent(type, {
          data: JSON.stringify(payload),
          lastEventId,
        });
        for (const listener of this.listeners.get(type) ?? []) listener(event);
      }
    }

    Object.defineProperty(window, "EventSource", {
      configurable: true,
      value: ProjectionEventSource,
    });
    const testingWindow = window as typeof window & {
      __emitProjectionEvent: (type: string, payload: object) => void;
      __activeProjectionStreamCount: () => number;
    };
    testingWindow.__emitProjectionEvent = (type, payload) => {
      let stream: ProjectionEventSource | undefined;
      for (let index = ProjectionEventSource.instances.length - 1; index >= 0; index -= 1) {
        const candidate = ProjectionEventSource.instances[index];
        if (!candidate.closed) {
          stream = candidate;
          break;
        }
      }
      if (!stream) throw new Error("Projection EventSource is not connected");
      stream.emit(type, payload);
    };
    testingWindow.__activeProjectionStreamCount = () =>
      ProjectionEventSource.instances.filter((candidate) => !candidate.closed).length;
  });

  type RootObservationRequest = { targetRef: string; after: string | null };
  const requests: RootObservationRequest[] = [];
  let streamA = "target-root-stream-a";
  let recoveredA = false;
  await page.route("**/api/v1/bundle/targets/*/root-observations?*", async (route) => {
    const url = new URL(route.request().url());
    const match = url.pathname.match(/\/targets\/([^/]+)\/root-observations$/);
    const targetRef = decodeURIComponent(match?.[1] ?? "");
    const after = url.searchParams.get("after");
    requests.push({ targetRef, after });
    expect(url.searchParams.get("limit")).toBe("128");

    const targetA = targetRef === "target-007-a";
    const suffix = targetA ? "a" : "b";
    const cursor = recoveredA && targetA
      ? "cursor-a-recovered"
      : after
        ? `cursor-${suffix}-2`
        : `cursor-${suffix}-1`;
    const text = recoveredA && targetA
      ? "recovered root output"
      : after
        ? `live ${suffix} output`
        : `initial ${suffix} output · token=[REDACTED]`;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        target_ref: targetRef,
        target_run_ref: `target-run-007-${suffix}`,
        attempt_ref: recoveredA && targetA
          ? "attempt-a-recovered"
          : `attempt-${suffix}`,
        attempt_generation: recoveredA && targetA ? 2 : 1,
        root_session_ref: recoveredA && targetA
          ? "root-a-recovered"
          : `root-${suffix}`,
        native_session_ref: `native-${suffix}`,
        fence_ref: `fence-${suffix}`,
        stream_ref: targetA ? streamA : "target-root-stream-b",
        status: "live",
        items: [{
          event_ref: `event-${cursor}`,
          cursor,
          operation_ref: `operation-${suffix}`,
          operation_generation: recoveredA && targetA ? 2 : 1,
          sequence: after ? 2 : 1,
          kind: after ? "output_gap" : "command_output",
          stream: "stdout",
          text,
          recorded_at: 1_787_520_000,
          redacted: true,
          truncated: Boolean(after),
          dropped_bytes: after ? 65_536 : 0,
          dropped_events: after ? 64 : 0,
        }],
        next_cursor: cursor,
        head_cursor: cursor,
        has_more: false,
        observation_only: true,
      }),
    });
  });

  await page.setViewportSize({ width: 800, height: 900 });
  await freezeBundleProjection(page);
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __activeProjectionStreamCount: () => number }
  ).__activeProjectionStreamCount())).toBe(1);

  const targetList = page.getByTestId("bundle-target-list");
  const targetA = targetList.locator('[data-target-ref="target-007-a"]');
  const targetB = targetList.locator('[data-target-ref="target-007-b"]');
  await targetA.getByRole("button", { name: "查看根 Session" }).click();
  const terminal = targetA.getByRole("region", { name: /gap-structure 根 Session 输出/ });
  await expect(terminal).toContainText("initial a output · token=[REDACTED]");
  await expect(terminal).toContainText("仅观察");
  await expect.poll(() => requests.filter(
    (request) => request.targetRef === "target-007-a",
  )).toEqual([{ targetRef: "target-007-a", after: null }]);

  await page.evaluate(() => {
    const testingWindow = window as typeof window & {
      __emitProjectionEvent: (type: string, payload: object) => void;
    };
    testingWindow.__emitProjectionEvent(
      "agent_runtime.target_root_observations_available",
      {
        target_ref: "target-007-a",
        target_run_ref: "target-run-007-a",
        stream_ref: "target-root-stream-a",
        head_cursor: "cursor-a-2",
      },
    );
    testingWindow.__emitProjectionEvent(
      "agent_runtime.target_root_observations_available",
      {
        target_ref: "target-007-b",
        target_run_ref: "target-run-007-b",
        stream_ref: "target-root-stream-b",
        head_cursor: "cursor-b-2",
      },
    );
  });
  await expect(terminal).toContainText("live a output");
  await expect(terminal).toContainText("OUTPUT GAP · 65536 bytes / 64 events");
  await expect.poll(() => requests.filter(
    (request) => request.targetRef === "target-007-a",
  )).toEqual([
    { targetRef: "target-007-a", after: null },
    { targetRef: "target-007-a", after: "cursor-a-1" },
  ]);
  expect(requests.filter((request) => request.targetRef === "target-007-b")).toEqual([]);
  await expect(targetB.locator(".lumen-target-terminal")).toHaveCount(0);

  await page.evaluate(() => (
    window as typeof window & {
      __emitProjectionEvent: (type: string, payload: object) => void;
    }
  ).__emitProjectionEvent(
    "agent_runtime.target_root_observations_available",
    {
      target_ref: "target-007-a",
      target_run_ref: "target-run-007-a",
      stream_ref: "target-root-stream-a",
      head_cursor: "cursor-a-2",
    },
  ));
  await page.waitForTimeout(100);
  expect(requests.filter((request) => request.targetRef === "target-007-a")).toHaveLength(2);

  recoveredA = true;
  streamA = "target-root-stream-a-recovered";
  await page.evaluate(() => (
    window as typeof window & {
      __emitProjectionEvent: (type: string, payload: object) => void;
    }
  ).__emitProjectionEvent(
    "agent_runtime.target_root_observations_available",
    {
      target_ref: "target-007-a",
      target_run_ref: "target-run-007-a",
      stream_ref: "target-root-stream-a-recovered",
      head_cursor: "cursor-a-recovered",
    },
  ));
  await expect(terminal).toContainText("recovered root output");
  await expect(terminal).not.toContainText("initial a output");
  await expect.poll(() => requests.at(-1)).toEqual({
    targetRef: "target-007-a",
    after: null,
  });
});
