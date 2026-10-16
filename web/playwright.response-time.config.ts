import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/response-time",
  testMatch: "*.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 240_000,
  globalTimeout: 600_000,
  expect: { timeout: 10_000 },
  outputDir: "response-time-results/traces",
  reporter: [
    ["line"],
    ["html", { outputFolder: "response-time-results/html", open: "never" }],
    ["json", { outputFile: "response-time-results/playwright.json" }],
  ],
  use: {
    browserName: "chromium",
    headless: true,
    serviceWorkers: "block",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
