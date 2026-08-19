import assert from "node:assert/strict";
import test from "node:test";
import {
  issueSemanticFingerprint,
  pullRequestDisposition,
  PROTECTED_CANDIDATE_PATHS,
  removeQueueAttempt,
  replaceQueueAttempt,
  selectFrontierBatch,
  selectNextFrontier,
  summarizeQueue,
  upsertQueueAttempt,
  type PullRequestState,
  type QueueIssue,
  type QueueSnapshot,
  type QueueState,
} from "./queue-core.mts";

test("candidate protection includes GitHub workflows", () => {
  assert.ok(PROTECTED_CANDIDATE_PATHS.includes(".github"));
});

const issue = (
  number: number,
  overrides: Partial<QueueIssue> = {},
): QueueIssue => ({
  assignees: [],
  blockedBy: { nodes: [], totalCount: 0 },
  body: "",
  labels: [{ name: "ready-for-agent" }],
  number,
  state: "OPEN",
  title: `实现：ticket ${number}`,
  updatedAt: "2026-08-19T00:00:00Z",
  url: `https://github.com/15253a/meta_research/issues/${number}`,
  ...overrides,
});

test("selects the lowest open native frontier and permits automatic claim", () => {
  const issues = [
    issue(114, {
      blockedBy: {
        totalCount: 1,
        nodes: [{ number: 113, state: "OPEN", title: "ticket 113" }],
      },
    }),
    issue(113),
    issue(115),
  ];

  assert.equal(selectNextFrontier(issues, "15253a")?.number, 113);
});

test("continues an issue already assigned only to the viewer", () => {
  assert.equal(
    selectNextFrontier(
      [issue(113, { assignees: [{ login: "15253a" }] })],
      "15253a",
    )?.number,
    113,
  );
});

test("selects at most three deterministic native-frontier issues", () => {
  assert.deepEqual(
    selectFrontierBatch({
      issues: [issue(116), issue(114), issue(113), issue(115)],
      viewer: "15253a",
      activeIssueNumbers: new Set(),
      limit: 3,
    }).map(({ number }) => number),
    [113, 114, 115],
  );
});

test("batch selection excludes active and blocked issues", () => {
  assert.deepEqual(
    selectFrontierBatch({
      issues: [
        issue(113),
        issue(114),
        issue(115, {
          blockedBy: {
            totalCount: 1,
            nodes: [{ number: 112, state: "OPEN", title: "ticket 112" }],
          },
        }),
        issue(116),
      ],
      viewer: "15253a",
      activeIssueNumbers: new Set([113]),
      limit: 3,
    }).map(({ number }) => number),
    [114, 116],
  );
});

test("batch selection accepts only limits from one through three", () => {
  for (const limit of [0, 4, 1.5]) {
    assert.throws(
      () =>
        selectFrontierBatch({
          issues: [issue(113)],
          viewer: "15253a",
          activeIssueNumbers: new Set(),
          limit,
        }),
      /integer from 1 to 3/,
    );
  }
});

test("does not take an issue assigned to another account", () => {
  assert.equal(
    selectNextFrontier(
      [
        issue(113, { assignees: [{ login: "someone-else" }] }),
        issue(114),
      ],
      "15253a",
    )?.number,
    114,
  );
});

test("rejects an incomplete native blocker snapshot", () => {
  assert.throws(
    () =>
      selectNextFrontier(
        [issue(113, { blockedBy: { nodes: [], totalCount: 1 } })],
        "15253a",
      ),
    /blocker data is incomplete/,
  );
});

test("semantic fingerprints ignore comments/time but detect requirement changes", () => {
  const baseline = issue(113);
  assert.equal(
    issueSemanticFingerprint(baseline),
    issueSemanticFingerprint({
      ...baseline,
      updatedAt: "2026-08-20T00:00:00Z",
    }),
  );
  assert.notEqual(
    issueSemanticFingerprint(baseline),
    issueSemanticFingerprint({ ...baseline, body: "changed requirement" }),
  );
});

test("summarizes queue progress without treating spec or HITL tickets as work", () => {
  const summary = summarizeQueue(
    [
      issue(112, { title: "Spec：system" }),
      issue(113, { state: "CLOSED" }),
      issue(114, { assignees: [{ login: "15253a" }] }),
      issue(133, { title: "真人验收" }),
    ],
    "15253a",
  );

  assert.deepEqual(summary, {
    total: 2,
    closed: 1,
    open: 1,
    assignedToViewer: 1,
    assignedElsewhere: 0,
    next: 114,
  });
});

test("a merged PR is accepted; an unmerged close stops the queue", () => {
  const base: PullRequestState = {
    number: 1,
    url: "https://github.com/15253a/meta_research/pull/1",
    state: "OPEN",
    isDraft: false,
    baseRefName: "develop_main",
    baseRefOid: "base",
    headRefName: "codex/issue-113-attempt",
    headRefOid: "candidate",
    mergeable: "MERGEABLE",
    mergeStateStatus: "CLEAN",
    mergedAt: null,
    mergeCommit: null,
  };

  assert.equal(pullRequestDisposition(base), "WAIT");
  assert.equal(
    pullRequestDisposition({
      ...base,
      state: "MERGED",
      mergedAt: "2026-08-19T01:00:00Z",
      mergeCommit: { oid: "abc" },
    }),
    "ACCEPT",
  );
  assert.equal(pullRequestDisposition({ ...base, state: "CLOSED" }), "STOP");
});

const queueState = (
  issueNumber: number,
  attemptId: string,
  overrides: Partial<QueueState> = {},
): QueueState => ({
  version: 1,
  stage: "IMPLEMENTING",
  issueNumber,
  issueTitle: `ticket ${issueNumber}`,
  branch: `codex/issue-${issueNumber}-${attemptId}`,
  baseRef: "develop_main",
  baseSha: "base",
  attemptId,
  receiptPath: `.sandcastle/receipts/${attemptId}.json`,
  leaseRef: `refs/heads/sandcastle/lease-${issueNumber}`,
  leaseSha: `lease-${issueNumber}`,
  updatedAt: "2026-08-19T00:00:00Z",
  ...overrides,
});

test("snapshot upserts and removals preserve unrelated attempts", () => {
  const empty: QueueSnapshot = { version: 2, attempts: [] };
  const first = queueState(113, "attempt-113");
  const second = queueState(114, "attempt-114");

  const withFirst = upsertQueueAttempt(empty, first);
  const withBoth = upsertQueueAttempt(withFirst, second);
  const updatedFirst = upsertQueueAttempt(
    withBoth,
    queueState(113, "attempt-113", { stage: "PUBLISHING" }),
  );
  const withoutFirst = removeQueueAttempt(updatedFirst, "attempt-113");

  assert.deepEqual(empty.attempts, []);
  assert.deepEqual(withFirst.attempts.map(({ attemptId }) => attemptId), [
    "attempt-113",
  ]);
  assert.deepEqual(
    withBoth.attempts.map(({ attemptId }) => attemptId),
    ["attempt-113", "attempt-114"],
  );
  assert.equal(updatedFirst.attempts[0]?.stage, "PUBLISHING");
  assert.equal(updatedFirst.attempts[1]?.attemptId, "attempt-114");
  assert.deepEqual(withoutFirst.attempts.map(({ attemptId }) => attemptId), [
    "attempt-114",
  ]);
  assert.equal(withBoth.attempts[0]?.stage, "IMPLEMENTING");
});

test("targeted retry atomically replaces one failure and preserves siblings", () => {
  const firstFailure = queueState(113, "attempt-113", {
    stage: "NEEDS_HUMAN",
    failedStage: "IMPLEMENTING",
  });
  const secondFailure = queueState(114, "attempt-114", {
    stage: "NEEDS_HUMAN",
    failedStage: "IMPLEMENTING",
  });
  const snapshot: QueueSnapshot = {
    version: 2,
    attempts: [firstFailure, secondFailure],
  };
  const replacement = queueState(113, "retry-113", {
    stage: "CLAIMING",
  });

  const retried = replaceQueueAttempt(
    snapshot,
    firstFailure.attemptId,
    replacement,
  );

  assert.deepEqual(
    retried.attempts.map(({ issueNumber, attemptId, stage }) => ({
      issueNumber,
      attemptId,
      stage,
    })),
    [
      { issueNumber: 113, attemptId: "retry-113", stage: "CLAIMING" },
      { issueNumber: 114, attemptId: "attempt-114", stage: "NEEDS_HUMAN" },
    ],
  );
  assert.equal(snapshot.attempts[0]?.attemptId, "attempt-113");
});
