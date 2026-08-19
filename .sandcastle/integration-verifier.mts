import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import {
  HOST_RUNTIME_LOCK_CONFLICT_STATUS,
  boundedHostCommand,
} from "./codex-host.mts";

export type IntegrationVerificationOptions = {
  repoRoot: string;
  baseSha: string;
  candidateSha: string;
  verifyCommand: string;
  runtimeLockPath: string;
  wallClockSeconds: number;
  protectedPaths: readonly string[];
};

export type IntegrationVerification = {
  baseSha: string;
  candidateSha: string;
  treeSha: string;
  mergeCommitSha: string;
  verifiedAt: string;
};

export type AtomicBaseAdvanceOptions = {
  repoRoot: string;
  baseRef: string;
  expectedBaseSha: string;
  mergeCommitSha: string;
  remote?: string;
};

type CommandResult = {
  status: number;
  stdout: string;
  stderr: string;
};

const run = (
  command: string,
  args: readonly string[],
  cwd: string,
): CommandResult => {
  const result = spawnSync(command, args, {
    cwd,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 10 * 1024 * 1024,
  });
  if (result.error) throw result.error;
  return {
    status: result.status ?? 1,
    stdout: result.stdout,
    stderr: result.stderr,
  };
};

const describeFailure = (result: CommandResult): string => {
  const detail = `${result.stderr}\n${result.stdout}`.trim();
  return detail ? `: ${detail}` : "";
};

const shellQuote = (value: string): string =>
  `'${value.replaceAll("'", `'\\''`)}'`;

const git = (repoRoot: string, args: readonly string[]): string =>
  execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    maxBuffer: 10 * 1024 * 1024,
  }).trim();

const resolveCommit = (
  repoRoot: string,
  value: string,
  label: string,
): string => {
  if (!/^[0-9a-f]{40}(?:[0-9a-f]{24})?$/i.test(value)) {
    throw new Error(`${label} must be a full Git object ID.`);
  }
  try {
    return git(repoRoot, ["rev-parse", "--verify", `${value}^{commit}`]);
  } catch {
    throw new Error(`${label} does not resolve to a commit: ${value}.`);
  }
};

const readRemoteBranch = (
  repoRoot: string,
  remote: string,
  baseRef: string,
): string | undefined => {
  const ref = `refs/heads/${baseRef}`;
  const result = run("git", ["ls-remote", "--heads", remote, ref], repoRoot);
  if (result.status !== 0) {
    throw new Error(
      `Could not read ${remote}/${baseRef}${describeFailure(result)}.`,
    );
  }
  if (!result.stdout.trim()) return undefined;
  const lines = result.stdout.trim().split("\n");
  if (lines.length !== 1) {
    throw new Error(`Remote branch ${remote}/${baseRef} is ambiguous.`);
  }
  const [sha, returnedRef, ...extra] = lines[0]!.split(/\s+/);
  if (!sha || returnedRef !== ref || extra.length > 0) {
    throw new Error(`Malformed remote branch response for ${remote}/${baseRef}.`);
  }
  return sha;
};

export const advanceBaseAtomically = (
  options: AtomicBaseAdvanceOptions,
): string => {
  const repoRoot = resolve(options.repoRoot);
  const remote = options.remote?.trim() || "origin";
  if (
    run(
      "git",
      ["check-ref-format", `refs/heads/${options.baseRef}`],
      repoRoot,
    ).status !== 0
  ) {
    throw new Error(`Invalid base branch name: ${options.baseRef}.`);
  }
  const expectedBaseSha = resolveCommit(
    repoRoot,
    options.expectedBaseSha,
    "expectedBaseSha",
  );
  const mergeCommitSha = resolveCommit(
    repoRoot,
    options.mergeCommitSha,
    "mergeCommitSha",
  );
  const before = readRemoteBranch(repoRoot, remote, options.baseRef);
  if (before !== expectedBaseSha) {
    throw new Error(
      `${remote}/${options.baseRef} moved from expected ${expectedBaseSha} to ${before ?? "missing"}.`,
    );
  }

  const ref = `refs/heads/${options.baseRef}`;
  const push = run(
    "git",
    [
      "push",
      `--force-with-lease=${ref}:${expectedBaseSha}`,
      remote,
      `${mergeCommitSha}:${ref}`,
    ],
    repoRoot,
  );
  const after = readRemoteBranch(repoRoot, remote, options.baseRef);
  if (after === mergeCommitSha) return after;
  if (after !== expectedBaseSha) {
    throw new Error(
      `${remote}/${options.baseRef} changed concurrently to ${after ?? "missing"}; verified merge ${mergeCommitSha} was not applied.`,
    );
  }
  throw new Error(
    `Atomic base advance failed with status ${push.status}${describeFailure(push)}.`,
  );
};

const assertCandidateContributionIsAllowed = (
  repoRoot: string,
  baseSha: string,
  candidateSha: string,
  protectedPaths: readonly string[],
): void => {
  if (protectedPaths.length === 0) return;
  const commonBase = git(repoRoot, ["merge-base", baseSha, candidateSha]);
  const changed = git(repoRoot, [
    "diff",
    "--name-only",
    commonBase,
    candidateSha,
    "--",
    ...protectedPaths,
  ]);
  if (changed) {
    throw new Error(
      `Candidate contribution modifies protected path(s): ${changed
        .split("\n")
        .join(", ")}.`,
    );
  }
};

const cleanupTemporaryWorktree = (
  repoRoot: string,
  temporaryRoot: string,
  worktreePath: string,
  worktreeAdded: boolean,
): string | undefined => {
  let removalFailure: string | undefined;
  const listed = run("git", ["worktree", "list", "--porcelain"], repoRoot);
  const worktreeRegistered =
    listed.status === 0 && listed.stdout.includes(`worktree ${worktreePath}\n`);
  if (worktreeAdded || worktreeRegistered) {
    const removal = run(
      "git",
      ["worktree", "remove", "--force", worktreePath],
      repoRoot,
    );
    if (removal.status !== 0) {
      removalFailure =
        `git worktree remove failed with status ${removal.status}` +
        describeFailure(removal);
    }
  }
  rmSync(temporaryRoot, { recursive: true, force: true });
  if (removalFailure) {
    run("git", ["worktree", "prune", "--expire", "now"], repoRoot);
    const registered = git(repoRoot, ["worktree", "list", "--porcelain"]);
    if (registered.includes(`worktree ${worktreePath}`)) return removalFailure;
  }
  return undefined;
};

export const verifyIntegratedCandidate = (
  options: IntegrationVerificationOptions,
): IntegrationVerification => {
  const repoRoot = resolve(options.repoRoot);
  const baseSha = resolveCommit(repoRoot, options.baseSha, "baseSha");
  const candidateSha = resolveCommit(
    repoRoot,
    options.candidateSha,
    "candidateSha",
  );
  if (!options.verifyCommand.trim()) {
    throw new Error("verifyCommand must not be empty.");
  }
  if (
    !Number.isInteger(options.wallClockSeconds) ||
    options.wallClockSeconds < 1
  ) {
    throw new Error("wallClockSeconds must be a positive integer.");
  }

  assertCandidateContributionIsAllowed(
    repoRoot,
    baseSha,
    candidateSha,
    options.protectedPaths,
  );

  const ancestry = run(
    "git",
    ["merge-base", "--is-ancestor", baseSha, candidateSha],
    repoRoot,
  );
  if (ancestry.status !== 0 && ancestry.status !== 1) {
    throw new Error(
      `Could not compare integration ancestry${describeFailure(ancestry)}.`,
    );
  }
  const candidateContainsBase = ancestry.status === 0;
  const inboxRoot = join(repoRoot, ".sandcastle", "inbox");
  const runtimeLockPath = resolve(repoRoot, options.runtimeLockPath);
  mkdirSync(inboxRoot, { recursive: true, mode: 0o700 });
  mkdirSync(dirname(runtimeLockPath), {
    recursive: true,
    mode: 0o700,
  });
  const temporaryRoot = mkdtempSync(join(inboxRoot, "integration-"));
  const worktreePath = join(temporaryRoot, "worktree");
  let worktreeAdded = false;
  let result: IntegrationVerification | undefined;
  let operationFailure: unknown;

  try {
    const worktreeStart = candidateContainsBase ? candidateSha : baseSha;
    const add = run(
      "git",
      ["worktree", "add", "--detach", worktreePath, worktreeStart],
      repoRoot,
    );
    if (add.status !== 0) {
      throw new Error(
        `Could not create integration worktree${describeFailure(add)}.`,
      );
    }
    worktreeAdded = true;

    if (!candidateContainsBase) {
      const merge = run(
        "git",
        ["merge", "--no-commit", "--no-ff", candidateSha],
        worktreePath,
      );
      if (merge.status !== 0) {
        throw new Error(
          `Candidate cannot be integrated cleanly with base ${baseSha}` +
            describeFailure(merge),
        );
      }
    }

    const treeSha = git(worktreePath, ["write-tree"]);
    const verification = run(
      "sh",
      [
        "-c",
        boundedHostCommand(
          `bash -lc ${shellQuote(options.verifyCommand)}`,
          options.wallClockSeconds,
          runtimeLockPath,
        ),
      ],
      worktreePath,
    );
    if (verification.status === HOST_RUNTIME_LOCK_CONFLICT_STATUS) {
      throw new Error(
        `Integration verification runtime lock is busy: ${runtimeLockPath}.`,
      );
    }
    if (verification.status !== 0) {
      throw new Error(
        `Integration verification failed with status ${verification.status}` +
          describeFailure(verification),
      );
    }

    const unstaged = run(
      "git",
      ["diff", "--quiet", "--exit-code", "--"],
      worktreePath,
    );
    if (unstaged.status === 1) {
      throw new Error(
        "Integration verification left unstaged changes to tracked files.",
      );
    }
    if (unstaged.status !== 0) {
      throw new Error(
        `Could not audit tracked integration files${describeFailure(unstaged)}.`,
      );
    }
    const verifiedTreeSha = git(worktreePath, ["write-tree"]);
    if (verifiedTreeSha !== treeSha) {
      throw new Error(
        `Integration verification changed the index tree from ${treeSha} to ${verifiedTreeSha}.`,
      );
    }

    const mergeCommitSha = git(repoRoot, [
      "commit-tree",
      treeSha,
      "-p",
      baseSha,
      "-p",
      candidateSha,
      "-m",
      `Sandcastle verified merge ${candidateSha.slice(0, 12)} onto ${baseSha.slice(0, 12)}`,
    ]);

    result = {
      baseSha,
      candidateSha,
      treeSha,
      mergeCommitSha,
      verifiedAt: new Date().toISOString(),
    };
  } catch (error) {
    operationFailure = error;
  } finally {
    const cleanupFailure = cleanupTemporaryWorktree(
      repoRoot,
      temporaryRoot,
      worktreePath,
      worktreeAdded,
    );
    if (cleanupFailure) {
      operationFailure = operationFailure
        ? new Error(
            `${operationFailure instanceof Error ? operationFailure.message : String(operationFailure)}; ${cleanupFailure}`,
            { cause: operationFailure },
          )
        : new Error(cleanupFailure);
    }
  }

  if (operationFailure) throw operationFailure;
  if (!result) throw new Error("Integration verification produced no result.");
  return result;
};
