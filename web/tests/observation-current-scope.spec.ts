import { expect, test } from "@playwright/test";
import { openTimeline } from "./research-timeline-fixture.js";

test("a newer live foreground cannot label the retained projection's Target facts as current", async ({ page }) => {
  const writes: string[] = [];
  page.on("request", request => {
    if (!["GET", "HEAD"].includes(request.method())) writes.push(request.method());
  });
  const { snapshot, runtime, errors } = await openTimeline(page, { runningBundle: true });
  await page.locator("details.research-existing-details > summary").click();
  const facts = page.getByRole("region", { name: "当前研究工作状态", exact: true });
  await expect(facts).toBeVisible();
  const observedAt = new Date().toISOString();
  runtime.status = {
    schema_ref: "meta-research/runtime-status/v1", revision: snapshot.revision + 2,
    observed_at: observedAt, updated_at: observedAt, state: "running",
    current_task: { kind: "stage", title: "新的研究轮次", run_ref: "new-cycle-run", target_ref: null, status: "running" },
    waiting_reason: null, pending_requests: 0,
    foreground: { ...snapshot.research_control.foreground, cycle_ref: "new-observed-cycle", question_ref: "new-observed-question" },
    health: { status: "ready", checks: [] },
  };
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await expect(page.getByTestId("workspace-live-overview")).toHaveAttribute("data-cycle-ref", "new-observed-cycle");
  await expect(facts).toHaveCount(0);
  expect(snapshot.research_control.foreground.cycle_ref).not.toBe("new-observed-cycle");
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});
