import { expect, test } from "@playwright/test";
import { DeterministicProduct } from "./support/deterministic-product";

test("v4 windows use real product facts and keep editable HumanRequest reading state", async ({ page }, testInfo) => {
  test.setTimeout(90_000);
  const product = await DeterministicProduct.start({ manualRoot: true, humanRequestHandoff: true });
  try {
    await product.authenticate(page);
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`${product.baseUrl}/?panel=offline-operation`);
    const human = page.getByRole("dialog", { name: "需要你处理的事项", exact: true });
    await expect(human.getByRole("heading", { name: "提供温度传感器原始校准记录。", exact: true })).toBeVisible();
    const note = human.getByRole("textbox", { name: "自然语言回应", exact: true });
    await note.fill("未提交：现场设备编号尚待核对。");
    const assistant = human.getByRole("textbox", { name: "就线下操作事项发消息" });
    await assistant.fill("中文输入尚未完成");
    const sent: string[] = [];
    page.on("request", request => {
      if (request.method() === "POST" && /\/(responses|messages)$/.test(new URL(request.url()).pathname)) sent.push(request.url());
    });
    await assistant.dispatchEvent("compositionstart");
    await assistant.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
    await assistant.dispatchEvent("compositionend");
    await expect(assistant).toHaveValue("中文输入尚未完成");
    const details = human.locator("details.hc-request-details");
    await details.locator("summary").click();
    const core = human.locator(".hc-request-core");
    await core.hover();
    await page.mouse.wheel(0, 250);
    await expect.poll(() => core.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
    const position = await core.evaluate(element => element.scrollTop);
    await human.getByRole("button", { name: "关闭需要你处理的事项", exact: true }).click();
    await page.getByRole("button", { name: "需要你", exact: true }).click();
    await expect(note).toHaveValue("未提交：现场设备编号尚待核对。");
    await expect(details).toHaveAttribute("open", "");
    await expect.poll(() => core.evaluate(element => element.scrollTop)).toBeGreaterThanOrEqual(position - 2);
    for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 500 }, { width: 390, height: 600 }]) {
      await page.setViewportSize(viewport);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
      await page.screenshot({ path: testInfo.outputPath(`product-human-${viewport.width}.png`) });
    }
    await human.getByRole("button", { name: "关闭需要你处理的事项", exact: true }).click();
    await page.getByRole("button", { name: "修改配置", exact: true }).click();
    const config = page.getByRole("dialog", { name: "修改配置", exact: true });
    await expect(config.getByRole("region", { name: "图书馆与全文获取" })).toBeVisible();
    await expect(config.getByLabel("图书馆／数据库入口链接", { exact: true })).toBeEnabled();
    await expect(config.getByRole("region", { name: "系统配置", exact: true })).toBeVisible();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 500 }, { width: 390, height: 600 }]) {
      await page.setViewportSize(viewport);
      expect(await config.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
      await page.screenshot({ path: testInfo.outputPath(`product-config-${viewport.width}.png`) });
    }
    await config.getByRole("button", { name: "关闭修改配置", exact: true }).click();
    await page.getByRole("button", { name: "创建研究任务", exact: true }).click();
    const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题", exact: true });
    await expect(quest.getByRole("navigation", { name: "创建准备阶段" })).toBeVisible();
    await expect(quest.getByLabel("目标", { exact: true })).toBeEnabled();
    for (const viewport of [{ width: 1440, height: 900 }, { width: 800, height: 500 }, { width: 390, height: 600 }]) {
      await page.setViewportSize(viewport);
      expect(await quest.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBeTruthy();
      await page.screenshot({ path: testInfo.outputPath(`product-quest-${viewport.width}.png`) });
    }
    expect(sent).toEqual([]);
  } finally {
    await page.close();
    await product.stop();
  }
});
