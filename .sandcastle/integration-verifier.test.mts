import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, test } from "node:test";
import { verifyIntegratedCandidate } from "./integration-verifier.mts";

const fixtureRoots: string[] = [];

const git = (repoRoot: string, args: string[]): string =>
  execFileSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();

const writeFixtureFile = (
  repoRoot: string,
  path: string,
  content: string,
): void => {
  const absolutePath = join(repoRoot, path);
  mkdirSync(dirname(absolutePath), { recursive: true });
  writeFileSync(absolutePath, content);
};

const commitAll = (repoRoot: string, message: string): string => {
  git(repoRoot, ["add", "--all"]);
  git(repoRoot, ["commit", "-m", message]);
  return git(repoRoot, ["rev-parse", "HEAD"]);
};

const createFixture = (): { repoRoot: string; baseSha: string } => {
  const root = mkdtempSync(join(tmpdir(), "integration-verifier-test-"));
  fixtureRoots.push(root);
  const repoRoot = join(root, "repo");
  mkdirSync(join(repoRoot, ".sandcastle", "inbox"), { recursive: true });
  mkdirSync(join(repoRoot, ".sandcastle", "queue"), { recursive: true });
  git(repoRoot, ["init", "--initial-branch=main"]);
  git(repoRoot, ["config", "user.name", "Sandcastle Test"]);
  git(repoRoot, ["config", "user.email", "sandcastle@example.invalid"]);
  writeFixtureFile(repoRoot, "base.txt", "base\n");
  return { repoRoot, baseSha: commitAll(repoRoot, "initial") };
};

const assertIntegrationWorkspaceRemoved = (repoRoot: string): void => {
  assert.deepEqual(
    readdirSync(join(repoRoot, ".sandcastle", "inbox")).filter((entry) =>
      entry.startsWith("integration-"),
    ),
    [],
  );
  assert.doesNotMatch(
    git(repoRoot, ["worktree", "list", "--porcelain"]),
    /\.sandcastle\/inbox\/integration-/,
  );
};

afterEach(() => {
  for (const root of fixtureRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

test("reuses a candidate tree that already contains the accepted base", () => {
  const { repoRoot, baseSha } = createFixture();
  writeFixtureFile(repoRoot, "feature.txt", "candidate\n");
  const candidateSha = commitAll(repoRoot, "candidate");

  const result = verifyIntegratedCandidate({
    repoRoot,
    baseSha,
    candidateSha,
    verifyCommand: 'test "$(cat feature.txt)" = candidate',
    runtimeLockPath: join(repoRoot, ".sandcastle", "queue", "runtime.lock"),
    wallClockSeconds: 10,
    protectedPaths: [".sandcastle"],
  });

  assert.deepEqual(
    {
      baseSha: result.baseSha,
      candidateSha: result.candidateSha,
      treeSha: result.treeSha,
    },
    {
      baseSha,
      candidateSha,
      treeSha: git(repoRoot, ["rev-parse", `${candidateSha}^{tree}`]),
    },
  );
  assert.equal(Number.isNaN(Date.parse(result.verifiedAt)), false);
  assert.equal(
    git(repoRoot, ["rev-parse", `${result.mergeCommitSha}^{tree}`]),
    result.treeSha,
  );
  assert.deepEqual(
    git(repoRoot, ["show", "--format=%P", "--no-patch", result.mergeCommitSha])
      .split(" "),
    [baseSha, candidateSha],
  );
  assertIntegrationWorkspaceRemoved(repoRoot);
});

test("verifies the clean integration tree when the accepted base advanced", () => {
  const { repoRoot, baseSha: commonBaseSha } = createFixture();
  git(repoRoot, ["switch", "--create", "candidate", commonBaseSha]);
  writeFixtureFile(repoRoot, "candidate.txt", "candidate\n");
  const candidateSha = commitAll(repoRoot, "candidate");
  git(repoRoot, ["switch", "main"]);
  writeFixtureFile(repoRoot, "advanced-base.txt", "advanced\n");
  const baseSha = commitAll(repoRoot, "advance base");

  const result = verifyIntegratedCandidate({
    repoRoot,
    baseSha,
    candidateSha,
    verifyCommand:
      'test "$(cat candidate.txt)" = candidate && test "$(cat advanced-base.txt)" = advanced',
    runtimeLockPath: join(repoRoot, ".sandcastle", "queue", "runtime.lock"),
    wallClockSeconds: 10,
    protectedPaths: [".sandcastle"],
  });

  assert.equal(result.baseSha, baseSha);
  assert.equal(result.candidateSha, candidateSha);
  assert.equal(git(repoRoot, ["show", `${result.treeSha}:candidate.txt`]), "candidate");
  assert.equal(
    git(repoRoot, ["show", `${result.treeSha}:advanced-base.txt`]),
    "advanced",
  );
  assert.notEqual(result.treeSha, git(repoRoot, ["rev-parse", `${baseSha}^{tree}`]));
  assert.notEqual(
    result.treeSha,
    git(repoRoot, ["rev-parse", `${candidateSha}^{tree}`]),
  );
  assert.deepEqual(
    git(repoRoot, ["show", "--format=%P", "--no-patch", result.mergeCommitSha])
      .split(" "),
    [baseSha, candidateSha],
  );
  assert.equal(
    git(repoRoot, ["rev-parse", `${result.mergeCommitSha}^{tree}`]),
    result.treeSha,
  );
  assertIntegrationWorkspaceRemoved(repoRoot);
});

test("rejects a conflicting stale candidate and removes its worktree", () => {
  const { repoRoot } = createFixture();
  writeFixtureFile(repoRoot, "shared.txt", "common\n");
  const commonBaseSha = commitAll(repoRoot, "shared base");
  git(repoRoot, ["switch", "--create", "candidate", commonBaseSha]);
  writeFixtureFile(repoRoot, "shared.txt", "candidate\n");
  const candidateSha = commitAll(repoRoot, "candidate conflict");
  git(repoRoot, ["switch", "main"]);
  writeFixtureFile(repoRoot, "shared.txt", "advanced base\n");
  const baseSha = commitAll(repoRoot, "base conflict");

  assert.throws(
    () =>
      verifyIntegratedCandidate({
        repoRoot,
        baseSha,
        candidateSha,
        verifyCommand: "true",
        runtimeLockPath: join(
          repoRoot,
          ".sandcastle",
          "queue",
          "runtime.lock",
        ),
        wallClockSeconds: 10,
        protectedPaths: [".sandcastle"],
      }),
    /cannot be integrated cleanly.*CONFLICT/is,
  );
  assertIntegrationWorkspaceRemoved(repoRoot);
});

test("rejects protected paths changed by the candidate contribution", () => {
  const { repoRoot, baseSha } = createFixture();
  writeFixtureFile(repoRoot, ".sandcastle/policy.txt", "changed\n");
  const candidateSha = commitAll(repoRoot, "modify protected policy");

  assert.throws(
    () =>
      verifyIntegratedCandidate({
        repoRoot,
        baseSha,
        candidateSha,
        verifyCommand: "true",
        runtimeLockPath: join(
          repoRoot,
          ".sandcastle",
          "queue",
          "runtime.lock",
        ),
        wallClockSeconds: 10,
        protectedPaths: [".sandcastle"],
      }),
    /Candidate contribution modifies protected path\(s\): \.sandcastle\/policy\.txt/,
  );
  assertIntegrationWorkspaceRemoved(repoRoot);
});

test("reports verification command failures and removes the worktree", () => {
  const { repoRoot, baseSha } = createFixture();
  writeFixtureFile(repoRoot, "feature.txt", "candidate\n");
  const candidateSha = commitAll(repoRoot, "candidate");

  assert.throws(
    () =>
      verifyIntegratedCandidate({
        repoRoot,
        baseSha,
        candidateSha,
        verifyCommand: "printf 'verification failed\\n' >&2; exit 7",
        runtimeLockPath: join(
          repoRoot,
          ".sandcastle",
          "queue",
          "runtime.lock",
        ),
        wallClockSeconds: 10,
        protectedPaths: [".sandcastle"],
      }),
    /Integration verification failed with status 7: verification failed/,
  );
  assertIntegrationWorkspaceRemoved(repoRoot);
});

test("rejects verification commands that alter the integrated tracked tree", () => {
  for (const [verifyCommand, expectedFailure] of [
    [
      "printf 'unstaged\\n' > feature.txt",
      /left unstaged changes to tracked files/,
    ],
    [
      "printf 'staged\\n' > feature.txt && git add feature.txt",
      /changed the index tree/,
    ],
  ] as const) {
    const { repoRoot, baseSha } = createFixture();
    writeFixtureFile(repoRoot, "feature.txt", "candidate\n");
    const candidateSha = commitAll(repoRoot, "candidate");

    assert.throws(
      () =>
        verifyIntegratedCandidate({
          repoRoot,
          baseSha,
          candidateSha,
          verifyCommand,
          runtimeLockPath: join(
            repoRoot,
            ".sandcastle",
            "queue",
            "runtime.lock",
          ),
          wallClockSeconds: 10,
          protectedPaths: [".sandcastle"],
        }),
      expectedFailure,
    );
    assertIntegrationWorkspaceRemoved(repoRoot);
  }
});
