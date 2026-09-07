import { expect, test, type Page } from "@playwright/test";
import { DeterministicProduct, openAuthenticatedProduct } from "./support/deterministic-product";
import type { ManualQuestionCreationRawView } from "../src/api";

let product: DeterministicProduct;
test.beforeEach(async () => { product = await DeterministicProduct.start({ manualRoot: true }); });
test.afterEach(async ({ page }) => { await page.close(); await product?.stop(); });

async function openManual(page: Page) {
  await openAuthenticatedProduct(page, product);
  const snapshot = await (await page.request.get(`${product.baseUrl}/api/v1/snapshot`)).json();
  const root = snapshot.question_tree.items.find((item: { parent_question_ref: string | null }) => item.parent_question_ref === null);
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  await tree.locator(`[data-question-ref="${root.question_ref}"]`).hover();
  await tree.getByRole("button", { name: `在 ${root.question_ref} 下创建子问题` }).click();
  const dialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await dialog.locator("#manual-seed-intent").fill("比较不同路线的边界与反例");
  await dialog.getByRole("button", { name: "确认当前 Seed，开始讨论" }).click();
  await expect(dialog.getByLabel("在 Question Drafting Session 中发消息")).toBeEnabled();
  const current = await (await page.request.get(`${product.baseUrl}/api/v1/manual-question-creations/current?quest_ref=${encodeURIComponent(root.quest_ref)}&parent_question_ref=${encodeURIComponent(root.question_ref)}`)).json() as ManualQuestionCreationRawView;
  return { dialog, current };
}

test("manual draft echoes the message while its POST is pending and restores it on failure", async ({ page }) => {
  const { dialog } = await openManual(page);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let posts = 0;
  await page.route("**/drafting-session/messages", async (route) => {
    posts += 1;
    await gate;
    await route.fulfill({ status: 503, json: { error: { code: "temporarily_unavailable" } } });
  });
  const input = dialog.getByLabel("在 Question Drafting Session 中发消息");
  await input.fill("立即显示这条讨论消息");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  try {
    await expect.poll(() => posts).toBe(1);
    await expect(dialog.locator(".manual-message.user")).toContainText("立即显示这条讨论消息", { timeout: 750 });
    await expect(input).toHaveValue("");
    await expect(dialog.getByRole("button", { name: "发送消息" })).toBeDisabled();
    await expect(dialog.getByText("正在准备回复…", { exact: true })).toBeVisible();
  } finally { release(); }
  await expect(input).toBeEnabled();
  await expect(input).toHaveValue("立即显示这条讨论消息");
  await expect(dialog.locator(".manual-message.user")).toHaveCount(0);
  expect(posts).toBe(1);
});

test("manual draft renders real reply chunks then replaces them with one durable answer", async ({ page }) => {
  await page.addInitScript(() => {
    const native = window.EventSource;
    const streams = new Map<string, { closed: boolean; emit(value: object): void }>();
    Object.assign(window, { manualReplyStreams: streams });
    window.EventSource = new Proxy(native, {
      construct(target, args: ConstructorParameters<typeof EventSource>) {
        const path = String(args[0]);
        if (!path.includes("/drafting-session/turns/")) return Reflect.construct(target, args);
        const stream = {
          closed: false,
          onmessage: null as null | ((event: MessageEvent<string>) => void),
          close() { this.closed = true; },
          emit(value: object) { if (!this.closed) this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) })); },
        };
        streams.set(path, stream);
        return stream;
      },
    });
  });
  const { dialog, current } = await openManual(page);
  const turn = { ref: "manual-stream-turn", ordinal: 1, basis_hash: current.research_path.basis_hash ?? current.seed!.hash, user_content: "逐段解释这个问题", assistant_status: "running" as const, assistant_content: null, reason: null };
  let projected = { ...current, drafting_session: { ...current.drafting_session!, turns: [turn] } } as ManualQuestionCreationRawView;
  await page.route(`**/api/v1/manual-question-creations/${current.context_ref}`, (route) => route.fulfill({ json: projected }));
  await page.route("**/drafting-session/messages", (route) => route.fulfill({ json: projected }));
  const input = dialog.getByLabel("在 Question Drafting Session 中发消息");
  await input.fill(turn.user_content);
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { manualReplyStreams: Map<string, unknown> }).manualReplyStreams.size)).toBe(1);
  async function emit(text: string, status: string) {
    await page.evaluate(({ text, status }) => {
      const streams = (window as unknown as { manualReplyStreams: Map<string, { emit(value: object): void }> }).manualReplyStreams;
      streams.values().next().value!.emit({ turn_ref: "manual-stream-turn", text, status });
    }, { text, status });
  }
  await emit("第一段：先明确研究边界。", "running");
  await expect(dialog.locator(".manual-message.assistant")).toContainText("第一段：先明确研究边界。");
  await emit("第一段：先明确研究边界。\n第二段：再列出反例。", "running");
  await expect(dialog.locator(".manual-message.assistant")).toContainText("第二段：再列出反例。");
  await expect(input).toBeDisabled();
  projected = { ...projected, drafting_session: { ...projected.drafting_session!, turns: [{ ...turn, assistant_status: "completed", assistant_content: "已完成的正式回答。" }] } };
  await emit("已完成的正式回答。", "completed");
  await expect(dialog.locator(".manual-message.assistant")).toContainText("已完成的正式回答。");
  await expect(dialog.locator(".manual-message.assistant")).toHaveCount(1);
  await expect(dialog.locator(".manual-message.user")).toHaveCount(1);
  await expect(input).toBeEnabled();
});
