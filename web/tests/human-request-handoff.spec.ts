import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import type { HumanRequestItem } from "../src/api";
import { DeterministicProduct } from "./support/deterministic-product";

let product: DeterministicProduct | null = null;
test.describe.configure({ timeout: 90_000 });

test.afterEach(async ({ page }) => {
  await page.close();
  await product?.stop();
  product = null;
});

test("a real HumanRequest explains the recorded work and required return without starting the assistant", async ({ page }) => {
  product = await DeterministicProduct.start({ manualRoot: true, humanRequestHandoff: true });
  await product.authenticate(page);
  const writes: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.startsWith("/api/v1/") && request.method() !== "GET") {
      writes.push(`${request.method()} ${url.pathname}`);
    }
  });
  await page.goto(`${product.baseUrl}/?panel=offline-operation`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("heading", { name: "提供温度传感器原始校准记录。", exact: true })).toBeVisible();
  const handoff = page.getByRole("region", { name: "求助交接材料" });
  await expect(handoff).toContainText("温度传感器审计需要对齐设备原始记录。");
  await expect(handoff).toContainText("读取导出的设备日志并核对时间戳。");
  await expect(handoff).toContainText("两台设备记录相差九分钟，现有日志未注明校准时间。");
  await expect(handoff).toContainText("缺少现场校准时间，无法判断真实测量时差。");
  await expect(handoff).toContainText("提供原始校准记录和设备编号，不改写原始数值。");
  await expect(handoff).toContainText("确认后只继续本次设备审计，其他研究任务仍可推进。");
  await expect(handoff).toContainText("回到这张请求，选择原始校准文件并明确提交回应。");
  expect(writes).toEqual([]);
});

test("the three handoff downloads preserve the exact real request and remain usable at desktop and narrow widths", async ({ page }, testInfo) => {
  product = await DeterministicProduct.start({ manualRoot: true, humanRequestHandoff: true });
  await product.authenticate(page);
  const snapshot = await (await page.request.get(`${product.baseUrl}/api/v1/snapshot`)).json();
  const request = snapshot.human_collaboration.human_requests.items[0] as HumanRequestItem;
  const identity = `/api/v1/human-requests/${encodeURIComponent(request.request_ref)}`;
  const query = `?revision=${request.revision}`;
  const beforeResponse = await page.request.get(`${product.baseUrl}${identity}${query}`);
  expect(beforeResponse.ok()).toBeTruthy();
  const before = await beforeResponse.json();
  const writes: string[] = [];
  const handoffReads: string[] = [];
  page.on("request", (operation) => {
    const url = new URL(operation.url());
    if (!url.pathname.startsWith("/api/v1/")) return;
    if (operation.method() !== "GET") writes.push(`${operation.method()} ${url.pathname}`);
    if (url.pathname.startsWith(`${identity}/handoff`)) handoffReads.push(url.pathname + url.search);
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${product.baseUrl}/?panel=offline-operation`, { waitUntil: "domcontentloaded" });
  const handoff = page.getByRole("region", { name: "求助交接材料" });
  for (const [extension, label] of [["md", "Markdown"], ["html", "HTML"], ["pdf", "PDF"]]) {
    const button = handoff.getByRole("button", { name: `下载 ${label}`, exact: true });
    await expect(button).toBeEnabled();
    const downloadPromise = page.waitForEvent("download");
    await button.click();
    const download = await downloadPromise;
    expect(await download.failure()).toBeNull();
    expect(download.suggestedFilename()).toBe(`human-request-${request.request_id}-r${request.revision}.${extension}`);
    const output = testInfo.outputPath(download.suggestedFilename());
    await download.saveAs(output);
    const bytes = readFileSync(output);
    if (extension === "pdf") expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
    else {
      expect(bytes.toString("utf8")).toContain("两台设备记录相差九分钟，现有日志未注明校准时间。");
      expect(bytes.toString("utf8")).toContain("回到这张请求，选择原始校准文件并明确提交回应。");
    }
  }
  for (const width of [1440, 800, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const label of ["Markdown", "HTML", "PDF"]) {
      await expect(handoff.getByRole("button", { name: `下载 ${label}`, exact: true })).toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`handoff-${width}.png`) });
  }
  expect(new Set(handoffReads)).toEqual(new Set([
    `${identity}/handoff${query}`,
    `${identity}/handoff.md${query}`,
    `${identity}/handoff.html${query}`,
    `${identity}/handoff.pdf${query}`,
  ]));
  const afterResponse = await page.request.get(`${product.baseUrl}${identity}${query}`);
  expect(afterResponse.ok()).toBeTruthy();
  expect(await afterResponse.json()).toEqual(before);
  expect(writes).toEqual([]);
});

test("a library request keeps the original response reachable after its complete handoff", async ({ page }, testInfo) => {
  product = await DeterministicProduct.start({ manualRoot: true, humanRequestHandoff: true, humanRequestHandoffKind: "library_reconnect" });
  await product.authenticate(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${product.baseUrl}/?panel=human-request`, { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("region", { name: "求助交接材料" })).toContainText("公开来源只有摘要，机构入口提示会话已失效，尚未取得全文。");
  for (const width of [1440, 800, 390]) {
    await page.setViewportSize({ width, height: 900 });
    const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
    const main = await dialog.getByRole("main").boundingBox();
    expect(main).not.toBeNull();
    await page.mouse.move(main!.x + 20, main!.y + 20);
    await page.mouse.wheel(0, 5_000);
    await expect(dialog.getByRole("button", { name: "提交这条想法", exact: true })).toBeInViewport();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath(`library-response-${width}.png`) });
  }
});

test("a system-operation handoff points to its actual retry action and downloads without submitting a response", async ({ page }, testInfo) => {
  product = await DeterministicProduct.start({ humanRequestHandoff: true, humanRequestHandoffKind: "system_operation_help" });
  await product.authenticate(page);
  const snapshot = await (await page.request.get(`${product.baseUrl}/api/v1/snapshot`)).json();
  const request = snapshot.human_collaboration.human_requests.items[0] as HumanRequestItem;
  const identity = `/api/v1/human-requests/${encodeURIComponent(request.request_ref)}?revision=${request.revision}`;
  const beforeResponse = await page.request.get(`${product.baseUrl}${identity}`);
  expect(beforeResponse.ok()).toBeTruthy();
  const before = await beforeResponse.json();
  const writes: string[] = [];
  page.on("request", (operation) => {
    const url = new URL(operation.url());
    if (url.pathname.startsWith("/api/v1/") && operation.method() !== "GET") {
      writes.push(`${operation.method()} ${url.pathname}`);
    }
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${product.baseUrl}/?panel=system-operation-help`, { waitUntil: "domcontentloaded" });
  const dialog = page.getByRole("dialog", { name: "需要你处理的事项" });
  await expect(dialog.getByRole("button", { name: "重试", exact: true })).toBeVisible();
  await expect(dialog.getByRole("button", { name: /提交/ })).toHaveCount(0);
  const handoff = dialog.getByRole("region", { name: "求助交接材料" });
  const instructions = handoff.locator("section").filter({ has: page.getByRole("heading", { name: /如何交回回应|如何重试当前操作/ }) });
  await expect(instructions).toContainText(/点击.{0,8}重试/);
  await expect(instructions).not.toContainText("正式提交");
  await expect(instructions).not.toContainText("填写处理说明");
  for (const [extension, label] of [["md", "Markdown"], ["html", "HTML"], ["pdf", "PDF"]]) {
    const button = handoff.getByRole("button", { name: `下载 ${label}`, exact: true });
    await expect(button).toBeEnabled();
    const downloadPromise = page.waitForEvent("download");
    await button.click();
    const download = await downloadPromise;
    expect(await download.failure()).toBeNull();
    const output = testInfo.outputPath(`system-handoff.${extension}`);
    await download.saveAs(output);
    const bytes = readFileSync(output);
    if (extension === "pdf") expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
    else {
      expect(bytes.toString("utf8")).toMatch(/点击.{0,8}重试/);
      expect(bytes.toString("utf8")).not.toContain("正式提交");
    }
  }
  const afterResponse = await page.request.get(`${product.baseUrl}${identity}`);
  expect(afterResponse.ok()).toBeTruthy();
  expect(await afterResponse.json()).toEqual(before);
  expect(writes).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("system-operation-handoff.png") });
});
