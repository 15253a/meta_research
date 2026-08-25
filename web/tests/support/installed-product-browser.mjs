import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";

import { chromium } from "playwright";


const require = createRequire(import.meta.url);
const playwrightVersion = require("playwright/package.json").version;
const statePath = process.argv[2];
const chromePath = "/usr/bin/google-chrome";

assert.equal(playwrightVersion, "1.55.1", "installed Web acceptance must use locked Playwright 1.55.1");
assert.ok(statePath, "usage: installed-product-browser.mjs <browser-session.json>");
assert.ok(existsSync(resolve(statePath)), `browser session does not exist: ${statePath}`);
assert.ok(existsSync(chromePath), `system Chrome does not exist: ${chromePath}`);

const state = JSON.parse(readFileSync(resolve(statePath), "utf8"));
const baseUrl = new URL(state.base_url);
const expected = state.expected;
const viewports = [
  { width: 1440, height: 900 },
  { width: 800, height: 900 },
  { width: 390, height: 844 },
];

assert.equal(baseUrl.hostname, "127.0.0.1", "installed browser must stay on loopback");
assert.equal(baseUrl.protocol, "http:", "installed browser expects loopback HTTP");
assert.ok(Array.isArray(state.cookies) && state.cookies.length >= 2, "authenticated cookie state is incomplete");

const browser = await chromium.launch({
  executablePath: chromePath,
  headless: true,
});

async function visible(locator, description) {
  await locator.waitFor({ state: "visible", timeout: 15_000 });
  assert.ok(await locator.isVisible(), `${description} is not visible`);
}

async function contains(locator, value, description) {
  await visible(locator, description);
  await locator.filter({ hasText: value }).waitFor({
    state: "visible",
    timeout: 15_000,
  });
  const text = await locator.innerText();
  assert.ok(text.includes(value), `${description} does not contain ${JSON.stringify(value)}: ${text}`);
}

async function assertNoHorizontalOverflow(page, description) {
  const geometry = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  assert.ok(
    geometry.document <= geometry.viewport && geometry.body <= geometry.viewport,
    `${description} overflows horizontally: ${JSON.stringify(geometry)}`,
  );
}

async function closeObserver(page) {
  const observer = page.getByTestId("execution-observer");
  if (await observer.getAttribute("data-open") !== "true") return;
  await observer.getByRole("button", { name: "关闭当前实验观测窗" }).click();
  await page.waitForFunction(() => (
    document.querySelector("[data-testid=execution-observer]")?.getAttribute("data-open") === "false"
  ));
}

const shellSignatures = [];

try {
  for (const viewport of viewports) {
    const context = await browser.newContext({ viewport });
    await context.addCookies(state.cookies.map((cookie) => ({
      ...cookie,
      url: baseUrl.origin,
      sameSite: "Strict",
      secure: false,
    })));
    const page = await context.newPage();
    const researchWrites = [];
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        url.origin === baseUrl.origin
        && url.pathname.startsWith("/api/v1/")
        && ["POST", "PUT", "PATCH", "DELETE"].includes(request.method())
      ) {
        researchWrites.push(`${request.method()} ${url.pathname}`);
      }
    });

    try {
      const response = await page.goto(baseUrl.origin, { waitUntil: "domcontentloaded" });
      assert.ok(response?.ok(), `installed wheel shell returned ${response?.status()}`);
      assert.equal(new URL(page.url()).origin, baseUrl.origin, "browser escaped installed loopback origin");

      const shell = page.getByTestId("product-shell");
      await visible(shell, "Lumen Shell");
      await visible(shell.getByRole("banner"), "Lumen header");
      await visible(shell.getByRole("navigation", { name: "主导航" }), "Lumen rail");
      await visible(page.getByRole("complementary", { name: "Quest Companion" }), "Quest Companion");
      await closeObserver(page);

      const currentQuestion = page.getByTestId("current-question-card");
      await contains(currentQuestion, expected.quest_ref, "current Quest projection");
      await contains(currentQuestion, expected.question_ref, "current Question projection");
      await contains(currentQuestion, expected.stage, "current Stage projection");
      await visible(
        page.getByRole("list", { name: "当前研究周期的四个 Stage" }),
        "four-Stage strip",
      );
      await assertNoHorizontalOverflow(page, `${viewport.width}px overview`);

      const experimentEntry = currentQuestion.getByRole("button", {
        name: "当前实验 · stdout",
      });
      await visible(experimentEntry, "current experiment stdout entry");
      await experimentEntry.click();
      const observer = page.getByTestId("execution-observer");
      await page.waitForFunction(() => (
        document.querySelector("[data-testid=execution-observer]")?.getAttribute("data-open") === "true"
      ));
      await contains(observer, expected.experiment_title, "current experiment observer");
      await contains(observer, expected.execution_fence_key, "current Execution Fence");
      await visible(
        observer.getByRole("log", { name: /当前实验原始标准输出/ }),
        "current experiment stdout",
      );
      await closeObserver(page);

      await page.getByRole("button", { name: "问题树", exact: true }).click();
      const tree = page.getByTestId("question-tree");
      await visible(tree, "production QuestionTree");
      await visible(
        tree.locator(`[data-question-ref=${JSON.stringify(expected.question_ref)}]`),
        "accepted Question in QuestionTree",
      );
      const layout = viewport.width <= 620 ? "outline" : "canvas";
      assert.equal(
        await tree.locator(".question-tree-canvas").getAttribute("data-layout-mode"),
        layout,
        `QuestionTree uses wrong ${viewport.width}px layout`,
      );

      const treeExperimentEntry = tree.getByRole("button", {
        name: "当前实验 · stdout",
      });
      await visible(treeExperimentEntry, "QuestionTree current experiment entry");
      assert.equal(await treeExperimentEntry.isEnabled(), true, "QuestionTree current experiment entry is disabled");
      await treeExperimentEntry.click();
      await page.waitForFunction(() => (
        document.querySelector("[data-testid=execution-observer]")?.getAttribute("data-open") === "true"
      ));
      await contains(observer, expected.execution_fence_key, "QuestionTree-reopened Execution Fence");
      await closeObserver(page);

      await tree.getByRole("button", { name: "问题历史 ↗" }).click();
      const questionHistory = tree.getByRole("region", { name: "问题历史" });
      await contains(questionHistory, expected.question_ref, "installed Question history identity");
      assert.equal(
        new URL(page.url()).searchParams.get("inspector"),
        "history",
        "Question history did not retain its deep-link state",
      );

      await tree.getByRole("button", { name: "查看证据与来源" }).click();
      const questionEvidence = tree.getByRole("region", { name: "问题证据与来源" });
      await contains(
        questionEvidence,
        "question_evidence_refs_empty",
        "installed Question evidence typed absence",
      );
      assert.equal(
        new URL(page.url()).searchParams.get("inspector"),
        "evidence",
        "Question evidence did not retain its deep-link state",
      );
      await assertNoHorizontalOverflow(page, `${viewport.width}px QuestionTree`);

      await tree.getByRole("button", { name: "回到总览" }).click();
      const historyRail = shell.getByRole("button", { name: "历史", exact: true });
      await visible(historyRail, "History rail entry");
      assert.equal(await historyRail.isEnabled(), true, "History rail entry is disabled");
      await historyRail.click();
      await contains(
        page.getByRole("region", { name: "问题历史" }),
        expected.question_ref,
        "History rail Question identity",
      );
      await page.getByTestId("question-tree").getByRole("button", { name: "回到总览" }).click();
      await page.waitForFunction(() => (
        document.activeElement === document.querySelector(
          '[data-testid="product-shell"] nav[aria-label="主导航"] button[aria-label="历史"]',
        )
      ), undefined, { timeout: 15_000 });
      assert.equal(
        await historyRail.evaluate((button) => document.activeElement === button),
        true,
        "closing History did not restore focus to its rail opener",
      );

      await assertNoHorizontalOverflow(page, `${viewport.width}px History return`);
      await page.waitForTimeout(100);
      assert.deepEqual(
        researchWrites,
        [],
        `${viewport.width}px read-only Web traversal emitted research writes`,
      );

      const signature = {
        brand: await shell.locator(".lumen-brand").getAttribute("aria-label"),
        companion: await page.getByRole("complementary", { name: "Quest Companion" })
          .locator(".lumen-companion-head b")
          .innerText(),
        rail: await shell.getByRole("navigation", { name: "主导航" })
          .getByRole("button")
          .evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-label"))),
      };
      shellSignatures.push(signature);
    } finally {
      await context.close();
    }
  }

  assert.deepEqual(shellSignatures[1], shellSignatures[0], "800px did not retain the same Lumen Shell");
  assert.deepEqual(shellSignatures[2], shellSignatures[0], "390px did not retain the same Lumen Shell");
  process.stdout.write(
    `installed wheel browser verification passed (${browser.version()}, Playwright ${playwrightVersion}; 1440/800/390; 0 research writes)\n`,
  );
} finally {
  await browser.close();
}
