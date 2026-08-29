import { expect, test, type Page, type Route } from "@playwright/test";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";

type JsonRecord = Record<string, unknown>;

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

test("Quest materials use one server directory path without browser upload", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  let intakeBody: JsonRecord | null = null;
  let intakePosts = 0;
  let releaseIntake!: () => void;
  const intakeGate = new Promise<void>((resolveGate) => {
    releaseIntake = resolveGate;
  });
  const holdFirstIntake = async (route: Route) => {
    if (route.request().method() === "POST") {
      intakePosts += 1;
      if (intakePosts === 1) await intakeGate;
    }
    await route.fallback();
  };
  await page.route("**/api/v1/research-assets/intakes", holdFirstIntake);
  page.on("request", (request) => {
    if (
      request.method() === "POST"
      && request.url().endsWith("/api/v1/research-assets/intakes")
    ) {
      intakeBody = request.postDataJSON() as JsonRecord;
    }
  });

  try {
    await page.getByRole("button", { name: "创建 Quest" }).click();
    const quest = page.getByRole("dialog", {
      name: "创建 Quest，并决定第一个研究问题",
    });
    const materials = quest.locator("[data-journey-section='materials']");
    await materials.getByLabel("研究材料目录", { exact: true }).fill(
      linkedMaterialDirectory,
    );
    await materials.getByRole("button", { name: "使用此目录", exact: true }).click();

    await expect.poll(() => intakePosts).toBe(1);
    await materials.getByLabel("研究材料目录", { exact: true }).fill(
      linkedMaterialDirectory,
    );
    await materials.getByRole("button", { name: "使用此目录", exact: true }).click();
    await expect(materials.locator(".quest-material-status")).toHaveCount(1);
    await expect(materials.getByLabel("研究材料目录", { exact: true })).toHaveValue("");
    expect(intakePosts).toBe(1);
    releaseIntake();

    await expect.poll(() => intakeBody).toMatchObject({
      source_kind: "directory",
      custody_mode: "linked_local",
      source_locator: linkedMaterialDirectory,
      asynchronous: true,
    });
    expect(intakeBody).not.toHaveProperty("content_base64");
    await expect(materials.locator(".quest-material-status.accepted")).toContainText(
      "references",
      { timeout: 15_000 },
    );
  } finally {
    releaseIntake();
    await page.unroute("**/api/v1/research-assets/intakes", holdFirstIntake);
  }
});

test("an over-capacity Quest material selection is rejected as one batch before intake", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  let intakePosts = 0;
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    intakePosts += 1;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ detail: "capacity test must not submit" }),
    });
  });
  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const materials = quest.locator("[data-journey-section='materials']");
  await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles(
    Array.from({ length: 101 }, (_, index) => ({
      name: `capacity-${String(index).padStart(3, "0")}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`capacity ${index}\n`),
    })),
  );

  const alert = quest.getByRole("alert");
  await expect(alert).toContainText("quest_material_limit_exceeded");
  await expect(alert).toContainText("剩余 100 个");
  expect(intakePosts).toBe(0);
  await expect(materials.locator(".quest-material-status")).toHaveCount(0);
  await expect(quest.getByRole("button", { name: "直接根据目标生成" })).toBeEnabled();
});

test("pending uploads reserve all Quest material slots across repeat selection", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);
  await acceptTextAsset(page, "slot-picker.md", "accepted picker slot\n");

  let intakePosts = 0;
  let releaseFirstPost!: () => void;
  const firstPostGate = new Promise<void>((resolve) => {
    releaseFirstPost = resolve;
  });
  const holdFirstPost = async (route: Route) => {
    intakePosts += 1;
    await firstPostGate;
    try {
      await route.abort("failed");
    } catch {
      // The renderer is deliberately gone before the held request is released.
    }
  };
  await page.route("**/api/v1/research-assets/intakes", holdFirstPost);

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const materials = quest.locator("[data-journey-section='materials']");
  const input = materials.getByLabel("上传研究材料文件", { exact: true });
  try {
    await input.setInputFiles(Array.from({ length: 100 }, (_, index) => ({
      name: `reserved-${String(index).padStart(3, "0")}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`reserved ${index}\n`),
    })));
    await expect.poll(() => intakePosts).toBe(1);
    await expect(materials.locator(".quest-material-status")).toHaveCount(100);

    await input.setInputFiles({
      name: "repeat-overflow.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("must be rejected before read or POST\n"),
    });
    const alert = quest.getByRole("alert");
    await expect(alert).toContainText("quest_material_limit_exceeded");
    await expect(alert).toContainText("剩余 0 个");
    expect(intakePosts).toBe(1);
    await expect(materials.locator(".quest-material-status")).toHaveCount(100);

    await expect(quest.getByRole("button", { name: "直接根据目标生成" })).toBeEnabled();
  } finally {
    await page.goto("about:blank");
    releaseFirstPost();
    await page.unroute("**/api/v1/research-assets/intakes", holdFirstPost);
  }
});

test("Quest creation resumes a queued material after its POST ACK and renderer restart", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  await expect(quest).toBeVisible();
  const materials = quest.locator("[data-journey-section='materials']");
  let releaseIntakePoll!: () => void;
  const intakePollGate = new Promise<void>((resolve) => {
    releaseIntakePoll = resolve;
  });
  await page.route("**/api/v1/research-assets/intakes/*", async (route) => {
    await intakePollGate;
    await route.fallback();
  });
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    const response = await route.fetch();
    const result = await response.json() as JsonRecord;
    await route.fulfill({
      response,
      json: { ...result, status: "queued", asset: null },
    });
  });
  await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles({
    name: "quest-source.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("exact Quest source material\n"),
  });

  // Research Memory acceptance is independent background work. The formal
  // Quest form remains usable while that work settles.
  await expect(quest.getByRole("textbox", { name: "目标", exact: true })).toBeEnabled();
  await expect(quest.getByRole("button", { name: "直接根据目标生成" })).toBeEnabled();
  await expect(materials.getByText("quest-source.md", { exact: true })).toBeVisible();
  await expect(materials).toContainText("后台处理中；你可以继续填写");
  await expect.poll(async () => page.evaluate(() => Object.keys(sessionStorage).filter(
    (key) => key.startsWith(
      "meta_research_pending_asset_intake:v2:quest_creation:",
    ),
  ).length)).toBe(1);
  expect(await page.evaluate(() => sessionStorage.getItem(
    "meta_research_pending_asset_intake",
  ))).toBeNull();
  await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(1);

  // The POST was acknowledged with a real queued job. IndexedDB, rather than
  // the tab-scoped locator, must retain enough information to resume polling
  // after the renderer disappears.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
  const resumedQuest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(resumedQuest).toBeVisible();
  await expect(
    resumedQuest.locator("[data-journey-section='materials']")
      .locator(".quest-material-status")
      .getByText("quest-source.md", { exact: true }),
  ).toBeVisible();

  releaseIntakePoll();
  await page.unroute("**/api/v1/research-assets/intakes/*");
  await page.unroute("**/api/v1/research-assets/intakes");
  const acceptedMaterial = resumedQuest.locator("[data-journey-section='materials']")
    .locator(".quest-material-status.accepted");
  await expect(acceptedMaterial.getByText("quest-source.md", { exact: true })).toBeVisible();
  await expect(acceptedMaterial.getByText("已加入本次 Quest", { exact: true })).toBeVisible();
  await resumedQuest.getByLabel("文献搜索范围").selectOption("provided_only");

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

  await expect.poll(async () => page.evaluate(() => Object.keys(sessionStorage).filter(
    (key) => key.startsWith(
      "meta_research_pending_asset_intake:v2:quest_creation:",
    ),
  ).length)).toBe(0);
  await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(0);

  // Binding acknowledgement removes the durable job descriptor. A second
  // renderer restart keeps the accepted binding but must not poll or bind the
  // already-completed job again.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
  const acknowledgedQuest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(acknowledgedQuest).toBeVisible();
  await expect(
    acknowledgedQuest.locator("[data-journey-section='materials']")
      .locator(".quest-material-status.accepted")
      .getByText("quest-source.md", { exact: true }),
  ).toBeVisible();
  await expect(
    acknowledgedQuest.locator("[data-journey-section='materials']")
      .locator(".quest-material-status.accepted")
      .getByText("已加入本次 Quest", { exact: true }),
  ).toBeVisible();
  expect(await scopedAssetIntakeRecoveryCount(page)).toBe(0);
});

test("each selected Quest material reaches a durable POST ACK before earlier polling settles", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  const submittedNames: string[] = [];
  const jobRefs: string[] = [];
  let releasePolls!: () => void;
  const pollGate = new Promise<void>((resolve) => {
    releasePolls = resolve;
  });
  const holdPolls = async (route: Route) => {
    await pollGate;
    await route.fallback();
  };
  const recordQueuedAck = async (route: Route) => {
    const request = route.request().postDataJSON() as { display_name?: string };
    const response = await route.fetch();
    const result = await response.json() as JsonRecord;
    submittedNames.push(String(request.display_name ?? ""));
    jobRefs.push(String(result.job_ref ?? ""));
    await route.fulfill({
      response,
      json: { ...result, status: "queued", asset: null },
    });
  };
  await page.route("**/api/v1/research-assets/intakes/*", holdPolls);
  await page.route("**/api/v1/research-assets/intakes", recordQueuedAck);

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const materials = quest.locator("[data-journey-section='materials']");
  try {
    await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles([
      {
        name: "queued-first.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("first durable queued material\n"),
      },
      {
        name: "queued-second.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("second durable queued material\n"),
      },
    ]);

    await expect.poll(() => submittedNames).toEqual([
      "queued-first.md",
      "queued-second.md",
    ]);
    expect(new Set(jobRefs).size).toBe(2);
    expect(jobRefs.every(Boolean)).toBe(true);
    await expect.poll(async () => page.evaluate(() => {
      const slot = Object.keys(sessionStorage).find((key) => key.startsWith(
        "meta_research_pending_asset_intake:v2:quest_creation:",
      ));
      if (!slot) return [];
      const parsed = JSON.parse(sessionStorage.getItem(slot) ?? "[]") as Array<{
        job_ref?: string;
      }>;
      return parsed.map((pointer) => pointer.job_ref).sort();
    })).toEqual([...jobRefs].sort());
    await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(2);
    await expect(materials).toContainText("queued-first.md");
    await expect(materials).toContainText("queued-second.md");
  } finally {
    releasePolls();
    await page.unroute("**/api/v1/research-assets/intakes/*", holdPolls);
    await page.unroute("**/api/v1/research-assets/intakes", recordQueuedAck);
  }

  await expect(materials.locator(".quest-material-status.accepted")).toHaveCount(2, {
    timeout: 15_000,
  });
  await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(0);
});

test("each recovered Quest material starts settlement before an earlier recovered poll finishes", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  const jobRefs: string[] = [];
  let recoveryPhase = false;
  const recoveryGets = new Map<string, number>();
  let firstPollHeld = false;
  let releaseFirstPoll!: () => void;
  const firstPollGate = new Promise<void>((resolve) => {
    releaseFirstPoll = resolve;
  });
  const queueEveryPostAck = async (route: Route) => {
    const response = await route.fetch();
    const result = await response.json() as JsonRecord;
    jobRefs.push(String(result.job_ref ?? ""));
    await route.fulfill({
      response,
      json: { ...result, status: "queued", asset: null },
    });
  };
  const controlRecoveryPolls = async (route: Route) => {
    const jobRef = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    if (!recoveryPhase) {
      const response = await route.fetch();
      const result = await response.json() as JsonRecord;
      await route.fulfill({
        response,
        json: { ...result, status: "queued", asset: null },
      });
      return;
    }
    const count = (recoveryGets.get(jobRef) ?? 0) + 1;
    recoveryGets.set(jobRef, count);
    if (jobRef === jobRefs[0]) {
      if (count === 1) {
        const response = await route.fetch();
        const result = await response.json() as JsonRecord;
        await route.fulfill({
          response,
          json: { ...result, status: "queued", asset: null },
        });
        return;
      }
      firstPollHeld = true;
      await firstPollGate;
    }
    await route.fallback();
  };
  await page.route("**/api/v1/research-assets/intakes/*", controlRecoveryPolls);
  await page.route("**/api/v1/research-assets/intakes", queueEveryPostAck);

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await quest.locator("[data-journey-section='materials']")
    .getByLabel("上传研究材料文件", { exact: true })
    .setInputFiles([
      {
        name: "recovery-first.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("first recovered material\n"),
      },
      {
        name: "recovery-second.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("second recovered material\n"),
      },
    ]);
  await expect.poll(() => jobRefs.length).toBe(2);
  await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(2);

  const questUrl = page.url();
  await page.evaluate(() => sessionStorage.clear());
  await page.goto("about:blank");
  recoveryPhase = true;
  await page.goto(questUrl, { waitUntil: "domcontentloaded" });
  const resumed = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const resumedMaterials = resumed.locator("[data-journey-section='materials']");
  await expect(resumed).toBeVisible();
  try {
    await expect.poll(() => firstPollHeld).toBe(true);
    await expect.poll(() => recoveryGets.get(jobRefs[1]) ?? 0).toBeGreaterThanOrEqual(1);
    await expect(
      resumedMaterials.locator(".quest-material-status.accepted")
        .getByText("recovery-second.md", { exact: true }),
    ).toBeVisible();
    await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(1);
  } finally {
    releaseFirstPoll();
  }

  await expect(resumedMaterials.locator(".quest-material-status.accepted")).toHaveCount(2, {
    timeout: 15_000,
  });
  await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(0);
  await page.unroute("**/api/v1/research-assets/intakes/*", controlRecoveryPolls);
  await page.unroute("**/api/v1/research-assets/intakes", queueEveryPostAck);
});

test("accepted material commits serialize apply and acknowledgement without losing a concurrent edit", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  const versionByName = new Map<string, string>();
  const nameByJobRef = new Map<string, string>();
  const concurrentGoalWriteRefs: string[][] = [];
  let firstCommitted = false;
  let secondCommitted = false;
  let markFirstCommitted!: () => void;
  const firstCommitReached = new Promise<void>((resolve) => {
    markFirstCommitted = resolve;
  });
  let markSecondCommitted!: () => void;
  const secondCommitReached = new Promise<void>((resolve) => {
    markSecondCommitted = resolve;
  });
  let releaseFirst!: () => void;
  let releaseSecond!: () => void;
  const firstResponseGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  const secondResponseGate = new Promise<void>((resolve) => {
    releaseSecond = resolve;
  });
  const captureAcceptedIntake = async (route: Route) => {
    const request = route.request().postDataJSON() as { display_name?: string };
    const response = await route.fetch();
    const result = await response.json() as {
      asset?: { version_ref?: string } | null;
      [key: string]: unknown;
    };
    const name = String(request.display_name ?? "");
    const jobRef = String(result.job_ref ?? "");
    const versionRef = String(result.asset?.version_ref ?? "");
    if (name && jobRef) nameByJobRef.set(jobRef, name);
    if (name && versionRef) versionByName.set(name, versionRef);
    await route.fulfill({ response, json: result });
  };
  const captureAcceptedPoll = async (route: Route) => {
    const jobRef = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    const response = await route.fetch();
    const result = await response.json() as {
      asset?: { version_ref?: string } | null;
      [key: string]: unknown;
    };
    const name = nameByJobRef.get(jobRef);
    if (name === "commit-race-b.md") await firstCommitReached;
    const versionRef = String(result.asset?.version_ref ?? "");
    if (name && versionRef) versionByName.set(name, versionRef);
    await route.fulfill({ response, json: result });
  };
  const holdAcceptedDraftResponses = async (route: Route) => {
    if (route.request().method() !== "PUT") {
      await route.fallback();
      return;
    }
    const body = route.request().postDataJSON() as {
      draft?: {
        goal?: string;
        literature?: {
          accepted_material_bindings?: Array<{ version_ref?: string }>;
        };
      };
    };
    const refs = new Set(
      body.draft?.literature?.accepted_material_bindings
        ?.map((binding) => binding.version_ref)
        .filter((value): value is string => Boolean(value)) ?? [],
    );
    if (body.draft?.goal === "A/B accepted binding 与本地目标必须一起 durable") {
      concurrentGoalWriteRefs.push([...refs].sort());
    }
    const firstRef = versionByName.get("commit-race-a.md");
    const secondRef = versionByName.get("commit-race-b.md");
    if (!firstCommitted && firstRef && refs.has(firstRef) && !refs.has(secondRef ?? "")) {
      const response = await route.fetch();
      if (!response.ok()) {
        await route.fulfill({ response });
        return;
      }
      firstCommitted = true;
      markFirstCommitted();
      await firstResponseGate;
      await route.fulfill({ response });
      return;
    }
    if (!secondCommitted && secondRef && refs.has(secondRef)) {
      const response = await route.fetch();
      if (response.ok()) {
        secondCommitted = true;
        markSecondCommitted();
        await secondResponseGate;
      }
      await route.fulfill({ response });
      return;
    }
    await route.fallback();
  };
  await page.route("**/api/v1/research-assets/intakes", captureAcceptedIntake);
  await page.route("**/api/v1/research-assets/intakes/*", captureAcceptedPoll);
  await page.route(
    "**/api/v1/quest-initializations/*/draft",
    holdAcceptedDraftResponses,
  );

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const materials = quest.locator("[data-journey-section='materials']");
  await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles([
    {
      name: "commit-race-a.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("accepted race material A\n"),
    },
    {
      name: "commit-race-b.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("accepted race material B\n"),
    },
  ]);

  try {
    await expect.poll(() => firstCommitted).toBe(true);
    await expect.poll(() => versionByName.size).toBe(2);
    await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(2);

    const secondCommittedWhileFirstResponseHeld = await Promise.race([
      secondCommitReached.then(() => true),
      new Promise<false>((resolveDelay) => {
        setTimeout(() => resolveDelay(false), 750);
      }),
    ]);
    expect(secondCommittedWhileFirstResponseHeld).toBe(false);
    await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(2);

    const concurrentGoal = "A/B accepted binding 与本地目标必须一起 durable";
    await quest.getByRole("textbox", { name: "目标", exact: true }).fill(concurrentGoal);
    releaseFirst();
    await expect.poll(() => secondCommitted).toBe(true);
    releaseSecond();

    await expect.poll(async () => {
      const response = await page.request.get(
        `${product?.baseUrl}/api/v1/quest-initializations/current`,
      );
      const current = await response.json() as {
        quest_draft: {
          value: {
            goal: string;
            literature: {
              accepted_material_bindings: Array<{ version_ref?: string }>;
            };
          };
        };
      };
      return {
        goal: current.quest_draft.value.goal,
        refs: current.quest_draft.value.literature.accepted_material_bindings
          .map((binding) => binding.version_ref)
          .sort(),
      };
    }, { timeout: 15_000 }).toEqual({
      goal: concurrentGoal,
      refs: [...versionByName.values()].sort(),
    });
    await expect.poll(() => concurrentGoalWriteRefs.at(-1) ?? [])
      .toEqual([...versionByName.values()].sort());
    expect(concurrentGoalWriteRefs).not.toContainEqual([
      versionByName.get("commit-race-b.md"),
    ]);
    await expect.poll(() => scopedAssetIntakeRecoveryCount(page)).toBe(0);
  } finally {
    releaseFirst();
    releaseSecond();
    await page.unroute("**/api/v1/research-assets/intakes", captureAcceptedIntake);
    await page.unroute("**/api/v1/research-assets/intakes/*", captureAcceptedPoll);
    await page.unroute(
      "**/api/v1/quest-initializations/*/draft",
      holdAcceptedDraftResponses,
    );
  }
});

test("a slow optional material bind never gates the formal Direct Quest flow", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  let bindingHeld = false;
  let releaseBinding!: () => void;
  const bindingGate = new Promise<void>((resolve) => {
    releaseBinding = resolve;
  });
  const holdAcceptedBinding = async (route: Route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as {
        draft?: {
          literature?: { accepted_material_bindings?: Array<Record<string, unknown>> };
        };
      };
      if (
        !bindingHeld &&
        (body.draft?.literature?.accepted_material_bindings?.length ?? 0) > 0
      ) {
        bindingHeld = true;
        await bindingGate;
      }
    }
    await route.fallback();
  };
  await page.route(
    "**/api/v1/quest-initializations/*/draft",
    holdAcceptedBinding,
  );

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  const materials = quest.locator("[data-journey-section='materials']");
  for (const viewport of [
    { width: 800, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    await expect(materials.getByLabel("研究材料目录", { exact: true })).toBeVisible();
    await expect(materials.getByRole("button", { name: "使用此目录", exact: true }))
      .toBeDisabled();
    await expect(materials.getByRole("button", { name: "选择文件", exact: true }))
      .toHaveCount(0);
    await expect(materials.getByRole("button", { name: "选择文件夹", exact: true }))
      .toHaveCount(0);
    expect(await page.evaluate(() => (
      document.documentElement.scrollWidth <= document.documentElement.clientWidth
    ))).toBe(true);
  }
  await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles({
    name: "slow-optional-binding.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("accepted material whose Quest binding is deliberately slow\n"),
  });
  await expect.poll(() => bindingHeld).toBe(true);
  await expect(materials).toContainText("已接纳，正在加入当前草案");

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

  releaseBinding();
  await page.unroute(
    "**/api/v1/quest-initializations/*/draft",
    holdAcceptedBinding,
  );
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
  }, { timeout: 12_000 }).toEqual({ goal, boundary, bindingCount: 1 });
});

test("a committed scoped Quest intake replays the exact POST after its ACK is lost", async ({
  page,
}) => {
  if (!product) throw new Error("deterministic product missing");
  await openAuthenticatedProduct(page, product);

  const requestBodies: string[] = [];
  const idempotencyKeys: string[] = [];
  const jobRefs: string[] = [];
  let committedWithoutAck = false;
  let releaseReplayAck!: () => void;
  const replayAckGate = new Promise<void>((resolve) => {
    releaseReplayAck = resolve;
  });
  await page.route("**/api/v1/research-assets/intakes", async (route) => {
    if (route.request().method() !== "POST") {
      await route.fallback();
      return;
    }
    requestBodies.push(route.request().postData() ?? "");
    idempotencyKeys.push(route.request().headers()["idempotency-key"] ?? "");
    const response = await route.fetch();
    const result = await response.json() as JsonRecord;
    jobRefs.push(String(result.job_ref ?? ""));
    if (!committedWithoutAck) {
      committedWithoutAck = true;
      await route.abort("connectionreset");
      return;
    }
    await replayAckGate;
    await route.fulfill({ response, json: result });
  });

  await page.getByRole("button", { name: "创建 Quest" }).click();
  const quest = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题" });
  const materials = quest.locator("[data-journey-section='materials']");
  const exactBytes = "scoped ACK-loss bytes must be replayed exactly\n";
  await materials.getByLabel("上传研究材料文件", { exact: true }).setInputFiles({
    name: "quest-ack-loss.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(exactBytes),
  });

  await expect.poll(() => committedWithoutAck).toBe(true);
  await expect.poll(async () => page.evaluate(() => {
    const slot = Object.keys(sessionStorage).find((key) => key.startsWith(
      "meta_research_pending_asset_intake:v2:quest_creation:",
    ));
    return slot ? sessionStorage.getItem(slot) : null;
  })).not.toBeNull();
  expect(await page.evaluate(() => Object.values(sessionStorage).join("\n")))
    .not.toContain(Buffer.from(exactBytes).toString("base64"));

  // IndexedDB is the durable source of truth. Losing the tab-scoped locator
  // must not orphan an exact operation after a renderer/browser restart.
  await page.evaluate(() => sessionStorage.clear());
  await page.reload({ waitUntil: "domcontentloaded" });
  const reopened = page.getByRole("dialog", {
    name: "创建 Quest，并决定第一个研究问题",
  });
  await expect(reopened).toBeVisible();
  const concurrentGoal = "ACK 丢失恢复时继续编辑的研究目标";
  const concurrentBoundary = "用户文本与 accepted material 必须一起 durable";
  await reopened.getByRole("textbox", { name: "目标", exact: true }).fill(concurrentGoal);
  await reopened.getByRole("textbox", { name: "边界", exact: true }).fill(concurrentBoundary);
  releaseReplayAck();
  const acceptedMaterial = reopened.locator("[data-journey-section='materials']")
    .locator(".quest-material-status.accepted");
  await expect(acceptedMaterial.getByText("quest-ack-loss.md", { exact: true })).toBeVisible();
  await expect(acceptedMaterial.getByText("已加入本次 Quest", { exact: true })).toBeVisible();
  await expect(reopened.getByRole("alert").filter({ hasText: "quest_draft_stale" }))
    .toHaveCount(0);

  await expect.poll(() => idempotencyKeys.length).toBe(2);
  expect(idempotencyKeys[0]).not.toBe("");
  expect(idempotencyKeys[1]).toBe(idempotencyKeys[0]);
  expect(requestBodies[1]).toBe(requestBodies[0]);
  expect(jobRefs[0]).not.toBe("");
  expect(jobRefs[1]).toBe(jobRefs[0]);

  await expect.poll(async () => {
    const response = await page.request.get(
      `${product?.baseUrl}/api/v1/research-assets?offset=0&limit=50`,
    );
    const inventory = await response.json() as { items: Array<{ display_name: string }> };
    return inventory.items.filter((item) => item.display_name === "quest-ack-loss.md").length;
  }).toBe(1);
  await expect.poll(async () => {
    const response = await page.request.get(
      `${product?.baseUrl}/api/v1/quest-initializations/current`,
    );
    const current = await response.json() as {
      quest_draft: {
        value: {
          goal: string;
          completion_criteria: string;
          literature: { accepted_material_bindings: Array<{ version_ref?: string }> };
        };
      };
    };
    return {
      goal: current.quest_draft.value.goal,
      completionCriteria: current.quest_draft.value.completion_criteria,
      bindingCount: current.quest_draft.value.literature.accepted_material_bindings.length,
    };
  }).toEqual({
    goal: concurrentGoal,
    completionCriteria: concurrentBoundary,
    bindingCount: 1,
  });
  await expect.poll(async () => page.evaluate(() => Object.keys(sessionStorage).filter(
    (key) => key.startsWith("meta_research_pending_asset_intake:v2:quest_creation:"),
  ).length)).toBe(0);
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

async function scopedAssetIntakeRecoveryCount(page: Page): Promise<number> {
  return page.evaluate(() => new Promise<number>((resolve, reject) => {
    const openRequest = indexedDB.open("meta_research_human_request_recovery");
    openRequest.onerror = () => reject(openRequest.error);
    openRequest.onsuccess = () => {
      const database = openRequest.result;
      const transaction = database.transaction("recovery_manifests", "readonly");
      const keysRequest = transaction.objectStore("recovery_manifests").getAllKeys();
      let count = 0;
      keysRequest.onsuccess = () => {
        count = keysRequest.result.filter((key) =>
          typeof key === "string" && key.startsWith(
            "meta-research/human-request-recovery/v2:scoped-asset-intake:",
          )
        ).length;
      };
      transaction.oncomplete = () => {
        database.close();
        resolve(count);
      };
      transaction.onerror = () => {
        database.close();
        reject(transaction.error);
      };
      transaction.onabort = () => {
        database.close();
        reject(transaction.error);
      };
    };
  }));
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
