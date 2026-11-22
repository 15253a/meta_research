import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const webDist = resolve(process.env.META_RESEARCH_WEB_DIST ?? "../src/meta_research/web_dist");

test("assistant options stay collapsed and material controls retain readable dimensions", async ({ page }, testInfo) => {
  const snapshot = JSON.parse(readFileSync(resolve("tests/snapshot-before.json"), "utf8"));
  Object.assign(snapshot.human_collaboration.companion, {
    status: "ready", scope_ref: "quest:quest_chrome_1", session_ref: "readability-session",
    messages: [], agent_proposals: [], soft_constraints: [],
  });
  snapshot.human_collaboration.human_requests.items = [];
  const command = {
    ...snapshot.human_collaboration.commands.items[0],
    scope_ref: "quest:quest_chrome_1", status: "previewed", executed: false,
    confirmation_receipt: null, authorization: null,
    draft: { command_kind: "research_control", payload: {
      action: "pause", reason: "暂停当前实验，保留已有结果。",
      target: { quest_ref: "quest_chrome_1", cycle_ref: "cycle_private_identity", question_ref: "question_private_identity", epoch: 1 },
    } },
  };
  command.impact_preview.status = "current";
  command.impact_preview.owner_previews[0].will_happen = ["暂停当前研究"];
  command.impact_preview.owner_previews[0].will_not_happen = ["保留已有结果"];
  command.impact_preview.owner_previews[0].risks = [];
  command.impact_preview.owner_previews[0].stale_conditions = [];
  snapshot.human_collaboration.commands.items = [command];
  snapshot.human_collaboration.commands.authorizations = [];
  await page.setViewportSize({ width: 1440, height: 900 });
  const base = "http://localhost:19832";
  await page.route(`${base}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/snapshot") return route.fulfill({ json: snapshot });
    if (path.startsWith("/api/")) return route.fulfill({ json: {} });
    return route.fulfill({ body: readFileSync(resolve(webDist, path === "/" ? "index.html" : path.slice(1))), contentType: path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.goto(`${base}/?workspace=1`);
  const sidebar = page.getByRole("complementary", { name: "研究助手" });
  const options = sidebar.locator(".guidance-options");
  const material = options.getByRole("button", { name: "选择服务器文件或目录" });
  await expect(sidebar.getByLabel("给研究助手发消息")).toBeVisible();
  await expect(options).not.toHaveAttribute("open", "");
  await expect(material).toBeHidden();
  await options.locator("summary").click();
  await expect(material).toBeVisible();
  const dimensions = await material.evaluate(element => ({
    width: element.getBoundingClientRect().width,
    fontSize: Number.parseFloat(getComputedStyle(element).fontSize),
    overflowing: element.scrollWidth > element.clientWidth,
  }));
  expect(dimensions.width).toBeGreaterThan(130);
  expect(dimensions.fontSize).toBeGreaterThanOrEqual(13);
  expect(dimensions.overflowing).toBe(false);
  await options.locator("summary").click();
  await expect(material).toBeHidden();
  await expect(sidebar.getByLabel("给研究助手发消息")).toBeVisible();
  const card = sidebar.locator(".lumen-command").filter({ hasText: "暂停当前 Quest" });
  await expect(card).toBeVisible();
  await expect(card.locator(":scope > .assistant-fold")).not.toHaveAttribute("open", "");
  await expect(card.getByRole("button", { name: "请先查看操作与影响" })).toBeHidden();
  await card.locator(":scope > .assistant-fold > summary").click();
  expect(await card.innerText()).not.toContain("cycle_private_identity");
  expect(await card.innerText()).not.toContain("command_kind");
  await expect(card.getByRole("button", { name: "请先查看操作与影响" })).toBeDisabled();
  await card.getByText("查看操作与影响", { exact: true }).click();
  await expect(card.getByRole("button", { name: "确认当前草案与预览" })).toBeEnabled();
  await expect(card.getByText("暂停当前实验，保留已有结果。", { exact: true })).toBeVisible();
  expect(await card.innerText()).not.toContain("cycle_private_identity");
  await card.getByText("查看操作与影响", { exact: true }).click();
  await card.locator(":scope > .assistant-fold > summary").click();
  await sidebar.screenshot({ path: testInfo.outputPath("assistant-compact.png") });
});

test("completed operations fold into history while confirmed work stays actionable", async ({ page }, testInfo) => {
  const snapshot = JSON.parse(readFileSync(resolve("tests/snapshot-before.json"), "utf8"));
  const scope = "quest:assistant-history";
  Object.assign(snapshot.human_collaboration.companion, {
    status: "ready", scope_ref: scope, messages: [], agent_proposals: [], soft_constraints: [],
  });
  snapshot.human_collaboration.human_requests.items = [];
  const baseCommand = snapshot.human_collaboration.commands.items[0];
  const pending = { ...baseCommand, intent_id: "pending-control", scope_ref: scope, executed: false,
    status: "confirmed", authorization: null, control_execution: null,
    draft: { command_kind: "research_control", payload: { action: "resume", reason: "继续当前研究。",
      target: { quest_ref: "assistant-history", cycle_ref: "cycle-1", question_ref: "question-1", epoch: 1 } } },
  };
  const completed = { ...pending, intent_id: "completed-control", executed: true,
    control_execution: { status: "completed", receipt_ref: "completed-execution-receipt" },
  };
  const cancelled = { ...pending, intent_id: "cancelled-control", status: "cancelled", confirmation_receipt: null };
  snapshot.human_collaboration.commands.items = [completed, pending, cancelled];
  snapshot.human_collaboration.commands.authorizations = [];
  await page.setViewportSize({ width: 1440, height: 900 });
  const base = "http://localhost:19835";
  const writes: string[] = [];
  await page.route(`${base}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "GET") writes.push(path);
    if (path === "/api/v1/snapshot") return route.fulfill({ json: snapshot });
    if (path.startsWith("/api/")) return route.fulfill({ json: {} });
    return route.fulfill({ body: readFileSync(resolve(webDist, path === "/" ? "index.html" : path.slice(1))), contentType: path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.goto(`${base}/?workspace=1`);
  const sidebar = page.getByRole("complementary", { name: "研究助手" });
  const history = sidebar.locator(".assistant-operation-history");
  const current = sidebar.locator('[data-command-ref="pending-control"]');
  const finished = sidebar.locator('[data-command-ref="completed-control"]');
  await expect(current.locator(":scope > .assistant-fold")).not.toHaveAttribute("open", "");
  await current.locator(":scope > .assistant-fold > summary").click();
  await expect(current.getByRole("button", { name: "执行已确认控制" })).toBeVisible();
  await expect(current.getByRole("button", { name: "执行已确认控制" })).toBeEnabled();
  await expect(history.locator(":scope > summary")).toContainText("已处理操作2");
  await expect(finished).toBeHidden();
  await history.locator(":scope > summary").click();
  await expect(finished).toBeVisible();
  await expect(finished.locator(":scope > .assistant-fold")).not.toHaveAttribute("open", "");
  await finished.locator(":scope > .assistant-fold > summary").click();
  await expect(finished.getByRole("button")).toHaveCount(0);
  await expect(sidebar.locator('[data-command-ref="cancelled-control"]').getByRole("button")).toHaveCount(0);
  await finished.getByText("操作回执", { exact: true }).click();
  await finished.getByText("执行回执", { exact: true }).click();
  await expect(finished.getByText("completed-execution-receipt", { exact: true })).toBeVisible();
  await history.locator(":scope > summary").click();
  await expect(finished).toBeHidden();
  expect(writes).toEqual([]);
  await sidebar.screenshot({ path: testInfo.outputPath("assistant-operation-history.png") });
});

test("all non-dialogue assistant cards default to collapsed while dialogue stays open and previews expand cleanly", async ({ page }, testInfo) => {
  const snapshot = JSON.parse(readFileSync(resolve("tests/snapshot-before.json"), "utf8"));
  const scope = "quest:quest_chrome_1";
  Object.assign(snapshot.human_collaboration.companion, {
    status: "ready",
    scope_ref: scope,
    session_ref: "fold-session",
    messages: [
      { message_ref: "m-1", scope_ref: scope, role: "user", status: "completed", content: "请帮我确认这次实验只调整学习率。" },
      { message_ref: "m-2", scope_ref: scope, role: "assistant", status: "completed", content: "好的，我已整理出理解预览，你可以在下方展开核对并确认。" },
      { message_ref: "m-3", scope_ref: scope, role: "system", status: "completed", content: "上下文已同步到当前轮次。" },
    ],
    agent_proposals: [
      {
        proposal_ref: "proposal-fold-1",
        scope_ref: scope,
        proposal_hash: "a".repeat(64),
        status: "proposed",
        title: "仅调整本次实验学习率",
        proposal: {
          proposal_kind: "soft_constraint",
          text: "本次实验只改学习率，总目标不变。",
          assistant_understanding: "只在当前实验中调整学习率，保留其余条件与总目标。",
          applies_to: ["仅所选实验"],
          semantic_scope: { kind: "target", quest_ref: "quest_chrome_1", question_ref: "q1", cycle_ref: "c1", target_ref: "t1" },
          strength: 4,
          preserve_conditions: ["总目标不变"],
          work_materials: null,
        },
      },
      {
        proposal_ref: "proposal-old-converted",
        scope_ref: scope,
        proposal_hash: "b".repeat(64),
        status: "converted",
        title: "已转换的历史建议",
        proposal: {
          proposal_kind: "soft_constraint",
          text: "旧指导",
          assistant_understanding: "旧理解",
          applies_to: ["当前研究"],
          semantic_scope: { kind: "quest", quest_ref: "quest_chrome_1" },
          strength: 3,
          preserve_conditions: [],
          work_materials: null,
        },
      },
    ],
    soft_constraints: [
      {
        constraint_ref: "constraint-active-1",
        scope_ref: scope,
        status: "active",
        revision: 1,
        strength: 3,
        text: "优先采用可复现的公开基线配置。",
        guidance: { text: "优先采用可复现的公开基线配置。", assistant_understanding: "优先使用公开基线。", semantic_scope: { kind: "quest", quest_ref: "quest_chrome_1" } },
        deliveries: [],
      },
    ],
  });
  snapshot.human_collaboration.commands.items = [];
  snapshot.human_collaboration.commands.authorizations = [
    {
      authorization_ref: "auth-broad-1",
      receipt_ref: "receipt-broad-1",
      scope_ref: scope,
      quest_ref: "quest_chrome_1",
      authorization_kind: "broad_research",
      capability: "broad_research",
      decision: "granted",
      effective_decision: "granted",
      is_current: true,
      created_at: 1_700_000_000,
      requirement: { scope: { quest_ref: "quest_chrome_1" } },
    },
  ];
  await page.setViewportSize({ width: 1440, height: 900 });
  const base = "http://localhost:19836";
  await page.route(`${base}/**`, route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/snapshot") return route.fulfill({ json: snapshot });
    if (path.startsWith("/api/")) return route.fulfill({ json: {} });
    return route.fulfill({ body: readFileSync(resolve(webDist, path === "/" ? "index.html" : path.slice(1))), contentType: path.endsWith(".js") ? "text/javascript" : path.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.goto(`${base}/?workspace=1`);
  const sidebar = page.getByRole("complementary", { name: "研究助手" });
  await expect(sidebar).toContainText("请帮我确认这次实验只调整学习率。");
  await expect(sidebar).toContainText("好的，我已整理出理解预览，你可以在下方展开核对并确认。");
  const folds = sidebar.locator("details.assistant-fold");
  expect(await folds.count()).toBeGreaterThanOrEqual(4);
  for (const fold of await folds.all()) {
    await expect(fold).not.toHaveAttribute("open", "");
  }
  const review = sidebar.getByRole("article", { name: "确认研究指导" });
  await expect(review).toBeVisible();
  await expect(review).toContainText("理解预览 · 尚未交给研究");
  await expect(review.getByLabel("指导原文", { exact: true })).toBeHidden();
  await sidebar.screenshot({ path: testInfo.outputPath("assistant-all-folded.png") });
  await review.locator(":scope > .assistant-fold > summary").click();
  await expect(review.getByLabel("指导原文", { exact: true })).toBeVisible();
  await expect(review.getByLabel("助手理解", { exact: true })).toHaveValue("只在当前实验中调整学习率，保留其余条件与总目标。");
  await sidebar.screenshot({ path: testInfo.outputPath("assistant-preview-expanded.png") });
});
