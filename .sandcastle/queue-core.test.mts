import assert from "node:assert/strict";
import test from "node:test";
import {
  issueSemanticFingerprint,
  pullRequestDisposition,
  PROTECTED_CANDIDATE_PATHS,
  selectNextFrontier,
  summarizeQueue,
  type PullRequestState,
  type QueueIssue,
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
