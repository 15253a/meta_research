import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 90_000 });

test.beforeEach(async () => {
  product = await DeterministicProduct.start({ manualRoot: true });
});

test.afterEach(async () => {
  await product?.stop();
  product = undefined;
});

test("Writing report stays autonomous and exposes distinct Owner layers", async ({
  page,
  context,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page, product);

  const opener = page.getByRole("button", { name: "Writing", exact: true });
  await expect(opener).toBeEnabled();
  await opener.click();
  let dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "关闭 Writing" })).toBeFocused();

  await dialog.getByLabel("标题").fill("浏览器阶段报告");
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  const preview = dialog.getByTestId("writing-intent-preview");
  await expect(preview).toBeVisible();
  await expect(preview).toContainText("主 Quest Stage 不会被暂停或推进");
  await preview.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();

  const runRef = await currentRunRef(page.request);
  await expect(dialog.getByTestId("writing-run-detail")).toBeVisible();
  await dialog.getByRole("button", { name: "暂停" }).click();
  await expect(dialog.getByRole("button", { name: "继续" })).toBeVisible();
  await expect.poll(() => runStatus(page.request, runRef)).toBe("paused");

  await dialog.getByRole("button", { name: "继续" }).click();
  await expect.poll(() => runStatus(page.request, runRef)).toBe("active");

  // A real browser page owns neither the root Session nor the daemon worker.
  await page.close();
  const reopened = await context.newPage();
  await expect.poll(
    () => citationStatus(reopened.request, runRef),
    { timeout: 12_000 },
  ).toBe("accepted");

  await reopened.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
  const reopenedOpener = reopened.getByRole("button", { name: "Writing", exact: true });
  await reopenedOpener.click();
  dialog = reopened.getByRole("dialog", { name: "Writing report 核心闭环" });
  const layers = dialog.getByLabel("Writing 四层状态").locator("section");
  await expect(layers.nth(0)).toContainText("Executioncompleted");
  await expect(layers.nth(1)).toContainText("Deliverable / RMaccepted");
  await expect(layers.nth(2)).toContainText("Citation / RGaccepted");
  await expect(layers.nth(3)).toContainText("Rendererready");
  await expect(dialog.getByText("root Session", { exact: true })).toBeVisible();
  await expect(dialog.getByText("native Session", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Fence", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Snapshot", { exact: true })).toBeVisible();
  await expect(dialog.getByText("v1", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "查看 v1 正文" }).click();
  const viewer = dialog.getByTestId("writing-report-viewer");
  await expect(viewer).toContainText("浏览器阶段报告 · r1");
  await expect(viewer).toContainText("Evidence gap");

  const beforeRender = await reportView(reopened.request, runRef);
  const firstRender = await reopened.request.get(
    `${product.baseUrl}/api/v1/writing/runs/${runRef}/render?format=markdown`,
  );
  const secondRender = await reopened.request.get(
    `${product.baseUrl}/api/v1/writing/runs/${runRef}/render?format=markdown`,
  );
  expect(firstRender.ok()).toBe(true);
  expect(await firstRender.text()).toBe(await secondRender.text());
  expect(firstRender.headers()["x-writing-render-hash"])
    .toBe(secondRender.headers()["x-writing-render-hash"]);
  const afterRender = await reportView(reopened.request, runRef);
  expect(afterRender.versions).toHaveLength(beforeRender.versions.length);

  await dialog.getByLabel("反馈形成 successor revision").fill(
    "把证据缺口提升到摘要，并保持同一 Session。",
  );
  await dialog.getByRole("button", { name: "提交修订" }).click();
  await expect.poll(
    async () => (await reportView(reopened.request, runRef)).versions.length,
    { timeout: 12_000 },
  ).toBe(2);
  await expect.poll(
    () => citationStatus(reopened.request, runRef),
    { timeout: 12_000 },
  ).toBe("accepted");
  await expect(
    dialog.getByRole("complementary", { name: "Writing 版本与比较" })
      .getByRole("button", { name: /^v2 / }),
  ).toBeVisible();
  await dialog.getByLabel("左版本").selectOption({ label: "v1" });
  await dialog.getByLabel("右版本").selectOption({ label: "v2" });
  await dialog.getByRole("button", { name: "比较三轴" }).click();
  const comparison = dialog.getByTestId("writing-comparison");
  await expect(comparison).toContainText("内容 diff");
  await expect(comparison).toContainText("浏览器阶段报告 · r1");
  await expect(comparison).toContainText("浏览器阶段报告 · r2");
  await expect(comparison).toContainText("Citation 变化");

  // A comparison is a fact about one exact Run and version pair.  Build a
  // second two-version Run, then prove switching tabs cannot relabel A's facts
  // as B's comparison.
  await dialog.getByLabel("标题").fill("第二个浏览器报告");
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  await dialog.getByTestId("writing-intent-preview")
    .getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const secondRunRef = await currentRunRef(reopened.request);
  expect(secondRunRef).not.toBe(runRef);
  await expect.poll(
    () => citationStatus(reopened.request, secondRunRef),
    { timeout: 12_000 },
  ).toBe("accepted");
  await dialog.getByLabel("反馈形成 successor revision").fill(
    "为第二个 Run 形成独立的第二版。",
  );
  await dialog.getByRole("button", { name: "提交修订" }).click();
  await expect.poll(
    async () => (await reportView(reopened.request, secondRunRef)).versions.length,
    { timeout: 12_000 },
  ).toBe(2);
  await expect.poll(
    () => citationStatus(reopened.request, secondRunRef),
    { timeout: 12_000 },
  ).toBe("accepted");

  await dialog.getByRole("button", { name: /^浏览器阶段报告/ }).click();
  await dialog.getByLabel("左版本").selectOption({ label: "v1" });
  await dialog.getByLabel("右版本").selectOption({ label: "v2" });
  await dialog.getByRole("button", { name: "比较三轴" }).click();
  await expect(dialog.getByTestId("writing-comparison")).toContainText(
    "浏览器阶段报告 · r1",
  );
  await dialog.getByRole("button", { name: /^第二个浏览器报告/ }).click();
  await expect(dialog.getByTestId("writing-comparison")).toBeHidden();

  for (const viewport of [
    { width: 800, height: 900 },
    { width: 390, height: 844 },
    { width: 1440, height: 900 },
  ]) {
    await reopened.setViewportSize(viewport);
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("complementary", { name: "Writing 版本与比较" }),
    ).toBeVisible();
    await expect(dialog.getByRole("button", { name: "比较三轴" })).toBeVisible();
    expect(
      await reopened.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    ).toBe(true);
  }

  await reopened.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(reopenedOpener).toBeFocused();
});

test("Writing cancellation requires exact preview and is terminal", async ({ page }) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Writing", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  await dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const runRef = await currentRunRef(page.request);
  await dialog.getByRole("button", { name: "预览取消" }).click();
  const cancelPreview = dialog.getByTestId("writing-cancel-preview");
  await expect(cancelPreview).toContainText("终态后不能继续或再修订");
  await cancelPreview.getByRole("button", { name: "确认终止 Writing Run" }).click();
  await expect.poll(() => runStatus(page.request, runRef)).toBe("cancelled");
  await expect(dialog.getByTestId("writing-run-detail")).not.toContainText("继续");
  await expect(dialog.getByTestId("writing-run-detail")).not.toContainText("暂停");
});

test("a frozen Writing intent is recoverable after preview acknowledgement loss", async ({ page }) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  let dropped = false;
  await page.route("**/api/v1/writing/intents/*/preview", async (route) => {
    if (!dropped) {
      dropped = true;
      await route.abort("connectionreset");
      return;
    }
    await route.continue();
  });
  await page.getByRole("button", { name: "Writing", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await dialog.getByLabel("标题").fill("可恢复冻结报告");
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  const frozen = dialog.getByTestId("writing-intent-preview");
  await expect(frozen).toContainText("已冻结，等待影响预览");
  await expect(frozen).toContainText("可恢复冻结报告");
  await expect(frozen).toContainText("Owner cut");
  await page.reload({ waitUntil: "domcontentloaded" });
  dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("writing-intent-preview")).toContainText(
    "可恢复冻结报告",
  );
  await dialog.getByRole("button", { name: "重新生成影响预览" }).click();
  await expect(dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" })).toBeVisible();
  await dialog.getByLabel("标题").fill("重新冻结后的报告");
  await dialog.getByRole("button", { name: "以当前状态重新冻结新 Intent" }).click();
  await expect(dialog.getByTestId("writing-intent-preview")).toContainText(
    "重新冻结后的报告",
  );
  await expect(dialog.getByRole("button", { name: "可恢复冻结报告" })).toBeVisible();
});

type WritingView = {
  run: { run_ref: string; status: string } | null;
  citation: { status: string };
  versions: Array<{ version_ref: string }>;
};

async function reportView(request: APIRequestContext, runRef: string): Promise<WritingView> {
  if (!product) throw new Error("deterministic product missing");
  const response = await request.get(`${product.baseUrl}/api/v1/writing/runs/${runRef}`);
  expect(response.ok()).toBe(true);
  return await response.json() as WritingView;
}

async function currentRunRef(request: APIRequestContext): Promise<string> {
  if (!product) throw new Error("deterministic product missing");
  await expect.poll(async () => {
    const response = await request.get(`${product?.baseUrl}/api/v1/writing`);
    const body = await response.json() as { runs: WritingView[] };
    return body.runs[0]?.run?.run_ref ?? "";
  }).not.toBe("");
  const response = await request.get(`${product.baseUrl}/api/v1/writing`);
  const body = await response.json() as { runs: WritingView[] };
  const runRef = body.runs[0]?.run?.run_ref;
  if (!runRef) throw new Error("Writing Run missing after confirmation");
  return runRef;
}

async function runStatus(request: APIRequestContext, runRef: string): Promise<string> {
  return (await reportView(request, runRef)).run?.status ?? "missing";
}

async function citationStatus(request: APIRequestContext, runRef: string): Promise<string> {
  return (await reportView(request, runRef)).citation.status;
}
