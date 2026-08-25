import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { PNG } from "pngjs";

import {
  assertDiffGridSignature,
  assertReviewedProductionBaseline,
  normalizeReviewedRasterPixels,
  normalizeReviewedRasterRegion,
} from "./fixed-reference.js";


test("rejects a spatially displaced diff with the same total ratio", () => {
  const reviewed = [0.4, ...Array<number>(15).fill(0)];
  const displaced = [0, 0.4, ...Array<number>(14).fill(0)];

  expect(
    reviewed.reduce((sum, value) => sum + value, 0),
  ).toBe(displaced.reduce((sum, value) => sum + value, 0));
  expect(() =>
    assertDiffGridSignature(displaced, reviewed, 0.001, "same-total-spatial-drift"),
  ).toThrow(/grid cell 0 drifted/);
});

test("rejects an in-cell spatial rearrangement with the same diff count", () => {
  const baseline = new PNG({ width: 8, height: 8 });
  const rearranged = new PNG({ width: 8, height: 8 });
  for (const [index, x] of [0, 1, 2, 3].entries()) {
    baseline.data[(index * 8 + x) * 4 + 3] = 255;
    rearranged.data[(index * 8 + x + 4) * 4 + 3] = 255;
  }

  expect(() => assertReviewedProductionBaseline(
    PNG.sync.write(rearranged),
    PNG.sync.write(baseline),
    0,
    "same-count-in-cell-rearrangement",
  )).toThrow(/reviewed production baseline drifted/);
});

test("rejects a fixed color-token drift below the prototype similarity threshold", () => {
  const baseline = new PNG({ width: 8, height: 8 });
  const recolored = new PNG({ width: 8, height: 8 });
  for (let offset = 0; offset < baseline.data.length; offset += 4) {
    baseline.data.set([23, 32, 51, 255], offset);
    recolored.data.set([55, 64, 83, 255], offset);
  }

  expect(() => assertReviewedProductionBaseline(
    PNG.sync.write(recolored),
    PNG.sync.write(baseline),
    0,
    "fixed-color-token-drift",
  )).toThrow(/reviewed production baseline drifted/);
});

test("normalizes only an exact reviewed rounded-corner raster variant", () => {
  const image = new PNG({ width: 4, height: 4 });
  image.data.fill(255);
  image.data.set([227, 229, 236, 255], (2 * image.width + 1) * 4);

  expect(normalizeReviewedRasterPixels(image, [
    {
      x: 1,
      y: 2,
      offsetX: 1,
      offsetY: 2,
      reviewedRgba: [226, 229, 236, 255],
      chromiumRgba: [227, 229, 236, 255],
    },
  ], "rounded-corner")).toBe(1);
  expect(
    Array.from(image.data.subarray((2 * image.width + 1) * 4, (2 * image.width + 2) * 4)),
  ).toEqual([226, 229, 236, 255]);
  expect(Array.from(image.data.subarray(0, 4))).toEqual([255, 255, 255, 255]);

  image.data.set([227, 230, 236, 255], (2 * image.width + 1) * 4);
  expect(() => normalizeReviewedRasterPixels(image, [
    {
      x: 1,
      y: 2,
      offsetX: 1,
      offsetY: 2,
      reviewedRgba: [226, 229, 236, 255],
      chromiumRgba: [227, 229, 236, 255],
    },
  ], "rounded-corner")).toThrow(/raster normalization pixel 1,2 changed/);
});

test("normalizes only an exact reviewed Chromium raster region", () => {
  const reviewed = new PNG({ width: 2, height: 2 });
  const chromium = new PNG({ width: 2, height: 2 });
  reviewed.data.set([1, 2, 3, 255], 0);
  chromium.data.set([4, 5, 6, 255], 0);
  const hash = (bytes: Buffer) =>
    createHash("sha256").update(bytes).digest("hex");
  const normalization = {
    viewport: { width: 2, height: 2 },
    region: { x: 0, y: 0, width: 1, height: 1 },
    reason: "test exact Chromium raster",
    reviewedRgbaSha256: hash(Buffer.from([1, 2, 3, 255])),
    chromiumRgbaSha256: hash(Buffer.from([4, 5, 6, 255])),
  };

  expect(normalizeReviewedRasterRegion(
    chromium,
    reviewed,
    normalization,
    "exact-region",
  )).toBe(true);
  expect(Array.from(chromium.data.subarray(0, 4))).toEqual([1, 2, 3, 255]);

  chromium.data.set([7, 8, 9, 255], 0);
  expect(() => normalizeReviewedRasterRegion(
    chromium,
    reviewed,
    normalization,
    "unexpected-region",
  )).toThrow(/Chromium raster region changed/);
});
