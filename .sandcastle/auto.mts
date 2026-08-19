import { execFileSync, spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  HOST_RUNTIME_LOCK_CONFLICT_STATUS,
  boundedHostCommand,
  hostAgentRuntimeLockPath,
  requireHostCodexEnvironment,
  requireHostAgentIdle,
  requireHostCodexLogin,
  requireHostCodexModelAccess,
  requireSafeControllerEnvironment,
} from "./codex-host.mts";
import {
  IMPLEMENTATION_ISSUE_MAX,
  IMPLEMENTATION_ISSUE_MIN,
  issueSemanticFingerprint,
  pullRequestDisposition,
  selectNextFrontier,
  summarizeQueue,
  type PullRequestState,
  type QueueIssue,
  type QueueStage,
  type QueueState,
} from "./queue-core.mts";

const REPOSITORY = "15253a/meta_research";
const PARENT_SPEC_NUMBER = 112;
const DEFAULT_BASE_REF = "develop_main";
const DEFAULT_MODEL = "gpt-5.4";
const DEFAULT_POLL_SECONDS = 60;
const DEFAULT_AGENT_WALL_CLOCK_SECONDS = 8 * 60 * 60;
const MAX_TRANSIENT_FAILURES = 8;
const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const queueDir = join(repoRoot, ".sandcastle", "queue");
const statePath = join(queueDir, "state.json");
const liveStatusPath = join(repoRoot, ".sandcastle", "status.json");
const receiptDir = join(repoRoot, ".sandcastle", "receipts");
let activeState: QueueState | undefined;
let codexModelPreflightPassedForCurrentTicket = false;

type Receipt = {
  status: "READY_FOR_MERGE" | "NEEDS_HUMAN";
  acceptance: "PENDING";
  attemptId: string;
  issueNumber: number;
  branch: string;
  baseRef: string;
  baseSha: string;
  issueFingerprint: string;
  parentSpecFingerprint: string;
  commits: Array<{ sha: string }>;
  finishedAt: string;
  currentness?: {
    issueUnchanged: boolean;
    parentSpecUnchanged: boolean;
    baseUnchanged: boolean;
  };
};

class QueueFailure extends Error {
  constructor(message: string, readonly resumable: boolean) {
    super(message);
    this.name = "QueueFailure";
  }
}

type CandidateState = QueueState & { candidateSha: string };
type MergingState = CandidateState & { prNumber: number; prUrl: string };
type ClosingState = MergingState & { acceptedSha: string };

const requireCandidateState: (
  state: QueueState,
) => asserts state is CandidateState = (state) => {
  if (!state.candidateSha) {
    throw new QueueFailure(
      `Queue stage ${state.stage} is missing its verified candidate SHA.`,
      false,
    );
  }
};

const requireMergingState: (
  state: QueueState,
) => asserts state is MergingState = (state) => {
  requireCandidateState(state);
  if (!state.prNumber || !state.prUrl) {
    throw new QueueFailure(
      `Queue stage ${state.stage} is missing its pull request identity.`,
      false,
    );
  }
};

const requireClosingState: (
  state: QueueState,
) => asserts state is ClosingState = (state) => {
  requireMergingState(state);
  if (!state.acceptedSha) {
    throw new QueueFailure("Closing state is missing its accepted commit.", false);
  }
};

const runHost = (command: string, args: string[]): string =>
  execFileSync(command, args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();

const runHostRead = (command: string, args: string[]): string => {
  let lastError: unknown;
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    try {
      return runHost(command, args);
    } catch (error) {
      lastError = error;
      if (attempt < 3) {
        spawnSync("sleep", [String(2 ** (attempt - 1))], { stdio: "ignore" });
      }
    }
  }
  throw lastError;
};

const runVisible = (command: string, args: string[]): void => {
  const result = spawnSync(command, args, {
    cwd: repoRoot,
    env: process.env,
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(`${command} exited with status ${result.status ?? "unknown"}.`);
  }
};

const runStatus = (command: string, args: string[]): number =>
  spawnSync(command, args, {
    cwd: repoRoot,
    stdio: "ignore",
  }).status ?? 1;

const githubHttpStatus = (error: unknown): number | undefined => {
  if (!error || typeof error !== "object") return undefined;
  const candidate = error as { message?: unknown; stderr?: unknown };
  const stderr = Buffer.isBuffer(candidate.stderr)
    ? candidate.stderr.toString("utf8")
    : typeof candidate.stderr === "string"
      ? candidate.stderr
      : "";
  const text = `${typeof candidate.message === "string" ? candidate.message : ""}\n${stderr}`;
  const match = text.match(/\bHTTP\s+(\d{3})\b/i);
  return match ? Number(match[1]) : undefined;
};

const readRemoteRef = (ref: string): string | undefined => {
  const output = runHostRead("git", ["ls-remote", "--heads", "origin", ref]);
  if (!output) return undefined;
  const lines = output.split("\n").filter(Boolean);
  if (lines.length !== 1) throw new Error(`Remote ref ${ref} is ambiguous.`);
  const [sha, remoteRef, ...extra] = lines[0].split(/\s+/);
  if (!sha || remoteRef !== ref || extra.length > 0) {
    throw new Error(`Malformed remote ref response for ${ref}.`);
  }
  return sha;
};

const leaseRefForIssue = (issueNumber: number): string =>
  `refs/heads/sandcastle/lease-${issueNumber}`;

const ensureLease = (state: QueueState): void => {
  const expectedRef = leaseRefForIssue(state.issueNumber);
  if (state.leaseRef !== expectedRef) {
    throw new QueueFailure(`Invalid lease ref ${state.leaseRef}.`, false);
  }
  const existing = readRemoteRef(state.leaseRef);
  if (existing === state.leaseSha) return;
  if (existing) {
    throw new QueueFailure(
      `Issue #${state.issueNumber} is leased by another controller (${existing}).`,
      false,
    );
  }
  let pushFailure: unknown;
  try {
    runVisible("git", ["push", "origin", `${state.leaseSha}:${state.leaseRef}`]);
  } catch (error) {
    pushFailure = error;
  }
  const acquired = readRemoteRef(state.leaseRef);
  if (acquired !== state.leaseSha) {
    if (pushFailure) throw pushFailure;
    throw new Error(`Failed to acquire lease ${state.leaseRef}.`);
  }
};

const requireLeaseOwned = (state: QueueState): void => {
  const expectedRef = leaseRefForIssue(state.issueNumber);
  const existing = readRemoteRef(state.leaseRef);
  if (state.leaseRef !== expectedRef || existing !== state.leaseSha) {
    throw new QueueFailure(
      `Controller lease for issue #${state.issueNumber} is missing or changed.`,
      false,
    );
  }
};

const validateLeaseIdentity = (state: QueueState): void => {
  const commit = JSON.parse(
    runHostRead("gh", [
      "api",
      `repos/${REPOSITORY}/git/commits/${state.leaseSha}`,
    ]),
  ) as {
    message?: string;
    tree?: { sha?: string };
    parents?: Array<{ sha?: string }>;
  };
  const expectedMessage =
    `Sandcastle lease #${state.issueNumber} ${state.attemptId}`;
  const baseTree = runHost("git", ["rev-parse", `${state.baseSha}^{tree}`]);
  if (
    commit.message !== expectedMessage ||
    commit.tree?.sha !== baseTree ||
    commit.parents?.length !== 1 ||
    commit.parents[0]?.sha !== state.baseSha
  ) {
    throw new QueueFailure(
      `Lease ${state.leaseRef} is not bound to attempt ${state.attemptId}.`,
      false,
    );
  }
};

const releaseLease = (state: QueueState): void => {
  const existing = readRemoteRef(state.leaseRef);
  if (!existing) return;
  if (existing !== state.leaseSha) {
    throw new QueueFailure(
      `Refusing to release lease ${state.leaseRef}; owner changed to ${existing}.`,
      false,
    );
  }
  let deleteFailure: unknown;
  try {
    runVisible("git", [
      "push",
      `--force-with-lease=${state.leaseRef}:${state.leaseSha}`,
      "origin",
      `:${state.leaseRef}`,
    ]);
  } catch (error) {
    deleteFailure = error;
  }
  if (readRemoteRef(state.leaseRef)) {
    if (deleteFailure) throw deleteFailure;
    throw new Error(`Lease ${state.leaseRef} remained after release.`);
  }
};

const verifyAcceptedCommit = (acceptedSha: string): void => {
  try {
    requireHostAgentIdle(repoRoot);
  } catch (error) {
    throw new QueueFailure(
      error instanceof Error ? error.message : String(error),
      true,
    );
  }
  const verificationRoot = join(repoRoot, ".sandcastle", "inbox");
  mkdirSync(verificationRoot, { recursive: true, mode: 0o700 });
  const temporaryRoot = mkdtempSync(
    join(verificationRoot, `post-merge-${acceptedSha.slice(0, 12)}-`),
  );
  const archivePath = join(temporaryRoot, "candidate.tar");
  const workspacePath = join(temporaryRoot, "workspace");
  const isolatedHome = join(temporaryRoot, "home");
  mkdirSync(workspacePath, { mode: 0o700 });
  mkdirSync(isolatedHome, { mode: 0o700 });
  try {
    runHost("git", [
      "archive",
      "--format=tar",
      `--output=${archivePath}`,
      acceptedSha,
    ]);
    runVisible("tar", ["-xf", archivePath, "-C", workspacePath]);
    const verificationEnvironment: NodeJS.ProcessEnv = {
      ...process.env,
      HOME: isolatedHome,
    };
    for (const key of [
      "CODEX_HOME",
      "CODEX_API_KEY",
      "CODEX_ACCESS_TOKEN",
      "OPENAI_API_KEY",
      "GH_TOKEN",
      "GITHUB_TOKEN",
      "SSH_AUTH_SOCK",
    ]) {
      delete verificationEnvironment[key];
    }
    const result = spawnSync(
      "sh",
      [
        "-c",
        boundedHostCommand(
          "bash .sandcastle/verify-ticket.sh",
          1800,
          hostAgentRuntimeLockPath(repoRoot),
        ),
      ],
      {
        cwd: workspacePath,
        env: verificationEnvironment,
        stdio: "inherit",
      },
    );
    if (result.error) throw result.error;
    if (result.status === HOST_RUNTIME_LOCK_CONFLICT_STATUS) {
      throw new QueueFailure(
        "Another host Agent or verifier is still running; retry post-merge verification after it exits.",
        true,
      );
    }
    if (result.status !== 0) {
      throw new Error(
        `verification exited with status ${result.status ?? "unknown"}`,
      );
    }
  } catch (error) {
    if (error instanceof QueueFailure) throw error;
    throw new QueueFailure(
      `Post-merge verification failed for ${acceptedSha}: ${error instanceof Error ? error.message : String(error)}`,
      false,
    );
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
};

const readEnvironmentFile = (): Record<string, string> => {
  const path = join(repoRoot, ".sandcastle", ".env");
  if (!existsSync(path)) return {};
  return Object.fromEntries(
    readFileSync(path, "utf8")
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith("#") && line.includes("="))
      .map((line) => {
        const separator = line.indexOf("=");
        const key = line.slice(0, separator).trim();
        let value = line.slice(separator + 1).trim();
        if (
          value.length >= 2 &&
          ((value.startsWith('"') && value.endsWith('"')) ||
            (value.startsWith("'") && value.endsWith("'")))
        ) {
          value = value.slice(1, -1);
        }
        return [key, value];
      }),
  );
};

const controllerEnvironment = readEnvironmentFile();
const { values } = parseArgs({
  options: {
    "dry-run": { type: "boolean" },
    status: { type: "boolean" },
    watch: { type: "boolean" },
    once: { type: "boolean" },
    retry: { type: "boolean" },
    resume: { type: "boolean" },
    model: { type: "string" },
    "base-ref": { type: "string" },
    "poll-seconds": { type: "string" },
    "max-tickets": { type: "string" },
  },
  strict: true,
});

const dryRun = values["dry-run"] ?? false;
const statusOnly = values.status ?? false;
const watch = values.watch ?? false;
const once = values.once ?? false;
const retry = values.retry ?? false;
const resume = values.resume ?? false;
if (retry && resume) {
  throw new Error("Use only one of --retry or --resume.");
}
const model =
  (
    values.model ??
    process.env.SANDCASTLE_CODEX_MODEL ??
    controllerEnvironment.SANDCASTLE_CODEX_MODEL ??
    DEFAULT_MODEL
  ).trim() || DEFAULT_MODEL;
const baseRef =
  (
    values["base-ref"] ??
    process.env.SANDCASTLE_BASE_REF ??
    controllerEnvironment.SANDCASTLE_BASE_REF ??
    DEFAULT_BASE_REF
  ).trim() || DEFAULT_BASE_REF;
const pollSeconds = Number(
  values["poll-seconds"] ??
    process.env.SANDCASTLE_POLL_SECONDS ??
    controllerEnvironment.SANDCASTLE_POLL_SECONDS ??
    DEFAULT_POLL_SECONDS,
);
const maxTickets = Number(values["max-tickets"] ?? (once ? 1 : 20));
const agentWallClockSeconds = Number(
  process.env.SANDCASTLE_AGENT_WALL_CLOCK_SECONDS ??
    controllerEnvironment.SANDCASTLE_AGENT_WALL_CLOCK_SECONDS ??
    DEFAULT_AGENT_WALL_CLOCK_SECONDS,
);

if (!Number.isInteger(pollSeconds) || pollSeconds < 5 || pollSeconds > 3600) {
  throw new Error("--poll-seconds must be an integer between 5 and 3600.");
}
if (!Number.isInteger(maxTickets) || maxTickets < 1 || maxTickets > 20) {
  throw new Error("--max-tickets must be an integer between 1 and 20.");
}
if (
  !Number.isInteger(agentWallClockSeconds) ||
  agentWallClockSeconds < 3600 ||
  agentWallClockSeconds > 24 * 60 * 60
) {
  throw new Error(
    "SANDCASTLE_AGENT_WALL_CLOCK_SECONDS must be an integer from 3600 to 86400.",
  );
}

const ensureExecutionPrerequisites = (): void => {
  requireSafeControllerEnvironment(repoRoot);
  try {
    requireHostCodexEnvironment();
    requireHostCodexLogin(repoRoot);
    requireHostAgentIdle(repoRoot);
  } catch (error) {
    throw new QueueFailure(
      error instanceof Error ? error.message : String(error),
      true,
    );
  }
  if (!codexModelPreflightPassedForCurrentTicket) {
    // Online auth/model failures may be transient. Let the controller's
    // bounded retry policy distinguish them from local login-policy failures.
    requireHostCodexModelAccess(repoRoot, model);
    codexModelPreflightPassedForCurrentTicket = true;
  }
};

const fetchIssue = (number: number): QueueIssue =>
  JSON.parse(
    runHostRead("gh", [
      "issue",
      "view",
      String(number),
      "--repo",
      REPOSITORY,
      "--json",
      "number,title,body,state,assignees,labels,url,updatedAt,blockedBy",
    ]),
  ) as QueueIssue;

type IssueClosure = {
  state: "OPEN" | "CLOSED";
  comments: Array<{ body: string }>;
};

const fetchIssueClosure = (number: number): IssueClosure =>
  JSON.parse(
    runHostRead("gh", [
      "issue",
      "view",
      String(number),
      "--repo",
      REPOSITORY,
      "--json",
      "state,comments",
    ]),
  ) as IssueClosure;

const fetchImplementationIssues = (): QueueIssue[] => {
  const issueSelections: string[] = [];
  for (
    let number = IMPLEMENTATION_ISSUE_MIN;
    number <= IMPLEMENTATION_ISSUE_MAX;
    number += 1
  ) {
    issueSelections.push(`
      i${number}: issue(number: ${number}) {
        number
        title
        body
        state
        updatedAt
        url
        assignees(first: 10) { nodes { login } }
        labels(first: 100) { nodes { name } }
        blockedBy(first: 100) {
          totalCount
          nodes { number state title }
        }
      }
    `);
  }
  const query = `
    query SandcastleImplementationQueue {
      repository(owner: "15253a", name: "meta_research") {
        ${issueSelections.join("\n")}
      }
    }
  `;
  type GraphQlIssue = Omit<QueueIssue, "assignees" | "labels"> & {
    assignees: { nodes: QueueIssue["assignees"] };
    labels: { nodes: QueueIssue["labels"] };
  };
  const response = JSON.parse(
    runHostRead("gh", ["api", "graphql", "-f", `query=${query}`]),
  ) as { data?: { repository?: Record<string, GraphQlIssue | null> } };
  const repository = response.data?.repository;
  if (!repository) throw new Error("GitHub returned no repository queue data.");
  const issues = Object.values(repository).map((issue) => {
    if (!issue) throw new Error("GitHub returned a missing implementation issue.");
    return {
      ...issue,
      assignees: issue.assignees.nodes,
      labels: issue.labels.nodes,
    };
  });
  if (issues.length !== IMPLEMENTATION_ISSUE_MAX - IMPLEMENTATION_ISSUE_MIN + 1) {
    throw new Error(`GitHub returned ${issues.length} implementation issues.`);
  }
  return issues;
};

const readState = (): QueueState | undefined => {
  if (!existsSync(statePath)) return undefined;
  const state = JSON.parse(readFileSync(statePath, "utf8")) as QueueState;
  if (state.version !== 1) throw new Error("Unsupported queue state version.");
  return state;
};

const writeState = (state: QueueState): void => {
  mkdirSync(queueDir, { recursive: true });
  const temporaryPath = `${statePath}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporaryPath, statePath);
};

const persistState = (state: QueueState): QueueState => {
  writeState(state);
  activeState = state;
  return state;
};

const clearState = (): void => {
  if (existsSync(statePath)) unlinkSync(statePath);
  activeState = undefined;
};

const writeLiveStatus = (
  phase: string,
  details: Record<string, unknown> = {},
): void => {
  const temporaryPath = `${liveStatusPath}.tmp`;
  writeFileSync(
    temporaryPath,
    `${JSON.stringify(
      {
        phase,
        repository: REPOSITORY,
        baseRef,
        ...details,
        updatedAt: new Date().toISOString(),
      },
      null,
      2,
    )}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  renameSync(temporaryPath, liveStatusPath);
};

const readLiveStatus = (): unknown => {
  if (!existsSync(liveStatusPath)) return null;
  try {
    return JSON.parse(readFileSync(liveStatusPath, "utf8"));
  } catch {
    return { phase: "UNKNOWN", error: "status.json is unreadable" };
  }
};

const readReceipts = (): Array<{ path: string; receipt: Receipt }> => {
  if (!existsSync(receiptDir)) return [];
  const receipts: Array<{ path: string; receipt: Receipt }> = [];
  for (const name of readdirSync(receiptDir).filter((entry) => entry.endsWith(".json"))) {
    const path = join(receiptDir, name);
    try {
      const receipt = JSON.parse(readFileSync(path, "utf8")) as Receipt;
      if (
        typeof receipt.finishedAt !== "string" ||
        typeof receipt.branch !== "string" ||
        typeof receipt.attemptId !== "string" ||
        !Array.isArray(receipt.commits)
      ) {
        continue;
      }
      receipts.push({
        path,
        receipt,
      });
    } catch {
      // A partially written or manually edited receipt is never recoverable.
    }
  }
  return receipts.sort((left, right) =>
    right.receipt.finishedAt.localeCompare(left.receipt.finishedAt),
  );
};

const findReceiptForBranch = (branch: string) =>
  readReceipts().find(({ receipt }) => receipt.branch === branch);

const readReceiptForState = (state: QueueState): Receipt => {
  requireCandidateState(state);
  const path = resolve(state.receiptPath);
  if (dirname(path) !== receiptDir || !existsSync(path)) {
    throw new Error(`Queue receipt is outside ${receiptDir} or missing.`);
  }
  const receipt = JSON.parse(readFileSync(path, "utf8")) as Receipt;
  const candidateSha = receipt.commits.at(-1)?.sha;
  if (
    receipt.status !== "READY_FOR_MERGE" ||
    receipt.acceptance !== "PENDING" ||
    receipt.issueNumber !== state.issueNumber ||
    receipt.branch !== state.branch ||
    receipt.baseRef !== state.baseRef ||
    receipt.baseSha !== state.baseSha ||
    candidateSha !== state.candidateSha ||
    receipt.currentness?.issueUnchanged !== true ||
    receipt.currentness.parentSpecUnchanged !== true ||
    receipt.currentness.baseUnchanged !== true
  ) {
    throw new Error(`Queue state does not match verified receipt ${path}.`);
  }
  return receipt;
};

const assertCandidateRequirementsCurrent = (
  state: QueueState,
  requireFrozenBase = true,
): void => {
  const receipt = readReceiptForState(state);
  const currentIssue = fetchIssue(state.issueNumber);
  const currentParentSpec = fetchIssue(PARENT_SPEC_NUMBER);
  if (currentIssue.state !== "OPEN") {
    throw new QueueFailure(
      `Issue #${state.issueNumber} was closed outside the accepted PR flow.`,
      false,
    );
  }
  if (
    receipt.issueFingerprint !== issueSemanticFingerprint(currentIssue) ||
    receipt.parentSpecFingerprint !== issueSemanticFingerprint(currentParentSpec)
  ) {
    throw new QueueFailure(
      `Issue #${state.issueNumber} or parent spec #${PARENT_SPEC_NUMBER} changed after implementation.`,
      false,
    );
  }
  if (
    requireFrozenBase &&
    readRemoteRef(`refs/heads/${state.baseRef}`) !== state.baseSha
  ) {
    throw new QueueFailure(
      `${state.baseRef} moved after candidate verification; start a fresh implementation attempt.`,
      false,
    );
  }
};

const fetchPullRequest = (number: number): PullRequestState =>
  JSON.parse(
    runHostRead("gh", [
      "pr",
      "view",
      String(number),
      "--repo",
      REPOSITORY,
      "--json",
      "number,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,mergeable,mergeStateStatus,mergedAt,mergeCommit",
    ]),
  ) as PullRequestState;

const findPullRequestForBranch = (
  branch: string,
): PullRequestState | undefined => {
  const pullRequests = JSON.parse(
    runHostRead("gh", [
      "pr",
      "list",
      "--repo",
      REPOSITORY,
      "--state",
      "all",
      "--head",
      branch,
      "--limit",
      "10",
      "--json",
      "number,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,mergeable,mergeStateStatus,mergedAt,mergeCommit",
    ]),
  ) as PullRequestState[];
  return pullRequests.find(
    (pullRequest) =>
      pullRequest.headRefName === branch && pullRequest.baseRefName === baseRef,
  );
};

const recoverOpenPullRequest = (): QueueState | undefined => {
  const pullRequests = JSON.parse(
    runHostRead("gh", [
      "pr",
      "list",
      "--repo",
      REPOSITORY,
      "--state",
      "open",
      "--base",
      baseRef,
      "--limit",
      "100",
      "--json",
      "number,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,mergeable,mergeStateStatus,mergedAt,mergeCommit",
    ]),
  ) as PullRequestState[];
  const automationPullRequests = pullRequests.filter(({ headRefName }) =>
    /^codex\/issue-(?:11[3-9]|12\d|13[0-2])-/.test(headRefName),
  );
  if (automationPullRequests.length > 1) {
    throw new Error(
      `Multiple open automation PRs target ${baseRef}: ` +
        automationPullRequests.map(({ number }) => `#${number}`).join(", "),
    );
  }
  const pullRequest = automationPullRequests[0];
  if (!pullRequest) return undefined;
  const recovered = findReceiptForBranch(pullRequest.headRefName);
  if (
    !recovered ||
    recovered.receipt.status !== "READY_FOR_MERGE" ||
    recovered.receipt.acceptance !== "PENDING" ||
    recovered.receipt.baseRef !== baseRef ||
    recovered.receipt.currentness?.issueUnchanged !== true ||
    recovered.receipt.currentness.parentSpecUnchanged !== true ||
    recovered.receipt.currentness.baseUnchanged !== true
  ) {
    throw new Error(
      `PR #${pullRequest.number} has no matching READY_FOR_MERGE receipt.`,
    );
  }
  const candidateSha = recovered.receipt.commits.at(-1)?.sha;
  if (!candidateSha || pullRequest.headRefOid !== candidateSha) {
    throw new Error(
      `PR #${pullRequest.number} does not match its verified receipt commit.`,
    );
  }
  const currentIssue = fetchIssue(recovered.receipt.issueNumber);
  const currentParentSpec = fetchIssue(PARENT_SPEC_NUMBER);
  if (
    recovered.receipt.issueFingerprint !==
      issueSemanticFingerprint(currentIssue) ||
    recovered.receipt.parentSpecFingerprint !==
      issueSemanticFingerprint(currentParentSpec)
  ) {
    throw new Error(
      `PR #${pullRequest.number} requirements changed after implementation.`,
    );
  }
  const leaseRef = leaseRefForIssue(recovered.receipt.issueNumber);
  const leaseSha = readRemoteRef(leaseRef);
  if (!leaseSha) {
    throw new Error(
      `PR #${pullRequest.number} has no active controller lease ${leaseRef}.`,
    );
  }
  const recoveredState: QueueState = {
    version: 1,
    stage: "MERGING",
    issueNumber: recovered.receipt.issueNumber,
    issueTitle: currentIssue.title,
    branch: recovered.receipt.branch,
    baseRef: recovered.receipt.baseRef,
    baseSha: recovered.receipt.baseSha,
    candidateSha,
    attemptId: recovered.receipt.attemptId,
    receiptPath: recovered.path,
    leaseRef,
    leaseSha,
    prNumber: pullRequest.number,
    prUrl: pullRequest.url,
    updatedAt: new Date().toISOString(),
  };
  requireLeaseOwned(recoveredState);
  validateLeaseIdentity(recoveredState);
  return recoveredState;
};

const remoteTrackingRef = `refs/remotes/origin/${baseRef}`;

const syncBase = (): { sha: string; changed: boolean } => {
  const dirty = runHost("git", ["status", "--porcelain=v1", "--untracked-files=all"]);
  if (dirty) {
    throw new QueueFailure(
      "The controller worktree must be clean before autopilot runs.",
      false,
    );
  }
  const currentBranch = runHost("git", ["branch", "--show-current"]);
  if (currentBranch !== baseRef) {
    throw new QueueFailure(
      `Check out ${baseRef}; current branch is ${currentBranch || "detached"}.`,
      false,
    );
  }
  runHost("git", [
    "fetch",
    "origin",
    `refs/heads/${baseRef}:${remoteTrackingRef}`,
  ]);
  const localSha = runHost("git", ["rev-parse", "HEAD"]);
  const remoteSha = runHost("git", ["rev-parse", remoteTrackingRef]);
  let changed = false;
  if (localSha !== remoteSha) {
    if (runStatus("git", ["merge-base", "--is-ancestor", localSha, remoteSha]) !== 0) {
      throw new QueueFailure(
        `${baseRef} diverged from origin/${baseRef}; reconcile it manually.`,
        false,
      );
    }
    runVisible("git", ["merge", "--ff-only", remoteTrackingRef]);
    changed = true;
  }
  return { sha: runHost("git", ["rev-parse", "HEAD"]), changed };
};

const reexecAfterBaseChange = (changed: boolean): void => {
  if (!changed) return;
  console.log(`[RESTART] ${baseRef} advanced; reloading the committed controller.`);
  const result = spawnSync(
    join(repoRoot, "node_modules", ".bin", "tsx"),
    [".sandcastle/auto.mts", ...process.argv.slice(2)],
    { cwd: repoRoot, env: process.env, stdio: "inherit" },
  );
  if (result.error) throw result.error;
  process.exit(result.status ?? 1);
};

const createAttemptState = (
  issue: QueueIssue,
  currentBaseSha: string,
): QueueState => {
  const attemptId = `${new Date()
    .toISOString()
    .replaceAll(/[-:.]/g, "")}-${randomUUID().slice(0, 8)}`;
  const branch = `codex/issue-${issue.number}-${attemptId.toLowerCase()}`;
  const leaseRef = leaseRefForIssue(issue.number);
  const leaseSha = runHost("git", [
    "commit-tree",
    `${currentBaseSha}^{tree}`,
    "-p",
    currentBaseSha,
    "-m",
    `Sandcastle lease #${issue.number} ${attemptId}`,
  ]);
  return {
    version: 1,
    stage: "CLAIMING",
    issueNumber: issue.number,
    issueTitle: issue.title,
    branch,
    baseRef,
    baseSha: currentBaseSha,
    attemptId,
    receiptPath: join(
      receiptDir,
      `${attemptId}-issue-${issue.number}.json`,
    ),
    leaseRef,
    leaseSha,
    updatedAt: new Date().toISOString(),
  };
};

const claimIssue = (candidate: QueueIssue, viewer: string): QueueIssue => {
  const current = fetchIssue(candidate.number);
  if (!selectNextFrontier([current], viewer)) {
    throw new QueueFailure(
      `Issue #${candidate.number} left the native frontier before claim.`,
      false,
    );
  }
  const assignees = current.assignees.map(({ login }) => login);
  let claimFailure: unknown;
  if (assignees.length === 0) {
    writeLiveStatus("CLAIMING", {
      issueNumber: current.number,
      issueTitle: current.title,
    });
    console.log(`[CLAIM] Assigning issue #${current.number} to ${viewer}.`);
    try {
      runVisible("gh", [
        "issue",
        "edit",
        String(current.number),
        "--repo",
        REPOSITORY,
        "--add-assignee",
        viewer,
      ]);
    } catch (error) {
      claimFailure = error;
    }
  }
  const claimed = fetchIssue(candidate.number);
  const claimedAssignees = claimed.assignees.map(({ login }) => login);
  if (claimedAssignees.length !== 1 || claimedAssignees[0] !== viewer) {
    if (claimFailure) throw claimFailure;
    throw new QueueFailure(
      `Issue #${candidate.number} claim changed concurrently: ` +
        `${claimedAssignees.join(", ") || "unassigned"}.`,
      false,
    );
  }
  if (!selectNextFrontier([claimed], viewer)) {
    throw new QueueFailure(
      `Issue #${candidate.number} left the native frontier after claim.`,
      false,
    );
  }
  return claimed;
};

const runTicket = (
  issue: QueueIssue,
  state: QueueState,
): { path: string; receipt: Receipt } => {
  if (existsSync(state.receiptPath)) {
    throw new QueueFailure(
      `Attempt receipt already exists: ${state.receiptPath}.`,
      false,
    );
  }
  writeLiveStatus("IMPLEMENTING", {
    issueNumber: issue.number,
    issueTitle: issue.title,
    attemptId: state.attemptId,
    branch: state.branch,
    model,
  });
  console.log(`[IMPLEMENT] Starting issue #${issue.number}: ${issue.title}`);
  const result = spawnSync(
    "setpriv",
    [
      "--pdeathsig",
      "TERM",
      join(repoRoot, "node_modules", ".bin", "tsx"),
      ".sandcastle/main.mts",
      "--issue",
      String(issue.number),
      "--base-ref",
      baseRef,
      "--model",
      model,
      "--attempt-id",
      state.attemptId,
      "--agent-wall-clock-seconds",
      String(agentWallClockSeconds),
    ],
    { cwd: repoRoot, env: process.env, stdio: "inherit" },
  );
  if (result.error) throw result.error;
  let created: { path: string; receipt: Receipt } | undefined;
  if (existsSync(state.receiptPath)) {
    try {
      created = {
        path: state.receiptPath,
        receipt: JSON.parse(readFileSync(state.receiptPath, "utf8")) as Receipt,
      };
    } catch {
      // The actionable error below preserves the exact attempt checkpoint.
    }
  }
  if (!created) {
    throw new QueueFailure(
      `Issue #${issue.number} exited with status ${result.status ?? "unknown"} without a receipt.`,
      false,
    );
  }
  if (result.status !== 0 || created.receipt.status !== "READY_FOR_MERGE") {
    throw new QueueFailure(
      `Issue #${issue.number} needs human attention; receipt: ${relative(repoRoot, created.path)}.`,
      false,
    );
  }
  return created;
};

const stateFromReceipt = (
  state: QueueState,
  recovered: { path: string; receipt: Receipt },
): QueueState => {
  const candidateSha = recovered.receipt.commits.at(-1)?.sha;
  if (
    recovered.path !== state.receiptPath ||
    recovered.receipt.status !== "READY_FOR_MERGE" ||
    recovered.receipt.issueNumber !== state.issueNumber ||
    recovered.receipt.branch !== state.branch ||
    recovered.receipt.baseRef !== state.baseRef ||
    recovered.receipt.baseSha !== state.baseSha ||
    recovered.receipt.attemptId !== state.attemptId ||
    !candidateSha
  ) {
    throw new QueueFailure(
      `Receipt ${recovered.path} does not match attempt ${state.attemptId}.`,
      false,
    );
  }
  return {
    ...state,
    stage: "PUBLISHING",
    candidateSha,
    error: undefined,
    failedStage: undefined,
    resumeStage: undefined,
    updatedAt: new Date().toISOString(),
  };
};

const worktreePathForBranch = (branch: string): string | undefined => {
  const matches = runHost("git", ["worktree", "list", "--porcelain"])
    .split("\n\n")
    .map((record) => record.split("\n"))
    .filter((lines) => lines.includes(`branch refs/heads/${branch}`))
    .map((lines) => lines.find((line) => line.startsWith("worktree ")))
    .filter((line): line is string => line !== undefined)
    .map((line) => line.slice("worktree ".length));
  if (matches.length > 1) {
    throw new QueueFailure(
      `Branch ${branch} is linked to multiple worktrees.`,
      false,
    );
  }
  return matches[0];
};

const retryInterruptedImplementation = (state: QueueState): void => {
  if (
    state.stage !== "NEEDS_HUMAN" ||
    state.failedStage !== "IMPLEMENTING" ||
    state.prNumber ||
    state.acceptedSha
  ) {
    throw new QueueFailure(
      "--retry is only supported for an interrupted implementation before PR creation.",
      false,
    );
  }
  try {
    requireHostAgentIdle(repoRoot);
  } catch (error) {
    throw new QueueFailure(
      error instanceof Error ? error.message : String(error),
      false,
    );
  }
  const leaseBeforeRetry = readRemoteRef(state.leaseRef);
  if (leaseBeforeRetry && leaseBeforeRetry !== state.leaseSha) {
    throw new QueueFailure(
      `Controller lease for issue #${state.issueNumber} changed before retry.`,
      false,
    );
  }
  const worktreePath = worktreePathForBranch(state.branch);
  if (!leaseBeforeRetry && worktreePath) {
    throw new QueueFailure(
      `Cannot remove preserved worktree ${worktreePath} after its controller lease disappeared.`,
      false,
    );
  }
  if (leaseBeforeRetry) validateLeaseIdentity(state);
  if (worktreePath) {
    const managedWorktreeRoot = resolve(repoRoot, ".sandcastle", "worktrees");
    const resolvedWorktreePath = resolve(worktreePath);
    if (!resolvedWorktreePath.startsWith(`${managedWorktreeRoot}${sep}`)) {
      throw new QueueFailure(
        `Refusing to remove non-Sandcastle worktree ${resolvedWorktreePath}.`,
        false,
      );
    }
    const dirty = runHost("git", [
      "-C",
      resolvedWorktreePath,
      "status",
      "--porcelain=v1",
      "--untracked-files=all",
    ]);
    if (dirty) {
      throw new QueueFailure(
        `Preserved worktree ${resolvedWorktreePath} has uncommitted evidence. ` +
          "Commit or archive it before --retry.",
        false,
      );
    }
    console.log(
      `[RETRY] Removing clean preserved worktree ${resolvedWorktreePath}; branch ${state.branch} is retained for evidence.`,
    );
    runVisible("git", ["worktree", "remove", "--force", resolvedWorktreePath]);
  }
  if (worktreePathForBranch(state.branch)) {
    throw new QueueFailure(
      `Preserved worktree for ${state.branch} remained after retry cleanup.`,
      false,
    );
  }
  if (leaseBeforeRetry) releaseLease(state);
  clearState();
  writeLiveStatus("RETRYING_IMPLEMENTATION", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    previousAttemptId: state.attemptId,
    preservedBranch: state.branch,
  });
};

const reconcileAttemptState = (
  state: QueueState,
  viewer: string,
): QueueState => {
  if (
    state.stage === "CLAIMING" ||
    (state.stage === "IMPLEMENTING" && !existsSync(state.receiptPath))
  ) {
    // A recovered attempt that still needs an Agent must pass the complete
    // login/model gate before ensureLease can write a remote ref.
    ensureExecutionPrerequisites();
  }
  ensureLease(state);
  requireLeaseOwned(state);
  validateLeaseIdentity(state);
  if (runHost("git", ["rev-parse", "HEAD"]) !== state.baseSha) {
    throw new QueueFailure(
      `Attempt ${state.attemptId} was checkpointed on ${state.baseSha}, but ${baseRef} moved.`,
      false,
    );
  }
  let currentState = state;
  let issue = fetchIssue(state.issueNumber);
  if (currentState.stage === "CLAIMING") {
    issue = claimIssue(issue, viewer);
    currentState = persistState({
      ...currentState,
      stage: "IMPLEMENTING",
      issueTitle: issue.title,
      updatedAt: new Date().toISOString(),
    });
  }
  if (currentState.stage !== "IMPLEMENTING") return currentState;

  if (existsSync(currentState.receiptPath)) {
    let recovered: { path: string; receipt: Receipt };
    try {
      recovered = {
        path: currentState.receiptPath,
        receipt: JSON.parse(
          readFileSync(currentState.receiptPath, "utf8"),
        ) as Receipt,
      };
    } catch {
      throw new QueueFailure(
        `Attempt receipt is unreadable: ${currentState.receiptPath}.`,
        false,
      );
    }
    if (recovered.receipt.status !== "READY_FOR_MERGE") {
      throw new QueueFailure(
        `Attempt ${state.attemptId} requires human inspection: ${relative(repoRoot, currentState.receiptPath)}.`,
        false,
      );
    }
    return persistState(stateFromReceipt(currentState, recovered));
  }

  const branchExists =
    runStatus("git", [
      "show-ref",
      "--verify",
      `refs/heads/${currentState.branch}`,
    ]) === 0;
  const worktreeExists = worktreePathForBranch(currentState.branch) !== undefined;
  if (branchExists || worktreeExists) {
    throw new QueueFailure(
      `Attempt ${currentState.attemptId} was interrupted with a preserved branch/worktree; inspect it before retrying.`,
      false,
    );
  }

  ensureExecutionPrerequisites();
  const result = runTicket(issue, currentState);
  return persistState(stateFromReceipt(currentState, result));
};

const publishCandidate = (state: QueueState): QueueState => {
  requireCandidateState(state);
  requireLeaseOwned(state);
  assertCandidateRequirementsCurrent(state);
  writeLiveStatus("PUBLISHING", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    branch: state.branch,
    attemptId: state.attemptId,
  });
  const localCandidateSha = runHost("git", ["rev-parse", `${state.branch}^{commit}`]);
  if (localCandidateSha !== state.candidateSha) {
    throw new QueueFailure(
      `${state.branch} moved from verified ${state.candidateSha} to ${localCandidateSha}.`,
      false,
    );
  }
  console.log(`[PUBLISH] Pushing ${state.branch}.`);
  let pushFailure: unknown;
  try {
    runVisible("git", ["push", "--set-upstream", "origin", state.branch]);
  } catch (error) {
    pushFailure = error;
  }
  const remoteCandidate = runHost("git", [
    "ls-remote",
    "--heads",
    "origin",
    state.branch,
  ]).split(/\s+/)[0];
  if (remoteCandidate !== state.candidateSha) {
    if (pushFailure) throw pushFailure;
    throw new QueueFailure(
      `Remote ${state.branch} does not match verified ${state.candidateSha}.`,
      false,
    );
  }
  let pullRequest = findPullRequestForBranch(state.branch);
  if (!pullRequest) {
    const body = [
      `Automated Sandcastle candidate for #${state.issueNumber}.`,
      "",
      `- Attempt: \`${state.attemptId}\``,
      `- Base: \`${state.baseRef}@${state.baseSha}\``,
      `- Receipt: \`${relative(repoRoot, state.receiptPath)}\``,
      "- Matt $implement (tdd + code-review) and fixed verification: passed",
      "",
      `The controller will merge this PR, close #${state.issueNumber}, and continue automatically.`,
    ].join("\n");
    let createFailure: unknown;
    try {
      const url = runHost("gh", [
        "pr",
        "create",
        "--repo",
        REPOSITORY,
        "--base",
        state.baseRef,
        "--head",
        state.branch,
        "--title",
        `#${state.issueNumber} ${state.issueTitle}`,
        "--body",
        body,
      ]);
      pullRequest = JSON.parse(
        runHostRead("gh", [
          "pr",
          "view",
          url,
          "--repo",
          REPOSITORY,
          "--json",
          "number,url,state,isDraft,baseRefName,baseRefOid,headRefName,headRefOid,mergeable,mergeStateStatus,mergedAt,mergeCommit",
        ]),
      ) as PullRequestState;
    } catch (error) {
      createFailure = error;
      pullRequest = findPullRequestForBranch(state.branch);
    }
    if (!pullRequest) {
      if (createFailure) throw createFailure;
      throw new Error(`Failed to create or recover a PR for ${state.branch}.`);
    }
  }
  if (pullRequest.headRefOid !== state.candidateSha) {
    throw new QueueFailure(
      `PR #${pullRequest.number} head ${pullRequest.headRefOid} does not match ` +
        `verified ${state.candidateSha}.`,
      false,
    );
  }
  const merging: QueueState = {
    ...state,
    stage: "MERGING",
    prNumber: pullRequest.number,
    prUrl: pullRequest.url,
    updatedAt: new Date().toISOString(),
  };
  persistState(merging);
  writeLiveStatus("MERGING", {
    issueNumber: merging.issueNumber,
    issueTitle: merging.issueTitle,
    branch: merging.branch,
    prNumber: merging.prNumber,
    prUrl: merging.prUrl,
    attemptId: merging.attemptId,
  });
  console.log(`[MERGING] PR #${pullRequest.number}: ${pullRequest.url}`);
  return merging;
};

const acceptMergedPullRequest = (
  state: QueueState,
  pullRequest: PullRequestState,
): ClosingState => {
  requireMergingState(state);
  assertCandidateRequirementsCurrent(state);
  if (
    pullRequest.baseRefName !== state.baseRef ||
    pullRequest.headRefName !== state.branch
  ) {
    throw new Error(`PR #${pullRequest.number} no longer matches the queue state.`);
  }
  const acceptedSha = pullRequest.mergeCommit?.oid;
  if (!acceptedSha) throw new Error(`PR #${pullRequest.number} has no merge commit.`);
  const mergeCommit = JSON.parse(
    runHostRead("gh", [
      "api",
      `repos/${REPOSITORY}/git/commits/${acceptedSha}`,
    ]),
  ) as { parents?: Array<{ sha?: string }> };
  if (
    mergeCommit.parents?.length !== 2 ||
    mergeCommit.parents[0]?.sha !== state.baseSha ||
    mergeCommit.parents[1]?.sha !== state.candidateSha
  ) {
    throw new QueueFailure(
      `PR #${pullRequest.number} merge commit parents do not match the frozen base and candidate.`,
      false,
    );
  }
  reexecAfterBaseChange(syncBase().changed);
  if (runStatus("git", ["merge-base", "--is-ancestor", acceptedSha, "HEAD"]) !== 0) {
    throw new Error(
      `PR #${pullRequest.number} merge commit is not reachable from ${baseRef}.`,
    );
  }
  writeLiveStatus("POST_MERGE_VERIFY", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    branch: state.branch,
    prNumber: pullRequest.number,
    prUrl: pullRequest.url,
    acceptedSha,
  });
  verifyAcceptedCommit(acceptedSha);
  assertCandidateRequirementsCurrent(state, false);
  const closing: ClosingState = {
    ...state,
    stage: "CLOSING",
    acceptedSha,
    updatedAt: new Date().toISOString(),
  };
  persistState(closing);
  return closing;
};

const finalizeClosingState = (state: QueueState): void => {
  requireClosingState(state);
  const leaseBeforeClose = readRemoteRef(state.leaseRef);
  if (leaseBeforeClose && leaseBeforeClose !== state.leaseSha) {
    throw new QueueFailure(
      `Controller lease for issue #${state.issueNumber} changed before close.`,
      false,
    );
  }
  if (leaseBeforeClose) validateLeaseIdentity(state);
  const pullRequest = fetchPullRequest(state.prNumber);
  if (
    pullRequestDisposition(pullRequest) !== "ACCEPT" ||
    pullRequest.baseRefName !== state.baseRef ||
    pullRequest.headRefName !== state.branch ||
    pullRequest.headRefOid !== state.candidateSha ||
    pullRequest.mergeCommit?.oid !== state.acceptedSha
  ) {
    throw new QueueFailure(
      `PR #${state.prNumber} no longer matches the accepted closing checkpoint.`,
      false,
    );
  }
  reexecAfterBaseChange(syncBase().changed);
  if (
    runStatus("git", [
      "merge-base",
      "--is-ancestor",
      state.acceptedSha,
      "HEAD",
    ]) !== 0
  ) {
    throw new Error(
      `Accepted commit ${state.acceptedSha} is not reachable from ${baseRef}.`,
    );
  }
  const marker =
    `<!-- sandcastle-acceptance:${state.attemptId}:pr-${state.prNumber}:${state.acceptedSha} -->`;
  let closure = fetchIssueClosure(state.issueNumber);
  const markerExists = closure.comments.some(({ body }) => body.includes(marker));
  if (closure.state === "CLOSED" && !markerExists) {
    throw new QueueFailure(
      `Issue #${state.issueNumber} was closed without this Sandcastle acceptance receipt.`,
      false,
    );
  }
  if (closure.state === "OPEN") {
    if (leaseBeforeClose !== state.leaseSha) {
      throw new QueueFailure(
        `Cannot close issue #${state.issueNumber} without its controller lease.`,
        false,
      );
    }
    assertCandidateRequirementsCurrent(state, false);
    const closeArgs = [
      "issue",
      "close",
      String(state.issueNumber),
      "--repo",
      REPOSITORY,
      "--reason",
      "completed",
    ];
    if (!markerExists) {
      closeArgs.push(
        "--comment",
        [
          "## Resolution",
          "",
          `Accepted by automated merge of PR #${state.prNumber}.`,
          `Accepted commit: \`${state.acceptedSha}\`.`,
          `Sandcastle attempt: \`${state.attemptId}\`.`,
          "",
          marker,
        ].join("\n"),
      );
    }
    let closeFailure: unknown;
    try {
      runVisible("gh", closeArgs);
    } catch (error) {
      closeFailure = error;
    }
    closure = fetchIssueClosure(state.issueNumber);
    const accepted = closure.comments.some(({ body }) => body.includes(marker));
    if (closure.state !== "CLOSED" || !accepted) {
      if (closeFailure) throw closeFailure;
      throw new Error(
        `Issue #${state.issueNumber} did not retain its acceptance receipt.`,
      );
    }
  }
  releaseLease(state);
  clearState();
  writeLiveStatus("ACCEPTED", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    branch: state.branch,
    prNumber: state.prNumber,
    prUrl: state.prUrl,
    acceptedSha: state.acceptedSha,
  });
  console.log(`[ACCEPTED] Issue #${state.issueNumber} merged and closed.`);
};

const mergePullRequest = (state: QueueState): void => {
  requireMergingState(state);
  requireLeaseOwned(state);
  validateLeaseIdentity(state);
  assertCandidateRequirementsCurrent(state);

  let pullRequest = fetchPullRequest(state.prNumber);
  const assertExactCandidate = (): void => {
    if (
      pullRequest.baseRefName !== state.baseRef ||
      pullRequest.headRefName !== state.branch ||
      pullRequest.headRefOid !== state.candidateSha
    ) {
      throw new QueueFailure(
        `PR #${pullRequest.number} no longer matches verified candidate ${state.candidateSha}.`,
        false,
      );
    }
  };
  const assertCleanMergeState = (): void => {
    if (pullRequest.isDraft) {
      throw new QueueFailure(
        `PR #${pullRequest.number} is a draft and cannot be merged automatically.`,
        true,
      );
    }
    if (pullRequest.baseRefOid !== state.baseSha) {
      throw new QueueFailure(
        `PR #${pullRequest.number} base moved from ${state.baseSha} to ${pullRequest.baseRefOid} before merge.`,
        false,
      );
    }
    if (
      pullRequest.mergeable === "UNKNOWN" ||
      pullRequest.mergeStateStatus === "UNKNOWN"
    ) {
      throw new Error(
        `GitHub has not computed mergeability for PR #${pullRequest.number} yet.`,
      );
    }
    if (
      pullRequest.mergeable !== "MERGEABLE" ||
      pullRequest.mergeStateStatus !== "CLEAN"
    ) {
      throw new QueueFailure(
        `PR #${pullRequest.number} is not cleanly mergeable ` +
          `(mergeable=${pullRequest.mergeable}, state=${pullRequest.mergeStateStatus}).`,
        ["BLOCKED", "DRAFT", "HAS_HOOKS", "UNSTABLE"].includes(
          pullRequest.mergeStateStatus,
        ),
      );
    }
  };
  assertExactCandidate();

  let disposition = pullRequestDisposition(pullRequest);
  if (disposition === "ACCEPT") {
    const closing = acceptMergedPullRequest(state, pullRequest);
    finalizeClosingState(closing);
    return;
  }
  if (disposition === "STOP") {
    throw new QueueFailure(
      `PR #${pullRequest.number} was closed without merge; queue state was preserved.`,
      false,
    );
  }
  assertCleanMergeState();

  writeLiveStatus("AUTO_MERGING", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    branch: state.branch,
    prNumber: state.prNumber,
    prUrl: state.prUrl,
    candidateSha: state.candidateSha,
    attemptId: state.attemptId,
  });
  console.log(`[AUTO_MERGING] PR #${pullRequest.number}: ${pullRequest.url}`);

  let mergeFailure: unknown;
  try {
    runHost("gh", [
      "api",
      "--method",
      "PUT",
      `repos/${REPOSITORY}/pulls/${pullRequest.number}/merge`,
      "-f",
      "merge_method=merge",
      "-f",
      `sha=${state.candidateSha}`,
    ]);
  } catch (error) {
    mergeFailure = error;
  }

  pullRequest = fetchPullRequest(state.prNumber);
  assertExactCandidate();
  disposition = pullRequestDisposition(pullRequest);
  if (disposition === "ACCEPT") {
    const closing = acceptMergedPullRequest(state, pullRequest);
    finalizeClosingState(closing);
    return;
  }
  if (disposition === "STOP") {
    throw new QueueFailure(
      `PR #${pullRequest.number} closed during automatic merge without a merge commit.`,
      false,
    );
  }
  assertCleanMergeState();
  if (mergeFailure) {
    const httpStatus = githubHttpStatus(mergeFailure);
    if (
      httpStatus !== undefined &&
      httpStatus >= 400 &&
      httpStatus < 500 &&
      httpStatus !== 408 &&
      httpStatus !== 429
    ) {
      throw new QueueFailure(
        `GitHub refused automatic merge for PR #${pullRequest.number} (HTTP ${httpStatus}).`,
        true,
      );
    }
    throw mergeFailure;
  }
  throw new Error(
    `PR #${pullRequest.number} remained open after GitHub accepted the merge command.`,
  );
};

const runController = async (): Promise<void> => {
  let state = readState();
  activeState = state;
  const viewer = runHostRead("gh", ["api", "user", "--jq", ".login"]);
  if (statusOnly || dryRun) {
    do {
      state = readState() ?? recoverOpenPullRequest();
      const issues = fetchImplementationIssues();
      console.log(
        JSON.stringify(
          {
            mode: statusOnly ? "status" : "dry-run",
            repository: REPOSITORY,
            viewer,
            baseRef,
            model,
            queue: summarizeQueue(issues, viewer),
            live: readLiveStatus(),
            active: state,
            plannedActions: state
              ? state.stage === "NEEDS_HUMAN"
                ? state.resumeStage
                  ? [
                      "fix-reported-condition",
                      "npm run sandcastle:auto -- --resume",
                    ]
                  : state.failedStage === "IMPLEMENTING" && !state.prNumber
                    ? [
                        "inspect-preserved-attempt",
                        "npm run sandcastle:auto -- --retry",
                      ]
                    : ["inspect-preserved-attempt"]
                : [`resume-${state.stage.toLowerCase()}`]
              : [
                  "claim-next-frontier",
                  "implement-with-$implement",
                  "verify",
                  "push",
                  "open-pr",
                  "auto-merge",
                  "post-merge-verify",
                  "close-issue",
                ],
          },
          null,
          2,
        ),
      );
      if (!statusOnly || !watch) break;
      await new Promise((resolve) => setTimeout(resolve, pollSeconds * 1000));
    } while (true);
    return;
  }

  state ??= recoverOpenPullRequest();
  activeState = state;
  reexecAfterBaseChange(syncBase().changed);
  if (state && !existsSync(statePath)) state = persistState(state);
  if (state && state.stage !== "NEEDS_HUMAN" && state.error) {
    state = persistState({
      ...state,
      error: undefined,
      updatedAt: new Date().toISOString(),
    });
  }
  if (
    state &&
    state.stage !== "CLAIMING" &&
    state.stage !== "IMPLEMENTING" &&
    state.stage !== "CLOSING" &&
    state.stage !== "NEEDS_HUMAN"
  ) {
    requireLeaseOwned(state);
    validateLeaseIdentity(state);
  }

  if ((retry || resume) && state?.stage !== "NEEDS_HUMAN") {
    throw new QueueFailure(
      `${retry ? "--retry" : "--resume"} requires an active NEEDS_HUMAN checkpoint.`,
      false,
    );
  }

  if (state?.stage === "NEEDS_HUMAN") {
    if (retry) {
      retryInterruptedImplementation(state);
      state = undefined;
    }
  }

  if (state?.stage === "NEEDS_HUMAN") {
    if (!resume || !state.resumeStage) {
      throw new QueueFailure(
        state.error ??
          `Attempt ${state.attemptId} needs human inspection before it can continue.`,
        false,
      );
    }
    state = persistState({
      ...state,
      stage: state.resumeStage,
      failedStage: undefined,
      resumeStage: undefined,
      error: undefined,
      updatedAt: new Date().toISOString(),
    });
  }

  let acceptedThisRun = 0;
  while (acceptedThisRun < maxTickets) {
    if (state?.stage === "CLAIMING" || state?.stage === "IMPLEMENTING") {
      state = reconcileAttemptState(state, viewer);
    }
    if (state?.stage === "PUBLISHING") state = publishCandidate(state);
    if (state?.stage === "CLOSING") {
      finalizeClosingState(state);
      acceptedThisRun += 1;
      codexModelPreflightPassedForCurrentTicket = false;
      state = undefined;
      if (acceptedThisRun >= maxTickets) break;
    }
    if (state?.stage === "MERGING") {
      mergePullRequest(state);
      acceptedThisRun += 1;
      codexModelPreflightPassedForCurrentTicket = false;
      state = undefined;
      if (acceptedThisRun >= maxTickets) break;
    }
    if (state?.stage === "NEEDS_HUMAN") {
      throw new QueueFailure(
        state.error ?? `Attempt ${state.attemptId} needs human inspection.`,
        false,
      );
    }
    if (state) continue;

    const issues = fetchImplementationIssues();
    const candidate = selectNextFrontier(issues, viewer);
    if (!candidate) {
      const summary = summarizeQueue(issues, viewer);
      if (summary.open === 0) {
        writeLiveStatus("DONE", { queue: summary });
        console.log("[DONE] All implementation tickets are closed.");
      } else {
        writeLiveStatus("IDLE", { queue: summary });
        console.log(
          `[IDLE] No claimable native frontier. ` +
            `${summary.open} open; ${summary.assignedElsewhere} assigned elsewhere.`,
        );
      }
      break;
    }

    ensureExecutionPrerequisites();
    const currentBaseSha = runHost("git", ["rev-parse", "HEAD"]);
    state = persistState(createAttemptState(candidate, currentBaseSha));
  }
};

let transientFailures = 0;
while (true) {
  try {
    await runController();
    break;
  } catch (error) {
    let message = error instanceof Error ? error.message : String(error);
    if (statusOnly || dryRun) {
      console.error(`[ERROR] ${message}`);
      process.exitCode = 2;
      break;
    }
    let terminal = error instanceof QueueFailure;
    let exhaustedTransient = false;
    if (!terminal) {
      transientFailures += 1;
      exhaustedTransient = transientFailures > MAX_TRANSIENT_FAILURES;
      if (exhaustedTransient) {
        terminal = true;
        message =
          `Transient retry budget exhausted after ${MAX_TRANSIENT_FAILURES} retries. ` +
          message;
      }
    }
    if (!terminal) {
      const retrySeconds = Math.min(300, 2 ** Math.min(transientFailures + 2, 8));
      if (activeState && activeState.stage !== "NEEDS_HUMAN") {
        persistState({
          ...activeState,
          error: message,
          updatedAt: new Date().toISOString(),
        });
      }
      writeLiveStatus("RETRYING", {
        issueNumber: activeState?.issueNumber,
        issueTitle: activeState?.issueTitle,
        attemptId: activeState?.attemptId,
        branch: activeState?.branch,
        error: message,
        retryInSeconds: retrySeconds,
      });
      console.error(`[RETRYING in ${retrySeconds}s] ${message}`);
      await new Promise((resolve) => setTimeout(resolve, retrySeconds * 1000));
      continue;
    }

    if (activeState?.stage !== "NEEDS_HUMAN" && activeState) {
      persistState({
        ...activeState,
        stage: "NEEDS_HUMAN",
        failedStage: activeState.stage as Exclude<QueueStage, "NEEDS_HUMAN">,
        resumeStage:
          (error instanceof QueueFailure && error.resumable) || exhaustedTransient
            ? (activeState.stage as Exclude<QueueStage, "NEEDS_HUMAN">)
            : undefined,
        error: message,
        updatedAt: new Date().toISOString(),
      });
    }
    writeLiveStatus("NEEDS_HUMAN", {
      issueNumber: activeState?.issueNumber,
      issueTitle: activeState?.issueTitle,
      attemptId: activeState?.attemptId,
      branch: activeState?.branch,
      error: message,
    });
    console.error(`[NEEDS_HUMAN] ${message}`);
    process.exitCode = 2;
    break;
  }
}
