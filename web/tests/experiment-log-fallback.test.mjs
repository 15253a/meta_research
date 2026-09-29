import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import vm from "node:vm";
import ts from "typescript";

// Execute the actual launcher and scope-filter bodies without mounting the app,
// starting a browser, fetching APIs, or fabricating a Bundle projection.
const source = readFileSync(new URL("../src/main.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("main.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const names = ["stageProjectionMatchesForeground", "allStageSurfaces", "ExperimentLogLauncher"];
const bodies = names.map(name => {
  const node = ast.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
  assert.ok(node, `actual ${name} must be tested`);
  return node.getText(ast);
}).join("\n");
const compiled = ts.transpileModule(bodies + "\nexports.launcher = ExperimentLogLauncher;", {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const scope = { quest_ref: "quest:15", cycle_ref: "cycle:15", question_ref: "question:1", stage: "reasoning" };
const target = { target_ref: "target:1", target_key: "T1 exact FTD audit", target_run_ref: "target-run:1", status: "committed" };
const session = { session_ref: "target-root:1", kind: "target", title: "T1 exact FTD audit", short_title: "T1",
  cycle_ref: scope.cycle_ref, question_ref: scope.question_ref, target_ref: target.target_ref, run_ref: target.target_run_ref,
  stage: "bundle", status: "completed", is_current: false, is_executing: false, operations: [] };
function fixtures() {
  const selections = [];
  return {
    snapshot: { research_control: { foreground: { ...scope } }, bundle_stage: {
      eligibility: { cycle_ref: "cycle:old", question_ref: scope.question_ref }, target_graph: { targets: [target] },
    } },
    blocked: false, paused: false, observationPointers: {},
    rootConversations: { context: { foreground: { ...scope }, stale: false }, error: null,
      data: { quest_ref: scope.quest_ref, limited: false, sessions: [session] },
      selectStage: (stage, ref) => selections.push([stage, ref]),
    }, selections,
  };
}
function mount(props) {
  const state = [], effects = [], refs = [];
  let cursor = 0, pending = [], tree, scrolls = 0;
  const context = { exports: {}, require: createRequire(import.meta.url),
    useState(initial) { const i = cursor++; if (!(i in state)) state[i] = initial;
      return [state[i], value => state[i] = typeof value === "function" ? value(state[i]) : value]; },
    useRef(initial) { const i = cursor++; return refs[i] ??= { current: initial }; },
    useEffect(effect, deps) { const i = cursor++; if (!effects[i] || deps.some((v, k) => v !== effects[i][k])) {
      effects[i] = deps; pending.push(effect);
    } },
    document: { getElementById: () => ({ focus() {}, scrollIntoView() { scrolls++; } }) },
    TargetTerminalDialog: "TargetTerminalDialog", ExperimentLogs: "ExperimentLogs",
  };
  vm.runInNewContext(compiled, context);
  const render = () => { cursor = 0; tree = context.exports.launcher(props); return tree; };
  const flush = () => { render(); const tasks = pending; pending = []; tasks.forEach(task => task()); return render(); };
  flush();
  return { props, render: flush, get tree() { return tree; }, get scrolls() { return scrolls; } };
}
function elements(node, type) {
  if (Array.isArray(node)) return node.flatMap(child => elements(child, type));
  if (!node || typeof node !== "object") return [];
  return [...(node.type === type ? [node] : []), ...elements(node.props?.children, type)];
}
const text = node => Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object" ? text(node.props?.children) : node ?? "";
const button = model => elements(model.tree, "button")[0];

test("current Target public records remain reachable when Bundle belongs to an older cycle", () => {
  const props = fixtures(), model = mount(props);
  assert.equal(button(model).props.disabled, false);
  assert.match(text(button(model)), /实验工作记录/);
  assert.doesNotMatch(text(model.tree), /本轮尚无实验日志/);
  button(model).props.onClick();
  assert.deepEqual(props.selections, [["bundle", session.session_ref]]);
  assert.equal(model.scrolls, 1);
  assert.equal(elements(model.tree, "TargetTerminalDialog").length, 0);
});

test("normal Bundle Target retains execution records and log-file dialogs", () => {
  const props = fixtures(); props.snapshot.bundle_stage.eligibility.cycle_ref = scope.cycle_ref;
  const model = mount(props);
  assert.equal(button(model).props.disabled, false); assert.match(text(button(model)), /实验日志/);
  button(model).props.onClick(); model.render();
  const dialog = elements(model.tree, "TargetTerminalDialog")[0];
  assert.equal(dialog.props.target.target_ref, target.target_ref);
  dialog.props.onShowLogFiles(); model.render();
  assert.equal(elements(model.tree, "ExperimentLogs")[0].props.target.target_ref, target.target_ref);
  assert.deepEqual(props.selections, []);
});

test("uncertain, foreign and missing session scope cannot claim a current public record", () => {
  for (const change of [
    p => p.rootConversations.context.stale = true,
    p => p.rootConversations.error = "root_session_timeout",
    p => p.rootConversations.data.quest_ref = "other-quest",
    p => p.rootConversations.context.foreground.question_ref = "other-question",
    p => p.rootConversations.data.sessions = [{ ...session, cycle_ref: "old-cycle" }],
    p => p.rootConversations.data.sessions = [{ ...session, question_ref: null }],
    p => p.rootConversations.data.sessions = [{ ...session, kind: "stage" }],
    p => p.rootConversations.data.sessions = [{ ...session, run_ref: null }],
  ]) {
    const props = fixtures(); change(props); const model = mount(props);
    assert.equal(button(model).props.disabled, true);
    if (props.rootConversations.error || props.rootConversations.context.stale)
      assert.doesNotMatch(text(model.tree), /本轮尚无实验日志/);
  }
});

test("multiple current Target sessions are selectable; exact observed records survive unrelated index limits", () => {
  const props = fixtures(); props.rootConversations.data.limited = true;
  props.rootConversations.data.sessions.push({ ...session, session_ref: "target-root:2", target_ref: "target:2", title: "T2 audit" });
  const model = mount(props), select = elements(model.tree, "select")[0];
  assert.ok(select); select.props.onChange({ currentTarget: { value: "target-root:2" } }); model.render();
  button(model).props.onClick(); assert.deepEqual(props.selections, [["bundle", "target-root:2"]]);
});

test("Quest, Cycle and Question changes clear prior Target selection", () => {
  for (const field of ["quest_ref", "cycle_ref", "question_ref"]) {
    const props = fixtures(); props.rootConversations.data.sessions.push({ ...session, session_ref: "target-root:2", target_ref: "target:2" });
    const model = mount(props); elements(model.tree, "select")[0].props.onChange({ currentTarget: { value: "target-root:2" } }); model.render();
    props.snapshot.research_control.foreground[field] += ":new";
    props.rootConversations.context.foreground = { ...props.snapshot.research_control.foreground };
    props.rootConversations.data.quest_ref = props.snapshot.research_control.foreground.quest_ref;
    props.rootConversations.data.sessions = props.rootConversations.data.sessions.map(s => ({ ...s,
      cycle_ref: props.snapshot.research_control.foreground.cycle_ref, question_ref: props.snapshot.research_control.foreground.question_ref }));
    model.render(); button(model).props.onClick(); assert.deepEqual(props.selections.at(-1), ["bundle", "target-root:1"]);
  }
});
