import { expect, test, type Page } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";

import { selectServerMaterial } from "./support/server-material-selection.js";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const linkedMaterialDirectory = resolve(
  repositoryRoot,
  "src/meta_research/skills/deepfetch_v4/references",
);

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

  const opener = page.getByRole("button", { name: "研究资料", exact: true });
  await expect(opener).toBeEnabled();
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "关闭研究资料" })).toBeFocused();

  const kinds = await dialog.getByLabel("Research Asset 来源类型").locator("option").allTextContents();
  expect(kinds).toEqual([
    "文本",
    "服务器目录",
    "服务器文件路径",
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

test("Quest server directory selection saves unread metadata without RM intake", async ({ page }) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  const intakes: string[] = [];
  const references: unknown[] = [];
  page.on("request", request => {
    if (request.method() !== "POST") return;
    if (request.url().endsWith("/research-assets/intakes")) intakes.push(request.url());
    if (request.url().endsWith("/material-references")) references.push(request.postDataJSON());
  });
  await page.getByRole("button", { name: "创建研究任务" }).click();
  const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const materials = quest.locator("[data-journey-section='materials']");
  await selectServerMaterial(page, materials, linkedMaterialDirectory, "Original directory description.");
  expect(intakes).toEqual([]);
  expect(references).toEqual([]);
  await materials.getByRole("button", { name: "保存材料引用到创建草稿", exact: true }).click();
  await expect.poll(() => references.length).toBe(1);
  await expect(materials).toContainText("已保存，尚未读取");
  expect(intakes).toEqual([]);
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "创建研究任务" }).click();
  await expect(materials).toContainText(linkedMaterialDirectory);
  await expect(materials).toContainText("Original directory description.");
  expect(references).toHaveLength(1);
});

test("an optional server candidate never gates the formal Direct Quest flow", async ({ page }) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "创建研究任务" }).click();
  const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const materials = quest.locator("[data-journey-section='materials']");
  for (const viewport of [{ width: 800, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await expect(materials.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeVisible();
    await expect(materials.locator("input[type=file]")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await selectServerMaterial(page, materials, linkedMaterialDirectory, "Optional original source.");
  const goal = "材料绑定缓慢时 Direct Quest 仍可正式创建";
  const boundary = "草案、首问与确认资格不依赖可选材料绑定完成";
  await quest.getByRole("textbox", { name: "目标", exact: true }).fill(goal);
  await quest.getByRole("textbox", { name: "边界", exact: true }).fill(boundary);
  await quest.getByRole("textbox", { name: "目标", exact: true }).blur();
  await expect(quest.getByText("草案已自动保存", { exact: true })).toBeVisible({
    timeout: 8_000,
  });
  await expect.poll(async () => {
    const response = await page.request.get(
      `${product?.baseUrl}/api/v1/quest-initializations/current`,
    );
    const current = await response.json() as {
      quest_draft: { value: { goal: string; completion_criteria: string } };
    };
    return {
      goal: current.quest_draft.value.goal,
      boundary: current.quest_draft.value.completion_criteria,
    };
  }).toEqual({ goal, boundary });

  await quest.getByRole("button", { name: "直接根据目标生成" }).click();
  await quest.getByRole("button", { name: "检测本机计算卡" }).click();
  await expect(
    quest.getByText("capability_unavailable · deterministic_probe_unavailable", {
      exact: true,
    }),
  ).toBeVisible();
  await quest.getByRole("button", { name: "重新检测", exact: true }).click();
  await quest.getByRole("button", {
    name: /Deterministic GPU.*GPU-deterministic-1/,
  }).click();
  await quest.getByRole("button", { name: "生成第一个问题" }).click();
  await expect(quest.getByLabel("首问题标题")).toHaveValue(
    "低照度显微图像中的稀有形态保真",
    { timeout: 15_000 },
  );
  await expect(
    quest.getByText("当前 Impact Preview 已绑定，可以确认", { exact: true }),
  ).toBeVisible();
  await expect(
    quest.getByRole("button", { name: "确认创建 Quest 与第一个问题" }),
  ).toBeEnabled();

  await quest.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(quest).toBeHidden();

  await expect.poll(async () => {
    const response = await page.request.get(
      `${product?.baseUrl}/api/v1/quest-initializations/current`,
    );
    const current = await response.json() as {
      quest_draft: {
        value: {
          goal: string;
          completion_criteria: string;
          literature: {
            accepted_material_bindings: Array<{ version_ref?: string }>;
          };
        };
      };
    };
    return {
      goal: current.quest_draft.value.goal,
      boundary: current.quest_draft.value.completion_criteria,
      bindingCount:
        current.quest_draft.value.literature.accepted_material_bindings.length,
    };
  }, { timeout: 12_000 }).toEqual({ goal, boundary, bindingCount: 0 });
});

test("paged inventory keeps an exact off-page version reachable", async ({
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
  await page.getByRole("button", { name: "研究资料", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
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
});

test("an authorization interruption retains the durable intake pointer and resumes", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "研究资料", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
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
  const resumed = page.getByRole("dialog", { name: "研究资料" });
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
  await page.getByRole("button", { name: "研究资料", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
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
  await page.getByRole("button", { name: "研究资料", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
  const csrf = (await page.context().cookies()).find(cookie => cookie.name === "meta_research_csrf")?.value;
  if (!csrf) throw new Error("csrf cookie missing");
  const response = await page.request.post(`${product.baseUrl}/api/v1/research-assets/intakes`, {
    headers: { Origin: product.baseUrl, "X-CSRF-Token": csrf, "Idempotency-Key": "empty-compatible-formal-api" },
    data: { source_kind: "file", custody_mode: "managed", display_name: "empty-observation.txt", media_type: "text/plain", content_base64: "" },
  });
  expect(response.ok()).toBeTruthy();
  await dialog.getByRole("button", { name: "刷新", exact: true }).click();
  const item = dialog.getByRole("listitem").filter({ hasText: "empty-observation.txt" });
  await expect(item).toContainText("integrity · verified");
  await item.click();
  await expect(
    dialog.getByRole("region", { name: "Research Asset 版本详情" })
      .getByText("0", { exact: true }),
  ).toBeVisible();
});

async function acceptTextAsset(page: Page, name: string, content: string): Promise<void> {
  const opener = page.getByRole("button", { name: "研究资料", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "研究资料" });
  await dialog.locator(".asset-storage-details > summary").click();
  await dialog.getByLabel("Research Asset 显示名称").fill(name);
  await dialog.getByLabel("Research Asset 原始文本").fill(content);
  await dialog.getByRole("button", { name: "提交 Asset Intake" }).click();
  await expect(dialog.getByText("已接纳精确版本", { exact: false })).toBeVisible();
  await dialog.getByRole("button", { name: "关闭研究资料" }).click();
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
