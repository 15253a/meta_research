import { expect, test, type Route } from "@playwright/test";
import { PNG } from "pngjs";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product";


type JsonRecord = Record<string, any>;

let product: DeterministicProduct | null = null;

test.afterEach(async ({ page }) => {
  await page.close().catch(() => undefined);
  const running = product;
  product = null;
  if (running) await running.stop();
});

async function fulfill(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

test("Foreground controls require draft preview confirmation and execution in the fixed Companion", async ({
  page,
}) => {
  product = await DeterministicProduct.start();
  await product.authenticate(page);
  const baseResponse = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(baseResponse.ok()).toBeTruthy();
  const base = await baseResponse.json() as JsonRecord;
  const writes: string[] = [];
  let resumeExecutionAttempts = 0;
  let rejectNextConfirmationAsStale = false;
  let nextPreviewError: string | null = null;
  let nextExecutionError: string | null = null;
  const target = {
    quest_ref: "quest_control_1",
    cycle_ref: "cycle_control_1",
    question_ref: "question_control_root",
    epoch: 3,
    target_scope: "cycle",
    target_question_ref: "question_control_branch",
  };
  let command: JsonRecord = {
    intent_id: "intent-control-1",
    scope_ref: "quest:quest_control_1",
    status: "draft",
    draft_revision: 1,
    draft_hash: "a".repeat(64),
    draft: {
      command_kind: "research_control",
      payload: {
        action: "forced_switch",
        target,
        reason: "operator_requested",
      },
    },
    executed: false,
    impact_preview: null,
    confirmation_receipt: null,
  };
  const snapshot: JsonRecord = {
    ...base,
    research_space: {
      ...base.research_space,
      status: "active",
      quest_count: 1,
      question_count: 2,
      foreground_cycle_count: 1,
    },
    question_tree: {
      status: "ready",
      reason: null,
      items: [{
        question_ref: "question_control_root",
        quest_ref: "quest_control_1",
        parent_question_ref: null,
        title: "Root control question",
        unknown_statement: "Root unknown",
        content_ref: "content-root",
        content_hash: "1".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-root",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "bound",
          cycle_ref: "cycle_control_1",
          foreground: {
            quest_ref: "quest_control_1",
            cycle_ref: "cycle_control_1",
            question_ref: "question_control_root",
            stage: "idea",
            epoch: 3,
            status: "active",
          },
          reason: null,
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }, {
        question_ref: "question_control_branch",
        quest_ref: "quest_control_1",
        parent_question_ref: "question_control_root",
        title: "Branch control question",
        unknown_statement: "Branch unknown",
        content_ref: "content-branch",
        content_hash: "2".repeat(64),
        schema_ref: "question/v1",
        question_receipt_ref: "receipt-branch",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }],
    },
    research_control: {
      status: "ready",
      quest_ref: "quest_control_1",
      foreground: {
        quest_ref: "quest_control_1",
        cycle_ref: "cycle_control_1",
        question_ref: "question_control_root",
        stage: "idea",
        epoch: 3,
        status: "active",
        grant_ref: "foreground-grant-3",
        grant_status: "active",
        safe_point_ref: null,
        pending_operation_ref: null,
        owner_revision: 7,
      },
      managed_runs: [{
        run_ref: "idea-run-1",
        run_kind: "idea_stage",
        quest_ref: "quest_control_1",
        cycle_ref: "cycle_control_1",
        epoch: 3,
        status: "running",
        attempt_ref: "idea-attempt-1",
        root_session_ref: "idea-session-1",
        fence_ref: "idea-fence-1",
        control_revision: 1,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_720_000_000,
      }],
      recovery_records: [{
        prune_record_ref: "prune-record-old-1",
        quest_ref: "quest_control_1",
        root_question_ref: "question_pruned_old",
        affected_question_refs: ["question_pruned_old"],
        affected_question_count: 1,
        receipt_ref: "prune-receipt-old-1",
        receipt_hash: "8".repeat(64),
        created_at: 1_719_999_900,
      }],
      actions: [
        "pause", "resume", "normal_switch", "forced_switch",
        "cancel", "abandon", "prune", "restore",
      ],
    },
    human_collaboration: {
      companion: {
        status: "ready",
        scope_ref: "quest:quest_control_1",
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
        items: [],
      },
      commands: { status: "ready", items: [], authorizations: [] },
    },
  };

  await page.route("**/api/v1/snapshot", (route) => fulfill(route, snapshot));
  await page.route("**/api/v1/events*", (route) => route.abort("connectionrefused"));
  await page.route("**/api/v1/human-collaboration/commands", async (route) => {
    const body = route.request().postDataJSON() as JsonRecord;
    const action = body.command.payload.action as string;
    writes.push(`draft:${action}`);
    if (action === "forced_switch") {
      expect(body.command.payload).toEqual({
        action: "forced_switch",
        target,
        reason: "operator_requested",
      });
    } else if (action === "pause") {
      if (body.command.payload.target.target_scope === "run") {
        expect(body.command.payload).toEqual({
          action: "pause",
          target: {
            quest_ref: "quest_control_1",
            cycle_ref: "cycle_control_1",
            question_ref: "question_control_root",
            epoch: 3,
            target_scope: "run",
            run_ref: "idea-run-1",
          },
          reason: "operator_requested",
        });
      } else {
        expect(body.command.payload).toEqual({
          action: "pause",
          target: {
            quest_ref: "quest_control_1",
            cycle_ref: "cycle_control_1",
            question_ref: "question_control_root",
            epoch: 3,
            target_scope: "cycle",
          },
          reason: "operator_requested",
        });
      }
    } else if (action === "resume") {
      expect(body.command.payload).toEqual({
        action: "resume",
        target: {
          quest_ref: "quest_control_1",
          cycle_ref: "cycle_control_1",
          question_ref: "question_control_root",
          epoch: 3,
          target_scope: "cycle",
        },
        reason: "operator_requested",
      });
    } else if (action === "restore") {
      expect(body.command.payload).toEqual({
        action: "restore",
        target: {
          quest_ref: "quest_control_1",
          cycle_ref: "cycle_control_1",
          question_ref: "question_control_root",
          epoch: 3,
          target_scope: "cycle",
          target_question_ref: "question_pruned_old",
          prune_record_ref: "prune-record-old-1",
        },
        reason: "operator_requested",
      });
    } else if (action === "prune") {
      expect(body.command.payload).toEqual({
        action: "prune",
        target: {
          quest_ref: "quest_control_1",
          cycle_ref: "cycle_control_1",
          question_ref: "question_control_root",
          epoch: 3,
          target_question_ref: "question_control_branch",
        },
        reason: "operator_requested",
      });
    }
    command = {
      ...command,
      intent_id: `intent-control-${action}`,
      status: "draft",
      draft_hash: String(action.length % 10).repeat(64),
      draft: body.command,
      executed: false,
      impact_preview: null,
      confirmation_receipt: null,
      control_execution: null,
    };
    snapshot.human_collaboration.commands.items = [
      command,
      ...snapshot.human_collaboration.commands.items.filter(
        (item: JsonRecord) => item.intent_id !== command.intent_id,
      ),
    ];
    await fulfill(route, command, 201);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/previews", async (route) => {
    writes.push("preview");
    if (nextPreviewError) {
      const code = nextPreviewError;
      nextPreviewError = null;
      await fulfill(route, { detail: { code } }, 503);
      return;
    }
    command.status = "previewed";
    command.impact_preview = {
      preview_ref: "control-preview-1",
      preview_hash: "b".repeat(64),
      draft_revision: 1,
      draft_hash: command.draft_hash,
      owner_revisions: { advancement_engine: 7, agent_runtime: 9 },
      status: "current",
      owner_previews: [{
        source_owner: "advancement_engine",
        target_assertion: { source_epoch: 3, target_question_ref: target.target_question_ref },
        will_happen: ["旧 Foreground Epoch 被撤销"],
        will_not_happen: ["不会改写 Question"],
        risks: ["强制路径可能留下异步清理"],
        stale_conditions: ["Epoch 改变"],
        digest: "c".repeat(64),
      }, {
        source_owner: "agent_runtime",
        target_assertion: { affected_runs: ["idea-run-1"] },
        will_happen: ["保存 durable Safe Point 并永久撤销旧 Fence"],
        will_not_happen: ["不会提交 Stage"],
        risks: ["外部效果可能需要对账"],
        stale_conditions: ["Attempt 改变"],
        digest: "d".repeat(64),
      }],
    };
    await fulfill(route, command, 201);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/confirmations", async (route) => {
    writes.push("confirm");
    if (rejectNextConfirmationAsStale) {
      rejectNextConfirmationAsStale = false;
      await fulfill(route, { detail: { code: "command_preview_stale" } }, 409);
      return;
    }
    command.status = "confirmed";
    command.impact_preview.status = "consumed";
    command.confirmation_receipt = {
      issuer: "human_collaboration",
      kind: "human_confirmation",
      receipt_ref: "control-confirmation-1",
      subject_ref: command.intent_id,
      payload_hash: "e".repeat(64),
      status: "accepted",
    };
    await fulfill(route, command, 201);
  });
  await page.route("**/api/v1/human-collaboration/commands/*/executions", async (route) => {
    writes.push("execute");
    if (nextExecutionError) {
      const code = nextExecutionError;
      nextExecutionError = null;
      await fulfill(route, { detail: { code } }, 409);
      return;
    }
    if (command.draft.payload.action === "resume") {
      resumeExecutionAttempts += 1;
      if (resumeExecutionAttempts === 1) {
        await fulfill(route, {
          detail: { code: "runtime_quiescence_pending" },
        }, 409);
        return;
      }
    }
    command.executed = true;
    command.control_execution = {
      execution_ref: "control-execution-1",
      status: "completed",
      owner_receipts: [],
      receipt_ref: "control-execution-receipt-1",
      receipt_hash: "f".repeat(64),
    };
    if (
      command.draft.payload.action === "pause"
      && command.draft.payload.target.target_scope === "cycle"
    ) {
      snapshot.research_control.foreground.status = "suspended";
    } else if (command.draft.payload.action === "resume") {
      snapshot.research_control.foreground.status = "active";
    }
    await fulfill(route, command, 201);
  });

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  const companion = page.getByRole("complementary", { name: "研究助手" });
  const composer = companion.getByRole("region", { name: "研究控制" });
  await expect(composer).toContainText("Epoch 3 · idea");
  await expect(composer.getByLabel("控制动作")).not.toBeVisible();
  await composer.getByText("研究控制", { exact: true }).click();
  await composer.getByLabel("控制动作").selectOption("forced_switch");
  await composer.getByLabel("目标 Question").selectOption("question_control_branch");
  await composer.getByRole("button", { name: "查看操作草案" }).click();

  const card = companion.locator(".lumen-command").filter({ hasText: "强制切换" });
  await card.getByRole("button", { name: "生成 Owner Impact Preview" }).click();
  await card.getByText("查看精确草案与 Owner Impact Preview", { exact: true }).click();
  await expect(card).toContainText("durable Safe Point");
  await card.getByRole("button", { name: "确认当前草案与预览" }).click();
  await expect(card).toContainText("确认已记录，控制尚未执行");
  await card.getByRole("button", { name: "执行已确认控制" }).click();
  await expect(card).toContainText("CONTROL EXECUTION · COMPLETED");
  expect(writes).toEqual(["draft:forced_switch", "preview", "confirm", "execute"]);

  await composer.getByLabel("控制动作").selectOption("pause");
  await composer.getByLabel("控制范围").selectOption("run");
  await composer.getByLabel("Managed Run").selectOption("idea-run-1");
  await composer.getByRole("button", { name: "查看操作草案" }).click();
  await expect.poll(() => writes.at(-1)).toBe("draft:pause");

  await composer.getByLabel("控制动作").selectOption("restore");
  await composer.getByLabel("恢复记录").selectOption("prune-record-old-1");
  await composer.getByRole("button", { name: "查看操作草案" }).click();
  await expect.poll(() => writes.at(-1)).toBe("draft:restore");

  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  const branch = tree.locator('[data-question-ref="question_control_branch"]');
  await branch.hover();
  await branch.getByRole("button", { name: "剪裁 question_control_branch" }).click();
  await expect.poll(() => writes.at(-1)).toBe("draft:prune");
  await expect(companion.locator(".lumen-command").filter({ hasText: "剪裁 Question" }))
    .toBeVisible();

  const headerControl = page.getByRole("button", { name: "暂停研究", exact: true });
  await expect(headerControl).toBeVisible();
  await headerControl.click();
  const confirmation = page.getByRole("dialog", { name: "暂停当前研究？" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("当前界面与 Web 服务保持在线");
  await expect(confirmation).toContainText("不会提交 Stage");
  await expect.poll(() => writes.slice(-2)).toEqual(["draft:pause", "preview"]);
  await confirmation.getByRole("button", { name: "确认并暂停研究" }).click();
  await expect.poll(() => writes.slice(-4)).toEqual([
    "draft:pause",
    "preview",
    "confirm",
    "execute",
  ]);
  const resumedHeaderControl = page.getByRole("button", {
    name: "继续研究",
    exact: true,
  });
  await expect(resumedHeaderControl).toBeVisible();
  for (const viewport of [
    { width: 800, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(resumedHeaderControl).toBeVisible();
    const geometry = await page.evaluate(() => {
      const header = document.querySelector(".lumen-header")?.getBoundingClientRect();
      const control = document.querySelector(".lumen-research-power")?.getBoundingClientRect();
      if (!header || !control) throw new Error("missing header research control");
      return {
        headerRight: header.right,
        controlRight: control.right,
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
      };
    });
    expect(geometry.controlRight).toBeLessThanOrEqual(geometry.headerRight + 1);
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  }

  await resumedHeaderControl.click();
  const resumeConfirmation = page.getByRole("dialog", { name: "继续当前研究？" });
  await expect(resumeConfirmation).toBeVisible();
  await resumeConfirmation.getByRole("button", { name: "确认并继续研究" }).click();
  await expect(resumeConfirmation).toContainText(
    "Human Confirmation 已记录，执行尚未完成",
  );
  await expect(resumeConfirmation).toContainText("不会提交 Stage");
  await expect(resumeConfirmation.getByRole("button", { name: "重试执行继续" }))
    .toBeVisible();
  await page.reload({ waitUntil: "domcontentloaded" });
  const recoveredControl = page.getByRole("button", { name: "完成继续", exact: true });
  await expect(recoveredControl).toBeVisible();
  await recoveredControl.click();
  const recoveredConfirmation = page.getByRole("dialog", { name: "继续当前研究？" });
  await expect(recoveredConfirmation).toContainText("不会提交 Stage");
  await recoveredConfirmation.getByRole("button", { name: "重试执行继续" }).click();
  await expect.poll(() => writes.slice(-5)).toEqual([
    "draft:resume",
    "preview",
    "confirm",
    "execute",
    "execute",
  ]);
  await expect(page.getByRole("button", { name: "暂停研究", exact: true }))
    .toBeVisible();

  rejectNextConfirmationAsStale = true;
  const executeCountBeforeStale = writes.filter((item) => item === "execute").length;
  await page.getByRole("button", { name: "暂停研究", exact: true }).click();
  const staleConfirmation = page.getByRole("dialog", { name: "暂停当前研究？" });
  await staleConfirmation.getByRole("button", { name: "确认并暂停研究" }).click();
  await expect(staleConfirmation).toContainText("控制尚未确认，也不会执行");
  await expect(staleConfirmation).toContainText("command_preview_stale");
  await expect(staleConfirmation.getByRole("button", { name: "重新生成预览" }))
    .toBeVisible();
  expect(writes.filter((item) => item === "execute")).toHaveLength(
    executeCountBeforeStale,
  );
  nextPreviewError = "preview_temporarily_unavailable";
  await staleConfirmation.getByRole("button", { name: "重新生成预览" }).click();
  await expect(staleConfirmation).toContainText("preview_temporarily_unavailable");
  await expect(staleConfirmation.getByRole("button", { name: "确认并暂停研究" }))
    .toHaveCount(0);
  await staleConfirmation.getByRole("button", { name: "重新生成预览" }).click();
  await expect(staleConfirmation.getByRole("button", { name: "确认并暂停研究" }))
    .toBeVisible();
  await staleConfirmation.getByRole("button", { name: "暂不操作" }).click();

  nextExecutionError = "runtime_control_repreview_required";
  await page.getByRole("button", { name: "暂停研究", exact: true }).click();
  const repreviewConfirmation = page.getByRole("dialog", { name: "暂停当前研究？" });
  await repreviewConfirmation.getByRole("button", { name: "确认并暂停研究" }).click();
  await expect(repreviewConfirmation).toContainText("当前控制绑定已变化，原确认不会继续执行");
  await expect(repreviewConfirmation.getByRole("button", { name: "重新读取当前状态" }))
    .toBeVisible();
  await expect(repreviewConfirmation.getByRole("button", { name: "重试执行暂停" }))
    .toHaveCount(0);
  await repreviewConfirmation.getByRole("button", { name: "暂不操作" }).click();
  await page.getByRole("button", { name: "完成暂停", exact: true }).click();
  const reopenedRepreview = page.getByRole("dialog", { name: "暂停当前研究？" });
  await expect(reopenedRepreview.getByRole("button", { name: "重新读取当前状态" }))
    .toBeVisible();
  await expect(reopenedRepreview.getByRole("button", { name: "重试执行暂停" }))
    .toHaveCount(0);
  await reopenedRepreview.getByRole("button", { name: "重新读取当前状态" }).click();

  nextExecutionError = "command_preview_stale";
  await page.getByRole("button", { name: "暂停研究", exact: true }).click();
  const executionStale = page.getByRole("dialog", { name: "暂停当前研究？" });
  await executionStale.getByRole("button", { name: "确认并暂停研究" }).click();
  await expect(executionStale).toContainText("原确认不会继续执行");
  await expect(executionStale.getByRole("button", { name: "重新读取当前状态" }))
    .toBeVisible();
  await expect(executionStale.getByRole("button", { name: "重试执行暂停" }))
    .toHaveCount(0);
  await executionStale.getByRole("button", { name: "重新读取当前状态" }).click();
  await expect(page.getByRole("button", { name: "暂停研究", exact: true }))
    .toBeVisible();
});

test("production control seam preserves Companion order, focus, and responsive geometry", async ({
  page,
}) => {
  test.setTimeout(60_000);
  product = await DeterministicProduct.start({ manualRoot: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await openAuthenticatedProduct(page, product);

  const companion = page.getByRole("complementary", { name: "Quest Companion" });
  const control = companion.getByRole("region", { name: "研究控制" });
  const summary = control.locator("summary");
  const dynamicAuthorizationIdentities = companion.locator([
    ".lumen-broad-authorization p:first-of-type",
    ".lumen-broad-authorization code",
  ].join(", "));
  await expect(summary).toBeVisible();
  await expect(control.getByLabel("控制动作")).not.toBeVisible();
  await expect(dynamicAuthorizationIdentities).toHaveCount(2);

  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 800, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await summary.focus();
    await expect(summary).toBeFocused();
    await page.setViewportSize(viewport);
    await expect(summary).toBeFocused();
    const geometry = await page.evaluate(() => {
      const box = (selector: string) => {
        const rect = document.querySelector(selector)?.getBoundingClientRect();
        if (!rect) throw new Error(`missing ${selector}`);
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      };
      const chat = document.querySelector(".lumen-chat");
      const message = chat?.querySelector(".lumen-message");
      const researchControl = chat?.querySelector(".lumen-research-control");
      const compose = document.querySelector(".lumen-compose");
      if (!chat || !message || !researchControl || !compose) {
        throw new Error("missing Companion hierarchy");
      }
      return {
        main: box(".lumen-main"),
        companion: box(".lumen-companion"),
        messageBeforeControl: Boolean(
          message.compareDocumentPosition(researchControl)
          & Node.DOCUMENT_POSITION_FOLLOWING
        ),
        controlBeforeCompose: Boolean(
          researchControl.compareDocumentPosition(compose)
          & Node.DOCUMENT_POSITION_FOLLOWING
        ),
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: window.innerWidth,
      };
    });
    expect(geometry.messageBeforeControl).toBe(true);
    expect(geometry.controlBeforeCompose).toBe(true);
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    if (viewport.width === 1440) {
      expect(geometry.main.x + geometry.main.width).toBeLessThanOrEqual(
        geometry.companion.x + 1,
      );
    } else {
      expect(geometry.companion.y).toBeGreaterThanOrEqual(
        geometry.main.y + geometry.main.height - 1,
      );
      if (viewport.width === 390) {
        expect(geometry.companion.height).toBeGreaterThanOrEqual(560);
      } else {
        expect(geometry.companion.height).toBe(540);
      }
    }
    const screenshotOptions = {
      animations: "disabled" as const,
      mask: [
        dynamicAuthorizationIdentities.nth(0),
        dynamicAuthorizationIdentities.nth(1),
      ],
      maskColor: "#dfe4ee",
      maxDiffPixels: 0,
    };
    const screenshotName = `research-control-companion-${viewport.width}.png`;
    if (viewport.width === 390) {
      // Locator screenshots round both fractional document edges outward.  The
      // Companion follows a content-height main surface, so its mobile Y origin
      // can be fractional even though its audited 570px box is fixed.  Snap the
      // screenshot-only margin to the device-pixel grid, then assert the raw
      // locator image at zero tolerance.  A layout offset (rather than a
      // compositor transform) also makes text rasterize from the same origin.
      // Production layout remains unchanged after the capture.
      await companion.scrollIntoViewIfNeeded();
      await companion.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const shift = Math.round(rect.y) - rect.y;
        const marginTop = Number.parseFloat(getComputedStyle(element).marginTop);
        (element as HTMLElement).style.marginTop = `${marginTop + shift}px`;
      });
      await expect.poll(async () => companion.evaluate((element) => {
        const y = element.getBoundingClientRect().y;
        return Math.abs(y - Math.round(y));
      })).toBeLessThan(0.01);
      try {
        const screenshot = await companion.screenshot({
          animations: screenshotOptions.animations,
          mask: screenshotOptions.mask,
          maskColor: screenshotOptions.maskColor,
        });
        const dimensions = PNG.sync.read(screenshot);
        expect({ width: dimensions.width, height: dimensions.height }).toEqual({
          width: 374,
          height: 570,
        });
        expect(screenshot).toMatchSnapshot(screenshotName, {
          maxDiffPixels: screenshotOptions.maxDiffPixels,
        });
      } finally {
        await companion.evaluate((element) => {
          (element as HTMLElement).style.removeProperty("margin-top");
        });
      }
    } else {
      await expect(companion).toHaveScreenshot(
        screenshotName,
        screenshotOptions,
      );
    }
  }

  await page.setViewportSize({ width: 1440, height: 1000 });
  await summary.click();
  await expect(control.getByLabel("控制动作")).toHaveValue("pause");
  await control.getByRole("button", { name: "查看操作草案" }).click();
  const card = companion.locator(".lumen-command").filter({
    hasText: "暂停当前 Quest",
  });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "生成 Owner Impact Preview" }).click();
  await expect(card.getByRole("button", {
    name: "先展开并检查 Owner Impact Preview",
  })).toBeVisible();
  await card.getByText(
    "查看精确草案与 Owner Impact Preview",
    { exact: true },
  ).click();
  await expect(card.locator(".lumen-owner-previews")).toBeVisible();
  await card.getByRole("button", { name: "确认当前草案与预览" }).click();
  await expect(card).toContainText("确认已记录，控制尚未执行");
  await card.getByRole("button", { name: "执行已确认控制" }).click();
  await expect(card).toContainText("CONTROL EXECUTION · COMPLETED");
  await expect(control).toContainText("suspended");
});
