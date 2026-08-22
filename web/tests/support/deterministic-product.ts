import type { Page } from "@playwright/test";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import type { Readable } from "node:stream";
import { fileURLToPath } from "node:url";


type ProductStart = {
  base_url: string;
  bootstrap_token: string;
};

type DeterministicProductOptions = {
  legacyState?: "draft" | "recovering";
  manualRoot?: boolean;
};

const supportDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(supportDirectory, "../../..");
const serverScript = resolve(supportDirectory, "deterministic_product.py");

export class DeterministicProduct {
  private stopRequested = false;
  private unexpectedExit: string | null = null;

  private constructor(
    readonly baseUrl: string,
    private readonly bootstrapToken: string,
    private readonly dataRoot: string,
    private readonly process: ChildProcessByStdio<null, Readable, Readable>,
    private readonly processErrors: Error[],
  ) {
    process.once("exit", (code, signal) => {
      if (!this.stopRequested) {
        this.unexpectedExit = `code=${String(code)} signal=${String(signal)}`;
      }
    });
  }

  static async start(
    options: DeterministicProductOptions = {},
  ): Promise<DeterministicProduct> {
    const dataRoot = mkdtempSync(join(tmpdir(), "meta-research-chrome-states-"));
    const argv = ["run", "python", serverScript, "--data-root", dataRoot];
    const sourceWebRoot = process.env.META_RESEARCH_TEST_WEB_ROOT;
    if (sourceWebRoot) {
      argv.push("--web-root", sourceWebRoot);
    }
    if (options.legacyState) {
      argv.push("--legacy-state", options.legacyState);
    }
    if (options.manualRoot) {
      argv.push("--manual-root");
    }
    const child = spawn(
      "uv",
      argv,
      {
        cwd: repositoryRoot,
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    let stderr = "";
    const processErrors: Error[] = [];
    child.on("error", (error) => {
      processErrors.push(error);
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });

    try {
      const started = await new Promise<ProductStart>((resolveStart, reject) => {
        const lines = createInterface({ input: child.stdout });
        const cleanup = () => {
          clearTimeout(timeout);
          child.off("error", onError);
          child.off("exit", onExit);
          lines.close();
        };
        const onError = (error: Error) => {
          cleanup();
          reject(new Error(`deterministic product spawn failed\n${stderr}`, {
            cause: error,
          }));
        };
        const onExit = (code: number | null, signal: NodeJS.Signals | null) => {
          cleanup();
          reject(new Error(
            `deterministic product exited (code=${String(code)} signal=${String(signal)})\n${stderr}`,
          ));
        };
        const timeout = setTimeout(() => {
          cleanup();
          reject(new Error(`deterministic product startup timed out\n${stderr}`));
        }, 20_000);
        child.once("error", onError);
        child.once("exit", onExit);
        lines.once("line", (line) => {
          cleanup();
          try {
            resolveStart(JSON.parse(line) as ProductStart);
          } catch (error) {
            reject(new Error(`invalid deterministic product handshake: ${line}`, {
              cause: error,
            }));
          }
        });
      });
      return new DeterministicProduct(
        started.base_url,
        started.bootstrap_token,
        dataRoot,
        child,
        processErrors,
      );
    } catch (error) {
      const exited = child.exitCode !== null || child.signalCode !== null
        ? true
        : await terminateAndWait(child, "SIGKILL", 5_000);
      if (!exited) {
        throw new Error("failed deterministic product did not exit after SIGKILL", {
          cause: error,
        });
      }
      rmSync(dataRoot, { recursive: true, force: true });
      throw error;
    }
  }

  async authenticate(page: Page): Promise<void> {
    const response = await page.request.post(`${this.baseUrl}/auth/bootstrap`, {
      data: { token: this.bootstrapToken },
      headers: { Origin: this.baseUrl },
    });
    if (!response.ok()) {
      throw new Error(`deterministic product authentication failed: ${response.status()}`);
    }
  }

  damageQuestionContentCustody(): void {
    const custodyRoot = join(
      this.dataRoot,
      "objects",
      "sha256",
      "formal-question-content",
    );
    if (!existsSync(custodyRoot)) {
      throw new Error("deterministic product has no accepted question content to damage");
    }
    rmSync(custodyRoot, { recursive: true });
  }

  async stop(): Promise<void> {
    if (this.unexpectedExit || this.process.exitCode !== null || this.process.signalCode !== null) {
      const detail = this.unexpectedExit ??
        `code=${String(this.process.exitCode)} signal=${String(this.process.signalCode)}`;
      rmSync(this.dataRoot, { recursive: true, force: true });
      throw new Error(`deterministic product exited before teardown (${detail})`);
    }
    this.stopRequested = true;
    if (this.process.exitCode === null && this.process.signalCode === null) {
      const stopped = await terminateAndWait(this.process, "SIGTERM", 5_000);
      if (!stopped) {
        if (!await terminateAndWait(this.process, "SIGKILL", 5_000)) {
          throw new Error("deterministic product did not exit after SIGKILL");
        }
      }
    }
    rmSync(this.dataRoot, { recursive: true, force: true });
    if (this.processErrors.length) {
      throw new Error("deterministic product child process emitted an error", {
        cause: this.processErrors[0],
      });
    }
  }
}

async function terminateAndWait(
  child: ChildProcessByStdio<null, Readable, Readable>,
  signal: NodeJS.Signals,
  timeoutMs: number,
): Promise<boolean> {
  const exited = waitForExit(child, timeoutMs);
  child.kill(signal);
  return exited;
}

async function waitForExit(
  child: ChildProcessByStdio<null, Readable, Readable>,
  timeoutMs: number,
): Promise<boolean> {
  if (child.exitCode !== null || child.signalCode !== null) return true;
  return new Promise<boolean>((resolveExit) => {
    const finish = (exited: boolean) => {
      clearTimeout(timeout);
      child.off("exit", onExit);
      child.off("close", onExit);
      resolveExit(exited);
    };
    const onExit = () => finish(true);
    const timeout = setTimeout(() => finish(false), timeoutMs);
    child.once("exit", onExit);
    child.once("close", onExit);
    if (child.exitCode !== null || child.signalCode !== null) finish(true);
  });
}

export async function openAuthenticatedProduct(
  page: Page,
  product: DeterministicProduct,
): Promise<void> {
  await product.authenticate(page);
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
}
