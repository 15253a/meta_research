import {
  createSandbox,
  codex,
  type AgentStreamEvent,
} from "@ai-hero/sandcastle";
import { docker } from "@ai-hero/sandcastle/sandboxes/docker";
import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  readlinkSync,
  readSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import {
  issueSemanticFingerprint,
  PROTECTED_CANDIDATE_PATHS,
} from "./queue-core.mts";

const REPOSITORY = "15253a/meta_research";
const PARENT_SPEC_NUMBER = 112;
const IMPLEMENTATION_ISSUE_MIN = 113;
const IMPLEMENTATION_ISSUE_MAX = 132;
const DEFAULT_IMAGE = "meta-research-sandcastle:0.12.0";
const DEFAULT_MODEL = "gpt-5.4";
const DEFAULT_VERIFY_COMMAND = "bash .sandcastle/verify-ticket.sh";
const DEFAULT_AGENT_WALL_CLOCK_SECONDS = 8 * 60 * 60;
const VERIFICATION_WALL_CLOCK_SECONDS = 30 * 60;
const CONTROLLER_PATHS = [
  ".sandcastle/main.mts",
  ".sandcastle/auto.mts",
  ".sandcastle/queue-core.mts",
  ".sandcastle/queue-core.test.mts",
  ".sandcastle/implement-ticket.md",
  ".sandcastle/verify-ticket.sh",
  ".agents/skills/implement-ticket/SKILL.md",
  ".agents/skills/implement-ticket/agents/openai.yaml",
  "package.json",
  "package-lock.json",
  "tsconfig.json",
] as const;
const repoRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const liveStatusPath = join(repoRoot, ".sandcastle", "status.json");

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

const readLinkedWorktrees = (): string[] =>
  runHost("git", ["worktree", "list", "--porcelain"])
    .split("\n")
    .filter((line) => line.startsWith("worktree "))
    .map((line) => resolve(line.slice("worktree ".length)));

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

const resolveCodexApiKey = (): string => {
  if (process.env.CODEX_API_KEY?.trim()) {
    return process.env.CODEX_API_KEY.trim();
  }
  try {
    const envFile = readFileSync(join(repoRoot, ".sandcastle", ".env"), "utf8");
    const match = envFile.match(/^CODEX_API_KEY=(.*)$/m);
    let value = match?.[1]?.trim() ?? "";
    if (
      value.length >= 2 &&
      ((value.startsWith('"') && value.endsWith('"')) ||
        (value.startsWith("'") && value.endsWith("'")))
    ) {
      value = value.slice(1, -1);
    }
    if (value) return value;
  } catch {
    // Fall through to the actionable error below.
  }
  throw new Error(
    "Configure CODEX_API_KEY in .sandcastle/.env or the host environment before a real run.",
  );
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
    image: { type: "string" },
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
const imageName = values.image ?? process.env.SANDCASTLE_IMAGE ?? DEFAULT_IMAGE;
const viewer = runHost("gh", ["api", "user", "--jq", ".login"]);
const issue = fetchIssue(issueNumber);
const parentSpec = fetchIssue(PARENT_SPEC_NUMBER);
const baseSha = resolveCommit(baseRef);
const additionalLinkedWorktrees = readLinkedWorktrees().filter(
  (path) => path !== resolve(repoRoot),
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
  additionalLinkedWorktrees,
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
if (additionalLinkedWorktrees.length > 0) {
  throw new Error(
    `Refusing a single-ticket run while other linked worktrees exist: ${additionalLinkedWorktrees.join(", ")}. Resolve or remove preserved attempts first.`,
  );
}
const codexApiKey = resolveCodexApiKey();

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
const expectedBranchRef = `refs/heads/${branch}`;
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
    sandbox: docker({
      imageName,
      cpus: 4,
      env: { CODEX_API_KEY: codexApiKey },
    }),
  });
  implementation = await sandbox.run({
    name: `issue-${issueNumber}`,
    agent: codex(selectedModel, { effort: "high", captureSessions: false }),
    promptFile: join(repoRoot, ".sandcastle", "implement-ticket.md"),
    promptArgs: {
      ISSUE_NUMBER: String(issueNumber),
      ISSUE_SNAPSHOT: JSON.stringify(snapshot, null, 2),
      PARENT_SPEC_SNAPSHOT: JSON.stringify(parentSpec, null, 2),
      BASE_SHA: baseSha,
      ATTEMPT_ID: attemptId,
      VERIFY_COMMAND: verifyCommand,
    },
    maxIterations: 1,
    completionSignal: [
      "<implementation-ready/>",
      "<implementation-blocked/>",
    ],
    idleTimeoutSeconds: 900,
    completionTimeoutSeconds: 30,
    signal: AbortSignal.timeout(agentWallClockSeconds * 1000),
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
      `timeout --signal=TERM --kill-after=30s ${VERIFICATION_WALL_CLOCK_SECONDS}s env -u CODEX_API_KEY -u OPENAI_API_KEY -u GH_TOKEN bash -lc ${shellQuote(verifyCommand)}`,
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
};
try {
  const refsAfter = readRefs();
  const unexpectedRefChanges = [...new Set([...refsBefore.keys(), ...refsAfter.keys()])]
    .filter((ref) => ref !== expectedBranchRef)
    .filter((ref) => !refsEqual(refsBefore.get(ref), refsAfter.get(ref)))
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
  };
} catch (error) {
  const metadataFailure = `post-run Git metadata audit failed: ${error instanceof Error ? error.message : String(error)}`;
  failure = failure ? `${failure}; ${metadataFailure}` : metadataFailure;
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
  gitMetadataAudit.expectedBranchCreated &&
  gitMetadataAudit.branchTipMatchesRun &&
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
  imageName,
  limits: {
    cpus: 4,
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
