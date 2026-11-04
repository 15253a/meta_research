import { expect, test, type Page, type Route } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";

type AccountView = {
  status: "authenticated" | "unauthenticated" | "unavailable";
  account: null | { type: "chatgpt" | "apiKey"; email: string | null; plan_type: string | null };
  login: null | { id: string; status: "pending" | "completed" | "failed" | "cancelled"; verification_url: string | null; user_code: string | null; error_code: string | null; expires_at: number | null };
  error_code: string | null;
};
const signedIn = (): AccountView => ({ status: "authenticated", account: { type: "chatgpt", email: "researcher@example.com", plan_type: "plus" }, login: null, error_code: null });
const dialog = (page: Page) => page.getByRole("dialog", { name: "Codex 账户", exact: true });
const entry = (page: Page) => page.getByRole("button", { name: /^Codex ·/ });

async function workspace(page: Page, view = signedIn()) {
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  snapshot.research_space.current_quest = { ...snapshot.research_space.current_quest, status: "not_bound", quest_ref: null, guidance_alignment: [] };
  snapshot.research_space.current_question = { ...snapshot.research_space.current_question, status: "not_bound", quest_ref: null, question_ref: null };
  snapshot.research_space.status = "empty"; snapshot.research_control.foreground = null; snapshot.research_control.quest_ref = null;
  const state = { view, reads: 0, writes: [] as { path: string; body: unknown }[], readFailure: false,
    intercept: null as null | ((route: Route) => Promise<boolean>) };
  await page.context().addCookies([{ name: "meta_research_csrf", value: "test-csrf", domain: "127.0.0.1", path: "/" }]);
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== "http://127.0.0.1:18768") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname.startsWith("/api/v1/codex/account")) {
      if (request.method() === "GET") {
        state.reads++;
        if (state.intercept && await state.intercept(route)) return;
        return state.readFailure ? json({ detail: { code: "account_unavailable" } }, 503) : json(state.view);
      }
      expect(request.headers()["x-csrf-token"]).toBe("test-csrf");
      expect(request.headers()["origin"]).toBe("http://127.0.0.1:18768");
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      state.writes.push({ path: url.pathname, body: request.postDataJSON() });
      if (state.intercept && await state.intercept(route)) return;
      return json(state.view);
    }
    if (request.method() !== "GET") return route.abort();
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://127.0.0.1:18768/?workspace=1", { waitUntil: "domcontentloaded" });
  return state;
}

test("current Codex account is visible before a Quest and opens an accessible account panel", async ({ page }) => {
  await workspace(page);
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  await entry(page).click();
  await expect(dialog(page).getByText("researcher@example.com", { exact: true })).toBeVisible();
  await expect(dialog(page).getByText("ChatGPT", { exact: true })).toBeVisible();
  await expect(dialog(page).getByText("Plus", { exact: true })).toBeVisible();
  await expect(dialog(page).getByRole("button", { name: "切换账户", exact: true })).toBeEnabled();
  await page.keyboard.press("Escape");
  await expect(dialog(page)).not.toBeVisible();
  await expect(entry(page)).toBeFocused();
});

test("switching keeps the current account while device login survives closing and reopening", async ({ page }) => {
  const state = await workspace(page);
  await entry(page).click();
  state.intercept = async route => {
    if (new URL(route.request().url()).pathname.endsWith("/login") && route.request().method() === "POST") {
      state.view = { ...signedIn(), login: { id: "login-one", status: "pending", verification_url: "https://auth.openai.com/codex/device", user_code: "ABCD-EFGH", error_code: null, expires_at: Date.now() / 1000 + 600 } };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(state.view) });
      return true;
    }
    return false;
  };
  await dialog(page).getByRole("button", { name: "切换账户", exact: true }).click();
  await expect(dialog(page).getByText("ABCD-EFGH", { exact: true })).toBeVisible();
  await expect(dialog(page).getByRole("link", { name: "打开登录页面", exact: true })).toHaveAttribute("href", "https://auth.openai.com/codex/device");
  await expect(dialog(page).getByText("researcher@example.com", { exact: true })).toBeVisible();
  await expect(dialog(page).getByRole("button", { name: "切换账户", exact: true })).toBeDisabled();
  await page.keyboard.press("Escape");
  await entry(page).click();
  await expect(dialog(page).getByText("ABCD-EFGH", { exact: true })).toBeVisible();
  expect(state.writes).toEqual([{ path: "/api/v1/codex/account/login", body: {} }]);
  state.view = { ...signedIn(), account: { type: "chatgpt", email: "new@example.com", plan_type: "pro" }, login: { ...state.view.login!, status: "completed", user_code: null, verification_url: null } };
  await expect(dialog(page).getByText("new@example.com", { exact: true })).toBeVisible();
  await expect(entry(page)).toHaveText("Codex · new@example.com");
});

test("cancel preserves the old account and logout requires a separate confirmation", async ({ page }) => {
  const state = await workspace(page, { ...signedIn(), login: { id: "login-cancel", status: "pending", verification_url: "https://auth.openai.com/codex/device", user_code: "CANCEL-ME", error_code: null, expires_at: Date.now() / 1000 + 600 } });
  state.intercept = async route => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() !== "POST") return false;
    if (path.endsWith("/login/cancel")) state.view = { ...signedIn(), login: { ...state.view.login!, status: "cancelled", user_code: null, verification_url: null } };
    else if (path.endsWith("/logout")) state.view = { status: "unauthenticated", account: null, login: null, error_code: null };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(state.view) });
    return true;
  };
  await entry(page).click();
  await dialog(page).getByRole("button", { name: "取消登录", exact: true }).click();
  await expect(dialog(page).getByText("已取消本次登录。", { exact: true })).toBeVisible();
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  expect(state.writes[0]).toEqual({ path: "/api/v1/codex/account/login/cancel", body: { login_id: "login-cancel" } });
  await dialog(page).getByRole("button", { name: "退出账户", exact: true }).click();
  expect(state.writes).toHaveLength(1);
  await dialog(page).getByRole("button", { name: "保留当前账户", exact: true }).click();
  await dialog(page).getByRole("button", { name: "退出账户", exact: true }).click();
  await dialog(page).getByRole("button", { name: "确认退出", exact: true }).click();
  await expect(entry(page)).toHaveText("Codex · 未登录");
  await expect(dialog(page).getByRole("button", { name: "登录 Codex", exact: true })).toBeEnabled();
  expect(state.writes[1]).toEqual({ path: "/api/v1/codex/account/logout", body: {} });
});

test("failed login can retry without losing the current account or exposing unapproved fields", async ({ page }) => {
  const state = await workspace(page);
  let attempts = 0;
  state.intercept = async route => {
    if (route.request().method() !== "POST") return false;
    attempts++;
    state.view = { ...signedIn(), login: { id: `try-${attempts}`, status: attempts === 1 ? "failed" : "pending", verification_url: attempts === 1 ? null : "https://untrusted.example/phish", user_code: attempts === 1 ? null : "SAFE-CODE", error_code: attempts === 1 ? "login_expired" : null, expires_at: null } };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify({ ...state.view, refresh_token: "do-not-display-secret", account: { ...state.view.account, access_token: "also-private" } }) });
    return true;
  };
  await entry(page).click();
  await dialog(page).getByRole("button", { name: "切换账户", exact: true }).click();
  await expect(dialog(page).getByRole("alert")).toContainText("原账户仍保留");
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  await dialog(page).getByRole("button", { name: "切换账户", exact: true }).click();
  await expect(dialog(page).getByText("SAFE-CODE", { exact: true })).toBeVisible();
  await expect(dialog(page).getByRole("link", { name: "打开登录页面" })).toHaveCount(0);
  await expect(dialog(page).getByRole("alert")).toContainText("可信的官方登录地址");
  await expect(page.locator("body")).not.toContainText("do-not-display-secret");
  await expect(page.locator("body")).not.toContainText("also-private");
});

test("a delayed account read cannot overwrite a completed switch and repeated clicks submit once", async ({ page }) => {
  const state = await workspace(page);
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  let releaseRead!: () => void, releaseWrite!: () => void;
  const readGate = new Promise<void>(resolve => { releaseRead = resolve; });
  const writeGate = new Promise<void>(resolve => { releaseWrite = resolve; });
  let held = false;
  state.intercept = async route => {
    if (route.request().method() === "GET" && !held) {
      held = true; await readGate;
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(signedIn()) });
      return true;
    }
    if (route.request().method() === "POST") {
      await writeGate;
      state.view = { ...signedIn(), account: { type: "chatgpt", email: "switched@example.com", plan_type: "pro" } };
      await route.fulfill({ contentType: "application/json", body: JSON.stringify(state.view) });
      return true;
    }
    return false;
  };
  try {
    await entry(page).click();
    await expect.poll(() => held).toBe(true);
    await dialog(page).getByRole("button", { name: "切换账户", exact: true }).click();
    await expect(dialog(page).getByRole("button", { name: "切换账户", exact: true })).toBeDisabled();
    expect(state.writes).toHaveLength(1);
    releaseWrite();
    await expect(entry(page)).toHaveText("Codex · switched@example.com");
    releaseRead();
    await expect(dialog(page).getByText("switched@example.com", { exact: true })).toBeVisible();
    await dialog(page).getByRole("button", { name: "刷新账户", exact: true }).click();
    await expect(entry(page)).toHaveText("Codex · switched@example.com");
  } finally { releaseRead(); releaseWrite(); }
});

test("unknown account state offers refresh and login without presenting old data as current", async ({ page }) => {
  const state = await workspace(page);
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  state.readFailure = true;
  await entry(page).click();
  await expect(entry(page)).toHaveText("Codex · 状态未知");
  await expect(dialog(page).getByRole("alert")).toContainText("上方保留上次读取的账户");
  state.readFailure = false;
  state.view = { status: "unauthenticated", account: null, login: null, error_code: null };
  await dialog(page).getByRole("button", { name: "刷新账户", exact: true }).click();
  await expect(entry(page)).toHaveText("Codex · 未登录");
  await expect(dialog(page).getByRole("button", { name: "登录 Codex", exact: true })).toBeEnabled();
});

test("a rejected logout returned as HTTP 200 explains the failure and preserves the current account", async ({ page }) => {
  const state = await workspace(page);
  state.intercept = async route => {
    if (route.request().method() !== "POST") return false;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...signedIn(), error_code: "codex_account_logout_failed" }) });
    return true;
  };
  await entry(page).click();
  await dialog(page).getByRole("button", { name: "退出账户", exact: true }).click();
  await dialog(page).getByRole("button", { name: "确认退出", exact: true }).click();
  await expect(dialog(page).getByRole("alert")).toHaveText("操作未完成，请刷新账户状态后重试。");
  await expect(entry(page)).toHaveText("Codex · researcher@example.com");
  await expect(dialog(page).getByRole("button", { name: "退出账户", exact: true })).toBeEnabled();
  await expect(page.locator("body")).not.toContainText("codex_account_logout_failed");
  expect(state.writes).toEqual([{ path: "/api/v1/codex/account/logout", body: {} }]);
});

test("long account identities stay reachable on desktop and mobile", async ({ page }) => {
  const email = "researcher-with-a-long-account-name@example-research-institution.com";
  await workspace(page, { ...signedIn(), account: { type: "chatgpt", email, plan_type: "plus" }, login: { id: "visual-login", status: "pending", verification_url: "https://auth.openai.com/codex/device", user_code: "ABCD-EFGH", error_code: null, expires_at: Date.now() / 1000 + 600 } });
  const screenshots = resolve(import.meta.dirname, "../../.scratch/codex-account-20261104");
  await mkdir(screenshots, { recursive: true });
  for (const width of [1440, 800, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(entry(page)).toBeVisible();
    const trigger = await entry(page).boundingBox();
    expect(trigger!.x).toBeGreaterThanOrEqual(0);
    expect(trigger!.x + trigger!.width).toBeLessThanOrEqual(width);
    expect(await page.locator(".lumen-header").evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: resolve(screenshots, `account-header-${width}.png`) });
    await entry(page).click();
    await expect(dialog(page).getByText(email, { exact: true })).toBeVisible();
    expect(await dialog(page).evaluate(element => element.scrollWidth - element.clientWidth)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: resolve(screenshots, `account-panel-${width}.png`) });
    await page.keyboard.press("Escape");
  }
});
