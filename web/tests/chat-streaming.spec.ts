import { expect, test, type Page } from "@playwright/test";
import { DeterministicProduct, openAuthenticatedProduct } from "./support/deterministic-product";
import type { QuestCreationView, PublicSnapshot, CompanionMessage } from "../src/api";

let product: DeterministicProduct;
test.beforeEach(async () => { product = await DeterministicProduct.start(); });
test.afterEach(async ({ page }) => {
  await page.close();
  await product?.stop();
});

type ControlledStream = { closed: boolean; emit: (value: object) => void };
declare global {
  interface Window { replyStreams: Map<string, ControlledStream> }
}

async function controlReplyStreams(page: Page) {
  await page.addInitScript(() => {
    const native = window.EventSource;
    const replyStreams = new Map<string, ControlledStream>();
    window.replyStreams = replyStreams;
    window.EventSource = new Proxy(native, {
      construct(target, args: ConstructorParameters<typeof EventSource>) {
        const path = String(args[0]);
        if (!path.includes("/stream")) return Reflect.construct(target, args);
        const stream = {
          closed: false,
          onmessage: null as null | ((event: MessageEvent<string>) => void),
          close() { this.closed = true; },
          emit(value: object) {
            if (!this.closed) this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) }));
          },
        };
        replyStreams.set(path, stream);
        return stream;
      },
    });
  });
}

async function emitReply(page: Page, pathPart: string, text: string, status = "processing") {
  await expect.poll(() => page.evaluate((part) =>
    [...window.replyStreams.keys()].some((path) => path.includes(part)), pathPart,
  )).toBe(true);
  await page.evaluate(({ pathPart: part, text: content, status: state }) => {
    const match = [...window.replyStreams].find(([path]) => path.includes(part));
    match![1].emit({ text: content, status: state });
  }, { pathPart, text, status });
}

test("draft conversation displays successive reply chunks before the final durable answer", async ({ page }) => {
  await controlReplyStreams(page);
  const session = await openDraft(page);
  await page.getByRole("textbox", { name: "目标", exact: true }).fill("解释这个草案的研究边界");
  await expect(page.getByText("草案已自动保存", { exact: true })).toBeVisible();
  const response = await page.request.get(`${product.baseUrl}/api/v1/quest-initializations/current`);
  const view = await response.json() as QuestCreationView;
  const message = "流式解释我的草案";
  const turn = {
    ref: "stream-turn", ordinal: 1,
    basis_revision: view.quest_draft.revision, basis_hash: view.quest_draft.hash,
    user_content: message, user_content_hash: "a".repeat(64),
    assistant_status: "running" as const, assistant_content: null,
    assistant_content_hash: null, reason: null,
  };
  let projected = { ...view, intent_session: { ...view.intent_session!, turns: [turn] } } as QuestCreationView;
  await page.route(`**/api/v1/quest-initializations/${view.initialization_id}`, (route) => route.fulfill({ json: projected }));
  await page.route("**/intent-session/messages", (route) => route.fulfill({ json: projected }));
  await session.getByLabel("在 Quest Drafting Session 中发消息").fill(message);
  await session.getByRole("button", { name: "发送消息" }).click();
  await emitReply(page, "/turns/stream-turn/stream", "第一段：先明确研究边界。");
  const reply = session.locator(".quest-intent-message:not(.user)");
  await expect(reply).toContainText("第一段：先明确研究边界。");
  await expect(session.getByRole("button", { name: "发送消息" })).toBeDisabled();
  await emitReply(page, "/turns/stream-turn/stream", "第一段：先明确研究边界。第二段：再判断材料是否足够。");
  await expect(reply).toContainText("第二段：再判断材料是否足够。");
  projected = { ...projected, intent_session: { ...projected.intent_session!, turns: [{ ...turn, assistant_status: "completed", assistant_content: "正式完成的回答。" }] } };
  await emitReply(page, "/turns/stream-turn/stream", "正式完成的回答。", "completed");
  await expect(reply).toContainText("正式完成的回答。");
  await expect(session.locator(".quest-intent-message.user")).toHaveCount(1);
  await expect(reply).toHaveCount(1);
  await expect(session.getByLabel("在 Quest Drafting Session 中发消息")).toBeEnabled();
});

test("companion conversation shows reply chunks and replaces them with one durable answer", async ({ page }) => {
  await controlReplyStreams(page);
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  const snapshot = await response.json() as PublicSnapshot;
  const messages: CompanionMessage[] = [];
  snapshot.human_collaboration!.companion = {
    status: "ready", scope_ref: "quest_chrome_1", session_ref: "stream-session", messages,
    soft_constraints: [], agent_proposals: [],
  };
  snapshot.human_collaboration!.human_requests.items = [];
  await page.route("**/api/v1/events*", (route) => route.abort());
  await page.route("**/api/v1/snapshot", (route) => route.fulfill({ json: snapshot }));
  await page.route("**/api/v1/companion/messages", (route) => {
    messages.push(
      { message_ref: "stream-interaction:user", scope_ref: "quest_chrome_1", role: "user", content: "现在逐步解释", status: "completed" },
      { message_ref: "stream-interaction:assistant", scope_ref: "quest_chrome_1", role: "assistant", content: "", status: "processing" },
    );
    snapshot.revision += 1;
    return route.fulfill({ json: { interaction_ref: "stream-interaction", status: "queued" } });
  });
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  const session = page.getByRole("complementary", { name: "研究助手" });
  const input = session.getByLabel("给研究助手发消息");
  await input.fill("现在逐步解释");
  await session.getByRole("button", { name: "发送消息" }).click();
  await expect(input).toHaveValue("");
  await emitReply(page, "/messages/stream-interaction/stream", "先看看已经完成的研究。");
  const reply = session.locator(".lumen-message:not(.me)");
  await expect(reply).toContainText("先看看已经完成的研究。");
  await emitReply(page, "/messages/stream-interaction/stream", "先看看已经完成的研究。接着核实剩下的问题。");
  await expect(reply).toContainText("接着核实剩下的问题。");
  messages[1] = { ...messages[1], status: "completed", content: "研究助手的正式回答。" };
  snapshot.revision += 1;
  await emitReply(page, "/messages/stream-interaction/stream", "研究助手的正式回答。", "completed");
  await expect(reply).toContainText("研究助手的正式回答。");
  await expect(reply).toHaveCount(1);
  await expect(session.locator(".lumen-message.me")).toHaveCount(1);
  await expect(session.getByText("正在回复…", { exact: true })).toHaveCount(0);
});

async function openDraft(page: Page) {
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "创建研究任务", exact: true }).click();
  const session = page.getByRole("complementary", { name: "讨论 Quest 与第一问" });
  await expect(session.getByLabel("在 Quest Drafting Session 中发消息")).toBeEnabled();
  return session;
}

test("draft conversation echoes and clears the message while its POST is still pending", async ({ page }) => {
  const session = await openDraft(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let posts = 0;
  await page.route("**/intent-session/messages", async (route) => {
    posts += 1;
    await gate;
    await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: { code: "temporarily_unavailable" } }) });
  });
  const input = session.getByLabel("在 Quest Drafting Session 中发消息");
  await input.fill("先立即显示我的草案消息");
  await session.getByRole("button", { name: "发送消息" }).click();
  try {
    await expect.poll(() => posts).toBe(1);
    await expect(session.locator(".quest-intent-message.user")).toContainText("先立即显示我的草案消息", { timeout: 750 });
    await expect(input).toHaveValue("");
    await expect(session.getByRole("button", { name: "发送消息" })).toBeDisabled();
    await expect(session.getByText("正在准备回复…", { exact: true })).toBeVisible();
  } finally { release(); }
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue("先立即显示我的草案消息");
  await expect(session.locator(".quest-intent-message.user")).toHaveCount(0);
  expect(posts).toBe(1);
});

test("companion keeps the accepted message and streams while the snapshot is still stale", async ({ page }) => {
  await controlReplyStreams(page);
  await product.authenticate(page);
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  const snapshot = await response.json() as PublicSnapshot;
  snapshot.human_collaboration!.companion = {
    status: "ready", scope_ref: "quest_chrome_1", session_ref: "stale-session", messages: [],
    soft_constraints: [], agent_proposals: Array.from({ length: 3 }, (_, index) => ({
      proposal_ref: `old-proposal-${index}`, scope_ref: "quest_chrome_1", proposal_hash: "1".repeat(64),
      title: "现有研究建议", summary: "这是现有研究建议的详细说明。".repeat(30),
      proposal: { proposal_kind: "narrow_scope", text: "继续使用公开材料。" }, status: "proposed",
    })),
  };
  snapshot.human_collaboration!.human_requests.items = [];
  await page.route("**/api/v1/events*", (route) => route.abort());
  await page.route("**/api/v1/snapshot", (route) => route.fulfill({ json: snapshot }));
  await page.route("**/api/v1/companion/messages", (route) =>
    route.fulfill({ json: { interaction_ref: "stale-interaction", status: "queued" } }));
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  const session = page.getByRole("complementary", { name: "研究助手" });
  const input = session.getByLabel("给研究助手发消息");
  await input.fill("快照更新慢也请保留我的消息");
  await session.getByRole("button", { name: "发送消息" }).click();
  await expect(input).toHaveValue("");
  await expect(session.locator(".lumen-message.me")).toContainText("快照更新慢也请保留我的消息", { timeout: 750 });
  await expect(session.locator(".lumen-message.me")).toBeInViewport();
  await emitReply(page, "/messages/stale-interaction/stream", "回复已开始。");
  await expect(session.locator(".lumen-message:not(.me)")).toContainText("回复已开始。", { timeout: 750 });
  await expect(session.locator(".lumen-message:not(.me)")).toBeInViewport();
  await emitReply(page, "/messages/stale-interaction/stream", "回复已开始。\n第二段也已输出。");
  await expect(session.locator(".lumen-message:not(.me)")).toContainText("第二段也已输出。");
  await expect(session.locator(".lumen-message:not(.me)")).toBeInViewport();
  await expect(session.locator(".lumen-message.me")).toHaveCount(1);
});
