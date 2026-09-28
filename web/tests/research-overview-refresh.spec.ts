import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import type { PublicSnapshot } from "../src/api.js";
import type { useResearchOverview } from "../src/ResearchOverview.js";

// Execute the real hook, including its dependency arrays and cleanup, without
// a browser or HTTP server. Deferred responses reproduce reads slower than
// same-scope snapshot updates; the scheduler supplies only React's hook seam.
const sourcePath = resolve(process.cwd(), "src/ResearchOverview.tsx");
const source = readFileSync(sourcePath, "utf8");
const parsed = ts.createSourceFile(sourcePath, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node)
  && ["overviewQuestRef", "useResearchOverview"].includes(node.name?.text ?? ""));
expect(functions).toHaveLength(2);
const javascript = ts.transpileModule(functions.map(node => node.getText(parsed)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

type Slot = { value?: unknown; deps?: readonly unknown[]; cleanup?: () => void };
type Reply = { ok: boolean; status: number; json: () => Promise<unknown> };
type Read = { signal: AbortSignal; finish: (status?: number, questRef?: string) => void };
const settle = async () => { for (let index = 0; index < 8; index++) await Promise.resolve(); };

function harness() {
  const slots: Slot[] = [], effects: Array<() => void> = [], reads: Read[] = [];
  let cursor = 0, scope = "one";
  const same = (a: readonly unknown[] | undefined, b: readonly unknown[]) => a?.length === b.length
    && a.every((value, index) => Object.is(value, b[index]));
  const sandbox = {
    exports: {} as { useResearchOverview: typeof useResearchOverview }, AbortController, URLSearchParams,
    useState<T>(initial: T | (() => T)) {
      const index = cursor++;
      slots[index] ??= { value: typeof initial === "function" ? (initial as () => T)() : initial };
      return [slots[index].value, (value: T | ((previous: T) => T)) => {
        slots[index].value = typeof value === "function" ? (value as (previous: T) => T)(slots[index].value as T) : value;
      }];
    },
    useRef(value: unknown) { const index = cursor++; slots[index] ??= { value: { current: value } }; return slots[index].value; },
    useCallback(value: unknown, deps: readonly unknown[]) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = { value, deps };
      return slots[index].value;
    },
    useEffect(effect: () => (() => void) | undefined, deps: readonly unknown[]) {
      const index = cursor++;
      if (same(slots[index]?.deps, deps)) return;
      const previous = slots[index]; slots[index] = { deps };
      effects.push(() => { previous?.cleanup?.(); slots[index].cleanup = effect(); });
    },
    fetch(_url: string, options: { signal: AbortSignal }) {
      const capturedScope = scope;
      return new Promise<Reply>((resolveReply, reject) => {
        reads.push({ signal: options.signal, finish: (status = 200, questRef = capturedScope) => resolveReply({
          ok: status === 200, status, json: async () => ({
            schema_ref: "meta-research/research-overview/v1", quest_ref: questRef,
            cycle_ref: capturedScope, question_ref: capturedScope,
            cycles: [], findings: { quest: [], question: [], cycle: [] },
          }),
        }) });
        options.signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
      });
    },
  };
  runInNewContext(javascript, sandbox, { filename: sourcePath });
  return {
    reads,
    render(revision = 1, options: { scope?: string; active?: boolean; result?: string; expanded?: boolean } = {}) {
      cursor = 0; scope = options.scope ?? "one";
      const snapshot = {
        revision, research_space: { current_quest: { status: "ready", quest_ref: scope } },
        research_control: { foreground: { quest_ref: scope, cycle_ref: scope, question_ref: scope, stage: "reasoning", epoch: 15 } },
        plan_stage: { stage_commit: options.result ?? null },
      } as unknown as PublicSnapshot;
      const result = sandbox.exports.useResearchOverview(snapshot, options.active ?? true, options.expanded ?? true);
      effects.splice(0).forEach(effect => effect());
      return result;
    },
    unmount() { slots.forEach(slot => slot.cleanup?.()); },
  };
}

test("same-scope revisions deliver the in-flight history before one coalesced refresh", async () => {
  const hook = harness();
  hook.render(1); hook.render(2); hook.render(3);
  expect(hook.reads).toHaveLength(1);
  expect(hook.reads[0].signal.aborted).toBe(false);
  hook.reads[0].finish(); await settle();
  expect(hook.render(3).data?.quest_ref).toBe("one");
  expect(hook.reads).toHaveLength(2);
  hook.reads[1].finish(); await settle();
  expect(hook.render(3).loading).toBe(false);
  expect(hook.reads).toHaveLength(2);
  hook.unmount();
});

test("formal result changes refresh after delivery even when heartbeat refresh is off", async () => {
  const hook = harness();
  hook.render(1, { expanded: false }); hook.render(2, { expanded: false, result: "accepted" });
  expect(hook.reads).toHaveLength(1);
  hook.reads[0].finish(); await settle();
  expect(hook.reads).toHaveLength(2);
  hook.reads[1].finish(); await settle();
  expect(hook.render(3, { expanded: false, result: "accepted" }).data).not.toBeNull();
  expect(hook.reads).toHaveLength(2);
  hook.unmount();
});

test("scope changes and leaving the workspace cancel old reads without publishing late data", async () => {
  const hook = harness();
  hook.render(); hook.render(2, { scope: "two" });
  expect(hook.reads[0].signal.aborted).toBe(true);
  hook.reads[0].finish(); await settle();
  expect(hook.render(2, { scope: "two" }).data).toBeNull();
  hook.reads[1].finish(); await settle();
  expect(hook.render(2, { scope: "two" }).data?.quest_ref).toBe("two");
  hook.render(3, { scope: "two" });
  hook.render(3, { scope: "two", active: false });
  expect(hook.reads.at(-1)?.signal.aborted).toBe(true);
  hook.unmount();
});

test("a failed read keeps its error and explicit retry starts a new request", async () => {
  const hook = harness(); hook.render();
  hook.reads[0].finish(503); await settle();
  const failed = hook.render();
  expect(failed.error).toBe("research_overview_unavailable:503");
  expect(failed.loading).toBe(false);
  failed.retry(); hook.render();
  expect(hook.reads).toHaveLength(2);
  hook.reads[1].finish(); await settle();
  expect(hook.render().data?.quest_ref).toBe("one");
  expect(hook.render().error).toBeNull();
  hook.unmount();
});

test("a response with another scope is rejected and queued updates still get their own read", async () => {
  const hook = harness(); hook.render(); hook.render(2);
  hook.reads[0].finish(200, "other"); await settle();
  expect(hook.render(2).data).toBeNull();
  expect(hook.reads).toHaveLength(2);
  hook.reads[1].finish(); await settle();
  expect(hook.render(2).data?.quest_ref).toBe("one");
  hook.unmount();
});
