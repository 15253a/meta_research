import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  accessSync,
  constants as fsConstants,
  existsSync,
  lstatSync,
  readFileSync,
  readlinkSync,
  realpathSync,
} from "node:fs";
import { join, relative, resolve, sep } from "node:path";

export const SANDBOX_CODEX_HOME = "/home/agent/.codex";
export const SANDBOX_CODEX_POLICY_HOME = "/opt/sandcastle-codex-policy";

const CODEX_HOME_RELATIVE_PATH = join(".sandcastle", "codex-home");
const CODEX_CONFIG_RELATIVE_PATH = join(
  ".sandcastle",
  "codex-policy",
  "config.toml",
);
const REQUIRED_CONFIG_LINES = [
  'cli_auth_credentials_store = "file"',
  'forced_login_method = "chatgpt"',
] as const;
const ALLOWED_SANDCASTLE_ENV_KEYS = new Set([
  "SANDCASTLE_CODEX_MODEL",
  "SANDCASTLE_BASE_REF",
  "SANDCASTLE_POLL_SECONDS",
  "SANDCASTLE_AGENT_WALL_CLOCK_SECONDS",
]);

export type DedicatedCodexHome = {
  hostPath: string;
  authPath: string;
  policyPath: string;
  configPath: string;
  configLinkPath: string;
};

/**
 * Sandcastle 0.12 injects every key declared in .sandcastle/.env into the
 * Agent container, and an empty value falls back to the controller process's
 * value for the same key. Keep that file restricted to documented,
 * non-secret controller settings so an obsolete GH_TOKEN= (for example)
 * cannot expose a host credential.
 */
export const requireSafeSandcastleEnvironment = (repoRoot: string): void => {
  const envPath = join(repoRoot, ".sandcastle", ".env");
  if (!existsSync(envPath)) return;

  const stat = lstatSync(envPath);
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(
      `Sandcastle environment must be a regular file, not a symlink: ${envPath}.`,
    );
  }

  const declaredKeys = new Set<string>();
  for (const rawLine of readFileSync(envPath, "utf8").split("\n")) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    if (separator === -1) continue;
    const key = line.slice(0, separator).trim();
    if (!key) {
      throw new Error(
        `Sandcastle environment contains an empty key: ${envPath}.`,
      );
    }
    declaredKeys.add(key);
  }

  const forbiddenKeys = [...declaredKeys]
    .filter((key) => !ALLOWED_SANDCASTLE_ENV_KEYS.has(key))
    .sort();
  if (forbiddenKeys.length > 0) {
    throw new Error(
      `Remove forbidden key(s) from ${envPath}: ${forbiddenKeys.join(", ")}. ` +
        "Only the documented non-secret SANDCASTLE controller settings are allowed; even an empty value can import the matching host secret.",
    );
  }
};

const requirePrivatePath = (
  path: string,
  kind: "directory" | "file",
): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(
      `Missing dedicated Codex ${kind}: ${path}. Run npm run sandcastle:login.`,
    );
  }
  if (stat.isSymbolicLink()) {
    throw new Error(`Dedicated Codex ${kind} must not be a symlink: ${path}.`);
  }
  if (kind === "directory" ? !stat.isDirectory() : !stat.isFile()) {
    throw new Error(`Dedicated Codex ${kind} has the wrong type: ${path}.`);
  }
  const requiredOwnerMode = kind === "directory" ? 0o700 : 0o600;
  if (
    (stat.mode & 0o077) !== 0 ||
    (stat.mode & requiredOwnerMode) !== requiredOwnerMode
  ) {
    throw new Error(
      `Dedicated Codex ${kind} must be private to its owner: ${path}. Run npm run sandcastle:login to repair permissions.`,
    );
  }
  const expectedUid = process.getuid?.();
  const expectedGid = process.getgid?.();
  if (
    (expectedUid !== undefined && stat.uid !== expectedUid) ||
    (expectedGid !== undefined && stat.gid !== expectedGid)
  ) {
    throw new Error(
      `Dedicated Codex ${kind} must be owned by the controller UID:GID: ${path}.`,
    );
  }
};

const requireVersionedConfig = (path: string): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(`Missing versioned Codex sandbox config: ${path}.`);
  }
  if (stat.isSymbolicLink() || !stat.isFile()) {
    throw new Error(
      `Versioned Codex sandbox config must be a regular file, not a symlink: ${path}.`,
    );
  }
  if ((stat.mode & 0o022) !== 0 || (stat.mode & 0o400) !== 0o400) {
    throw new Error(
      `Versioned Codex sandbox config must not be writable by group or others: ${path}.`,
    );
  }
};

const requireVersionedPolicyDirectory = (path: string): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(`Missing versioned Codex policy directory: ${path}.`);
  }
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error(
      `Versioned Codex policy directory must be a real directory, not a symlink: ${path}.`,
    );
  }
  if ((stat.mode & 0o022) !== 0 || (stat.mode & 0o500) !== 0o500) {
    throw new Error(
      `Versioned Codex policy directory must be owner-readable and not writable by group or others: ${path}.`,
    );
  }
};

const requireSandboxConfigLink = (path: string): void => {
  let stat;
  try {
    stat = lstatSync(path);
  } catch {
    throw new Error(
      `Missing dedicated Codex config link: ${path}. Run npm run sandcastle:login.`,
    );
  }
  if (!stat.isSymbolicLink()) {
    throw new Error(
      `Dedicated Codex config must be a symlink created by the login wizard: ${path}.`,
    );
  }
  const expectedTarget = `${SANDBOX_CODEX_POLICY_HOME}/config.toml`;
  if (readlinkSync(path) !== expectedTarget) {
    throw new Error(
      `Dedicated Codex config link must target ${expectedTarget}: ${path}.`,
    );
  }
};

export const requireDedicatedCodexHome = (
  repoRoot: string,
): DedicatedCodexHome => {
  const sandcastleRoot = resolve(repoRoot, ".sandcastle");
  const hostPath = resolve(repoRoot, CODEX_HOME_RELATIVE_PATH);
  const relativePath = relative(sandcastleRoot, hostPath);
  if (
    relativePath === "" ||
    relativePath === ".." ||
    relativePath.startsWith(`..${sep}`) ||
    resolve(sandcastleRoot, relativePath) !== hostPath
  ) {
    throw new Error("Dedicated Codex home must remain under .sandcastle/.");
  }

  requirePrivatePath(hostPath, "directory");
  if (realpathSync(hostPath) !== hostPath) {
    throw new Error(
      `Dedicated Codex home resolves outside its fixed path: ${hostPath}.`,
    );
  }
  accessSync(
    hostPath,
    fsConstants.R_OK | fsConstants.W_OK | fsConstants.X_OK,
  );

  const authPath = join(hostPath, "auth.json");
  const configPath = resolve(repoRoot, CODEX_CONFIG_RELATIVE_PATH);
  const policyPath = resolve(repoRoot, ".sandcastle", "codex-policy");
  const configLinkPath = join(hostPath, "config.toml");
  requirePrivatePath(authPath, "file");
  requireVersionedPolicyDirectory(policyPath);
  requireVersionedConfig(configPath);
  requireSandboxConfigLink(configLinkPath);
  accessSync(authPath, fsConstants.R_OK | fsConstants.W_OK);

  const config = readFileSync(configPath, "utf8");
  for (const requiredLine of REQUIRED_CONFIG_LINES) {
    if (!config.split("\n").some((line) => line.trim() === requiredLine)) {
      throw new Error(
        `Dedicated Codex config must contain ${JSON.stringify(requiredLine)}. Re-run npm run sandcastle:login.`,
      );
    }
  }

  return { hostPath, authPath, policyPath, configPath, configLinkPath };
};

export const codexSandboxAuth = (home: DedicatedCodexHome) => ({
  env: {
    HOME: "/home/agent",
    CODEX_HOME: SANDBOX_CODEX_HOME,
    // Prevent an obsolete .sandcastle/.env or host environment from silently
    // changing the dedicated ChatGPT login into API-key authentication.
    CODEX_API_KEY: "",
    CODEX_ACCESS_TOKEN: "",
    OPENAI_API_KEY: "",
  },
  mounts: [
    {
      hostPath: home.hostPath,
      sandboxPath: SANDBOX_CODEX_HOME,
    },
    {
      hostPath: home.policyPath,
      sandboxPath: SANDBOX_CODEX_POLICY_HOME,
      readonly: true,
    },
  ],
});

export const dockerCodexLoginStatusArgs = (
  imageName: string,
  home: DedicatedCodexHome,
  uid = process.getuid?.() ?? 1000,
  gid = process.getgid?.() ?? 1000,
  containerName?: string,
): string[] => [
  "run",
  "--rm",
  ...(containerName ? ["--name", containerName] : []),
  "--network=none",
  "--cap-drop=ALL",
  "--security-opt",
  "no-new-privileges",
  "--user",
  `${uid}:${gid}`,
  "--env",
  "HOME=/home/agent",
  "--env",
  `CODEX_HOME=${SANDBOX_CODEX_HOME}`,
  "--env",
  "CODEX_API_KEY=",
  "--env",
  "CODEX_ACCESS_TOKEN=",
  "--env",
  "OPENAI_API_KEY=",
  "--volume",
  `${home.hostPath}:${SANDBOX_CODEX_HOME}:rw`,
  "--volume",
  `${home.policyPath}:${SANDBOX_CODEX_POLICY_HOME}:ro`,
  "--entrypoint",
  "codex",
  imageName,
  "login",
  "status",
];

export const dockerCodexModelSmokeArgs = (
  imageName: string,
  model: string,
  home: DedicatedCodexHome,
  uid = process.getuid?.() ?? 1000,
  gid = process.getgid?.() ?? 1000,
  containerName?: string,
): string[] => [
  "run",
  "--rm",
  ...(containerName ? ["--name", containerName] : []),
  "--cap-drop=ALL",
  "--security-opt",
  "no-new-privileges",
  "--user",
  `${uid}:${gid}`,
  "--env",
  "HOME=/home/agent",
  "--env",
  `CODEX_HOME=${SANDBOX_CODEX_HOME}`,
  "--env",
  "CODEX_API_KEY=",
  "--env",
  "CODEX_ACCESS_TOKEN=",
  "--env",
  "OPENAI_API_KEY=",
  "--volume",
  `${home.hostPath}:${SANDBOX_CODEX_HOME}:rw`,
  "--volume",
  `${home.policyPath}:${SANDBOX_CODEX_POLICY_HOME}:ro`,
  "--workdir",
  "/tmp",
  "--entrypoint",
  "codex",
  imageName,
  "-a",
  "never",
  "exec",
  "--ephemeral",
  "--skip-git-repo-check",
  "--sandbox",
  "read-only",
  "--model",
  model,
  "--config",
  'model_reasoning_effort="low"',
  "Reply exactly AUTH_OK without using tools.",
];

export const requireDockerChatGptLogin = (
  repoRoot: string,
  imageName: string,
  home: DedicatedCodexHome,
): void => {
  const containerName = `sandcastle-auth-status-${randomUUID()}`;
  const result = spawnSync(
    "docker",
    dockerCodexLoginStatusArgs(
      imageName,
      home,
      process.getuid?.() ?? 1000,
      process.getgid?.() ?? 1000,
      containerName,
    ),
    { cwd: repoRoot, stdio: "ignore", timeout: 30_000, killSignal: "SIGTERM" },
  );
  spawnSync("docker", ["rm", "--force", containerName], {
    cwd: repoRoot,
    stdio: "ignore",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      "The sandbox image cannot reuse the dedicated ChatGPT login. Run npm run sandcastle:login.",
    );
  }
};

export const requireDockerCodexModelAccess = (
  repoRoot: string,
  imageName: string,
  model: string,
  home: DedicatedCodexHome,
): void => {
  const containerName = `sandcastle-model-smoke-${randomUUID()}`;
  const result = spawnSync(
    "docker",
    dockerCodexModelSmokeArgs(
      imageName,
      model,
      home,
      process.getuid?.() ?? 1000,
      process.getgid?.() ?? 1000,
      containerName,
    ),
    {
      cwd: repoRoot,
      stdio: "ignore",
      timeout: 120_000,
      killSignal: "SIGTERM",
    },
  );
  spawnSync("docker", ["rm", "--force", containerName], {
    cwd: repoRoot,
    stdio: "ignore",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `The dedicated ChatGPT login cannot run model ${model}. Re-run npm run sandcastle:login -- --force or choose an available model.`,
    );
  }
};
