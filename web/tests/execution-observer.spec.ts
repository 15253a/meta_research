import { expect, test, type Page, type Route } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";

type ExperimentFixture = Record<string, unknown>;

const QUESTION = {
  quest_ref: "quest-experiment-001",
  question_ref: "question-experiment-001",
  graph_revision: 91,
  title: "固定样本偏移是否改变整体均值？",
  unknown_statement: "固定样本上的数值偏移会怎样改变整体均值？",
  answer_shape: "报告基线均值、变体均值和差值。",
  applicability_scope: "当前 accepted Quest 的微型真实实验。",
};

const IDEA_STAGE = {
  eligibility: {
    status: "eligible",
    cycle_ref: "cycle-experiment-001",
    question_ref: QUESTION.question_ref,
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

const SOURCE_CHECKPOINT_REFS = [
  "checkpoint-role-source-a",
  "checkpoint-role-source-b",
  "checkpoint-role-source-c",
] as const;

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

function experimentFixture({
  generation = 2,
  status = "running",
  sampledAt = Date.now() / 1_000,
  staleAfter = 5,
}: {
  generation?: number;
  status?: string;
  sampledAt?: number;
  staleAfter?: number;
} = {}): ExperimentFixture {
  const runRef = "run-formal-measurement-001";
  const attemptRef = `attempt-formal-measurement-g${generation}`;
  const sessionRef = `session-formal-measurement-g${generation}`;
  const fenceRef = `fence-formal-measurement-g${generation}`;
  return {
    intent: {
      execution_request_ref: `experiment-request-g${generation}`,
      quest_ref: QUESTION.quest_ref,
      title: "负向结果仍可接纳",
      hypothesis: "固定偏移可能为负，但符号不是 Formal Measurement 门禁。",
      variant_parameter: -0.25,
      sample_count: 16,
    },
    identities: {
      baseline_ref: "baseline-001",
      variant_ref: "variant-001",
      evaluation_protocol_ref: "evaluation-protocol-001",
      protocol_version_ref: "protocol-version-001",
      evaluation_ref: "evaluation-001",
      variant_run_ref: "variant-run-001",
      evaluation_attempt_ref: "evaluation-attempt-001",
    },
    execution: {
      status,
      run_ref: runRef,
      attempt_ref: attemptRef,
      attempt_generation: generation,
      root_session_ref: sessionRef,
      fence_ref: fenceRef,
      fence_status: status === "running" ? "current" : "completed",
      stdout_observation: {
        mode: "raw_stdout",
        complete: status !== "running",
        truncated: false,
        dropped: 0,
        first_sequence: 2,
        last_sequence: 3,
        observed_at: sampledAt,
      },
      events: [
        {
          event_ref: `event-status-g${generation}`,
          sequence: 1,
          attempt_ref: attemptRef,
          fence_ref: fenceRef,
          kind: "status",
          payload: { status: "running" },
          observed_at: sampledAt - 0.1,
        },
        {
          event_ref: `event-stdout-g${generation}`,
          sequence: 2,
          attempt_ref: attemptRef,
          fence_ref: fenceRef,
          kind: "stdout",
          payload: { line: "state formation complete", stream: "stdout" },
          observed_at: sampledAt,
        },
        {
          event_ref: `event-telemetry-g${generation}`,
          sequence: 3,
          attempt_ref: attemptRef,
          fence_ref: fenceRef,
          kind: "telemetry",
          payload: {
            collector: "test-host-telemetry-v1",
            device: "host:test",
            scope: "host-wide; correlated, not exclusive",
            correlation: "same current execution fence",
            cadence: 1,
            staleAfter,
            sampleTime: sampledAt,
            measurements: {
              cpuLoad: {
                value: 0.25,
                unit: "ratio",
                denominator: "host logical CPUs",
              },
            },
          },
          observed_at: sampledAt,
        },
      ],
    },
    execution_request: {},
    frozen_inputs: {},
    assets: { status: "not_attempted" },
    formal_measurement: { status: "not_attempted", metric_result: null },
  };
}

function experimentWithStdoutLines(lineCount: number): ExperimentFixture {
  const sampledAt = Date.now() / 1_000;
  const fixture = experimentFixture({ sampledAt });
  const execution = fixture.execution as Record<string, unknown>;
  const events = execution.events as Array<Record<string, unknown>>;
  const attemptRef = String(execution.attempt_ref);
  const fenceRef = String(execution.fence_ref);
  const stdoutEvents = Array.from({ length: lineCount }, (_, index) => ({
    event_ref: `event-stdout-${index + 1}`,
    sequence: index + 2,
    attempt_ref: attemptRef,
    fence_ref: fenceRef,
    kind: "stdout",
    payload: {
      line: `stdout line ${String(index + 1).padStart(3, "0")}`,
      stream: "stdout",
    },
    observed_at: sampledAt,
  }));
  const telemetry = events.find((event) => event.kind === "telemetry");
  if (!telemetry) throw new Error("fixture telemetry event is missing");

  return {
    ...fixture,
    execution: {
      ...execution,
      stdout_observation: {
        ...(execution.stdout_observation as Record<string, unknown>),
        first_sequence: 2,
        last_sequence: lineCount + 1,
        observed_at: sampledAt,
      },
      events: [
        events[0],
        ...stdoutEvents,
        {
          ...telemetry,
          sequence: lineCount + 2,
          observed_at: sampledAt,
          payload: {
            ...(telemetry.payload as Record<string, unknown>),
            sampleTime: sampledAt,
          },
        },
      ],
    },
  };
}

function completedSourceExperimentFixture(): ExperimentFixture {
  const fixture = experimentFixture({ status: "executed" });
  return {
    ...fixture,
    intent: {
      ...(fixture.intent as Record<string, unknown>),
      request_kind: "retrain",
      source_variant_run_ref: null,
      selected_checkpoint_role_refs: [],
    },
    identities: {
      ...(fixture.identities as Record<string, unknown>),
      variant_run_ref: "variant-run-source-001",
    },
    assets: {
      status: "accepted",
      checkpoint_artifacts: SOURCE_CHECKPOINT_REFS.map((roleRef, index) => ({
        role_ref: roleRef,
        role: "checkpoint_artifact",
        subject_ref: "variant-run-source-001",
        display_name: `checkpoint ${index + 1}`,
      })),
    },
    formal_measurement: { status: "accepted", metric_result: {} },
  };
}

async function fillExperimentIntent(
  launcher: ReturnType<Page["getByTestId"]>,
  title: string,
) {
  await launcher.getByText("填写实验意图", { exact: true }).click();
  await launcher.getByLabel("标题").fill(title);
  await launcher.getByLabel("假设").fill(`${title} 的 Formal Measurement intent。`);
  await launcher.getByLabel("Variant parameter").fill("-0.75");
  await launcher.getByLabel("Sample count").fill("32");
}

async function freezeExperimentProjection(
  page: Page,
  initial: ExperimentFixture | null,
  installEvents?: (route: Route) => Promise<void>,
  snapshotPatch: Record<string, unknown> = {},
) {
  const running = runningProduct();
  await running.authenticate(page);
  const response = await page.request.get(`${running.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as Record<string, unknown>;
  let current = initial;
  let revision = Number(base.revision) + 30;

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
        idea_stage: IDEA_STAGE,
        experiment: {
          status: current ? "active" : "idle",
          current,
        },
        ...snapshotPatch,
      }),
    });
  });
  if (installEvents) await page.route("**/api/v1/events*", installEvents);
  await page.goto(running.baseUrl, { waitUntil: "domcontentloaded" });
  await expect(page.getByTestId("product-shell")).toHaveAttribute(
    "data-shell-state",
    "ready-active",
  );
  return {
    update(next: ExperimentFixture | null) {
      current = next;
      revision += 1;
    },
  };
}

test("a quest-wide HumanRequest wins first presentation over a new current Fence", async ({
  page,
}) => {
  const requestRef = "agent_runtime:HR-observer-priority:r1";
  await freezeExperimentProjection(
    page,
    experimentFixture(),
    undefined,
    {
      human_collaboration: {
        companion: {
          status: "ready",
          scope_ref: `quest:${QUESTION.quest_ref}`,
          messages: [],
          soft_constraints: [],
          agent_proposals: [],
        },
        human_requests: {
          status: "ready",
          waiting: {
            scope: "quest",
            safe_meaningful_runnable_exists: false,
            other_blockers: [],
          },
          items: [{
            request_ref: requestRef,
            request_id: "HR-observer-priority",
            revision: 1,
            issuer: "agent_runtime",
            quest_ref: QUESTION.quest_ref,
            kind: "offline_action",
            status: "open",
            obligation: "先完成当前 Quest 的线下校准。",
            business_purpose: "恢复同一 current Fence 的受控执行。",
            target_assertion: {
              question_ref: QUESTION.question_ref,
              cycle_ref: "cycle-experiment-001",
            },
            acceptance_conditions: ["校准 receipt 已由对应 Owner 验证。"],
            required_authorization: null,
            direct_waiters: [{
              waiter_ref: "run-formal-measurement-001",
              generation: 2,
              wait_scope: "quest",
              status: "blocked",
              other_blockers: [],
            }],
            responses: [],
            evaluation: null,
            disposition: null,
          }],
        },
        commands: {
          status: "ready",
          authorizations: [],
          items: [],
        },
      },
    },
  );

  const humanRequest = page.getByRole("dialog", { name: "HumanRequest" });
  await expect(humanRequest).toBeVisible();
  await expect(humanRequest).toContainText("先完成当前 Quest 的线下校准。");
  await expect(page.getByTestId("execution-observer")).toBeHidden();
  await expect(page.locator(".execution-observer-backdrop")).toHaveAttribute(
    "data-open",
    "false",
  );

  await humanRequest.getByRole("button", { name: "关闭 HumanRequest" }).click();
  const notice = page.getByTestId("execution-start-notice");
  await expect(notice).toBeVisible();
  await notice.getByRole("button", { name: "打开 stdout" }).click();
  await expect(page.getByTestId("execution-observer")).toBeVisible();
});

test("accepted current Quest starts one high-level experiment intent through the Web", async ({
  page,
}) => {
  await freezeExperimentProjection(page, null);
  const launcher = page.getByTestId("experiment-launcher");
  await expect(launcher).toBeVisible();
  await expect(launcher).toContainText("浏览器只提交 title、hypothesis、variant 与 sample intent");
  await launcher.getByText("填写实验意图", { exact: true }).click();
  await launcher.getByLabel("标题").fill("真实 Web 微实验");
  await launcher.getByLabel("假设").fill("负向差值仍然可以形成完整 Formal Measurement。");
  await launcher.getByLabel("Variant parameter").fill("-0.5");
  await launcher.getByLabel("Sample count").fill("24");

  let posted: Record<string, unknown> = {};
  let idempotencyKey = "";
  let csrf = "";
  await page.route("**/api/v1/experiments", async (route) => {
    const request = route.request();
    posted = request.postDataJSON() as Record<string, unknown>;
    idempotencyKey = request.headers()["idempotency-key"] ?? "";
    csrf = request.headers()["x-csrf-token"] ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(experimentFixture()),
    });
  });

  await launcher.getByRole("button", { name: "启动实验" }).click();
  await expect.poll(() => Object.keys(posted).length).toBeGreaterThan(0);
  expect(posted).toMatchObject({
    quest_ref: QUESTION.quest_ref,
    title: "真实 Web 微实验",
    hypothesis: "负向差值仍然可以形成完整 Formal Measurement。",
    variant_parameter: -0.5,
    sample_count: 24,
    request_kind: "retrain",
  });
  expect(String(posted.execution_request_ref)).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect(idempotencyKey).not.toBe("");
  expect(csrf).not.toBe("");
});

test("an existing experiment keeps a second retrain intent reachable", async ({
  page,
}) => {
  await freezeExperimentProjection(page, completedSourceExperimentFixture());
  const launcher = page.getByTestId("experiment-launcher");
  await expect(page.getByTestId("current-experiment-card")).toBeVisible();
  await expect(launcher).toBeVisible();
  await expect(launcher).toContainText("下一次实验 intent");
  await fillExperimentIntent(launcher, "第二次 retrain");
  await launcher.getByLabel("实验类型").selectOption("retrain");

  let posted: Record<string, unknown> = {};
  await page.route("**/api/v1/experiments", async (route) => {
    posted = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(experimentFixture({ generation: 3 })),
    });
  });

  await launcher.getByRole("button", { name: "启动实验" }).click();
  await expect.poll(() => Object.keys(posted).length).toBeGreaterThan(0);
  const executionRequestRef = String(posted.execution_request_ref);
  expect(executionRequestRef).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
  );
  expect({ ...posted, execution_request_ref: "<uuid>" }).toEqual({
    execution_request_ref: "<uuid>",
    quest_ref: QUESTION.quest_ref,
    title: "第二次 retrain",
    hypothesis: "第二次 retrain 的 Formal Measurement intent。",
    variant_parameter: -0.75,
    sample_count: 32,
    request_kind: "retrain",
  });
  await expect(page.getByRole("button", { name: "打开 stdout 与硬件利用 ↗" })).toBeVisible();
  await expect(page.getByRole("button", { name: "当前实验 · stdout" })).toBeVisible();
  await expect(page.getByTestId("experiment-layer-status")).toContainText(
    "Formal Measurement accepted",
  );
});

for (const remeasureCase of [
  { name: "zero", add: [] as string[], expected: [] as string[] },
  {
    name: "one",
    add: [SOURCE_CHECKPOINT_REFS[1]],
    expected: [SOURCE_CHECKPOINT_REFS[1]],
  },
  {
    name: "many ordered",
    add: [
      SOURCE_CHECKPOINT_REFS[2],
      SOURCE_CHECKPOINT_REFS[0],
      SOURCE_CHECKPOINT_REFS[1],
    ],
    expected: [
      SOURCE_CHECKPOINT_REFS[2],
      SOURCE_CHECKPOINT_REFS[0],
      SOURCE_CHECKPOINT_REFS[1],
    ],
  },
]) {
  test(`remeasure submits ${remeasureCase.name} source checkpoints in selected order`, async ({
    page,
  }) => {
    await freezeExperimentProjection(page, completedSourceExperimentFixture());
    const launcher = page.getByTestId("experiment-launcher");
    await fillExperimentIntent(launcher, `remeasure ${remeasureCase.name}`);
    await launcher.getByLabel("实验类型").selectOption("remeasure");
    await expect(launcher.getByTestId("remeasure-source-variant")).toContainText(
      "variant-run-source-001",
    );
    await expect(launcher.getByLabel("Variant parameter")).toHaveValue("-0.25");
    await expect(launcher.getByLabel("Sample count")).toHaveValue("16");
    for (const checkpointRef of remeasureCase.add) {
      await launcher.getByRole("button", {
        name: `加入 checkpoint ${checkpointRef}`,
        exact: true,
      }).click();
    }

    let posted: Record<string, unknown> = {};
    await page.route("**/api/v1/experiments", async (route) => {
      posted = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(completedSourceExperimentFixture()),
      });
    });
    await launcher.getByRole("button", { name: "启动实验" }).click();
    await expect.poll(() => Object.keys(posted).length).toBeGreaterThan(0);
    expect({ ...posted, execution_request_ref: "<uuid>" }).toEqual({
      execution_request_ref: "<uuid>",
      quest_ref: QUESTION.quest_ref,
      title: `remeasure ${remeasureCase.name}`,
      hypothesis: `remeasure ${remeasureCase.name} 的 Formal Measurement intent。`,
      variant_parameter: -0.25,
      sample_count: 16,
      request_kind: "remeasure",
      source_variant_run_ref: "variant-run-source-001",
      selected_checkpoint_role_refs: remeasureCase.expected,
    });
  });
}

test("current Fence auto-presents once, remains reopenable, and exposes honest observations", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const frozen = await freezeExperimentProjection(
    page,
    experimentFixture({ staleAfter: 0.8 }),
  );

  const observer = page.getByTestId("execution-observer");
  await expect(observer).toBeVisible();
  await expect(observer).toHaveAttribute("data-freshness", "live");
  const autoClose = observer.getByRole("button", { name: "关闭当前实验观测窗" });
  await expect(autoClose).toBeFocused();
  await expect(observer).toHaveAttribute(
    "aria-describedby",
    "execution-observer-boundary execution-observer-identity",
  );
  await expect(page.getByTestId("experiment-layer-status")).toHaveText(
    /execution\s*running\s*asset\s*not_attempted\s*Formal Measurement\s*not_attempted/,
  );
  await expect(observer.getByTestId("experiment-observer-layer-status")).toContainText(
    "execution running / asset not_attempted / Formal Measurement not_attempted",
  );
  await expect(observer.getByRole("log")).toContainText("state formation complete");
  await expect(observer.getByTestId("stdout-observation-mode")).toContainText(
    "raw_stdout · incomplete · not truncated · dropped 0",
  );
  await expect(observer.getByText("test-host-telemetry-v1", { exact: true })).toBeVisible();
  await expect(observer.getByText("host-wide; correlated, not exclusive", { exact: true })).toBeVisible();
  await expect(observer.getByText("unit: ratio")).toBeVisible();
  await expect(observer.getByText("denominator: host logical CPUs")).toBeVisible();
  await expect(observer.getByTestId("gpu-telemetry-unavailable")).toContainText(
    "不显示推测的 GPU / VRAM / power",
  );
  await expect(observer).toHaveAttribute("data-freshness", "stale", { timeout: 3_000 });

  await autoClose.click();
  await expect(observer).toBeHidden();
  await expect(observer.getByRole("log")).toHaveCount(0);
  await expect(
    page.locator("[data-execution-observer-entry]:visible").first(),
  ).toBeFocused();

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(observer).toBeHidden();
  const overviewEntry = page.getByRole("button", { name: "打开 stdout 与硬件利用 ↗" });
  const questionEntry = page.getByRole("button", { name: "当前实验 · stdout" });
  await expect(overviewEntry).toBeVisible();
  await expect(questionEntry).toBeVisible();

  await overviewEntry.click();
  await expect(observer).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(observer).toBeHidden();
  await expect(overviewEntry).toBeFocused();

  await questionEntry.click();
  await expect(observer).toBeVisible();
  await page.locator(".execution-observer-backdrop").click({ position: { x: 1, y: 1 } });
  await expect(observer).toBeHidden();
  await expect(questionEntry).toBeFocused();

  frozen.update(experimentFixture({ generation: 3 }));
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(observer).toBeVisible();
  await expect(observer).toContainText(
    "run-formal-measurement-001:g3:session-formal-measurement-g3",
  );
});

test("an open replaced Fence becomes historical without switching its reading context", async ({
  page,
}) => {
  let releaseReplacement!: () => void;
  const replacementGate = new Promise<void>((resolve) => {
    releaseReplacement = resolve;
  });
  let eventRequest = 0;
  const generationTwo = experimentFixture({ generation: 2, staleAfter: 60 });
  const generationTwoEvents = (
    generationTwo.execution as Record<string, unknown>
  ).events as Array<Record<string, unknown>>;
  const generationTwoStdout = generationTwoEvents.find(
    (event) => event.kind === "stdout",
  );
  if (!generationTwoStdout) throw new Error("generation two stdout fixture missing");
  generationTwoStdout.payload = {
    ...(generationTwoStdout.payload as Record<string, unknown>),
    line: "generation two retained output",
  };
  const generationThree = experimentFixture({ generation: 3, staleAfter: 60 });
  const generationThreeEvents = (
    generationThree.execution as Record<string, unknown>
  ).events as Array<Record<string, unknown>>;
  const generationThreeStdout = generationThreeEvents.find(
    (event) => event.kind === "stdout",
  );
  if (!generationThreeStdout) throw new Error("generation three stdout fixture missing");
  generationThreeStdout.payload = {
    ...(generationThreeStdout.payload as Record<string, unknown>),
    line: "generation three new output",
  };

  const frozen = await freezeExperimentProjection(
    page,
    generationTwo,
    async (route) => {
      eventRequest += 1;
      if (eventRequest === 1) {
        await replacementGate;
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          headers: { "Cache-Control": "no-cache" },
          body: "id: 999999\nevent: agent_runtime.experiment_replaced\ndata: {\"run_ref\":\"run-formal-measurement-001\"}\n\n",
        });
        return;
      }
      await route.abort("connectionrefused");
    },
  );

  const observer = page.getByTestId("execution-observer");
  const close = observer.getByRole("button", { name: "关闭当前实验观测窗" });
  await expect(observer).toBeVisible();
  await expect(close).toBeFocused();
  await expect(observer.getByRole("log")).toContainText(
    "generation two retained output",
  );

  frozen.update(generationThree);
  releaseReplacement();
  await expect(observer).toHaveAttribute("data-freshness", "historical");
  await expect(observer).toContainText(
    "run-formal-measurement-001:g2:session-formal-measurement-g2",
  );
  await expect(observer.getByRole("log")).not.toContainText(
    "generation three new output",
  );
  await expect(close).toBeFocused();
  await expect(page.getByTestId("execution-start-notice")).toBeHidden();

  await close.click();
  const notice = page.getByTestId("execution-start-notice");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText(
    "run-formal-measurement-001:g3:session-formal-measurement-g3",
  );
  await notice.getByRole("button", { name: "打开 stdout" }).click();
  await expect(observer).toBeVisible();
  await expect(observer).toContainText(
    "run-formal-measurement-001:g3:session-formal-measurement-g3",
  );
  await expect(observer.getByRole("log")).toContainText(
    "generation three new output",
  );
});

test("an admitted current Fence auto-presents as connecting before stdout exists", async ({
  page,
}) => {
  const admitted = experimentFixture({ status: "admitted" });
  const execution = admitted.execution as Record<string, unknown>;
  execution.fence_status = "current";
  execution.events = [];
  execution.stdout_observation = {
    mode: "raw_stdout",
    complete: false,
    truncated: false,
    dropped: 0,
    first_sequence: null,
    last_sequence: null,
    observed_at: null,
  };
  await freezeExperimentProjection(page, admitted);

  const observer = page.getByTestId("execution-observer");
  await expect(observer).toBeVisible();
  await expect(observer).toHaveAttribute("data-freshness", "connecting");
  await expect(observer.getByRole("log")).toContainText(
    "[connecting] current Attempt 尚未投影 stdout。",
  );
});

test("a locally started Fence still presents when the first reload already executed it", async ({
  page,
}) => {
  const frozen = await freezeExperimentProjection(page, null);
  const launcher = page.getByTestId("experiment-launcher");
  await fillExperimentIntent(launcher, "快速完成实验");

  const admitted = experimentFixture({ generation: 3, status: "admitted" });
  const admittedExecution = admitted.execution as Record<string, unknown>;
  admittedExecution.fence_status = "current";
  admittedExecution.events = [];
  admittedExecution.stdout_observation = {
    mode: "raw_stdout",
    complete: false,
    truncated: false,
    dropped: 0,
    first_sequence: null,
    last_sequence: null,
    observed_at: null,
  };
  const executed = experimentFixture({ generation: 3, status: "executed" });
  (executed.execution as Record<string, unknown>).fence_status = "current";
  await page.route("**/api/v1/experiments", async (route) => {
    frozen.update(executed);
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(admitted),
    });
  });

  await launcher.getByRole("button", { name: "启动实验" }).click();
  const observer = page.getByTestId("execution-observer");
  await expect(observer).toBeVisible();
  await expect(observer).toContainText(
    "run-formal-measurement-001:g3:session-formal-measurement-g3",
  );
  await expect(observer).toHaveAttribute("data-freshness", "historical");

  await observer.getByRole("button", { name: "关闭当前实验观测窗" }).click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(observer).toBeHidden();

  const historical = experimentFixture({ generation: 4, status: "executed" });
  (historical.execution as Record<string, unknown>).fence_status = "current";
  frozen.update(historical);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(observer).toBeHidden();
});

test("an admitted or running current Fence keeps next-experiment controls unavailable until settled", async ({
  page,
}) => {
  await page.addInitScript(() => {
    sessionStorage.setItem(
      "meta-research:execution-observer:auto-presented-fence",
      "run-formal-measurement-001:g2:session-formal-measurement-g2",
    );
  });
  const admitted = experimentFixture({ status: "admitted" });
  const admittedExecution = admitted.execution as Record<string, unknown>;
  admittedExecution.fence_status = "current";
  admittedExecution.events = [];
  const frozen = await freezeExperimentProjection(page, admitted);
  await expect(page.getByTestId("execution-observer")).toBeHidden();

  const launcher = page.getByTestId("experiment-launcher");
  await expect(launcher).toHaveAttribute(
    "data-experiment-admission",
    "blocked-active-execution",
  );
  await expect(launcher.getByText("填写实验意图", { exact: true })).toHaveCount(0);
  await expect(launcher.getByRole("button", { name: "启动实验" })).toHaveCount(0);

  frozen.update(experimentFixture({ status: "running" }));
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(launcher).toHaveAttribute(
    "data-experiment-admission",
    "blocked-active-execution",
  );
  await expect(launcher.getByRole("button", { name: "启动实验" })).toHaveCount(0);

  frozen.update(completedSourceExperimentFixture());
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(launcher).toHaveAttribute("data-experiment-admission", "available");
  await fillExperimentIntent(launcher, "settled 后的下一次实验");
  await expect(launcher.getByRole("button", { name: "启动实验" })).toBeEnabled();
});

test("new current Fence preserves active input focus and emits a nonmodal notice", async ({
  page,
}) => {
  let releaseEvent!: () => void;
  const eventGate = new Promise<void>((resolve) => {
    releaseEvent = resolve;
  });
  let eventRequest = 0;
  const frozen = await freezeExperimentProjection(page, null, async (route) => {
    eventRequest += 1;
    if (eventRequest === 1) {
      await eventGate;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: "id: 999999\nevent: agent_runtime.experiment_started\ndata: {\"run_ref\":\"run-formal-measurement-001\"}\n\n",
      });
      return;
    }
    await route.abort("connectionrefused");
  });

  await page.evaluate(() => {
    const input = document.createElement("input");
    input.id = "persistent-focused-input";
    input.setAttribute("aria-label", "持续编辑中的输入");
    document.body.append(input);
    input.focus();
  });
  const focusedInput = page.getByLabel("持续编辑中的输入");
  await expect(focusedInput).toBeFocused();

  frozen.update(experimentFixture({ generation: 3 }));
  releaseEvent();

  const notice = page.getByTestId("execution-start-notice");
  await expect(notice).toContainText("新 Execution Attempt 已开始");
  await expect(focusedInput).toBeFocused();
  await expect(page.getByTestId("execution-observer")).toBeHidden();
  const noticeTrigger = notice.getByRole("button", { name: "打开 stdout" });
  await noticeTrigger.click();
  const observer = page.getByTestId("execution-observer");
  await expect(notice).toBeHidden();
  await expect(observer).toBeVisible();
  await observer.getByRole("button", { name: "关闭当前实验观测窗" }).click();
  await expect(observer).toBeHidden();
  await expect(
    page.locator("[data-execution-observer-entry]:visible").first(),
  ).toBeFocused();
});

test("Execution Observer keeps the fixed 1440, 800, and 390 responsive hierarchy", async ({
  page,
}) => {
  await freezeExperimentProjection(page, experimentFixture());
  const observer = page.getByTestId("execution-observer");
  await expect(observer).toBeVisible();

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const geometry = await page.evaluate(() => {
      const box = (selector: string) => {
        const rect = document.querySelector(selector)?.getBoundingClientRect();
        if (!rect) throw new Error(`missing ${selector}`);
        return {
          x: rect.x,
          y: rect.y,
          width: rect.width,
          height: rect.height,
        };
      };
      return {
        observer: box(".execution-observer"),
        stdout: box(".execution-stdout-pane"),
        telemetry: box(".execution-telemetry-pane"),
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
        viewportHeight: window.innerHeight,
      };
    });
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    if (viewport.width > 720) {
      expect(geometry.stdout.x + geometry.stdout.width).toBeLessThanOrEqual(
        geometry.telemetry.x + 1,
      );
    } else {
      expect(geometry.telemetry.y + geometry.telemetry.height).toBeLessThanOrEqual(
        geometry.stdout.y + 1,
      );
      expect(geometry.observer.height).toBeCloseTo(geometry.viewportHeight - 8, 0);
    }
  }

  const visibleButtonSizes = await observer.locator("button:visible").evaluateAll(
    (buttons) => buttons.map((button) => {
      const rect = button.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }),
  );
  expect(visibleButtonSizes.length).toBeGreaterThan(0);
  for (const size of visibleButtonSizes) {
    expect(size.width).toBeGreaterThanOrEqual(44);
    expect(size.height).toBeGreaterThanOrEqual(44);
  }

  const terminal = observer.getByRole("log");
  await terminal.focus();
  await expect(terminal).toBeFocused();
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press("Tab");
    expect(await page.evaluate(() => Boolean(
      document.activeElement?.closest("[data-testid='execution-observer']"),
    ))).toBe(true);
  }
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press("Shift+Tab");
    expect(await page.evaluate(() => Boolean(
      document.activeElement?.closest("[data-testid='execution-observer']"),
    ))).toBe(true);
  }
});

test("disabling follow latest preserves stdout scrollTop when a new line arrives", async ({
  page,
}) => {
  let releaseEvent!: () => void;
  const eventGate = new Promise<void>((resolve) => {
    releaseEvent = resolve;
  });
  let eventRequest = 0;
  const frozen = await freezeExperimentProjection(
    page,
    experimentWithStdoutLines(80),
    async (route) => {
      eventRequest += 1;
      if (eventRequest === 1) {
        await eventGate;
        await route.fulfill({
          status: 200,
          contentType: "text/event-stream",
          headers: { "Cache-Control": "no-cache" },
          body: "id: 999999\nevent: agent_runtime.experiment_observed\ndata: {\"run_ref\":\"run-formal-measurement-001\"}\n\n",
        });
        return;
      }
      await route.abort("connectionrefused");
    },
  );

  const observer = page.getByTestId("execution-observer");
  const terminal = observer.getByRole("log");
  await expect(observer).toBeVisible();
  await expect(terminal).toContainText("stdout line 080");
  await expect.poll(() => terminal.evaluate((element) => (
    element.scrollHeight > element.clientHeight
  ))).toBe(true);

  const follow = observer.getByRole("button", { name: "跟随最新" });
  await expect(follow).toHaveAttribute("aria-pressed", "true");
  await follow.click();
  await expect(follow).toHaveAttribute("aria-pressed", "false");
  await terminal.evaluate((element) => {
    element.scrollTop = 0;
  });
  expect(await terminal.evaluate((element) => element.scrollTop)).toBe(0);

  frozen.update(experimentWithStdoutLines(81));
  releaseEvent();
  await expect(terminal).toContainText("stdout line 081");
  expect(await terminal.evaluate((element) => element.scrollTop)).toBe(0);
});
