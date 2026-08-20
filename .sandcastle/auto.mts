import { execFileSync, spawn, spawnSync } from "node:child_process";
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
  requireAllHostAgentsIdle,
  requireHostCodexEnvironment,
  requireHostAgentIdle,
  requireHostCodexLogin,
  requireHostCodexModelAccess,
  requireSafeControllerEnvironment,
} from "./codex-host.mts";
import {
  IMPLEMENTATION_ISSUE_MAX,
  IMPLEMENTATION_ISSUE_MIN,
  PROTECTED_CANDIDATE_PATHS,
  issueSemanticFingerprint,
  pullRequestDisposition,
  removeQueueAttempt,
  resumeStageForAttempt,
  replaceQueueAttempt,
  selectFrontierBatch,
  selectNextFrontier,
  summarizeQueue,
  upsertQueueAttempt,
  type PullRequestState,
  type QueueIssue,
  type QueueSnapshot,
  type QueueStage,
  type QueueState,
} from "./queue-core.mts";
import {
  advanceBaseAtomically,
  verifyIntegratedCandidate,
} from "./integration-verifier.mts";

const REPOSITORY = "15253a/meta_research";
const PARENT_SPEC_NUMBER = 112;
const DEFAULT_BASE_REF = "develop_main";
const DEFAULT_MODEL = "gpt-5.6-sol";
const DEFAULT_POLL_SECONDS = 60;
const DEFAULT_MAX_AGENTS = 3;
const DEFAULT_AGENT_WALL_CLOCK_SECONDS = 8 * 60 * 60;
const FIXED_VERIFY_COMMAND = "bash .sandcastle/verify-ticket.sh";
const INTEGRATION_VERIFY_WALL_CLOCK_SECONDS = 30 * 60;
const MAX_TRANSIENT_FAILURES = 8;
const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const queueDir = join(repoRoot, ".sandcastle", "queue");
const statePath = join(queueDir, "state.json");
const liveStatusPath = join(repoRoot, ".sandcastle", "status.json");
const receiptDir = join(repoRoot, ".sandcastle", "receipts");
let activeState: QueueState | undefined;
let codexModelPreflightPassed = false;

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

const verifyAcceptedCommit = (acceptedSha: string, attemptId: string): void => {
  try {
    requireHostAgentIdle(repoRoot, attemptId);
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
          hostAgentRuntimeLockPath(repoRoot, attemptId),
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
    const message = error instanceof Error ? error.message : String(error);
    throw new QueueFailure(
      `Post-merge verification failed for ${acceptedSha}: ${message}`,
      /verification exited with status 124/.test(message),
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
    issue: { type: "string" },
    "base-ref": { type: "string" },
    "max-agents": { type: "string" },
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
const maxAgents = Number(
  values["max-agents"] ??
    process.env.SANDCASTLE_MAX_AGENTS ??
    controllerEnvironment.SANDCASTLE_MAX_AGENTS ??
    DEFAULT_MAX_AGENTS,
);
const requestedIssueNumber = values.issue === undefined
  ? undefined
  : Number(values.issue);
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
if (!Number.isInteger(maxAgents) || maxAgents < 1 || maxAgents > 3) {
  throw new Error("--max-agents must be an integer between 1 and 3.");
}
if (
  requestedIssueNumber !== undefined &&
  (!/^\d+$/.test(values.issue ?? "") ||
    !Number.isSafeInteger(requestedIssueNumber))
) {
  throw new Error("--issue must be an integer.");
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
    requireAllHostAgentsIdle(repoRoot);
  } catch (error) {
    throw new QueueFailure(
      error instanceof Error ? error.message : String(error),
      true,
    );
  }
  if (!codexModelPreflightPassed) {
    // Online auth/model failures may be transient. Let the controller's
    // bounded retry policy distinguish them from local login-policy failures.
    requireHostCodexModelAccess(repoRoot, model);
    codexModelPreflightPassed = true;
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

const emptyQueueSnapshot = (): QueueSnapshot => ({ version: 2, attempts: [] });

const readQueueSnapshot = (): QueueSnapshot => {
  if (!existsSync(statePath)) return emptyQueueSnapshot();
  const parsed = JSON.parse(readFileSync(statePath, "utf8")) as
    | QueueSnapshot
    | QueueState;
  const snapshot = parsed.version === 2
    ? parsed
    : parsed.version === 1
      ? { version: 2 as const, attempts: [parsed] }
      : undefined;
  if (!snapshot) throw new Error("Unsupported queue state version.");
  const attemptIds = new Set<string>();
  const issueNumbers = new Set<number>();
  for (const attempt of snapshot.attempts) {
    if (attempt.version !== 1) {
      throw new Error("Unsupported ticket attempt state version.");
    }
    if (attemptIds.has(attempt.attemptId) || issueNumbers.has(attempt.issueNumber)) {
      throw new Error("Queue state contains duplicate attempt or issue ownership.");
    }
    attemptIds.add(attempt.attemptId);
    issueNumbers.add(attempt.issueNumber);
  }
  return snapshot;
};

const writeQueueSnapshot = (snapshot: QueueSnapshot): void => {
  mkdirSync(queueDir, { recursive: true });
  if (snapshot.attempts.length === 0) {
    if (existsSync(statePath)) unlinkSync(statePath);
    return;
  }
  const temporaryPath = `${statePath}.tmp`;
  writeFileSync(temporaryPath, `${JSON.stringify(snapshot, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporaryPath, statePath);
};

const persistState = (state: QueueState): QueueState => {
  const snapshot = readQueueSnapshot();
  const conflicting = snapshot.attempts.find(
    (attempt) =>
      attempt.issueNumber === state.issueNumber &&
      attempt.attemptId !== state.attemptId,
  );
  if (conflicting) {
    throw new Error(
      `Issue #${state.issueNumber} already belongs to attempt ${conflicting.attemptId}.`,
    );
  }
  writeQueueSnapshot(upsertQueueAttempt(snapshot, state));
  activeState = state;
  return state;
};

const clearState = (state: QueueState): void => {
  writeQueueSnapshot(removeQueueAttempt(readQueueSnapshot(), state.attemptId));
  const ticketStatusPath = join(
    repoRoot,
    ".sandcastle",
    "status",
    `issue-${state.issueNumber}.json`,
  );
  if (existsSync(ticketStatusPath)) unlinkSync(ticketStatusPath);
  if (activeState?.attemptId === state.attemptId) activeState = undefined;
};

const replaceState = (
  previous: QueueState,
  replacement: QueueState,
): QueueState => {
  const snapshot = readQueueSnapshot();
  const checkpoint = snapshot.attempts.find(
    ({ attemptId }) => attemptId === previous.attemptId,
  );
  if (
    !checkpoint ||
    checkpoint.issueNumber !== previous.issueNumber ||
    replacement.issueNumber !== previous.issueNumber ||
    replacement.attemptId === previous.attemptId
  ) {
    throw new Error(
      `Cannot atomically replace retry checkpoint ${previous.attemptId}.`,
    );
  }
  writeQueueSnapshot(
    replaceQueueAttempt(snapshot, previous.attemptId, replacement),
  );
  activeState = replacement;
  return replacement;
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

const readTicketStatuses = (): unknown[] => {
  const statusDir = join(repoRoot, ".sandcastle", "status");
  if (!existsSync(statusDir)) return [];
  return readdirSync(statusDir)
    .filter((name) => /^issue-\d+\.json$/.test(name))
    .sort()
    .flatMap((name) => {
      try {
        return [JSON.parse(readFileSync(join(statusDir, name), "utf8"))];
      } catch {
        return [];
      }
    });
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

const recoverOpenPullRequests = (): QueueState[] => {
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
  return automationPullRequests.map((pullRequest) => {
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
  });
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

const createAttemptState = (
  issue: QueueIssue,
  currentBaseSha: string,
  slot: number,
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
    slot,
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

const runTicket = async (
  issue: QueueIssue,
  state: QueueState,
  cohortBranches: readonly string[],
): Promise<{ path: string; receipt: Receipt }> => {
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
  if (state.slot === undefined || state.slot < 0 || state.slot >= maxAgents) {
    throw new QueueFailure(
      `Attempt ${state.attemptId} has no valid Agent slot.`,
      false,
    );
  }
  console.log(
    `[IMPLEMENT slot ${state.slot + 1}/${maxAgents}] Starting issue #${issue.number}: ${issue.title}`,
  );
  const args = [
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
    "--slot",
    String(state.slot),
    "--agent-wall-clock-seconds",
    String(agentWallClockSeconds),
  ];
  for (const branch of cohortBranches) {
    args.push("--cohort-branch", branch);
  }
  const result = await new Promise<{ status: number | null; signal: NodeJS.Signals | null }>(
    (resolvePromise, rejectPromise) => {
      const child = spawn(
    "setpriv",
        args,
        { cwd: repoRoot, env: process.env, stdio: "inherit" },
      );
      child.once("error", rejectPromise);
      child.once("close", (status, signal) =>
        resolvePromise({ status, signal }),
      );
    },
  );
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
      `Issue #${issue.number} exited with status ${result.status ?? result.signal ?? "unknown"} without a receipt.`,
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
    requireHostAgentIdle(repoRoot, state.attemptId);
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
};

type PreparedAttempt = {
  state: QueueState;
  issue: QueueIssue;
  needsAgent: boolean;
};

const prepareAttemptState = (
  state: QueueState,
  viewer: string,
): PreparedAttempt => {
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
  if (currentState.stage !== "IMPLEMENTING") {
    return { state: currentState, issue, needsAgent: false };
  }

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
    const recoveredState = persistState(stateFromReceipt(currentState, recovered));
    return { state: recoveredState, issue, needsAgent: false };
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

  return { state: currentState, issue, needsAgent: true };
};

const publishCandidate = (state: QueueState): QueueState => {
  requireCandidateState(state);
  requireLeaseOwned(state);
  // Parallel siblings may have advanced develop_main after this candidate was
  // verified. Requirements stay frozen here; merge-lane integration verifies
  // the exact candidate against the current base before any merge write.
  assertCandidateRequirementsCurrent(state, false);
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
  assertCandidateRequirementsCurrent(state, false);
  const integration = state.integrationVerification;
  if (
    !integration ||
    integration.sourceBaseSha !== state.baseSha ||
    integration.sourceCandidateSha !== state.candidateSha ||
    integration.candidateSha !== state.candidateSha ||
    !integration.mergeCommitSha
  ) {
    throw new QueueFailure(
      `PR #${pullRequest.number} has no matching integration verification checkpoint.`,
      false,
    );
  }
  if (
    pullRequest.baseRefName !== state.baseRef ||
    pullRequest.headRefName !== state.branch ||
    pullRequest.headRefOid !== state.candidateSha
  ) {
    throw new Error(`PR #${pullRequest.number} no longer matches the queue state.`);
  }
  const acceptedSha = integration.mergeCommitSha;
  if (pullRequest.mergeCommit?.oid !== acceptedSha) {
    throw new QueueFailure(
      `GitHub recorded ${pullRequest.mergeCommit?.oid ?? "no merge commit"} for PR #${pullRequest.number}, not verified ${acceptedSha}.`,
      false,
    );
  }
  const mergeCommit = JSON.parse(
    runHostRead("gh", [
      "api",
      `repos/${REPOSITORY}/git/commits/${acceptedSha}`,
    ]),
  ) as {
    tree?: { sha?: string };
    parents?: Array<{ sha?: string }>;
  };
  if (
    mergeCommit.parents?.length !== 2 ||
    mergeCommit.parents[0]?.sha !== integration.baseSha ||
    mergeCommit.parents[1]?.sha !== state.candidateSha ||
    mergeCommit.tree?.sha !== integration.treeSha
  ) {
    throw new QueueFailure(
      `PR #${pullRequest.number} merge commit does not match the verified base, candidate, and integration tree.`,
      false,
    );
  }
  syncBase();
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
  verifyAcceptedCommit(acceptedSha, state.attemptId);
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
    !pullRequest.mergedAt ||
    pullRequest.mergeCommit?.oid !== state.acceptedSha
  ) {
    throw new QueueFailure(
      `PR #${state.prNumber} no longer matches the accepted closing checkpoint.`,
      false,
    );
  }
  syncBase();
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
  clearState(state);
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

const mergePullRequest = (initialState: QueueState): void => {
  requireMergingState(initialState);
  let state: MergingState = initialState;
  requireLeaseOwned(state);
  validateLeaseIdentity(state);
  assertCandidateRequirementsCurrent(state, false);

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
  const assertMergeableAgainstVerifiedBase = (): void => {
    const integration = state.integrationVerification;
    if (!integration) {
      throw new QueueFailure(
        `PR #${pullRequest.number} has no integration verification checkpoint.`,
        false,
      );
    }
    if (pullRequest.isDraft) {
      throw new QueueFailure(
        `PR #${pullRequest.number} is a draft and cannot be merged automatically.`,
        true,
      );
    }
    if (pullRequest.baseRefOid !== integration.baseSha) {
      throw new Error(
        `${state.baseRef} moved from verified ${integration.baseSha} to ${pullRequest.baseRefOid}; integration will be reverified.`,
      );
    }
    const remoteBaseSha = readRemoteRef(`refs/heads/${state.baseRef}`);
    if (remoteBaseSha !== integration.baseSha) {
      throw new Error(
        `${state.baseRef} moved from verified ${integration.baseSha} to ${remoteBaseSha ?? "missing"}; integration will be reverified.`,
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
    const allowedState = ["CLEAN", "BEHIND"].includes(
      pullRequest.mergeStateStatus,
    );
    if (pullRequest.mergeable !== "MERGEABLE" || !allowedState) {
      throw new QueueFailure(
        `PR #${pullRequest.number} is not eligible for verified automatic merge ` +
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

  const remoteBaseBeforeIntegration = readRemoteRef(
    `refs/heads/${state.baseRef}`,
  );
  const sourceIntegration = state.integrationVerification;
  const sourceIntegrationIsCurrent =
    sourceIntegration?.sourceBaseSha === state.baseSha &&
    sourceIntegration.sourceCandidateSha === state.candidateSha &&
    sourceIntegration.candidateSha === state.candidateSha;
  if (
    sourceIntegrationIsCurrent &&
    remoteBaseBeforeIntegration === sourceIntegration.mergeCommitSha
  ) {
    throw new Error(
      `Verified merge ${sourceIntegration.mergeCommitSha} is on ${state.baseRef}; waiting for GitHub to mark PR #${pullRequest.number} merged.`,
    );
  }
  if (remoteBaseBeforeIntegration !== pullRequest.baseRefOid) {
    throw new Error(
      `${state.baseRef} moved while PR #${pullRequest.number} was being read; retrying from the new base.`,
    );
  }

  const currentBaseSha = pullRequest.baseRefOid;
  const existingIntegration = state.integrationVerification;
  const integrationIsCurrent =
    existingIntegration?.sourceBaseSha === state.baseSha &&
    existingIntegration.sourceCandidateSha === state.candidateSha &&
    existingIntegration.baseSha === currentBaseSha &&
    existingIntegration.candidateSha === state.candidateSha &&
    Boolean(existingIntegration.mergeCommitSha) &&
    runStatus("git", [
      "cat-file",
      "-e",
      `${existingIntegration.mergeCommitSha}^{commit}`,
    ]) === 0;
  if (!integrationIsCurrent) {
    writeLiveStatus("INTEGRATION_VERIFY", {
      issueNumber: state.issueNumber,
      issueTitle: state.issueTitle,
      branch: state.branch,
      prNumber: state.prNumber,
      sourceBaseSha: state.baseSha,
      currentBaseSha,
      candidateSha: state.candidateSha,
      attemptId: state.attemptId,
    });
    console.log(
      `[INTEGRATION_VERIFY] PR #${pullRequest.number} against ${currentBaseSha}.`,
    );
    let verified;
    try {
      verified = verifyIntegratedCandidate({
        repoRoot,
        baseSha: currentBaseSha,
        candidateSha: state.candidateSha,
        verifyCommand: FIXED_VERIFY_COMMAND,
        runtimeLockPath: hostAgentRuntimeLockPath(repoRoot, state.attemptId),
        wallClockSeconds: INTEGRATION_VERIFY_WALL_CLOCK_SECONDS,
        protectedPaths: PROTECTED_CANDIDATE_PATHS,
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      throw new QueueFailure(
        `Integration verification failed for PR #${pullRequest.number}: ${message}`,
        message.includes("runtime lock is busy"),
      );
    }
    state = persistState({
      ...state,
      integrationVerification: {
        sourceBaseSha: state.baseSha,
        sourceCandidateSha: state.candidateSha,
        ...verified,
      },
      updatedAt: new Date().toISOString(),
    }) as MergingState;
  }

  assertCandidateRequirementsCurrent(state, false);
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
      `PR #${pullRequest.number} was closed without merge after integration verification.`,
      false,
    );
  }
  assertMergeableAgainstVerifiedBase();

  writeLiveStatus("AUTO_MERGING", {
    issueNumber: state.issueNumber,
    issueTitle: state.issueTitle,
    branch: state.branch,
    prNumber: state.prNumber,
    prUrl: state.prUrl,
    candidateSha: state.candidateSha,
    integrationVerification: state.integrationVerification,
    attemptId: state.attemptId,
  });
  console.log(`[AUTO_MERGING] PR #${pullRequest.number}: ${pullRequest.url}`);

  const integration = state.integrationVerification!;
  const preparedTreeSha = runHost("git", [
    "rev-parse",
    `${integration.mergeCommitSha}^{tree}`,
  ]);
  const preparedParents = runHost("git", [
    "show",
    "--format=%P",
    "--no-patch",
    integration.mergeCommitSha,
  ]).split(" ");
  if (
    preparedTreeSha !== integration.treeSha ||
    preparedParents.length !== 2 ||
    preparedParents[0] !== integration.baseSha ||
    preparedParents[1] !== state.candidateSha
  ) {
    throw new QueueFailure(
      `Prepared merge ${integration.mergeCommitSha} no longer matches its verified tree and parents.`,
      false,
    );
  }

  advanceBaseAtomically({
    repoRoot,
    baseRef: state.baseRef,
    expectedBaseSha: integration.baseSha,
    mergeCommitSha: integration.mergeCommitSha,
  });

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
  throw new Error(
    `Verified merge ${integration.mergeCommitSha} reached ${state.baseRef}; waiting for GitHub to mark PR #${pullRequest.number} merged.`,
  );
};

const sortAttempts = (attempts: readonly QueueState[]): QueueState[] =>
  [...attempts].sort(
    (left, right) =>
      left.issueNumber - right.issueNumber ||
      left.attemptId.localeCompare(right.attemptId),
  );

const reconcileRecoveredPullRequests = (
  snapshot: QueueSnapshot,
): QueueSnapshot => {
  let next = snapshot;
  for (const recovered of recoverOpenPullRequests()) {
    const existing = next.attempts.find(
      ({ issueNumber }) => issueNumber === recovered.issueNumber,
    );
    if (!existing) {
      next = upsertQueueAttempt(next, recovered);
      continue;
    }
    if (
      existing.attemptId !== recovered.attemptId ||
      (existing.candidateSha &&
        existing.candidateSha !== recovered.candidateSha) ||
      (existing.prNumber && existing.prNumber !== recovered.prNumber)
    ) {
      throw new QueueFailure(
        `Open PR #${recovered.prNumber} conflicts with checkpoint for issue #${recovered.issueNumber}.`,
        false,
      );
    }
    if (existing.stage === "PUBLISHING" || existing.stage === "MERGING") {
      next = upsertQueueAttempt(next, {
        ...existing,
        stage: "MERGING",
        candidateSha: recovered.candidateSha,
        prNumber: recovered.prNumber,
        prUrl: recovered.prUrl,
        updatedAt: new Date().toISOString(),
      });
      continue;
    }
    if (existing.stage !== "NEEDS_HUMAN") {
      throw new QueueFailure(
        `Open PR #${recovered.prNumber} conflicts with ${existing.stage} attempt ${existing.attemptId}.`,
        false,
      );
    }
  }
  return next;
};

const plannedActionsForAttempt = (state: QueueState): string[] => {
  if (state.stage !== "NEEDS_HUMAN") {
    return [`resume-${state.stage.toLowerCase()}`];
  }
  if (resumeStageForAttempt(state)) {
    return [
      "fix-reported-condition",
      `npm run sandcastle:auto -- --resume --issue ${state.issueNumber}`,
    ];
  }
  if (state.failedStage === "IMPLEMENTING" && !state.prNumber) {
    return [
      "inspect-preserved-attempt",
      `npm run sandcastle:auto -- --retry --issue ${state.issueNumber}`,
    ];
  }
  return ["inspect-preserved-attempt"];
};

const recordAttemptFailure = (
  fallbackState: QueueState,
  error: unknown,
  allowResume = error instanceof QueueFailure && error.resumable,
): QueueState => {
  const current =
    readQueueSnapshot().attempts.find(
      ({ attemptId }) => attemptId === fallbackState.attemptId,
    ) ?? fallbackState;
  if (current.stage === "NEEDS_HUMAN") return current;
  const failedStage = current.stage as Exclude<QueueStage, "NEEDS_HUMAN">;
  const message = error instanceof Error ? error.message : String(error);
  return persistState({
    ...current,
    stage: "NEEDS_HUMAN",
    failedStage,
    resumeStage:
      allowResume && failedStage !== "IMPLEMENTING" ? failedStage : undefined,
    error: message,
    updatedAt: new Date().toISOString(),
  });
};

const selectRequestedFailure = (
  snapshot: QueueSnapshot,
): QueueState => {
  const failures = sortAttempts(
    snapshot.attempts.filter(({ stage }) => stage === "NEEDS_HUMAN"),
  );
  const selected = requestedIssueNumber === undefined
    ? failures.length === 1
      ? failures[0]
      : undefined
    : failures.find(({ issueNumber }) => issueNumber === requestedIssueNumber);
  if (!selected) {
    const qualifier = requestedIssueNumber === undefined
      ? failures.length === 0
        ? "There is no NEEDS_HUMAN checkpoint."
        : "Multiple tickets need attention; pass --issue <number>."
      : `Issue #${requestedIssueNumber} has no NEEDS_HUMAN checkpoint.`;
    throw new QueueFailure(qualifier, false);
  }
  return selected;
};

const runController = async (): Promise<void> => {
  activeState = undefined;
  const viewer = runHostRead("gh", ["api", "user", "--jq", ".login"]);
  if (statusOnly || dryRun) {
    do {
      const snapshot = reconcileRecoveredPullRequests(readQueueSnapshot());
      const issues = fetchImplementationIssues();
      const running = snapshot.attempts.filter(({ stage }) =>
        stage === "CLAIMING" || stage === "IMPLEMENTING",
      ).length;
      const hasRoundBarrier = snapshot.attempts.length > 0;
      const frontier = hasRoundBarrier
        ? []
        : selectFrontierBatch({
            issues,
            viewer,
            activeIssueNumbers: new Set(),
            limit: maxAgents,
          });
      console.log(
        JSON.stringify(
          {
            mode: statusOnly ? "status" : "dry-run",
            repository: REPOSITORY,
            viewer,
            baseRef,
            model,
            maxAgents,
            capacity: {
              limit: maxAgents,
              running,
              available: Math.max(0, maxAgents - running),
              roundBarrier: hasRoundBarrier,
            },
            queue: summarizeQueue(issues, viewer),
            frontier: frontier.map(({ number, title }) => ({ number, title })),
            live: {
              controller: readLiveStatus(),
              tickets: readTicketStatuses(),
            },
            active: sortAttempts(snapshot.attempts),
            plannedActions:
              snapshot.attempts.length > 0
                ? sortAttempts(snapshot.attempts).map((state) => ({
                    issueNumber: state.issueNumber,
                    actions: plannedActionsForAttempt(state),
                  }))
                : [
                    `claim-up-to-${maxAgents}-native-frontier-tickets`,
                    "implement-each-with-$implement",
                    "wait-for-round-barrier",
                    "serial-integration-verify-and-auto-merge",
                    "post-merge-verify-and-close",
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

  if (requestedIssueNumber !== undefined && !retry && !resume) {
    throw new QueueFailure("--issue is only used with --retry or --resume.", false);
  }

  syncBase();
  let snapshot = readQueueSnapshot();
  const reconciled = reconcileRecoveredPullRequests(snapshot);
  if (JSON.stringify(reconciled) !== JSON.stringify(snapshot)) {
    writeQueueSnapshot(reconciled);
  }
  snapshot = readQueueSnapshot();
  for (const state of snapshot.attempts) {
    if (state.stage !== "NEEDS_HUMAN" && state.error) {
      persistState({
        ...state,
        error: undefined,
        updatedAt: new Date().toISOString(),
      });
    }
  }

  if (retry || resume) {
    const target = selectRequestedFailure(readQueueSnapshot());
    activeState = target;
    if (retry) {
      const issue = fetchIssue(target.issueNumber);
      if (!selectNextFrontier([issue], viewer)) {
        throw new QueueFailure(
          `Issue #${target.issueNumber} is no longer on the native frontier.`,
          false,
        );
      }
      ensureExecutionPrerequisites();
      const occupiedSlots = new Set(
        readQueueSnapshot().attempts
          .filter(
            ({ attemptId, stage }) =>
              attemptId !== target.attemptId &&
              (stage === "CLAIMING" || stage === "IMPLEMENTING"),
          )
          .map(({ slot }) => slot)
          .filter((slot): slot is number => slot !== undefined),
      );
      const retrySlot = Array.from({ length: maxAgents }, (_, slot) => slot)
        .find((slot) => !occupiedSlots.has(slot));
      if (retrySlot === undefined) {
        throw new QueueFailure(
          `No Agent slot is available to retry issue #${target.issueNumber}.`,
          true,
        );
      }
      const replacement = createAttemptState(
        issue,
        runHost("git", ["rev-parse", "HEAD"]),
        retrySlot,
      );
      retryInterruptedImplementation(target);
      replaceState(target, replacement);
      writeLiveStatus("RETRYING_IMPLEMENTATION", {
        issueNumber: target.issueNumber,
        issueTitle: target.issueTitle,
        previousAttemptId: target.attemptId,
        attemptId: replacement.attemptId,
        preservedBranch: target.branch,
        branch: replacement.branch,
        slot: replacement.slot,
      });
    } else {
      const resumeStage = resumeStageForAttempt(target);
      if (!resumeStage) {
        throw new QueueFailure(
          `Issue #${target.issueNumber} has no resumable stage; inspect its preserved evidence.`,
          false,
        );
      }
      persistState({
        ...target,
        stage: resumeStage,
        failedStage: undefined,
        resumeStage: undefined,
        error: undefined,
        updatedAt: new Date().toISOString(),
      });
    }
  }

  let acceptedThisRun = 0;
  while (acceptedThisRun < maxTickets) {
    snapshot = readQueueSnapshot();
    const workerStates = sortAttempts(
      snapshot.attempts.filter(
        ({ stage }) => stage === "CLAIMING" || stage === "IMPLEMENTING",
      ),
    );

    if (workerStates.length > 0) {
      if (workerStates.length > maxAgents) {
        throw new QueueFailure(
          `${workerStates.length} implementation attempts exceed --max-agents ${maxAgents}.`,
          false,
        );
      }
      activeState = undefined;
      ensureExecutionPrerequisites();

      const slottedStates = workerStates.map((state, slot) =>
        state.slot === slot
          ? state
          : persistState({
              ...state,
              slot,
              updatedAt: new Date().toISOString(),
            }),
      );
      const prepared: PreparedAttempt[] = [];
      for (const state of slottedStates) {
        activeState = state;
        try {
          const attempt = prepareAttemptState(state, viewer);
          if (attempt.needsAgent) prepared.push(attempt);
        } catch (error) {
          if (!(error instanceof QueueFailure)) throw error;
          const failed = recordAttemptFailure(state, error);
          console.error(
            `[NEEDS_HUMAN #${failed.issueNumber}] ${failed.error ?? error.message}`,
          );
        }
      }

      if (prepared.length > 0) {
        const cohortBranches = sortAttempts(readQueueSnapshot().attempts)
          .map(({ branch }) => branch);
        if (cohortBranches.length > 3) {
          throw new QueueFailure(
            `The active cohort has ${cohortBranches.length} branches; maximum is 3.`,
            false,
          );
        }
        writeLiveStatus("ROUND_IMPLEMENTING", {
          maxAgents,
          tickets: prepared.map(({ state, issue }) => ({
            issueNumber: issue.number,
            issueTitle: issue.title,
            attemptId: state.attemptId,
            branch: state.branch,
            slot: state.slot,
          })),
        });
        activeState = undefined;
        const results = await Promise.allSettled(
          prepared.map(({ issue, state }) =>
            runTicket(issue, state, cohortBranches),
          ),
        );
        for (const [index, result] of results.entries()) {
          const state = prepared[index]!.state;
          activeState = state;
          if (result.status === "fulfilled") {
            try {
              persistState(stateFromReceipt(state, result.value));
            } catch (error) {
              const failed = recordAttemptFailure(state, error, false);
              console.error(
                `[NEEDS_HUMAN #${failed.issueNumber}] ${failed.error ?? String(error)}`,
              );
            }
          } else {
            const failed = recordAttemptFailure(state, result.reason, false);
            console.error(
              `[NEEDS_HUMAN #${failed.issueNumber}] ${failed.error ?? String(result.reason)}`,
            );
          }
        }
      }
      activeState = undefined;
      continue;
    }

    for (const state of sortAttempts(
      snapshot.attempts.filter(({ stage }) => stage === "CLOSING"),
    )) {
      if (acceptedThisRun >= maxTickets) break;
      activeState = state;
      try {
        finalizeClosingState(state);
        acceptedThisRun += 1;
      } catch (error) {
        if (!(error instanceof QueueFailure)) throw error;
        recordAttemptFailure(state, error);
      }
    }
    if (acceptedThisRun >= maxTickets) break;

    snapshot = readQueueSnapshot();
    for (const state of sortAttempts(
      snapshot.attempts.filter(({ stage }) => stage === "PUBLISHING"),
    )) {
      activeState = state;
      try {
        publishCandidate(state);
      } catch (error) {
        if (!(error instanceof QueueFailure)) throw error;
        recordAttemptFailure(state, error);
      }
    }

    snapshot = readQueueSnapshot();
    for (const state of sortAttempts(
      snapshot.attempts.filter(({ stage }) => stage === "MERGING"),
    )) {
      if (acceptedThisRun >= maxTickets) break;
      activeState = state;
      try {
        mergePullRequest(state);
        acceptedThisRun += 1;
      } catch (error) {
        if (!(error instanceof QueueFailure)) throw error;
        recordAttemptFailure(state, error);
      }
    }
    if (acceptedThisRun >= maxTickets) break;

    snapshot = readQueueSnapshot();
    const failures = sortAttempts(
      snapshot.attempts.filter(({ stage }) => stage === "NEEDS_HUMAN"),
    );
    if (failures.length > 0) {
      activeState = failures[0];
      throw new QueueFailure(
        failures[0]!.error ??
          `Issue #${failures[0]!.issueNumber} needs human inspection.`,
        false,
      );
    }
    if (snapshot.attempts.length > 0) continue;

    const issues = fetchImplementationIssues();
    const remaining = maxTickets - acceptedThisRun;
    const frontier = selectFrontierBatch({
      issues,
      viewer,
      activeIssueNumbers: new Set(),
      limit: Math.min(maxAgents, remaining),
    });
    if (frontier.length === 0) {
      const summary = summarizeQueue(issues, viewer);
      if (summary.open === 0) {
        writeLiveStatus("DONE", { maxAgents, queue: summary });
        console.log("[DONE] All implementation tickets are closed.");
      } else {
        writeLiveStatus("IDLE", { maxAgents, queue: summary });
        console.log(
          `[IDLE] No claimable native frontier. ` +
            `${summary.open} open; ${summary.assignedElsewhere} assigned elsewhere.`,
        );
      }
      break;
    }

    activeState = undefined;
    ensureExecutionPrerequisites();
    const currentBaseSha = runHost("git", ["rev-parse", "HEAD"]);
    for (const [slot, issue] of frontier.entries()) {
      persistState(createAttemptState(issue, currentBaseSha, slot));
    }
  }
  activeState = undefined;
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
