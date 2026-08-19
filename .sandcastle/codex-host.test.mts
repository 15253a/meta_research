import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import {
  IMPLEMENT_WORKFLOW_SKILLS,
  MATT_SKILL_NAMES,
  boundedHostCommand,
  hostAgentRuntimeLockPath,
  hostCodexAgent,
  readMattWorkflowSkills,
  requireHostAgentIdle,
  requireHostCodexEnvironment,
  requireSafeControllerEnvironment,
} from "./codex-host.mts";

const roots: string[] = [];
const createFixture = (): { repoRoot: string; codexHome: string } => {
  const root = mkdtempSync(join(tmpdir(), "sandcastle-host-test-"));
  roots.push(root);
  const repoRoot = join(root, "repo");
  const codexHome = join(root, "codex-home");
  mkdirSync(join(repoRoot, ".sandcastle"), { recursive: true });
  for (const skill of MATT_SKILL_NAMES) {
    const directory = join(codexHome, "skills", skill);
    mkdirSync(directory, { recursive: true });
    writeFileSync(join(directory, "SKILL.md"), `# ${skill}\n`);
  }
  return { repoRoot, codexHome };
};

afterEach(() => {
  delete process.env.CODEX_HOME;
  for (const path of roots.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

test("requires the complete Matt skill bundle from the active Codex home", () => {
  const { codexHome } = createFixture();
  process.env.CODEX_HOME = codexHome;
  const context = requireHostCodexEnvironment();
  assert.equal(context.codexHome, codexHome);
  assert.equal(MATT_SKILL_NAMES.length, 25);
  assert.deepEqual(IMPLEMENT_WORKFLOW_SKILLS, [
    "implement",
    "tdd",
    "code-review",
  ]);
});

test("loads implement and both direct workflow dependencies", () => {
  const { codexHome } = createFixture();
  process.env.CODEX_HOME = codexHome;
  const workflow = readMattWorkflowSkills(requireHostCodexEnvironment());
  assert.equal(workflow.implement.content, "# implement\n");
  assert.equal(workflow.tdd.content, "# tdd\n");
  assert.equal(workflow["code-review"].content, "# code-review\n");
});

test("bounds the stock host Codex process with GNU timeout", () => {
  const provider = hostCodexAgent(
    "gpt-5.4",
    3600,
    "/tmp/sandcastle agent.lock",
    {
      effort: "high",
      captureSessions: false,
    },
  );
  const command = provider.buildPrintCommand({
    prompt: "test",
    dangerouslySkipPermissions: true,
  });
  assert.match(
    command.command,
    /^exec setpriv --pdeathsig TERM flock --no-fork --exclusive --nonblock --conflict-exit-code 75 '\/tmp\/sandcastle agent\.lock' timeout --signal=TERM --kill-after=30s 3600s codex exec /,
  );
  assert.match(command.command, /codex exec --ephemeral --json/);
  assert.match(command.command, /--dangerously-bypass-approvals-and-sandbox/);
});

test("uses the same bounded host wrapper for verification commands", () => {
  assert.equal(
    boundedHostCommand("bash scripts/verify", 1800, "/tmp/runtime.lock"),
    "exec setpriv --pdeathsig TERM flock --no-fork --exclusive --nonblock " +
      "--conflict-exit-code 75 '/tmp/runtime.lock' " +
      "timeout --signal=TERM --kill-after=30s 1800s bash scripts/verify",
  );
});

test("killing the auto parent reaps a bounded verifier and releases its lock", async () => {
  const { repoRoot } = createFixture();
  const marker = join(repoRoot, ".sandcastle", "verifier-started");
  const runtimeLock = hostAgentRuntimeLockPath(repoRoot);
  mkdirSync(join(repoRoot, ".sandcastle", "queue"), {
    recursive: true,
    mode: 0o700,
  });
  const quote = (value: string): string =>
    `'${value.replaceAll("'", `'\\''`)}'`;
  const verifierCommand = boundedHostCommand(
    `sh -c ${quote(`trap '' TERM; : > ${quote(marker)}; sleep 60`)}`,
    5,
    runtimeLock,
    1,
  );
  const mainCode = [
    'const { spawn } = require("node:child_process");',
    'spawn("sh", ["-c", process.env.TEST_VERIFIER_COMMAND], { stdio: "ignore", env: process.env });',
    "setInterval(() => {}, 1000);",
  ].join("\n");
  const autoCode = [
    'const { spawn } = require("node:child_process");',
    'spawn("setpriv", ["--pdeathsig", "TERM", process.execPath, "-e", process.env.TEST_MAIN_CODE], { stdio: "ignore", env: process.env });',
    "setInterval(() => {}, 1000);",
  ].join("\n");
  const auto = spawn(process.execPath, ["-e", autoCode], {
    stdio: "ignore",
    env: {
      ...process.env,
      TEST_MAIN_CODE: mainCode,
      TEST_VERIFIER_COMMAND: verifierCommand,
    },
  });

  try {
    const deadline = Date.now() + 5_000;
    while (!existsSync(marker) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(existsSync(marker), true, "bounded verifier did not start");
    assert.throws(() => requireHostAgentIdle(repoRoot), /still holds/);

    auto.kill("SIGKILL");
    const releaseDeadline = Date.now() + 5_000;
    let released = false;
    while (!released && Date.now() < releaseDeadline) {
      try {
        requireHostAgentIdle(repoRoot);
        released = true;
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 20));
      }
    }
    assert.equal(released, true, "runtime lock survived its controller chain");
  } finally {
    auto.kill("SIGKILL");
  }
});

test("rejects an incomplete host Matt bundle", () => {
  const { codexHome } = createFixture();
  process.env.CODEX_HOME = codexHome;
  rmSync(join(codexHome, "skills", "tdd", "SKILL.md"));
  assert.throws(() => requireHostCodexEnvironment(), /Matt skill tdd/);
});

test("allows only documented non-secret controller settings", () => {
  const { repoRoot } = createFixture();
  writeFileSync(
    join(repoRoot, ".sandcastle", ".env"),
    [
      "SANDCASTLE_CODEX_MODEL=gpt-5.4",
      "SANDCASTLE_BASE_REF=develop_main",
      "SANDCASTLE_POLL_SECONDS=60",
      "SANDCASTLE_AGENT_WALL_CLOCK_SECONDS=28800",
      "",
    ].join("\n"),
  );
  assert.doesNotThrow(() => requireSafeControllerEnvironment(repoRoot));
  writeFileSync(
    join(repoRoot, ".sandcastle", ".env"),
    "GH_TOKEN=\n",
  );
  assert.throws(
    () => requireSafeControllerEnvironment(repoRoot),
    /unsupported key\(s\).*GH_TOKEN/,
  );
});
