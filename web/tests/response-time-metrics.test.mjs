import assert from "node:assert/strict";
import test from "node:test";
import { summarizeLatency } from "./support/response-time-metrics.ts";

test("latency gate uses nearest-rank P95 and keeps the original sample order", () => {
  const samples = Array.from({ length: 20 }, (_, i) => 20 - i);
  const result = summarizeLatency(samples, 20, 19);
  assert.equal(result.p95_ms, 19);
  assert.equal(result.median_ms, 10.5);
  assert.equal(result.max_ms, 20);
  assert.deepEqual(result.raw_ms, samples);
  assert.equal(result.passed, true);
});

test("one extreme delay fails the max cap even when P95 passes", () => {
  const result = summarizeLatency([...Array(19).fill(100), 2001], 20, 1000);
  assert.equal(result.p95_ms, 100);
  assert.equal(result.passed, false);
});

test("slow P95 and incomplete sampling cannot produce a passing gate", () => {
  assert.equal(summarizeLatency(Array(20).fill(1001), 20, 1000).passed, false);
  assert.equal(summarizeLatency(Array(19).fill(1), 20, 1000).passed, false);
  assert.equal(summarizeLatency([], 20, 1000).passed, false);
});

test("invalid samples and too few expected samples are rejected", () => {
  for (const value of [-1, NaN, Infinity]) {
    assert.throws(() => summarizeLatency([value], 20, 1000));
  }
  assert.throws(() => summarizeLatency([1], 5, 1000));
  assert.throws(() => summarizeLatency([1], 20, 0));
});
