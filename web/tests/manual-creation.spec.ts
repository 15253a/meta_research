import { expect, test, type Page } from "@playwright/test";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";

type QuestionTreeSnapshot = {
  question_tree: {
    status: "ready" | "unavailable";
    items: Array<{
      question_ref: string;
      quest_ref: string;
      parent_question_ref: string | null;
    }>;
  };
};

type ManualRawView = {
  context_ref: string;
  status: string;
  parent_question_ref: string;
  seed: null | {
    value: {
      accepted_material_bindings: Array<Record<string, unknown>>;
    };
  };
  question_anchor: null | {
    question_ref: string;
    parent_question_ref: string;
  };
};

let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 120_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start({ manualRoot: true });
});

test.afterEach(async () => {
  await product?.stop();
  product = undefined;
});

async function acceptedRoot(page: Page) {
  if (!product) throw new Error("deterministic product missing");
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const snapshot = await response.json() as QuestionTreeSnapshot;
  expect(snapshot.question_tree.status).toBe("ready");
  const root = snapshot.question_tree.items.find(
    (item) => item.parent_question_ref === null,
  );
  if (!root) throw new Error("manualRoot fixture did not publish a root Question");
  return root;
}

async function openQuestionTree(page: Page) {
  await page.getByRole("button", { name: "问题树", exact: true }).click();
  const tree = page.getByTestId("question-tree");
  await expect(tree).toBeVisible();
  return tree;
}

test("QuestionTree preserves the fixed canvas and Manual close-only boundaries", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page, product);
  const root = await acceptedRoot(page);
  const tree = await openQuestionTree(page);

  await expect(tree.getByLabel("可拖拽和缩放的 Quest 问题树画布")).toBeVisible();
  await expect(tree.getByLabel("问题树小地图")).toBeVisible();
  await expect(tree.getByRole("button", { name: "适配全图" })).toBeVisible();
  await expect(tree.getByRole("button", { name: "当前实验 · stdout" })).toBeDisabled();
  await expect(tree.getByRole("button", { name: `剪裁 ${root.question_ref}` })).toBeEnabled();

  const canvasWorld = tree.locator(".question-tree-canvas-world");
  const beforeZoom = await canvasWorld.getAttribute("style");
  await tree.getByRole("button", { name: "放大问题树" }).click();
  await expect.poll(() => canvasWorld.getAttribute("style")).not.toBe(beforeZoom);
  await tree.getByRole("button", { name: "适配画布", exact: true }).click();

  let cancelRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      /\/api\/v1\/manual-question-creations\/[^/]+\/cancel$/.test(
        new URL(request.url()).pathname,
      )
    ) {
      cancelRequests += 1;
    }
  });

  const opener = tree.getByRole("button", {
    name: `在 ${root.question_ref} 下创建子问题`,
  });
  const rootNode = tree.locator(`[data-question-ref="${root.question_ref}"]`);
  await rootNode.hover();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await expect(dialog).toBeVisible();
  expect(new URL(page.url()).searchParams.get("node")).toBe(root.question_ref);
  expect(new URL(page.url()).searchParams.get("panel")).toBe("create-question");
  await dialog.getByRole("button", { name: "关闭创建 Question 窗口" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  expect(cancelRequests).toBe(0);
  expect(new URL(page.url()).searchParams.get("panel")).toBe("question-tree");

  await rootNode.hover();
  await opener.click();
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  expect(cancelRequests).toBe(0);

  await rootNode.hover();
  await opener.click();
  await expect(dialog).toBeVisible();
  await page.mouse.click(2, 2);
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  expect(cancelRequests).toBe(0);

  await page.goto(
    `${product.baseUrl}/?variant=A&view=questions&node=${encodeURIComponent(root.question_ref)}&panel=create-question`,
    { waitUntil: "domcontentloaded" },
  );
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".manual-parent-context code")).toContainText(
    root.question_ref,
  );
  await dialog.getByRole("button", { name: "关闭创建 Question 窗口" }).click();
  await expect(dialog).toBeHidden();
  expect(cancelRequests).toBe(0);
});

test("ManualCreation intakes real material before Seed and reloads the completed child", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page, product);
  const root = await acceptedRoot(page);
  const tree = await openQuestionTree(page);
  const opener = tree.getByRole("button", {
    name: `在 ${root.question_ref} 下创建子问题`,
  });
  await tree.locator(`[data-question-ref="${root.question_ref}"]`).hover();
  await opener.click();

  const dialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await expect(dialog).toBeVisible();
  await expect(dialog.locator("#manual-seed-intent")).toHaveAttribute(
    "maxlength",
    "12000",
  );
  await dialog.locator("#manual-seed-intent").fill(
    "比较稀有细胞形态在不同低照度去噪路线下是否仍可辨认。",
  );
  await dialog.getByLabel("问题标题，可选", { exact: true }).fill(
    "低照度去噪的稀有形态保真边界",
  );
  await dialog.getByLabel("要解决的未知，可选", { exact: true }).fill(
    "不同去噪路线何时会抹除稀有细胞形态？",
  );
  await dialog.getByLabel("合格答案的形状，可选", { exact: true }).fill(
    "给出路线比较、失真边界与反例。",
  );
  await dialog.getByLabel("适用范围与排除项，可选", { exact: true }).fill(
    "适用于低照度显微图像；排除合成对象。",
  );
  await dialog.locator("#manual-material-files").setInputFiles({
    name: "manual-evidence.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# exact browser material\nrare morphology evidence\n"),
  });

  const commandOrder: string[] = [];
  let projectionStreamCount = 0;
  let seedRequest: unknown;
  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path === "/api/v1/events") {
      projectionStreamCount += 1;
    }
    if (request.method() === "POST" && path === "/api/v1/research-assets/intakes") {
      commandOrder.push("asset-intake");
    }
    if (request.method() === "POST" && path.endsWith("/seed-confirmation")) {
      commandOrder.push("seed-confirmation");
      seedRequest = request.postDataJSON();
    }
    if (request.method() === "PUT" && path.endsWith("/proposal")) {
      commandOrder.push("proposal-save");
    }
    if (request.method() === "POST" && path.endsWith("/proposal-confirmation")) {
      commandOrder.push("proposal-confirmation");
    }
  });

  await dialog.getByRole("button", {
    name: "确认当前 Seed，开始讨论",
  }).click();
  await expect(dialog.getByText("CreationSeed 已冻结", { exact: true })).toBeVisible();
  expect(commandOrder.slice(0, 2)).toEqual(["asset-intake", "seed-confirmation"]);
  const seed = (seedRequest as {
    seed: { accepted_material_bindings: Array<Record<string, unknown>> };
  }).seed;
  expect(seed.accepted_material_bindings).toHaveLength(1);
  const binding = seed.accepted_material_bindings[0];
  expect(Object.keys(binding).sort()).toEqual([
    "asset_ref",
    "content_hash",
    "manifest_hash",
    "receipt",
    "version_ref",
  ]);
  expect(Object.keys(binding.receipt as Record<string, unknown>).sort()).toEqual([
    "issuer",
    "kind",
    "payload_hash",
    "receipt_ref",
    "status",
    "subject_ref",
  ]);

  const currentResponse = await page.request.get(
    `${product.baseUrl}/api/v1/manual-question-creations/current?quest_ref=${encodeURIComponent(root.quest_ref)}&parent_question_ref=${encodeURIComponent(root.question_ref)}`,
  );
  expect(currentResponse.ok()).toBeTruthy();
  const current = await currentResponse.json() as ManualRawView;
  expect(current.seed?.value.accepted_material_bindings).toHaveLength(1);

  await dialog.getByRole("button", {
    name: "确认本次不运行 DeepFetch",
  }).click();
  await expect(dialog.getByText(/explicit waiver · accepted/)).toBeVisible();
  const message = dialog.getByLabel("在 Question Drafting Session 中发消息");
  await expect(message).toHaveAttribute("maxlength", "12000");
  await message.fill("请检查答案形状是否明确包含反例。 ");
  await dialog.getByRole("button", { name: "发送消息" }).click();
  await expect(dialog.locator(".manual-message.user")).toContainText(
    "请检查答案形状是否明确包含反例。",
  );

  const title = dialog.getByLabel("问题标题", { exact: true });
  await expect(title).toHaveAttribute("maxlength", "500");
  await title.fill("unknown");
  await expect(dialog.getByRole("button", { name: "确认最终问题" })).toBeDisabled();
  await title.fill("低照度去噪的稀有形态保真边界");
  await expect(dialog.getByRole("button", { name: "确认最终问题" })).toBeEnabled();
  await dialog.getByRole("button", { name: "确认最终问题" }).click();
  await expect.poll(() => commandOrder.includes("proposal-confirmation")).toBe(true);
  expect(projectionStreamCount).toBeLessThanOrEqual(2);

  await expect.poll(async () => {
    const response = await page.request.get(
      `${product?.baseUrl}/api/v1/manual-question-creations/${encodeURIComponent(current.context_ref)}`,
    );
    if (!response.ok()) return `http-${response.status()}`;
    return ((await response.json()) as ManualRawView).status;
  }, { timeout: 30_000 }).toBe("completed");
  await expect(dialog.getByText(/稳定 QuestionAnchor/)).toBeVisible();

  const detailResponse = await page.request.get(
    `${product.baseUrl}/api/v1/manual-question-creations/${encodeURIComponent(current.context_ref)}`,
  );
  const completed = await detailResponse.json() as ManualRawView;
  expect(completed.question_anchor?.parent_question_ref).toBe(root.question_ref);

  await dialog.getByRole("button", { name: "关闭创建 Question 窗口" }).click();
  await expect(dialog).toBeHidden();
  await expect(
    page.getByRole("button", {
      name: `在 ${completed.question_anchor?.question_ref} 下创建子问题`,
    }),
  ).toBeVisible();
});
