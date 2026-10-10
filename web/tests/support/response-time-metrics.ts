export function summarizeLatency(
  samples: readonly number[],
  expectedSamples: number,
  p95BudgetMs: number,
) {
  if (!Number.isInteger(expectedSamples) || expectedSamples < 20
    || !Number.isFinite(p95BudgetMs) || p95BudgetMs <= 0
    || samples.some(value => !Number.isFinite(value) || value < 0)) {
    throw new Error("invalid response-time measurements or budget");
  }
  const sorted = [...samples].sort((a, b) => a - b);
  const count = sorted.length;
  const middle = Math.floor(count / 2);
  const median = count === 0 ? null : count % 2
    ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  const p95 = count === 0 ? null : sorted[Math.ceil(count * 0.95) - 1];
  const maximum = count === 0 ? null : sorted[count - 1];
  return {
    count,
    expected_samples: expectedSamples,
    raw_ms: [...samples],
    median_ms: median,
    p95_ms: p95,
    max_ms: maximum,
    p95_budget_ms: p95BudgetMs,
    max_budget_ms: p95BudgetMs * 2,
    passed: count === expectedSamples && p95 !== null && maximum !== null
      && p95 <= p95BudgetMs && maximum <= p95BudgetMs * 2,
  };
}
