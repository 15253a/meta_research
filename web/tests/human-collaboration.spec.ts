import {
  expect,
  test,
  type Download,
  type Page,
  type Response,
  type Route,
} from "@playwright/test";
import { readFile } from "node:fs/promises";
import { DeterministicProduct } from "./support/deterministic-product";
import { HANDOFF_DOCUMENT_PROMPT } from "../src/humanRequestHandoff";

type JsonRecord = Record<string, unknown>;

let product: DeterministicProduct | null = null;

test.afterEach(async ({ page }, testInfo) => {
  await page.close().catch(() => undefined);
  if (!product) return;
  const running = product;
  product = null;
  if (testInfo.status !== testInfo.expectedStatus) {
    console.error("preserving failed Human Collaboration E2E DataRoot");
    return;
  }
  await running.stop();
});

const request = (
  kind: string,
  requestRef: string,
  issuer: string,
  obligation: string,
  waiterRef: string,
): JsonRecord => ({
  request_ref: requestRef,
  request_id: requestRef.split(":")[1],
  revision: 1,
  issuer,
  quest_ref: "quest_chrome_1",
  kind,
  status: "open",
  obligation,
  business_purpose: `Resume only ${waiterRef}.`,
  target_assertion: {
    waiter_ref: waiterRef,
    generation: 3,
    ...(kind === "library_reconnect"
      ? {
          acquisition_paper_id: "paper-ACQ-17",
          item_hash: "8".repeat(64),
        }
      : {}),
    ...(kind === "offline_action"
      ? { protocol_material_ref: "research_asset_version-protocol-fsr-04" }
      : {}),
  },
  acceptance_conditions: ["The exact blocked dependency is current and verified."],
  required_authorization: kind === "capability_authorization"
    ? {
        capability: "web_fetch",
        scope: {
          schema_ref: "meta-research/root-human-request-capability-scope/v1",
          human_request_effect_id: "permission-effect-63",
          quest_ref: "quest_chrome_1",
          task_ref: "RUN-219",
          root_session_ref: "root-session-219",
          operation_id: "human_request.open",
          attempt_ref: "attempt-219",
          generation: 3,
          destination: "api.vendor-example.org:443",
          duration: "RUN-219 generation 3",
          exclusions: ["local files", "secrets"],
        },
      }
    : null,
  direct_waiters: [{
    waiter_ref: waiterRef,
    generation: 3,
    wait_scope: "local",
    status: "blocked",
    other_blockers: [],
  }],
  responses: [],
  evaluation: null,
  disposition: null,
});

async function installHumanCollaborationSnapshot(
  page: Page,
  options: { questBlock?: boolean } = {},
) {
  product = await DeterministicProduct.start();
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const base = await response.json() as JsonRecord;
  const items = [
    request(
      "library_reconnect",
      "agent_runtime:HR-27:r1",
      "agent_runtime",
      "恢复当前图书馆会话，或选择合法全文替代路线。",
      "ACQ-17",
    ),
    request(
      "external_material_api_access",
      "research_memory:HR-41:r1",
      "research_memory",
      "本人完成外部材料申请并交回批准结果。",
      "DatasetBinding-11",
    ),
    request(
      "offline_action",
      "research_graph:HR-52:r1",
      "research_graph",
      "按已接纳协议完成线下校准并交回原始结果。",
      "ExperimentBrief-12",
    ),
    request(
      "capability_authorization",
      "agent_runtime:HR-63:r1",
      "agent_runtime",
      "决定是否允许当前 Run 访问一个新增外部目的地。",
      "RUN-219",
    ),
    request(
      "system_operation_help",
      "writing:HR-70:r1",
      "writing",
      "重试失败的 Writing 操作：https://status.example.invalid/run-219",
      "writing-run-219",
    ),
  ];
  items[1].business_purpose = "交回原始批准结果：https://vendor.example.invalid/approval";
  items[1].target_assertion = {
    ...(items[1].target_assertion as JsonRecord),
    condition: { guidance_asset_ref: "research_asset_version-guidance-41" },
  };
  items[2].business_purpose = "交回原始校准结果；不要代写摘要。";
  items[4].business_purpose = "只重试上述同一操作，不展开根因分析。";
  if (options.questBlock) {
    (items[2].direct_waiters as JsonRecord[])[0] = {
      ...(items[2].direct_waiters as JsonRecord[])[0],
      wait_scope: "quest",
    };
  }
  const snapshot = {
    ...base,
    human_collaboration: {
      companion: {
        status: "ready",
        scope_ref: "quest_chrome_1",
        messages: [
          {
            message_ref: "msg-1",
            scope_ref: "quest_chrome_1",
            role: "assistant",
            content: "DF-09 只有一篇全文等待图书馆访问，其他工作仍可继续。",
            status: "completed",
          },
          {
            message_ref: "msg-2",
            scope_ref: "quest_chrome_1",
            role: "user",
            content: "这会冻结整个 Quest 吗？",
            status: "completed",
          },
          {
            message_ref: "msg-request-only",
            scope_ref: "quest_chrome_1",
            role: "assistant",
            content: "REQUEST-ONLY transcript marker",
            status: "completed",
            view_context: {
              kind: "human_request",
              quest_ref: "quest_chrome_1",
              request_ref: "research_memory:HR-41:r1",
              revision: 1,
            },
          },
        ],
        soft_constraints: [{
          constraint_ref: "constraint-1",
          scope_ref: "quest_chrome_1",
          revision: 1,
          guidance: { text: "优先使用可公开复核的材料。" },
          status: "active",
        }, {
          constraint_ref: "constraint-request-only",
          scope_ref: "research_memory:HR-41:r1",
          revision: 1,
          guidance: { text: "REQUEST-ONLY constraint marker" },
          status: "active",
        }],
        agent_proposals: [{
          proposal_ref: "proposal-1",
          scope_ref: "quest_chrome_1",
          proposal_hash: "1".repeat(64),
          title: "建议复核证据边界",
          summary: "接受建议最多形成草案，不直接改变研究。",
          proposal: {
            proposal_kind: "narrow_scope",
            text: "首轮只使用公开文献。",
          },
          status: "proposed",
        }, {
          proposal_ref: "proposal-command-1",
          scope_ref: "quest_chrome_1",
          proposal_hash: "2".repeat(64),
          proposal: {
            proposal_kind: "command_draft",
            title: "建议建立精确归档授权草案",
            summary: "只有明确建立后才形成 Command Draft。",
            command: {
              command_kind: "capability_authorization",
              payload: {
                capability: "archive_export",
                decision: "granted",
                scope: { destination: "archive.example.invalid" },
              },
            },
          },
          status: "proposed",
        }, {
          proposal_ref: "proposal-request-only",
          scope_ref: "research_memory:HR-41:r1",
          proposal_hash: "3".repeat(64),
          proposal: {
            proposal_kind: "narrow_scope",
            text: "REQUEST-ONLY proposal marker",
          },
          status: "proposed",
        }],
      },
      human_requests: {
        status: "ready",
        waiting: {
          scope: options.questBlock ? "quest" : "local",
          safe_meaningful_runnable_exists: !options.questBlock,
          other_blockers: [],
        },
        items,
      },
      commands: {
        status: "ready",
        authorizations: [],
        items: [{
          intent_id: "intent-1",
          scope_ref: "quest_chrome_1",
          status: "draft",
          draft_revision: 1,
          draft_hash: "a".repeat(64),
          draft: {
            command_kind: "capability_authorization",
            payload: {
              capability: "external_publish",
              decision: "granted",
              scope: {
                destination: "https://example.invalid/publication",
                asset_ref: "asset_publication_1",
              },
            },
          },
          executed: false,
          impact_preview: null,
          confirmation_receipt: null,
        }, {
          intent_id: "intent-request-only",
          scope_ref: "research_memory:HR-41:r1",
          status: "draft",
          draft_revision: 1,
          draft_hash: "4".repeat(64),
          draft: {
            command_kind: "capability_authorization",
            payload: {
              capability: "request_only_capability",
              decision: "denied",
              scope: { marker: "REQUEST-ONLY command marker" },
            },
          },
          executed: false,
          impact_preview: null,
          confirmation_receipt: null,
        }],
      },
    },
    unavailable: Array.isArray(base.unavailable)
      ? (base.unavailable as JsonRecord[]).filter(
          (item) => item.capability !== "quest_companion",
        )
      : [],
  };
  await page.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(snapshot),
    });
  });
  await page.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
  return snapshot;
}

async function keepProjectionRoutesAcrossPages(
  page: Page,
  snapshot: JsonRecord,
): Promise<void> {
  const context = page.context();
  await context.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(snapshot),
    });
  });
  await context.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
}

async function installWorkspaceRepairSnapshot(page: Page) {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const humanRequests = (snapshot.human_collaboration as JsonRecord)
    .human_requests as JsonRecord;
  const items = ["双手接口核验", "摄像头采集", "会话代码", "测量协议"].map(
    (work, index) => {
      const item = request(
        "system_operation_help",
        `agent_runtime:HR-workspace-${index + 1}:r1`,
        "agent_runtime",
        `恢复${work} Target 的本地工作区读写`,
        `root_run:target_run_${index + 1}`,
      );
      item.business_purpose = `${work}需读取冻结 manifest 并保存 implementation/ 和 outputs/result.json。`
        + "根会话与独立子会话的只读命令均在启动前返回 bwrap: Failed to make / slave: Permission denied。"
        + "apply_patch 写入无害探针也返回 Failed to write file。请修复当前执行容器，并保持原 Target、fence 与工作区。";
      item.target_assertion = {
        condition: {
          safe_response: "请由系统维护者修复当前 Target 的命令执行与文件读写，确认冻结 manifest 可读，工作区可写。无需授予新的外部权限。",
          impact: "本 Target 的实现和结果文件无法验证或提交；恢复后才能继续同一研究工作。",
        },
        root: {
          target_ref: `target_${index + 1}`,
          fence_ref: `harness_fence_${"8".repeat(96)}_${index + 1}`,
          run_ref: `target_run_${index + 1}`,
          root_session_ref: `harness_session_${index + 1}`,
          root_kind: "target",
          phase: "target_root_lifecycle",
          operation_id: "human_request.open",
          waiter_generation: 3,
        },
      };
      item.acceptance_conditions = [
        "当前 Target 的 exec_command 能成功执行只读 pwd 并读取冻结 manifest。",
        "当前 Target 能创建并读回无害测试文件，再写入 implementation/ 和 outputs/。",
        "保留原 Target、fence 与输入归属。",
      ];
      return item;
    },
  );
  items.push(request(
    "system_operation_help",
    "writing:HR-independent:r1",
    "writing",
    "重新生成排版预览。",
    "writing-preview-independent",
  ));
  humanRequests.items = items;
  return { snapshot, items };
}

async function fulfillJson(route: Route, body: object) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function acceptedAssetIntake(body: JsonRecord, suffix: string): JsonRecord {
  return {
    job_ref: `asset-intake-${suffix}`,
    status: "accepted",
    source_kind: body.source_kind,
    custody_mode: body.custody_mode,
    attempt_count: 1,
    failure: null,
    asset: {
      asset_ref: `research_asset-${suffix}`,
      version_ref: `research_asset_version-${suffix}`,
      memory_ref: `research_asset_version-${suffix}`,
      version_number: 1,
      source_kind: body.source_kind,
      display_name: body.display_name,
      media_type: body.media_type,
      content_hash: "5".repeat(64),
      manifest_hash: "6".repeat(64),
      byte_count: 8,
      provenance: body.provenance,
      custody_modes: [body.custody_mode],
      integrity: "verified",
      availability: "available",
      verification_observed_at: 1720000000,
      verification_pending: false,
      accepted_at: 1720000000,
      receipt: {
        issuer: "research_memory",
        kind: "asset_accepted",
        receipt_ref: `asset-receipt-${suffix}`,
        subject_ref: `research_asset_version-${suffix}`,
        payload_hash: "7".repeat(64),
      },
    },
  };
}

async function humanRequestRecoveryDatabaseState(page: Page): Promise<{
  keyCount: number;
  ciphertextCount: number;
  manifests: string[];
}> {
  return page.evaluate(async () => new Promise((resolve, reject) => {
    const open = indexedDB.open("meta_research_human_request_recovery");
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const database = open.result;
      const transaction = database.transaction(
        ["sealed_response_keys", "sealed_payloads", "recovery_manifests"],
        "readonly",
      );
      const keyCount = transaction.objectStore("sealed_response_keys").count();
      const ciphertextCount = transaction.objectStore("sealed_payloads").count();
      const manifests = transaction.objectStore("recovery_manifests").getAll();
      transaction.onerror = () => reject(transaction.error);
      transaction.oncomplete = () => {
        database.close();
        resolve({
          keyCount: keyCount.result,
          ciphertextCount: ciphertextCount.result,
          manifests: manifests.result as string[],
        });
      };
    };
  }));
}

test("the Quest Companion shows the user's message before its reply is ready", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const collaboration = snapshot.human_collaboration as JsonRecord;
  const humanRequests = collaboration.human_requests as JsonRecord;
  humanRequests.items = [];
  const companionProjection = collaboration.companion as JsonRecord;
  const projectedMessages = companionProjection.messages as JsonRecord[];
  let posted: JsonRecord | null = null;
  let releaseResponse!: () => void;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let markResponseFulfilled!: () => void;
  const responseFulfilled = new Promise<void>((resolve) => {
    markResponseFulfilled = resolve;
  });
  await page.route("**/api/v1/companion/messages", async (route) => {
    posted = route.request().postDataJSON() as JsonRecord;
    await responseGate;
    projectedMessages.push(
      {
        message_ref: "interaction-optimistic:user",
        scope_ref: "quest_chrome_1",
        role: "user",
        content: posted.message,
        status: "completed",
      },
      {
        message_ref: "interaction-optimistic:assistant",
        scope_ref: "quest_chrome_1",
        role: "assistant",
        content: "这是稍后返回的 Codex 回复。",
        status: "completed",
      },
    );
    (snapshot as JsonRecord).revision = Number((snapshot as JsonRecord).revision) + 1;
    await fulfillJson(route, {
      interaction_ref: "interaction-optimistic",
      status: "queued",
    });
    markResponseFulfilled();
  });
  await page.goto(product!.baseUrl, { waitUntil: "domcontentloaded" });

  const companion = page.getByRole("complementary", { name: "研究助手" });
  const message = "先立即显示我的这条消息。";
  const delayedReply = "这是稍后返回的 Codex 回复。";
  const replyBubble = companion.locator(".lumen-message:not(.me)").filter({
    hasText: delayedReply,
  });
  await companion.getByLabel("给研究助手发消息").fill(message);
  await companion.getByRole("button", { name: "发送消息" }).click();
  let snapshotRefresh: Promise<Response> | null = null;
  try {
    await expect.poll(() => posted).toEqual({
      scope_ref: "quest_chrome_1",
      message,
    });
    await expect(
      companion.locator(".lumen-message.me").filter({ hasText: message }),
    ).toBeInViewport({ timeout: 750 });
    await expect(companion.getByText("Codex 正在思考…", { exact: true }))
      .toBeVisible({ timeout: 750 });
    await expect(replyBubble).toHaveCount(0);
  } finally {
    snapshotRefresh = page.waitForResponse((response) =>
      new URL(response.url()).pathname === "/api/v1/snapshot"
    );
    releaseResponse();
  }
  await responseFulfilled;
  await snapshotRefresh;
  await expect(replyBubble).toBeInViewport();
  await expect(
    companion.locator(".lumen-message.me").filter({ hasText: message }),
  ).toHaveCount(1);
});

test("the persistent Quest Companion sends ordinary conversation without command authority", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const humanRequestsProjection = (snapshot.human_collaboration as JsonRecord)
    .human_requests as JsonRecord;
  humanRequestsProjection.items = [];
  let posted: JsonRecord | null = null;
  const explicitWrites: Array<{ path: string; body: JsonRecord }> = [];
  await page.route("**/api/v1/companion/messages", async (route) => {
    posted = route.request().postDataJSON() as JsonRecord;
    const messages = companionProjection.messages as JsonRecord[];
    messages.push(
      {
        message_ref: "msg-web-user",
        scope_ref: "quest_chrome_1",
        role: "user",
        content: posted.message,
        status: "completed",
      },
      {
        message_ref: "msg-web-assistant",
        scope_ref: "quest_chrome_1",
        role: "assistant",
        content: "普通聊天不会被猜成硬命令。",
        status: "completed",
      },
    );
    await fulfillJson(route, (snapshot.human_collaboration as JsonRecord).companion as object);
  });
  const companionProjection = (snapshot.human_collaboration as JsonRecord)
    .companion as JsonRecord;
  await page.route("**/api/v1/human-collaboration/agent-proposals/proposal-1/soft-constraint", async (route) => {
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    const proposal = (companionProjection.agent_proposals as JsonRecord[]).find(
      (item) => item.proposal_ref === "proposal-1",
    )!;
    proposal.status = "converted";
    const constraint = {
      constraint_ref: "constraint-2",
      scope_ref: "quest_chrome_1",
      source_proposal_ref: "proposal-1",
      revision: 1,
      guidance: proposal.proposal,
      status: "active",
    };
    (companionProjection.soft_constraints as JsonRecord[]).push(constraint);
    await fulfillJson(route, { proposal, soft_constraint: constraint });
  });
  await page.route("**/api/v1/human-collaboration/soft-constraints/*/withdrawals", async (route) => {
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    await fulfillJson(route, { constraint_ref: "constraint-1", status: "withdrawn" });
  });
  const commandsProjection = (snapshot.human_collaboration as JsonRecord)
    .commands as JsonRecord;
  const command = (commandsProjection.items as JsonRecord[])[0];
  await page.route("**/api/v1/human-collaboration/agent-proposals/proposal-command-1/command-draft", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body,
    });
    const proposal = (companionProjection.agent_proposals as JsonRecord[]).find(
      (item) => item.proposal_ref === "proposal-command-1",
    )!;
    proposal.status = "converted";
    const created = {
      intent_id: "intent-2",
      scope_ref: "quest_chrome_1",
      source_proposal_ref: "proposal-command-1",
      status: "draft",
      draft_revision: 1,
      draft_hash: "f".repeat(64),
      draft: (proposal.proposal as JsonRecord).command,
      executed: false,
      impact_preview: null,
      confirmation_receipt: null,
    };
    (commandsProjection.items as JsonRecord[]).push(created);
    await fulfillJson(route, { proposal, command_draft: created });
  });
  await page.route("**/api/v1/human-collaboration/commands/*/revisions", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body,
    });
    const created = (commandsProjection.items as JsonRecord[]).find(
      (item) => item.intent_id === "intent-2",
    )!;
    created.draft_revision = 2;
    created.draft_hash = "0".repeat(64);
    created.draft = body.command;
    created.impact_preview = null;
    await fulfillJson(route, created);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/previews", async (route) => {
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    command.status = "previewed";
    command.impact_preview = {
      preview_ref: "preview-1",
      preview_hash: "b".repeat(64),
      draft_revision: 1,
      draft_hash: "a".repeat(64),
      owner_previews: [{
        source_owner: "human_collaboration",
        target_assertion: {
          operation: "decide_capability_authorization",
          capability: "external_publish",
        },
        will_happen: ["record an independent grant decision"],
        will_not_happen: ["confirmation alone will not grant the capability"],
        risks: ["the exact destination may become reachable"],
        stale_conditions: ["the command draft changes"],
        digest: "c".repeat(64),
      }],
      owner_revisions: { human_collaboration: 0 },
      status: "current",
    };
    await fulfillJson(route, command);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/confirmations", async (route) => {
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    command.status = "confirmed";
    (command.impact_preview as JsonRecord).status = "consumed";
    command.confirmation_receipt = {
      status: "accepted",
      issuer: "human_collaboration",
      kind: "human_confirmation",
      receipt_ref: "confirmation-1",
      subject_ref: "intent-1",
      payload_hash: "d".repeat(64),
    };
    await fulfillJson(route, command);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/authorizations", async (route) => {
    explicitWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    const authorization = {
      authorization_ref: "authorization-1",
      scope_ref: "quest_chrome_1",
      authorization_kind: "capability",
      capability: "external_publish",
      decision: "granted",
      status: "granted",
      requirement: {
        capability: "external_publish",
        scope: {
          destination: "https://example.invalid/publication",
          asset_ref: "asset_publication_1",
        },
      },
      policy: {},
      confirmation_receipt_ref: "confirmation-1",
      quest_ref: null,
      receipt_ref: "authorization-receipt-1",
      receipt: {
        issuer: "human_collaboration",
        kind: "capability_authorization",
        receipt_ref: "authorization-receipt-1",
        subject_ref: "authorization-1",
        payload_hash: "e".repeat(64),
      },
      created_at: 1720000000,
    };
    commandsProjection.authorizations = [authorization];
    await fulfillJson(route, authorization);
  });
  await page.goto(product!.baseUrl, { waitUntil: "domcontentloaded" });

  const companion = page.getByRole("complementary", { name: "研究助手" });
  await expect(companion).toBeVisible();
  expect(posted).toBeNull();
  expect(explicitWrites).toEqual([]);
  await expect(companion).toContainText("DF-09 只有一篇全文等待图书馆访问");
  await expect(companion).toContainText("YOU · CONVERSATION");
  await expect(companion).toContainText("SOFT CONSTRAINT · ACTIVE");
  await expect(companion).toContainText("建议复核证据边界");
  await expect(companion).toContainText("接受建议最多形成草案，不直接改变研究");
  await expect(companion).toContainText("建议建立精确归档授权草案");
  await expect(companion).not.toContainText("capability_unavailable");
  await expect(companion).not.toContainText("REQUEST-ONLY");

  await companion.getByRole("button", { name: "建立精确 Command Draft" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/agent-proposals/proposal-command-1/command-draft",
    body: {
      expected_scope_ref: "quest_chrome_1",
      expected_proposal_hash: "2".repeat(64),
    },
  });
  const createdCommand = companion.locator(".lumen-command").filter({ hasText: "archive_export" });
  await expect(createdCommand).toContainText("source · proposal-command-1");
  await createdCommand.getByText("修订精确 Command Draft", { exact: true }).click();
  await createdCommand.getByLabel("Command intent-2 exact scope").fill(JSON.stringify({
    destination: "archive.example.invalid",
    collection_ref: "collection-2",
  }));
  await createdCommand.getByRole("button", { name: "保存修订并使旧 Preview 失效" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands/intent-2/revisions",
    body: {
      expected_revision: 1,
      command: {
        command_kind: "capability_authorization",
        payload: {
          capability: "archive_export",
          decision: "granted",
          scope: {
            destination: "archive.example.invalid",
            collection_ref: "collection-2",
          },
        },
      },
    },
  });

  await companion.getByRole("button", { name: "明确接受为软约束" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/agent-proposals/proposal-1/soft-constraint",
    body: {
      expected_scope_ref: "quest_chrome_1",
      expected_proposal_hash: "1".repeat(64),
    },
  });
  await expect(companion.locator(".lumen-constraint").filter({ hasText: "首轮只使用公开文献" }))
    .toContainText("source · proposal-1");
  await companion.locator(".lumen-constraint").filter({ hasText: "优先使用可公开复核的材料" })
    .getByRole("button", { name: "撤回软约束" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/soft-constraints/constraint-1/withdrawals",
    body: { expected_revision: 1 },
  });

  const existingCommand = companion.locator(".lumen-command").filter({ hasText: "external_publish" });
  await existingCommand.getByRole("button", { name: "生成 Owner Impact Preview" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands/intent-1/previews",
    body: { draft_revision: 1, draft_hash: "a".repeat(64) },
  });
  await expect(companion).toContainText("confirmation alone will not grant the capability");
  await existingCommand.getByText("查看精确草案与 Owner Impact Preview", { exact: true }).click();
  await existingCommand.getByRole("button", { name: "确认当前草案与预览" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands/intent-1/confirmations",
    body: {
      draft_revision: 1,
      draft_hash: "a".repeat(64),
      preview_ref: "preview-1",
      preview_hash: "b".repeat(64),
    },
  });
  await expect(companion).toContainText("确认没有执行命令，也没有签发 Capability Authorization");
  await existingCommand.getByRole("button", { name: "签发独立 Capability Authorization" }).click();
  await expect.poll(() => explicitWrites.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands/intent-1/authorizations",
    body: {
      capability: "external_publish",
      decision: "granted",
      scope: {
        destination: "https://example.invalid/publication",
        asset_ref: "asset_publication_1",
      },
      confirmation_receipt_ref: "confirmation-1",
    },
  });
  await expect(companion).toContainText("Capability Authorization · granted");

  await companion.getByLabel("给研究助手发消息").fill("为什么这里只是局部等待？");
  await companion.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => posted).toEqual({
    scope_ref: "quest_chrome_1",
    message: "为什么这里只是局部等待？",
  });
  await expect(companion).toContainText("普通聊天不会被猜成硬命令");
});

test("a prefixed Quest scope exposes the current broad grant and revokes only through the full command ladder", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const collaboration = snapshot.human_collaboration as JsonRecord;
  const companionProjection = collaboration.companion as JsonRecord;
  companionProjection.scope_ref = "quest:quest_chrome_1";
  const commandsProjection = collaboration.commands as JsonRecord;
  const issuedGrant: JsonRecord = {
    authorization_ref: "authorization-broad-grant",
    scope_ref: "quest:quest_chrome_1",
    authorization_kind: "broad_research",
    capability: "broad_research",
    decision: "granted",
    status: "granted",
    requirement: { capability: "broad_research", scope: { quest_ref: "quest_chrome_1" } },
    policy: { ordinary_reversible_local_research: "allowed_without_additional_confirmation" },
    confirmation_receipt_ref: "quest-confirmation-receipt",
    quest_ref: "quest_chrome_1",
    receipt_ref: "broad-grant-receipt",
    receipt: {
      issuer: "human_collaboration",
      kind: "broad_research_authorization",
      receipt_ref: "broad-grant-receipt",
      subject_ref: "authorization-broad-grant",
      payload_hash: "8".repeat(64),
    },
    created_at: 1720000000,
    is_current: true,
    effective_decision: "granted",
  };
  (commandsProjection.authorizations as JsonRecord[]).push(issuedGrant);
  const writes: Array<{ path: string; body: JsonRecord }> = [];
  let revokeCommand: JsonRecord | null = null;

  await page.route("**/api/v1/human-collaboration/commands", async (route) => {
    writes.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    revokeCommand = {
      intent_id: "intent-broad-revoke",
      scope_ref: "quest:quest_chrome_1",
      status: "draft",
      draft_revision: 1,
      draft_hash: "9".repeat(64),
      draft: (writes.at(-1)!.body.command as JsonRecord),
      executed: false,
      impact_preview: null,
      confirmation_receipt: null,
    };
    (commandsProjection.items as JsonRecord[]).push(revokeCommand);
    await fulfillJson(route, revokeCommand);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-broad-revoke/previews", async (route) => {
    writes.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    revokeCommand!.status = "previewed";
    revokeCommand!.impact_preview = {
      preview_ref: "preview-broad-revoke",
      preview_hash: "a".repeat(64),
      draft_revision: 1,
      draft_hash: "9".repeat(64),
      owner_previews: [{
        source_owner: "human_collaboration",
        target_assertion: {
          operation: "decide_capability_authorization",
          capability: "broad_research",
          decision: "revoked",
          scope: { quest_ref: "quest_chrome_1" },
        },
        will_happen: ["record an independent broad-research revoke decision"],
        will_not_happen: ["preview or confirmation alone will not revoke the grant"],
        risks: ["ordinary research admission will close"],
        stale_conditions: ["the exact draft or current authorization changes"],
        digest: "b".repeat(64),
      }],
      owner_revisions: { human_collaboration: 1 },
      status: "current",
    };
    await fulfillJson(route, revokeCommand!);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-broad-revoke/confirmations", async (route) => {
    writes.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    revokeCommand!.status = "confirmed";
    (revokeCommand!.impact_preview as JsonRecord).status = "consumed";
    revokeCommand!.confirmation_receipt = {
      status: "accepted",
      issuer: "human_collaboration",
      kind: "human_confirmation",
      receipt_ref: "confirmation-broad-revoke",
      subject_ref: "intent-broad-revoke",
      payload_hash: "c".repeat(64),
    };
    await fulfillJson(route, revokeCommand!);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-broad-revoke/authorizations", async (route) => {
    writes.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    issuedGrant.effective_decision = "revoked";
    issuedGrant.effective_authorization = {
      authorization_ref: "authorization-broad-revoke",
      receipt_ref: "broad-revoke-receipt",
      decision: "revoked",
    };
    const revoked = {
      authorization_ref: "authorization-broad-revoke",
      scope_ref: "quest:quest_chrome_1",
      authorization_kind: "capability",
      capability: "broad_research",
      decision: "revoked",
      status: "revoked",
      requirement: { capability: "broad_research", scope: { quest_ref: "quest_chrome_1" } },
      policy: {},
      confirmation_receipt_ref: "confirmation-broad-revoke",
      quest_ref: "quest_chrome_1",
      is_current: true,
      effective_decision: "revoked",
      created_at: 1720000001,
      receipt_ref: "broad-revoke-receipt",
      receipt: {
        issuer: "human_collaboration",
        kind: "capability_authorization",
        receipt_ref: "broad-revoke-receipt",
        subject_ref: "authorization-broad-revoke",
        payload_hash: "d".repeat(64),
      },
    };
    (commandsProjection.authorizations as JsonRecord[]).push(revoked);
    await fulfillJson(route, revoked);
  });

  await page.goto(product!.baseUrl, { waitUntil: "domcontentloaded" });
  const companion = page.getByRole("complementary", { name: "研究助手" });
  await expect(companion).toContainText("需要你 · 只影响相关任务");
  await expect(companion).toContainText("BROAD RESEARCH AUTHORIZATION · CURRENT GRANT");
  await page.getByRole("dialog", { name: "需要你处理的事项" })
    .getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await companion.getByRole("button", { name: "建立 revoke Command Draft" }).click();
  await expect.poll(() => writes.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands",
    body: {
      scope_ref: "quest:quest_chrome_1",
      command: {
        command_kind: "capability_authorization",
        payload: {
          capability: "broad_research",
          decision: "revoked",
          scope: { quest_ref: "quest_chrome_1" },
        },
      },
    },
  });
  const commandCard = companion.locator(".lumen-command").filter({
    hasText: "revoked · broad_research",
  });
  await commandCard.getByRole("button", { name: "生成 Owner Impact Preview" }).click();
  await commandCard.getByText("查看精确草案与 Owner Impact Preview", { exact: true }).click();
  await commandCard.getByRole("button", { name: "确认当前草案与预览" }).click();
  await commandCard.getByRole("button", { name: "签发独立 Capability Authorization" }).click();
  await expect.poll(() => writes.at(-1)).toEqual({
    path: "/api/v1/human-collaboration/commands/intent-broad-revoke/authorizations",
    body: {
      capability: "broad_research",
      decision: "revoked",
      scope: { quest_ref: "quest_chrome_1" },
      confirmation_receipt_ref: "confirmation-broad-revoke",
    },
  });
  await expect(commandCard).toContainText("Capability Authorization · revoked");
  await expect(companion).not.toContainText("BROAD RESEARCH AUTHORIZATION · CURRENT GRANT");
});

test("the HumanRequest surface keeps five raw Agent requests and their shortest response paths", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const snapshot = await installHumanCollaborationSnapshot(page);
  const posts: Array<{ path: string; body: JsonRecord }> = [];
  const permissionWrites: Array<{ path: string; body: JsonRecord }> = [];
  const assetIntakes: JsonRecord[] = [];
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    assetIntakes.push(body);
    const sequence = assetIntakes.length;
    await fulfillJson(route, {
      job_ref: `asset-intake-${sequence}`,
      status: "accepted",
      source_kind: body.source_kind,
      custody_mode: body.custody_mode,
      attempt_count: 1,
      failure: null,
      asset: {
        asset_ref: `research_asset-${sequence}`,
        version_ref: `research_asset_version-${sequence}`,
        memory_ref: `research_asset_version-${sequence}`,
        version_number: 1,
        source_kind: body.source_kind,
        display_name: body.display_name,
        media_type: body.media_type,
        content_hash: "5".repeat(64),
        manifest_hash: "6".repeat(64),
        byte_count: 8,
        provenance: body.provenance,
        custody_modes: [body.custody_mode],
        integrity: "verified",
        availability: "available",
        verification_observed_at: 1720000000,
        verification_pending: false,
        accepted_at: 1720000000,
        receipt: {
          issuer: "research_memory",
          kind: "asset_accepted",
          receipt_ref: `asset-receipt-${sequence}`,
          subject_ref: `research_asset_version-${sequence}`,
          payload_hash: "7".repeat(64),
        },
      },
    });
  });
  await page.route("**/api/v1/human-requests/*/responses", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as JsonRecord;
    posts.push({
      path,
      body,
    });
    await fulfillJson(route, { response_ref: `response-${posts.length}`, status: "recorded" });
  });
  await page.route("**/api/v1/human-requests/*/retry", async (route) => {
    const path = new URL(route.request().url()).pathname;
    posts.push({
      path,
      body: (route.request().postDataJSON() ?? {}) as JsonRecord,
    });
    const requestRef = decodeURIComponent(path.split("/").at(-2)!);
    const items = ((snapshot.human_collaboration as JsonRecord)
      .human_requests as JsonRecord).items as JsonRecord[];
    const item = items.find((candidate) => candidate.request_ref === requestRef)!;
    item.responses = [{
      response_ref: `response-retry-${item.revision}`,
      decision: "provided",
      facts: { action: "retry", effect_id: "writing-effect-70" },
      note: "",
    }];
    const retryStatus = requestRef === "writing:HR-70:r2" ? "succeeded" : "processing";
    await fulfillJson(route, {
      ...item,
      ...(retryStatus === "succeeded" ? {
        status: "satisfied",
        evaluation: { decision: "satisfied" },
        disposition: { decision: "satisfied" },
      } : {}),
      retry: { status: retryStatus },
    });
  });
  await page.route("**/api/v1/companion/messages", async (route) => {
    posts.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    await fulfillJson(route, { status: "queued" });
  });
  const collaboration = snapshot.human_collaboration as JsonRecord;
  const commands = collaboration.commands as JsonRecord;
  const permissionRequest = ((collaboration.human_requests as JsonRecord)
    .items as JsonRecord[]).find(
      (item) => item.kind === "capability_authorization",
    )!;
  const permissionRequirement = permissionRequest.required_authorization as JsonRecord;
  let permissionCommand: JsonRecord | null = null;
  await page.route("**/api/v1/human-collaboration/commands", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    permissionWrites.push({
      path: new URL(route.request().url()).pathname,
      body,
    });
    permissionCommand = {
      intent_id: "intent-permission-63",
      scope_ref: "human_request:agent_runtime:HR-63:r1",
      status: "draft",
      draft_revision: 1,
      draft_hash: "9".repeat(64),
      draft: body.command,
      executed: false,
      impact_preview: null,
      confirmation_receipt: null,
    };
    (commands.items as JsonRecord[]).push(permissionCommand);
    await fulfillJson(route, permissionCommand);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-permission-63/previews", async (route) => {
    permissionWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    permissionCommand!.status = "previewed";
    permissionCommand!.impact_preview = {
      preview_ref: "preview-permission-63",
      preview_hash: "a".repeat(64),
      draft_revision: 1,
      draft_hash: "9".repeat(64),
      owner_previews: [{
        source_owner: "human_collaboration",
        target_assertion: {
          operation: "decide_capability_authorization",
          capability: "web_fetch",
          decision: "granted",
        },
        will_happen: ["只记录当前任务的精确授权"],
        will_not_happen: ["确认本身不会执行外部访问"],
        risks: ["当前任务可以访问指定目的地"],
        stale_conditions: ["任务或授权范围发生变化"],
        digest: "b".repeat(64),
      }],
      owner_revisions: { human_collaboration: 0 },
      status: "current",
    };
    await fulfillJson(route, permissionCommand!);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-permission-63/confirmations", async (route) => {
    permissionWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    permissionCommand!.status = "confirmed";
    (permissionCommand!.impact_preview as JsonRecord).status = "consumed";
    permissionCommand!.confirmation_receipt = {
      status: "accepted",
      issuer: "human_collaboration",
      kind: "human_confirmation",
      receipt_ref: "confirmation-permission-63",
      subject_ref: "intent-permission-63",
      payload_hash: "c".repeat(64),
    };
    await fulfillJson(route, permissionCommand!);
  });
  await page.route("**/api/v1/human-collaboration/commands/intent-permission-63/authorizations", async (route) => {
    permissionWrites.push({
      path: new URL(route.request().url()).pathname,
      body: route.request().postDataJSON() as JsonRecord,
    });
    const authorization = {
      authorization_ref: "authorization-permission-63",
      scope_ref: "human_request:agent_runtime:HR-63:r1",
      authorization_kind: "capability",
      capability: "web_fetch",
      decision: "granted",
      status: "granted",
      requirement: permissionRequirement,
      policy: {},
      confirmation_receipt_ref: "confirmation-permission-63",
      quest_ref: "quest_chrome_1",
      receipt_ref: "authorization-receipt-permission-63",
      receipt: {
        issuer: "human_collaboration",
        kind: "capability_authorization",
        receipt_ref: "authorization-receipt-permission-63",
        subject_ref: "authorization-permission-63",
        payload_hash: "d".repeat(64),
      },
      created_at: 1720000000,
      is_current: true,
      effective_decision: "granted",
    };
    (commands.authorizations as JsonRecord[]).push(authorization);
    await fulfillJson(route, authorization);
  });
  await page.goto(`${product!.baseUrl}/?workspace`, { waitUntil: "domcontentloaded" });

  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog).toBeVisible();
  const queue = dialog.getByRole("navigation", { name: "当前待办队列" });
  const nextRequest = dialog.getByRole("button", { name: "查看下一个待办" });
  await expect(queue).toContainText("1 / 5");
  await expect(dialog.getByRole("heading", {
    name: "恢复当前图书馆会话，或选择合法全文替代路线。",
  })).toBeVisible();
  await expect(dialog).toContainText("Resume only ACQ-17.");
  await expect(dialog.getByRole("button", { name: "我已重连" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /跳过，之后只用 OA/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /手动上传该文献/ })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "提交这条想法" })).toBeVisible();
  await expect(dialog).not.toContainText("不要粘贴密码");
  await expect(dialog.getByText("Human Waiting Projection", { exact: true })).toHaveCount(0);
  await expect(dialog.getByText(
    "准备好后，在“事项与回应”中提交结果，作为研究继续的依据。",
    { exact: true },
  )).toBeVisible();
  const libraryDraft = dialog.getByLabel("就图书馆恢复事项发消息");
  await libraryDraft.fill("如果今天不处理会怎样？");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/companion/messages",
    body: {
      scope_ref: "quest_chrome_1",
      message: "如果今天不处理会怎样？",
      view_context: {
        kind: "human_request",
        quest_ref: "quest_chrome_1",
        request_ref: "agent_runtime:HR-27:r1",
        revision: 1,
      },
    },
  });
  await dialog.getByPlaceholder("例如：先暂停这篇；或者改为向作者索取全文。").fill(
    "今天先暂停，之后再处理。",
  );
  await dialog.getByRole("button", { name: "提交这条想法" }).click();
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses",
    body: {
      decision: "deferred",
      facts: {},
      note: "今天先暂停，之后再处理。",
    },
  });

  await nextRequest.click();
  await expect(queue).toContainText("2 / 5");
  await expect(dialog.getByRole("heading", {
    name: "本人完成外部材料申请并交回批准结果。",
  })).toBeVisible();
  await expect(dialog.getByRole("link", {
    name: "https://vendor.example.invalid/approval",
  })).toHaveAttribute("href", "https://vendor.example.invalid/approval");
  const guidance = dialog.getByRole("link", { name: "下载参考材料 ↓" });
  await expect(guidance).toHaveAttribute(
    "href",
    "/api/v1/research-assets/research_asset_version-guidance-41/content",
  );
  await expect(dialog.getByLabel("自然语言回应")).toBeVisible();
  await expect(dialog.getByLabel("回应文件")).toBeVisible();
  await expect(dialog.getByLabel("绝对本地文件或目录路径")).toBeVisible();
  await expect(dialog.getByRole("button", { name: "提交", exact: true })).toHaveCount(1);
  await expect(dialog).toContainText("REQUEST-ONLY transcript marker");
  await dialog.getByLabel("自然语言回应").fill("批准已返回；按原始结果处理。");
  await dialog.getByLabel("回应文件").setInputFiles({
    name: "approval.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("approved"),
  });
  await expect(dialog.getByLabel("绝对本地文件或目录路径")).toBeDisabled();
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => assetIntakes.at(-1)).toEqual({
    source_kind: "file",
    custody_mode: "managed",
    display_name: "approval.pdf",
    media_type: "application/pdf",
    content_base64: "YXBwcm92ZWQ=",
    provenance: {
      submitted_via: "human_request_response",
      human_request_ref: "research_memory:HR-41:r1",
      evidence_kind: "external_approval",
    },
    asynchronous: false,
  });
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/research_memory%3AHR-41%3Ar1/responses",
    body: {
      decision: "provided",
      facts: {
        material_source_ref: "research_asset_version-1",
        material_version_ref: "research_asset_version-1",
        material_content_hash: "5".repeat(64),
        material_manifest_hash: "6".repeat(64),
        material_acceptance_receipt_ref: "asset-receipt-1",
      },
      note: "批准已返回；按原始结果处理。",
    },
  });
  await expect(dialog.getByRole("heading", { name: "回应已提交" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "提交", exact: true })).toHaveCount(0);

  await nextRequest.click();
  await expect(queue).toContainText("3 / 5");
  await expect(dialog.getByRole("heading", {
    name: "按已确认协议完成线下校准并交回原始结果。",
  })).toBeVisible();
  await expect(dialog).toContainText("交回原始校准结果；不要代写摘要。");
  await expect(dialog.getByLabel("就线下操作事项发消息")).toBeVisible();
  await expect(dialog.getByRole("link", { name: "下载参考材料 ↓" })).toHaveCount(0);
  await dialog.getByLabel("自然语言回应").fill("校准完成，原始目录如下。");
  await dialog.getByLabel("绝对本地文件或目录路径").fill("/data/research/raw-calibration");
  await expect(dialog.getByLabel("回应文件")).toBeDisabled();
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/research_graph%3AHR-52%3Ar1/responses",
    body: {
      decision: "provided",
      facts: {},
      note: "校准完成，原始目录如下。",
      linked_local_material: {
        source_locator: "/data/research/raw-calibration",
      },
    },
  });
  expect(assetIntakes).toHaveLength(1);
  await expect(dialog.getByRole("button", { name: "提交", exact: true })).toHaveCount(0);

  await nextRequest.click();
  await expect(queue).toContainText("4 / 5");
  await expect(dialog.getByRole("heading", {
    name: "决定是否允许当前 Run 访问一个新增外部目的地。",
  })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "稍后处理", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "拒绝", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "接受", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "稍后处理", exact: true }).click();
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/agent_runtime%3AHR-63%3Ar1/responses",
    body: { decision: "deferred", facts: {}, note: "" },
  });
  expect(permissionWrites).toHaveLength(0);

  await page.goto(`${product!.baseUrl}/?panel=permission-request`, {
    waitUntil: "domcontentloaded",
  });
  await dialog.getByRole("button", { name: "接受", exact: true }).click();
  await expect.poll(() => permissionWrites.map((item) => item.path)).toEqual([
    "/api/v1/human-collaboration/commands",
    "/api/v1/human-collaboration/commands/intent-permission-63/previews",
    "/api/v1/human-collaboration/commands/intent-permission-63/confirmations",
    "/api/v1/human-collaboration/commands/intent-permission-63/authorizations",
  ]);
  expect(permissionWrites[0].body).toEqual({
    scope_ref: "human_request:agent_runtime:HR-63:r1",
    command: {
      command_kind: "capability_authorization",
      payload: {
        capability: "web_fetch",
        decision: "granted",
        scope: permissionRequirement.scope,
      },
    },
  });
  expect(permissionWrites[3].body).toEqual({
    capability: "web_fetch",
    decision: "granted",
    scope: permissionRequirement.scope,
    confirmation_receipt_ref: "confirmation-permission-63",
  });
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/agent_runtime%3AHR-63%3Ar1/responses",
    body: {
      decision: "provided",
      facts: { authorization_receipt_ref: "authorization-receipt-permission-63" },
      note: "",
    },
  });
  await expect(dialog.getByRole("heading", { name: "回应已提交" })).toBeVisible();
  await expect(dialog.getByRole("button", { name: "提交授权回应" })).toHaveCount(0);

  await nextRequest.click();
  await expect(queue).toContainText("5 / 5");
  await expect(dialog.getByRole("heading", {
    name: "重试失败的 Writing 操作：https://status.example.invalid/run-219",
  })).toBeVisible();
  await expect(dialog.locator(".hc-head").getByRole("link", {
    name: "https://status.example.invalid/run-219",
  })).toHaveAttribute("href", "https://status.example.invalid/run-219");
  await expect(dialog).toContainText("只重试上述同一操作，不展开根因分析。");
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toHaveCount(1);
  await expect(dialog.getByRole("button", { name: /提交|接受|拒绝/ })).toHaveCount(0);
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await expect.poll(() => posts.at(-1)).toEqual({
    path: "/api/v1/human-requests/writing%3AHR-70%3Ar1/retry",
    body: {},
  });
  await expect(dialog).toContainText("重试进行中");
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toHaveCount(0);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog).toContainText("重试进行中");
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toHaveCount(0);

  const humanRequests = (collaboration.human_requests as JsonRecord);
  const systemR1 = (humanRequests.items as JsonRecord[]).find(
    (item) => item.request_ref === "writing:HR-70:r1",
  )!;
  const systemR2: JsonRecord = {
    ...systemR1,
    request_ref: "writing:HR-70:r2",
    revision: 2,
    status: "open",
    obligation: "再次重试同一 Writing 操作。",
    predecessor_request_ref: "writing:HR-70:r1",
    successor_request_ref: null,
    responses: [],
    evaluation: null,
    disposition: null,
  };
  humanRequests.items = (humanRequests.items as JsonRecord[]).map(
    (item) => item.request_ref === "writing:HR-70:r1" ? systemR2 : item,
  );
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog.getByRole("heading", { name: "再次重试同一 Writing 操作。" }))
    .toBeVisible();
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await expect(dialog).toContainText("这件事已处理 · 已满足");
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toHaveCount(0);
});

test("a legal library fulltext response binds the Owner-projected acquisition paper id", async ({
  page,
}) => {
  await installHumanCollaborationSnapshot(page);
  const responseAttempts: JsonRecord[] = [];
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    await fulfillJson(route, acceptedAssetIntake(body, "library-bound"));
  });
  await page.route(
    "**/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses",
    async (route) => {
      responseAttempts.push(route.request().postDataJSON() as JsonRecord);
      await fulfillJson(route, {
        response_ref: "response-library-bound",
        status: "recorded",
      });
    },
  );

  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByRole("button", { name: /手动上传该文献/ }).click();
  await expect(dialog.getByLabel(/acquisition_paper_id/i)).toHaveCount(0);
  await dialog.getByLabel("合法全文 PDF").setInputFiles({
    name: "paper.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("lawfully acquired fulltext"),
  });
  await dialog.getByRole("button", { name: "提交全文来源回应" }).click();

  await expect.poll(() => responseAttempts.at(-1)).toMatchObject({
    decision: "provided",
    facts: {
      acquisition_paper_id: "paper-ACQ-17",
      material_source_ref: "research_asset_version-library-bound",
      material_version_ref: "research_asset_version-library-bound",
      material_content_hash: "5".repeat(64),
      material_manifest_hash: "6".repeat(64),
      material_acceptance_receipt_ref: "asset-receipt-library-bound",
    },
    note: "",
  });
  await expect(dialog.getByRole("heading", { name: "回应已提交" })).toBeVisible();
});

test("a non-asset HumanRequestResponse reloads the same sealed body and key when the first send never commits", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const attempts: Array<{ key: string; body: JsonRecord }> = [];
  await context.route("**/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses", async (route) => {
    attempts.push({
      key: route.request().headers()["idempotency-key"] ?? "",
      body: route.request().postDataJSON() as JsonRecord,
    });
    if (attempts.length === 1) {
      await route.abort("connectionreset");
      return;
    }
    const humanRequests = (snapshot.human_collaboration as JsonRecord)
      .human_requests as JsonRecord;
    const item = (humanRequests.items as JsonRecord[]).find(
      (candidate) => candidate.request_ref === "agent_runtime:HR-27:r1",
    )!;
    item.responses = [{
      response_ref: "response-generic-retry",
      status: "recorded",
      ...route.request().postDataJSON() as JsonRecord,
    }];
    item.status = "satisfied";
    item.evaluation = { evaluation_ref: "evaluation-generic-retry", decision: "satisfied" };
    item.disposition = { disposition_ref: "disposition-generic-retry", decision: "satisfied" };
    await fulfillJson(route, { response_ref: "response-generic-retry", status: "recorded" });
  });

  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.locator(".hc-optional-note textarea").fill(
    "PRIVATE GENERIC RESPONSE NOTE",
  );
  await dialog.getByRole("button", { name: "我已重连" }).click();
  await expect.poll(() => attempts.length).toBe(1);
  const pending = await page.evaluate(() => JSON.parse(sessionStorage.getItem(
    "meta_research_pending_human_request_response",
  )!) as JsonRecord);
  expect(pending).toMatchObject({
    schema: "meta-research/human-request-response/v1",
    request_ref: "agent_runtime:HR-27:r1",
    response_idempotency_key: attempts[0].key,
    sealed_response: {
      algorithm: "AES-GCM",
      ciphertext_ref: expect.any(String),
      body_hash: expect.any(String),
      binding_hash: expect.any(String),
    },
  });
  expect(pending).not.toHaveProperty("response");
  expect(JSON.stringify(await page.evaluate(() => Object.fromEntries(
    Object.entries(sessionStorage),
  )))).not.toContain("PRIVATE GENERIC RESPONSE NOTE");

  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => attempts.length).toBe(2);
  expect(attempts[1]).toEqual(attempts[0]);
  expect(await recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_response",
  ))).toBeNull();
  expect(await humanRequestRecoveryDatabaseState(recoveredPage)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
});

test("a committed non-asset HumanRequestResponse with a lost ACK replays once under the same identity", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const attempts: Array<{ key: string; body: JsonRecord }> = [];
  const committed = new Map<string, JsonRecord>();
  let ownerCommitCount = 0;
  await context.route("**/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses", async (route) => {
    const key = route.request().headers()["idempotency-key"] ?? "";
    const body = route.request().postDataJSON() as JsonRecord;
    attempts.push({ key, body });
    const replay = committed.get(key);
    if (replay) {
      await fulfillJson(route, replay);
      return;
    }
    ownerCommitCount += 1;
    const response = { response_ref: "response-generic-ack-loss", status: "recorded" };
    committed.set(key, response);
    const humanRequests = (snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord;
    const item = (humanRequests.items as JsonRecord[]).find(
      (candidate) => candidate.request_ref === "agent_runtime:HR-27:r1",
    )!;
    item.responses = [{ ...response, ...body }];
    item.status = "satisfied";
    item.evaluation = { evaluation_ref: "evaluation-generic", decision: "satisfied" };
    item.disposition = { disposition_ref: "disposition-generic", decision: "satisfied" };
    await route.abort("connectionreset");
  });

  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByRole("button", { name: "我已重连" }).click();
  await expect.poll(() => attempts.length).toBe(1);
  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => attempts.length).toBe(2);
  expect(attempts[1]).toEqual(attempts[0]);
  expect(ownerCommitCount).toBe(1);
  expect(committed.size).toBe(1);
  expect(await recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_response",
  ))).toBeNull();
});

test("two tabs retain independently keyed HumanRequestResponse recovery until a third page replays both", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const attempts = new Map<string, Array<{ key: string; body: JsonRecord }>>();
  const responsePaths = new Map([
    [
      "/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses",
      "agent_runtime:HR-27:r1",
    ],
    [
      "/api/v1/human-requests/agent_runtime%3AHR-63%3Ar1/responses",
      "agent_runtime:HR-63:r1",
    ],
  ]);
  await context.route("**/api/v1/human-requests/*/responses", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const requestRef = responsePaths.get(path);
    if (!requestRef) {
      await route.fallback();
      return;
    }
    const requestAttempts = attempts.get(requestRef) ?? [];
    requestAttempts.push({
      key: route.request().headers()["idempotency-key"] ?? "",
      body: route.request().postDataJSON() as JsonRecord,
    });
    attempts.set(requestRef, requestAttempts);
    if (requestAttempts.length === 1) {
      await route.abort("connectionreset");
      return;
    }
    const humanRequests = (snapshot.human_collaboration as JsonRecord)
      .human_requests as JsonRecord;
    const item = (humanRequests.items as JsonRecord[]).find(
      (candidate) => candidate.request_ref === requestRef,
    )!;
    item.responses = [{
      response_ref: `response-${requestRef}`,
      status: "recorded",
      ...route.request().postDataJSON() as JsonRecord,
    }];
    item.status = "satisfied";
    item.evaluation = { evaluation_ref: `evaluation-${requestRef}`, decision: "satisfied" };
    item.disposition = {
      disposition_ref: `disposition-${requestRef}`,
      decision: "satisfied",
    };
    await fulfillJson(route, {
      response_ref: `response-${requestRef}`,
      status: "recorded",
    });
  });

  const pageB = await context.newPage();
  await Promise.all([
    page.goto(`${product!.baseUrl}/?panel=human-request`, {
      waitUntil: "domcontentloaded",
    }),
    pageB.goto(`${product!.baseUrl}/?panel=permission-request`, {
      waitUntil: "domcontentloaded",
    }),
  ]);
  const dialogA = page.getByRole("dialog", { name: "需要你处理的事项" });
  const dialogB = pageB.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialogA.getByRole("button", { name: "我已重连" })).toBeVisible();
  await expect(dialogB.getByRole("button", { name: "拒绝", exact: true })).toBeVisible();

  await dialogA.getByRole("button", { name: "我已重连" }).click();
  await dialogB.getByRole("button", { name: "拒绝", exact: true }).click();
  await expect.poll(() => attempts.get("agent_runtime:HR-27:r1")?.length ?? 0).toBe(1);
  await expect.poll(() => attempts.get("agent_runtime:HR-63:r1")?.length ?? 0).toBe(1);

  const durable = await humanRequestRecoveryDatabaseState(page);
  expect(durable).toMatchObject({ keyCount: 2, ciphertextCount: 2 });
  expect(durable.manifests).toHaveLength(2);
  expect(durable.manifests.map((serialized) => (
    JSON.parse(serialized) as JsonRecord
  ).request_ref).sort()).toEqual([
    "agent_runtime:HR-27:r1",
    "agent_runtime:HR-63:r1",
  ]);

  const firstLibraryAttempt = attempts.get("agent_runtime:HR-27:r1")![0];
  const firstCapabilityAttempt = attempts.get("agent_runtime:HR-63:r1")![0];
  await pageB.close();
  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => attempts.get("agent_runtime:HR-27:r1")?.length ?? 0).toBe(2);
  await expect.poll(() => attempts.get("agent_runtime:HR-63:r1")?.length ?? 0).toBe(2);
  expect(attempts.get("agent_runtime:HR-27:r1")![1]).toEqual(firstLibraryAttempt);
  expect(attempts.get("agent_runtime:HR-63:r1")![1]).toEqual(firstCapabilityAttempt);
  await expect.poll(async () => humanRequestRecoveryDatabaseState(recoveredPage)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
  await recoveredPage.close();
});

test("an aborted atomic recovery cleanup retains its manifest and payload for the next page retry", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const attempts: Array<{ key: string; body: JsonRecord }> = [];
  const committed = new Map<string, JsonRecord>();
  let ownerCommitCount = 0;
  await context.route(
    "**/api/v1/human-requests/agent_runtime%3AHR-27%3Ar1/responses",
    async (route) => {
      const key = route.request().headers()["idempotency-key"] ?? "";
      const body = route.request().postDataJSON() as JsonRecord;
      attempts.push({ key, body });
      const existing = committed.get(key);
      if (existing) {
        await fulfillJson(route, existing);
        return;
      }
      ownerCommitCount += 1;
      const response = { response_ref: "response-cleanup-abort", status: "recorded" };
      committed.set(key, response);
      const humanRequests = (snapshot.human_collaboration as JsonRecord)
        .human_requests as JsonRecord;
      const item = (humanRequests.items as JsonRecord[]).find(
        (candidate) => candidate.request_ref === "agent_runtime:HR-27:r1",
      )!;
      item.responses = [{ ...response, ...body }];
      item.status = "satisfied";
      item.evaluation = { evaluation_ref: "evaluation-cleanup-abort", decision: "satisfied" };
      item.disposition = {
        disposition_ref: "disposition-cleanup-abort",
        decision: "satisfied",
      };
      await fulfillJson(route, response);
    },
  );

  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await page.evaluate(() => {
    const originalDelete = IDBObjectStore.prototype.delete;
    let abortCleanup = true;
    IDBObjectStore.prototype.delete = function (key) {
      const request = originalDelete.call(this, key);
      if (abortCleanup && this.name === "sealed_response_keys") {
        abortCleanup = false;
        queueMicrotask(() => {
          try {
            this.transaction.abort();
          } catch {
            // The transaction may already have completed on a broken implementation.
          }
        });
      }
      return request;
    };
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByRole("button", { name: "我已重连" }).click();
  await expect.poll(() => attempts.length).toBe(1);
  await expect(dialog).toContainText("human_request_recovery_manifest_store_unavailable");
  expect(await humanRequestRecoveryDatabaseState(page)).toMatchObject({
    keyCount: 1,
    ciphertextCount: 1,
    manifests: [expect.stringContaining("agent_runtime:HR-27:r1")],
  });

  const firstAttempt = attempts[0];
  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => attempts.length).toBe(2);
  expect(attempts[1]).toEqual(firstAttempt);
  expect(ownerCommitCount).toBe(1);
  await expect.poll(async () => humanRequestRecoveryDatabaseState(recoveredPage)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
  await recoveredPage.close();
});

test("an RM commit with a lost intake ACK reloads the sealed operation without duplicating a 6 MiB asset", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const assetAttempts: Array<{ key: string; body: JsonRecord }> = [];
  const responseAttempts: Array<{ key: string; body: JsonRecord }> = [];
  const committedAssets = new Map<string, JsonRecord>();
  let assetCommitCount = 0;
  await context.route("**/api/v1/research-assets/intakes", async (route) => {
    const key = route.request().headers()["idempotency-key"] ?? "";
    const body = route.request().postDataJSON() as JsonRecord;
    assetAttempts.push({ key, body });
    const committed = committedAssets.get(key);
    if (committed) {
      await fulfillJson(route, committed);
      return;
    }
    assetCommitCount += 1;
    committedAssets.set(key, acceptedAssetIntake(body, "before-hc"));
    await route.abort("connectionreset");
  });
  await context.route("**/api/v1/human-requests/research_memory%3AHR-41%3Ar1/responses", async (route) => {
    responseAttempts.push({
      key: route.request().headers()["idempotency-key"] ?? "",
      body: route.request().postDataJSON() as JsonRecord,
    });
    const humanRequests = (snapshot.human_collaboration as JsonRecord)
      .human_requests as JsonRecord;
    const item = (humanRequests.items as JsonRecord[]).find(
      (candidate) => candidate.request_ref === "research_memory:HR-41:r1",
    )!;
    item.responses = [{
      response_ref: "response-before-hc",
      status: "recorded",
      ...route.request().postDataJSON() as JsonRecord,
    }];
    item.status = "satisfied";
    item.evaluation = { evaluation_ref: "evaluation-before-hc", decision: "satisfied" };
    item.disposition = { disposition_ref: "disposition-before-hc", decision: "satisfied" };
    await fulfillJson(route, { response_ref: "response-before-hc", status: "recorded" });
  });

  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByLabel("回应文件").setInputFiles({
    name: "approval.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.alloc(6 * 1024 * 1024, 0x61),
  });
  await dialog.getByLabel("自然语言回应").fill(
    "PRIVATE RECOVERY NOTE SHOULD NEVER BE PLAINTEXT",
  );
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => assetAttempts.length).toBe(1);
  expect(responseAttempts).toHaveLength(0);

  const operation = await page.evaluate(() => {
    const value = sessionStorage.getItem(
      "meta_research_pending_human_request_asset_intake_operation",
    );
    return value ? JSON.parse(value) as JsonRecord : null;
  });
  expect(operation).toMatchObject({
    schema: "meta-research/human-request-asset-intake/v1",
    request_ref: "research_memory:HR-41:r1",
    intake_path: "/api/v1/research-assets/intakes",
    asset_idempotency_key: assetAttempts[0].key,
    sealed_operation: {
      algorithm: "AES-GCM",
      key_ref: expect.any(String),
      iv_base64: expect.any(String),
      ciphertext_ref: expect.any(String),
      body_hash: expect.any(String),
      binding_hash: expect.any(String),
    },
  });
  expect(JSON.stringify(operation).length).toBeLessThan(5_000);
  expect(operation).not.toHaveProperty("intake");
  expect(operation).not.toHaveProperty("response");
  const sessionStorageDump = await page.evaluate(() => JSON.stringify(
    Object.fromEntries(Object.entries(sessionStorage)),
  ));
  expect(sessionStorageDump).not.toContain("PRIVATE RECOVERY NOTE SHOULD NEVER BE PLAINTEXT");
  expect(sessionStorageDump.length).toBeLessThan(10_000);
  const durableBeforeRestart = await humanRequestRecoveryDatabaseState(page);
  expect(durableBeforeRestart).toMatchObject({ keyCount: 1, ciphertextCount: 1 });
  expect(durableBeforeRestart.manifests).toHaveLength(1);
  expect(JSON.stringify(durableBeforeRestart.manifests)).not.toContain(
    "PRIVATE RECOVERY NOTE SHOULD NEVER BE PLAINTEXT",
  );

  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => assetAttempts.length).toBe(2);
  expect(assetAttempts[1].key).toBe(assetAttempts[0].key);
  expect(JSON.stringify(assetAttempts[1].body)).toBe(JSON.stringify(assetAttempts[0].body));
  await expect.poll(() => responseAttempts.length).toBe(1);
  expect(responseAttempts[0].body.note).toBe(
    "PRIVATE RECOVERY NOTE SHOULD NEVER BE PLAINTEXT",
  );
  expect(assetCommitCount).toBe(1);
  expect(committedAssets.size).toBe(1);
  await expect.poll(() => recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_asset_intake_operation",
  ))).toBeNull();
  expect(await recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_asset_intake",
  ))).toBeNull();
  expect(await humanRequestRecoveryDatabaseState(recoveredPage)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
});

test("an HC commit with a lost ACK reloads under the same response identity without duplicating the asset", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  await keepProjectionRoutesAcrossPages(page, snapshot);
  const context = page.context();
  const assetIntakes: JsonRecord[] = [];
  const responseAttempts: Array<{ key: string; body: JsonRecord }> = [];
  const committedResponses = new Map<string, JsonRecord>();
  let ownerCommitCount = 0;
  await context.route("**/api/v1/research-assets/intakes", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    assetIntakes.push(body);
    await fulfillJson(route, acceptedAssetIntake(body, "ack-loss"));
  });
  await context.route("**/api/v1/human-requests/research_memory%3AHR-41%3Ar1/responses", async (route) => {
    const key = route.request().headers()["idempotency-key"] ?? "";
    const body = route.request().postDataJSON() as JsonRecord;
    responseAttempts.push({ key, body });
    const committed = committedResponses.get(key);
    if (committed) {
      await fulfillJson(route, committed);
      return;
    }

    ownerCommitCount += 1;
    const response = { response_ref: "response-ack-loss", status: "recorded" };
    committedResponses.set(key, response);
    const humanRequests = (snapshot.human_collaboration as JsonRecord)
      .human_requests as JsonRecord;
    const item = (humanRequests.items as JsonRecord[]).find(
      (candidate) => candidate.request_ref === "research_memory:HR-41:r1",
    )!;
    item.responses = [{ ...response, ...body }];
    item.evaluation = { evaluation_ref: "evaluation-ack-loss", decision: "satisfied" };
    item.disposition = { disposition_ref: "disposition-ack-loss", decision: "satisfied" };
    item.status = "satisfied";
    await route.abort("connectionreset");
  });

  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByLabel("回应文件").setInputFiles({
    name: "approval.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("approved"),
  });
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => responseAttempts.length).toBe(1);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_asset_response",
  ))).not.toBeNull();
  const delivery = await page.evaluate(() => JSON.parse(sessionStorage.getItem(
    "meta_research_pending_human_request_asset_response",
  )!) as JsonRecord);
  expect(delivery).toMatchObject({
    request_ref: "research_memory:HR-41:r1",
    asset_job_ref: "asset-intake-ack-loss",
    fact_prefix: "material",
    accepted_asset: {
      version_ref: "research_asset_version-ack-loss",
      content_hash: "5".repeat(64),
      manifest_hash: "6".repeat(64),
      receipt: {
        receipt_ref: "asset-receipt-ack-loss",
        payload_hash: "7".repeat(64),
      },
    },
    sealed_response: {
      algorithm: "AES-GCM",
      ciphertext_ref: expect.any(String),
      body_hash: expect.any(String),
      binding_hash: expect.any(String),
    },
    response_idempotency_key: responseAttempts[0].key,
  });
  expect(delivery).not.toHaveProperty("response");

  await page.close();
  const recoveredPage = await context.newPage();
  await recoveredPage.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect.poll(() => responseAttempts.length).toBe(2);
  expect(responseAttempts[1]).toEqual(responseAttempts[0]);
  expect(ownerCommitCount).toBe(1);
  expect(committedResponses.size).toBe(1);
  expect(assetIntakes).toHaveLength(1);
  await expect.poll(() => recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_asset_response",
  ))).toBeNull();
  expect(await recoveredPage.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_asset_intake",
  ))).toBeNull();
  expect(await humanRequestRecoveryDatabaseState(recoveredPage)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
});

test("a HumanRequest response preserves the human's raw note without secret-content classification", async ({
  page,
}) => {
  await installHumanCollaborationSnapshot(page);
  const assetIntakes: JsonRecord[] = [];
  const responseAttempts: JsonRecord[] = [];
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    assetIntakes.push(body);
    await fulfillJson(route, acceptedAssetIntake(body, "secret-retry"));
  });
  await page.route("**/api/v1/human-requests/research_memory%3AHR-41%3Ar1/responses", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    responseAttempts.push(body);
    await fulfillJson(route, { response_ref: "response-raw-note", status: "recorded" });
  });

  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByLabel("回应文件").setInputFiles({
    name: "approval.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("approved"),
  });
  await dialog.getByLabel("自然语言回应").fill("token: ghp_examplecredential");
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => responseAttempts.length).toBe(1);
  expect(responseAttempts[0]).toMatchObject({
    decision: "provided",
    facts: {
      material_source_ref: "research_asset_version-secret-retry",
      material_version_ref: "research_asset_version-secret-retry",
      material_acceptance_receipt_ref: "asset-receipt-secret-retry",
    },
    note: "token: ghp_examplecredential",
  });
  expect(assetIntakes).toHaveLength(1);
  await expect(dialog.getByRole("heading", { name: "回应已提交" })).toBeVisible();
  expect(await humanRequestRecoveryDatabaseState(page)).toEqual({
    keyCount: 0,
    ciphertextCount: 0,
    manifests: [],
  });
});

test("an orphaned material delivery is discarded when its request revision is no longer current", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const responseAttempts: Array<{ path: string; body: JsonRecord }> = [];
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    await fulfillJson(route, acceptedAssetIntake(body, "superseded"));
  });
  let externalAttempt = 0;
  await page.route("**/api/v1/human-requests/*/responses", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as JsonRecord;
    responseAttempts.push({ path, body });
    if (path.includes("research_memory%3AHR-41%3Ar1")) {
      externalAttempt += 1;
      if (externalAttempt === 1) {
        await route.abort("connectionreset");
        return;
      }
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "human_request_not_current" } }),
      });
      return;
    }
    await fulfillJson(route, { response_ref: "response-after-orphan", status: "recorded" });
  });

  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByLabel("回应文件").setInputFiles({
    name: "approval.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("approved"),
  });
  await dialog.getByRole("button", { name: "提交", exact: true }).click();
  await expect.poll(() => externalAttempt).toBe(1);
  const humanRequests = (snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord;
  humanRequests.items = (humanRequests.items as JsonRecord[]).filter(
    (item) => item.request_ref !== "research_memory:HR-41:r1",
  );

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect.poll(() => externalAttempt).toBe(2);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_human_request_asset_response",
  ))).toBeNull();
  await dialog.getByRole("button", { name: "我已重连" }).click();
  await expect.poll(() => responseAttempts.at(-1)?.path).toContain(
    "agent_runtime%3AHR-27%3Ar1",
  );
});

test("a current HumanRequest revision auto-opens once and stays reopenable from the persistent rail", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page, { questBlock: true });
  const requests = ((snapshot.human_collaboration as JsonRecord)
    .human_requests as JsonRecord).items as JsonRecord[];
  const returnLocation = "/?variant=A&view=questions&panel=question-tree";
  await page.goto(`${product!.baseUrl}${returnLocation}`, {
    waitUntil: "domcontentloaded",
  });

  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("heading", {
    name: "恢复当前图书馆会话，或选择合法全文替代路线。",
  })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect.poll(() => new URL(page.url()).pathname
    + new URL(page.url()).search + new URL(page.url()).hash).toBe(returnLocation);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("complementary", { name: "研究助手" })).toContainText(
    "需要你处理",
  );
  await page.getByRole("button", { name: "需要你" }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await expect(dialog).toBeHidden();

  const questWide = requests.find(
    (item) => item.request_ref === "research_graph:HR-52:r1",
  )!;
  questWide.status = "superseded";
  questWide.successor_request_ref = "research_graph:HR-52:r2";
  requests.push({
    ...questWide,
    request_ref: "research_graph:HR-52:r2",
    revision: 2,
    status: "open",
    obligation: "修订后的线下校准请求，交回原始结果。",
    predecessor_request_ref: "research_graph:HR-52:r1",
    successor_request_ref: null,
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("修订后的线下校准请求，交回原始结果。");
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog).toBeHidden();
});

test("auto presentation holds one exact request and dismissal does not cascade", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const humanRequests = (snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord;
  const items = humanRequests.items as JsonRecord[];
  const first = items.find(
    (item) => item.request_ref === "research_graph:HR-52:r1",
  )!;
  const second = request(
    "offline_action",
    "research_graph:HR-53:r1",
    "research_graph",
    "处理当前 Quest 的第二个全局阻塞。",
    "ExperimentBrief-13",
  );
  items.push(second);
  for (const item of items) item.status = "satisfied";

  const mutableSnapshot = snapshot as JsonRecord;
  const initialRevision = Number(mutableSnapshot.revision);
  expect(Number.isSafeInteger(initialRevision)).toBeTruthy();
  const queueRevision = initialRevision + 1;
  let releaseQueue!: () => void;
  const queueGate = new Promise<void>((resolve) => {
    releaseQueue = resolve;
  });
  let eventRequests = 0;
  await page.unroute("**/api/v1/events*");
  await page.route("**/api/v1/events*", async (route) => {
    eventRequests += 1;
    if (eventRequests > 1) {
      await route.abort("connectionrefused");
      return;
    }
    await queueGate;
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
      body: `id: ${queueRevision}\nevent: projection.updated\ndata: {"revision":${queueRevision}}\n\n`,
    });
  });

  let draftingReloads = 0;
  await page.route("**/api/v1/companion/messages", async (route) => {
    draftingReloads += 1;
    mutableSnapshot.revision = queueRevision + 1;
    await fulfillJson(
      route,
      ((snapshot.human_collaboration as JsonRecord).companion as object),
    );
  });

  const returnLocation = "/?variant=A&view=questions&panel=question-tree#queue-return";
  await page.goto(`${product!.baseUrl}${returnLocation}`, {
    waitUntil: "domcontentloaded",
  });
  const returnFocus = page.getByRole("button", { name: "问题树", exact: true });
  await expect(returnFocus).toBeEnabled();
  await returnFocus.focus();
  await expect(returnFocus).toBeFocused();
  await expect.poll(() => eventRequests).toBe(1);

  (first.direct_waiters as JsonRecord[])[0].wait_scope = "quest";
  (second.direct_waiters as JsonRecord[])[0].wait_scope = "quest";
  first.status = "open";
  second.status = "open";
  humanRequests.waiting = {
    scope: "quest",
    safe_meaningful_runnable_exists: false,
    other_blockers: [],
  };
  mutableSnapshot.revision = queueRevision;
  releaseQueue();

  const firstPresentationKey =
    "meta_research_human_request_presented:research_graph:HR-52:r1:1";
  const secondPresentationKey =
    "meta_research_human_request_presented:research_graph:HR-53:r1:1";
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("按已接纳协议完成线下校准并交回原始结果。");
  await expect(dialog).not.toContainText("处理当前 Quest 的第二个全局阻塞。");
  await expect.poll(() => page.evaluate(
    (key) => sessionStorage.getItem(key),
    firstPresentationKey,
  )).toBe("1");
  await expect.poll(() => page.evaluate(
    (key) => sessionStorage.getItem(key),
    secondPresentationKey,
  )).toBeNull();

  await dialog.getByLabel("就线下操作事项发消息")
    .fill("刷新 Projection 后仍保持这个精确请求。");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => draftingReloads).toBe(1);
  await expect(page.locator(".lumen-connection code"))
    .toHaveText(`状态 ${queueRevision + 1}`);
  await expect(dialog).toContainText("按已接纳协议完成线下校准并交回原始结果。");
  await expect(dialog).not.toContainText("处理当前 Quest 的第二个全局阻塞。");
  await expect.poll(() => page.evaluate(
    (key) => sessionStorage.getItem(key),
    secondPresentationKey,
  )).toBeNull();

  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await expect(dialog).toBeHidden();
  await expect.poll(() => page.evaluate(
    (key) => sessionStorage.getItem(key),
    secondPresentationKey,
  )).toBe("1");
  await expect.poll(() => new URL(page.url()).pathname
    + new URL(page.url()).search + new URL(page.url()).hash).toBe(returnLocation);
  await expect(returnFocus).toBeFocused();
});

test("a newly current HumanRequest remains dismissible beside a formal creation window", async ({
  page,
}) => {
  await installHumanCollaborationSnapshot(page, { questBlock: true });
  await page.goto(`${product!.baseUrl}/?panel=create-quest`, {
    waitUntil: "domcontentloaded",
  });

  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const humanRequest = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(quest).toBeVisible();
  await expect(humanRequest).toBeVisible();
  await expect(page.getByRole("complementary", { name: "研究助手" }))
    .toContainText("需要你处理");

  await humanRequest.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await expect(humanRequest).toBeHidden();
  await expect(quest).toBeVisible();
  await quest.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(quest).toBeHidden();
});

test("auto presentation skips requests already presented in this browser session", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page, { questBlock: true });
  const humanRequests = (snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord;
  const items = humanRequests.items as JsonRecord[];
  const secondCurrent = request(
    "offline_action",
    "research_graph:HR-53:r1",
    "research_graph",
    "处理当前 Quest 的第二个全局阻塞。",
    "ExperimentBrief-13",
  );
  (secondCurrent.direct_waiters as JsonRecord[])[0].wait_scope = "quest";
  const foreignQuest = request(
    "offline_action",
    "research_graph:HR-99:r1",
    "research_graph",
    "FOREIGN QUEST GLOBAL BLOCK",
    "ExperimentBrief-99",
  );
  foreignQuest.quest_ref = "quest_foreign_9";
  (foreignQuest.direct_waiters as JsonRecord[])[0].wait_scope = "quest";
  foreignQuest.status = "satisfied";
  items.push(secondCurrent, foreignQuest);
  await page.addInitScript((alreadyPresented: Array<{ requestRef: string; revision: number }>) => {
    for (const item of alreadyPresented) {
      sessionStorage.setItem(
        `meta_research_human_request_presented:${item.requestRef}:${item.revision}`,
        "1",
      );
    }
  }, items.filter((item) => item !== secondCurrent && item.status === "open").map((item) => ({
    requestRef: String(item.request_ref),
    revision: Number(item.revision),
  })));

  await page.goto(product!.baseUrl, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("处理当前 Quest 的第二个全局阻塞。");
  await expect(dialog).not.toContainText("FOREIGN QUEST GLOBAL BLOCK");
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(dialog).toBeHidden();
});

test("human obligation copy keeps operation facts inside verification details", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const humanRequests = (snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord;
  const library = (humanRequests.items as JsonRecord[]).find(
    (item) => item.request_ref === "agent_runtime:HR-27:r1",
  )!;
  library.open_effect = {
    effect_id: "effect-library-1",
    operation_binding: {
      quest_ref: "quest_chrome_1",
      task_ref: "acquisition-task-17",
      root_session_ref: "root-session-17",
      operation_id: "acquisition-primary",
      attempt_ref: "attempt-17",
      generation: 3,
      request_owner: "agent_runtime",
    },
    waiter_ref: "ACQ-consumed",
    receipt: {
      issuer: "agent_runtime",
      kind: "human_request_open",
      receipt_ref: "open-effect-receipt-1",
      subject_ref: "effect-library-1",
      payload_hash: "d".repeat(64),
    },
    task_yield: {
      status: "yielded",
      task_ref: "acquisition-task-17",
      session_ref: "root-session-17",
    },
  };
  library.predecessor_request_ref = "agent_runtime:HR-26:r1";
  library.successor_request_ref = "agent_runtime:HR-28:r1";
  library.response_rejections = [{
    rejection_ref: "human-response-rejection-1",
    request_ref: "agent_runtime:HR-27:r1",
    reason_code: "human_response_secret_forbidden",
    receipt_ref: "human-response-rejection-receipt-1",
    receipt: {
      issuer: "human_collaboration",
      kind: "human_response_rejection",
      receipt_ref: "human-response-rejection-receipt-1",
      subject_ref: "human-response-rejection-1",
      payload_hash: "1".repeat(64),
    },
  }];
  library.direct_waiters = [{
    waiter_ref: "ACQ-consumed",
    generation: 3,
    wait_scope: "local",
    status: "consumed",
    other_blockers: [],
    target_assertion: { acquisition_ref: "ACQ-consumed" },
    resume_validation: {
      validation_ref: "resume-validation-1",
      request_ref: "agent_runtime:HR-27:r1",
      waiter_ref: "ACQ-consumed",
      generation: 3,
      target_assertion_hash: "e".repeat(64),
      authorization_receipt_ref: "authorization-receipt-1",
      other_blockers: [],
      status: "released",
      reason: null,
      started_work: true,
      consumption: {
        consumption_ref: "resume-consumption-1",
        request_ref: "agent_runtime:HR-27:r1",
        waiter_ref: "ACQ-consumed",
        generation: 3,
        validation_ref: "resume-validation-1",
        work_ref: "acquisition-work-1",
        work_hash: "f".repeat(64),
        receipt: {
          issuer: "agent_runtime",
          kind: "human_request_resume_consumption",
          receipt_ref: "resume-consumption-receipt-1",
          subject_ref: "resume-consumption-1",
          payload_hash: "0".repeat(64),
        },
        created_at: 1720000002,
      },
      created_at: 1720000001,
    },
  }, {
    waiter_ref: "ACQ-still-blocked",
    generation: 4,
    wait_scope: "local",
    status: "blocked",
    other_blockers: ["policy-review"],
    target_assertion: { acquisition_ref: "ACQ-still-blocked" },
    resume_validation: null,
  }];

  for (const item of humanRequests.items as JsonRecord[]) item.status = "satisfied";
  (humanRequests.items as JsonRecord[]).at(-1)!.status = "unsatisfied";

  await page.goto(`${product!.baseUrl}/?workspace`, {
    waitUntil: "domcontentloaded",
  });
  await page.getByRole("button", { name: "需要你" }).click();
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const ordinaryList = (await dialog.locator(".hc-head, .hc-list").allTextContents()).join(" ");
  expect(ordinaryList).toContain("需要你处理的事项");
  expect(ordinaryList).toContain("处理记录");
  expect(ordinaryList).toContain("已回应，未满足");
  expect(ordinaryList).toContain("只有条件满足且其他阻碍已解除，相关任务才会继续");
  expect(ordinaryList).not.toContain("Resume only");
  expect(ordinaryList).not.toMatch(/HumanRequest|Owner|waiter|Projection|receipt|current revision/);

  library.status = "open";
  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  const ordinaryRequest = (await dialog.locator(
    ".hc-head, .hc-request-core > :not(.hc-request-details)",
  ).allTextContents()).join(" ");
  expect(ordinaryRequest).toContain("恢复当前图书馆会话");
  expect(ordinaryRequest).not.toMatch(/HumanRequest|Owner|waiter|Projection|receipt|current revision/);
  await dialog.getByText("原始请求与技术详情", { exact: true }).click();
  await expect(dialog).toContainText("agent_runtime");
  await expect(dialog).toContainText("acquisition-primary");
  await expect(dialog).toContainText("attempt-17 · generation 3");
  await expect(dialog).toContainText("open-effect-receipt-1");
  await expect(dialog).toContainText("human-response-rejection-receipt-1");
  await expect(dialog).toContainText("yielded · acquisition-task-17 · root-session-17");
  await expect(dialog).toContainText("agent_runtime:HR-26:r1");
  await expect(dialog).toContainText("agent_runtime:HR-28:r1");
  await expect(dialog).toContainText("resume-validation-1 · released");
  await expect(dialog).toContainText("resume-consumption-1 · acquisition-work-1");
  await expect(dialog).toContainText("resume-consumption-receipt-1");
  await expect(dialog).toContainText("ACQ-still-blocked");
  await expect(dialog).toContainText("generation 4 · blocked · local");
  await expect(dialog).toContainText("policy-review");
  await expect(dialog).toContainText("not recorded");
});

test("workspace repair requests explain the action and keep each recovery check independent", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const { items } = await installWorkspaceRepairSnapshot(page);
  const writes: Array<{ path: string; body: JsonRecord }> = [];
  page.on("request", (outgoing) => {
    if (["GET", "HEAD", "OPTIONS"].includes(outgoing.method())) return;
    const path = new URL(outgoing.url()).pathname;
    if (!path.startsWith("/api/v1/")) return;
    writes.push({ path, body: (outgoing.postDataJSON() ?? {}) as JsonRecord });
  });
  await page.route("**/api/v1/human-requests/*/retry", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const requestRef = decodeURIComponent(path.split("/").at(-2)!);
    const item = items.find((candidate) => candidate.request_ref === requestRef)!;
    item.responses = [{
      response_ref: `response-${item.request_id}`,
      decision: "provided",
      facts: { action: "retry", effect_id: `effect-${item.request_id}` },
      note: "",
    }];
    await fulfillJson(route, { ...item, retry: { status: "processing" } });
  });

  await page.goto(`${product!.baseUrl}/?panel=system-operation-help`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const queue = dialog.getByRole("navigation", { name: "当前待办队列" });
  await expect(dialog.getByRole("heading", {
    name: "研究程序无法运行命令或保存文件",
    exact: true,
  })).toBeVisible();
  await expect(queue).toContainText("1 / 5");
  await expect(dialog.locator(".hc-request-requirements")).not.toHaveAttribute("open");
  await dialog.getByText("查看具体要求与完成条件", { exact: true }).click();
  for (const text of ["需要谁处理", "服务器维护者", "现在需要做什么", "为什么需要处理", "怎样算恢复"]) {
    await expect(dialog.getByText(text, { exact: true })).toBeVisible();
  }
  expect(await dialog.innerText()).toMatch(/另有\s*3\s*项/);
  expect(await dialog.innerText()).not.toMatch(/bwrap|Target|fence|manifest/);
  const checkRecovery = dialog.getByRole("button", { name: "检查是否恢复", exact: true });
  await expect(checkRecovery).toBeVisible();
  await expect(dialog.getByRole("button", { name: /授予|授权|允许访问|批量/ })).toHaveCount(0);
  expect(writes).toEqual([]);

  const details = dialog.locator("details.hc-request-details");
  await expect(details).not.toHaveAttribute("open");
  await dialog.getByText("原始请求与技术详情", { exact: true }).click();
  await expect(details).toHaveAttribute("open", "");
  await expect(details).toContainText(items[0].obligation as string);
  await expect(details).toContainText(items[0].business_purpose as string);
  await expect(details).toContainText("bwrap: Failed to make / slave: Permission denied");
  await expect(details).toContainText(`harness_fence_${"8".repeat(96)}_1`);
  await dialog.getByText("原始请求与技术详情", { exact: true }).click();
  expect(writes).toEqual([]);
  const desktopScreenshot = testInfo.outputPath("workspace-repair-1440.png");
  await dialog.screenshot({ path: desktopScreenshot });
  await testInfo.attach("workspace-repair-1440", {
    path: desktopScreenshot,
    contentType: "image/png",
  });

  await checkRecovery.click();
  await expect.poll(() => writes).toEqual([{
    path: "/api/v1/human-requests/agent_runtime%3AHR-workspace-1%3Ar1/retry",
    body: {},
  }]);
  await expect(checkRecovery).toHaveCount(0);
  expect(items.slice(1).every((item) => (item.responses as unknown[]).length === 0)).toBe(true);

  await dialog.getByRole("button", { name: "查看下一个待办" }).click();
  await expect(queue).toContainText("2 / 5");
  await expect(checkRecovery).toBeVisible();
  expect(writes).toHaveLength(1);
  await checkRecovery.click();
  await expect.poll(() => writes).toEqual([
    { path: "/api/v1/human-requests/agent_runtime%3AHR-workspace-1%3Ar1/retry", body: {} },
    { path: "/api/v1/human-requests/agent_runtime%3AHR-workspace-2%3Ar1/retry", body: {} },
  ]);
  expect(items.slice(2).every((item) => (item.responses as unknown[]).length === 0)).toBe(true);
});

test("workspace repair guidance and its original technical detail fit a 390px screen", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installWorkspaceRepairSnapshot(page);
  await page.goto(`${product!.baseUrl}/?panel=system-operation-help`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog.getByRole("heading", {
    name: "研究程序无法运行命令或保存文件",
  })).toBeVisible();
  const measureOverflow = () => dialog.evaluate((root) => ({
    page: document.documentElement.scrollWidth - window.innerWidth,
    dialog: root.scrollWidth - root.clientWidth,
    core: (() => {
      const core = root.querySelector(".hc-request-core")!;
      return core.scrollWidth - core.clientWidth;
    })(),
  }));
  for (const overflow of Object.values(await measureOverflow())) {
    expect(overflow).toBeLessThanOrEqual(1);
  }
  expect(await dialog.innerText()).not.toMatch(/bwrap|Target|fence|manifest/);
  const mobileScreenshot = testInfo.outputPath("workspace-repair-390.png");
  await dialog.screenshot({ path: mobileScreenshot });
  await testInfo.attach("workspace-repair-390", {
    path: mobileScreenshot,
    contentType: "image/png",
  });
  await dialog.getByText("原始请求与技术详情", { exact: true }).click();
  await expect(dialog.locator("details.hc-request-details")).toContainText("bwrap: Failed to make / slave: Permission denied");
  for (const overflow of Object.values(await measureOverflow())) {
    expect(overflow).toBeLessThanOrEqual(1);
  }
});

test("ordinary requests explain the supplied action, impact and acceptance without hiding their source", async ({
  page,
}) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const humanRequests = (snapshot.human_collaboration as JsonRecord)
    .human_requests as JsonRecord;
  const item = (humanRequests.items as JsonRecord[])[1];
  item.business_purpose = "外部材料申请 Target 的技术背景；保留原始 manifest 供维护者追查。";
  item.target_assertion = {
    ...(item.target_assertion as JsonRecord),
    condition: {
      safe_response: "请上传供应方已经批准的材料，或提供可以访问的下载地址。",
      impact: "缺少这些材料，研究程序无法核对实验结果。",
    },
  };
  item.acceptance_conditions = ["文件包含供应方的批准结果，并且能够完整打开。"];
  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByText("查看具体要求与完成条件", { exact: true }).click();
  await expect(dialog.getByText("请上传供应方已经批准的材料，或提供可以访问的下载地址。", { exact: true })).toBeVisible();
  await expect(dialog.getByText("缺少这些材料，研究程序无法核对实验结果。", { exact: true })).toBeVisible();
  await expect(dialog.getByLabel("处理说明").getByText("文件包含供应方的批准结果，并且能够完整打开。", { exact: true })).toBeVisible();
  expect(await dialog.innerText()).not.toMatch(/Target|manifest/);
  await dialog.getByText("原始请求与技术详情", { exact: true }).click();
  await expect(dialog.locator("details.hc-request-details")).toContainText(item.business_purpose as string);
});

test("HumanRequest deep links select the exact route and keep the background inert", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installHumanCollaborationSnapshot(page);

  await page.goto(`${product!.baseUrl}/?panel=external-request`, {
    waitUntil: "domcontentloaded",
  });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const shell = page.getByTestId("product-shell");
  await expect(dialog.getByRole("heading", {
    name: "本人完成外部材料申请并交回批准结果。",
  }))
    .toBeVisible();
  await expect(page).toHaveURL(/\?panel=external-request$/);
  expect(await shell.evaluate((element) => (element as HTMLElement).inert)).toBe(true);
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();
  await expect(dialog).toBeHidden();
  expect(await shell.evaluate((element) => (element as HTMLElement).inert)).toBe(false);

  await page.goto(`${product!.baseUrl}/?panel=offline-operation`, {
    waitUntil: "domcontentloaded",
  });
  await expect(dialog.getByRole("heading", {
    name: "按已确认协议完成线下校准并交回原始结果。",
  }))
    .toBeVisible();
  await expect(page).toHaveURL(/\?panel=offline-operation$/);
  expect(await shell.evaluate((element) => (element as HTMLElement).inert)).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  await page.goto(`${product!.baseUrl}/?panel=permission-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect(dialog.getByRole("heading", {
    name: "决定是否允许当前 Run 访问一个新增外部目的地。",
  })).toBeVisible();
  await expect(page).toHaveURL(/\?panel=permission-request$/);
  expect(await shell.evaluate((element) => (element as HTMLElement).inert)).toBe(true);
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();

  await page.goto(`${product!.baseUrl}/?panel=system-operation-help`, {
    waitUntil: "domcontentloaded",
  });
  await expect(dialog.getByRole("heading", {
    name: "重试失败的 Writing 操作：https://status.example.invalid/run-219",
  })).toBeVisible();
  await expect(page).toHaveURL(/\?panel=system-operation-help$/);
  await dialog.getByRole("button", { name: "关闭需要你处理的事项" }).click();

  await page.goto(`${product!.baseUrl}/?panel=human-request`, {
    waitUntil: "domcontentloaded",
  });
  await expect(dialog.getByRole("heading", {
    name: "恢复当前图书馆会话，或选择合法全文替代路线。",
  })).toBeVisible();
  await expect(page).toHaveURL(/\?panel=human-request$/);
});

test("the HumanRequest workspace puts the assistant first at 1440/800/390", async ({
  page,
}, testInfo) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const requests = ((snapshot.human_collaboration as JsonRecord).human_requests as JsonRecord).items as JsonRecord[];
  const offlineRequest = requests.find((request) => request.kind === "offline_action")!;
  offlineRequest.obligation = "取得两只 OmniHand 的现场访问与安全停止依据，并逐步检查设备连接、接口文档和操作结果。";
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${product!.baseUrl}/?panel=offline-operation`, { waitUntil: "domcontentloaded" });
  const railEntry = page.getByRole("button", { name: "需要你" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const closeButton = dialog.getByRole("button", { name: "关闭需要你处理的事项" });
  await expect(closeButton).toBeVisible();
  expect(await page.getByTestId("product-shell").evaluate((element) => (element as HTMLElement).inert))
    .toBe(true);
  await expect(dialog.getByRole("button", { name: "带我开始第一步 →" })).toBeInViewport();
  await expect(dialog.locator(".hc-request-requirements")).not.toHaveAttribute("open");

  const regions = () => dialog.evaluate((root) => {
    const box = (selector: string) => {
      const rect = root.querySelector(selector)?.getBoundingClientRect();
      if (!rect) throw new Error(`missing ${selector}`);
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return {
      core: box(".hc-request-core"),
      draft: box(".hc-request-draft"),
      dialogOverflow: root.scrollWidth - root.clientWidth,
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
    };
  });

  const desktop = await regions();
  expect(desktop.draft.x + desktop.draft.width).toBeLessThanOrEqual(desktop.core.x + 1);
  expect(desktop.draft.width).toBeGreaterThan(desktop.core.width);
  expect(desktop.pageWidth).toBeLessThanOrEqual(desktop.viewportWidth);
  expect(desktop.dialogOverflow).toBeLessThanOrEqual(1);
  const desktopScreenshot = testInfo.outputPath("human-request-assistant-1440.png");
  await dialog.screenshot({ path: desktopScreenshot });
  await testInfo.attach("human-request-assistant-1440", {
    path: desktopScreenshot, contentType: "image/png",
  });

  await page.setViewportSize({ width: 800, height: 900 });
  const tablet = await regions();
  expect(tablet.core.y).toBeGreaterThanOrEqual(tablet.draft.y + tablet.draft.height - 1);
  expect(tablet.pageWidth).toBeLessThanOrEqual(tablet.viewportWidth);
  expect(tablet.dialogOverflow).toBeLessThanOrEqual(1);
  await expect(dialog.getByRole("button", { name: "带我开始第一步 →" })).toBeInViewport();
  const tabletScreenshot = testInfo.outputPath("human-request-assistant-800.png");
  await dialog.screenshot({ path: tabletScreenshot });
  await testInfo.attach("human-request-assistant-800", {
    path: tabletScreenshot, contentType: "image/png",
  });

  await page.setViewportSize({ width: 390, height: 844 });
  const mobile = await regions();
  expect(mobile.core.y).toBeGreaterThanOrEqual(mobile.draft.y + mobile.draft.height - 1);
  expect(mobile.pageWidth).toBeLessThanOrEqual(mobile.viewportWidth);
  expect(mobile.dialogOverflow).toBeLessThanOrEqual(1);
  await dialog.locator(".hc-draft-transcript").evaluate((transcript) => {
    transcript.scrollTop = 0;
  });
  const firstStep = dialog.getByRole("button", { name: "带我开始第一步 →" });
  const firstStepClip = await firstStep.evaluate((button) => {
    const transcript = button.closest(".hc-draft-transcript")!;
    const buttonRect = button.getBoundingClientRect();
    const transcriptRect = transcript.getBoundingClientRect();
    return {
      scrollTop: transcript.scrollTop,
      visibleHeight: Math.min(buttonRect.bottom, transcriptRect.bottom, window.innerHeight)
        - Math.max(buttonRect.top, transcriptRect.top, 0),
    };
  });
  expect(firstStepClip.scrollTop).toBe(0);
  expect(firstStepClip.visibleHeight).toBeGreaterThan(0);
  await expect(firstStep).toBeInViewport();
  const mobileScreenshot = testInfo.outputPath("human-request-assistant-390.png");
  await dialog.screenshot({ path: mobileScreenshot });
  await testInfo.attach("human-request-assistant-390", {
    path: mobileScreenshot, contentType: "image/png",
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(railEntry).toBeVisible();
  expect(await page.getByTestId("product-shell").evaluate((element) => (element as HTMLElement).inert))
    .toBe(false);
});

test("human request first-step and tool shortcuts send scoped messages without submitting a response", async ({ page }) => {
  await installHumanCollaborationSnapshot(page);
  const writes: Array<{ path: string; body: JsonRecord }> = [];
  page.on("request", (request) => {
    if (request.method() !== "POST") return;
    writes.push({ path: new URL(request.url()).pathname, body: request.postDataJSON() as JsonRecord });
  });
  await page.route("**/api/v1/companion/messages", (route) => route.fulfill({
    json: { interaction_ref: `guided-turn-${writes.length}`, status: "queued" },
  }));
  await page.goto(`${product!.baseUrl}/?panel=offline-operation`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const input = dialog.getByLabel("就线下操作事项发消息");
  expect(writes).toEqual([]);
  await dialog.getByRole("button", { name: "带我开始第一步 →" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].body.message).toMatch(/第一步.*具体怎么做.*预期看到什么/);
  expect(writes[0].body.view_context).toEqual({
    kind: "human_request", quest_ref: "quest_chrome_1",
    request_ref: "research_graph:HR-52:r1", revision: 1,
  });
  await expect(input).toBeEnabled();
  await input.fill("这是我正在整理的错误日志");
  await dialog.getByRole("button", { name: "先帮我搜索", exact: true }).click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].body.message).toMatch(/搜索.*官方文档.*来源/);
  await expect(input).toHaveValue("这是我正在整理的错误日志");
  await expect(input).toBeEnabled();
  await dialog.getByRole("button", { name: "一起排查问题", exact: true }).click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2].body.message).toContain("编写和运行调试脚本");
  expect(writes.every((write) => write.path === "/api/v1/companion/messages")).toBe(true);
  await expect(dialog.locator(".hc-draft-transcript .me")).toHaveCount(3);
  await expect(dialog.locator(".hc-request-requirements")).not.toHaveAttribute("open");
});

test("human request Draft echoes immediately while its POST is pending", async ({ page }) => {
  await installHumanCollaborationSnapshot(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let posts = 0;
  await page.route("**/api/v1/companion/messages", async (route) => {
    posts += 1;
    await gate;
    await route.fulfill({ status: 503, json: { error: { code: "temporarily_unavailable" } } });
  });
  await page.goto(`${product!.baseUrl}/?panel=human-request`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const input = dialog.getByLabel("就图书馆恢复事项发消息");
  await input.fill("请立即显示本条讨论消息");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  try {
    await expect.poll(() => posts).toBe(1);
    await expect(dialog.locator(".hc-draft-transcript .me")).toContainText("请立即显示本条讨论消息", { timeout: 750 });
    await expect(dialog.locator(".hc-draft-transcript .me")).toBeInViewport();
    await expect(input).toHaveValue("");
    await expect(dialog.getByRole("button", { name: "发送消息" })).toBeDisabled();
  } finally { release(); }
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue("请立即显示本条讨论消息");
  await expect(dialog.locator(".hc-draft-transcript .me")).toHaveCount(0);
  expect(posts).toBe(1);
});

test("human request Draft streams before its snapshot and keeps request conversations separate", async ({ page }) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const collaboration = snapshot.human_collaboration as JsonRecord;
  const companion = collaboration.companion as JsonRecord;
  const messages = companion.messages as JsonRecord[];
  await page.addInitScript(() => {
    const native = window.EventSource;
    window.replyStreams = new Map();
    window.EventSource = new Proxy(native, {
      construct(target, args: ConstructorParameters<typeof EventSource>) {
        const path = String(args[0]);
        if (!path.includes("/stream")) return Reflect.construct(target, args);
        const stream = {
          closed: false,
          onmessage: null as null | ((event: MessageEvent<string>) => void),
          close() { this.closed = true; },
          emit(value: object) { if (!this.closed) this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) })); },
        };
        window.replyStreams.set(path, stream);
        return stream;
      },
    });
  });
  await page.route("**/api/v1/companion/messages", (route) =>
    route.fulfill({ json: { interaction_ref: "request-stream", status: "queued" } }));
  await page.goto(`${product!.baseUrl}/?panel=human-request`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await dialog.getByLabel("就图书馆恢复事项发消息").fill("这个事项请分段解释");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => page.evaluate(() => [...window.replyStreams.keys()].some((path) => path.includes("request-stream")))).toBe(true);
  await page.evaluate(() => [...window.replyStreams.values()][0].emit({ text: "第一段事项解释。", status: "processing" }));
  const reply = dialog.locator(".hc-draft-transcript article:not(.me)").filter({ hasText: "第一段事项解释。" });
  await expect(reply).toBeInViewport();
  await page.evaluate(() => [...window.replyStreams.values()][0].emit({ text: "第一段事项解释。\n第二段事项解释。", status: "processing" }));
  await expect(reply).toContainText("第二段事项解释。");
  await expect(reply).toBeInViewport();
  const context = { kind: "human_request", quest_ref: "quest_chrome_1", request_ref: "agent_runtime:HR-27:r1", revision: 1 };
  messages.push(
    { message_ref: "request-stream:user", scope_ref: "quest_chrome_1", role: "user", content: "这个事项请分段解释", status: "completed", view_context: context },
    { message_ref: "request-stream:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "事项正式完整回答。\n\n1. 查看连接状态。\n2. 把结果发回来。\n\n```shell\nprintf 'ok'\n```\n\n[官方文档](https://example.org/docs)", status: "completed", view_context: context },
  );
  (snapshot as JsonRecord).revision = Number((snapshot as JsonRecord).revision) + 1;
  await page.evaluate(() => [...window.replyStreams.values()][0].emit({ text: "事项正式完整回答。", status: "completed" }));
  await expect(dialog.locator(".hc-draft-transcript .me")).toHaveCount(1);
  await expect(dialog.locator(".hc-draft-transcript article:not(.me)")).toHaveCount(1);
  await expect(dialog.locator(".hc-draft-transcript")).toContainText("事项正式完整回答。");
  await expect(dialog.locator(".hc-draft-transcript ol li")).toHaveCount(2);
  await expect(dialog.locator(".hc-draft-transcript pre code")).toHaveText("printf 'ok'");
  await expect(dialog.getByRole("link", { name: "官方文档" })).toHaveAttribute("href", "https://example.org/docs");
  await expect(dialog.getByText("正在回复…", { exact: true })).toHaveCount(0);
  await dialog.getByRole("button", { name: "查看下一个待办" }).click();
  await expect(dialog.getByLabel("就外部材料事项发消息")).toHaveValue("");
  await expect(dialog.locator(".hc-draft-transcript")).not.toContainText("事项正式完整回答。");
  await expect(dialog.locator(".hc-draft-transcript")).not.toContainText("这个事项请分段解释");
});

async function downloadedText(download: Download): Promise<string> {
  const path = await download.path();
  if (!path) throw new Error("download did not produce a local file");
  return readFile(path, "utf8");
}

test("human request handoff generates an exact scoped document and downloads the completed turn after reopening", async ({ page }, testInfo) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const collaboration = snapshot.human_collaboration as JsonRecord;
  const messages = (collaboration.companion as JsonRecord).messages as JsonRecord[];
  const requests = (collaboration.human_requests as JsonRecord).items as JsonRecord[];
  const context = {
    kind: "human_request", quest_ref: "quest_chrome_1",
    request_ref: "research_graph:HR-52:r1", revision: 1,
  };
  const body = [
    "# 线下校准协作说明",
    "",
    "先确认设备断电，再核对接口文档。",
    "",
    "1. 找到设备型号。",
    "2. 将原始读数与操作结果带回来。",
    "",
    "```text",
    `outputs/${"long-result-path-".repeat(30)}.txt`,
    "```",
    "",
    "[官方资料](https://example.org/official-protocol)",
    "",
    '<script>window.handoffExecuted = true</script>',
    "![remote](https://example.invalid/tracker.png)",
    "[unsafe](javascript:alert(1))",
  ].join("\n");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const writes: Array<{ path: string; body: JsonRecord }> = [];
  page.on("request", (request) => {
    if (request.method() === "POST") writes.push({
      path: new URL(request.url()).pathname, body: request.postDataJSON() as JsonRecord,
    });
  });
  await page.route("**/api/v1/companion/messages", async (route) => {
    messages.push(
      { message_ref: "handoff-1:user", scope_ref: "quest_chrome_1", role: "user", content: HANDOFF_DOCUMENT_PROMPT, status: "completed", view_context: context },
      { message_ref: "handoff-1:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "尚未完成的文档片段", status: "processing", view_context: context },
      { message_ref: "later-chat:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "LATER CHAT MUST NOT BE EXPORTED", status: "completed", view_context: context },
      { message_ref: "wrong-revision:user", scope_ref: "quest_chrome_1", role: "user", content: HANDOFF_DOCUMENT_PROMPT, status: "completed", view_context: { ...context, revision: 2 } },
      { message_ref: "wrong-revision:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "WRONG REVISION MUST NOT BE EXPORTED", status: "completed", view_context: { ...context, revision: 2 } },
    );
    (snapshot as JsonRecord).revision = Number((snapshot as JsonRecord).revision) + 1;
    await gate;
    await route.fulfill({ json: { interaction_ref: "handoff-1", status: "queued" } });
  });
  await page.route("**/api/v1/companion/messages/*/stream*", (route) => route.abort());
  await page.goto(`${product!.baseUrl}/?panel=offline-operation`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const document = dialog.getByRole("region", { name: "协作说明文档" });
  const input = dialog.getByLabel("就线下操作事项发消息");
  await expect(document.getByText("交给其他 AI 或自己做", { exact: true })).toBeVisible();
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toHaveCount(0);
  await input.fill("这段尚未发送的设备情况需要保留");
  await document.getByRole("button", { name: "生成协作说明", exact: true }).click();
  try {
    await expect.poll(() => writes.length).toBe(1);
    expect(writes[0]).toEqual({ path: "/api/v1/companion/messages", body: {
      message: HANDOFF_DOCUMENT_PROMPT, scope_ref: "quest_chrome_1", view_context: context,
    } });
    await expect(document.getByRole("button", { name: "正在整理文档…", exact: true })).toBeDisabled();
    await expect(document.getByRole("button", { name: "下载 Markdown" })).toHaveCount(0);
    await expect(input).toHaveValue("这段尚未发送的设备情况需要保留");
  } finally { release(); }
  await expect(input).toBeEnabled();
  await expect(document.getByRole("button", { name: "正在整理文档…", exact: true })).toBeDisabled();
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(document.getByRole("button", { name: "正在整理文档…", exact: true })).toBeDisabled();
  await expect(document.getByRole("button", { name: "下载阅读版" })).toHaveCount(0);

  const reply = messages.find((message) => message.message_ref === "handoff-1:assistant")!;
  reply.content = body;
  reply.status = "completed";
  (snapshot as JsonRecord).revision = Number((snapshot as JsonRecord).revision) + 1;
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(document.getByRole("button", { name: "重新生成说明", exact: true })).toBeEnabled();
  const [markdownDownload] = await Promise.all([
    page.waitForEvent("download"), document.getByRole("button", { name: "下载 Markdown", exact: true }).click(),
  ]);
  expect(markdownDownload.suggestedFilename()).toMatch(/\.md$/);
  const markdown = await downloadedText(markdownDownload);
  expect(markdown).toContain(body);
  expect(markdown).toContain("research_graph:HR-52:r1");
  expect(markdown).toContain("按已接纳协议完成线下校准并交回原始结果。");
  expect(markdown).not.toContain("LATER CHAT MUST NOT BE EXPORTED");
  expect(markdown).not.toContain("WRONG REVISION MUST NOT BE EXPORTED");
  expect(markdown).not.toContain("REQUEST-ONLY transcript marker");
  const [htmlDownload] = await Promise.all([
    page.waitForEvent("download"), document.getByRole("button", { name: "下载阅读版", exact: true }).click(),
  ]);
  expect(htmlDownload.suggestedFilename()).toMatch(/\.html$/);
  const html = await downloadedText(htmlDownload);
  const reader = await page.context().newPage();
  await reader.route("**/*", (route) => route.abort());
  await reader.setContent(html);
  await expect(reader.locator("body")).toContainText("先确认设备断电，再核对接口文档。");
  await expect(reader.locator("script,iframe,object,embed,form,img,link[rel=stylesheet]")).toHaveCount(0);
  expect(await reader.locator("[href]").evaluateAll((elements) =>
    elements.every((element) => !/^(?:javascript|data|vbscript):/i.test(element.getAttribute("href")?.trim() ?? "")),
  )).toBe(true);
  await reader.close();

  await dialog.getByRole("button", { name: "查看下一个待办" }).click();
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toHaveCount(0);
  await dialog.getByRole("button", { name: "查看上一个待办" }).click();
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  const overflow = await document.evaluate((root) => ({
    page: window.document.documentElement.scrollWidth - window.innerWidth,
    section: root.scrollWidth - root.clientWidth,
    dialog: root.closest("dialog")!.scrollWidth - root.closest("dialog")!.clientWidth,
  }));
  for (const amount of Object.values(overflow)) expect(amount).toBeLessThanOrEqual(1);
  const screenshot = testInfo.outputPath("human-request-handoff-390.png");
  await document.screenshot({ path: screenshot });
  await testInfo.attach("human-request-handoff-390", { path: screenshot, contentType: "image/png" });
  expect(writes).toHaveLength(1);
  expect(requests.every((request) => (request.responses as unknown[]).length === 0)).toBe(true);
});

test("human request handoff hides an older document when the newest generation fails and allows retry", async ({ page }) => {
  const snapshot = await installHumanCollaborationSnapshot(page);
  const messages = ((snapshot.human_collaboration as JsonRecord).companion as JsonRecord).messages as JsonRecord[];
  const context = {
    kind: "human_request", quest_ref: "quest_chrome_1",
    request_ref: "agent_runtime:HR-27:r1", revision: 1,
  };
  messages.push(
    { message_ref: "older-handoff:user", scope_ref: "quest_chrome_1", role: "user", content: HANDOFF_DOCUMENT_PROMPT, status: "completed", view_context: context },
    { message_ref: "older-handoff:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "这份旧说明不能替代失败的新一轮整理。", status: "completed", view_context: context },
  );
  let posts = 0;
  await page.route("**/api/v1/companion/messages", async (route) => {
    posts += 1;
    const interactionRef = `retry-handoff-${posts}`;
    messages.push(
      { message_ref: `${interactionRef}:user`, scope_ref: "quest_chrome_1", role: "user", content: HANDOFF_DOCUMENT_PROMPT, status: "completed", view_context: context },
      { message_ref: `${interactionRef}:assistant`, scope_ref: "quest_chrome_1", role: "assistant", content: posts === 1 ? "仅有未完成片段" : "# 新的完整协作说明\n\n先查看图书馆登录状态。", status: posts === 1 ? "failed" : "completed", view_context: context },
    );
    (snapshot as JsonRecord).revision = Number((snapshot as JsonRecord).revision) + 1;
    await route.fulfill({ json: { interaction_ref: interactionRef, status: "queued" } });
  });
  await page.route("**/api/v1/companion/messages/*/stream*", (route) => route.abort());
  await page.goto(`${product!.baseUrl}/?panel=human-request`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  const document = dialog.getByRole("region", { name: "协作说明文档" });
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toBeVisible();
  await document.getByRole("button", { name: "重新生成说明", exact: true }).click();
  await expect(document.getByRole("status")).toHaveText("文档生成失败，请重试。");
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toHaveCount(0);
  await expect(document.getByRole("button", { name: "下载阅读版" })).toHaveCount(0);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(document.getByRole("status")).toHaveText("文档生成失败，请重试。");
  await document.getByRole("button", { name: "生成协作说明", exact: true }).click();
  await expect(document.getByRole("button", { name: "下载 Markdown" })).toBeVisible();
  const [download] = await Promise.all([
    page.waitForEvent("download"), document.getByRole("button", { name: "下载 Markdown", exact: true }).click(),
  ]);
  const markdown = await downloadedText(download);
  expect(markdown).toContain("先查看图书馆登录状态。");
  expect(markdown).not.toContain("这份旧说明不能替代失败的新一轮整理。");
  expect(markdown).not.toContain("仅有未完成片段");
  expect(posts).toBe(2);
});
