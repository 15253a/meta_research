import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { HostResourceSample } from "../src/HostResources";

const sample = (): HostResourceSample => ({
  schema_ref: "meta-research/runtime-resources/v1", scope: "execution_host", host: { hostname: "compute-test" },
  observed_at: 1791458400, sampled_from: 1791458399.9, sampled_to: 1791458400, refresh_interval_seconds: 5,
  cpu: { status: "ready", utilization_percent: 0, source: "linux_proc_stat", reason_code: null, sampled_from: 1791458399.8, sampled_to: 1791458399.9 },
  memory: { status: "ready", used_bytes: 8589934592, total_bytes: 17179869184, source: "linux_proc_meminfo", reason_code: null, observed_at: 1791458399.9 },
  gpu: { status: "ready", source: "nvidia_smi", reason_code: null, observed_at: 1791458400, devices: [
    { uuid: "GPU-A", name: "Card A", utilization_percent: 0, memory_used_mib: 0, memory_total_mib: 24576 },
    { uuid: "GPU-B", name: "Card B", utilization_percent: 73, memory_used_mib: 8192, memory_total_mib: 24576 },
  ] },
});

test("research view uses the CPU interval separately from the GPU query and stays usable at 390px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await openResources(page, true);
  state.sample.sampled_from = 1791458399;
  await page.getByRole("button", { name: "刷新资源", exact: true }).click();
  const panel = page.getByRole("region", { name: "执行机器整机资源" });
  await expect(panel).toContainText("100 ms");
  await expect(panel).toContainText(/CPU 采样区间 \d{2}:\d{2}:\d{2}\.800 至 \d{2}:\d{2}:\d{2}\.900/);
  await expect(panel).toContainText("GPU 采样时间");
  expect(await panel.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await panel.evaluate(element => {
    const bounds = element.getBoundingClientRect();
    return element.contains(document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + 260));
  })).toBe(true);
  await page.keyboard.press("Escape");
  await expect(panel).toHaveCount(0);
  await expect(page.getByRole("button", { name: "执行机器资源", exact: true })).toBeFocused();
  expect(state.writes).toEqual([]);
});

async function openResources(page: Page, workspace = false) {
  const state = { sample: sample(), fail: false, writes: [] as string[] };
  const snapshot = JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8"));
  snapshot.human_collaboration.human_requests.items = [];
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== "GET") { state.writes.push(request.method() + " " + url.pathname); return route.abort(); }
    if (url.origin !== "http://host-resources.test") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/runtime/resources") return state.fail ? json({ detail: { code: "offline" } }, 503) : json(state.sample);
    if (url.pathname === "/api/v1/status") return json({ schema_ref: "meta-research/runtime-status/v1", revision: 1, observed_at: "2026-10-08T11:20:00Z", updated_at: "2026-10-08T11:20:00Z", state: "idle", current_task: null, waiting_reason: null, pending_requests: 0, foreground: null, health: { status: "ready", checks: [] } });
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto(`http://host-resources.test/${workspace ? "?workspace=1" : ""}`);
  await page.getByRole("button", { name: "执行机器资源", exact: true }).click();
  return state;
}

test("home shows whole-host CPU, memory and each GPU with sample time and true zero", async ({ page }) => {
  const state = await openResources(page);
  const panel = page.getByRole("region", { name: "执行机器整机资源" });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("整机口径");
  await expect(panel).toContainText("compute-test");
  await expect(panel.getByTestId("host-cpu")).toContainText("0%");
  await expect(panel.getByTestId("host-memory")).toContainText("8 / 16 GiB");
  await expect(panel.locator('[data-gpu-uuid="GPU-A"]')).toContainText("0%");
  await expect(panel.locator('[data-gpu-uuid="GPU-B"]')).toContainText("73%");
  await expect(panel.locator("time").first()).toHaveAttribute("dateTime", "2026-10-08T11:20:00.000Z");
  expect(state.writes).toEqual([]);
});

test("resources distinguish missing values, no devices and disconnects, and recover after GPU changes", async ({ page }) => {
  const state = await openResources(page);
  const panel = page.getByRole("region", { name: "执行机器整机资源" });
  const refresh = page.getByRole("button", { name: "刷新资源", exact: true });
  await expect(panel.locator('[data-gpu-uuid="GPU-B"]')).toContainText("73%");
  state.sample.cpu = { ...state.sample.cpu, status: "unavailable", utilization_percent: null, reason_code: "cpu_read_failed" };
  state.sample.memory = { ...state.sample.memory, status: "unavailable", used_bytes: null, total_bytes: null, reason_code: "memory_read_failed" };
  state.sample.gpu.devices = [{ uuid: "GPU-B", name: "Card B", utilization_percent: null, memory_used_mib: null, memory_total_mib: 24576 }];
  await refresh.click();
  await expect(panel.getByTestId("host-cpu")).toContainText("缺测");
  await expect(panel.getByTestId("host-memory")).toContainText("缺测");
  await expect(panel.locator('[data-gpu-uuid="GPU-A"]')).toHaveCount(0);
  await expect(panel.locator('[data-gpu-uuid="GPU-B"]')).toContainText("缺测");
  state.sample.gpu = { ...state.sample.gpu, status: "no_devices", devices: [] };
  await refresh.click();
  await expect(panel).toContainText("未检测到 GPU 设备");
  state.sample.gpu = { ...state.sample.gpu, status: "unavailable", reason_code: "gpu_probe_failed" };
  await refresh.click();
  await expect(panel).toContainText("GPU 采集不可用");
  await expect(panel).not.toContainText("未检测到 GPU 设备");
  state.fail = true;
  await refresh.click();
  await expect(panel).toContainText("保留上次成功采样，当前状态未知");
  state.fail = false;
  state.sample = sample();
  state.sample.gpu.devices = [{ uuid: "GPU-C", name: "New card", utilization_percent: 0, memory_used_mib: 0, memory_total_mib: 16384 }];
  await refresh.click();
  await expect(panel).not.toContainText("当前状态未知");
  await expect(panel.locator('[data-gpu-uuid="GPU-C"]')).toContainText("0 / 16384 MiB");
  await expect(panel.locator('[data-gpu-uuid="GPU-B"]')).toHaveCount(0);
  expect(state.writes).toEqual([]);
});
