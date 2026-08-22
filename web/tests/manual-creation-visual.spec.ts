import { expect, test, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";

import {
  DeterministicProduct,
  openAuthenticatedProduct,
} from "./support/deterministic-product.js";
import { captureFixedProductionScreenshot } from "./support/fixed-reference.js";
import {
  assertManualCreationFixedVisualState,
  attachManualCreationFixedVisual,
  inspectManualCreationFixedReferences,
  MANUAL_CREATION_FIXED_VIEWPORTS,
} from "./support/manual-creation-fixed-visual.js";


type QuestionTreeSnapshot = {
  question_tree: {
    status: "ready" | "unavailable";
    items: Array<{
      question_ref: string;
      parent_question_ref: string | null;
    }>;
  };
};

const productionReviewPending = inspectManualCreationFixedReferences().some(
  (reference) => reference.productionReview === "pending",
);
const capturePending = productionReviewPending &&
  process.env.FIXED_REFERENCE_CAPTURE_PENDING === "1";
test.skip(
  productionReviewPending && !capturePending,
  "ManualCreation reviewed-production rasters await explicit visual review",
);

let product: DeterministicProduct | undefined;

test.beforeAll(async () => {
  product = await DeterministicProduct.start({ manualRoot: true });
});

test.afterAll(async () => {
  await product?.stop();
  product = undefined;
});

async function acceptedRootQuestionRef(page: Page): Promise<string> {
  if (!product) throw new Error("deterministic product missing");
  const response = await page.request.get(`${product.baseUrl}/api/v1/snapshot`);
  expect(response.ok()).toBeTruthy();
  const snapshot = await response.json() as QuestionTreeSnapshot;
  expect(snapshot.question_tree.status).toBe("ready");
  const root = snapshot.question_tree.items.find(
    (item) => item.parent_question_ref === null,
  );
  if (!root) throw new Error("manualRoot fixture did not publish a root Question");
  return root.question_ref;
}

test("ManualCreation matches the reviewed production baseline and fixed prototype", async (
  { page },
  testInfo,
) => {
  test.setTimeout(120_000);
  if (!product) throw new Error("deterministic product missing");
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize(MANUAL_CREATION_FIXED_VIEWPORTS[0]);
  await openAuthenticatedProduct(page, product);
  const parentQuestionRef = await acceptedRootQuestionRef(page);
  await page.goto(
    `${product.baseUrl}/?variant=A&view=questions&node=${encodeURIComponent(parentQuestionRef)}&panel=create-question`,
    { waitUntil: "domcontentloaded" },
  );
  const dialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "关闭创建 Question 窗口" }),
  ).toBeFocused();
  await expect(dialog.getByTestId("manual-confirmation-footer")).toContainText(
    "描述",
  );

  for (const viewport of MANUAL_CREATION_FIXED_VIEWPORTS) {
    await page.setViewportSize(viewport);
    await assertManualCreationFixedVisualState(page);
    if (capturePending) {
      const capture = await captureFixedProductionScreenshot(
        page,
        "create-question",
        viewport,
      );
      const outputPath = testInfo.outputPath(
        `reviewed-production-create-question-${viewport.width}.png`,
      );
      writeFileSync(outputPath, capture.bytes);
      await testInfo.attach(
        `pending-reviewed-production-create-question-${viewport.width}`,
        { path: outputPath, contentType: "image/png" },
      );
    } else {
      await attachManualCreationFixedVisual(page, testInfo, viewport);
    }
  }
});
