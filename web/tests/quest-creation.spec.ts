import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type RunningProduct = { base_url: string };

const testDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(testDirectory, "../..");
let dataRoot = "";
let product: RunningProduct;

// Fixed product-design contract copied from the accepted #107 prototype at
// d7e2c9b7. Intentional production deviations must be added here explicitly;
// screenshots alone are not an authority for these structural/motion tokens.
const QUEST_CREATION_PROTOTYPE = {
  sourceCommit: "d7e2c9b7",
  openTransform: "matrix(1, 0, 0, 1, 0, 0)",
  closedTranslateY: 24,
  closedScale: 0.985,
  opacityDurationSeconds: 0.22,
  transformDurationSeconds: 0.24,
  requiredQuestionFields: 4,
  questionFields: 6,
} as const;

type MotionSample = {
  opacity: number;
  scaleX: number;
  scaleY: number;
  translateY: number;
};

async function observeInitialModalMotion(page: Page) {
  await page.evaluate(() => {
    const target = window as unknown as {
      questInitialMotion?: MotionSample;
      questMotionObserver?: MutationObserver;
    };
    target.questInitialMotion = undefined;
    target.questMotionObserver?.disconnect();
    const capture = () => {
      const dialog = document.querySelector(".quest-dialog[data-open='false']");
      if (!(dialog instanceof HTMLDialogElement)) return;
      const style = getComputedStyle(dialog);
      const matrix = new DOMMatrixReadOnly(style.transform);
      target.questInitialMotion = {
        opacity: Number(style.opacity),
        scaleX: matrix.a,
        scaleY: matrix.d,
        translateY: matrix.f,
      };
    };
    const observer = new MutationObserver(capture);
    observer.observe(document.body, { childList: true, subtree: true });
    target.questMotionObserver = observer;
  });
}

async function initialModalMotion(page: Page): Promise<MotionSample> {
  await expect.poll(() => page.evaluate(() => (
    window as unknown as { questInitialMotion?: MotionSample }
  ).questInitialMotion ?? null)).not.toBeNull();
  return page.evaluate(() => (
    window as unknown as { questInitialMotion: MotionSample }
  ).questInitialMotion);
}

function runProductCommand(...args: string[]) {
  return JSON.parse(
    execFileSync("uv", ["run", "meta-research", ...args, "--json"], {
      cwd: repositoryRoot,
      encoding: "utf8",
      timeout: 20_000,
    }),
  ) as Record<string, unknown>;
}

test.beforeAll(() => {
  dataRoot = mkdtempSync(join(tmpdir(), "meta-research-quest-e2e-"));
  product = runProductCommand(
    "start",
    "--data-root",
    dataRoot,
    "--port",
    "0",
  ) as unknown as RunningProduct;
});

test.afterAll(() => {
  if (!dataRoot) return;
  try {
    runProductCommand("stop", "--data-root", dataRoot);
  } finally {
    rmSync(dataRoot, { recursive: true, force: true });
  }
});

async function openAuthenticatedProduct(page: Page) {
  const issued = runProductCommand("session", "--data-root", dataRoot) as {
    bootstrap_token: string;
  };
  const exchanged = await page.request.post(`${product.base_url}/auth/bootstrap`, {
    data: { token: issued.bootstrap_token },
    headers: { Origin: product.base_url },
  });
  expect(exchanged.ok()).toBeTruthy();
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });
}

async function openCreation(page: Page) {
  const opener = page.getByRole("button", { name: "创建 Quest" });
  await opener.click();
  const dialog = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("data-open", "true");
  await expect.poll(async () => dialog.evaluate((element) => {
    const style = getComputedStyle(element);
    return { opacity: style.opacity, transform: style.transform };
  })).toEqual({
    opacity: "1",
    transform: QUEST_CREATION_PROTOTYPE.openTransform,
  });
  return { dialog, opener };
}

test("create Quest is one continuous production window with fixed responsibilities", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page);
  const { dialog } = await openCreation(page);

  await expect(dialog.getByText("QST", { exact: true })).toBeVisible();
  await expect(dialog.getByText("首次创建专用", { exact: true })).toBeVisible();
  await expect(
    dialog.getByRole("heading", {
      name: "创建 Quest，并决定第一个研究问题",
    }),
  ).toBeVisible();

  const form = dialog.getByTestId("quest-continuous-form");
  const session = dialog.getByRole("complementary", {
    name: "讨论 Quest 与第一问",
  });
  const footer = dialog.getByTestId("quest-confirmation-footer");
  await expect(form).toBeVisible();
  await expect(session).toBeVisible();
  await expect(footer).toBeVisible();

  const geometry = await dialog.evaluate((root) => {
    const rect = (selector: string) => {
      const element = root.querySelector(selector);
      if (!(element instanceof HTMLElement)) throw new Error(`missing ${selector}`);
      const box = element.getBoundingClientRect();
      return { x: box.x, y: box.y, width: box.width, bottom: box.bottom };
    };
    return {
      form: rect("[data-testid=quest-continuous-form]"),
      session: rect("[data-testid=quest-intent-session]"),
      footer: rect("[data-testid=quest-confirmation-footer]"),
    };
  });
  expect(geometry.form.x + geometry.form.width).toBeLessThanOrEqual(
    geometry.session.x + 1,
  );
  expect(geometry.session.width).toBeGreaterThanOrEqual(300);
  expect(geometry.session.width).toBeLessThanOrEqual(350);
  expect(geometry.footer.y).toBeGreaterThan(geometry.form.y);

  const visibleSections = await form
    .locator("[data-journey-section]")
    .evaluateAll((sections) => sections.map((section) => section.getAttribute("data-journey-section")));
  expect(visibleSections).toEqual([
    "goal",
    "configuration",
    "literature",
    "optional-basis",
    "route",
    "question-proposal",
  ]);

  await expect(form.getByLabel("这个 Quest 最终要完成什么？")).toBeVisible();
  await expect(form.getByLabel("什么情况算完成？")).toBeVisible();
  await expect(form.getByLabel("时间预算")).toBeVisible();
  await expect(form.getByRole("button", { name: "检测本机计算卡" })).toBeVisible();
  await expect(form.getByText("Quest Resource Envelope：", { exact: true })).toBeVisible();
  await expect(form.getByLabel("文献搜索范围")).toHaveValue("oa_then_institution");
  await expect(form.getByLabel("文献搜索范围").locator("option")).toHaveCount(3);
  await expect(form.getByText("Google Chrome", { exact: false })).toBeVisible();
  await expect(form.getByRole("button", { name: "先运行 DeepFetch" })).toBeDisabled();
  await expect(form.getByText("capability_unavailable", { exact: true }).first()).toBeVisible();
  await expect(form.getByRole("button", { name: "直接根据目标生成" })).toBeEnabled();

  await expect(session.getByText("INTENT DRAFTING SESSION", { exact: true })).toBeVisible();
  await expect(session.getByText("聊天不会直接创建 Quest。", { exact: true })).toBeVisible();
  await expect(session.getByLabel("在 Quest Drafting Session 中发消息")).toBeVisible();

  const footerButtons = await footer
    .getByRole("button")
    .evaluateAll((buttons) => buttons.map((button) => button.textContent?.trim()));
  expect(footerButtons).toEqual(["取消", "确认创建 Quest 与第一个问题"]);
  await expect(dialog.getByRole("button", { name: "保存修改" })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "生成影响预览" })).toHaveCount(0);
});

test("the accepted #107 modal motion contract remains explicit", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page);
  await observeInitialModalMotion(page);
  const { dialog, opener } = await openCreation(page);

  expect(await initialModalMotion(page)).toEqual({
    opacity: 0,
    scaleX: QUEST_CREATION_PROTOTYPE.closedScale,
    scaleY: QUEST_CREATION_PROTOTYPE.closedScale,
    translateY: QUEST_CREATION_PROTOTYPE.closedTranslateY,
  });

  await expect(dialog).toHaveAttribute("data-open", "true");
  await expect(dialog).toHaveCSS("opacity", "1");
  await expect(dialog).toHaveCSS(
    "transform",
    QUEST_CREATION_PROTOTYPE.openTransform,
  );

  const motion = await dialog.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      source: element.getAttribute("data-prototype-source"),
      open: element.getAttribute("data-open"),
      opacity: style.opacity,
      transform: style.transform,
      transitionDurations: style.transitionDuration
        .split(",")
        .map((value) => Number.parseFloat(value)),
      transitionProperties: style.transitionProperty.split(",").map((value) => value.trim()),
    };
  });
  expect(motion.source).toBe(QUEST_CREATION_PROTOTYPE.sourceCommit);
  expect(motion.open).toBe("true");
  expect(motion.opacity).toBe("1");
  expect(motion.transform).toBe(QUEST_CREATION_PROTOTYPE.openTransform);
  expect(motion.transitionProperties).toEqual(["opacity", "transform", "visibility"]);
  expect(motion.transitionDurations).toEqual([
    QUEST_CREATION_PROTOTYPE.opacityDurationSeconds,
    QUEST_CREATION_PROTOTYPE.transformDurationSeconds,
    QUEST_CREATION_PROTOTYPE.opacityDurationSeconds,
  ]);

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toHaveAttribute("data-open", "false");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test("reduced motion keeps the same modal endpoints without a visible transition", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openAuthenticatedProduct(page);
  await observeInitialModalMotion(page);
  const { dialog, opener } = await openCreation(page);

  expect(await initialModalMotion(page)).toEqual({
    opacity: 0,
    scaleX: QUEST_CREATION_PROTOTYPE.closedScale,
    scaleY: QUEST_CREATION_PROTOTYPE.closedScale,
    translateY: QUEST_CREATION_PROTOTYPE.closedTranslateY,
  });
  await expect(dialog).toHaveCSS("opacity", "1");
  await expect(dialog).toHaveCSS(
    "transform",
    QUEST_CREATION_PROTOTYPE.openTransform,
  );
  const durations = await dialog.evaluate((element) => (
    getComputedStyle(element).transitionDuration
      .split(",")
      .map((value) => Number.parseFloat(value))
  ));
  expect(durations.every((duration) => duration <= 0.001)).toBeTruthy();

  await dialog.getByRole("button", { name: "关闭创建 Quest 窗口" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test("draft autosaves, closes without cancellation, and restores from the fixed opener", async ({
  page,
}) => {
  await page.setViewportSize({ width: 800, height: 900 });
  await openAuthenticatedProduct(page);
  const { dialog, opener } = await openCreation(page);
  const goal = dialog.getByLabel("这个 Quest 最终要完成什么？");
  await goal.fill("确定低照度显微图像去噪的可证伪边界");
  await dialog.getByLabel("什么情况算完成？").fill("形成证据、反例和适用范围");
  // Closing must flush the latest controlled values even when the debounce has
  // not fired and the currently focused field has never blurred.
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("这个 Quest 最终要完成什么？")).toHaveValue(
    "确定低照度显微图像去噪的可证伪边界",
  );
  await expect(dialog.getByLabel("什么情况算完成？")).toHaveValue(
    "形成证据、反例和适用范围",
  );

  await dialog.getByLabel("这个 Quest 最终要完成什么？").fill(
    "遮罩关闭也必须保留精确 durable 草案",
  );
  await page.mouse.click(3, 3);
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();

  await opener.click();
  await expect(dialog).toBeVisible();
  await expect(dialog.getByLabel("这个 Quest 最终要完成什么？")).toHaveValue(
    "遮罩关闭也必须保留精确 durable 草案",
  );
});

test("small viewport stacks the form before the persistent drafting session", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openAuthenticatedProduct(page);
  const { dialog } = await openCreation(page);

  const geometry = await dialog.evaluate((root) => {
    const form = root.querySelector("[data-testid=quest-continuous-form]")!.getBoundingClientRect();
    const session = root.querySelector("[data-testid=quest-intent-session]")!.getBoundingClientRect();
    return {
      formBottom: form.bottom,
      sessionTop: session.top,
      viewportWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    };
  });
  expect(geometry.sessionTop).toBeGreaterThanOrEqual(geometry.formBottom - 1);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.viewportWidth);

  const close = dialog.getByRole("button", { name: "关闭创建 Quest 窗口" });
  const cancel = dialog.getByRole("button", { name: "取消" });
  const confirm = dialog.getByRole("button", {
    name: "确认创建 Quest 与第一个问题",
  });
  for (const target of [close, cancel, confirm]) {
    const box = await target.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    expect(box?.width ?? 0).toBeGreaterThanOrEqual(44);
  }
});
