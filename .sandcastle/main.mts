import {
  createSandbox,
  type AgentStreamEvent,
} from "@ai-hero/sandcastle";
import { noSandbox } from "@ai-hero/sandcastle/sandboxes/no-sandbox";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readlinkSync,
  readSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  boundedHostCommand,
  HOST_AGENT_SLOT_COUNT,
  hostAgentLockPaths,
  hostAgentRuntimeLockPath,
  hostAgentSlotLockPath,
  hostCodexAgent,
  readMattWorkflowSkills,
  requireHostAgentIdle,
  requireHostCodexEnvironment,
  requireHostCodexLogin,
  requireSafeControllerEnvironment,
} from "./codex-host.mts";
import {
  issueSemanticFingerprint,
  PROTECTED_CANDIDATE_PATHS,
} from "./queue-core.mts";

const REPOSITORY = "15253a/meta_research";
const PARENT_SPEC_NUMBER = 112;
const IMPLEMENTATION_ISSUE_MIN = 113;
const IMPLEMENTATION_ISSUE_MAX = 132;
const DEFAULT_MODEL = "gpt-5.6-sol";
const DEFAULT_VERIFY_COMMAND = "bash .sandcastle/verify-ticket.sh";
const DEFAULT_AGENT_WALL_CLOCK_SECONDS = 8 * 60 * 60;
const VERIFICATION_WALL_CLOCK_SECONDS = 30 * 60;
const CONTROLLER_PATHS = [
  ".sandcastle/main.mts",
  ".sandcastle/auto.mts",
  ".sandcastle/queue-core.mts",
  ".sandcastle/queue-core.test.mts",
  ".sandcastle/codex-host.mts",
  ".sandcastle/codex-host.test.mts",
  ".sandcastle/integration-verifier.mts",
  ".sandcastle/integration-verifier.test.mts",
  ".sandcastle/.gitignore",
  ".sandcastle/.env.example",
  ".sandcastle/implement-ticket.md",
  ".sandcastle/verify-ticket.sh",
  ".agents/skills/implement-ticket/SKILL.md",
  ".agents/skills/implement-ticket/agents/openai.yaml",
  "package.json",
  "package-lock.json",
  "tsconfig.json",
] as const;
const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));

type IssueRef = {
  number: number;
  state: "OPEN" | "CLOSED";
  title: string;
};

type GitHubIssue = {
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

const runHost = (command: string, args: string[]): string =>
  execFileSync(command, args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();

const fetchIssue = (number: number): GitHubIssue =>
  JSON.parse(
    runHost("gh", [
      "issue",
      "view",
      String(number),
      "--repo",
      REPOSITORY,
      "--json",
      "number,title,body,state,assignees,labels,url,updatedAt,blockedBy",
    ]),
  ) as GitHubIssue;

const resolveCommit = (ref: string): string =>
  runHost("git", ["rev-parse", "--verify", `${ref}^{commit}`]);

const resolveRemoteBranch = (ref: string): string | undefined => {
  const output = runHost("git", [
    "ls-remote",
    "--heads",
    "origin",
    `refs/heads/${ref}`,
  ]);
  if (!output) return undefined;
  const [sha, remoteRef, ...extra] = output.split(/\s+/);
  if (!sha || remoteRef !== `refs/heads/${ref}` || extra.length > 0) {
    throw new Error(`Malformed remote branch response for ${ref}.`);
  }
  return sha;
};

const fingerprintPath = (
  root: string,
  shouldSkip: (relativePath: string) => boolean = () => false,
): string => {
  const hash = createHash("sha256");
  const walk = (absolutePath: string, relativePath: string): void => {
    if (relativePath !== "." && shouldSkip(relativePath)) return;
    if (!existsSync(absolutePath)) {
      hash.update(`missing\0${relativePath}\0`);
      return;
    }

    const stat = lstatSync(absolutePath);
    hash.update(`${relativePath}\0${stat.mode.toString(8)}\0`);
    if (stat.isSymbolicLink()) {
      hash.update(`link\0${stat.size.toString()}\0${readlinkSync(absolutePath)}\0`);
      return;
    }
    if (stat.isDirectory()) {
      hash.update("directory\0");
      for (const entry of readdirSync(absolutePath).sort()) {
        walk(join(absolutePath, entry), join(relativePath, entry));
      }
      return;
    }
    if (!stat.isFile()) {
      hash.update("other\0");
      return;
    }

    hash.update(`file\0${stat.size.toString()}\0`);
    const descriptor = openSync(absolutePath, "r");
    const buffer = Buffer.allocUnsafe(64 * 1024);
    try {
      let bytesRead = 0;
      while ((bytesRead = readSync(descriptor, buffer, 0, buffer.length, null)) > 0) {
        hash.update(buffer.subarray(0, bytesRead));
      }
    } finally {
      closeSync(descriptor);
    }
  };

  walk(root, ".");
  return hash.digest("hex");
};

type RefState = { object: string; symref: string };

const readRefs = (): Map<string, RefState> => {
  const output = runHost("git", [
    "for-each-ref",
    "--format=%(refname)%09%(symref)%09%(objectname)",
  ]);
  return new Map(
    output
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const fields = line.split("\t");
        if (fields.length !== 3 || !fields[0] || !fields[2]) {
          throw new Error(`Malformed ref snapshot line: ${line}`);
        }
        return [fields[0], { symref: fields[1], object: fields[2] }];
      }),
  );
};

const refsEqual = (
  left: RefState | undefined,
  right: RefState | undefined,
): boolean =>
  left?.object === right?.object && left?.symref === right?.symref;

type LinkedWorktree = { path: string; branch?: string };

const readLinkedWorktrees = (): LinkedWorktree[] =>
  runHost("git", ["worktree", "list", "--porcelain"])
    .split("\n\n")
    .map((record) => record.split("\n").filter(Boolean))
    .filter((lines) => lines.some((line) => line.startsWith("worktree ")))
    .map((lines) => {
      const worktree = lines.find((line) => line.startsWith("worktree "))!;
      const branch = lines.find((line) => line.startsWith("branch refs/heads/"));
      return {
        path: resolve(worktree.slice("worktree ".length)),
        branch: branch?.slice("branch refs/heads/".length),
      };
    });

const requireValue = (value: string | undefined, message: string): string => {
  if (!value?.trim()) throw new Error(message);
  return value.trim();
};

const shellQuote = (value: string): string =>
  `'${value.replaceAll("'", `'\\''`)}'`;

const streamProgress = (details: Record<string, unknown>) => {
  let lastHeartbeat = 0;
  return (event: AgentStreamEvent): void => {
    if (Date.now() - lastHeartbeat >= 15_000) {
      writeLiveStatus("IMPLEMENTING", {
        ...details,
        heartbeatAt: new Date().toISOString(),
      });
      lastHeartbeat = Date.now();
    }
    if (event.type === "toolCall") {
      console.log(`[IMPLEMENT] tool: ${event.name} ${event.formattedArgs}`);
    } else if (event.type === "text" && event.message.trim()) {
      process.stdout.write(`[IMPLEMENT] ${event.message}`);
      if (!event.message.endsWith("\n")) process.stdout.write("\n");
    }
  };
};

const writeLiveStatus = (
  phase: string,
  details: Record<string, unknown> = {},
): void => {
  mkdirSync(dirname(liveStatusPath), { recursive: true, mode: 0o700 });
  const temporaryPath = `${liveStatusPath}.tmp`;
  writeFileSync(
    temporaryPath,
    `${JSON.stringify(
      { phase, repository: REPOSITORY, ...details, updatedAt: new Date().toISOString() },
      null,
      2,
    )}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  renameSync(temporaryPath, liveStatusPath);
};

const { values } = parseArgs({
  options: {
    issue: { type: "string", short: "i" },
    "dry-run": { type: "boolean" },
    model: { type: "string" },
    verify: { type: "string" },
    "attempt-id": { type: "string" },
    "agent-wall-clock-seconds": { type: "string" },
    "base-ref": { type: "string" },
    slot: { type: "string" },
    "cohort-branch": { type: "string", multiple: true },
  },
  strict: true,
});

const issueArgument = requireValue(
  values.issue,
  "Pass an implementation ticket with --issue <number>.",
);
const issueNumber = Number(issueArgument);
if (!/^\d+$/.test(issueArgument) || !Number.isSafeInteger(issueNumber)) {
  throw new Error("--issue must be an integer.");
}
const liveStatusPath = join(
  repoRoot,
  ".sandcastle",
  "status",
  `issue-${issueNumber}.json`,
);

const slotArgument = values.slot ?? "0";
const slot = Number(slotArgument);
if (!/^\d+$/.test(slotArgument) || !Number.isInteger(slot)) {
  throw new Error("--slot must be an integer.");
}
if (slot < 0 || slot >= HOST_AGENT_SLOT_COUNT) {
  throw new Error(
    `--slot must be between 0 and ${HOST_AGENT_SLOT_COUNT - 1}.`,
  );
}

const generatedAttemptId = `${new Date()
  .toISOString()
  .replaceAll(/[-:.]/g, "")}-${randomUUID().slice(0, 8)}`;
const attemptId = requireValue(
  values["attempt-id"] ?? generatedAttemptId,
  "An attempt id is required.",
);
if (!/^[0-9A-Za-z][0-9A-Za-z-]{7,79}$/.test(attemptId)) {
  throw new Error("--attempt-id must contain only 8-80 letters, digits, or hyphens.");
}
const branch = `codex/issue-${issueNumber}-${attemptId.toLowerCase()}`;
const cohortArguments = values["cohort-branch"] ?? [];
const cohortBranches = cohortArguments.length === 0
  ? [branch]
  : [...cohortArguments];
if (new Set(cohortBranches).size !== cohortBranches.length) {
  throw new Error("--cohort-branch values must be unique.");
}
if (cohortBranches.length > HOST_AGENT_SLOT_COUNT) {
  throw new Error(
    `A ticket cohort may contain at most ${HOST_AGENT_SLOT_COUNT} branches.`,
  );
}
const invalidCohortBranch = cohortBranches.find(
  (candidate) =>
    !/^codex\/issue-(?:11[3-9]|12\d|13[0-2])-[0-9a-z][0-9a-z-]{7,79}$/.test(
      candidate,
    ),
);
if (invalidCohortBranch) {
  throw new Error(`Invalid managed cohort branch: ${invalidCohortBranch}.`);
}
if (!cohortBranches.includes(branch)) {
  throw new Error(
    `The current ticket branch ${branch} must be included with --cohort-branch.`,
  );
}
const cohortBranchSet = new Set(cohortBranches);
const cohortRefs = new Set(cohortBranches.map((name) => `refs/heads/${name}`));

const dryRun = values["dry-run"] ?? false;
const agentWallClockSeconds = Number(
  values["agent-wall-clock-seconds"] ??
    process.env.SANDCASTLE_AGENT_WALL_CLOCK_SECONDS ??
    DEFAULT_AGENT_WALL_CLOCK_SECONDS,
);
if (
  !Number.isInteger(agentWallClockSeconds) ||
  agentWallClockSeconds < 3600 ||
  agentWallClockSeconds > 24 * 60 * 60
) {
  throw new Error(
    "--agent-wall-clock-seconds must be an integer from 3600 to 86400.",
  );
}
const baseRef =
  values["base-ref"] ?? process.env.SANDCASTLE_BASE_REF ?? "develop_main";
const viewer = runHost("gh", ["api", "user", "--jq", ".login"]);
const issue = fetchIssue(issueNumber);
const parentSpec = fetchIssue(PARENT_SPEC_NUMBER);
const baseSha = resolveCommit(baseRef);
const managedWorktreeRoot = resolve(repoRoot, ".sandcastle", "worktrees");
const managedLinkedWorktrees = readLinkedWorktrees().filter(
  ({ path }) =>
    path !== resolve(repoRoot) &&
    (path === managedWorktreeRoot ||
      path.startsWith(`${managedWorktreeRoot}${sep}`)),
);
const currentBranchLinkedWorktrees = managedLinkedWorktrees.filter(
  ({ branch: linkedBranch }) => linkedBranch === branch,
);
const cohortSiblingWorktrees = managedLinkedWorktrees.filter(
  ({ branch: linkedBranch }) =>
    linkedBranch !== branch &&
    linkedBranch !== undefined &&
    cohortBranchSet.has(linkedBranch),
);
const unknownManagedWorktrees = managedLinkedWorktrees.filter(
  ({ branch: linkedBranch }) =>
    linkedBranch === undefined || !cohortBranchSet.has(linkedBranch),
);
const controllerMismatches = CONTROLLER_PATHS.filter((path) => {
  try {
    const baseBlob = runHost("git", ["rev-parse", `${baseSha}:${path}`]);
    const workingBlob = runHost("git", ["hash-object", "--", path]);
    return baseBlob !== workingBlob;
  } catch {
    return true;
  }
});
const assignees = issue.assignees.map(({ login }) => login);
const blockers = issue.blockedBy.nodes;

if (
  issueNumber < IMPLEMENTATION_ISSUE_MIN ||
  issueNumber > IMPLEMENTATION_ISSUE_MAX ||
  !issue.title.startsWith("实现：")
) {
  throw new Error(
    `Issue #${issueNumber} is outside the explicit implementation scope #${IMPLEMENTATION_ISSUE_MIN}-#${IMPLEMENTATION_ISSUE_MAX}.`,
  );
}
if (issue.state !== "OPEN") throw new Error(`Issue #${issueNumber} is not open.`);
if (!issue.labels.some(({ name }) => name === "ready-for-agent")) {
  throw new Error(`Issue #${issueNumber} lacks the ready-for-agent label.`);
}
const openBlockers = blockers.filter(({ state }) => state !== "CLOSED");
if (issue.blockedBy.totalCount !== blockers.length) {
  throw new Error(
    `Issue #${issueNumber} blocker data is incomplete (${blockers.length}/${issue.blockedBy.totalCount}); refusing to run.`,
  );
}
if (openBlockers.length > 0) {
  throw new Error(
    `Issue #${issueNumber} is blocked by ${openBlockers.map(({ number }) => `#${number}`).join(", ")}.`,
  );
}

const eligibility = {
  repository: REPOSITORY,
  issue: issueNumber,
  title: issue.title,
  baseRef,
  baseSha,
  viewer,
  assignees,
  blockedBy: blockers.map(({ number, state }) => ({ number, state })),
  controllerAtBase: controllerMismatches.length === 0,
  controllerMismatches,
  slot,
  branch,
  cohortBranches,
  cohortSiblingWorktrees,
  currentBranchLinkedWorktrees,
  unknownManagedWorktrees,
  additionalLinkedWorktrees: unknownManagedWorktrees.map(({ path }) => path),
  eligibleToClaim: assignees.length === 0,
  runnableByViewer: assignees.length === 1 && assignees[0] === viewer,
};

if (dryRun) {
  console.log(JSON.stringify(eligibility, null, 2));
  process.exit(0);
}

if (assignees.length !== 1 || assignees[0] !== viewer) {
  throw new Error(
    `Issue #${issueNumber} must be assigned only to ${viewer} before a real run.`,
  );
}
if (controllerMismatches.length > 0) {
  throw new Error(
    `Base ${baseSha} does not contain the active controller files: ${controllerMismatches.join(", ")}. Commit the controller to --base-ref before a real run.`,
  );
}
if (currentBranchLinkedWorktrees.length > 0) {
  throw new Error(
    `Refusing to reuse the current ticket's existing worktree: ${currentBranchLinkedWorktrees.map(({ path }) => path).join(", ")}. Recover or remove that exact attempt first.`,
  );
}
if (unknownManagedWorktrees.length > 0) {
  throw new Error(
    `Unregistered Sandcastle worktrees are present: ${unknownManagedWorktrees.map(({ path, branch: linkedBranch }) => `${path} (${linkedBranch ?? "detached"})`).join(", ")}.`,
  );
}
requireSafeControllerEnvironment(repoRoot);
const hostCodex = requireHostCodexEnvironment();
requireHostCodexLogin(repoRoot);
requireHostAgentIdle(repoRoot, attemptId);
const mattWorkflow = readMattWorkflowSkills(hostCodex);

const selectedModel = requireValue(
  values.model ?? process.env.SANDCASTLE_CODEX_MODEL ?? DEFAULT_MODEL,
  "A Codex model is required.",
);
const verifyCommand = requireValue(
  values.verify ??
    process.env.SANDCASTLE_VERIFY_COMMAND ??
    DEFAULT_VERIFY_COMMAND,
  "A verification command is required.",
);
const expectedBranchRef = `refs/heads/${branch}`;
const runtimeLockPath = hostAgentRuntimeLockPath(repoRoot, attemptId);
const slotLockPath = hostAgentSlotLockPath(repoRoot, slot);
const agentLockPaths = hostAgentLockPaths(repoRoot, attemptId, slot);
const logDir = join(repoRoot, ".sandcastle", "logs");
const receiptDir = join(repoRoot, ".sandcastle", "receipts");
const logPath = join(logDir, `${attemptId}-issue-${issueNumber}.log`);
const receiptPath = join(receiptDir, `${attemptId}-issue-${issueNumber}.json`);
mkdirSync(logDir, { recursive: true });
mkdirSync(receiptDir, { recursive: true });

const snapshot = {
  repository: REPOSITORY,
  number: issue.number,
  url: issue.url,
  title: issue.title,
  body: issue.body,
  state: issue.state,
  updatedAt: issue.updatedAt,
  assignees,
  labels: issue.labels.map(({ name }) => name),
  blockedBy: blockers,
  fetchedAt: new Date().toISOString(),
  baseRef,
  baseSha,
  attemptId,
};

const gitCommonDir = resolve(
  repoRoot,
  runHost("git", ["rev-parse", "--git-common-dir"]),
);
const gitConfigPath = join(gitCommonDir, "config");
const gitHooksPath = join(gitCommonDir, "hooks");
const gitObjectAlternatePaths = [
  join(gitCommonDir, "objects", "info", "alternates"),
  join(gitCommonDir, "objects", "info", "http-alternates"),
];
const isExpectedGitRuntimePath = (relativePath: string): boolean =>
  ["objects", "refs", "logs", "worktrees"].some(
    (name) => relativePath === name || relativePath.startsWith(`${name}/`),
  );
const refsBefore = readRefs();
if (refsBefore.has(expectedBranchRef)) {
  throw new Error(`Ref ${expectedBranchRef} already exists.`);
}
const gitMetadataBefore = {
  config: fingerprintPath(gitConfigPath),
  hooks: fingerprintPath(gitHooksPath),
  control: fingerprintPath(gitCommonDir, isExpectedGitRuntimePath),
  objectAlternates: gitObjectAlternatePaths.map((path) => fingerprintPath(path)),
};
const controllerWorktreeStatusBefore = runHost("git", [
  "status",
  "--porcelain=v1",
  "--untracked-files=all",
]);

type SandboxSession = Awaited<ReturnType<typeof createSandbox>>;
let sandbox: SandboxSession | undefined;
let implementation: Awaited<ReturnType<SandboxSession["run"]>> | undefined;
let verification: Awaited<ReturnType<SandboxSession["exec"]>> | undefined;
let ancestryAudit: Awaited<ReturnType<SandboxSession["exec"]>> | undefined;
let protectedPathAudit: Awaited<ReturnType<SandboxSession["exec"]>> | undefined;
let worktreeAudit: Awaited<ReturnType<SandboxSession["exec"]>> | undefined;
let closeResult: Awaited<ReturnType<SandboxSession["close"]>> | undefined;
let failure: string | undefined;

try {
  writeLiveStatus("IMPLEMENTING", {
    issueNumber,
    issueTitle: issue.title,
    branch,
    attemptId,
    model: selectedModel,
    logPath,
  });
  console.log(
    JSON.stringify({
      phase: "IMPLEMENT",
      issueNumber,
      branch,
      model: selectedModel,
      logPath,
    }),
  );
  sandbox = await createSandbox({
    cwd: repoRoot,
    branch,
    baseBranch: baseSha,
    sandbox: noSandbox({ env: { CODEX_HOME: hostCodex.codexHome } }),
  });
  implementation = await sandbox.run({
    name: `issue-${issueNumber}`,
    agent: hostCodexAgent(
      selectedModel,
      agentWallClockSeconds,
      agentLockPaths,
      { effort: "max", captureSessions: false },
    ),
    promptFile: join(repoRoot, ".sandcastle", "implement-ticket.md"),
    promptArgs: {
      ISSUE_NUMBER: String(issueNumber),
      ISSUE_SNAPSHOT: JSON.stringify(snapshot, null, 2),
      PARENT_SPEC_SNAPSHOT: JSON.stringify(parentSpec, null, 2),
      BASE_SHA: baseSha,
      ATTEMPT_ID: attemptId,
      VERIFY_COMMAND: verifyCommand,
      MATT_IMPLEMENT_SKILL: mattWorkflow.implement.content,
      MATT_IMPLEMENT_PATH: mattWorkflow.implement.path,
      MATT_TDD_SKILL: mattWorkflow.tdd.content,
      MATT_TDD_PATH: mattWorkflow.tdd.path,
      MATT_CODE_REVIEW_SKILL: mattWorkflow["code-review"].content,
      MATT_CODE_REVIEW_PATH: mattWorkflow["code-review"].path,
    },
    maxIterations: 1,
    completionSignal: [
      "<implementation-ready/>",
      "<implementation-blocked/>",
    ],
    // GNU timeout inside hostCodexAgent must end the real process before any
    // Sandcastle race can return and leave it running against the worktree.
    idleTimeoutSeconds: agentWallClockSeconds + 60,
    completionTimeoutSeconds: agentWallClockSeconds + 60,
    signal: AbortSignal.timeout((agentWallClockSeconds + 90) * 1000),
    logging: {
      type: "file",
      path: logPath,
      verbose: true,
      onAgentStreamEvent: streamProgress({
        issueNumber,
        issueTitle: issue.title,
        branch,
        attemptId,
        model: selectedModel,
        logPath,
      }),
    },
  });

  if (implementation.completionSignal === "<implementation-ready/>") {
    writeLiveStatus("VERIFYING", {
      issueNumber,
      issueTitle: issue.title,
      branch,
      attemptId,
      verifyCommand,
    });
    console.log(JSON.stringify({ phase: "VERIFY", issueNumber, verifyCommand }));
    verification = await sandbox.exec(
      boundedHostCommand(
        `env -u CODEX_API_KEY -u CODEX_ACCESS_TOKEN -u OPENAI_API_KEY -u GH_TOKEN bash -lc ${shellQuote(verifyCommand)}`,
        VERIFICATION_WALL_CLOCK_SECONDS,
        agentLockPaths,
      ),
      { onLine: (line) => process.stdout.write(`${line}\n`) },
    );
  }

  writeLiveStatus("AUDITING", {
    issueNumber,
    issueTitle: issue.title,
    branch,
    attemptId,
  });
  ancestryAudit = await sandbox.exec(
    `git merge-base --is-ancestor ${baseSha} HEAD`,
  );
  protectedPathAudit = await sandbox.exec(
    `git diff --name-only ${baseSha}...HEAD -- ${PROTECTED_CANDIDATE_PATHS.map(shellQuote).join(" ")}`,
  );
  worktreeAudit = await sandbox.exec(
    "git status --porcelain=v1 --untracked-files=all",
  );
} catch (error) {
  failure = error instanceof Error ? error.message : String(error);
} finally {
  if (sandbox) {
    try {
      closeResult = await sandbox.close();
    } catch (error) {
      const closeFailure = `sandbox close failed: ${error instanceof Error ? error.message : String(error)}`;
      failure = failure ? `${failure}; ${closeFailure}` : closeFailure;
    }
  }
}

let gitMetadataAudit = {
  configUnchanged: false,
  hooksUnchanged: false,
  controlFilesUnchanged: false,
  objectAlternatesUnchanged: false,
  unexpectedRefChanges: ["audit-not-completed"],
  expectedBranchCreated: false,
  branchTipMatchesRun: false,
  unexpectedManagedWorktrees: ["audit-not-completed"],
};
try {
  const refsAfter = readRefs();
  const unexpectedRefChanges = [...new Set([...refsBefore.keys(), ...refsAfter.keys()])]
    .filter((ref) => !cohortRefs.has(ref))
    .filter((ref) => !refsEqual(refsBefore.get(ref), refsAfter.get(ref)))
    .sort();
  const unexpectedManagedWorktrees = readLinkedWorktrees()
    .filter(
      ({ path }) =>
        path !== resolve(repoRoot) &&
        (path === managedWorktreeRoot ||
          path.startsWith(`${managedWorktreeRoot}${sep}`)),
    )
    .filter(
      ({ branch: linkedBranch }) =>
        linkedBranch === undefined || !cohortBranchSet.has(linkedBranch),
    )
    .map(({ path, branch: linkedBranch }) =>
      `${path} (${linkedBranch ?? "detached"})`,
    )
    .sort();
  const expectedBranchState = refsAfter.get(expectedBranchRef);
  const reportedTip = implementation?.commits.at(-1)?.sha;
  gitMetadataAudit = {
    configUnchanged:
      fingerprintPath(gitConfigPath) === gitMetadataBefore.config,
    hooksUnchanged: fingerprintPath(gitHooksPath) === gitMetadataBefore.hooks,
    controlFilesUnchanged:
      fingerprintPath(gitCommonDir, isExpectedGitRuntimePath) ===
      gitMetadataBefore.control,
    objectAlternatesUnchanged:
      gitObjectAlternatePaths
        .map((path) => fingerprintPath(path))
        .every(
          (fingerprint, index) =>
            fingerprint === gitMetadataBefore.objectAlternates[index],
        ),
    unexpectedRefChanges,
    expectedBranchCreated: expectedBranchState !== undefined,
    branchTipMatchesRun:
      expectedBranchState !== undefined &&
      reportedTip !== undefined &&
      expectedBranchState.object === reportedTip,
    unexpectedManagedWorktrees,
  };
} catch (error) {
  const metadataFailure = `post-run Git metadata audit failed: ${error instanceof Error ? error.message : String(error)}`;
  failure = failure ? `${failure}; ${metadataFailure}` : metadataFailure;
}

let controllerWorktreeUnchanged = false;
try {
  controllerWorktreeUnchanged =
    runHost("git", [
      "status",
      "--porcelain=v1",
      "--untracked-files=all",
    ]) === controllerWorktreeStatusBefore;
  if (!controllerWorktreeUnchanged) {
    failure = failure
      ? `${failure}; controller worktree changed during host Agent execution`
      : "controller worktree changed during host Agent execution";
  }
} catch (error) {
  const controllerAuditFailure =
    `controller worktree audit failed: ${error instanceof Error ? error.message : String(error)}`;
  failure = failure
    ? `${failure}; ${controllerAuditFailure}`
    : controllerAuditFailure;
}

let currentness = {
  issueUnchanged: false,
  parentSpecUnchanged: false,
  baseUnchanged: false,
};
try {
  const currentIssue = fetchIssue(issueNumber);
  const currentParentSpec = fetchIssue(PARENT_SPEC_NUMBER);
  const currentBaseSha = resolveRemoteBranch(baseRef);
  currentness = {
    issueUnchanged:
      issueSemanticFingerprint(currentIssue) === issueSemanticFingerprint(issue),
    parentSpecUnchanged:
      issueSemanticFingerprint(currentParentSpec) ===
      issueSemanticFingerprint(parentSpec),
    baseUnchanged: currentBaseSha === baseSha,
  };
} catch (error) {
  const currentnessFailure = `post-run currentness check failed: ${error instanceof Error ? error.message : String(error)}`;
  failure = failure ? `${failure}; ${currentnessFailure}` : currentnessFailure;
}
const ready =
  !failure &&
  implementation?.completionSignal === "<implementation-ready/>" &&
  implementation.commits.length > 0 &&
  verification?.exitCode === 0 &&
  ancestryAudit?.exitCode === 0 &&
  protectedPathAudit?.exitCode === 0 &&
  protectedPathAudit.stdout.trim() === "" &&
  worktreeAudit?.exitCode === 0 &&
  worktreeAudit.stdout.trim() === "" &&
  gitMetadataAudit.configUnchanged &&
  gitMetadataAudit.hooksUnchanged &&
  gitMetadataAudit.controlFilesUnchanged &&
  gitMetadataAudit.objectAlternatesUnchanged &&
  gitMetadataAudit.unexpectedRefChanges.length === 0 &&
  gitMetadataAudit.unexpectedManagedWorktrees.length === 0 &&
  gitMetadataAudit.expectedBranchCreated &&
  gitMetadataAudit.branchTipMatchesRun &&
  controllerWorktreeUnchanged &&
  currentness.issueUnchanged &&
  currentness.parentSpecUnchanged &&
  currentness.baseUnchanged;

const receipt = {
  status: ready ? "READY_FOR_MERGE" : "NEEDS_HUMAN",
  acceptance: "PENDING",
  attemptId,
  issueNumber,
  branch,
  baseRef,
  baseSha,
  issueFingerprint: issueSemanticFingerprint(issue),
  parentSpecFingerprint: issueSemanticFingerprint(parentSpec),
  model: selectedModel,
  execution: {
    provider: "host-no-docker",
    codexVersion: hostCodex.codexVersion,
    codexHome: hostCodex.codexHome,
    worktreeRoot: join(repoRoot, ".sandcastle", "worktrees"),
    slot,
    slotLock: slotLockPath,
    runtimeLock: runtimeLockPath,
    cohortBranches,
  },
  limits: {
    maxAgentRuns: 1,
    maxIterationsPerRun: 1,
    agentWallClockSeconds,
    verificationWallClockSeconds: VERIFICATION_WALL_CLOCK_SECONDS,
  },
  completionSignal: implementation?.completionSignal,
  commits: implementation?.commits ?? [],
  implementation,
  verification,
  policyAudit: {
    ancestry: ancestryAudit,
    protectedPaths: protectedPathAudit,
    cleanWorktree: worktreeAudit,
  },
  gitMetadataAudit,
  controllerWorktreeAudit: {
    unchanged: controllerWorktreeUnchanged,
  },
  currentness,
  failure,
  logPath,
  preservedWorktreePath: closeResult?.preservedWorktreePath,
  finishedAt: new Date().toISOString(),
};
const temporaryReceiptPath = `${receiptPath}.tmp`;
writeFileSync(temporaryReceiptPath, `${JSON.stringify(receipt, null, 2)}\n`, {
  encoding: "utf8",
  mode: 0o600,
});
renameSync(temporaryReceiptPath, receiptPath);
writeLiveStatus(receipt.status, {
  issueNumber,
  issueTitle: issue.title,
  branch,
  attemptId,
  receiptPath,
  failure,
});
console.log(JSON.stringify({ receiptPath, ...receipt }, null, 2));
if (!ready) process.exitCode = 2;
