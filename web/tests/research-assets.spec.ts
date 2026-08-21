import { expect, test, type Page } from "@playwright/test";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 90_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start();
});

test.afterEach(async () => {
  await product?.stop();
  product = undefined;
});

test("Research Asset is a real responsive intake, inventory, and receipt surface", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page, product);

  const opener = page.getByRole("button", { name: "Research Asset", exact: true });
  await expect(opener).toBeEnabled();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "关闭 Research Asset 工作台" })).toBeFocused();

  const kinds = await dialog.getByLabel("Research Asset 来源类型").locator("option").allTextContents();
  expect(kinds).toEqual([
    "文本",
    "文件上传",
    "本地目录",
    "本地路径",
    "代码仓库",
    "链接",
    "系统产物",
  ]);

  await dialog.getByLabel("Research Asset 显示名称").fill("accepted-browser-note.md");
  await dialog.getByLabel("Research Asset 原始文本").fill(
    "# Browser accepted asset\n\nThese exact bytes must remain addressable.\n",
  );
  await dialog.getByRole("button", { name: "提交 Asset Intake" }).click();
  await expect(dialog.getByText("已接纳精确版本", { exact: false })).toBeVisible();
  await expect(dialog.getByText("asset_acceptance", { exact: true }).first()).toBeVisible();

  const item = dialog.getByRole("listitem").filter({ hasText: "accepted-browser-note.md" });
  await expect(item).toContainText("integrity · verified");
  await expect(item).toContainText("availability · available");
  await item.click();
  await expect(dialog.getByText("MemoryRef · exact, never latest", { exact: true })).toBeVisible();

  await dialog.getByText("Hold 与 ReleaseEligibility", { exact: true }).click();
  await dialog.getByRole("button", { name: "放置 Hold" }).click();
  await expect(dialog.getByText("Hold 已由 Research Memory 接纳", { exact: false })).toBeVisible();
  await dialog.getByRole("button", { name: /检查 ReleaseEligibility/ }).click();
  await expect(dialog.getByText("fail closed · active_hold", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "释放当前 Hold" }).click();
  await expect(dialog.getByText("Hold release receipt 已形成", { exact: false })).toBeVisible();

  const beforeBrowse = await ownerRevision(page, product.baseUrl, "research_memory");
  await dialog.getByRole("button", { name: "刷新" }).click();
  const downloadPromise = page.waitForEvent("download");
  await dialog.getByRole("link", { name: "只读下载" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("accepted-browser-note.md");
  const afterBrowse = await ownerRevision(page, product.baseUrl, "research_memory");
  expect(afterBrowse).toBe(beforeBrowse);

  for (const viewport of [
    { width: 800, height: 900 },
    { width: 390, height: 844 },
    { width: 1440, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(dialog).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test("Quest creation selects an exact accepted version instead of a raw browser path", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await acceptTextAsset(page, "quest-source.md", "exact Quest source material\n");

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  await expect(quest).toBeVisible();
  await quest.getByText("补充范围、排除项与已有材料", { exact: true }).click();
  const selectFile = quest.getByRole("button", { name: "选择文件", exact: true });
  await expect(selectFile).toBeEnabled();
  await selectFile.click();
  const picker = quest.getByRole("group", { name: "选择已接纳 Research Asset" });
  const accepted = picker.getByRole("button").filter({ hasText: "quest-source.md" });
  await accepted.click();
  await expect(accepted).toHaveAttribute("aria-pressed", "true");
  await quest.getByLabel("文献搜索范围").selectOption("provided_only");

  await expect.poll(async () => {
    const response = await page.request.get(`${product?.baseUrl}/api/v1/quest-initializations/current`);
    const current = await response.json() as {
      quest_draft: {
        value: {
          literature: { accepted_material_bindings: Array<Record<string, unknown>> };
        };
      };
    };
    return current.quest_draft.value.literature.accepted_material_bindings;
  }).toEqual([
    expect.objectContaining({
      asset_ref: expect.any(String),
      version_ref: expect.any(String),
      content_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      manifest_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      receipt: expect.objectContaining({
        issuer: "research_memory",
        kind: "asset_acceptance",
      }),
    }),
  ]);

  await quest.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(quest).toBeHidden();
  await page.getByRole("button", { name: "创建 Quest", exact: true }).click();
  await expect(quest).toBeVisible();
  await quest.getByText("补充范围、排除项与已有材料", { exact: true }).click();
  await quest.getByRole("button", { name: "选择文件", exact: true }).click();
  await expect(
    quest.getByRole("group", { name: "选择已接纳 Research Asset" })
      .getByRole("button")
      .filter({ hasText: "quest-source.md" }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("paged inventory and Quest materials keep an exact off-page version reachable", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  const csrf = (await page.context().cookies()).find(
    (cookie) => cookie.name === "meta_research_csrf",
  )?.value;
  if (!csrf) throw new Error("csrf cookie missing");
  for (let index = 0; index < 52; index += 1) {
    const response = await page.request.post(
      `${product.baseUrl}/api/v1/research-assets/intakes`,
      {
        headers: {
          Origin: product.baseUrl,
          "X-CSRF-Token": csrf,
          "Idempotency-Key": `browser-page-${index}`,
        },
        data: {
          source_kind: "text",
          custody_mode: "managed",
          display_name: `paged-${String(index).padStart(2, "0")}.txt`,
          text: `paged exact version ${index}\n`,
        },
      },
    );
    expect(response.status()).toBe(201);
  }
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Research Asset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await expect(dialog.getByText("50 / 52 versions", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: /加载更多（已显示 50 \/ 52）/ }).click();
  await expect(dialog.getByText("52 / 52 versions", { exact: true })).toBeVisible();

  const offPage = dialog.getByRole("listitem").filter({ hasText: "paged-00.txt" });
  await offPage.click();
  await expect(
    dialog.getByRole("region", { name: "Research Asset 版本详情" }),
  ).toContainText("paged-00.txt");
  await dialog.getByText("Hold 与 ReleaseEligibility", { exact: true }).click();
  await dialog.getByRole("button", { name: "放置 Hold" }).click();
  await expect(dialog.getByText("asset_hold_placed", { exact: true }).last()).toBeVisible();
  await expect(
    dialog.getByRole("region", { name: "Research Asset 版本详情" }),
  ).toContainText("paged-00.txt");
  await dialog.getByRole("button", { name: /加载更多（已显示 51 \/ 52）/ }).click();
  await expect(dialog.getByText("52 / 52 versions", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("listitem")).toHaveCount(52);

  await dialog.getByRole("button", { name: "关闭 Research Asset 工作台" }).click();
  await page.getByRole("button", { name: "创建 Quest", exact: true }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await quest.getByText("补充范围、排除项与已有材料", { exact: true }).click();
  await quest.getByRole("button", { name: "选择文件", exact: true }).click();
  const picker = quest.getByRole("group", { name: "选择已接纳 Research Asset" });
  await expect(
    picker.getByRole("button").filter({ hasText: "paged-00.txt" }),
  ).toHaveCount(0);
  await picker.getByRole("button", { name: /加载更多已接纳版本/ }).click();
  await expect(
    picker.getByRole("button").filter({ hasText: "paged-00.txt" }),
  ).toBeVisible();
});

test("an authorization interruption retains the durable intake pointer and resumes", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Research Asset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await dialog.getByRole("checkbox", { name: /异步接纳/ }).check();
  await dialog.getByLabel("Research Asset 显示名称").fill("durable-auth-note.md");
  await dialog.getByLabel("Research Asset 原始文本").fill("durable auth recovery\n");

  let interrupted = false;
  await page.route("**/api/v1/research-assets/intakes/*", async (route) => {
    if (!interrupted && route.request().method() === "GET") {
      interrupted = true;
      await route.fulfill({ status: 401, contentType: "application/json", body: "{}" });
      return;
    }
    await route.fallback();
  });

  await dialog.getByRole("button", { name: "提交 Asset Intake" }).click();
  await expect(dialog.getByText("request_failed:401", { exact: true })).toBeVisible();
  await expect.poll(async () => page.evaluate(() =>
    window.sessionStorage.getItem("meta_research_pending_asset_intake"),
  )).not.toBeNull();

  await page.unroute("**/api/v1/research-assets/intakes/*");
  await page.reload({ waitUntil: "domcontentloaded" });
  const resumed = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await expect(resumed).toBeVisible();
  await expect(resumed.getByText("已接纳精确版本", { exact: false })).toBeVisible();
  await expect.poll(async () => page.evaluate(() =>
    window.sessionStorage.getItem("meta_research_pending_asset_intake"),
  )).toBeNull();
  await expect(resumed.getByRole("listitem").filter({ hasText: "durable-auth-note.md" }))
    .toContainText("integrity · verified");
});

test("an accepted Hold remains truthful when Projection refresh is interrupted", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await acceptTextAsset(page, "refresh-recovery.md", "accepted before refresh failure\n");
  await page.getByRole("button", { name: "Research Asset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await dialog.getByRole("listitem").filter({ hasText: "refresh-recovery.md" }).click();
  await dialog.getByText("Hold 与 ReleaseEligibility", { exact: true }).click();

  let interrupted = false;
  await page.route(/\/api\/v1\/research-assets(?:\?.*)?$/, async (route) => {
    if (!interrupted && route.request().method() === "GET") {
      interrupted = true;
      await route.fulfill({ status: 503, contentType: "application/json", body: "{}" });
      return;
    }
    await route.fallback();
  });

  await dialog.getByRole("button", { name: "放置 Hold" }).click();
  await expect(dialog.getByText("Hold 已由 Research Memory 接纳", { exact: false }))
    .toBeVisible();
  await expect(dialog.getByText("Projection 刷新待恢复", { exact: false })).toBeVisible();
  await expect(dialog.getByText("asset_hold_placed", { exact: true }).last()).toBeVisible();
  await expect(dialog.getByText("操作未完成", { exact: true })).toHaveCount(0);
});

test("an empty file remains a valid exact AssetVersion", async ({ page }) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Research Asset", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await dialog.getByLabel("Research Asset 来源类型").selectOption("file");
  await dialog.getByLabel("Research Asset 本地文件").setInputFiles({
    name: "empty-observation.txt",
    mimeType: "text/plain",
    buffer: Buffer.alloc(0),
  });
  await dialog.getByRole("button", { name: "提交 Asset Intake" }).click();
  await expect(dialog.getByText("已接纳精确版本", { exact: false })).toBeVisible();
  const item = dialog.getByRole("listitem").filter({ hasText: "empty-observation.txt" });
  await expect(item).toContainText("integrity · verified");
  await item.click();
  await expect(
    dialog.getByRole("region", { name: "Research Asset 版本详情" })
      .getByText("0", { exact: true }),
  ).toBeVisible();
});

async function acceptTextAsset(page: Page, name: string, content: string): Promise<void> {
  const opener = page.getByRole("button", { name: "Research Asset", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Research Asset 工作台" });
  await dialog.getByLabel("Research Asset 显示名称").fill(name);
  await dialog.getByLabel("Research Asset 原始文本").fill(content);
  await dialog.getByRole("button", { name: "提交 Asset Intake" }).click();
  await expect(dialog.getByText("已接纳精确版本", { exact: false })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭 Research Asset 工作台" }).click();
  await expect(dialog).toBeHidden();
}

async function ownerRevision(
  page: Page,
  baseUrl: string,
  owner: string,
): Promise<number> {
  const response = await page.request.get(`${baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBe(true);
  const snapshot = await response.json() as {
    owners: Record<string, { revision: number }>;
  };
  return snapshot.owners[owner].revision;
}
