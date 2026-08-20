import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: "line",
  use: {
    headless: true,
    launchOptions: {
      executablePath: process.env.META_RESEARCH_CHROME ?? "/usr/bin/google-chrome",
    },
    trace: "retain-on-failure",
  },
});
