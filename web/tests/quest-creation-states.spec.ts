import { expect, test, type Locator, type Page, type Route } from "@playwright/test";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";
import { attachFixedVisualPair } from "./support/fixed-reference.js";


const QUESTION = {
  title: "低照度显微图像中的稀有形态保真",
  unknown: "尚不明确哪种自监督去噪条件能保留稀有形态。",
  answer: "形成带反例和证据边界的比较结论。",
};

const ACCEPTED_PROPOSAL_CONTRACT = {
  sourceBackground: "rgb(242, 241, 255)",
  sourceBorder: "rgb(222, 221, 244)",
  requiredBorder: "rgb(98, 95, 231)",
  requiredFields: 4,
  fields: 6,
} as const;

const SNAPSHOT_MASK_ALLOWLIST = [
  {
    selector: ".lumen-connection code",
    reason: "durable feed revision",
  },
  {
    selector: ".quest-intent-header > div > span",
    reason: "durable Intent Session ref",
  },
  {
    selector: ".quest-intent-status",
    reason: "session status and bound draft revision",
  },
] as const;

type PublicReceipt = {
  status: string;
  issuer?: string;
  kind?: string;
  receipt_ref?: string;
  subject_ref?: string;
  payload_hash?: string;
  reason?: { code: string; upstream_step?: string };
};

type PublicQuestCreation = {
  initialization_id: string;
  status: string;
  quest_draft: {
    revision: number;
    hash: string;
    schema_ref: string;
    value: {
      goal: string;
      completion_criteria: string;
      time_budget: string;
      [key: string]: unknown;
    };
  };
  resource_envelope: null | {
    ref: string;
    hash: string;
    status: string;
    time_budget: string;
    hard_ceiling: { kind: string; seconds: number | null };
  };
  proposal: null | {
    ref: string;
    hash: string;
    status: string;
    literature_snapshot_ref: string | null;
    content: {
      title: string;
      unknown_statement: string;
      answer_shape: string;
      applicability_scope: string;
      background_context: string;
      requirements_constraints: string;
    };
  };
  proposal_generation: null | {
    status: string;
    failure: null | { code: string };
  };
  deepfetch: null | {
    request_ref: string;
    status: string;
    activity: string;
    freshness: string;
    progress: { completed: number; total: number };
    run: null | {
      run_ref: string;
      attempt_generation: number;
      root_session_ref: string;
      native_session_ref: string | null;
      execution_receipt: null | PublicReceipt;
    };
    literature_snapshot: null | {
      status: string;
      snapshot_ref: string;
      completion: string;
      paper_count: number;
      fulltext_count: number;
      limitations: string[];
      receipt: PublicReceipt;
    };
    failure: null | { code: string };
  };
  confirmation_preview: null | {
    ref: string;
    hash: string;
    status: string;
    target_assertions: Array<{
      owner: string;
      operation: string;
      target_hash: string;
      may_change: string[];
      will_not_change: string[];
      preconditions: string[];
      risks: string[];
      stale_if: string[];
      bindings: Record<string, unknown>;
    }>;
  };
  intent_session: null | {
    turns: Array<{
      user_content: string;
      assistant_status: string;
      assistant_content: string | null;
      reason: null | { code: string };
    }>;
  };
  receipts: Record<string, PublicReceipt>;
  quest_ref?: string;
  memory_ref?: string;
  question_ref?: string;
  cycle_ref?: string;
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

test("an upgraded recovering v1 Quest remains readable through the corrected Web", async ({
  page,
}) => {
  await product?.stop();
  product = await DeterministicProduct.start({ legacyState: "recovering" });
  const pageErrors: Error[] = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  await openAuthenticatedProduct(page, runningProduct());
  const legacy = await publicCurrent(page);
  expect(legacy).toMatchObject({
    status: "recovering",
    quest_draft: {
      schema_ref: "meta-research/quest-initialization-draft/v1",
    },
  });

  const { dialog, opener } = await openCreation(page);
  await expect(dialog.getByLabel("这个 Quest 最终要完成什么？")).toHaveValue(
    "保留升级前已确认的 legacy Quest。",
  );
  await expect(dialog.getByLabel("什么情况算完成？")).toHaveValue(
    "恢复期间仍可从公开 Web 检查既有 bundle。",
  );
  await expect(dialog.getByLabel("文献搜索范围")).toHaveValue("oa_only");
  await expect(
    dialog.getByText("正在从首个缺失 receipt 恢复", { exact: true }).first(),
  ).toBeVisible();
  expect(pageErrors).toEqual([]);

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

async function openCreation(page: Page) {
  const opener = page.getByRole("button", { name: "创建 Quest" });
  await opener.click();
  const dialog = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(dialog).toBeVisible();
  return { dialog, opener };
}

function runningProduct(): DeterministicProduct {
  if (!product) throw new Error("deterministic product is not running");
  return product;
}

function dynamicSnapshotMasks(page: Page, dialog: Locator): Locator[] {
  return SNAPSHOT_MASK_ALLOWLIST.map(({ selector }) => (
    selector.startsWith(".lumen-") ? page.locator(selector) : dialog.locator(selector)
  ));
}

async function expectAcceptedProposalContract(dialog: Locator) {
  const contract = await dialog.evaluate((root) => {
    const source = root.querySelector(".quest-question-sourcebar");
    const fields = Array.from(root.querySelectorAll(".quest-question-fields .seed-field"));
    const required = fields.filter((field) => field.classList.contains("core"));
    if (!(source instanceof HTMLElement)) throw new Error("proposal sourcebar is missing");
    const sourceStyle = getComputedStyle(source);
    return {
      sourceBackground: sourceStyle.backgroundColor,
      sourceBorder: sourceStyle.borderTopColor,
      fields: fields.length,
      requiredFields: required.length,
      requiredBorderColors: required.map((field) => getComputedStyle(field).borderLeftColor),
      requiredBorderWidths: required.map((field) => getComputedStyle(field).borderLeftWidth),
    };
  });
  expect(contract).toEqual({
    sourceBackground: ACCEPTED_PROPOSAL_CONTRACT.sourceBackground,
    sourceBorder: ACCEPTED_PROPOSAL_CONTRACT.sourceBorder,
    fields: ACCEPTED_PROPOSAL_CONTRACT.fields,
    requiredFields: ACCEPTED_PROPOSAL_CONTRACT.requiredFields,
    requiredBorderColors: Array(ACCEPTED_PROPOSAL_CONTRACT.requiredFields).fill(
      ACCEPTED_PROPOSAL_CONTRACT.requiredBorder,
    ),
    requiredBorderWidths: Array(ACCEPTED_PROPOSAL_CONTRACT.requiredFields).fill("3px"),
  });
  await expect(
    dialog.getByText("系统已填入六字段；确认前都可修改", { exact: true }),
  ).toBeVisible();
}

async function fillRequiredBasis(
  dialog: Locator,
  goal: string,
  completionCriteria: string,
) {
  const goalField = dialog.getByLabel("这个 Quest 最终要完成什么？");
  await goalField.fill(goal);
  await dialog.getByLabel("什么情况算完成？").fill(completionCriteria);
  await goalField.blur();
  await expect(dialog.getByText("草案已自动保存", { exact: true })).toBeVisible();
}

async function selectReadyCompute(dialog: Locator) {
  const probe = dialog.getByRole("button", {
    name: /^(检测本机计算卡|重新检测)$/,
  });
  await probe.click();
  const device = dialog.getByRole("button", {
    name: /Deterministic GPU.*GPU-deterministic-1/,
  });
  await expect(device).toBeVisible();
  await device.click();
  await expect(
    dialog.getByText("已绑定 1 张实际检测设备。", { exact: false }),
  ).toBeVisible();
}

async function generateReadyProposal(dialog: Locator) {
  const generate = dialog.getByRole("button", {
    name: /生成第一个问题|重新生成第一个问题/,
  });
  await generate.click();
  await waitForReadyProposal(dialog);
}

async function waitForReadyProposal(dialog: Locator) {
  await expect(
    dialog.getByText(
      "正在依据精确 DraftRevision 生成六字段；Quest 配置和右侧 Session 仍可使用。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(dialog.locator(".quest-proposal-state.generating code")).toHaveText(
    /proposal_generating|queued|running/,
  );
  await expect(dialog.getByLabel("首问题标题")).toHaveValue(QUESTION.title, {
    timeout: 12_000,
  });
  await expect(dialog.getByLabel("首问题要解决的未知")).toHaveValue(QUESTION.unknown);
  await expect(dialog.getByLabel("首问题的答案形态")).toHaveValue(QUESTION.answer);
  await expect(
    dialog.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();
}

test("the ready Proposal keeps the accepted violet source and six seed-field cards", async ({
  page,
}) => {
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  await fillRequiredBasis(
    dialog,
    "验证固定 Proposal 视觉合同",
    "violet sourcebar 与六字段层级保持可辨认",
  );
  await dialog.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await selectReadyCompute(dialog);
  await generateReadyProposal(dialog);

  await expectAcceptedProposalContract(dialog);
});

test("DeepFetch unfolds real progress and its accepted snapshot inside the same Quest window", async ({
  page,
}) => {
  test.setTimeout(45_000);
  await page.setViewportSize({ width: 800, height: 1000 });
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  const session = dialog.getByRole("complementary", {
    name: "讨论 Quest 与第一问",
  });
  await fillRequiredBasis(
    dialog,
    "用真实 Web Research 核查低照度显微去噪的证据边界",
    "形成带论文账本、全文限制和反例的第一问",
  );
  await dialog.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await selectReadyCompute(dialog);

  const deepfetchChoice = dialog.getByRole("button", { name: "先运行 DeepFetch" });
  await deepfetchChoice.click();
  await expect(deepfetchChoice).toHaveAttribute("aria-pressed", "true");
  await dialog.getByLabel("文献搜索范围").selectOption("oa_only");
  await expect(dialog.getByTestId("acquisition-session-status")).toContainText(
    "ready · current",
  );
  const runway = dialog.getByTestId("deepfetch-runway");
  await expect(runway).toHaveAttribute("data-status", "not-started");
  await expect(runway).toContainText("进度只读取 durable Projection");
  await expect(session).toBeVisible();

  await dialog.getByRole("button", { name: "生成第一个问题" }).click();
  await expect(runway).toHaveAttribute("data-status", /queued|running|succeeded/, {
    timeout: 8_000,
  });
  await expect(runway.getByLabel("DeepFetch 真实进度")).toBeVisible();
  await expect(session).toBeVisible();
  await expect(runway).toContainText("2 papers · 1 fulltexts · RM accepted", {
    timeout: 12_000,
  });
  await expect(runway).toContainText("第二篇论文没有可合法获取的开放全文。");
  await expect(dialog.getByLabel("首问题标题")).toHaveValue(QUESTION.title, {
    timeout: 15_000,
  });
  await expect(dialog.getByLabel("首问题背景上下文")).toHaveValue(
    "DeepFetch 已核查两篇论文；一篇没有可合法获取的开放全文。",
  );
  await expect(runway).toHaveAttribute("data-status", "succeeded");
  await expect(runway).toContainText("首问题已原位展开");
  await expectAcceptedProposalContract(dialog);

  const current = await publicCurrent(page);
  expect(current).toMatchObject({
    status: "proposal_ready",
    deepfetch: {
      status: "succeeded",
      freshness: "current",
      progress: { completed: 5, total: 5 },
      run: {
        attempt_generation: 1,
        native_session_ref: "chrome-deepfetch-native-session",
        execution_receipt: { status: "accepted", issuer: "agent_runtime" },
      },
      literature_snapshot: {
        status: "accepted",
        completion: "limited",
        paper_count: 2,
        fulltext_count: 1,
        receipt: { status: "accepted", issuer: "research_memory" },
      },
    },
  });
  expect(current?.proposal?.literature_snapshot_ref).toBe(
    current?.deepfetch?.literature_snapshot?.snapshot_ref,
  );
  expect(
    Object.values(current?.receipts ?? {}).every(
      (receipt) => receipt.status === "not_attempted",
    ),
  ).toBeTruthy();
  expect(current).not.toHaveProperty("quest_ref");
  expect(current).not.toHaveProperty("question_ref");
  await expect(page.getByRole("dialog")).toHaveCount(1);
  await expect(
    dialog.getByRole("button", { name: "确认创建 Quest 与第一个问题" }),
  ).toBeEnabled();

  for (const viewport of [
    { width: 390, height: 844 },
    { width: 800, height: 1000 },
    { width: 1440, height: 1000 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(runway).toBeVisible();
    await expect(session).toBeVisible();
    const geometry = await page.evaluate(() => ({
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    }));
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth);
  }
});

async function publicCurrent(page: Page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/quest-initializations/current", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`current failed: ${response.status}`);
    return response.json() as Promise<null | PublicQuestCreation>;
  });
}

async function publicSnapshotRevision(page: Page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/v1/snapshot", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`snapshot failed: ${response.status}`);
    return Number((await response.json() as { revision: number }).revision);
  });
}

async function publicStatus(page: Page, initializationId: string) {
  return page.evaluate(async (id) => {
    const response = await fetch(`/api/v1/quest-initializations/${id}`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`view failed: ${response.status}`);
    return (await response.json()) as PublicQuestCreation;
  }, initializationId);
}

async function waitForPublicStatus(
  page: Page,
  initializationId: string,
  wanted: string,
  timeoutMs = 12_000,
) {
  const deadline = Date.now() + timeoutMs;
  let latest = await publicStatus(page, initializationId);
  while (latest.status !== wanted && Date.now() < deadline) {
    await page.waitForTimeout(75);
    latest = await publicStatus(page, initializationId);
  }
  expect(latest.status).toBe(wanted);
  return latest;
}

async function publicPost(page: Page, path: string, body: object = {}) {
  return page.evaluate(async ({ requestPath, requestBody }) => {
    const csrfCookie = document.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("meta_research_csrf="));
    if (!csrfCookie) throw new Error("authenticated CSRF cookie is missing");
    const csrf = decodeURIComponent(csrfCookie.split("=", 2)[1]);
    const response = await fetch(requestPath, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify(requestBody),
    });
    return {
      status: response.status,
      body: await response.json() as Record<string, unknown>,
    };
  }, { requestPath: path, requestBody: body });
}

async function publicPut(page: Page, path: string, body: object) {
  return page.evaluate(async ({ requestPath, requestBody }) => {
    const csrfCookie = document.cookie
      .split(";")
      .map((value) => value.trim())
      .find((value) => value.startsWith("meta_research_csrf="));
    if (!csrfCookie) throw new Error("authenticated CSRF cookie is missing");
    const csrf = decodeURIComponent(csrfCookie.split("=", 2)[1]);
    const response = await fetch(requestPath, {
      method: "PUT",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
        "X-CSRF-Token": csrf,
      },
      body: JSON.stringify(requestBody),
    });
    return {
      status: response.status,
      body: await response.json() as Record<string, unknown>,
    };
  }, { requestPath: path, requestBody: body });
}

async function sendIntent(dialog: Locator, message: string) {
  await dialog.getByLabel("在 Quest Drafting Session 中发消息").fill(message);
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect(dialog.getByText(message, { exact: true })).toBeVisible();
}

async function expectMinimumTouchTarget(locator: Locator) {
  const box = await locator.boundingBox();
  expect(box, "critical control must have a rendered box").not.toBeNull();
  expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
  expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
}

async function openTechnicalDetails(dialog: Locator) {
  const details = dialog.locator("details", {
    hasText: "查看 Preview、Owner 与 receipt 技术详情",
  });
  if ((await details.getAttribute("open")) === null) {
    await details.getByText(
      "查看 Preview、Owner 与 receipt 技术详情",
      { exact: true },
    ).click();
  }
  await expect(details).toHaveAttribute("open", "");
  return details;
}

async function expectTargetAssertions(
  details: Locator,
  view: PublicQuestCreation,
) {
  const assertions = view.confirmation_preview?.target_assertions ?? [];
  expect(
    assertions
      .map((assertion) => `${assertion.owner}:${assertion.operation}`)
      .sort(),
  ).toEqual([
    "advancement_engine:activate_initial_cycle",
    "human_collaboration:issue_broad_research_authorization",
    "research_graph:accept_quest",
    "research_graph:accept_root_question",
    "research_memory:accept_question_content",
  ]);
  for (const assertion of assertions) {
    const record = details.getByRole("article").filter({
      hasText: `${assertion.owner} · ${assertion.operation}`,
    });
    await expect(record).toContainText(`target hash · ${assertion.target_hash}`);
    await expect(record).toContainText(
      `may change · ${assertion.may_change.join(" · ") || "none"}`,
    );
    await expect(record).toContainText(
      `will not change · ${assertion.will_not_change.join(" · ") || "none"}`,
    );
    await expect(record).toContainText(
      `preconditions · ${assertion.preconditions.join(" · ") || "none"}`,
    );
    await expect(record).toContainText(
      `risks · ${assertion.risks.join(" · ") || "none"}`,
    );
    await expect(record).toContainText(
      `stale if · ${assertion.stale_if.join(" · ") || "none"}`,
    );
    await expect(record).toContainText(
      `bindings · ${JSON.stringify(assertion.bindings)}`,
    );
  }
}

async function expectAcceptedReceipts(
  details: Locator,
  view: PublicQuestCreation,
) {
  const accepted = Object.entries(view.receipts).filter(
    ([, receipt]) => receipt.status === "accepted",
  );
  expect(accepted.map(([name]) => name).sort()).toEqual([
    "broad_research_authorization",
    "cycle_activation",
    "human_confirmation",
    "quest_goal",
    "question_content",
    "question_identity",
  ]);
  for (const [name, receipt] of accepted) {
    expect(receipt).toMatchObject({
      issuer: expect.any(String),
      kind: expect.any(String),
      receipt_ref: expect.any(String),
      subject_ref: expect.any(String),
      payload_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    const record = details.getByRole("article").filter({
      hasText: `${name} · accepted`,
    });
    await expect(record).toContainText(`issuer · ${receipt.issuer}`);
    await expect(record).toContainText(`kind · ${receipt.kind}`);
    await expect(record).toContainText(`receipt · ${receipt.receipt_ref}`);
    await expect(record).toContainText(`subject · ${receipt.subject_ref}`);
    await expect(record).toContainText(`payload hash · ${receipt.payload_hash}`);
  }
}

test("compute and Intent stay independent without losing an unsaved draft edit", async ({
  page,
}) => {
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  await fillRequiredBasis(
    dialog,
    "验证并行 Provider 响应不会覆盖人工草案",
    "并行期间的最后一次人工输入保持 durable",
  );

  const compute = dialog.getByRole("button", { name: /检测本机计算卡|正在检测/ });
  await compute.click();
  await expect(compute).toHaveText("正在检测…");

  const intent = dialog.getByLabel("在 Quest Drafting Session 中发消息");
  await intent.fill("并行解释当前计算配置");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect(dialog.getByText("并行解释当前计算配置", { exact: true })).toBeVisible();

  const cancel = dialog.getByRole("button", { name: "取消" });
  await expect(cancel).toBeEnabled();
  await expect(intent).toBeDisabled();

  const goal = dialog.getByLabel("这个 Quest 最终要完成什么？");
  const editedWhileComputeWasPending = "并行响应回来后仍须保留的人工草案";
  await expect(goal).toBeEnabled();
  await goal.fill(editedWhileComputeWasPending);

  await expect(
    dialog.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(goal).toHaveValue(editedWhileComputeWasPending);
  await expect(intent).toBeDisabled();

  await expect(
    dialog.getByRole("article").filter({
      hasText: "Drafting Session · completed",
    }),
  ).toContainText("建议先固定可证伪边界：并行解释当前计算配置", {
    timeout: 12_000,
  });
  await expect(intent).toBeEnabled();
  await expect.poll(async () => (await publicCurrent(page))?.quest_draft.value.goal).toBe(
    editedWhileComputeWasPending,
  );
});

test("a remote same-init draft write cannot steal a local dirty CAS basis", async ({
  page,
}) => {
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  const originalGoal = "本地与远端草案必须显式解决冲突";
  const originalCriteria = "不能以较新的 CAS basis 静默覆盖远端修改";
  await fillRequiredBasis(dialog, originalGoal, originalCriteria);
  const basis = await publicCurrent(page);
  expect(basis).not.toBeNull();

  const localGoal = "仍停留在浏览器里的未保存人工目标";
  const remoteCriteria = "另一标签已经保存的完成标准";
  const goal = dialog.getByLabel("这个 Quest 最终要完成什么？");
  const remote = await publicPut(
    page,
    `/api/v1/quest-initializations/${basis!.initialization_id}/draft`,
    {
      expected_draft_revision: basis!.quest_draft.revision,
      expected_draft_hash: basis!.quest_draft.hash,
      draft: {
        ...basis!.quest_draft.value,
        completion_criteria: remoteCriteria,
      },
    },
  );
  expect(remote.status).toBe(200);
  await goal.fill(localGoal);
  const remoteSnapshotRevision = await publicSnapshotRevision(page);
  await expect.poll(async () => Number(
    (await page.locator(".lumen-connection code").textContent())?.replace("rev ", ""),
  )).toBeGreaterThanOrEqual(remoteSnapshotRevision);
  await goal.blur();

  await expect(dialog.getByRole("alert")).toContainText("quest_draft_stale");
  const reloadLatest = dialog.getByRole("button", {
    name: "载入最新 durable 版本并放弃本地未保存修改",
  });
  await expect(reloadLatest).toBeFocused();
  await expect(goal).toHaveValue(localGoal);
  await expect.poll(async () => {
    const current = await publicCurrent(page);
    return {
      goal: current?.quest_draft.value.goal,
      completionCriteria: current?.quest_draft.value.completion_criteria,
    };
  }).toEqual({
    goal: originalGoal,
    completionCriteria: remoteCriteria,
  });

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeVisible();
  await expect(reloadLatest).toBeVisible();
  await expect(reloadLatest).toBeFocused();

  let rejectNextReload = true;
  const rejectReloadOnce = async (route: Route) => {
    if (rejectNextReload) {
      rejectNextReload = false;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "temporarily_unavailable" } }),
      });
      return;
    }
    await route.continue();
  };
  const exactViewPattern = `**/api/v1/quest-initializations/${basis!.initialization_id}`;
  await page.route(exactViewPattern, rejectReloadOnce);
  await reloadLatest.click();
  await expect(dialog.getByRole("alert")).toContainText(
    "quest_initialization_unavailable:503",
  );
  await expect(reloadLatest).toBeVisible();
  await expect(reloadLatest).toBeFocused();
  await page.unroute(exactViewPattern, rejectReloadOnce);

  await reloadLatest.click();
  await expect(dialog.getByRole("alert")).toBeHidden();
  await expect(goal).toHaveValue(originalGoal);
  await expect(dialog.getByLabel("什么情况算完成？")).toHaveValue(remoteCriteria);

  const recoveredGoal = "显式载入远端版本后可以继续编辑";
  await goal.fill(recoveredGoal);
  await goal.blur();
  await expect.poll(async () => (await publicCurrent(page))?.quest_draft.value.goal).toBe(
    recoveredGoal,
  );
});

test("a delayed conflict reload cannot replace a newer SSE view with stale form data", async ({
  page,
}) => {
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  const originalGoal = "恢复请求必须服从单调 durable view";
  const originalCriteria = "旧 GET 不得把新 revision 的字段静默改回";
  await fillRequiredBasis(dialog, originalGoal, originalCriteria);
  const initial = await publicCurrent(page);
  expect(initial).not.toBeNull();

  const localGoal = "本地尚未放弃的冲突目标";
  const goal = dialog.getByLabel("这个 Quest 最终要完成什么？");
  await goal.fill(localGoal);
  const remoteB = await publicPut(
    page,
    `/api/v1/quest-initializations/${initial!.initialization_id}/draft`,
    {
      expected_draft_revision: initial!.quest_draft.revision,
      expected_draft_hash: initial!.quest_draft.hash,
      draft: {
        ...initial!.quest_draft.value,
        completion_criteria: "远端版本 B",
      },
    },
  );
  expect(remoteB.status).toBe(200);
  await goal.blur();

  const reloadLatest = dialog.getByRole("button", {
    name: "载入最新 durable 版本并放弃本地未保存修改",
  });
  await expect(reloadLatest).toBeFocused();
  const versionB = await publicStatus(page, initial!.initialization_id);

  let releaseReload!: () => void;
  const reloadRelease = new Promise<void>((resolve) => {
    releaseReload = resolve;
  });
  let markReloadStarted!: () => void;
  const reloadStarted = new Promise<void>((resolve) => {
    markReloadStarted = resolve;
  });
  const delayedReload = async (route: Route) => {
    markReloadStarted();
    await reloadRelease;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(versionB),
    });
  };
  const exactViewPattern = `**/api/v1/quest-initializations/${initial!.initialization_id}`;
  await page.route(exactViewPattern, delayedReload);
  await reloadLatest.click();
  await reloadStarted;

  const versionC = await publicPut(
    page,
    `/api/v1/quest-initializations/${initial!.initialization_id}/draft`,
    {
      expected_draft_revision: versionB.quest_draft.revision,
      expected_draft_hash: versionB.quest_draft.hash,
      draft: {
        ...versionB.quest_draft.value,
        completion_criteria: "远端版本 C（reload 在途时推进）",
      },
    },
  );
  expect(versionC.status).toBe(200);
  const revisionC = (versionC.body as unknown as PublicQuestCreation).quest_draft.revision;
  await expect.poll(async () => Number(
    (await page.locator(".lumen-connection code").textContent())?.replace("rev ", ""),
  )).toBeGreaterThanOrEqual(await publicSnapshotRevision(page));
  expect(revisionC).toBeGreaterThan(versionB.quest_draft.revision);

  releaseReload();
  await expect(dialog.getByRole("alert")).toContainText("quest_reload_stale");
  await expect(reloadLatest).toBeVisible();
  await expect(goal).toHaveValue(localGoal);
  await page.unroute(exactViewPattern, delayedReload);

  await reloadLatest.click();
  await expect(dialog.getByRole("alert")).toBeHidden();
  await expect(goal).toHaveValue(originalGoal);
  await expect(dialog.getByLabel("什么情况算完成？")).toHaveValue(
    "远端版本 C（reload 在途时推进）",
  );
});

test("a remote Proposal write requires an explicit durable reload before editing resumes", async ({
  page,
}) => {
  await openAuthenticatedProduct(page, runningProduct());
  const { dialog } = await openCreation(page);
  await fillRequiredBasis(
    dialog,
    "Proposal 并发编辑必须显式解决冲突",
    "远端与本地六字段都不能被静默覆盖",
  );
  await dialog.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await selectReadyCompute(dialog);
  await generateReadyProposal(dialog);

  const basis = await publicCurrent(page);
  expect(basis?.proposal).not.toBeNull();
  const localTitle = `${QUESTION.title}（尚未保存的本地复核）`;
  const remoteTitle = `${QUESTION.title}（另一标签已保存）`;
  const title = dialog.getByLabel("首问题标题");
  const delayLocalSave = async (route: Route) => {
    if (route.request().postData()?.includes(localTitle)) {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
    }
    await route.continue();
  };
  await page.route("**/api/v1/quest-initializations/*/proposal", delayLocalSave);
  await title.fill(localTitle);
  const remote = await publicPut(
    page,
    `/api/v1/quest-initializations/${basis!.initialization_id}/proposal`,
    {
      expected_draft_revision: basis!.quest_draft.revision,
      expected_draft_hash: basis!.quest_draft.hash,
      expected_proposal_ref: basis!.proposal!.ref,
      expected_proposal_hash: basis!.proposal!.hash,
      explicit_review: false,
      content: { ...basis!.proposal!.content, title: remoteTitle },
    },
  );
  expect(remote.status).toBe(200);
  await title.blur();

  const reloadLatest = dialog.getByRole("button", {
    name: "载入最新 durable 版本并放弃本地未保存修改",
  });
  await expect(dialog.getByRole("alert")).toContainText("question_proposal_stale");
  await expect(reloadLatest).toBeFocused();
  await expect(title).toHaveValue(localTitle);
  expect((await publicStatus(page, basis!.initialization_id)).proposal?.content.title).toBe(
    remoteTitle,
  );

  await reloadLatest.click();
  await page.unroute("**/api/v1/quest-initializations/*/proposal", delayLocalSave);
  await expect(dialog.getByRole("alert")).toBeHidden();
  await expect(title).toHaveValue(remoteTitle);

  const recoveredTitle = `${QUESTION.title}（显式恢复后保存）`;
  await title.fill(recoveredTitle);
  await title.blur();
  await expect.poll(async () => (
    await publicStatus(page, basis!.initialization_id)
  ).proposal?.content.title).toBe(recoveredTitle);
});

test("real Chrome traverses the corrected durable state machine and a second creation", async ({
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page, runningProduct());
  expect(
    await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches),
  ).toBeTruthy();

  let { dialog, opener } = await openCreation(page);
  const initiallyFocusedClose = dialog.getByRole("button", {
    name: "关闭创建 Quest 窗口",
  });
  await expect(initiallyFocusedClose).toBeFocused();
  for (const control of [
    initiallyFocusedClose,
    dialog.getByRole("button", { name: "检测本机计算卡" }),
    dialog.getByRole("button", { name: "生成第一个问题" }),
    dialog.getByRole("button", { name: "发送消息" }),
    dialog.getByRole("button", { name: "取消" }),
    dialog.getByRole("button", { name: "确认创建 Quest 与第一个问题" }),
  ]) {
    await expectMinimumTouchTarget(control);
  }
  for (const maskedRegion of dynamicSnapshotMasks(page, dialog)) {
    await expect(maskedRegion).toBeVisible();
  }
  await expect(page).toHaveScreenshot("quest-state-draft-1440.png", {
    animations: "disabled",
    mask: dynamicSnapshotMasks(page, dialog),
    maskColor: "#202a3a",
    maxDiffPixels: 400,
  });
  await attachFixedVisualPair(
    page,
    testInfo,
    "create-quest",
    { width: 1440, height: 900 },
  );

  const close = dialog.getByRole("button", { name: "关闭创建 Quest 窗口" });
  await close.focus();
  await page.keyboard.press("Shift+Tab");
  expect(
    await dialog.evaluate((root) => root.contains(document.activeElement)),
  ).toBeTruthy();
  for (let index = 0; index < 24; index += 1) {
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((root) => root.contains(document.activeElement)),
    ).toBeTruthy();
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  ({ dialog, opener } = await openCreation(page));

  await page.mouse.click(3, 3);
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  ({ dialog, opener } = await openCreation(page));

  const cancellationCandidate = await publicCurrent(page);
  expect(cancellationCandidate).not.toBeNull();
  await dialog.getByRole("button", { name: "取消" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  await expect.poll(async () => publicCurrent(page)).toBeNull();
  const cancelled = await publicStatus(
    page,
    cancellationCandidate!.initialization_id,
  );
  expect(cancelled.status).toBe("cancelled");
  expect(cancelled).not.toHaveProperty("quest_ref");
  expect(cancelled).not.toHaveProperty("memory_ref");
  expect(cancelled).not.toHaveProperty("question_ref");
  expect(cancelled).not.toHaveProperty("cycle_ref");
  expect(
    Object.values(cancelled.receipts).every(
      (receipt) => receipt.status === "not_attempted",
    ),
  ).toBeTruthy();
  ({ dialog } = await openCreation(page));

  await fillRequiredBasis(
    dialog,
    "FAIL FIRST PROPOSAL：RECOVER FIRST OWNER：判断低照度显微图像去噪能否保留稀有形态",
    "形成带反例、适用范围和证据边界的比较结论",
  );

  const beforeIntent = await publicCurrent(page);
  expect(beforeIntent).not.toBeNull();
  await sendIntent(dialog, "怎样把第一问缩小到可证伪边界？");
  await expect(
    dialog.getByRole("article").filter({
      hasText: "Drafting Session · completed",
    }),
  ).toContainText("建议先固定可证伪边界：怎样把第一问缩小到可证伪边界？", {
    timeout: 12_000,
  });

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  ({ dialog } = await openCreation(page));
  await expect(
    dialog.getByRole("article").filter({
      hasText: "你 · draft r",
    }),
  ).toContainText("怎样把第一问缩小到可证伪边界？");
  await expect(
    dialog.getByRole("article").filter({
      hasText: "Drafting Session · completed",
    }),
  ).toContainText("建议先固定可证伪边界：怎样把第一问缩小到可证伪边界？");

  await sendIntent(dialog, "请模拟一次 typed unavailable，并保持左侧草案不变");
  await expect(
    dialog.getByRole("article").filter({
      hasText: "Drafting Session · unavailable",
    }),
  ).toContainText("capability_unavailable · deterministic_intent_unavailable", {
    timeout: 12_000,
  });
  const afterUnavailableIntent = await publicCurrent(page);
  expect(afterUnavailableIntent?.quest_draft).toEqual(beforeIntent?.quest_draft);
  await expect(dialog.getByLabel("这个 Quest 最终要完成什么？")).toHaveValue(
    beforeIntent!.quest_draft.value.goal,
  );
  await expect(dialog.getByLabel("什么情况算完成？")).toHaveValue(
    beforeIntent!.quest_draft.value.completion_criteria,
  );
  expect(
    Object.values(afterUnavailableIntent?.receipts ?? {}).every(
      (receipt) => receipt.status === "not_attempted",
    ),
  ).toBeTruthy();
  expect(afterUnavailableIntent).not.toHaveProperty("quest_ref");
  expect(afterUnavailableIntent).not.toHaveProperty("memory_ref");
  expect(afterUnavailableIntent).not.toHaveProperty("question_ref");
  expect(afterUnavailableIntent).not.toHaveProperty("cycle_ref");

  const literature = dialog.getByLabel("文献搜索范围");
  await expect(literature.locator("option")).toHaveCount(3);
  await literature.selectOption("oa_only");
  await expect(literature).toHaveValue("oa_only");
  await expect(literature.locator('option[value="provided_only"]')).toHaveAttribute(
    "disabled",
    "",
  );
  const acquisitionStatus = dialog.getByTestId("acquisition-session-status");
  await expect(acquisitionStatus).toContainText(
    "ready · current · browser not_required",
  );
  await literature.selectOption("oa_then_institution");
  await expect(literature).toHaveValue("oa_then_institution");
  await expect(acquisitionStatus).toContainText(
    "waiting_user · current · institutional_entry_required",
  );
  await expect(
    dialog.getByRole("button", { name: "重新检测登录" }),
  ).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "先运行 DeepFetch" })).toBeEnabled();
  await expect(dialog.getByRole("button", { name: "直接根据目标生成" })).toBeEnabled();

  await dialog.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    dialog.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await selectReadyCompute(dialog);

  const firstGeneration = dialog.getByRole("button", { name: "生成第一个问题" });
  await firstGeneration.click();
  const proposalFailure = dialog.getByRole("alert");
  await expect(proposalFailure).toContainText("deterministic_proposal_failed", {
    timeout: 12_000,
  });
  await expect(proposalFailure).toBeFocused();
  const failedGeneration = await publicCurrent(page);
  expect(failedGeneration?.proposal).toBeNull();
  expect(failedGeneration).toMatchObject({
    status: "draft",
    proposal_generation: {
      status: "failed",
      failure: { code: "deterministic_proposal_failed" },
    },
  });
  await generateReadyProposal(dialog);

  const proposalTitle = dialog.getByLabel("首问题标题");
  await proposalTitle.fill(`${QUESTION.title}（人工复核）`);
  await proposalTitle.blur();
  await expect(
    dialog.getByText("六字段完整 · 首问题已自动保存", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "确认创建 Quest 与第一个问题" }),
  ).toBeEnabled();

  const reviewedTitle = `${QUESTION.title}（人工复核）`;
  await proposalTitle.fill("");
  await proposalTitle.blur();
  await expect.poll(async () => {
    const incomplete = await publicCurrent(page);
    return {
      status: incomplete?.status,
      proposal: incomplete?.proposal?.status,
      preview: incomplete?.confirmation_preview?.status,
    };
  }).toEqual({ status: "draft", proposal: "incomplete", preview: "stale" });
  await expect(dialog.getByText("proposal_incomplete", { exact: true })).toBeVisible();
  await expect(
    dialog.getByText(
      "Proposal 当前 basis 仍有效，但四个必填字段尚未完整；补齐后才能确认。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "确认创建 Quest 与第一个问题" }),
  ).toBeDisabled();
  await proposalTitle.fill(reviewedTitle);
  await proposalTitle.blur();
  await expect(
    dialog.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();

  const regenerate = dialog.getByRole("button", {
    name: "重新生成第一个问题",
  });
  const delayProposalAutosave = async (route: Route) => {
    if (route.request().method() === "PUT") {
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 400));
    }
    await route.continue();
  };
  await page.route("**/api/v1/quest-initializations/*/proposal", delayProposalAutosave);
  await proposalTitle.fill(`${QUESTION.title}（不 blur 立即重生成）`);
  await regenerate.click();
  await waitForReadyProposal(dialog);
  await page.unroute("**/api/v1/quest-initializations/*/proposal", delayProposalAutosave);
  const regenerationKeptFocus = await regenerate.evaluate(
    (button) => button === document.activeElement,
  );

  await page.setViewportSize({ width: 800, height: 900 });
  await expect(page).toHaveScreenshot("quest-state-ready-800.png", {
    animations: "disabled",
    mask: dynamicSnapshotMasks(page, dialog),
    maskColor: "#202a3a",
    maxDiffPixels: 400,
  });
  await attachFixedVisualPair(
    page,
    testInfo,
    "create-quest",
    { width: 800, height: 900 },
  );

  const beforeBudgetChange = await publicCurrent(page);
  expect(beforeBudgetChange?.resource_envelope?.status).toBe("current");
  expect(beforeBudgetChange?.proposal?.status).toBe("current");
  expect(beforeBudgetChange?.confirmation_preview?.status).toBe("current");
  const lateRegeneration = dialog.getByRole("button", {
    name: "重新生成第一个问题",
  });
  await lateRegeneration.click();
  await expect(
    dialog.getByText(
      "正在依据精确 DraftRevision 生成六字段；Quest 配置和右侧 Session 仍可使用。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(lateRegeneration).toBeDisabled();
  await expect(proposalTitle).toBeDisabled();
  const parallelGenerationIntent = dialog.getByLabel(
    "在 Quest Drafting Session 中发消息",
  );
  await expect(parallelGenerationIntent).toBeEnabled();
  await parallelGenerationIntent.fill("并行解释重新生成的当前依据");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect(
    dialog.getByText("并行解释重新生成的当前依据", { exact: true }),
  ).toBeVisible();
  await expect(parallelGenerationIntent).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "取消" })).toBeEnabled();

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  ({ dialog } = await openCreation(page));
  const reopenedRegeneration = dialog.getByRole("button", {
    name: "重新生成第一个问题",
  });
  await expect(reopenedRegeneration).toBeDisabled();
  await expect(dialog.getByLabel("首问题标题")).toBeDisabled();
  const reopenedIntent = dialog.getByLabel("在 Quest Drafting Session 中发消息");
  await expect(reopenedIntent).toBeDisabled();
  await expect(dialog.getByRole("button", { name: "取消" })).toBeEnabled();

  const timeBudget = dialog.getByLabel("时间预算");
  await timeBudget.selectOption("7d");
  await timeBudget.blur();
  await expect(dialog.getByText("proposal_stale", { exact: true })).toBeVisible({
    timeout: 12_000,
  });
  await expect(reopenedRegeneration).toBeEnabled({ timeout: 12_000 });
  await expect(reopenedIntent).toBeDisabled();
  await expect(
    dialog.getByRole("article").filter({
      hasText: "Drafting Session · completed",
    }).last(),
  ).toContainText("建议先固定可证伪边界：并行解释重新生成的当前依据", {
    timeout: 12_000,
  });
  expect(
    (await publicCurrent(page))?.intent_session?.turns.some(
      (turn) => turn.user_content === "并行解释重新生成的当前依据",
    ),
  ).toBeTruthy();
  await expect(reopenedIntent).toBeEnabled();
  await expect.poll(async () => {
    return (await publicCurrent(page))?.quest_draft.value.time_budget;
  }).toBe("7d");
  const afterBudgetChange = await publicCurrent(page);
  expect(afterBudgetChange?.resource_envelope).toMatchObject({
    status: "current",
    time_budget: "7d",
    hard_ceiling: { kind: "wall_clock", seconds: 604_800 },
  });
  expect(afterBudgetChange?.resource_envelope?.ref).not.toBe(
    beforeBudgetChange?.resource_envelope?.ref,
  );
  expect(afterBudgetChange?.resource_envelope?.hash).not.toBe(
    beforeBudgetChange?.resource_envelope?.hash,
  );
  expect(afterBudgetChange?.proposal?.status).toBe("stale");
  expect(afterBudgetChange?.confirmation_preview?.status).toBe("stale");

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page).toHaveScreenshot("quest-state-stale-390.png", {
    animations: "disabled",
    mask: dynamicSnapshotMasks(page, dialog),
    maskColor: "#202a3a",
    maxDiffPixels: 400,
  });
  await attachFixedVisualPair(
    page,
    testInfo,
    "create-quest",
    { width: 390, height: 844 },
  );
  const compactLayout = await dialog.evaluate((root) => {
    const form = root.querySelector("[data-testid=quest-continuous-form]");
    const session = root.querySelector("[data-testid=quest-intent-session]");
    if (!(form instanceof HTMLElement) || !(session instanceof HTMLElement)) {
      throw new Error("corrected Quest layout regions are missing");
    }
    return {
      formBeforeSession: Boolean(
        form.compareDocumentPosition(session) & Node.DOCUMENT_POSITION_FOLLOWING,
      ),
      formBottom: form.getBoundingClientRect().bottom,
      sessionTop: session.getBoundingClientRect().top,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
    };
  });
  expect(compactLayout.formBeforeSession).toBeTruthy();
  expect(compactLayout.sessionTop).toBeGreaterThanOrEqual(
    compactLayout.formBottom - 1,
  );
  expect(compactLayout.scrollWidth).toBeLessThanOrEqual(compactLayout.innerWidth);

  await generateReadyProposal(dialog);
  const first = await publicCurrent(page);
  expect(first).not.toBeNull();
  const firstDetails = await openTechnicalDetails(dialog);
  await expectTargetAssertions(firstDetails, first!);
  const confirmation = dialog.getByRole("button", {
    name: "确认创建 Quest 与第一个问题",
  });
  await expect(confirmation).toBeEnabled();
  await expect(
    dialog.getByTestId("quest-confirmation-footer").getByRole("button"),
  ).toHaveCount(2);
  await confirmation.click();
  await expect(
    dialog.getByText("正在创建；重复确认已禁用，可安全关闭后恢复", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByText("正在从首个缺失 receipt 恢复", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Quest 与第一个问题已就绪。", { exact: false })).toBeVisible({
    timeout: 15_000,
  });
  const completedFirst = await publicStatus(page, first!.initialization_id);
  expect(completedFirst.status).toBe("completed");
  await expectAcceptedReceipts(firstDetails, completedFirst);
  await expect(dialog.getByRole("button", { name: "取消" })).toBeDisabled();
  const rejectedCompletedCancellation = await publicPost(
    page,
    `/api/v1/quest-initializations/${first!.initialization_id}/cancel`,
  );
  expect(rejectedCompletedCancellation).toEqual({
    status: 409,
    body: { detail: { code: "confirmed_quest_cannot_be_cancelled" } },
  });
  expect((await publicStatus(page, first!.initialization_id)).status).toBe("completed");

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  ({ dialog } = await openCreation(page));
  const second = await publicCurrent(page);
  expect(second).not.toBeNull();
  expect(second?.initialization_id).not.toBe(first?.initialization_id);
  await expect(dialog.getByLabel("这个 Quest 最终要完成什么？")).toHaveValue("");

  await fillRequiredBasis(
    dialog,
    "第二个 Quest：验证部分完成后的自动对账",
    "最终形成五张互不混淆的 Owner receipt",
  );
  await selectReadyCompute(dialog);
  await generateReadyProposal(dialog);
  await dialog.getByRole("button", {
    name: "确认创建 Quest 与第一个问题",
  }).click();
  await expect(
    dialog.getByText("Quest 已创建，第一个问题尚未完成", { exact: true }),
  ).toBeVisible({ timeout: 12_000 });
  const partial = await waitForPublicStatus(
    page,
    second!.initialization_id,
    "partial",
  );
  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  ({ dialog } = await openCreation(page));
  await expect(
    dialog.getByText(
      /Quest 已创建，第一个问题尚未完成|正在从首个缺失 receipt 恢复/,
    ).first(),
  ).toBeVisible();
  await waitForPublicStatus(page, second!.initialization_id, "recovering");
  await expect(
    dialog.getByText("正在从首个缺失 receipt 恢复", { exact: true }).first(),
  ).toBeVisible();
  await waitForPublicStatus(page, second!.initialization_id, "partial");
  await expect(
    dialog.getByText("Quest 已创建，第一个问题尚未完成", { exact: true }).first(),
  ).toBeVisible();
  const partialDetails = await openTechnicalDetails(dialog);
  const downstreamNotAttempted = Object.entries(partial.receipts).filter(
    ([, receipt]) =>
      receipt.status === "not_attempted" && Boolean(receipt.reason?.upstream_step),
  );
  expect(downstreamNotAttempted.length).toBeGreaterThan(0);
  for (const [name, receipt] of downstreamNotAttempted) {
    const record = partialDetails.getByRole("article").filter({
      hasText: `${name} · not_attempted`,
    });
    await expect(record).toContainText(`reason · ${receipt.reason?.code}`);
    await expect(record).toContainText(
      `upstream step · ${receipt.reason?.upstream_step}`,
    );
  }

  await expect
    .poll(async () => (await publicStatus(page, second!.initialization_id)).status, {
      timeout: 15_000,
    })
    .toBe("completed");
  await expect(dialog.getByText("Quest 与第一个问题已就绪。", { exact: false })).toBeVisible();

  runningProduct().damageQuestionContentCustody();
  await expect(
    dialog.getByText("已完成事实的持久对象暂时无法验证。", { exact: true }),
  ).toBeVisible({ timeout: 8_000 });
  const unavailable = await publicStatus(page, second!.initialization_id);
  expect(unavailable).toMatchObject({
    status: "unavailable",
    receipts: {
      question_content: {
        status: "rejected",
        reason: { code: "question_content_custody_unavailable" },
      },
      question_identity: {
        status: "not_attempted",
        reason: {
          code: "upstream_not_accepted",
          upstream_step: "question_content",
        },
      },
      cycle_activation: {
        status: "not_attempted",
        reason: {
          code: "upstream_not_accepted",
          upstream_step: "question_content",
        },
      },
    },
  });
  expect(regenerationKeptFocus).toBeTruthy();
});
