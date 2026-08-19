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
  receiptPath: string;
  leaseRef: string;
  leaseSha: string;
  prNumber?: number;
  prUrl?: string;
  acceptedSha?: string;
  failedStage?: Exclude<QueueStage, "NEEDS_HUMAN">;
  resumeStage?: Exclude<QueueStage, "NEEDS_HUMAN">;
  error?: string;
  updatedAt: string;
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

export const selectNextFrontier = (
  issues: readonly QueueIssue[],
  viewer: string,
): QueueIssue | undefined => {
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
    .sort((left, right) => left.number - right.number)[0];
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
