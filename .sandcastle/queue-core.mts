export const IMPLEMENTATION_ISSUE_MIN = 113;
export const IMPLEMENTATION_ISSUE_MAX = 132;
export const PROTECTED_CANDIDATE_PATHS = [
  ".sandcastle",
  ".agents",
  ".codex",
  ".github",
  "package.json",
  "package-lock.json",
  "tsconfig.json",
  ":(glob)**/AGENTS.md",
  ":(glob)**/CLAUDE.md",
] as const;

export type IssueRef = {
  number: number;
  state: "OPEN" | "CLOSED";
  title: string;
};

export type QueueIssue = {
  assignees: Array<{ login: string }>;
  blockedBy: { nodes: IssueRef[]; totalCount: number };
  body: string;
  labels: Array<{ name: string }>;
  number: number;
  state: "OPEN" | "CLOSED";
  title: string;
  updatedAt: string;
  url: string;
};

export type QueueStage =
  | "CLAIMING"
  | "IMPLEMENTING"
  | "PUBLISHING"
  | "MERGING"
  | "CLOSING"
  | "NEEDS_HUMAN";

export type IntegrationVerification = {
  sourceBaseSha: string;
  sourceCandidateSha: string;
  baseSha: string;
  candidateSha: string;
  treeSha: string;
  mergeCommitSha: string;
  verifiedAt: string;
};

export type QueueState = {
  version: 1;
  stage: QueueStage;
  issueNumber: number;
  issueTitle: string;
  branch: string;
  baseRef: string;
  baseSha: string;
  candidateSha?: string;
  attemptId: string;
  slot?: number;
  receiptPath: string;
  leaseRef: string;
  leaseSha: string;
  prNumber?: number;
  prUrl?: string;
  acceptedSha?: string;
  integrationVerification?: IntegrationVerification;
  failedStage?: Exclude<QueueStage, "NEEDS_HUMAN">;
  resumeStage?: Exclude<QueueStage, "NEEDS_HUMAN">;
  error?: string;
  updatedAt: string;
};

export type QueueSnapshot = {
  version: 2;
  attempts: QueueState[];
};

/**
 * A post-merge verification timeout cannot change the already accepted tree.
 * It is safe to retry that exact verification after the host condition clears.
 * This also recognizes checkpoints written before timeouts were resumable.
 */
export const resumeStageForAttempt = (
  state: QueueState,
): Exclude<QueueStage, "NEEDS_HUMAN"> | undefined => {
  if (state.stage !== "NEEDS_HUMAN") return undefined;
  if (state.resumeStage) return state.resumeStage;
  if (
    state.failedStage === "MERGING" &&
    state.candidateSha &&
    state.prNumber &&
    state.prUrl &&
    state.integrationVerification &&
    /Post-merge verification failed.+verification exited with status 124/.test(
      state.error ?? "",
    )
  ) {
    return "MERGING";
  }
  return undefined;
};

export type PullRequestState = {
  number: number;
  url: string;
  state: "OPEN" | "CLOSED" | "MERGED";
  isDraft: boolean;
  baseRefName: string;
  baseRefOid: string;
  headRefName: string;
  headRefOid: string;
  mergeable: "MERGEABLE" | "CONFLICTING" | "UNKNOWN";
  mergeStateStatus: string;
  mergedAt: string | null;
  mergeCommit: { oid: string } | null;
};

export const isImplementationIssue = (issue: QueueIssue): boolean =>
  issue.number >= IMPLEMENTATION_ISSUE_MIN &&
  issue.number <= IMPLEMENTATION_ISSUE_MAX &&
  issue.title.startsWith("实现：") &&
  issue.labels.some(({ name }) => name === "ready-for-agent");

export const validateBlockerSnapshot = (issue: QueueIssue): void => {
  if (issue.blockedBy.nodes.length !== issue.blockedBy.totalCount) {
    throw new Error(
      `Issue #${issue.number} blocker data is incomplete ` +
        `(${issue.blockedBy.nodes.length}/${issue.blockedBy.totalCount}).`,
    );
  }
};

export const issueSemanticFingerprint = (issue: QueueIssue): string =>
  JSON.stringify({
    state: issue.state,
    title: issue.title,
    body: issue.body,
    assignees: issue.assignees.map(({ login }) => login).sort(),
    labels: issue.labels.map(({ name }) => name).sort(),
    blockedBy: issue.blockedBy.nodes
      .map(({ number, state }) => ({ number, state }))
      .sort((left, right) => left.number - right.number),
  });

export type SelectFrontierBatchOptions = {
  issues: readonly QueueIssue[];
  viewer: string;
  activeIssueNumbers: ReadonlySet<number>;
  limit: number;
};

export const selectFrontierBatch = ({
  issues,
  viewer,
  activeIssueNumbers,
  limit,
}: SelectFrontierBatchOptions): QueueIssue[] => {
  if (!Number.isInteger(limit) || limit < 1 || limit > 3) {
    throw new RangeError("Frontier batch limit must be an integer from 1 to 3.");
  }
  for (const issue of issues) validateBlockerSnapshot(issue);

  return [...issues]
    .filter(isImplementationIssue)
    .filter(({ state }) => state === "OPEN")
    .filter(({ blockedBy }) =>
      blockedBy.nodes.every(({ state }) => state === "CLOSED"),
    )
    .filter(({ assignees }) =>
      assignees.length === 0 ||
      (assignees.length === 1 && assignees[0]?.login === viewer),
    )
    .filter(({ number }) => !activeIssueNumbers.has(number))
    .sort((left, right) => left.number - right.number)
    .slice(0, limit);
};

export const selectNextFrontier = (
  issues: readonly QueueIssue[],
  viewer: string,
): QueueIssue | undefined =>
  selectFrontierBatch({
    issues,
    viewer,
    activeIssueNumbers: new Set(),
    limit: 1,
  })[0];

const sortAttempts = (attempts: QueueState[]): QueueState[] =>
  attempts.sort(
    (left, right) =>
      left.issueNumber - right.issueNumber ||
      left.attemptId.localeCompare(right.attemptId),
  );

export const upsertQueueAttempt = (
  snapshot: QueueSnapshot,
  attempt: QueueState,
): QueueSnapshot => ({
  version: 2,
  attempts: sortAttempts([
    ...snapshot.attempts.filter(
      ({ attemptId }) => attemptId !== attempt.attemptId,
    ),
    attempt,
  ]),
});

export const removeQueueAttempt = (
  snapshot: QueueSnapshot,
  attemptId: string,
): QueueSnapshot => ({
  version: 2,
  attempts: snapshot.attempts.filter(
    (attempt) => attempt.attemptId !== attemptId,
  ),
});

export const replaceQueueAttempt = (
  snapshot: QueueSnapshot,
  previousAttemptId: string,
  replacement: QueueState,
): QueueSnapshot => {
  const previous = snapshot.attempts.find(
    ({ attemptId }) => attemptId === previousAttemptId,
  );
  if (
    !previous ||
    previous.issueNumber !== replacement.issueNumber ||
    previousAttemptId === replacement.attemptId
  ) {
    throw new Error(
      `Cannot replace queue attempt ${previousAttemptId} with ${replacement.attemptId}.`,
    );
  }
  if (
    snapshot.attempts.some(
      ({ attemptId }) => attemptId === replacement.attemptId,
    )
  ) {
    throw new Error(`Queue attempt ${replacement.attemptId} already exists.`);
  }
  return upsertQueueAttempt(
    removeQueueAttempt(snapshot, previousAttemptId),
    replacement,
  );
};

export const summarizeQueue = (
  issues: readonly QueueIssue[],
  viewer: string,
) => {
  const implementationIssues = issues.filter(isImplementationIssue);
  return {
    total: implementationIssues.length,
    closed: implementationIssues.filter(({ state }) => state === "CLOSED")
      .length,
    open: implementationIssues.filter(({ state }) => state === "OPEN").length,
    assignedToViewer: implementationIssues.filter(
      ({ state, assignees }) =>
        state === "OPEN" &&
        assignees.length === 1 &&
        assignees[0]?.login === viewer,
    ).length,
    assignedElsewhere: implementationIssues.filter(
      ({ state, assignees }) =>
        state === "OPEN" &&
        assignees.some(({ login }) => login !== viewer),
    ).length,
    next: selectNextFrontier(implementationIssues, viewer)?.number ?? null,
  };
};

export const pullRequestDisposition = (
  pullRequest: PullRequestState,
): "WAIT" | "ACCEPT" | "STOP" => {
  if (pullRequest.mergedAt || pullRequest.state === "MERGED") return "ACCEPT";
  if (pullRequest.state === "OPEN") return "WAIT";
  return "STOP";
};
