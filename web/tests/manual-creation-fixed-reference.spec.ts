import { expect, test } from "@playwright/test";

import {
  inspectManualCreationFixedReferences,
  MANUAL_CREATION_FIXED_ROUTE,
} from "./support/manual-creation-fixed-visual.js";


test("the accepted ManualCreation prototype route and rasters stay immutable", () => {
  expect(MANUAL_CREATION_FIXED_ROUTE).toBe(
    "?variant=A&view=questions&node=Q-38.2&panel=create-question",
  );
  expect(inspectManualCreationFixedReferences()).toEqual([
    {
      filename: "create-question-1440.png",
      sha256: "9f850600a06262f9a43b571d62e2bf87860e39085f290e26bd6fcffa68030d27",
      viewport: { width: 1440, height: 900 },
      productionReview: "reviewed",
    },
    {
      filename: "create-question-800.png",
      sha256: "504ff7dc22124664a983baaf351a02791b946986f8e2c97f93507d8afa3c5095",
      viewport: { width: 800, height: 900 },
      productionReview: "reviewed",
    },
    {
      filename: "create-question-390.png",
      sha256: "21464171e4c6173643a3e8906c58443b497b775155cefb4b21db21cff0b94ffb",
      viewport: { width: 390, height: 844 },
      productionReview: "reviewed",
    },
  ]);
});
