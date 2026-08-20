import type { Page, TestInfo } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";


export type FixedReferenceSurface = "shell" | "create-quest";

type FixedReferenceEntry = {
  surface: FixedReferenceSurface;
  viewport: { width: number; height: number };
  sha256: string;
  expectedDiffPixelRatio: number;
  maxDiffPixelRatioDelta: number;
  expectedDiffGrid: number[];
  maxGridCellDelta: number;
  reviewedProductionSha256: string;
  maxProductionDiffPixels: number;
};

type FixedReferenceManifest = {
  source: {
    repository: string;
    commit: string;
    path: string;
    blob_sha1: string;
    html_sha256: string;
  };
  references: Record<string, FixedReferenceEntry>;
};

const FIXED_SOURCE = {
  repository: "15253a/meta_research",
  commit: "d7e2c9b79792d82285881f39c3fb2de2dd260d5f",
  path: "meta-research/views/core-ui-prototype/index.html",
  blob_sha1: "ffd45bcd233ba5bed30cb47cbf82598dab2ce349",
  html_sha256: "36e48dc7e93f469a7ef7dcea0b163589655623aad66913c88061a6dda6e9d998",
} as const;

const supportDirectory = dirname(fileURLToPath(import.meta.url));
const referenceDirectory = resolve(
  supportDirectory,
  "../fixed-references/d7e2c9b7",
);
const manifestPath = resolve(referenceDirectory, "manifest.json");
const manifestBytes = readFileSync(manifestPath);
const manifest = JSON.parse(manifestBytes.toString("utf8")) as FixedReferenceManifest;
const MAX_REVIEWED_DIFF_DELTA = 0.003;
const DIFF_GRID_CELL_COUNT = 16;
const MAX_REVIEWED_GRID_CELL_DELTA = 0.006;
const MAX_REVIEWED_PRODUCTION_DIFF_PIXELS = 400;
const FIXED_DYNAMIC_MASK_ALLOWLIST: Record<
  FixedReferenceSurface,
  Array<{ selector: string; reason: string }>
> = {
  shell: [
    { selector: ".lumen-connection code", reason: "durable feed revision" },
  ],
  "create-quest": [
    { selector: ".lumen-connection code", reason: "durable feed revision" },
    {
      selector: ".quest-intent-header > div > span",
      reason: "durable Intent Session ref",
    },
    {
      selector: ".quest-intent-status",
      reason: "session status and bound draft revision",
    },
  ],
};
const FIXED_MASK_RGBA = [32, 42, 58, 255] as const;

function assertFixedManifestSource(): void {
  for (const [field, expected] of Object.entries(FIXED_SOURCE)) {
    const actual = manifest.source[field as keyof typeof FIXED_SOURCE];
    if (actual !== expected) {
      throw new Error(
        `fixed d7e2c9b7 manifest source ${field} changed: ${String(actual)}`,
      );
    }
  }
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function pngDimensions(bytes: Buffer): { width: number; height: number } {
  if (
    bytes.length < 24 ||
    bytes.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a"
  ) {
    throw new Error("fixed visual reference is not a PNG");
  }
  return {
    width: bytes.readUInt32BE(16),
    height: bytes.readUInt32BE(20),
  };
}

export function assertReviewedProductionBaseline(
  actualBytes: Buffer,
  reviewedBytes: Buffer,
  maxDiffPixels: number,
  label: string,
): { diffPixelCount: number; diffBytes: Buffer } {
  if (
    !Number.isInteger(maxDiffPixels) ||
    maxDiffPixels < 0 ||
    maxDiffPixels > MAX_REVIEWED_PRODUCTION_DIFF_PIXELS
  ) {
    throw new Error(`${label} production baseline envelope is not narrowly reviewed`);
  }
  const actual = PNG.sync.read(actualBytes);
  const reviewed = PNG.sync.read(reviewedBytes);
  if (actual.width !== reviewed.width || actual.height !== reviewed.height) {
    throw new Error(`${label} reviewed production baseline dimensions changed`);
  }
  const diff = new PNG({ width: actual.width, height: actual.height });
  const diffPixelCount = pixelmatch(
    reviewed.data,
    actual.data,
    diff.data,
    actual.width,
    actual.height,
    { diffMask: true, includeAA: false, threshold: 0 },
  );
  if (diffPixelCount > maxDiffPixels) {
    throw new Error(
      `${label} reviewed production baseline drifted: ` +
      `${diffPixelCount} pixels changed (maximum ${maxDiffPixels})`,
    );
  }
  return { diffPixelCount, diffBytes: PNG.sync.write(diff) };
}

function diffGridRatios(
  diff: PNG,
  columns = 4,
  rows = 4,
): number[] {
  const ratios: number[] = [];
  for (let row = 0; row < rows; row += 1) {
    const top = Math.floor((row * diff.height) / rows);
    const bottom = Math.floor(((row + 1) * diff.height) / rows);
    for (let column = 0; column < columns; column += 1) {
      const left = Math.floor((column * diff.width) / columns);
      const right = Math.floor(((column + 1) * diff.width) / columns);
      let different = 0;
      for (let y = top; y < bottom; y += 1) {
        for (let x = left; x < right; x += 1) {
          if (diff.data[(y * diff.width + x) * 4 + 3] !== 0) different += 1;
        }
      }
      ratios.push(different / ((right - left) * (bottom - top)));
    }
  }
  return ratios;
}

function assertRatioArray(values: number[], label: string): void {
  if (
    values.length !== DIFF_GRID_CELL_COUNT ||
    values.some((value) => !Number.isFinite(value) || value < 0 || value > 1)
  ) {
    throw new Error(
      `${label} must contain ${DIFF_GRID_CELL_COUNT} finite ratios in [0, 1]`,
    );
  }
}

export function assertDiffGridSignature(
  actual: number[],
  expected: number[],
  maxCellDelta: number,
  label: string,
): number[] {
  assertRatioArray(actual, `${label} actual diff grid`);
  assertRatioArray(expected, `${label} reviewed diff grid`);
  if (
    !Number.isFinite(maxCellDelta) ||
    maxCellDelta < 0 ||
    maxCellDelta > MAX_REVIEWED_GRID_CELL_DELTA
  ) {
    throw new Error(`${label} grid envelope is not narrowly reviewed`);
  }

  const deltas = actual.map((value, index) => Math.abs(value - expected[index]));
  const driftedCell = deltas.findIndex((delta) => delta > maxCellDelta);
  if (driftedCell !== -1) {
    throw new Error(
      `${label} grid cell ${driftedCell} drifted: actual ` +
      `${(actual[driftedCell] * 100).toFixed(3)}%, reviewed ` +
      `${(expected[driftedCell] * 100).toFixed(3)}% ± ` +
      `${(maxCellDelta * 100).toFixed(3)}%`,
    );
  }
  return deltas;
}

async function captureMaskedProductionScreenshot(
  page: Page,
  surface: FixedReferenceSurface,
): Promise<{ bytes: Buffer; masks: Array<{ selector: string; reason: string }> }> {
  const masks = FIXED_DYNAMIC_MASK_ALLOWLIST[surface];
  const boxes: Array<{ x: number; y: number; width: number; height: number }> = [];
  for (const { selector } of masks) {
    const locator = page.locator(selector);
    const count = await locator.count();
    if (count === 0) {
      throw new Error(`fixed visual dynamic mask selector is missing: ${selector}`);
    }
    for (let index = 0; index < count; index += 1) {
      const box = await locator.nth(index).boundingBox();
      if (box) boxes.push(box);
    }
  }

  const raw = await page.screenshot({ animations: "disabled" });
  const masked = PNG.sync.read(raw);
  for (const box of boxes) {
    const left = Math.max(0, Math.floor(box.x));
    const top = Math.max(0, Math.floor(box.y));
    const right = Math.min(masked.width, Math.ceil(box.x + box.width));
    const bottom = Math.min(masked.height, Math.ceil(box.y + box.height));
    for (let y = top; y < bottom; y += 1) {
      for (let x = left; x < right; x += 1) {
        const offset = (y * masked.width + x) * 4;
        masked.data.set(FIXED_MASK_RGBA, offset);
      }
    }
  }
  return { bytes: PNG.sync.write(masked), masks };
}

export async function attachFixedVisualPair(
  page: Page,
  testInfo: TestInfo,
  surface: FixedReferenceSurface,
  viewport: { width: number; height: number },
): Promise<void> {
  assertFixedManifestSource();
  const filename = `${surface}-${viewport.width}.png`;
  const entry = manifest.references[filename];
  if (!entry) throw new Error(`fixed visual reference is not allowlisted: ${filename}`);
  if (
    entry.surface !== surface ||
    entry.viewport.width !== viewport.width ||
    entry.viewport.height !== viewport.height
  ) {
    throw new Error(`fixed visual reference viewport mismatch: ${filename}`);
  }
  if (
    !Number.isFinite(entry.expectedDiffPixelRatio) ||
    entry.expectedDiffPixelRatio < 0 ||
    entry.expectedDiffPixelRatio > 1 ||
    !Number.isFinite(entry.maxDiffPixelRatioDelta) ||
    entry.maxDiffPixelRatioDelta < 0 ||
    entry.maxDiffPixelRatioDelta > MAX_REVIEWED_DIFF_DELTA ||
    !Array.isArray(entry.expectedDiffGrid) ||
    !Number.isFinite(entry.maxGridCellDelta) ||
    entry.maxGridCellDelta < 0 ||
    entry.maxGridCellDelta > MAX_REVIEWED_GRID_CELL_DELTA ||
    !/^[0-9a-f]{64}$/.test(entry.reviewedProductionSha256) ||
    !Number.isInteger(entry.maxProductionDiffPixels) ||
    entry.maxProductionDiffPixels < 0 ||
    entry.maxProductionDiffPixels > MAX_REVIEWED_PRODUCTION_DIFF_PIXELS
  ) {
    throw new Error(
      `fixed visual reference comparison envelope is not narrowly reviewed: ${filename}`,
    );
  }
  assertRatioArray(
    entry.expectedDiffGrid,
    `fixed visual reference ${filename} reviewed diff grid`,
  );

  const referenceBytes = readFileSync(resolve(referenceDirectory, filename));
  const actualHash = sha256(referenceBytes);
  if (actualHash !== entry.sha256) {
    throw new Error(
      `fixed visual reference hash mismatch for ${filename}: ${actualHash}`,
    );
  }
  const dimensions = pngDimensions(referenceBytes);
  if (
    dimensions.width !== viewport.width ||
    dimensions.height !== viewport.height
  ) {
    throw new Error(
      `fixed visual reference dimensions mismatch for ${filename}: ` +
      `${dimensions.width}x${dimensions.height}`,
    );
  }

  const manifestAttachment = "fixed-d7e2c9b7-manifest";
  if (!testInfo.attachments.some(({ name }) => name === manifestAttachment)) {
    await testInfo.attach(manifestAttachment, {
      body: manifestBytes,
      contentType: "application/json",
    });
  }
  await testInfo.attach(`fixed-d7e2c9b7-${surface}-${viewport.width}`, {
    body: referenceBytes,
    contentType: "image/png",
  });

  const maskedProduction = await captureMaskedProductionScreenshot(page, surface);
  const productionBytes = maskedProduction.bytes;
  const productionDimensions = pngDimensions(productionBytes);
  if (
    productionDimensions.width !== viewport.width ||
    productionDimensions.height !== viewport.height
  ) {
    throw new Error(
      `production visual attachment dimensions mismatch for ${surface}-${viewport.width}: ` +
      `${productionDimensions.width}x${productionDimensions.height}`,
    );
  }
  await testInfo.attach(`production-${surface}-${viewport.width}`, {
    body: productionBytes,
    contentType: "image/png",
  });

  const reviewedProductionFilename = `reviewed-production-${filename}`;
  const reviewedProductionBytes = readFileSync(
    resolve(referenceDirectory, reviewedProductionFilename),
  );
  const reviewedProductionHash = sha256(reviewedProductionBytes);
  if (reviewedProductionHash !== entry.reviewedProductionSha256) {
    throw new Error(
      `reviewed production baseline hash mismatch for ${filename}: ` +
      reviewedProductionHash,
    );
  }
  const reviewedProductionDimensions = pngDimensions(reviewedProductionBytes);
  if (
    reviewedProductionDimensions.width !== viewport.width ||
    reviewedProductionDimensions.height !== viewport.height
  ) {
    throw new Error(
      `reviewed production baseline dimensions mismatch for ${filename}: ` +
      `${reviewedProductionDimensions.width}x${reviewedProductionDimensions.height}`,
    );
  }
  await testInfo.attach(`reviewed-production-${surface}-${viewport.width}`, {
    body: reviewedProductionBytes,
    contentType: "image/png",
  });
  const productionBaseline = assertReviewedProductionBaseline(
    productionBytes,
    reviewedProductionBytes,
    entry.maxProductionDiffPixels,
    `production ${surface}-${viewport.width}`,
  );
  await testInfo.attach(`reviewed-production-diff-${surface}-${viewport.width}`, {
    body: productionBaseline.diffBytes,
    contentType: "image/png",
  });

  const referencePng = PNG.sync.read(referenceBytes);
  const productionPng = PNG.sync.read(productionBytes);
  const diffPng = new PNG({ width: viewport.width, height: viewport.height });
  const diffPixelCount = pixelmatch(
    referencePng.data,
    productionPng.data,
    diffPng.data,
    viewport.width,
    viewport.height,
    { diffMask: true, includeAA: false, threshold: 0.2 },
  );
  const diffPixelRatio = diffPixelCount / (viewport.width * viewport.height);
  const diffGrid = diffGridRatios(diffPng);
  if (process.env.FIXED_REFERENCE_CALIBRATE === "1") {
    process.stdout.write(
      `FIXED_REFERENCE_SIGNATURE ${filename} ${diffPixelRatio} ${JSON.stringify(diffGrid)}\n`,
    );
    return;
  }
  const diffGridCellDeltas = assertDiffGridSignature(
    diffGrid,
    entry.expectedDiffGrid,
    entry.maxGridCellDelta,
    `production ${surface}-${viewport.width}`,
  );
  const diffPixelRatioDelta = Math.abs(
    diffPixelRatio - entry.expectedDiffPixelRatio,
  );
  const comparison = {
    surface,
    viewport,
    diffPixelCount,
    diffPixelRatio,
    expectedDiffPixelRatio: entry.expectedDiffPixelRatio,
    diffPixelRatioDelta,
    maxDiffPixelRatioDelta: entry.maxDiffPixelRatioDelta,
    diffGrid,
    expectedDiffGrid: entry.expectedDiffGrid,
    diffGridCellDeltas,
    maxGridCellDelta: entry.maxGridCellDelta,
    reviewedProductionSha256: entry.reviewedProductionSha256,
    productionBaselineDiffPixelCount: productionBaseline.diffPixelCount,
    maxProductionDiffPixels: entry.maxProductionDiffPixels,
    dynamicMaskAllowlist: maskedProduction.masks,
    pixelThreshold: 0.2,
    enforcement: "reviewed-production-pixels-plus-fixed-prototype-diff-signature",
  };
  await testInfo.attach(`fixed-comparison-${surface}-${viewport.width}`, {
    body: Buffer.from(JSON.stringify(comparison, null, 2)),
    contentType: "application/json",
  });
  await testInfo.attach(`fixed-diff-${surface}-${viewport.width}`, {
    body: PNG.sync.write(diffPng),
    contentType: "image/png",
  });
  if (diffPixelRatioDelta > entry.maxDiffPixelRatioDelta) {
    throw new Error(
      `production ${surface}-${viewport.width} fixed-reference difference drifted: ` +
      `actual ${(diffPixelRatio * 100).toFixed(3)}%, reviewed ` +
      `${(entry.expectedDiffPixelRatio * 100).toFixed(3)}% ± ` +
      `${(entry.maxDiffPixelRatioDelta * 100).toFixed(3)}%`,
    );
  }
}
