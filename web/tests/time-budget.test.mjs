import assert from "node:assert/strict";
import test from "node:test";
import { timeBudgetHours, timeBudgetLabel, validTimeBudget } from "../src/timeBudget.ts";

test("custom hour budgets retain their unit and display legacy budgets in hours", () => {
  for (const [budget, hours] of [["2.5h", "2.5"], ["0.01h", "0.01"], ["7d", "168"], ["30d", "720"], ["90d", "2160"]]) {
    assert.equal(validTimeBudget(budget), true);
    assert.equal(timeBudgetHours(budget), hours);
    assert.equal(timeBudgetLabel(budget), `${hours} 小时`);
  }
  assert.equal(validTimeBudget("open"), true);
  assert.equal(timeBudgetLabel("open"), "不设硬截止");
});

test("hour budgets reject empty, nonpositive, excessive precision and unsafe values", () => {
  for (const budget of ["", "h", "0h", "0.00h", "-1h", "NaNh", "Infinityh", "1e3h", "0.001h", "2.5d", "2h\n", "constructor", "toString", "__proto__", "999999999999999h"]) {
    assert.equal(validTimeBudget(budget), false, budget);
  }
});
