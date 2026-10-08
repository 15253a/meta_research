import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { WorkMaterialReceipt, WorkMaterialReference } from "../src/workMaterialApi";

const receiptPath = process.env.META_RESEARCH_MATERIAL_BROWSER_RECEIPT;
test.skip(!receiptPath, "Requires an immutable isolated Linux product fixture and SSH loopback receipt.");
test.setTimeout(180_000);

type Fixture = {
  quest_ref: string; question_ref: string; request_ref: string;
  source_directory: string; small_file: string; large_file: string; missing_path: string; unreadable_path: string;
  actual_source_host: { hostname: string; platform: string; permission_context: string };
};

async function openPicker(page: Page, consumer: Locator) {
  await consumer.getByRole("button", { name: "选择服务器文件或目录", exact: true }).click();
  const picker = page.getByRole("dialog", { name: "选择服务器上的原始材料", exact: true });
  await expect(picker).toBeVisible();
  await expect(picker.getByLabel("服务器绝对路径")).toBeFocused();
  return picker;
}

async function choose(page: Page, consumer: Locator, path: string, description: string) {
  const picker = await openPicker(page, consumer);
  await expect(picker.getByLabel("服务器绝对路径")).toBeEnabled();
  await picker.getByLabel("服务器绝对路径").fill(path);
  await picker.getByLabel("原始材料说明").fill(description);
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker).toBeHidden();
  await expect(consumer.locator(".server-material-picker__candidate").first()).toContainText(path);
}

async function rejectOneStaleSource(page: Page, pattern: string, requestMaterial = false) {
  let first = true;
  const stale = async (route: Route) => {
    if (!first) { await route.fallback(); return; }
    first = false;
    const body = route.request().postDataJSON();
    const selection = requestMaterial ? body.materials[0].selection : body.selections[0];
    const changed = { ...selection, observation: { ...selection.observation, size: "0" } };
    if (requestMaterial) body.materials[0].selection = changed;
    else body.selections[0] = changed;
    const actual = await route.fetch({ postData: JSON.stringify(body) });
    expect(actual.status()).toBe(409);
    expect(await actual.json()).toMatchObject({ detail: { code: "material_source_changed" } });
    await route.fulfill({ response: actual });
  };
  await page.route(pattern, stale);
}

function assertSaved(receipt: WorkMaterialReceipt, expectedPath: string, kind: string) {
  expect(receipt.receiver.kind).toBe(kind);
  expect(receipt.references).toHaveLength(1);
  const reference = receipt.references[0];
  expect(reference.source.absolute_path).toBe(expectedPath);
  expect(reference.availability).toBe("available");
  expect(reference.read_state).toBe("not_read");
  expect(reference.read_ranges).toEqual([]);
  expect(reference.failures).toEqual([]);
  for (const key of ["device", "inode", "size", "modified_ns", "changed_ns"] as const) {
    expect(typeof reference.source.observation[key]).toBe("string");
  }
}

test("real server selection and explicit receipts cover every material consumer", async ({ page }, testInfo) => {
  if (!receiptPath) return;
  const directory = dirname(receiptPath);
  const handshake = JSON.parse(readFileSync(join(directory, "handshake.json"), "utf8")) as { base_url: string; bootstrap_token: string };
  const fixture = JSON.parse(readFileSync(join(directory, "fixture.json"), "utf8")) as Fixture;
  const base = handshake.base_url;
  const authenticated = await page.request.post(`${base}/auth/bootstrap`, { headers: { Origin: base }, data: { token: handshake.bootstrap_token } });
  expect(authenticated.ok()).toBeTruthy();
  const posts: Array<{ path: string; key: string | undefined; body: unknown }> = [];
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path.startsWith("/api/v1/")) {
      posts.push({ path, key: request.headers()["idempotency-key"], body: request.postDataJSON() });
    }
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`${base}/?workspace=1`);
  const requestClose = page.getByRole("button", { name: "关闭需要你处理的事项", exact: true });
  if (await requestClose.isVisible()) await requestClose.click();

  const guidance = page.getByRole("form", { name: "提交人类指导", exact: true });
  await expect(guidance).toBeVisible();
  if (await requestClose.isVisible()) await requestClose.click();
  await guidance.getByLabel("指导原文", { exact: true }).fill("Guidance keeps its exact text and strength.");
  await guidance.getByLabel("指导力度", { exact: true }).selectOption("4");
  await choose(page, guidance, fixture.small_file, "Exact guidance source description.");
  const guidanceResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/v1/human-collaboration/guidance"));
  await guidance.getByRole("button", { name: "保存指导", exact: true }).click();
  const savedGuidance = await (await guidanceResponse).json() as { work_materials: WorkMaterialReceipt[]; guidance: { text: string; strength: number } };
  assertSaved(savedGuidance.work_materials[0], fixture.small_file, "current");
  expect(savedGuidance.guidance.strength).toBe(4);
  expect(savedGuidance.guidance.text).toBe("Guidance keeps its exact text and strength.");

  await page.goto(`${base}/?panel=create-quest&workspace=1`);
  const creation = page.getByRole("dialog", { name: "创建 Quest，并决定第一个研究问题", exact: true });
  await expect(creation).toBeVisible();
  const creationMaterials = creation.locator("[data-journey-section='materials']");
  const picker = await openPicker(page, creationMaterials);
  await expect(picker.getByLabel("服务器绝对路径")).toBeEnabled();
  await picker.getByLabel("服务器绝对路径").fill(fixture.source_directory);
  await picker.getByLabel("服务器绝对路径").press("Enter");
  await expect(picker.getByText(fixture.actual_source_host.hostname, { exact: true })).toBeVisible();
  await expect(picker.getByText(fixture.actual_source_host.platform, { exact: true })).toBeVisible();
  await expect(picker.getByText(fixture.actual_source_host.permission_context, { exact: true })).toBeVisible();
  await expect(picker.locator(".server-material-dialog__listing li")).toHaveCount(50);
  await picker.getByRole("button", { name: "加载更多条目", exact: true }).click();
  await expect(picker.locator(".server-material-dialog__listing li")).toHaveCount(61);
  await expect(picker.getByRole("button", { name: "加载更多条目", exact: true })).toHaveCount(0);
  await picker.getByRole("button", { name: "打开目录 nested", exact: true }).click();
  await expect(picker.getByText("detail.txt", { exact: true })).toBeVisible();
  await picker.getByRole("button", { name: "上级目录", exact: true }).click();
  await expect(picker.getByLabel("服务器绝对路径")).toBeEnabled();
  await picker.getByLabel("服务器绝对路径").fill(fixture.missing_path);
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker.getByRole("alert")).toContainText("找不到此路径");
  await picker.getByLabel("服务器绝对路径").fill(`${fixture.source_directory}/unsafe-link`);
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker.getByRole("alert")).toContainText("符号链接");
  await picker.getByLabel("服务器绝对路径").fill(fixture.unreadable_path);
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker.getByRole("alert")).toContainText("没有读取此路径的权限");
  await picker.getByLabel("服务器绝对路径").fill(fixture.large_file);
  await picker.getByLabel("原始材料说明").fill("Two GiB original. Metadata only.");
  await picker.getByRole("button", { name: "选择此路径", exact: true }).click();
  await expect(picker).toBeHidden();
  expect(posts.some(post => post.path.endsWith("/material-references") || post.path === "/api/v1/research-assets/intakes")).toBe(false);

  await page.setViewportSize({ width: 390, height: 844 });
  const narrow = await openPicker(page, creationMaterials);
  await expect(narrow.getByLabel("服务器绝对路径")).toBeEnabled();
  const dimensions = await narrow.evaluate(element => ({ width: element.getBoundingClientRect().width, scroll: element.scrollWidth, client: element.clientWidth }));
  expect(dimensions.width).toBeLessThanOrEqual(390);
  expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.client + 1);
  await narrow.getByLabel("服务器绝对路径").fill(fixture.source_directory);
  await narrow.getByRole("button", { name: "打开目录", exact: true }).click();
  await expect(narrow.getByRole("button", { name: "加载更多条目", exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("picker-narrow.png"), fullPage: true });
  const cancellation = page.waitForResponse(response => response.request().method() === "DELETE" && response.url().includes("/server-materials/cursors/"));
  await narrow.getByLabel("服务器绝对路径").press("Escape");
  await expect(narrow).toBeHidden();
  expect((await cancellation).ok()).toBeTruthy();
  await expect(creationMaterials.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeFocused();
  await expect(creationMaterials.locator(".server-material-picker__candidate").first()).toContainText(fixture.large_file);
  await page.setViewportSize({ width: 1440, height: 900 });

  const register = creationMaterials.getByRole("button", { name: "保存材料引用到创建草稿", exact: true });
  await expect(register).toBeEnabled();
  let committedBeforeLostAck: WorkMaterialReceipt | null = null;
  let loseFirstAck = true;
  await page.route("**/api/v1/quest-initializations/*/material-references", async route => {
    if (!loseFirstAck) { await route.continue(); return; }
    loseFirstAck = false;
    const accepted = await route.fetch();
    expect(accepted.status()).toBe(201);
    committedBeforeLostAck = await accepted.json() as WorkMaterialReceipt;
    await route.abort("connectionreset");
  });
  await register.click();
  await expect(creationMaterials.getByText("待重试的封存提交", { exact: true })).toBeVisible();
  await expect(creationMaterials.getByRole("alert")).toBeVisible();
  const held = await page.evaluate(() => Object.entries(localStorage).find(([key]) => key.startsWith("meta_research_pending_work_material:v1:") && key.includes(":creation:")));
  expect(held).toBeTruthy();
  await page.reload();
  await expect(creationMaterials.getByText("待重试的封存提交", { exact: true })).toBeVisible();
  await expect(creationMaterials.getByText("重试保留原始正文、材料及接收位置。", { exact: true })).toBeVisible();
  const registerResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/material-references"));
  await creationMaterials.getByRole("button", { name: "重试封存提交", exact: true }).click();
  const creationReceipt = await (await registerResponse).json() as WorkMaterialReceipt;
  assertSaved(creationReceipt, fixture.large_file, "creation");
  expect(creationReceipt.references[0].source.observation.size).toBe("2147483648");
  expect(creationReceipt).toEqual(committedBeforeLostAck);
  const creationView = await page.request.get(`${base}/api/v1/quest-initializations/${encodeURIComponent(String(creationReceipt.receiver.initialization_id))}`);
  const durableCreation = await creationView.json() as { work_materials: WorkMaterialReceipt[] };
  expect(durableCreation.work_materials).toHaveLength(1);
  expect(durableCreation.work_materials[0].references[0].reference_ref).toBe(creationReceipt.references[0].reference_ref);
  const attempts = posts.filter(post => post.path.endsWith("/material-references"));
  expect(attempts.length).toBeGreaterThanOrEqual(2);
  expect(attempts.at(-1)?.body).toEqual(attempts.at(-2)?.body);
  expect(attempts.at(-1)?.key).toBe(attempts.at(-2)?.key);
  if (!held) throw new Error("sealed metadata record missing");
  await page.evaluate(key => localStorage.setItem(key, "{broken"), held[0]);
  await page.reload();
  await expect(creationMaterials.getByRole("alert")).toContainText("无法校验");
  expect(await page.evaluate(key => localStorage.getItem(key), held[0])).toBe("{broken");
  await creationMaterials.getByRole("button", { name: "明确放弃损坏的本地封存记录", exact: true }).click();
  await expect(creationMaterials.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeEnabled();
  await expect(creationMaterials.getByText("已保存，尚未读取", { exact: true }).first()).toBeVisible();

  await choose(page, creationMaterials, fixture.small_file, "Rejected stale expected observation.");
  await rejectOneStaleSource(page, "**/api/v1/quest-initializations/*/material-references");
  await register.click();
  const discardRejected = creationMaterials.getByRole("button", { name: "放弃未接受的命令，重新选择", exact: true });
  await expect(discardRejected).toBeEnabled();
  const rejectedCreationView = await page.request.get(`${base}/api/v1/quest-initializations/${encodeURIComponent(String(creationReceipt.receiver.initialization_id))}`);
  expect((await rejectedCreationView.json()).work_materials).toHaveLength(1);
  await expect(creationMaterials.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeDisabled();
  await discardRejected.click();
  await choose(page, creationMaterials, fixture.small_file, "Corrected original source observation.");
  const correctedCreationResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/material-references"));
  await register.click();
  assertSaved(await (await correctedCreationResponse).json() as WorkMaterialReceipt, fixture.small_file, "creation");
  const correctedAttempts = posts.filter(post => post.path.endsWith("/material-references"));
  expect(correctedAttempts.at(-1)?.key).not.toBe(correctedAttempts.at(-2)?.key);

  await page.goto(`${base}/?variant=A&view=questions&node=${encodeURIComponent(fixture.question_ref)}&panel=create-question`);
  const manual = page.getByRole("dialog", { name: "创建后续研究问题", exact: true });
  await expect(manual).toBeVisible();
  await choose(page, manual.locator(".manual-material-card"), fixture.source_directory, "Manual pre-Seed directory reference.");
  const manualResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().includes("/manual-question-creations/") && response.url().endsWith("/material-references"));
  await manual.getByRole("button", { name: "保存材料引用到创建上下文", exact: true }).click();
  const manualReceipt = await (await manualResponse).json() as WorkMaterialReceipt;
  assertSaved(manualReceipt, fixture.source_directory, "manual");
  const manualView = await page.request.get(`${base}/api/v1/manual-question-creations/${encodeURIComponent(String(manualReceipt.receiver.context_ref))}`);
  expect((await manualView.json()).seed).toBeNull();
  expect(posts.some(post => post.path === "/api/v1/research-assets/intakes")).toBe(false);

  await page.goto(`${base}/?panel=research-assets&workspace=1`);
  const library = page.getByRole("region", { name: "六大研究入口", exact: true });
  await library.getByRole("tab", { name: "人类输入", exact: true }).click();
  const input = library.locator(".library-guidance");
  await input.getByLabel("研究指导", { exact: true }).fill("Original proactive text.");
  await choose(page, input, fixture.small_file, "Proactive original description.");
  const inputResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/v1/research-inputs"));
  await input.getByRole("button", { name: "保存指导", exact: true }).click();
  const inputReceipt = await (await inputResponse).json() as { input_ref: string; work_materials: WorkMaterialReceipt[] };
  assertSaved(inputReceipt.work_materials[0], fixture.small_file, "current");
  await page.reload();
  await library.getByRole("tab", { name: "人类输入", exact: true }).click();
  await library.getByText("已保存的工作材料引用", { exact: true }).first().click();
  await expect(library.getByText("Proactive original description.", { exact: true }).first()).toBeVisible();

  await page.goto(`${base}/?panel=human-request&workspace=1`);
  const human = page.getByRole("dialog").filter({ has: page.getByLabel("自然语言回应", { exact: true }) });
  await expect(human).toBeVisible();
  await human.getByLabel("自然语言回应", { exact: true }).fill("Exact original-root response.");
  await choose(page, human, fixture.small_file, "HumanRequest exact original description.");
  await rejectOneStaleSource(page, "**/api/v1/human-requests/*/responses", true);
  await human.getByRole("button", { name: "提交", exact: true }).click();
  await expect(human).toContainText("material_source_changed");
  await expect(human.getByRole("button", { name: "选择服务器文件或目录", exact: true })).toBeEnabled();
  await choose(page, human, fixture.small_file, "HumanRequest corrected original description.");
  const humanResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().includes("/human-requests/") && response.url().endsWith("/responses"));
  await human.getByRole("button", { name: "提交", exact: true }).click();
  const humanReceipt = await (await humanResponse).json() as { delivery: { work_materials: Array<{ reference_ref: string }> } };
  expect(humanReceipt.delivery.work_materials).toHaveLength(1);
  const hrReferenceResponse = await page.request.get(`${base}/api/v1/work-materials/${encodeURIComponent(humanReceipt.delivery.work_materials[0].reference_ref)}`);
  const hrReference = await hrReferenceResponse.json() as WorkMaterialReference;
  expect(hrReference.receiver.kind).toBe("request");
  expect(hrReference.source.absolute_path).toBe(fixture.small_file);
  expect(hrReference.read_state).toBe("not_read");
  expect(hrReference.source.description).toBe("HumanRequest corrected original description.");
  const humanAttempts = posts.filter(post => post.path.includes("/human-requests/") && post.path.endsWith("/responses"));
  expect(humanAttempts.at(-1)?.key).not.toBe(humanAttempts.at(-2)?.key);

  await page.goto(`${base}/?panel=research-assets&workspace=1`);
  await page.locator(".asset-storage-details > summary").click();
  const assets = page.locator(".asset-intake");
  await expect(assets).toBeVisible();
  const formalInspection = page.waitForResponse(response => response.request().method() === "GET" && response.url().includes("/server-materials/inspect?"));
  await choose(page, assets, fixture.small_file, "Formal asset source candidate.");
  const formalSelection = await (await formalInspection).json();
  expect(posts.some(post => post.path === "/api/v1/research-assets/intakes")).toBe(false);
  await expect(assets.getByLabel("Research Asset 保管模式", { exact: true })).toHaveValue("linked_local");
  await expect(assets.getByLabel("Research Asset 来源位置", { exact: true })).toHaveValue(fixture.small_file);
  const intakeResponse = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/api/v1/research-assets/intakes"));
  await assets.getByRole("button", { name: "提交 Asset Intake", exact: true }).click();
  expect((await intakeResponse).ok()).toBeTruthy();
  const formalPosts = posts.filter(post => post.path === "/api/v1/research-assets/intakes");
  expect(formalPosts).toHaveLength(1);
  expect((formalPosts[0].body as { provenance: { server_material_selection: unknown } }).provenance.server_material_selection).toEqual(formalSelection);
  expect(formalPosts[0].body).toMatchObject({
    source_kind: "local_path", custody_mode: "linked_local", source_locator: fixture.small_file,
    provenance: { server_material_selection: { absolute_path: fixture.small_file, description: "Formal asset source candidate.", availability: "available", kind: "file" } },
  });
  expect(errors).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("all-material-consumers-desktop.png"), fullPage: true });
});
