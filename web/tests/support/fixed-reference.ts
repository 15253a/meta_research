import type { Page, TestInfo } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import pixelmatch from "pixelmatch";
import { PNG } from "pngjs";


export type FixedReferenceSurface =
  | "shell"
  | "create-quest"
  | "create-question"
  | "human-request"
  | "external-request"
  | "offline-operation"
  | "permission-request";

type FixedReferenceEntryBase = {
  surface: FixedReferenceSurface;
  viewport: { width: number; height: number };
  sha256: string;
};

type ReviewedFixedReferenceEntry = FixedReferenceEntryBase & {
  productionReview?: "reviewed";
  expectedDiffPixelRatio: number;
  maxDiffPixelRatioDelta: number;
  expectedDiffGrid: number[];
  maxGridCellDelta: number;
  reviewedProductionSha256: string;
  maxProductionDiffPixels: number;
};

type PendingFixedReferenceEntry = FixedReferenceEntryBase & {
  productionReview: "pending";
};

type FixedReferenceEntry =
  | ReviewedFixedReferenceEntry
  | PendingFixedReferenceEntry;

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
type FixedDynamicMask = {
  selector: string;
  reason: string;
  region?: "element" | "viewport-right-of-element";
};
type FixedDynamicText = {
  selector: string;
  canonicalText: string;
  reason: string;
};
const FIXED_DYNAMIC_MASK_ALLOWLIST: Record<
  FixedReferenceSurface,
  FixedDynamicMask[]
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
  "create-question": [
    { selector: ".lumen-connection code", reason: "durable feed revision" },
    {
      selector: ".manual-chip:first-child",
      reason: "accepted Quest ref",
    },
    {
      selector: ".manual-parent-context code",
      reason: "accepted parent Question ref",
    },
    {
      selector: ".manual-dialog",
      reason: "native dialog backdrop over independently scrolling Companion",
      region: "viewport-right-of-element",
    },
  ],
  "human-request": [],
  "external-request": [],
  "offline-operation": [],
  "permission-request": [],
};
const FIXED_DYNAMIC_TEXT_ALLOWLIST: Record<
  FixedReferenceSurface,
  FixedDynamicText[]
> = {
  shell: [
    {
      selector: ".lumen-connection code",
      canonicalText: "rev 1",
      reason: "durable feed revision must not change fixed-reference geometry",
    },
    {
      selector: ".lumen-next-card .lumen-card-head small",
      canonicalText: "rev 1",
      reason: "monotonic Snapshot revision",
    },
  ],
  "create-quest": [
    {
      selector: ".lumen-connection code",
      canonicalText: "rev 1",
      reason: "durable feed revision must not change fixed-reference geometry",
    },
  ],
  "create-question": [],
  "human-request": [],
  "external-request": [],
  "offline-operation": [],
  "permission-request": [],
};
const FIXED_MASK_RGBA = [32, 42, 58, 255] as const;
type RasterRgba = readonly [number, number, number, number];
type ReviewedRasterPixel = {
  offsetX: number;
  offsetY: number;
  reviewedRgba: RasterRgba;
  chromiumRgba: RasterRgba;
};
type FixedRasterNormalization = {
  viewport: { width: number; height: number };
  selector: string;
  reason: string;
  pixels: ReviewedRasterPixel[];
};
type FixedRasterRegionNormalization = {
  viewport: { width: number; height: number };
  region: { x: number; y: number; width: number; height: number };
  reason: string;
  reviewedRgbaSha256: string;
  chromiumRgbaSha256: string;
};
const FIXED_RASTER_NORMALIZATION_ALLOWLIST: Record<
  FixedReferenceSurface,
  FixedRasterNormalization[]
> = {
  shell: [],
  "create-quest": [
    {
      viewport: { width: 1440, height: 900 },
      selector: ".quest-footer .confirm:disabled",
      reason: "reviewed Chromium rounded-corner channel rounding",
      pixels: [
        {
          offsetX: 7,
          offsetY: 43,
          reviewedRgba: [226, 229, 236, 255],
          chromiumRgba: [227, 229, 236, 255],
        },
        {
          offsetX: 8,
          offsetY: 43,
          reviewedRgba: [228, 231, 238, 255],
          chromiumRgba: [228, 232, 238, 255],
        },
      ],
    },
  ],
  "create-question": [
    {
      viewport: { width: 1440, height: 900 },
      selector: "body",
      reason: "reviewed Chromium native dialog shadow channel rounding",
      pixels: [
        {
          offsetX: 1385,
          offsetY: 36,
          reviewedRgba: [131, 135, 145, 255],
          chromiumRgba: [131, 135, 144, 255],
        },
        {
          offsetX: 1384,
          offsetY: 55,
          reviewedRgba: [122, 127, 136, 255],
          chromiumRgba: [122, 127, 137, 255],
        },
        {
          offsetX: 1389,
          offsetY: 42,
          reviewedRgba: [128, 133, 143, 255],
          chromiumRgba: [129, 133, 143, 255],
        },
        {
          offsetX: 1390,
          offsetY: 42,
          reviewedRgba: [128, 133, 143, 255],
          chromiumRgba: [129, 133, 143, 255],
        },
        {
          offsetX: 1389,
          offsetY: 55,
          reviewedRgba: [123, 126, 137, 255],
          chromiumRgba: [123, 127, 138, 255],
        },
      ],
    },
  ],
  "human-request": [],
  "external-request": [],
  "offline-operation": [],
  "permission-request": [],
};
const FIXED_RASTER_REGION_NORMALIZATION_ALLOWLIST: Record<
  FixedReferenceSurface,
  FixedRasterRegionNormalization[]
> = {
  shell: [],
  "create-quest": [
    {
      viewport: { width: 1440, height: 900 },
      region: { x: 1389, y: 31, width: 32, height: 35 },
      reason: "reviewed Chromium native dialog corner compositing",
      reviewedRgbaSha256: "98fa65e99adb653e31815e1b08081cf1bbb3e9f0f700f5346a19b732ffa8dd16",
      chromiumRgbaSha256: "17040ea141af7ac5dc9da81bb77d37b875a6a2bd16c245d96b2200e385edcd71",
    },
  ],
  "create-question": [],
  "human-request": [],
  "external-request": [],
  "offline-operation": [],
  "permission-request": [],
};

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

export type FixedVisualReferenceInspection = {
  filename: string;
  sha256: string;
  viewport: { width: number; height: number };
  productionReview: "pending" | "reviewed";
};

export function inspectFixedVisualReference(
  surface: FixedReferenceSurface,
  viewport: { width: number; height: number },
): FixedVisualReferenceInspection {
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
  return {
    filename,
    sha256: actualHash,
    viewport: dimensions,
    productionReview: entry.productionReview === "pending" ? "pending" : "reviewed",
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

function rgbaMatches(
  data: Buffer,
  offset: number,
  expected: RasterRgba,
): boolean {
  return expected.every((value, index) => data[offset + index] === value);
}

export function normalizeReviewedRasterPixels(
  png: PNG,
  pixels: Array<ReviewedRasterPixel & { x: number; y: number }>,
  label: string,
): number {
  let normalizedPixelCount = 0;
  for (const pixel of pixels) {
    if (
      !Number.isInteger(pixel.x) ||
      !Number.isInteger(pixel.y) ||
      pixel.x < 0 ||
      pixel.y < 0 ||
      pixel.x >= png.width ||
      pixel.y >= png.height
    ) {
      throw new Error(`${label} raster normalization coordinate is invalid`);
    }
    const offset = (pixel.y * png.width + pixel.x) * 4;
    if (rgbaMatches(png.data, offset, pixel.reviewedRgba)) continue;
    if (!rgbaMatches(png.data, offset, pixel.chromiumRgba)) {
      const actual = Array.from(png.data.subarray(offset, offset + 4));
      throw new Error(
        `${label} raster normalization pixel ${pixel.x},${pixel.y} changed: ` +
        actual.join(","),
      );
    }
    png.data.set(pixel.reviewedRgba, offset);
    normalizedPixelCount += 1;
  }
  return normalizedPixelCount;
}

function rasterRegionBytes(
  png: PNG,
  region: FixedRasterRegionNormalization["region"],
  label: string,
): Buffer {
  const { x, y, width, height } = region;
  if (
    ![x, y, width, height].every(Number.isInteger) ||
    x < 0 ||
    y < 0 ||
    width <= 0 ||
    height <= 0 ||
    x + width > png.width ||
    y + height > png.height
  ) {
    throw new Error(`${label} raster region is invalid`);
  }
  const rows: Buffer[] = [];
  for (let row = y; row < y + height; row += 1) {
    const start = (row * png.width + x) * 4;
    rows.push(png.data.subarray(start, start + width * 4));
  }
  return Buffer.concat(rows);
}

export function normalizeReviewedRasterRegion(
  png: PNG,
  reviewed: PNG,
  normalization: FixedRasterRegionNormalization,
  label: string,
): boolean {
  if (png.width !== reviewed.width || png.height !== reviewed.height) {
    throw new Error(`${label} reviewed raster dimensions changed`);
  }
  const reviewedBytes = rasterRegionBytes(reviewed, normalization.region, label);
  const reviewedHash = sha256(reviewedBytes);
  if (reviewedHash !== normalization.reviewedRgbaSha256) {
    throw new Error(`${label} reviewed raster region changed`);
  }
  const actualBytes = rasterRegionBytes(png, normalization.region, label);
  const actualHash = sha256(actualBytes);
  if (actualHash === reviewedHash) return false;
  if (actualHash !== normalization.chromiumRgbaSha256) {
    throw new Error(`${label} Chromium raster region changed`);
  }
  const { x, y, width, height } = normalization.region;
  for (let row = y; row < y + height; row += 1) {
    const start = (row * png.width + x) * 4;
    const reviewedStart = (row * reviewed.width + x) * 4;
    png.data.set(
      reviewed.data.subarray(reviewedStart, reviewedStart + width * 4),
      start,
    );
  }
  return true;
}

export async function captureFixedProductionScreenshot(
  page: Page,
  surface: FixedReferenceSurface,
  viewport: { width: number; height: number },
): Promise<{
  bytes: Buffer;
  masks: FixedDynamicMask[];
  rasterNormalizations: Array<{
    selector: string;
    reason: string;
    pixels: Array<{ x: number; y: number }>;
    normalizedPixelCount: number;
  }>;
  canonicalizedTexts: FixedDynamicText[];
  rasterRegionNormalizations: Array<{
    reason: string;
    region: FixedRasterRegionNormalization["region"];
    normalized: boolean;
  }>;
}> {
  const canonicalizedTexts = FIXED_DYNAMIC_TEXT_ALLOWLIST[surface];
  const originalTexts: Array<{ locator: ReturnType<Page["locator"]>; text: string | null }> = [];
  for (const dynamicText of canonicalizedTexts) {
    const locator = page.locator(dynamicText.selector);
    const count = await locator.count();
    if (count === 0) {
      throw new Error(
        `fixed visual dynamic text selector is missing: ${dynamicText.selector}`,
      );
    }
    for (let index = 0; index < count; index += 1) {
      const item = locator.nth(index);
      originalTexts.push({ locator: item, text: await item.textContent() });
      await item.evaluate((element, text) => {
        element.textContent = text;
      }, dynamicText.canonicalText);
    }
  }
  if (canonicalizedTexts.length > 0) {
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    }));
  }

  const masks = FIXED_DYNAMIC_MASK_ALLOWLIST[surface];
  const boxes: Array<{
    mask: FixedDynamicMask;
    box: { x: number; y: number; width: number; height: number };
  }> = [];
  for (const mask of masks) {
    const locator = page.locator(mask.selector);
    const count = await locator.count();
    if (count === 0) {
      throw new Error(
        `fixed visual dynamic mask selector is missing: ${mask.selector}`,
      );
    }
    for (let index = 0; index < count; index += 1) {
      const box = await locator.nth(index).boundingBox();
      if (box) boxes.push({ mask, box });
    }
  }
  let raw: Buffer;
  try {
    raw = await page.screenshot({ animations: "disabled" });
  } finally {
    for (const original of originalTexts) {
      await original.locator.evaluate((element, text) => {
        element.textContent = text;
      }, original.text);
    }
  }
  const masked = PNG.sync.read(raw);
  for (const { mask, box } of boxes) {
    const masksRightBackdrop = mask.region === "viewport-right-of-element";
    const left = masksRightBackdrop
      ? Math.min(masked.width, Math.ceil(box.x + box.width))
      : Math.max(0, Math.floor(box.x));
    const top = masksRightBackdrop ? 0 : Math.max(0, Math.floor(box.y));
    const right = masksRightBackdrop
      ? masked.width
      : Math.min(masked.width, Math.ceil(box.x + box.width));
    const bottom = masksRightBackdrop
      ? masked.height
      : Math.min(masked.height, Math.ceil(box.y + box.height));
    for (let y = top; y < bottom; y += 1) {
      for (let x = left; x < right; x += 1) {
        const offset = (y * masked.width + x) * 4;
        masked.data.set(FIXED_MASK_RGBA, offset);
      }
    }
  }
  const rasterNormalizations = [];
  for (const normalization of FIXED_RASTER_NORMALIZATION_ALLOWLIST[surface]) {
    if (
      normalization.viewport.width !== viewport.width ||
      normalization.viewport.height !== viewport.height
    ) {
      continue;
    }
    const locator = page.locator(normalization.selector);
    if (await locator.count() !== 1) {
      throw new Error(
        `fixed visual raster normalization selector is not unique: ${normalization.selector}`,
      );
    }
    const box = await locator.boundingBox();
    if (!box) {
      throw new Error(
        `fixed visual raster normalization selector is not visible: ${normalization.selector}`,
      );
    }
    const pixels = normalization.pixels.map((pixel) => ({
      ...pixel,
      x: Math.floor(box.x) + pixel.offsetX,
      y: Math.floor(box.y) + pixel.offsetY,
    }));
    const normalizedPixelCount = normalizeReviewedRasterPixels(
      masked,
      pixels,
      `fixed visual ${surface}-${viewport.width} ${normalization.selector}`,
    );
    rasterNormalizations.push({
      selector: normalization.selector,
      reason: normalization.reason,
      pixels: pixels.map(({ x, y }) => ({ x, y })),
      normalizedPixelCount,
    });
  }
  const rasterRegionNormalizations = [];
  for (const normalization of FIXED_RASTER_REGION_NORMALIZATION_ALLOWLIST[surface]) {
    if (
      normalization.viewport.width !== viewport.width ||
      normalization.viewport.height !== viewport.height
    ) {
      continue;
    }
    const reviewedFilename =
      `reviewed-production-${surface}-${viewport.width}.png`;
    const reviewed = PNG.sync.read(
      readFileSync(resolve(referenceDirectory, reviewedFilename)),
    );
    const normalized = normalizeReviewedRasterRegion(
      masked,
      reviewed,
      normalization,
      `fixed visual ${surface}-${viewport.width}`,
    );
    rasterRegionNormalizations.push({
      reason: normalization.reason,
      region: normalization.region,
      normalized,
    });
  }
  return {
    bytes: PNG.sync.write(masked),
    masks,
    rasterNormalizations,
    canonicalizedTexts,
    rasterRegionNormalizations,
  };
}

export async function attachFixedVisualPair(
  page: Page,
  testInfo: TestInfo,
  surface: FixedReferenceSurface,
  viewport: { width: number; height: number },
): Promise<void> {
  const inspection = inspectFixedVisualReference(surface, viewport);
  const filename = inspection.filename;
  const entry = manifest.references[filename];
  if (!entry || entry.productionReview === "pending") {
    throw new Error(
      `fixed visual production baseline is pending review: ${filename}`,
    );
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

  const maskedProduction = await captureFixedProductionScreenshot(
    page,
    surface,
    viewport,
  );
  const visibleProductionBytes = maskedProduction.bytes;
  const productionDimensions = pngDimensions(visibleProductionBytes);
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
    body: visibleProductionBytes,
    contentType: "image/png",
  });
  const reviewedProductionFilename = `reviewed-production-${filename}`;
  const reviewedProductionPath = resolve(
    referenceDirectory,
    reviewedProductionFilename,
  );
  const reviewedProductionBytes = readFileSync(reviewedProductionPath);
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
  const productionBytes = visibleProductionBytes;
  await testInfo.attach(`contract-normalized-production-${surface}-${viewport.width}`, {
    body: productionBytes,
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
    rasterNormalizationAllowlist: maskedProduction.rasterNormalizations,
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
