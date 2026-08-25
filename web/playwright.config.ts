import { defineConfig } from "@playwright/test";

const manualCreationFixedRaster = /@manual-creation-fixed-raster/;
const writingFixedRaster = /@writing-fixed-raster/;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: "line",
  projects: [
    {
      name: "functional",
      grepInvert: [manualCreationFixedRaster, writingFixedRaster],
    },
    {
      name: "manual-creation-fixed-raster",
      grep: manualCreationFixedRaster,
    },
    {
      name: "writing-fixed-raster",
      grep: writingFixedRaster,
    },
  ],
  snapshotPathTemplate:
    "{snapshotDir}/{testFileDir}/{testFileName}-snapshots/{arg}{-snapshotSuffix}{ext}",
  use: {
    headless: true,
    launchOptions: {
      executablePath: process.env.META_RESEARCH_CHROME ?? "/usr/bin/google-chrome",
    },
    trace: "retain-on-failure",
  },
});
