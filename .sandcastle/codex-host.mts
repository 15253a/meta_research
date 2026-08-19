import { spawn, spawnSync } from "node:child_process";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  codex,
  type AgentProvider,
  type CodexOptions,
} from "@ai-hero/sandcastle";

export const MATT_SKILL_NAMES = [
  "ask-matt",
  "code-review",
  "codebase-design",
  "diagnosing-bugs",
  "domain-modeling",
  "grill-me",
  "grill-with-docs",
  "grilling",
  "handoff",
  "implement",
  "improve-codebase-architecture",
  "prototype",
  "research",
  "resolving-merge-conflicts",
  "setup-matt-pocock-skills",
  "tdd",
  "teach",
  "to-questionnaire",
  "to-spec",
  "to-tickets",
  "triage",
  "wait-what",
  "wayfinder",
  "wizard",
  "writing-for-agents",
] as const;

export const IMPLEMENT_WORKFLOW_SKILLS = [
  "implement",
  "tdd",
  "code-review",
] as const;

const ALLOWED_SANDCASTLE_ENV_KEYS = new Set([
  "SANDCASTLE_CODEX_MODEL",
  "SANDCASTLE_BASE_REF",
  "SANDCASTLE_POLL_SECONDS",
  "SANDCASTLE_AGENT_WALL_CLOCK_SECONDS",
  "SANDCASTLE_MAX_AGENTS",
]);

export type HostCodexContext = {
  codexHome: string;
  skillsRoot: string;
  codexVersion: string;
};

export type WorkflowSkill = {
  content: string;
  path: string;
};

export type MattWorkflow = {
  implement: WorkflowSkill;
  tdd: WorkflowSkill;
  "code-review": WorkflowSkill;
};

type HostCodexOptions = Omit<CodexOptions, "effort"> & {
  /** Sandcastle 0.12's type predates the Codex CLI's GPT-5.6 max effort. */
  readonly effort?: CodexOptions["effort"] | "max";
};

export const HOST_AGENT_SLOT_COUNT = 3;

const requireAttemptId = (attemptId: string): string => {
  if (!/^[0-9A-Za-z][0-9A-Za-z-]{0,79}$/.test(attemptId)) {
    throw new Error(
      "Host Agent attempt ids must contain only 1-80 letters, digits, or hyphens.",
    );
  }
  return attemptId;
};

const requireSlot = (slot: number): number => {
  if (!Number.isInteger(slot) || slot < 0 || slot >= HOST_AGENT_SLOT_COUNT) {
    throw new Error(
      `Host Agent slot must be an integer from 0 to ${HOST_AGENT_SLOT_COUNT - 1}.`,
    );
  }
  return slot;
};

export const hostAgentRuntimeLockPath = (
  repoRoot: string,
  attemptId: string,
): string =>
  join(
    repoRoot,
    ".sandcastle",
    "queue",
    `agent-runtime-${requireAttemptId(attemptId)}.lock`,
  );

export const hostAgentSlotLockPath = (
  repoRoot: string,
  slot: number,
): string =>
  join(
    repoRoot,
    ".sandcastle",
    "queue",
    `agent-slot-${requireSlot(slot)}.lock`,
  );

export const hostAgentLockPaths = (
  repoRoot: string,
  attemptId: string,
  slot: number,
): readonly [string, string] => [
  hostAgentSlotLockPath(repoRoot, slot),
  hostAgentRuntimeLockPath(repoRoot, attemptId),
];
export const HOST_RUNTIME_LOCK_CONFLICT_STATUS = 75;

const shellQuote = (value: string): string =>
  `'${value.replaceAll("'", `'\\''`)}'`;

export const boundedHostCommand = (
  command: string,
  wallClockSeconds: number,
  runtimeLockPaths: string | readonly string[],
  killAfterSeconds = 30,
): string => {
  const locks = typeof runtimeLockPaths === "string"
    ? [runtimeLockPaths]
    : [...runtimeLockPaths];
  if (locks.length === 0 || locks.some((path) => !path.trim())) {
    throw new Error("At least one non-empty host runtime lock is required.");
  }
  if (new Set(locks).size !== locks.length) {
    throw new Error("Host runtime locks must be unique.");
  }
  const lockCommands = locks
    .map(
      (path) =>
        "flock --no-fork --exclusive --nonblock " +
        `--conflict-exit-code ${HOST_RUNTIME_LOCK_CONFLICT_STATUS} ` +
        `${shellQuote(path)} `,
    )
    .join("");
  return (
    "exec setpriv --pdeathsig TERM " +
    lockCommands +
    `timeout --signal=TERM --kill-after=${killAfterSeconds}s ${wallClockSeconds}s ` +
    command
  );
};

/**
 * Sandcastle's host provider does not terminate its spawned shell when an
 * orchestration timeout wins a race. Prefix the stock Codex command with GNU
 * timeout so a host Agent cannot outlive its ticket wall-clock budget.
 */
export const hostCodexAgent = (
  model: string,
  wallClockSeconds: number,
  runtimeLockPaths: string | readonly string[],
  options: HostCodexOptions = {},
): AgentProvider => {
  // The 0.12 runtime forwards effort verbatim as model_reasoning_effort.
  const provider = codex(model, options as CodexOptions);
  return {
    ...provider,
    buildPrintCommand(commandOptions) {
      const command = provider.buildPrintCommand(commandOptions);
      const ephemeralCommand = command.command.replace(
        " --json",
        " --ephemeral --json",
      );
      if (ephemeralCommand === command.command) {
        throw new Error("Unexpected Sandcastle Codex command shape.");
      }
      return {
        ...command,
        command: boundedHostCommand(
          ephemeralCommand,
          wallClockSeconds,
          runtimeLockPaths,
        ),
      };
    },
  };
};

type SkillMetadata = {
  name: string;
  path: string;
  enabled: boolean;
};

const requireRegularFile = (path: string, label: string): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(`Missing ${label}: ${path}`);
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(`${label} must be a regular file, not a symlink: ${path}`);
  }
};

const requireDirectory = (path: string, label: string): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(`Missing ${label}: ${path}`);
  }
  if (!stat.isDirectory()) {
    throw new Error(`${label} must be a directory: ${path}`);
  }
};

export const resolveActiveCodexHome = (): string =>
  resolve(process.env.CODEX_HOME?.trim() || join(homedir(), ".codex"));

/**
 * Sandcastle loads every key declared in .sandcastle/.env into the Agent
 * process. Keep this file limited to non-secret controller settings even
 * though the host Agent already inherits the controller account's authority.
 */
export const requireSafeControllerEnvironment = (repoRoot: string): void => {
  const envPath = join(repoRoot, ".sandcastle", ".env");
  if (!existsSync(envPath)) return;
  requireRegularFile(envPath, "Sandcastle environment");

  const declaredKeys = new Set<string>();
  for (const rawLine of readFileSync(envPath, "utf8").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    if (!key) throw new Error(`Sandcastle environment has an empty key: ${envPath}`);
    declaredKeys.add(key);
  }

  const forbidden = [...declaredKeys]
    .filter((key) => !ALLOWED_SANDCASTLE_ENV_KEYS.has(key))
    .sort();
  if (forbidden.length > 0) {
    throw new Error(
      `Remove unsupported key(s) from ${envPath}: ${forbidden.join(", ")}. ` +
        "Use the current host Codex/GitHub login directly; do not duplicate credentials in this file.",
    );
  }
};

export const requireHostCodexEnvironment = (): HostCodexContext => {
  const codexHome = resolveActiveCodexHome();
  const skillsRoot = join(codexHome, "skills");
  requireDirectory(codexHome, "active Codex home");
  requireDirectory(skillsRoot, "active Codex skills root");

  for (const skill of MATT_SKILL_NAMES) {
    requireRegularFile(
      join(skillsRoot, skill, "SKILL.md"),
      `Matt skill ${skill}`,
    );
  }

  const version = spawnSync("codex", ["--version"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 30_000,
  });
  if (version.error) throw version.error;
  if (version.status !== 0 || !version.stdout.trim()) {
    throw new Error("The current host Codex CLI is unavailable.");
  }
  const timeout = spawnSync("timeout", ["--version"], {
    stdio: "ignore",
    timeout: 30_000,
  });
  if (timeout.error) throw timeout.error;
  if (timeout.status !== 0) {
    throw new Error("GNU timeout is required for bounded host Agent execution.");
  }
  const setpriv = spawnSync("setpriv", ["--help"], {
    stdio: "ignore",
    timeout: 30_000,
  });
  if (setpriv.error) throw setpriv.error;
  if (setpriv.status !== 0) {
    throw new Error("util-linux setpriv is required for parent-death cleanup.");
  }
  const flock = spawnSync("flock", ["--help"], {
    stdio: "ignore",
    timeout: 30_000,
  });
  if (flock.error) throw flock.error;
  if (flock.status !== 0) {
    throw new Error("util-linux flock is required for host Agent locking.");
  }

  return {
    codexHome,
    skillsRoot,
    codexVersion: version.stdout.trim(),
  };
};

const requireLockIdle = (
  repoRoot: string,
  lockPath: string,
  label: string,
): void => {
  mkdirSync(dirname(lockPath), { recursive: true, mode: 0o700 });
  const result = spawnSync(
    "flock",
    ["--exclusive", "--nonblock", lockPath, "true"],
    { cwd: repoRoot, stdio: "ignore", timeout: 30_000 },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `${label} still holds ${lockPath}; wait for it to exit before recovery or retry.`,
    );
  }
};

export const requireHostAgentIdle = (
  repoRoot: string,
  attemptId: string,
): void =>
  requireLockIdle(
    repoRoot,
    hostAgentRuntimeLockPath(repoRoot, attemptId),
    `Host Codex Agent attempt ${attemptId}`,
  );

export const requireHostAgentSlotIdle = (
  repoRoot: string,
  slot: number,
): void =>
  requireLockIdle(
    repoRoot,
    hostAgentSlotLockPath(repoRoot, slot),
    `Host Codex Agent slot ${slot}`,
  );

export const requireAllHostAgentsIdle = (repoRoot: string): void => {
  const queueDir = join(repoRoot, ".sandcastle", "queue");
  mkdirSync(queueDir, { recursive: true, mode: 0o700 });
  const runtimeLocks = readdirSync(queueDir)
    .filter((name) => /^agent-runtime-[0-9A-Za-z][0-9A-Za-z-]{0,79}\.lock$/.test(name))
    .map((name) => join(queueDir, name));
  const legacyRuntimeLock = join(queueDir, "agent-runtime.lock");
  if (existsSync(legacyRuntimeLock)) runtimeLocks.push(legacyRuntimeLock);
  const locks = [
    ...Array.from({ length: HOST_AGENT_SLOT_COUNT }, (_, slot) =>
      hostAgentSlotLockPath(repoRoot, slot),
    ),
    ...runtimeLocks,
  ];
  for (const lockPath of new Set(locks)) {
    requireLockIdle(repoRoot, lockPath, "A host Codex Agent or verifier");
  }
};

export const requireHostCodexLogin = (repoRoot: string): void => {
  const result = spawnSync("codex", ["login", "status"], {
    cwd: repoRoot,
    env: process.env,
    stdio: "ignore",
    timeout: 30_000,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      "The current host Codex profile is not logged in. Run `codex login`, then retry.",
    );
  }
};

const readWorkflowSkill = (
  context: HostCodexContext,
  name: (typeof IMPLEMENT_WORKFLOW_SKILLS)[number],
): WorkflowSkill => {
  const path = join(context.skillsRoot, name, "SKILL.md");
  requireRegularFile(path, `Matt workflow skill ${name}`);
  return { path, content: readFileSync(path, "utf8") };
};

export const readMattWorkflowSkills = (
  context: HostCodexContext,
): MattWorkflow => ({
  implement: readWorkflowSkill(context, "implement"),
  tdd: readWorkflowSkill(context, "tdd"),
  "code-review": readWorkflowSkill(context, "code-review"),
});

const runCodexSmoke = (
  repoRoot: string,
  model: string,
  prompt: string,
  expected: string,
): void => {
  const smokeRoot = join(repoRoot, ".sandcastle", "inbox");
  mkdirSync(smokeRoot, { recursive: true, mode: 0o700 });
  const workspace = mkdtempSync(join(smokeRoot, "host-codex-smoke-"));
  const outputPath = join(workspace, "last-message.txt");
  try {
    const result = spawnSync(
      "codex",
      [
        "exec",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        model,
        "--config",
        'model_reasoning_effort="low"',
        "--output-last-message",
        outputPath,
        "-",
      ],
      {
        cwd: workspace,
        env: process.env,
        input: prompt,
        encoding: "utf8",
        stdio: ["pipe", "ignore", "ignore"],
        timeout: 180_000,
        killSignal: "SIGTERM",
      },
    );
    if (result.error) throw result.error;
    const output = existsSync(outputPath)
      ? readFileSync(outputPath, "utf8").trim()
      : "";
    if (result.status !== 0 || output !== expected) {
      throw new Error(
        `Host Codex smoke test failed for model ${model} ` +
          `(status=${result.status ?? "unknown"}, expected=${expected}).`,
      );
    }
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
};

export const requireHostCodexModelAccess = (
  repoRoot: string,
  model: string,
): void => {
  runCodexSmoke(
    repoRoot,
    model,
    "Reply exactly HOST_CODEX_OK without using tools.",
    "HOST_CODEX_OK",
  );
};

export const verifyHostImplementWorkflow = (
  repoRoot: string,
  model: string,
  context = requireHostCodexEnvironment(),
): void => {
  const workflow = readMattWorkflowSkills(context);
  runCodexSmoke(
    repoRoot,
    model,
    [
      "Validate the following active Matt Pocock workflow documents.",
      "Do not use tools or modify files.",
      "If and only if implement requires TDD where possible and code review after implementation, reply exactly HOST_IMPLEMENT_WORKFLOW_OK.",
      "",
      `<skill name="implement" path="${workflow.implement.path}">`,
      workflow.implement.content,
      "</skill>",
      `<skill name="tdd" path="${workflow.tdd.path}">`,
      workflow.tdd.content,
      "</skill>",
      `<skill name="code-review" path="${workflow["code-review"].path}">`,
      workflow["code-review"].content,
      "</skill>",
    ].join("\n"),
    "HOST_IMPLEMENT_WORKFLOW_OK",
  );
};

export const listHostCodexSkills = async (
  repoRoot: string,
): Promise<SkillMetadata[]> => {
  const child = spawn("codex", ["app-server", "--stdio"], {
    cwd: repoRoot,
    env: process.env,
    stdio: ["pipe", "pipe", "pipe"],
  });
  let stderr = "";
  let buffer = "";
  let initialized = false;
  let settled = false;
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk: string) => {
    stderr += chunk;
  });

  return await new Promise<SkillMetadata[]>((resolvePromise, rejectPromise) => {
    const finish = (
      callback: () => void,
      error = false,
    ): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.kill("SIGTERM");
      callback();
      if (error) child.stdin.destroy();
    };
    const timer = setTimeout(
      () =>
        finish(
          () => rejectPromise(new Error("Timed out while listing host Codex skills.")),
          true,
        ),
      30_000,
    );

    child.on("error", (error) =>
      finish(() => rejectPromise(error), true),
    );
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      buffer += chunk;
      while (buffer.includes("\n")) {
        const newline = buffer.indexOf("\n");
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        let message: {
          id?: number;
          result?: {
            data?: Array<{ skills?: SkillMetadata[]; errors?: unknown[] }>;
          };
        };
        try {
          message = JSON.parse(line) as typeof message;
        } catch {
          continue;
        }
        if (message.id === 1 && !initialized) {
          initialized = true;
          child.stdin.write(`${JSON.stringify({ method: "initialized" })}\n`);
          child.stdin.write(
            `${JSON.stringify({
              id: 2,
              method: "skills/list",
              params: { cwds: [repoRoot], forceReload: true },
            })}\n`,
          );
        }
        if (message.id === 2) {
          const entry = message.result?.data?.[0];
          if (!entry || (entry.errors?.length ?? 0) > 0) {
            finish(
              () =>
                rejectPromise(
                  new Error(
                    `Codex skills/list returned errors: ${JSON.stringify(entry)}`,
                  ),
                ),
              true,
            );
            return;
          }
          finish(() => resolvePromise(entry.skills ?? []));
          return;
        }
      }
    });
    child.on("exit", (code) => {
      if (!settled) {
        finish(
          () =>
            rejectPromise(
              new Error(
                `Codex app-server exited ${code ?? "unknown"}: ${stderr.trim()}`,
              ),
            ),
          true,
        );
      }
    });
    child.stdin.write(
      `${JSON.stringify({
        id: 1,
        method: "initialize",
        params: {
          clientInfo: { name: "sandcastle-host-preflight", version: "1.0.0" },
        },
      })}\n`,
    );
  });
};

export const verifyHostSkillDiscovery = async (
  repoRoot: string,
  context = requireHostCodexEnvironment(),
): Promise<SkillMetadata[]> => {
  const discovered = await listHostCodexSkills(repoRoot);
  for (const name of MATT_SKILL_NAMES) {
    const expectedPath = join(context.skillsRoot, name, "SKILL.md");
    const match = discovered.find(
      (skill) =>
        skill.name === name &&
        skill.enabled &&
        resolve(skill.path) === resolve(expectedPath),
    );
    if (!match) {
      throw new Error(
        `Current Codex did not discover enabled Matt skill ${name} at ${expectedPath}.`,
      );
    }
  }
  return discovered.filter(({ name }) =>
    (MATT_SKILL_NAMES as readonly string[]).includes(name),
  );
};

const thisFile = fileURLToPath(import.meta.url);
if (process.argv[1] && resolve(process.argv[1]) === resolve(thisFile)) {
  const repoRoot = dirname(dirname(thisFile));
  const command = process.argv[2] ?? "--check";
  const model =
    process.argv[3] ?? process.env.SANDCASTLE_CODEX_MODEL ?? "gpt-5.6-sol";
  requireSafeControllerEnvironment(repoRoot);
  const context = requireHostCodexEnvironment();
  requireHostCodexLogin(repoRoot);
  requireAllHostAgentsIdle(repoRoot);

  if (command === "--check") {
    const discovered = await verifyHostSkillDiscovery(repoRoot, context);
    console.log(
      JSON.stringify(
        {
          execution: "host-no-docker",
          codexVersion: context.codexVersion,
          codexHome: context.codexHome,
          mattSkillsDiscovered: discovered.length,
          workflow: "implement -> tdd + code-review",
          worktreeRoot: join(repoRoot, ".sandcastle", "worktrees"),
        },
        null,
        2,
      ),
    );
  } else if (command === "--model-smoke") {
    requireHostCodexModelAccess(repoRoot, model);
    console.log(`Host Codex model smoke passed: ${model}`);
  } else if (command === "--workflow-smoke") {
    await verifyHostSkillDiscovery(repoRoot, context);
    verifyHostImplementWorkflow(repoRoot, model, context);
    console.log(`Host Matt implement workflow smoke passed: ${model}`);
  } else {
    throw new Error(
      "Usage: tsx .sandcastle/codex-host.mts [--check|--model-smoke|--workflow-smoke] [model]",
    );
  }
}
