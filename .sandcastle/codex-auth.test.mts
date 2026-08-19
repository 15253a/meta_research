import assert from "node:assert/strict";
import {
  chmodSync,
  mkdtempSync,
  mkdirSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";
import { rmSync } from "node:fs";
import {
  SANDBOX_CODEX_HOME,
  SANDBOX_CODEX_POLICY_HOME,
  codexSandboxAuth,
  dockerCodexLoginStatusArgs,
  dockerCodexModelSmokeArgs,
  requireDedicatedCodexHome,
  requireSafeSandcastleEnvironment,
} from "./codex-auth.mts";

const temporaryRoots: string[] = [];

const fixture = (): { repoRoot: string; codexHome: string } => {
  const repoRoot = mkdtempSync(join(tmpdir(), "sandcastle-auth-test-"));
  temporaryRoots.push(repoRoot);
  const sandcastle = join(repoRoot, ".sandcastle");
  const codexHome = join(sandcastle, "codex-home");
  const policyHome = join(sandcastle, "codex-policy");
  mkdirSync(codexHome, { recursive: true, mode: 0o700 });
  mkdirSync(policyHome, { recursive: true, mode: 0o755 });
  writeFileSync(join(codexHome, "auth.json"), "{}\n", { mode: 0o600 });
  writeFileSync(
    join(policyHome, "config.toml"),
    'cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n',
    { mode: 0o644 },
  );
  symlinkSync(
    `${SANDBOX_CODEX_POLICY_HOME}/config.toml`,
    join(codexHome, "config.toml"),
  );
  return { repoRoot, codexHome };
};

afterEach(() => {
  for (const path of temporaryRoots.splice(0)) {
    rmSync(path, { recursive: true, force: true });
  }
});

test("accepts a private dedicated ChatGPT login home", () => {
  const { repoRoot, codexHome } = fixture();
  const home = requireDedicatedCodexHome(repoRoot);

  assert.equal(home.hostPath, codexHome);
  assert.deepEqual(codexSandboxAuth(home), {
    env: {
      HOME: "/home/agent",
      CODEX_HOME: SANDBOX_CODEX_HOME,
      CODEX_API_KEY: "",
      CODEX_ACCESS_TOKEN: "",
      OPENAI_API_KEY: "",
    },
    mounts: [
      { hostPath: codexHome, sandboxPath: SANDBOX_CODEX_HOME },
      {
        hostPath: join(repoRoot, ".sandcastle", "codex-policy"),
        sandboxPath: SANDBOX_CODEX_POLICY_HOME,
        readonly: true,
      },
    ],
  });
});

test("rejects missing or symlinked auth material", () => {
  const { repoRoot, codexHome } = fixture();
  const authPath = join(codexHome, "auth.json");
  rmSync(authPath);
  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /Missing dedicated Codex file/,
  );

  const externalAuth = join(repoRoot, "external-auth.json");
  writeFileSync(externalAuth, "{}\n", { mode: 0o600 });
  symlinkSync(externalAuth, authPath);
  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /must not be a symlink/,
  );
});

test("requires the dedicated ChatGPT file-store policy", () => {
  const { repoRoot, codexHome } = fixture();
  writeFileSync(
    join(repoRoot, ".sandcastle", "codex-policy", "config.toml"),
    "model = \"gpt-5.4\"\n",
    { mode: 0o644 },
  );

  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /cli_auth_credentials_store/,
  );
});

test("rejects a symlinked policy directory", () => {
  const { repoRoot } = fixture();
  const policyHome = join(repoRoot, ".sandcastle", "codex-policy");
  const externalPolicy = join(repoRoot, "external-policy");
  rmSync(policyHome, { recursive: true });
  mkdirSync(externalPolicy, { mode: 0o755 });
  writeFileSync(
    join(externalPolicy, "config.toml"),
    'cli_auth_credentials_store = "file"\nforced_login_method = "chatgpt"\n',
    { mode: 0o644 },
  );
  symlinkSync(externalPolicy, policyHome);

  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /policy directory must be a real directory/,
  );
});

test("requires the fixed sandbox-policy config symlink", () => {
  const { repoRoot, codexHome } = fixture();
  rmSync(join(codexHome, "config.toml"));
  writeFileSync(join(codexHome, "config.toml"), "model = \"gpt-5.4\"\n", {
    mode: 0o600,
  });

  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /config must be a symlink/,
  );
});

test("rejects credentials that are readable by another user", () => {
  const { repoRoot, codexHome } = fixture();
  chmodSync(join(codexHome, "auth.json"), 0o644);

  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /must be private to its owner/,
  );
});

test("requires owner traversal on the dedicated home", () => {
  const { repoRoot, codexHome } = fixture();
  chmodSync(codexHome, 0o600);

  assert.throws(
    () => requireDedicatedCodexHome(repoRoot),
    /must be private to its owner/,
  );
});

test("builds an isolated, API-key-free Docker auth check", () => {
  const { repoRoot, codexHome } = fixture();
  const home = requireDedicatedCodexHome(repoRoot);
  const args = dockerCodexLoginStatusArgs("sandcastle:test", home, 123, 456);

  assert.deepEqual(args.slice(0, 8), [
    "run",
    "--rm",
    "--network=none",
    "--cap-drop=ALL",
    "--security-opt",
    "no-new-privileges",
    "--user",
    "123:456",
  ]);
  assert.ok(args.includes("CODEX_API_KEY="));
  assert.ok(args.includes("CODEX_ACCESS_TOKEN="));
  assert.ok(args.includes("OPENAI_API_KEY="));
  assert.ok(args.includes(`${codexHome}:${SANDBOX_CODEX_HOME}:rw`));
  assert.ok(
    args.includes(
      `${join(repoRoot, ".sandcastle", "codex-policy")}:${SANDBOX_CODEX_POLICY_HOME}:ro`,
    ),
  );
  assert.deepEqual(args.slice(-3), ["sandcastle:test", "login", "status"]);
});

test("builds an ephemeral model smoke test outside the repository", () => {
  const { repoRoot } = fixture();
  const home = requireDedicatedCodexHome(repoRoot);
  const args = dockerCodexModelSmokeArgs(
    "sandcastle:test",
    "gpt-5.4",
    home,
    123,
    456,
  );

  assert.ok(args.includes("/tmp"));
  assert.ok(args.includes("--ephemeral"));
  assert.ok(args.includes("--skip-git-repo-check"));
  assert.ok(args.includes("read-only"));
  assert.ok(args.includes("gpt-5.4"));
  assert.equal(args.includes(repoRoot), false);
});

test("allows only documented non-secret Sandcastle environment keys", () => {
  const { repoRoot } = fixture();
  writeFileSync(
    join(repoRoot, ".sandcastle", ".env"),
    [
      "# controller settings",
      "SANDCASTLE_CODEX_MODEL=gpt-5.4",
      "SANDCASTLE_BASE_REF=develop_main",
      "SANDCASTLE_POLL_SECONDS=60",
      "SANDCASTLE_AGENT_WALL_CLOCK_SECONDS=28800",
      "",
    ].join("\n"),
    { mode: 0o600 },
  );

  assert.doesNotThrow(() => requireSafeSandcastleEnvironment(repoRoot));
});

test("rejects secret-looking and empty fallback environment entries", () => {
  for (const entry of ["GH_TOKEN=secret", "GH_TOKEN="]) {
    const { repoRoot } = fixture();
    writeFileSync(join(repoRoot, ".sandcastle", ".env"), `${entry}\n`, {
      mode: 0o600,
    });

    assert.throws(
      () => requireSafeSandcastleEnvironment(repoRoot),
      /Remove forbidden key\(s\).*GH_TOKEN.*empty value can import/,
    );
  }
});
