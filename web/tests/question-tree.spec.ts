import { expect, test, type Page, type Route } from "@playwright/test";

import { DeterministicProduct } from "./support/deterministic-product.js";

type JsonRecord = Record<string, any>;

let product: DeterministicProduct | null = null;

test.describe.configure({ timeout: 120_000 });

test.afterEach(async () => {
  const running = product;
  product = null;
  if (running) await running.stop();
});

async function fulfill(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function openQuestionTreeFixture(
  page: Page,
  options: {
    transformQuestionHistory?: (body: JsonRecord) => JsonRecord;
    skipInitialSummaryAssertions?: boolean;
  } = {},
) {
  product = await DeterministicProduct.start();
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as JsonRecord;
  const snapshot: JsonRecord = {
    ...base,
    research_space: {
      ...base.research_space,
      status: "active",
      quest_count: 1,
      question_count: 3,
      foreground_cycle_count: 1,
      current_quest: {
        status: "ready",
        quest_ref: "quest_tree_1",
        goal_revision_ref: "quest-goal-revision-tree-1",
        draft_revision: 3,
        draft_hash: "9".repeat(64),
        goal: "验证问题树只读上下文与当前研究目标保持一致",
        completion_criteria: "三个宽度均可导航，并且选择不写 Owner",
        projection_digest: "8".repeat(64),
        reason: null,
      },
      current_question: {
        quest_ref: "quest_foreign",
        question_ref: "question_foreign_stage",
      },
    },
    question_tree: {
      status: "ready",
      reason: null,
      items: [{
        question_ref: "question_tree_root",
        quest_ref: "quest_tree_1",
        parent_question_ref: null,
        title: "Root question",
        unknown_statement: "Root unknown",
        content_ref: "content-tree-root",
        content_hash: "1".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-root",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "bound",
          cycle_ref: "cycle_tree_1",
          foreground: {
            quest_ref: "quest_tree_1",
            question_ref: "question_tree_root",
            cycle_ref: "cycle_tree_1",
            epoch: 4,
            stage: "idea",
            status: "active",
          },
          reason: null,
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }, {
        question_ref: "question_tree_formal",
        quest_ref: "quest_tree_1",
        parent_question_ref: "question_tree_root",
        title: "Formal question",
        unknown_statement: "Formal unknown",
        content_ref: "content-tree-formal",
        content_hash: "2".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-formal",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }, {
        question_ref: "question_tree_branch",
        quest_ref: "quest_tree_1",
        parent_question_ref: "question_tree_formal",
        title: "Branch question",
        unknown_statement: "Branch unknown",
        content_ref: "content-tree-branch",
        content_hash: "3".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-tree-branch",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: {
          status: "ready",
          items: [{
            request_ref: "human-request-tree-1",
            issuer: "agent_runtime",
            kind: "offline_action",
            status: "open",
            revision: 1,
            bindings: [{
              source: "target_assertion",
              field: "question_ref",
              ref: "question_tree_branch",
            }],
          }],
          reason: null,
        },
      }],
    },
    human_collaboration: {
      companion: {
        status: "ready",
        scope_ref: "quest:quest_tree_1",
        messages: [],
        soft_constraints: [],
        agent_proposals: [],
      },
      human_requests: {
        status: "ready",
        waiting: {
          scope: "none",
          safe_meaningful_runnable_exists: true,
          other_blockers: [],
        },
        items: [{
          request_ref: "human-request-foreign-first",
          request_id: "HR-FOREIGN",
          revision: 1,
          issuer: "research_graph",
          quest_ref: "quest_foreign",
          kind: "offline_action",
          status: "open",
          obligation: "foreign Quest blocker must not enter this summary",
          direct_waiters: [{
            waiter_ref: "waiter-foreign",
            wait_scope: "local",
            status: "blocked",
            target_assertion: { quest_ref: "quest_foreign" },
            other_blockers: [],
          }],
        }, {
          request_ref: "human-request-current-local",
          request_id: "HR-CURRENT",
          revision: 1,
          issuer: "research_graph",
          quest_ref: "quest_tree_1",
          kind: "library_reconnect",
          status: "open",
          obligation: "current Quest local blocker",
          direct_waiters: [{
            waiter_ref: "waiter-current",
            wait_scope: "local",
            status: "blocked",
            target_assertion: { quest_ref: "quest_tree_1" },
            other_blockers: [],
          }],
        }],
      },
      commands: { status: "ready", items: [], authorizations: [] },
    },
  };

  await page.route("**/api/v1/snapshot", (route) => fulfill(route, snapshot));
  await page.route("**/api/v1/questions/*/evidence", (route) => {
    const parts = new URL(route.request().url()).pathname.split("/");
    const questionRef = decodeURIComponent(parts.at(-2) ?? "");
    const item = snapshot.question_tree.items.find(
      (candidate: JsonRecord) => candidate.question_ref === questionRef,
    );
    if (!item || questionRef !== "question_tree_branch") {
      return fulfill(route, {
        status: "absent",
        question_ref: questionRef,
        quest_ref: item?.quest_ref ?? null,
        binding: null,
        items: [],
        reason: { code: "question_evidence_binding_absent" },
      });
    }
    return fulfill(route, {
      status: "ready",
      question_ref: questionRef,
      quest_ref: item.quest_ref,
      binding: {
        cycle_ref: "cycle_tree_1",
        request_ref: "idea-request-tree-1",
        context_pack_ref: "context-pack-tree-1",
        context_pack_hash: "4".repeat(64),
        evidence_reference_revision: 7,
        question_receipt_ref: item.question_receipt_ref,
        question_receipt_hash: "5".repeat(64),
      },
      items: [{
        evidence_ref: "asset-version-tree-evidence-1",
        role: {
          role_ref: "asset-role-tree-evidence-1",
          version_ref: "asset-version-tree-evidence-1",
          asset_ref: "asset-tree-evidence-1",
          asset_hash: "6".repeat(64),
          manifest_hash: "7".repeat(64),
          role: "evidence",
          quest_ref: item.quest_ref,
          accepted_at: 1_720_000_100,
          asset_receipt: {},
          receipt: { receipt_ref: "rg-role-receipt-tree-1" },
        },
        asset: {
          version_ref: "asset-version-tree-evidence-1",
          display_name: "verified-morphology-results.csv",
          media_type: "text/csv",
          content_hash: "6".repeat(64),
          manifest_hash: "7".repeat(64),
          integrity: "verified",
          availability: "available",
          receipt: { receipt_ref: "rm-asset-receipt-tree-1" },
        },
      }],
      reason: null,
    });
  });
  await page.route("**/api/v1/questions/*/history*", (route) => {
    const url = new URL(route.request().url());
    const parts = url.pathname.split("/");
    const questionRef = decodeURIComponent(parts.at(-2) ?? "");
    const item = snapshot.question_tree.items.find(
      (candidate: JsonRecord) => candidate.question_ref === questionRef,
    );
    if (!item) return fulfill(route, {
      status: "absent",
      question_ref: questionRef,
      question: null,
      lifecycle: null,
      events: [],
      offset: 0,
      limit: 50,
      total_count: 0,
      has_more: false,
      reason: { code: "accepted_question_not_found" },
    });
    const accepted = {
      action: "accepted",
      question_ref: questionRef,
      affected_question_refs: [questionRef],
      status: "active",
      lifecycle_revision: 1,
      record_ref: questionRef,
      prune_record_ref: null,
      restore_record_ref: null,
      base_graph_version: null,
      committed_graph_version: null,
      receipt_ref: item.question_receipt_ref,
      receipt_hash: "5".repeat(64),
      recorded_at: null,
    };
    const branchEvents = questionRef === "question_tree_branch" ? [{
      action: "prune",
      question_ref: questionRef,
      affected_question_refs: [questionRef],
      status: "pruned",
      lifecycle_revision: 2,
      record_ref: "prune-record-tree-1",
      prune_record_ref: "prune-record-tree-1",
      restore_record_ref: null,
      base_graph_version: 3,
      committed_graph_version: 4,
      receipt_ref: "rg-prune-receipt-tree-1",
      receipt_hash: "9".repeat(64),
      recorded_at: 1_720_000_150,
    }, {
      action: "restore",
      question_ref: questionRef,
      affected_question_refs: [questionRef],
      status: "active",
      lifecycle_revision: 3,
      record_ref: "restore-record-tree-1",
      prune_record_ref: "prune-record-tree-1",
      restore_record_ref: "restore-record-tree-1",
      base_graph_version: 4,
      committed_graph_version: 5,
      receipt_ref: "rg-restore-receipt-tree-1",
      receipt_hash: "a".repeat(64),
      recorded_at: 1_720_000_200,
    }] : [];
    const allEvents = [accepted, ...branchEvents];
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const limit = Number(url.searchParams.get("limit") ?? 50);
    const body = {
      status: "ready",
      question_ref: questionRef,
      question: {
        question_ref: questionRef,
        quest_ref: item.quest_ref,
        parent_question_ref: item.parent_question_ref,
        initialization_id: "quest-init-tree-1",
        context_ref: "manual-context-tree-1",
        content: {
          content_ref: item.content_ref,
          content_hash: item.content_hash,
          schema_ref: item.schema_ref,
          document: {
            title: item.title,
            unknown_statement: item.unknown_statement,
            answer_shape: "Auditable answer",
            applicability_scope: "Tree fixture",
            background_context: "Accepted context",
            requirements_constraints: "Read only",
          },
        },
        receipts: {
          content_acceptance: { receipt_ref: `rm-content-receipt-${questionRef}` },
          question_acceptance: { receipt_ref: item.question_receipt_ref },
          confirmation_ref: `hc-question-confirmation-${questionRef}`,
          confirmation_hash: "8".repeat(64),
        },
      },
      lifecycle: {
        question_ref: questionRef,
        quest_ref: item.quest_ref,
        status: "active",
        revision: allEvents.length,
        owner_revision: 17,
        updated_at: 1_720_000_200,
      },
      events: allEvents.slice(offset, offset + limit),
      offset,
      limit,
      total_count: allEvents.length,
      has_more: offset + limit < allEvents.length,
      reason: null,
    };
    return fulfill(
      route,
      options.transformQuestionHistory?.(body) ?? body,
    );
  });
  await page.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  if (!options.skipInitialSummaryAssertions) {
    await expect(page.getByTestId("return-summary")).toContainText(
      "验证问题树只读上下文与当前研究目标保持一致",
    );
    await expect(page.getByTestId("return-summary")).toContainText(
      "三个宽度均可导航，并且选择不写 Owner",
    );
    await expect(page.getByTestId("return-summary")).toContainText(
      "Local wait · library_reconnect",
    );
    await expect(page.getByTestId("return-summary")).not.toContainText(
      "Local wait · offline_action",
    );
  }
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  await expect(tree).toBeVisible();
  return tree;
}

test("QuestionTree rail overview reveals the cached overview by the next paint", async ({
  page,
}) => {
  const tree = await openQuestionTreeFixture(page, {
    skipInitialSummaryAssertions: true,
  });
  const overview = page.locator("main.lumen-main");
  await expect(tree).toBeVisible();
  await expect(overview).toBeHidden();

  await page.unroute("**/api/v1/snapshot");
  let releaseSnapshot!: () => void;
  const snapshotGate = new Promise<void>((resolve) => {
    releaseSnapshot = resolve;
  });
  let snapshotDeferred = false;
  await page.route("**/api/v1/snapshot", async (route) => {
    snapshotDeferred = true;
    await snapshotGate;
    await fulfill(route, {});
  });
  await page.evaluate(() => {
    void fetch("/api/v1/snapshot");
  });
  await expect.poll(() => snapshotDeferred).toBe(true);

  await page.getByRole("button", { name: "Quest 总览", exact: true }).click();
  const visibleByNextPaint = await page.evaluate(() => new Promise<boolean>((resolve) => {
    window.requestAnimationFrame(() => {
      resolve(!document.querySelector("main.lumen-main")?.hasAttribute("hidden"));
    });
  }));

  expect(visibleByNextPaint).toBe(true);
  await expect(overview).toBeVisible();
  await expect(tree).toBeHidden();
  releaseSnapshot();
});

test("QuestionTree keeps canvas at 800/1440 and reflows the same keyboard tree into a 390 outline", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tree = await openQuestionTreeFixture(page);
  const canvas = tree.locator(".question-tree-canvas");
  const root = tree.locator('[data-question-ref="question_tree_root"]');
  const formal = tree.locator('[data-question-ref="question_tree_formal"]');
  const branch = tree.locator('[data-question-ref="question_tree_branch"]');

  await expect(canvas).toHaveAttribute("data-layout-mode", "canvas");
  await expect(tree.getByLabel("问题树小地图")).toBeVisible();
  await root.focus();
  await root.press("ArrowDown");
  await expect(formal).toBeFocused();
  await expect(formal).toHaveAttribute("aria-selected", "true");
  await formal.press("End");
  await expect(branch).toBeFocused();
  await expect(branch).toHaveAttribute("aria-selected", "true");
  await expect(branch.getByLabel("关联 1 个 HumanRequest")).toBeVisible();
  await expect(tree.getByLabel("选中问题详情")).toContainText(
    "human-request-tree-1 · offline_action · open",
  );

  await page.setViewportSize({ width: 800, height: 900 });
  await expect(canvas).toHaveAttribute("data-layout-mode", "canvas");
  await expect(tree.getByLabel("问题树小地图")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(canvas).toHaveAttribute("data-layout-mode", "outline");
  await expect(tree.getByLabel("Quest 问题树缩进大纲")).toBeVisible();
  await expect(tree.getByLabel("问题树小地图")).toBeHidden();
  await expect(tree.getByRole("button", { name: "适配全图" })).toBeHidden();
  const geometry = await tree.locator(".question-tree-canvas-world").evaluate((world) => {
    const nodes = Array.from(world.querySelectorAll<HTMLElement>(".question-canvas-node"));
    return {
      transform: getComputedStyle(world).transform,
      positions: nodes.map((node) => ({
        x: node.getBoundingClientRect().x,
        position: getComputedStyle(node).position,
      })),
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  });
  expect(geometry.transform).toBe("none");
  expect(geometry.positions.map((item) => item.position)).toEqual([
    "relative",
    "relative",
    "relative",
  ]);
  expect(geometry.positions[1].x).toBeGreaterThan(geometry.positions[0].x);
  expect(geometry.positions[2].x).toBeGreaterThan(geometry.positions[1].x);
  expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
});

test("QuestionTree routes discussion without owner writes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tree = await openQuestionTreeFixture(page);
  const writes: string[] = [];
  let postedMessage: JsonRecord | null = null;
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  await page.route("**/api/v1/companion/messages", async (route) => {
    postedMessage = route.request().postDataJSON() as JsonRecord;
    await fulfill(route, { status: "queued" });
  });

  const branch = tree.locator('[data-question-ref="question_tree_branch"]');
  await branch.click();
  expect(new URL(page.url()).searchParams.get("node")).toBe("question_tree_branch");
  await expect(page.getByTestId("companion-question-context")).toContainText(
    "Branch question",
  );
  const discuss = tree.getByRole("button", { name: "与 Companion 讨论此题" });
  await expect(discuss).toBeEnabled();
  await discuss.click();
  await expect(page.getByLabel("给 Quest Companion 发消息")).toBeFocused();
  expect(writes).toEqual([]);

  await page.getByLabel("给 Quest Companion 发消息").fill("这个问题还缺少哪类证据？");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => postedMessage).toEqual({
    scope_ref: "quest:quest_tree_1",
    message: "这个问题还缺少哪类证据？",
    view_context: {
      kind: "question",
      quest_ref: "quest_tree_1",
      question_ref: "question_tree_branch",
      content_ref: "content-tree-branch",
      content_hash: "3".repeat(64),
      lifecycle_revision: 1,
    },
  });
  expect(writes).toEqual(["POST /api/v1/companion/messages"]);
});

test("QuestionTree evidence and history entries expose real read-only facts at every responsive width", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tree = await openQuestionTreeFixture(page);
  const unsafeRequests: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      unsafeRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  await tree.locator('[data-question-ref="question_tree_branch"]').click();

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    const evidenceButton = tree.getByRole("button", { name: "查看证据与来源" });
    await expect(evidenceButton).toBeEnabled();
    await evidenceButton.click();
    const evidence = tree.getByRole("region", { name: "问题证据与来源" });
    await expect(evidence).toContainText("verified-morphology-results.csv");
    await expect(evidence).toContainText("asset-version-tree-evidence-1");
    await expect(evidence).toContainText("rg-role-receipt-tree-1");

    const historyButton = tree.getByRole("button", { name: "问题历史 ↗" });
    await expect(historyButton).toBeEnabled();
    await historyButton.click();
    const history = tree.getByRole("region", { name: "问题历史" });
    await expect(history).toContainText("Branch unknown");
    await expect(history).toContainText("prune-record-tree-1");
    await expect(history).toContainText("rg-restore-receipt-tree-1");
    const width = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(width.scroll).toBeLessThanOrEqual(width.viewport);
  }
  expect(unsafeRequests).toEqual([]);
});

test("QuestionTree fails closed when a read endpoint returns another Question identity", async ({
  page,
}) => {
  const tree = await openQuestionTreeFixture(page);
  await tree.locator('[data-question-ref="question_tree_branch"]').click();
  await page.route("**/api/v1/questions/*/evidence", (route) => fulfill(route, {
    status: "ready",
    question_ref: "question_from_another_response",
    quest_ref: "quest_tree_1",
    binding: {
      question_receipt_ref: "receipt-from-another-question",
    },
    items: [],
    reason: null,
  }));
  await tree.getByRole("button", { name: "查看证据与来源" }).click();
  const evidence = tree.getByRole("region", { name: "问题证据与来源" });
  await expect(evidence).toContainText("只读查询失败");
  await expect(evidence).toContainText(
    "question_evidence_response_identity_invalid",
  );
  await expect(evidence).not.toContainText("receipt-from-another-question");
  const close = evidence.getByRole("button", { name: "关闭问题只读下钻" });
  await close.focus();
  await close.press("Enter");
  await expect(tree.getByRole("button", { name: "查看证据与来源" })).toBeFocused();
});

test("QuestionTree fails closed when Question history pagination contradicts lifecycle authority", async ({
  page,
}) => {
  const tree = await openQuestionTreeFixture(page, {
    transformQuestionHistory: (body) => ({
      ...body,
      total_count: body.total_count - 1,
    }),
  });
  await tree.locator('[data-question-ref="question_tree_branch"]').click();
  await tree.getByRole("button", { name: "问题历史 ↗" }).click();
  const history = tree.getByRole("region", { name: "问题历史" });
  await expect(history).toContainText("只读查询失败");
  await expect(history).toContainText(
    "question_history_response_identity_invalid",
  );
  await expect(history).not.toContainText("rg-restore-receipt-tree-1");
});

test("History rail opens the accepted current Question through the same read-only inspector at 1440/800/390", async ({
  page,
}) => {
  const tree = await openQuestionTreeFixture(page);
  const unsafeRequests: string[] = [];
  const questionReads: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
      unsafeRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/questions/")) {
      const parts = url.pathname.split("/");
      questionReads.push(`${parts.at(-2)}:${parts.at(-1)}`);
    }
  });
  const historyRail = page.getByRole("button", { name: "历史", exact: true });
  await tree.locator('[data-question-ref="question_tree_branch"]').click();
  expect(new URL(page.url()).searchParams.get("node")).toBe("question_tree_branch");
  await historyRail.click();
  await expect(page.getByRole("region", { name: "问题历史" })).toContainText(
    "Root unknown",
  );
  expect(new URL(page.url()).searchParams.get("node")).toBe("question_tree_root");
  const closeHistory = page.getByRole("button", { name: "关闭问题只读下钻" });
  await closeHistory.focus();
  await closeHistory.press("Enter");
  await expect(page.getByRole("region", { name: "问题历史" })).toBeHidden();
  await expect(historyRail).not.toHaveClass(/active/);
  expect(new URL(page.url()).searchParams.get("inspector")).toBeNull();
  await expect(tree.getByRole("button", { name: "问题历史 ↗" })).toBeFocused();
  await historyRail.click();
  await expect(page.getByRole("region", { name: "问题历史" })).toContainText(
    "Root unknown",
  );
  await page.getByRole("button", { name: "回到总览" }).click();
  await expect(historyRail).toBeFocused();

  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(historyRail).toBeEnabled();
    await historyRail.click();
    const history = page.getByRole("region", { name: "问题历史" });
    await expect(history).toContainText("Root unknown");
    await expect(history).toContainText("receipt-tree-root");
    await expect(historyRail).toHaveClass(/active/);
    const route = new URL(page.url());
    expect(route.searchParams.get("view")).toBe("questions");
    expect(route.searchParams.get("node")).toBe("question_tree_root");
    expect(route.searchParams.get("inspector")).toBe("history");
    await page.getByRole("button", { name: "查看证据与来源" }).click();
    await expect(page.getByRole("region", { name: "问题证据与来源" })).toContainText(
      "question_evidence_binding_absent",
    );
    await expect(historyRail).not.toHaveClass(/active/);
    expect(new URL(page.url()).searchParams.get("inspector")).toBe("evidence");
    await page.getByRole("button", { name: "问题历史 ↗" }).click();
    await expect(page.getByRole("region", { name: "问题历史" })).toContainText(
      "Root unknown",
    );
    await expect(historyRail).toHaveClass(/active/);
    expect(new URL(page.url()).searchParams.get("inspector")).toBe("history");
    const geometry = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth,
      viewport: window.innerWidth,
    }));
    expect(geometry.scroll).toBeLessThanOrEqual(geometry.viewport);
    await page.getByRole("button", { name: "回到总览" }).click();
    await expect(historyRail).toBeFocused();
  }
  expect(unsafeRequests).toEqual([]);
  expect(questionReads).toEqual([
    "question_tree_root:history", "question_tree_root:history",
    "question_tree_root:history", "question_tree_root:evidence", "question_tree_root:history",
    "question_tree_root:history", "question_tree_root:evidence", "question_tree_root:history",
    "question_tree_root:history", "question_tree_root:evidence", "question_tree_root:history",
  ]);
});

test("Question history follows bounded read pages without repeating the first query", async ({
  page,
}) => {
  const tree = await openQuestionTreeFixture(page);
  await tree.locator('[data-question-ref="question_tree_branch"]').click();
  const offsets: number[] = [];
  await page.route("**/api/v1/questions/*/history*", (route) => {
    const url = new URL(route.request().url());
    const offset = Number(url.searchParams.get("offset") ?? 0);
    offsets.push(offset);
    const events = [{
      action: "accepted",
      question_ref: "question_tree_branch",
      record_ref: "question_tree_branch",
      prune_record_ref: null,
      restore_record_ref: null,
      lifecycle_revision: 1,
      receipt_ref: "receipt-tree-branch",
      status: "active",
      base_graph_version: null,
      committed_graph_version: null,
      recorded_at: null,
    }, {
      action: "prune",
      question_ref: "question_tree_formal",
      record_ref: "prune-record-tree-page-2",
      prune_record_ref: "prune-record-tree-page-2",
      restore_record_ref: null,
      lifecycle_revision: 2,
      receipt_ref: "rg-prune-receipt-tree-page-2",
      status: "pruned",
      base_graph_version: 3,
      committed_graph_version: 4,
      recorded_at: 1_720_000_150,
    }, {
      action: "restore",
      question_ref: "question_tree_formal",
      record_ref: "restore-record-tree-page-3",
      prune_record_ref: "prune-record-tree-page-2",
      restore_record_ref: "restore-record-tree-page-3",
      lifecycle_revision: 3,
      receipt_ref: "rg-restore-receipt-tree-page-3",
      status: "active",
      base_graph_version: 4,
      committed_graph_version: 5,
      recorded_at: 1_720_000_200,
    }];
    const event = events[offset];
    return fulfill(route, {
      status: "ready",
      question_ref: "question_tree_branch",
      question: {
        question_ref: "question_tree_branch",
        quest_ref: "quest_tree_1",
        parent_question_ref: "question_tree_formal",
        initialization_id: "quest-init-tree-1",
        context_ref: "manual-context-tree-1",
        content: {
          content_ref: "content-tree-branch",
          content_hash: "3".repeat(64),
          schema_ref: "question/v1",
          document: {
            title: "Branch question",
            unknown_statement: "Branch unknown",
            answer_shape: "Auditable answer",
            applicability_scope: "Tree fixture",
            background_context: "Accepted context",
            requirements_constraints: "Read only",
          },
        },
        receipts: {
          content_acceptance: { receipt_ref: "rm-content-receipt-tree-1" },
          question_acceptance: { receipt_ref: "receipt-tree-branch" },
          confirmation_ref: "hc-question-confirmation-tree-1",
          confirmation_hash: "8".repeat(64),
        },
      },
      lifecycle: {
        question_ref: "question_tree_branch",
        quest_ref: "quest_tree_1",
        status: "active",
        revision: 3,
        owner_revision: 17,
        updated_at: 1_720_000_200,
      },
      events: event ? [{
        ...event,
        affected_question_refs: ["question_tree_branch"],
        receipt_hash: "b".repeat(64),
      }] : [],
      offset,
      limit: 1,
      total_count: 3,
      has_more: offset + 1 < events.length,
      reason: null,
    });
  });
  await tree.getByRole("button", { name: "问题历史 ↗" }).click();
  const history = tree.getByRole("region", { name: "问题历史" });
  const more = history.getByRole("button", { name: /继续读取历史/ });
  await expect(more).toHaveText("继续读取历史 · 1/3");
  await more.click();
  await expect(history).toContainText("prune-record-tree-page-2");
  await expect(more).toHaveText("继续读取历史 · 2/3");
  await more.click();
  await expect(history).toContainText("restore-record-tree-page-3");
  await expect(more).toBeHidden();
  expect(offsets).toEqual([0, 1, 2]);
});
