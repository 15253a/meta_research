import { defineConfig } from "@playwright/test";

const manualCreationFixedRaster = /@manual-creation-fixed-raster/;

export default defineConfig({
  testDir: "./tests",
  testIgnore: "**/response-time/**",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: "line",
  projects: [
    {
      name: "functional",
      grepInvert: [manualCreationFixedRaster],
    },
    {
      name: "manual-creation-fixed-raster",
      grep: manualCreationFixedRaster,
    },
  ],
  snapshotPathTemplate:
    "{snapshotDir}/{testFileDir}/{testFileName}-snapshots/{arg}{-snapshotSuffix}{ext}",
  use: {
    headless: true,
    channel: process.env.META_RESEARCH_CHROME ? undefined : "chrome",
    launchOptions: {
      executablePath: process.env.META_RESEARCH_CHROME,
    },
    trace: "retain-on-failure",
  },
});
