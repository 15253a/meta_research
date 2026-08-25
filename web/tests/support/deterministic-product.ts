import type { Page } from "@playwright/test";
import { spawn, type ChildProcessByStdio } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
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
  stagePipeline?:
    | "plan-gap"
    | "bundle-exhaustion"
    | "reasoning-no-evidence"
    | "reasoning-autonomous"
    | "quest-completion";
  writingDeliveryFaults?: "history-boundaries";
};

export type PlanProviderPhase = "plan-primary" | "plan-review";
export type ReasoningProviderPhase = "reasoning-primary" | "reasoning-review";

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
    private readonly processClose: Promise<void>,
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
    if (options.stagePipeline) {
      argv.push("--stage-pipeline", options.stagePipeline);
    }
    if (options.writingDeliveryFaults) {
      argv.push("--writing-delivery-faults", options.writingDeliveryFaults);
    }
    const child = spawn(
      "uv",
      argv,
      {
        cwd: repositoryRoot,
        detached: process.platform !== "win32",
        env: { ...process.env, PYTHONUNBUFFERED: "1" },
        stdio: ["ignore", "pipe", "pipe"],
      },
    );
    // `uv run` may report its own exit before the Python child that inherited
    // these pipes has stopped. Capture `close` immediately so teardown waits
    // for both the wrapper and its inherited stdio writers to quiesce.
    const childClose = new Promise<void>((resolveClose) => {
      child.once("close", () => resolveClose());
    });
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
        childClose,
        processErrors,
      );
    } catch (error) {
      const closed = await terminateAndWait(
        child,
        childClose,
        "SIGKILL",
        5_000,
      );
      if (!closed) {
        throw new Error("failed deterministic product did not close after SIGKILL", {
          cause: error,
        });
      }
      rmSync(dataRoot, {
        recursive: true,
        force: true,
        maxRetries: 5,
        retryDelay: 50,
      });
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

  async waitForPlanProviderPhase(
    phase: PlanProviderPhase,
    timeoutMs = 20_000,
  ): Promise<void> {
    const marker = this.providerMarker(phase, "started");
    const deadline = Date.now() + timeoutMs;
    while (!existsSync(marker)) {
      if (this.unexpectedExit || this.process.exitCode !== null) {
        throw new Error(
          `deterministic product exited while waiting for ${phase}`,
        );
      }
      if (Date.now() >= deadline) {
        throw new Error(`timed out waiting for deterministic ${phase}`);
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 25));
    }
  }

  releasePlanProviderPhase(phase: PlanProviderPhase): void {
    writeFileSync(this.providerMarker(phase, "release"), "release\n", {
      encoding: "utf8",
    });
  }

  async waitForReasoningProviderPhase(
    phase: ReasoningProviderPhase,
    timeoutMs = 20_000,
  ): Promise<void> {
    const marker = this.providerMarker(phase, "started");
    const deadline = Date.now() + timeoutMs;
    while (!existsSync(marker)) {
      if (this.unexpectedExit || this.process.exitCode !== null) {
        throw new Error(
          `deterministic product exited while waiting for ${phase}`,
        );
      }
      if (Date.now() >= deadline) {
        throw new Error(`timed out waiting for deterministic ${phase}`);
      }
      await new Promise((resolveDelay) => setTimeout(resolveDelay, 25));
    }
  }

  releaseReasoningProviderPhase(phase: ReasoningProviderPhase): void {
    writeFileSync(this.providerMarker(phase, "release"), "release\n", {
      encoding: "utf8",
    });
  }

  prepareWritingDeliveryTarget(fileName: string): string {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/.test(fileName)) {
      throw new Error("invalid deterministic Writing delivery file name");
    }
    const directory = join(this.dataRoot, "writing-deliveries");
    mkdirSync(directory, { recursive: true, mode: 0o700 });
    return join(directory, fileName);
  }

  writingDeliveryExists(fileName: string): boolean {
    return existsSync(this.writingDeliveryPath(fileName));
  }

  readWritingDelivery(fileName: string): string {
    return readFileSync(this.writingDeliveryPath(fileName), "utf8");
  }

  readWritingDeliveryBytes(fileName: string): Buffer {
    return readFileSync(this.writingDeliveryPath(fileName));
  }

  damageWritingAssetCustody(contentHash: string): void {
    if (!/^[0-9a-f]{64}$/.test(contentHash)) {
      throw new Error("invalid deterministic Writing asset hash");
    }
    const objectPath = join(
      this.dataRoot,
      "objects",
      "sha256",
      "assets",
      contentHash.slice(0, 2),
      contentHash,
    );
    if (!existsSync(objectPath)) {
      throw new Error("deterministic Writing asset custody is missing");
    }
    rmSync(objectPath);
  }

  private writingDeliveryPath(fileName: string): string {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$/.test(fileName)) {
      throw new Error("invalid deterministic Writing delivery file name");
    }
    return join(this.dataRoot, "writing-deliveries", fileName);
  }

  private providerMarker(
    phase: PlanProviderPhase | ReasoningProviderPhase,
    state: "started" | "release",
  ): string {
    return join(
      this.dataRoot,
      "run",
      "chrome-provider-control",
      `${phase}.${state}`,
    );
  }

  async stop(): Promise<void> {
    if (this.unexpectedExit || this.process.exitCode !== null || this.process.signalCode !== null) {
      const detail = this.unexpectedExit ??
        `code=${String(this.process.exitCode)} signal=${String(this.process.signalCode)}`;
      if (!await terminateAndWait(
        this.process,
        this.processClose,
        "SIGKILL",
        5_000,
      )) {
        throw new Error(
          `deterministic product exited before teardown without close quiescence (${detail})`,
        );
      }
      rmSync(this.dataRoot, {
        recursive: true,
        force: true,
        maxRetries: 5,
        retryDelay: 50,
      });
      throw new Error(`deterministic product exited before teardown (${detail})`);
    }
    this.stopRequested = true;
    if (this.process.exitCode === null && this.process.signalCode === null) {
      const stopped = await terminateAndWait(
        this.process,
        this.processClose,
        "SIGTERM",
        5_000,
      );
      if (!stopped) {
        if (!await terminateAndWait(
          this.process,
          this.processClose,
          "SIGKILL",
          5_000,
        )) {
          throw new Error("deterministic product did not close after SIGKILL");
        }
      }
    }
    rmSync(this.dataRoot, {
      recursive: true,
      force: true,
      maxRetries: 5,
      retryDelay: 50,
    });
    if (this.processErrors.length) {
      throw new Error("deterministic product child process emitted an error", {
        cause: this.processErrors[0],
      });
    }
  }
}

async function terminateAndWait(
  child: ChildProcessByStdio<null, Readable, Readable>,
  childClose: Promise<void>,
  signal: NodeJS.Signals,
  timeoutMs: number,
): Promise<boolean> {
  const closed = waitForClose(childClose, timeoutMs);
  signalProcessTree(child, signal);
  return closed;
}

function signalProcessTree(
  child: ChildProcessByStdio<null, Readable, Readable>,
  signal: NodeJS.Signals,
): void {
  if (process.platform !== "win32" && child.pid !== undefined) {
    try {
      process.kill(-child.pid, signal);
      return;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
      throw error;
    }
  }
  if (child.exitCode === null && child.signalCode === null) child.kill(signal);
}

async function waitForClose(
  childClose: Promise<void>,
  timeoutMs: number,
): Promise<boolean> {
  let timeout: NodeJS.Timeout | undefined;
  try {
    return await Promise.race([
      childClose.then(() => true),
      new Promise<boolean>((resolveTimeout) => {
        timeout = setTimeout(() => resolveTimeout(false), timeoutMs);
      }),
    ]);
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
  }
}

export async function openAuthenticatedProduct(
  page: Page,
  product: DeterministicProduct,
): Promise<void> {
  await product.authenticate(page);
  await page.goto(product.baseUrl, { waitUntil: "domcontentloaded" });
}
