import { expect, type Page, type TestInfo } from "@playwright/test";

import {
  attachFixedVisualPair,
  inspectFixedVisualReference,
} from "./fixed-reference.js";


export const MANUAL_CREATION_FIXED_ROUTE =
  "?variant=A&view=questions&node=Q-38.2&panel=create-question";

export const MANUAL_CREATION_FIXED_VIEWPORTS = [
  { width: 1440, height: 900 },
  { width: 800, height: 900 },
  { width: 390, height: 844 },
] as const;

export function inspectManualCreationFixedReferences() {
  return MANUAL_CREATION_FIXED_VIEWPORTS.map((viewport) =>
    inspectFixedVisualReference("create-question", viewport),
  );
}

export async function assertManualCreationFixedVisualState(
  page: Page,
): Promise<void> {
  const route = new URL(page.url());
  expect(route.searchParams.get("variant")).toBe("A");
  expect(route.searchParams.get("view")).toBe("questions");
  expect(route.searchParams.get("panel")).toBe("create-question");
  const parentQuestionRef = route.searchParams.get("node");
  expect(parentQuestionRef).not.toBeNull();

  const dialog = page.getByRole("dialog", { name: "创建后续研究问题" });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveAttribute("data-open", "true");
  await expect(dialog).toHaveAttribute("data-prototype-source", "d7e2c9b7");
  await expect(dialog.locator(".manual-parent-context code")).toContainText(
    parentQuestionRef!,
  );
}

export async function attachManualCreationFixedVisual(
  page: Page,
  testInfo: TestInfo,
  viewport: (typeof MANUAL_CREATION_FIXED_VIEWPORTS)[number],
): Promise<void> {
  await assertManualCreationFixedVisualState(page);
  await attachFixedVisualPair(
    page,
    testInfo,
    "create-question",
    viewport,
  );
}
