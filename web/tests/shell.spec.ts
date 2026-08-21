import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { attachFixedVisualPair } from "./support/fixed-reference";

type RunningProduct = {
  base_url: string;
  bootstrap_token: string;
};

const testDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(testDirectory, "../..");
let dataRoot = "";
let product: RunningProduct;

const SHELL_SNAPSHOT_MASK_ALLOWLIST = [
  {
    selector: ".lumen-connection code",
    reason: "monotonic Projection revision",
  },
  {
    selector: ".lumen-next-card .lumen-card-head small",
    reason: "monotonic Snapshot revision",
  },
] as const;

function startProduct(port = "0") {
  return JSON.parse(
    execFileSync(
      "uv",
      [
        "run",
        "meta-research",
        "start",
        "--data-root",
        dataRoot,
        "--port",
        port,
        "--json",
      ],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
    ),
  ) as RunningProduct;
}

function stopProductFor(targetDataRoot: string) {
  execFileSync(
    "uv",
    ["run", "meta-research", "stop", "--data-root", targetDataRoot, "--json"],
    { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
  );
}

function productStatusFor(targetDataRoot: string): { status: string } {
  return JSON.parse(
    execFileSync(
      "uv",
      ["run", "meta-research", "status", "--data-root", targetDataRoot, "--json"],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
    ),
  ) as { status: string };
}

function crashProduct() {
  const status = JSON.parse(
    execFileSync(
      "uv",
      ["run", "meta-research", "status", "--data-root", dataRoot, "--json"],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
    ),
  ) as { pid: number };
  process.kill(status.pid, "SIGKILL");
}

test.beforeEach(() => {
  dataRoot = mkdtempSync(join(tmpdir(), "meta-research-shell-e2e-"));
  product = startProduct();
});

test.afterEach(async ({ page }, testInfo) => {
  if (dataRoot) {
    await page.close().catch(() => undefined);
    const preservedRoot = dataRoot;
    dataRoot = "";
    if (testInfo.status !== testInfo.expectedStatus) {
      // Preserve daemon logs and SQLite/object evidence for the original failure.
      console.error(`preserving failed Shell E2E DataRoot: ${preservedRoot}`);
      try {
        const status = productStatusFor(preservedRoot);
        if (status.status === "running") stopProductFor(preservedRoot);
      } catch {
        // The test's original failure remains authoritative; evidence stays on disk.
      }
      return;
    }
    const status = productStatusFor(preservedRoot);
    if (status.status !== "running") {
      console.error(`preserving unexpectedly stopped Shell E2E DataRoot: ${preservedRoot}`);
      throw new Error(`meta-research daemon exited unexpectedly: ${status.status}`);
    }
    stopProductFor(preservedRoot);
    rmSync(preservedRoot, { recursive: true, force: true });
  }
});

async function authenticateBrowser(page: Page) {
  const session = JSON.parse(
    execFileSync(
      "uv",
      ["run", "meta-research", "session", "--data-root", dataRoot, "--json"],
      { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
    ),
  ) as { bootstrap_token: string };
  const exchanged = await page.request.post(`${product.base_url}/auth/bootstrap`, {
    data: { token: session.bootstrap_token },
    headers: { Origin: product.base_url },
  });
  expect(exchanged.ok()).toBeTruthy();
}

async function openAuthenticatedProduct(page: Page) {
  await authenticateBrowser(page);
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });
}

test("authenticated empty workspace keeps the fixed Lumen shell", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAuthenticatedProduct(page);

  const shell = page.getByTestId("product-shell");
  await expect(shell).toBeVisible();
  await expect(shell.getByRole("banner")).toBeVisible();
  await expect(shell.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(shell.getByRole("main")).toBeVisible();
  await expect(shell.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();

  const railNames = await shell
    .getByRole("navigation", { name: "主导航" })
    .getByRole("button")
    .evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-label")));
  expect(railNames).toEqual([
    "Quest 总览",
    "问题树",
    "Research Asset",
    "Writing",
    "历史",
    "HumanRequest",
    "创建 Quest",
    "用户入口",
  ]);

  const regions = await shell.evaluate((root) => {
    const box = (selector: string) => {
      const rect = root.querySelector(selector)?.getBoundingClientRect();
      if (!rect) throw new Error(`missing ${selector}`);
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return {
      header: box("[data-shell-region=header]"),
      rail: box("[data-shell-region=rail]"),
      main: box("[data-shell-region=main]"),
      companion: box("[data-shell-region=companion]"),
    };
  });

  expect(regions.header.x).toBeLessThan(regions.rail.x + 1);
  expect(regions.header.width).toBeGreaterThan(1_390);
  expect(regions.rail.width).toBeCloseTo(78, 0);
  expect(regions.rail.x).toBeLessThan(regions.main.x);
  expect(regions.main.x + regions.main.width).toBeLessThan(regions.companion.x);
  expect(regions.companion.width).toBeCloseTo(360, 0);
  await expect(page.getByRole("heading", { name: "这里还没有 Quest" })).toBeVisible();
  await expect(page.getByText("capability_unavailable", { exact: true }).first()).toBeVisible();
});

test("an SSE interruption warns over the last good snapshot without leaving the shell", async ({ page }) => {
  await openAuthenticatedProduct(page);
  await expect(page.getByText("Projection 实时连接", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "这里还没有 Quest" })).toBeVisible();

  const port = new URL(product.base_url).port;
  crashProduct();
  await expect.poll(() => {
    try {
      const status = JSON.parse(
        execFileSync(
          "uv",
          ["run", "meta-research", "status", "--data-root", dataRoot, "--json"],
          { cwd: repositoryRoot, encoding: "utf8", timeout: 20_000 },
        ),
      ) as { status: string };
      return status.status;
    } catch {
      return "stopped";
    }
  }).toBe("stopped");
  const warning = page.getByRole("alert");
  await expect(warning).toContainText("Projection 连接中断，正在重连");
  await expect(warning).toContainText("最后一次可用的单调 Snapshot");
  await expect(page.getByTestId("product-shell")).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();

  product = startProduct(port);
  await expect(page.getByText("Projection 实时连接", { exact: true })).toBeVisible();
  await expect(warning).toBeHidden();
});

test("an initial SSE failure warns over the last good snapshot before any connection opens", async ({ page }) => {
  await authenticateBrowser(page);
  await page.route("**/api/v1/events*", async (route) => {
    await route.abort("connectionrefused");
  });
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });

  await expect(page.getByRole("heading", { name: "这里还没有 Quest" })).toBeVisible();
  const warning = page.getByRole("alert");
  await expect(warning).toContainText("Projection 连接中断，正在重连");
  await expect(warning).toContainText("最后一次可用的单调 Snapshot");
  await expect(page.getByTestId("product-shell")).toBeVisible();
});

test("a generic projection event advances the Snapshot and reconnect cursor", async ({ page }) => {
  await authenticateBrowser(page);
  const initialResponse = await page.request.get(`${product.base_url}/api/v1/snapshot`);
  expect(initialResponse.ok()).toBeTruthy();
  const initialSnapshot = await initialResponse.json() as { revision: number };
  const nextRevision = initialSnapshot.revision + 7;
  let snapshotRequests = 0;
  await page.route("**/api/v1/snapshot", async (route) => {
    snapshotRequests += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...initialSnapshot,
        revision: snapshotRequests === 1 ? initialSnapshot.revision : nextRevision,
      }),
    });
  });
  const eventRequests: string[] = [];
  await page.route("**/api/v1/events*", async (route) => {
    eventRequests.push(route.request().url());
    if (eventRequests.length === 1) {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: `id: ${nextRevision}\nevent: projection.updated\ndata: {"revision":${nextRevision}}\n\n`,
      });
      return;
    }
    await route.abort("connectionrefused");
  });
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });

  await expect(page.locator(".lumen-connection code")).toHaveText(`rev ${nextRevision}`);
  expect(snapshotRequests).toBeGreaterThanOrEqual(2);
  await expect.poll(() => eventRequests.length).toBeGreaterThan(1);
  const reconnectUrl = new URL(eventRequests.at(-1)!);
  expect(Number(reconnectUrl.searchParams.get("after"))).toBe(nextRevision);
});

test("a gap reload retries after one 503 without requiring another SSE event", async ({
  page,
}) => {
  await authenticateBrowser(page);
  const initialResponse = await page.request.get(`${product.base_url}/api/v1/snapshot`);
  expect(initialResponse.ok()).toBeTruthy();
  const initialSnapshot = await initialResponse.json() as { revision: number };
  const gapRevision = initialSnapshot.revision + 9;
  let snapshotRequests = 0;
  let releaseRecovery!: () => void;
  const recoveryGate = new Promise<void>((resolveRecovery) => {
    releaseRecovery = resolveRecovery;
  });
  await page.route("**/api/v1/snapshot", async (route) => {
    snapshotRequests += 1;
    if (snapshotRequests === 2) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "snapshot_temporarily_unavailable" } }),
      });
      return;
    }
    if (snapshotRequests >= 3) await recoveryGate;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        ...initialSnapshot,
        revision: snapshotRequests >= 3 ? gapRevision : initialSnapshot.revision,
      }),
    });
  });
  let eventRequests = 0;
  await page.route("**/api/v1/events*", async (route) => {
    eventRequests += 1;
    if (eventRequests === 1) {
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        headers: { "Cache-Control": "no-cache" },
        body: `id: ${gapRevision}\nevent: snapshot.required\ndata: {"snapshot_url":"/api/v1/snapshot","snapshot_revision":${gapRevision}}\n\n`,
      });
      return;
    }
    await route.abort("connectionrefused");
  });
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });

  await expect.poll(() => snapshotRequests).toBeGreaterThanOrEqual(2);
  await expect(page.getByRole("alert")).toContainText(
    "继续显示最后一次可用的单调 Snapshot",
  );
  await expect.poll(() => snapshotRequests).toBeGreaterThanOrEqual(3);
  releaseRecovery();
  await expect(page.locator(".lumen-connection code")).toHaveText(
    `rev ${gapRevision}`,
    { timeout: 8_000 },
  );
  expect(eventRequests).toBeGreaterThanOrEqual(1);
});

test("a durable event burst stays monotonic and reconnects from the advanced SSE cursor", async ({ page }) => {
  test.setTimeout(60_000);
  const eventRequests: string[] = [];
  let snapshotRequests = 0;
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname === "/api/v1/events") eventRequests.push(request.url());
    if (url.pathname === "/api/v1/snapshot") snapshotRequests += 1;
  });
  await openAuthenticatedProduct(page);
  await expect(page.getByText("Projection 实时连接", { exact: true })).toBeVisible();
  const snapshotsBeforeBurst = snapshotRequests;

  const finalRevision = await page.evaluate(async () => {
    const csrf = document.cookie
      .split(";")
      .map((part) => part.trim())
      .find((part) => part.startsWith("meta_research_csrf="))
      ?.split("=")[1];
    if (!csrf) throw new Error("missing public CSRF cookie");

    const write = async (path: string, method: "POST" | "PUT", body: object) => {
      const response = await fetch(path, {
        method,
        credentials: "same-origin",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": crypto.randomUUID(),
          "X-CSRF-Token": decodeURIComponent(csrf),
        },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`write failed: ${response.status}`);
      return response.json();
    };

    let creation = await write("/api/v1/quest-initializations", "POST", {});
    let draft = {
      ...creation.quest_draft.value,
      goal: "验证大量 durable events 后的公开 Projection 单调性",
      completion_criteria: "浏览器保持最新 Snapshot 且重连不回放全部历史",
    };
    for (let index = 0; index < 220; index += 1) {
      draft = {
        ...draft,
        background_and_initial_direction: `真实 loopback daemon · burst ${index}`,
      };
      creation = await write(
        `/api/v1/quest-initializations/${creation.initialization_id}/draft`,
        "PUT",
        {
          expected_draft_revision: creation.quest_draft.revision,
          expected_draft_hash: creation.quest_draft.hash,
          draft,
        },
      );
    }
    const snapshot = await fetch("/api/v1/snapshot", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    }).then((response) => response.json());
    return snapshot.revision as number;
  });

  const headerRevision = page.locator(".lumen-connection code");
  await expect(headerRevision).toHaveText(`rev ${finalRevision}`);
  expect(snapshotRequests - snapshotsBeforeBurst).toBeLessThan(30);

  const requestsBeforeReconnect = eventRequests.length;
  const port = new URL(product.base_url).port;
  crashProduct();
  await expect(page.getByRole("alert")).toContainText("Projection 连接中断");
  product = startProduct(port);
  await expect(page.getByText("Projection 实时连接", { exact: true })).toBeVisible();
  await expect.poll(() => eventRequests.length).toBeGreaterThan(requestsBeforeReconnect);

  const reconnectUrl = new URL(eventRequests.at(-1)!);
  expect(Number(reconnectUrl.searchParams.get("after"))).toBeGreaterThanOrEqual(finalRevision);
  await expect(headerRevision).toHaveText(`rev ${finalRevision}`);
});

test("the fixed regions keep their 800px and 390px order without page overflow", async ({ page }) => {
  await openAuthenticatedProduct(page);

  await page.setViewportSize({ width: 800, height: 900 });
  const shell = page.getByTestId("product-shell");
  const regionsAt = async () => shell.evaluate((root) => {
    const box = (name: string) => {
      const rect = root.querySelector(`[data-shell-region=${name}]`)?.getBoundingClientRect();
      if (!rect) throw new Error(`missing ${name}`);
      return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
    };
    return {
      header: box("header"),
      rail: box("rail"),
      main: box("main"),
      companion: box("companion"),
      viewportWidth: window.innerWidth,
      pageWidth: document.documentElement.scrollWidth,
    };
  });

  const tablet = await regionsAt();
  expect(tablet.rail.x).toBeLessThan(tablet.main.x);
  expect(tablet.companion.x).toBeCloseTo(tablet.main.x, 0);
  expect(tablet.companion.y).toBeGreaterThanOrEqual(tablet.main.y + tablet.main.height);
  expect(tablet.pageWidth).toBeLessThanOrEqual(tablet.viewportWidth);

  await page.setViewportSize({ width: 390, height: 844 });
  const mobile = await regionsAt();
  expect(mobile.header.y).toBeLessThan(mobile.rail.y);
  expect(mobile.rail.y).toBeLessThan(mobile.main.y);
  expect(mobile.main.y).toBeLessThan(mobile.companion.y);
  expect(mobile.pageWidth).toBeLessThanOrEqual(mobile.viewportWidth);
  const railTargets = await page
    .getByRole("navigation", { name: "主导航" })
    .getByRole("button")
    .evaluateAll((buttons) => buttons.map((button) => {
      const rect = button.getBoundingClientRect();
      return { width: rect.width, height: rect.height };
    }));
  expect(railTargets.every(({ width, height }) => width >= 44 && height >= 44)).toBeTruthy();
});

test("skip navigation, visible focus, and reduced motion remain inside the fixed hierarchy", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await openAuthenticatedProduct(page);
  expect(await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches)).toBeTruthy();

  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "跳到主要内容" });
  await expect(skip).toBeFocused();
  expect(await skip.evaluate((element) => getComputedStyle(element).outlineStyle)).not.toBe("none");
  await page.keyboard.press("Enter");
  const main = page.getByRole("main");
  await expect(main).toBeFocused();

  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "创建 Quest" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Research Asset" })).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("button", { name: "Quest 总览" })).toBeFocused();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  await expect(main).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByText("查看运行详情", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("complementary", { name: "Quest Companion" })).toBeFocused();

  const transitionDuration = await skip.evaluate(
    (element) => getComputedStyle(element).transitionDuration,
  );
  expect(Number.parseFloat(transitionDuration)).toBeLessThanOrEqual(0.001);
});

test("loading and a first Snapshot failure stay inside the shell and recover in place", async ({ page }) => {
  await authenticateBrowser(page);
  let releaseFailure!: () => void;
  const failureGate = new Promise<void>((resolve) => { releaseFailure = resolve; });
  let snapshotAttempt = 0;
  await page.route("**/api/v1/snapshot", async (route) => {
    snapshotAttempt += 1;
    if (snapshotAttempt === 1) {
      await failureGate;
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: { code: "snapshot_unavailable" } }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });

  const shell = page.getByTestId("product-shell");
  await expect(shell).toHaveAttribute("data-shell-state", "loading");
  await expect(shell.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(shell.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();
  releaseFailure();

  await expect(shell).toHaveAttribute("data-shell-state", "first-error");
  await expect(page.getByRole("alert")).toContainText("研究空间暂时无法读取");
  await expect(shell.getByRole("navigation", { name: "主导航" })).toBeVisible();
  await expect(shell.getByRole("complementary", { name: "Quest Companion" })).toBeVisible();
  await page.getByRole("button", { name: "重新读取 Snapshot" }).click();
  await expect(shell).toHaveAttribute("data-shell-state", "ready-empty");
});

test("a readiness-unavailable Projection uses the same hero and Companion grammar", async ({ page }) => {
  await authenticateBrowser(page);
  const response = await page.request.get(`${product.base_url}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const unavailableSnapshot = await response.json();
  unavailableSnapshot.readiness = {
    status: "unavailable",
    checks: unavailableSnapshot.readiness.checks.map((check: { name: string }, index: number) => ({
      ...check,
      status: index === 0 ? "unavailable" : "ready",
    })),
  };
  await page.route("**/api/v1/snapshot", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(unavailableSnapshot),
    });
  });
  await page.goto(product.base_url, { waitUntil: "domcontentloaded" });

  const shell = page.getByTestId("product-shell");
  await expect(shell).toHaveAttribute("data-shell-state", "readiness-unavailable");
  await expect(page.getByRole("heading", { name: /Snapshot 已返回/ })).toBeVisible();
  await expect(page.getByText("readiness_unavailable", { exact: true })).toBeVisible();
  await expect(page.getByRole("complementary", { name: "Quest Companion" })).toContainText("底座尚未就绪");
  await expect(page.getByText("capability_unavailable", { exact: true }).first()).toBeVisible();
});

test("the ready-empty Lumen shell keeps its visual contract at the fixed widths", async (
  { page },
  testInfo,
) => {
  await openAuthenticatedProduct(page);
  await expect(page.getByTestId("product-shell")).toHaveAttribute("data-shell-state", "ready-empty");
  await expect(page.getByText("Projection 实时连接", { exact: true })).toBeVisible();
  const unavailableLabels = await page.locator(".lumen-availability li span").allTextContents();
  expect(unavailableLabels).toEqual([
    "Research Asset",
    "首问题 DeepFetch",
    "Quest Companion",
    "Stage 执行",
    "Writing",
  ]);
  await expect(
    page.locator(".lumen-availability li").filter({ hasText: "Research Asset" }),
  ).toContainText("ready");

  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 800, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(page).toHaveScreenshot(`lumen-ready-empty-${viewport.width}.png`, {
      animations: "disabled",
      fullPage: true,
      mask: SHELL_SNAPSHOT_MASK_ALLOWLIST.map(({ selector }) => page.locator(selector)),
      maskColor: "#dfe4ee",
      maxDiffPixelRatio: 0.001,
    });
    await attachFixedVisualPair(page, testInfo, "shell", viewport);
  }
});
