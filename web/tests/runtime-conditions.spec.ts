import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

const devices = [
  { uuid: "GPU-test-1", name: "NVIDIA A100-SXM4-80GB", memory_total_mib: 81920 },
  { uuid: "GPU-test-2", name: "NVIDIA A100-SXM4-80GB", memory_total_mib: 81920 },
  { uuid: "GPU-test-3", name: "NVIDIA A100-SXM4-80GB", memory_total_mib: 81920 },
];
const initialConditions = {
  time_budget: "30d", literature: { mode: "oa_only", scope_exclusions: "", retained: "custom literature value" },
  selected_devices: devices.slice(0, 2), selected_device_uuids: devices.slice(0, 2).map(device => device.uuid),
  key_configuration: { unchanged: "value with a } brace and a \"quote\"" },
  custom_condition: ["keep", { nested: true }],
};
const prefix = "按以下条件安排研究与委派；时间预算不会随新一轮重置。\n";
const suffix = "\n另请保留这条手写约束。";
const structuredText = (data: unknown = initialConditions) => prefix + JSON.stringify(data, null, 2) + suffix;
const savedJson = (text: string) => JSON.parse(text.slice(prefix.length, -suffix.length));

async function workspace(page: Page, text = "GPU：GPU-test-1，80 GiB\n时间预算：30 天") {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const questRef: string = snapshot.research_space.current_quest.quest_ref;
  const state = {
    snapshot, questRef, reads: [] as string[], writes: [] as { questRef: string; text: string; expected_revision: string }[],
    configs: { [questRef]: { quest_ref: questRef, text, revision: "r1" } },
    catalogFailure: false, catalogQuest: questRef, catalogReads: 0,
    failure: 0, delayedQuest: "", release: null as (() => void) | null,
  };
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    const conditions = /^\/api\/v1\/quests\/([^/]+)\/runtime-conditions$/.exec(url.pathname);
    if (conditions) {
      const selected = decodeURIComponent(conditions[1]);
      if (request.method() === "GET") {
        state.reads.push(selected);
        const value = { ...state.configs[selected] };
        if (selected === state.delayedQuest) await new Promise<void>(done => { state.release = done; });
        return json(value).catch(() => {});
      }
      expect(request.method()).toBe("PUT");
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      const body = request.postDataJSON();
      state.writes.push({ questRef: selected, ...body });
      if (state.failure) return json({ detail: { code: state.failure === 409 ? "runtime_conditions_stale" : "temporarily_unavailable" } }, state.failure);
      expect(body.expected_revision).toBe(state.configs[selected].revision);
      const next = { quest_ref: selected, text: body.text, revision: `r${state.writes.length + 1}` };
      state.configs[selected] = next;
      return json(next);
    }
    if (request.method() !== "GET") return route.abort();
    if (/^\/api\/v1\/questions\/[^/]+\/history$/.test(url.pathname)) return json({
      status: "ready", question_ref: state.snapshot.research_space.current_question.question_ref,
      question: { question_ref: state.snapshot.research_space.current_question.question_ref, quest_ref: state.catalogQuest, initialization_id: "init-runtime-conditions" },
    });
    if (url.pathname === "/api/v1/quest-initializations/init-runtime-conditions") {
      state.catalogReads += 1;
      if (state.catalogFailure) return json({ detail: { code: "unavailable" } }, 503);
      return json({ initialization_id: "init-runtime-conditions", quest_ref: state.catalogQuest,
        compute: { status: "ready", snapshot_ref: "compute-initial", devices }, resource_envelope: { host_snapshot_ref: "compute-initial", selected_device_uuids: devices.map(device => device.uuid), devices } });
    }
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/snapshot") return json(state.snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?workspace=1", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("button", { name: "运行条件", exact: true })).toBeEnabled();
  return state;
}

const dialog = (page: Page) => page.getByRole("dialog", { name: "运行条件", exact: true });
const editor = (page: Page) => dialog(page).getByRole("textbox", { name: "运行条件内容" });

test("opening reads the current configuration and cancelling never writes", async ({ page }) => {
  const state = await workspace(page);
  const opener = page.getByRole("button", { name: "运行条件", exact: true });
  expect(state.reads).toHaveLength(0);
  await opener.click();
  await expect(editor(page)).toHaveValue(state.configs[state.questRef].text);
  await expect(editor(page)).toHaveAttribute("maxlength", "24000");
  await editor(page).fill("  \n ");
  await expect(dialog(page).getByRole("button", { name: "保存运行条件" })).toBeDisabled();
  await editor(page).fill("本次临时编辑，不保存");
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await expect(dialog(page)).toHaveCount(0);
  await expect(opener).toBeFocused();
  expect(state.writes).toHaveLength(0);
  await opener.click();
  await expect(editor(page)).toHaveValue(state.configs[state.questRef].text);
  expect(state.reads).toHaveLength(2);
});

test("saving uses the displayed revision and keeps the server receipt for the next edit", async ({ page }) => {
  const state = await workspace(page);
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(editor(page)).toBeEnabled();
  await editor(page).fill("GPU：2 张获准 GPU\n时间预算：7 天\n只开展轻量验证。");
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toHaveText("已保存，将用于后续新调用。");
  expect(state.writes[0]).toMatchObject({ questRef: state.questRef, expected_revision: "r1" });
  await editor(page).fill("GPU：1 张获准 GPU\n时间预算：7 天");
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toHaveText("已保存，将用于后续新调用。");
  expect(state.writes[1].expected_revision).toBe("r2");
  await dialog(page).getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(editor(page)).toHaveValue(state.configs[state.questRef].text);
});

test("save failures retain edits and a conflict requires a fresh read", async ({ page }) => {
  const state = await workspace(page);
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(editor(page)).toBeEnabled();
  await editor(page).fill("失败时仍要保留的运行条件");
  state.failure = 503;
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("alert")).toContainText("当前编辑已保留");
  await expect(editor(page)).toHaveValue("失败时仍要保留的运行条件");
  state.failure = 409;
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("alert")).toContainText("关闭后重新打开");
  await expect(editor(page)).toHaveValue("失败时仍要保留的运行条件");
  await expect(dialog(page).getByRole("button", { name: "保存运行条件" })).toBeDisabled();
  state.configs[state.questRef] = { quest_ref: state.questRef, text: "另一处已更新的运行条件", revision: "r5" };
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(editor(page)).toHaveValue("另一处已更新的运行条件");
});

test("Quest changes close the editor and a late old response cannot replace the new configuration", async ({ page }) => {
  const state = await workspace(page);
  state.delayedQuest = state.questRef;
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect.poll(() => state.release !== null).toBe(true);
  const nextQuest = "quest-next";
  state.configs[nextQuest] = { quest_ref: nextQuest, text: "下一项研究：CPU，90 天", revision: "r-next" };
  state.snapshot.research_space.current_quest.quest_ref = nextQuest;
  state.snapshot.research_space.current_question.quest_ref = nextQuest;
  state.snapshot.research_control.quest_ref = nextQuest;
  state.snapshot.research_control.foreground.quest_ref = nextQuest;
  state.snapshot.revision += 1;
  await expect(dialog(page)).toHaveCount(0);
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(editor(page)).toHaveValue(state.configs[nextQuest].text);
  state.release?.();
  await expect(editor(page)).toHaveValue(state.configs[nextQuest].text);
  await editor(page).fill("下一项研究的新条件");
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toContainText("已保存");
  expect(state.writes).toEqual([{ questRef: nextQuest, text: "下一项研究的新条件", expected_revision: "r-next" }]);
});

test("the control stays available without a foreground and fits desktop and mobile", async ({ page }) => {
  const state = await workspace(page, structuredText());
  state.snapshot.research_control.foreground = null;
  state.snapshot.revision += 1;
  await expect(page.locator(".lumen-connection code")).toHaveText(`状态 ${state.snapshot.revision}`);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.getByRole("button", { name: "运行条件", exact: true }).click();
    await expect(dialog(page).getByRole("combobox", { name: "时间预算", exact: true })).toHaveValue("30d");
    await expect(dialog(page).getByRole("checkbox")).toHaveCount(3);
    await expect(editor(page)).not.toBeVisible();
    const bounds = await dialog(page).boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    await dialog(page).screenshot({ path: `test-results/runtime-conditions-${width}.png` });
    await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  }
});

test("common choices and advanced JSON stay in sync without losing custom conditions", async ({ page }) => {
  const state = await workspace(page, structuredText());
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  const budget = dialog(page).getByRole("combobox", { name: "时间预算", exact: true });
  const literature = dialog(page).getByRole("combobox", { name: "文献搜索范围", exact: true });
  await expect(budget).toHaveValue("30d");
  await expect(literature).toHaveValue("oa_only");
  await expect(editor(page)).not.toBeVisible();
  await budget.selectOption({ label: "7 天" });
  await literature.selectOption({ label: "全面搜索（包括图书馆）" });
  await dialog(page).getByRole("textbox", { name: "文献排除范围", exact: true }).fill("不纳入动物实验");
  await dialog(page).locator("summary").filter({ hasText: "高级" }).click();
  const first = await editor(page).inputValue();
  expect(first.startsWith(prefix)).toBe(true);
  expect(first.endsWith(suffix)).toBe(true);
  expect(savedJson(first)).toEqual({ ...initialConditions, time_budget: "7d",
    literature: { ...initialConditions.literature, mode: "oa_then_institution", scope_exclusions: "不纳入动物实验" } });
  await editor(page).fill(structuredText({ ...savedJson(first), time_budget: "90d" }));
  await expect(budget).toHaveValue("90d");
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toContainText("已保存");
  expect(state.writes).toHaveLength(1);
  expect(savedJson(state.writes[0].text).time_budget).toBe("90d");
  expect(state.writes[0].expected_revision).toBe("r1");
});

test("deselected GPUs can be selected again after saving and reopening", async ({ page }) => {
  const state = await workspace(page, structuredText());
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(3);
  await dialog(page).getByRole("checkbox").nth(0).uncheck();
  await dialog(page).getByRole("checkbox").nth(1).uncheck();
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toContainText("已保存");
  expect(savedJson(state.writes[0].text).selected_devices).toEqual([]);
  expect(savedJson(state.writes[0].text).selected_device_uuids).toEqual([]);
  await dialog(page).getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(3);
  await expect(dialog(page).getByRole("checkbox").nth(0)).not.toBeChecked();
  await dialog(page).getByRole("checkbox").nth(0).check();
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toContainText("已保存");
  const selected = savedJson(state.writes[1].text);
  expect(selected.selected_devices).toEqual([devices[0]]);
  expect(selected.selected_device_uuids).toEqual([devices[0].uuid]);
  expect(selected.gpu_configuration ?? "").not.toContain("未配置已选 GPU");
  expect(state.catalogReads).toBe(2);
});

test("invalid JSON stays editable and unsupported values are not silently normalized", async ({ page }) => {
  const state = await workspace(page, structuredText({ ...initialConditions, time_budget: "custom-budget",
    literature: { ...initialConditions.literature, mode: "custom-scope" } }));
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  const budget = dialog(page).getByRole("combobox", { name: "时间预算", exact: true });
  await expect(budget).toHaveValue("custom-budget");
  await expect(dialog(page).getByRole("combobox", { name: "文献搜索范围", exact: true })).toHaveValue("custom-scope");
  await dialog(page).locator("summary").filter({ hasText: "高级" }).click();
  const broken = prefix + '{"time_budget": "7d",';
  await editor(page).fill(broken);
  await expect(editor(page)).toHaveValue(broken);
  await expect(budget).toBeDisabled();
  expect(state.writes).toHaveLength(0);
  await editor(page).fill(structuredText());
  await expect(budget).toBeEnabled();
  await expect(budget).toHaveValue("30d");
});

test("catalog failures or wrong Quest metadata do not overwrite the current configuration", async ({ page }) => {
  const state = await workspace(page, structuredText());
  state.catalogFailure = true;
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(dialog(page).getByRole("combobox", { name: "时间预算", exact: true })).toHaveValue("30d");
  await expect(dialog(page).getByText(/完整设备清单读取失败/)).toBeVisible();
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(2);
  await dialog(page).getByRole("button", { name: "取消", exact: true }).click();
  state.catalogFailure = false;
  state.catalogQuest = "another-quest";
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(dialog(page).getByText(/完整设备清单读取失败/)).toBeVisible();
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(2);
  await expect(dialog(page).getByRole("button", { name: "保存运行条件" })).toBeDisabled();
  expect(state.writes).toHaveLength(0);
  expect(state.catalogReads).toBe(1);
});

test("advanced GPU edits cannot revive removed metadata or silently widen inconsistent selections", async ({ page }) => {
  const state = await workspace(page, structuredText({ ...initialConditions,
    selected_devices: [{ ...devices[0], custom_device_note: "remove this" }, devices[1]] }));
  await page.getByRole("button", { name: "运行条件", exact: true }).click();
  await expect(dialog(page).getByRole("checkbox")).toHaveCount(3);
  await dialog(page).locator("summary").filter({ hasText: "高级" }).click();
  await editor(page).fill(structuredText({ ...initialConditions, selected_device_uuids: [] }));
  await expect(dialog(page).getByRole("checkbox").nth(0)).toBeDisabled();
  await expect(editor(page)).toHaveValue(structuredText({ ...initialConditions, selected_device_uuids: [] }));
  await editor(page).fill(structuredText());
  await dialog(page).getByRole("checkbox").nth(2).check();
  await dialog(page).getByRole("button", { name: "保存运行条件" }).click();
  await expect(dialog(page).getByRole("status")).toContainText("已保存");
  expect(savedJson(state.writes[0].text).selected_devices).toEqual(devices);
});
