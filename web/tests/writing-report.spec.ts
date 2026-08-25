import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
  type Page,
} from "@playwright/test";
import { createHash } from "node:crypto";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";


let product: DeterministicProduct | undefined;

test.describe.configure({ timeout: 90_000 });

test.beforeEach(async ({}, testInfo) => {
  product = await DeterministicProduct.start({
    manualRoot: true,
    writingDeliveryFaults: testInfo.title.includes(
      "every external delivery intent",
    )
      ? "history-boundaries"
      : undefined,
  });
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
  await expect(dialog.getByTestId("writing-external-delivery")).toHaveCount(0);

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
  const delivery = dialog.getByTestId("writing-external-delivery");
  await expect(delivery).toBeVisible();
  await delivery.getByText("外部交付", { exact: true }).click();
  await expect(delivery.getByLabel("Writing Provider capabilities")).toContainText(
    "local-filesystem",
  );
  await expect(delivery.getByLabel("Writing Provider capabilities")).toContainText(
    "publish",
  );
  await expect(delivery.getByRole("combobox", { name: /^Provider/ })).toHaveValue(
    "local-filesystem",
  );
  await expect(delivery.getByLabel("Permissions")).toHaveValue("0600");
  const createDelivery = delivery.getByRole("button", { name: "创建交付 Draft" });
  await expect(createDelivery).toBeDisabled();
  const deliveryName = "browser-writing-report.md";
  const deliveryPath = product.prepareWritingDeliveryTarget(deliveryName);
  await delivery.getByLabel("绝对 path").fill(deliveryPath);
  await expect(createDelivery).toBeEnabled();
  await createDelivery.click();
  await expect(delivery).toContainText("HC COMMAND · draft");
  expect(product.writingDeliveryExists(deliveryName)).toBe(false);
  await delivery.getByRole("button", { name: "生成精确影响预览" }).click();
  const deliveryPreview = delivery.getByTestId("writing-delivery-preview");
  await expect(deliveryPreview).toContainText(deliveryPath);
  await expect(deliveryPreview).toContainText("create_file");
  expect(product.writingDeliveryExists(deliveryName)).toBe(false);
  await delivery.getByRole("button", { name: "确认本次外部交付" }).click();
  await expect.poll(
    () => externalDeliveryStatus(reopened.request, runRef),
    { timeout: 12_000 },
  ).toBe("completed");
  expect(product.readWritingDelivery(deliveryName)).toContain("浏览器阶段报告 · r1");
  const deliveryReceipts = delivery.getByLabel("Writing 交付 receipts 与 observations");
  await expect(deliveryReceipts).not.toContainText("not_confirmed");
  await expect(deliveryReceipts).not.toContainText("not_admitted");
  await expect(delivery).toContainText("Provider observations");
  await expect(delivery).toContainText("Provider ACK 不是 Owner receipt");
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
  await expect(comparison).toContainText("frozen ·");

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
  await expect.poll(
    () => runStatus(page.request, runRef),
    { timeout: 12_000 },
  ).toBe("cancelled");
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

test("every external delivery intent remains selectable and recoverable after reload", async ({
  page,
}) => {
  test.slow();
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Writing", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await dialog.getByLabel("标题").fill("多次外部交付报告");
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  await dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const runRef = await currentRunRef(page.request);
  await expect.poll(
    () => citationStatus(page.request, runRef),
    { timeout: 12_000 },
  ).toBe("accepted");

  const delivery = dialog.getByTestId("writing-external-delivery");
  await delivery.getByText("外部交付", { exact: true }).click();
  const createConfirmedDelivery = async (
    fileName: string,
    expectedStatus: "completed" | "partial" | "outcome_unknown",
  ): Promise<string> => {
    const targetPath = product?.prepareWritingDeliveryTarget(fileName);
    if (!targetPath) throw new Error("deterministic delivery target missing");
    await delivery.getByLabel("绝对 path").fill(targetPath);
    await delivery.getByRole("button", { name: "创建交付 Draft" }).click();
    await delivery.getByRole("button", { name: "生成精确影响预览" }).click();
    await expect.poll(
      () => deliveryConfirmationForTarget(page.request, runRef, targetPath),
    ).toBe("previewed");
    await delivery.getByRole("button", { name: "确认本次外部交付" }).click();
    await expect.poll(
      () => deliveryConfirmationForTarget(page.request, runRef, targetPath),
    ).toBe("confirmed");
    await expect.poll(
      () => deliveryStatusForTarget(page.request, runRef, targetPath),
      { timeout: 12_000 },
    ).toBe(expectedStatus);
    return targetPath;
  };

  const completedPath = await createConfirmedDelivery(
    "history-completed.md",
    "completed",
  );

  await delivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  }).click();
  const partialPath = await createConfirmedDelivery(
    "history-partial.md",
    "partial",
  );

  await delivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  }).click();
  const unknownPath = await createConfirmedDelivery(
    "history-unknown.md",
    "outcome_unknown",
  );
  await expect.poll(async () => {
    const unknown = await deliveryForTarget(page.request, runRef, unknownPath);
    return Boolean(unknown?.operation?.reconciliation_receipt);
  }).toBe(true);

  await delivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  }).click();
  const previewedPath = product.prepareWritingDeliveryTarget("history-previewed.md");
  await delivery.getByLabel("绝对 path").fill(previewedPath);
  await delivery.getByRole("button", { name: "创建交付 Draft" }).click();
  await delivery.getByRole("button", { name: "生成精确影响预览" }).click();
  await expect.poll(
    () => externalDeliveryConfirmationStatus(page.request, runRef),
  ).toBe("previewed");

  const history = delivery.getByRole("navigation", {
    name: "Writing delivery intent 历史",
  });
  await history.getByRole("button", {
    name: /history-completed\.md.*completed/,
  }).click();
  await delivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  }).click();
  const draftPath = product.prepareWritingDeliveryTarget("history-draft.md");
  await delivery.getByLabel("绝对 path").fill(draftPath);
  await delivery.getByRole("button", { name: "创建交付 Draft" }).click();

  await expect.poll(
    async () => (await reportView(page.request, runRef)).deliveries?.length ?? 0,
  ).toBe(5);
  const beforeCustodyLoss = await reportView(page.request, runRef);
  const contentHash = beforeCustodyLoss.deliverable.content_hash;
  if (!contentHash) throw new Error("accepted Writing content hash missing");
  product.damageWritingAssetCustody(contentHash);
  await expect.poll(
    () => rendererStatus(page.request, runRef),
    { timeout: 12_000 },
  ).toBe("unavailable");

  await page.reload({ waitUntil: "domcontentloaded" });
  dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  const recoveredDelivery = dialog.getByTestId("writing-external-delivery");
  await expect(recoveredDelivery).toBeVisible({ timeout: 8_000 });
  await recoveredDelivery.getByText("外部交付", { exact: true }).click();
  const recoveredHistory = recoveredDelivery.getByRole("navigation", {
    name: "Writing delivery intent 历史",
  });

  await expect(recoveredDelivery.getByTestId("writing-delivery-current-blocker"))
    .toContainText("asset custody / Citation / Renderer 当前不可用于新外部动作");
  await expect(recoveredHistory.getByRole("button")).toHaveCount(5);
  const recoveredCompleted = recoveredHistory.getByRole("button", {
    name: /history-completed\.md.*confirmed.*completed/,
  });
  await recoveredCompleted.click();
  await expect(recoveredCompleted).toHaveAttribute("aria-pressed", "true");
  await expect(recoveredDelivery).toContainText(completedPath);
  await expect(
    recoveredDelivery.getByLabel("Writing 交付 receipts 与 observations"),
  ).not.toContainText("not_confirmed");
  await expect(recoveredDelivery.getByText("Provider ACK 不是 Owner receipt")).toBeVisible();
  await expect(recoveredDelivery.getByText("completed", { exact: true }).last()).toBeVisible();
  await expect(recoveredDelivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  })).toBeDisabled();

  await recoveredHistory.getByRole("button", {
    name: /history-partial\.md.*confirmed.*partial/,
  }).click();
  await expect(recoveredDelivery).toContainText(partialPath);
  await expect(
    recoveredDelivery.getByLabel("Writing 交付 receipts 与 observations"),
  ).not.toContainText("not_attempted");
  await expect(recoveredDelivery.getByText("partial", { exact: true }).last()).toBeVisible();

  await recoveredHistory.getByRole("button", {
    name: /history-unknown\.md.*confirmed.*outcome_unknown/,
  }).click();
  await expect(recoveredDelivery).toContainText(unknownPath);
  const unknownReceipts = recoveredDelivery.getByLabel(
    "Writing 交付 receipts 与 observations",
  );
  await expect(unknownReceipts).not.toContainText("not_attempted");
  await expect(unknownReceipts).not.toContainText("none");
  await expect(
    recoveredDelivery.locator(".writing-delivery-observations"),
  ).toContainText("none");

  await recoveredHistory.getByRole("button", {
    name: /history-previewed\.md.*previewed.*not_attempted/,
  }).click();
  await expect(recoveredDelivery).toContainText(previewedPath);
  await expect(recoveredDelivery.getByTestId("writing-delivery-preview")).toBeVisible();
  await expect(
    recoveredDelivery.getByRole("button", { name: "确认本次外部交付" }),
  ).toBeDisabled();

  await recoveredHistory.getByRole("button", {
    name: /history-draft\.md.*draft.*not_attempted/,
  }).click();
  await expect(recoveredDelivery).toContainText(draftPath);
  await expect(
    recoveredDelivery.getByRole("button", { name: "生成精确影响预览" }),
  ).toBeDisabled();
});

test("external delivery actions come from the selected provider capability", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Writing", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  await dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const runRef = await currentRunRef(page.request);
  await expect.poll(
    () => citationStatus(page.request, runRef),
    { timeout: 12_000 },
  ).toBe("accepted");

  await page.route(`${product.baseUrl}/api/v1/writing`, async (route) => {
    const response = await route.fetch();
    const body = await response.json() as {
      delivery_capabilities?: {
        providers: Array<{ provider_ref: string; supported_actions: string[] }>;
      };
    };
    for (const provider of body.delivery_capabilities?.providers ?? []) {
      if (provider.provider_ref === "local-filesystem") {
        provider.supported_actions = ["publish"];
      }
    }
    await route.fulfill({ response, json: body });
  });
  await page.reload({ waitUntil: "domcontentloaded" });
  dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  const delivery = dialog.getByTestId("writing-external-delivery");
  await delivery.getByText("外部交付", { exact: true }).click();
  const action = delivery.getByRole("combobox", { name: "Action" });
  await expect(action.getByRole("option")).toHaveCount(1);
  await expect(action.getByRole("option")).toHaveText(["publish · 新建文件"]);
});

const officeDeliveryViewports = [
  { width: 1440, height: 900, visualSnapshot: true },
  { width: 800, height: 900, visualSnapshot: false },
  { width: 390, height: 844, visualSnapshot: false },
] as const;

for (const viewport of officeDeliveryViewports) {
  test(
    `Paper and Presentation become ready with real deterministic Office downloads at ${viewport.width}px`,
    async ({ page }) => {
      await exerciseOfficeDeliveryFlow(page, viewport);
    },
  );
}

async function exerciseOfficeDeliveryFlow(
  page: Page,
  viewport: (typeof officeDeliveryViewports)[number],
): Promise<void> {
  if (!product) throw new Error("deterministic product missing");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: viewport.width, height: viewport.height });
  await openAuthenticatedProduct(page, product);
  await page.getByRole("button", { name: "Writing", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Writing report 核心闭环" });
  await expectNoHorizontalOverflow(page, dialog);

  const overview = await writingOverview(page.request);
  expect(overview.delivery_capabilities.renderers).toEqual([
    { document_type: "report", default_format: "markdown", formats: ["markdown"] },
    { document_type: "paper", default_format: "docx", formats: ["docx"] },
    { document_type: "presentation", default_format: "pptx", formats: ["pptx"] },
  ]);

  await dialog.getByLabel("交付类型").selectOption("paper");
  await dialog.getByLabel("标题").fill("冻结证据边界论文");
  await dialog.getByRole("button", { name: "生成影响预览" }).click();
  await dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const paperRunRef = await currentRunRef(page.request);
  await expect.poll(
    () => citationStatus(page.request, paperRunRef),
    { timeout: 12_000 },
  ).toBe("accepted");
  await expect(dialog.getByRole("button", { name: /冻结证据边界论文.*PAPER/ })).toBeVisible();
  await expect(dialog.getByRole("link", { name: "下载确定性 DOCX" })).toBeVisible();
  const paperBody = await expectOfficeDownload(page, dialog, paperRunRef, {
    linkName: "下载确定性 DOCX",
    format: "docx",
    mediaType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    archiveEntry: "word/document.xml",
    fileExtension: ".docx",
  });
  await publishOfficeThroughWriting(
    page,
    dialog,
    paperRunRef,
    `ready-paper-${viewport.width}.docx`,
    paperBody,
  );
  await expectNoHorizontalOverflow(page, dialog);

  await dialog.getByLabel("交付类型").selectOption("presentation");
  await dialog.getByLabel("标题").fill("冻结证据边界演示");
  await dialog.getByRole("button", {
    name: /^(生成影响预览|以当前状态重新冻结新 Intent)$/,
  }).click();
  await dialog.getByRole("button", { name: "确认 Intent 与 Snapshot" }).click();
  const presentationRunRef = await currentRunRef(page.request);
  expect(presentationRunRef).not.toBe(paperRunRef);
  await expect.poll(
    () => citationStatus(page.request, presentationRunRef),
    { timeout: 12_000 },
  ).toBe("accepted");
  await expect(dialog.getByRole("button", { name: /冻结证据边界演示.*PPT/ })).toBeVisible();
  await expect(dialog.getByRole("link", { name: "下载确定性 PPTX" })).toBeVisible();
  const presentationBody = await expectOfficeDownload(page, dialog, presentationRunRef, {
    linkName: "下载确定性 PPTX",
    format: "pptx",
    mediaType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    archiveEntry: "ppt/presentation.xml",
    fileExtension: ".pptx",
  });
  await publishOfficeThroughWriting(
    page,
    dialog,
    presentationRunRef,
    `ready-presentation-${viewport.width}.pptx`,
    presentationBody,
  );
  await expectNoHorizontalOverflow(page, dialog);

  const readyLayers = dialog.getByLabel("Writing 四层状态").locator("section");
  await expect(readyLayers.nth(0)).toContainText("Executioncompleted");
  await expect(readyLayers.nth(1)).toContainText("Deliverable / RMaccepted");
  await expect(readyLayers.nth(2)).toContainText("Citation / RGaccepted");
  await expect(readyLayers.nth(3)).toContainText("Rendererready");
  if (viewport.visualSnapshot) {
    const removeDynamicMask = await installWritingSnapshotMask(dialog);
    try {
      await expect(dialog.getByTestId("writing-external-delivery"))
        .not.toHaveAttribute("open", "");
      await expect(dialog).toHaveScreenshot("writing-paper-presentation-ready-1440.png", {
        animations: "disabled",
        maxDiffPixels: 250,
      });
    } finally {
      await removeDynamicMask();
    }
  }
}

type WritingView = {
  run: { run_ref: string; status: string } | null;
  document_type: "report" | "paper" | "presentation";
  citation: { status: string };
  deliverable: { status: string; content_hash?: string };
  renderer: { status: string; default_format?: string; formats?: string[] };
  versions: Array<{ version_ref: string }>;
  deliveries?: Array<{
    status: string;
    confirmation_status: string;
    payload: {
      renderer_artifact_sha256: string;
      target: { path?: string } | { target_ref: string };
    };
    operation?: {
      reconciliation_receipt?: unknown;
      provider_observations: Array<{ outcome: string }>;
    } | null;
  }>;
};

type WritingOverviewView = {
  delivery_capabilities: {
    renderers: Array<{
      document_type: "report" | "paper" | "presentation";
      default_format: "markdown" | "docx" | "pptx";
      formats: string[];
    }>;
  };
  runs: WritingView[];
};

async function expectNoHorizontalOverflow(
  page: Page,
  dialog: Locator,
): Promise<void> {
  await expect(dialog).toBeVisible();
  const pageWidths = await page.evaluate(() => ({
    body: document.body.scrollWidth,
    document: document.documentElement.scrollWidth,
    viewport: window.innerWidth,
  }));
  expect(pageWidths.body).toBeLessThanOrEqual(pageWidths.viewport);
  expect(pageWidths.document).toBeLessThanOrEqual(pageWidths.viewport);
  const dialogWidths = await dialog.evaluate((element) => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }));
  expect(dialogWidths.scroll).toBeLessThanOrEqual(dialogWidths.client);
}

async function installWritingSnapshotMask(
  dialog: Locator,
): Promise<() => Promise<void>> {
  const selector = "code, .writing-identities dd, .writing-run-title small";
  await dialog.evaluate((element, dynamicSelector) => {
    type MaskedDialog = HTMLElement & {
      __writingSnapshotMask?: MutationObserver;
    };
    const root = element as MaskedDialog;
    root.__writingSnapshotMask?.disconnect();
    const mask = () => {
      for (const candidate of root.querySelectorAll<HTMLElement>(dynamicSelector)) {
        candidate.style.setProperty("color", "transparent", "important");
        candidate.style.setProperty("text-shadow", "none", "important");
      }
    };
    const observer = new MutationObserver(mask);
    observer.observe(root, { childList: true, subtree: true });
    root.__writingSnapshotMask = observer;
    mask();
  }, selector);
  const dynamicFields = dialog.locator(selector);
  await expect(dynamicFields).not.toHaveCount(0);
  expect(await dynamicFields.evaluateAll((elements) => (
    elements.every((element) => getComputedStyle(element).color === "rgba(0, 0, 0, 0)")
  ))).toBe(true);
  return async () => {
    await dialog.evaluate((element, dynamicSelector) => {
      type MaskedDialog = HTMLElement & {
        __writingSnapshotMask?: MutationObserver;
      };
      const root = element as MaskedDialog;
      root.__writingSnapshotMask?.disconnect();
      delete root.__writingSnapshotMask;
      for (const candidate of root.querySelectorAll<HTMLElement>(dynamicSelector)) {
        candidate.style.removeProperty("color");
        candidate.style.removeProperty("text-shadow");
      }
    }, selector);
  };
}

async function writingOverview(request: APIRequestContext): Promise<WritingOverviewView> {
  if (!product) throw new Error("deterministic product missing");
  const response = await request.get(`${product.baseUrl}/api/v1/writing`);
  expect(response.ok()).toBe(true);
  return await response.json() as WritingOverviewView;
}

async function expectOfficeDownload(
  page: Page,
  dialog: Locator,
  runRef: string,
  expected: {
    linkName: "下载确定性 DOCX" | "下载确定性 PPTX";
    format: "docx" | "pptx";
    mediaType: string;
    archiveEntry: string;
    fileExtension: string;
  },
): Promise<Buffer> {
  if (!product) throw new Error("deterministic product missing");
  const downloadStarted = page.waitForEvent("download");
  await dialog.getByRole("link", { name: expected.linkName }).click();
  const download = await downloadStarted;
  expect(download.suggestedFilename()).toMatch(
    new RegExp(`^[^/\\\\]+\\${expected.fileExtension}$`),
  );
  expect(await download.failure()).toBeNull();
  const downloadStream = await download.createReadStream();
  const downloadChunks: Buffer[] = [];
  for await (const chunk of downloadStream) {
    downloadChunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const downloadedBody = Buffer.concat(downloadChunks);

  const response = await page.request.get(
    `${product.baseUrl}/api/v1/writing/runs/${runRef}/render`,
  );
  expect(response.ok()).toBe(true);
  expect(response.headers()["content-type"]).toBe(expected.mediaType);
  expect(response.headers()["x-writing-render-format"]).toBe(expected.format);
  expect(response.headers()["x-content-type-options"]).toBe("nosniff");
  expect(response.headers()["content-disposition"]).toMatch(
    new RegExp(`attachment; filename="[^"]+\\${expected.fileExtension}"`),
  );
  const body = await response.body();
  expect(downloadedBody).toEqual(body);
  expect(body.byteLength).toBeGreaterThan(1_000);
  expect(body.subarray(0, 2)).toEqual(Buffer.from("PK"));
  expect(body.includes(Buffer.from(expected.archiveEntry))).toBe(true);
  expect(response.headers()["x-writing-render-hash"]).toMatch(/^[0-9a-f]{64}$/);
  return body;
}

async function publishOfficeThroughWriting(
  page: Page,
  dialog: Locator,
  runRef: string,
  fileName: string,
  renderedBody: Buffer,
): Promise<void> {
  if (!product) throw new Error("deterministic product missing");
  const delivery = dialog.getByTestId("writing-external-delivery");
  await delivery.getByText("外部交付", { exact: true }).click();
  const action = delivery.getByRole("combobox", { name: "Action" });
  await expect(action.getByRole("option")).toHaveText(["publish · 新建文件"]);
  const targetPath = product.prepareWritingDeliveryTarget(fileName);
  await delivery.getByLabel("绝对 path").fill(targetPath);
  await delivery.getByRole("button", { name: "创建交付 Draft" }).click();
  await delivery.getByRole("button", { name: "生成精确影响预览" }).click();
  await expect.poll(
    () => externalDeliveryConfirmationStatus(page.request, runRef),
  ).toBe("previewed");
  const preview = delivery.getByTestId("writing-delivery-preview");
  await expect(preview).toContainText(targetPath);
  await expect(preview).toContainText("create_file");
  await expectNoHorizontalOverflow(page, dialog);
  const confirm = delivery.getByRole("button", { name: "确认本次外部交付" });
  await confirm.click();
  await page.waitForTimeout(300);
  if (await externalDeliveryConfirmationStatus(page.request, runRef) !== "confirmed") {
    // React intentionally reuses the action button across Draft and Preview.
    // An SSE projection can replace it at the click boundary; one visible
    // user retry must still confirm the same frozen intent, never a new one.
    await confirm.click();
  }
  await expect.poll(
    () => externalDeliveryConfirmationStatus(page.request, runRef),
  ).toBe("confirmed");
  await expect.poll(
    () => externalDeliveryStatus(page.request, runRef),
    { timeout: 12_000 },
  ).toBe("completed");
  const projectedDelivery = (await reportView(page.request, runRef)).deliveries?.[0];
  if (!projectedDelivery) throw new Error("Writing delivery projection missing");
  const renderedSha256 = createHash("sha256").update(renderedBody).digest("hex");
  expect(projectedDelivery.payload.renderer_artifact_sha256).toBe(renderedSha256);
  const persistedBody = product.readWritingDeliveryBytes(fileName);
  expect(createHash("sha256").update(persistedBody).digest("hex")).toBe(renderedSha256);
  expect(persistedBody).toEqual(renderedBody);
  const history = delivery.getByRole("navigation", {
    name: "Writing delivery intent 历史",
  });
  await expect(history).toBeVisible();
  const completedHistory = history.getByRole("button", {
    name: new RegExp(`${escapeRegExp(fileName)}.*confirmed.*completed`),
  });
  await expect(completedHistory).toBeVisible();
  await completedHistory.click();
  await expect(completedHistory).toHaveAttribute("aria-pressed", "true");
  await expect(delivery.getByRole("button", {
    name: "为新的外部副作用创建新 Draft",
  })).toBeVisible();
  await expectNoHorizontalOverflow(page, dialog);
  await delivery.locator("summary").click();
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

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

async function externalDeliveryStatus(
  request: APIRequestContext,
  runRef: string,
): Promise<string> {
  return (await reportView(request, runRef)).deliveries?.[0]?.status ?? "missing";
}

async function externalDeliveryConfirmationStatus(
  request: APIRequestContext,
  runRef: string,
): Promise<string> {
  return (await reportView(request, runRef)).deliveries?.[0]?.confirmation_status ?? "missing";
}

async function deliveryForTarget(
  request: APIRequestContext,
  runRef: string,
  targetPath: string,
): Promise<NonNullable<WritingView["deliveries"]>[number] | undefined> {
  return (await reportView(request, runRef)).deliveries?.find((delivery) => (
    "path" in delivery.payload.target
    && delivery.payload.target.path === targetPath
  ));
}

async function deliveryStatusForTarget(
  request: APIRequestContext,
  runRef: string,
  targetPath: string,
): Promise<string> {
  return (await deliveryForTarget(request, runRef, targetPath))?.status ?? "missing";
}

async function deliveryConfirmationForTarget(
  request: APIRequestContext,
  runRef: string,
  targetPath: string,
): Promise<string> {
  return (await deliveryForTarget(request, runRef, targetPath))?.confirmation_status
    ?? "missing";
}

async function rendererStatus(
  request: APIRequestContext,
  runRef: string,
): Promise<string> {
  return (await reportView(request, runRef)).renderer.status;
}
