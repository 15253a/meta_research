import { expect, type Locator, type Page } from "@playwright/test";

export async function selectServerMaterial(page: Page, consumer: Locator, path: string, description: string) {
  await consumer.getByRole("button", { name: "选择服务器文件或目录", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择服务器上的原始材料" });
  const select = picker.getByRole("button", { name: "选择此路径", exact: true });
  await expect(select).toBeEnabled();
  await picker.getByLabel("服务器绝对路径", { exact: true }).fill(path);
  await picker.getByLabel("原始材料说明", { exact: true }).fill(description);
  await select.click();
  await expect(picker).toBeHidden();
  await expect(consumer.locator(".server-material-picker__candidate")).toContainText(path);
}

export async function readSelectedServerMaterial(page: Page, consumer: Locator, path: string, description = "") {
  const inspected = page.waitForResponse(response => response.url().includes("/api/v1/server-materials/inspect?") && response.request().method() === "GET");
  await selectServerMaterial(page, consumer, path, description);
  const response = await inspected;
  expect(response.ok()).toBeTruthy();
  return await response.json() as import("../../src/workMaterialApi").ServerMaterialSelection;
}
