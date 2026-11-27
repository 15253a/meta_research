import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { runInNewContext } from "node:vm";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const ts = require("typescript");

const sourceUrl = new URL("../src/HumanCollaboration.tsx", import.meta.url);
const shellCssUrl = new URL("../src/shell.css", import.meta.url);
const modalCssUrl = new URL("../src/workspace-modal.css", import.meta.url);

const source = readFileSync(sourceUrl, "utf8");
const shellCss = readFileSync(shellCssUrl, "utf8");
const modalCss = readFileSync(modalCssUrl, "utf8");

const parsed = ts.createSourceFile(
  sourceUrl.pathname,
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function extractDeclarations(names) {
  const wanted = new Set(names);
  const nodes = parsed.statements.filter((node) => {
    if (ts.isFunctionDeclaration(node) && node.name && wanted.has(node.name.text)) return true;
    if (ts.isVariableStatement(node)) {
      return node.declarationList.declarations.some(
        (decl) => ts.isIdentifier(decl.name) && wanted.has(decl.name.text),
      );
    }
    return false;
  });
  return ts.transpileModule(
    nodes.map((node) => node.getText(parsed)).join("\n"),
    {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.React,
      },
    },
  ).outputText;
}

async function mountWithHooks(componentName, extraDeclarations, props, overrides = {}) {
  const code = extractDeclarations([componentName, ...extraDeclarations]);
  const stateSlots = [];
  const refSlots = [];
  const effectDeps = [];
  let cursorState = 0;
  let cursorRef = 0;
  let cursorEffect = 0;
  let pendingEffects = [];
  let dirty = false;

  const useState = (initial) => {
    const index = cursorState++;
    if (!(index in stateSlots)) {
      stateSlots[index] = typeof initial === "function" ? initial() : initial;
    }
    const setState = (next) => {
      const resolved = typeof next === "function" ? next(stateSlots[index]) : next;
      if (!Object.is(resolved, stateSlots[index])) {
        stateSlots[index] = resolved;
        dirty = true;
      }
    };
    return [stateSlots[index], setState];
  };

  const useRef = (initial) => {
    const index = cursorRef++;
    if (!(index in refSlots)) refSlots[index] = { current: initial };
    return refSlots[index];
  };

  const useEffect = (effect, deps) => {
    const index = cursorEffect++;
    const previous = effectDeps[index];
    const changed = !previous
      || !deps
      || deps.length !== previous.length
      || deps.some((value, i) => !Object.is(value, previous[i]));
    if (changed) {
      effectDeps[index] = deps;
      pendingEffects.push(effect);
    }
  };

  const useMemo = (factory) => factory();
  const usePageDraft = (_scope, initial) => useState(initial);
  const usePageReadingPosition = () => {};

  const context = {
    React,
    useState,
    useRef,
    useEffect,
    useMemo,
    usePageDraft,
    usePageReadingPosition,
    window: {
      requestAnimationFrame: () => 0,
      cancelAnimationFrame: () => {},
    },
    ...overrides,
  };
  runInNewContext(code, context, { filename: sourceUrl.pathname });
  const Component = context[componentName];
  assert.equal(typeof Component, "function", `${componentName} must exist`);

  let tree = null;
  const renderPass = async () => {
    for (let guard = 0; guard < 8; guard += 1) {
      cursorState = 0;
      cursorRef = 0;
      cursorEffect = 0;
      pendingEffects = [];
      dirty = false;
      tree = Component(props);
      const effects = pendingEffects;
      pendingEffects = [];
      for (const effect of effects) {
        effect();
      }
      await Promise.resolve();
      await Promise.resolve();
      if (!dirty) break;
    }
    return tree;
  };

  await renderPass();

  return {
    get tree() {
      return tree;
    },
    html() {
      return renderToStaticMarkup(tree);
    },
    async rerender() {
      await renderPass();
    },
  };
}

function findElement(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, predicate);
      if (found) return found;
    }
    return null;
  }
  if (predicate(node)) return node;
  return findElement(node.props?.children, predicate);
}

function makeRequest(overrides = {}) {
  return {
    request_ref: "research_graph:HR-88:r1",
    request_id: "HR-88",
    revision: 1,
    issuer: "research_graph",
    quest_ref: "quest_real_run_1",
    kind: "offline_action",
    status: "satisfied",
    obligation: "确认本地数据集切分边界并交回说明。",
    business_purpose: "用于决定后续对比实验的验证集口径。",
    acceptance_conditions: ["已说明切分规则"],
    required_authorization: null,
    direct_waiters: [{
      waiter_ref: "waiter-88",
      generation: 1,
      wait_scope: "local",
      status: "consumed",
      other_blockers: [],
    }],
    responses: [{
      response_ref: "human_response_8c94603dc90a4118a4fcc0f75f22f6df",
      decision: "provided",
      note: "已确认按时间切分。",
    }],
    evaluation: { decision: "satisfied" },
    disposition: { decision: "satisfied" },
    ...overrides,
  };
}

function requestFormOverrides(deliveryResponseRef = "human_response_8c94603dc90a4118a4fcc0f75f22f6df") {
  return {
    humanResponseDeliveryState: () => (
      deliveryResponseRef
        ? { status: "recorded", result: { response_ref: deliveryResponseRef } }
        : null
    ),
    subscribeHumanResponseDelivery: () => () => {},
    pendingHumanRequestResponsePreview: async () => null,
    hasLegacyHumanRequestMaterial: () => false,
    HumanRequestMaterialReferences: () => null,
    HumanResponseReview: () => null,
    LibraryForm: () => null,
    TimeExtensionForm: () => null,
    NaturalLanguageMaterialForm: () => React.createElement("div", { className: "stub-form" }, "form"),
    PermissionForm: () => null,
    hydratePendingHumanRequestRecovery: async () => {},
    pendingHumanRequestResponse: () => null,
    deliverPendingHumanRequestResponse: async () => {},
    respondToHumanRequest: async () => {},
    retryHumanRequest: async () => ({ retry: { status: "succeeded" } }),
    ProductError: Error,
  };
}

const formExtraDecls = [
  "isRecord",
  "reasonCode",
  "humanRequestScopeStale",
  "requestStatusLabel",
  "explicitRequestDecisions",
  "requestStatusTone",
  "humanReplyMaterials",
  "humanRequestReceiptPresentation",
];

const viewExtraDecls = [
  "requestCopy",
  "scopeLabel",
  "requestStatusLabel",
  "renderAgentText",
  "explicitRequestDecisions",
  "requestStatusTone",
];

const hiddenDialogueAsidePattern =
  /<aside\b(?=[^>]*\bclass="[^"]*\bhc-request-draft\b[^"]*")(?=[^>]*\bhidden(?:=""|(?=[\s>])))\s*[^>]*>/;

function loadReceiptHelpers() {
  const code = extractDeclarations([
    "requestStatusLabel",
    "explicitRequestDecisions",
    "requestStatusTone",
    "humanRequestReceiptPresentation",
  ]);
  const context = {};
  runInNewContext(code, context, { filename: sourceUrl.pathname });
  return context;
}

test("submitted/satisfied receipt renders calm green success state without protocol jargon or raw receipt ID", async () => {
  const mounted = await mountWithHooks(
    "RequestForm",
    formExtraDecls,
    {
      request: makeRequest({ status: "satisfied" }),
      commands: [],
      authorizations: [],
      selectedReplies: [],
      onChanged: () => {},
      onReviewChange: () => {},
    },
    requestFormOverrides("human_response_8c94603dc90a4118a4fcc0f75f22f6df"),
  );
  const html = mounted.html();

  assert.match(html, /hc-response-boundary--success/, "satisfied receipt must use green success modifier class");
  assert.match(html, /data-receipt-tone="success"/, "satisfied receipt must expose success tone");
  assert.match(html, /回应已提交/, "satisfied receipt should confirm submission");
  assert.match(html, /已收到|交回研究流程/, "satisfied receipt should use natural Chinese status copy");
  assert.doesNotMatch(html, /原根核验/, "primary receipt view must not expose '原根核验' protocol jargon");
  assert.doesNotMatch(html, /后续处理[:：]\s*satisfied/i, "primary receipt view must not expose raw '后续处理: satisfied'");
  assert.doesNotMatch(html, /human_response_8c94603dc90a4118a4fcc0f75f22f6df/, "primary receipt view must not dump raw response_ref ID");
  assert.doesNotMatch(html, /提交不表示原根已恢复/, "satisfied receipt must not show alarming unrecovered-root warning copy");
});

test("awaiting-owner submission success card does not dump raw human_response ID in primary view", async () => {
  const mounted = await mountWithHooks(
    "RequestForm",
    formExtraDecls,
    {
      request: makeRequest({
        status: "open",
        evaluation: null,
        disposition: null,
      }),
      commands: [],
      authorizations: [],
      selectedReplies: [],
      onChanged: () => {},
      onReviewChange: () => {},
    },
    requestFormOverrides("human_response_8c94603dc90a4118a4fcc0f75f22f6df"),
  );
  const html = mounted.html();

  assert.match(html, /hc-submission-success/);
  assert.match(html, /回应已提交/);
  assert.doesNotMatch(html, /human_response_8c94603dc90a4118a4fcc0f75f22f6df/);
  assert.doesNotMatch(html, /原根/);
});

test("genuinely unsatisfied and declined states stay visually distinct from green success", async () => {
  const unsatisfied = await mountWithHooks(
    "RequestForm",
    formExtraDecls,
    {
      request: makeRequest({
        status: "unsatisfied",
        evaluation: { decision: "needs_input" },
        disposition: { decision: "unsatisfied" },
      }),
      commands: [],
      authorizations: [],
      selectedReplies: [],
      onChanged: () => {},
      onReviewChange: () => {},
    },
    requestFormOverrides("human_response_unsatisfied_123"),
  );
  const unsatisfiedHtml = unsatisfied.html();
  assert.match(unsatisfiedHtml, /hc-response-boundary--warning/, "unsatisfied receipt must remain visually distinct warning state");
  assert.doesNotMatch(unsatisfiedHtml, /hc-response-boundary--success/);
  assert.doesNotMatch(unsatisfiedHtml, /原根核验/);
  assert.doesNotMatch(unsatisfiedHtml, /human_response_unsatisfied_123/);

  const declined = await mountWithHooks(
    "RequestForm",
    formExtraDecls,
    {
      request: makeRequest({
        status: "declined",
        evaluation: { decision: "declined" },
        disposition: { decision: "declined" },
      }),
      commands: [],
      authorizations: [],
      selectedReplies: [],
      onChanged: () => {},
      onReviewChange: () => {},
    },
    requestFormOverrides("human_response_declined_456"),
  );
  const declinedHtml = declined.html();
  assert.match(declinedHtml, /hc-response-boundary--neutral/, "declined receipt should use calm neutral tone");
  assert.doesNotMatch(declinedHtml, /hc-response-boundary--success/);
  assert.doesNotMatch(declinedHtml, /原根核验/);
  assert.doesNotMatch(declinedHtml, /human_response_declined_456/);
});

test("collapsed RequestDetails retains formal receipt_ref without shadowing and shows response_ref post-reload", async () => {
  const mounted = await mountWithHooks(
    "RequestDetails",
    ["isRecord", "documentText", "firstDefined", "stringify", "Detail"],
    {
      request: makeRequest({
        status: "satisfied",
        responses: [{
          response_ref: "human_response_8c94603dc90a4118a4fcc0f75f22f6df",
          receipt: { receipt_ref: "formal_receipt_abc123" },
          decision: "provided",
          note: "已确认按时间切分。",
        }],
      }),
      otherBlockers: [],
    },
    {
      ImpactPreview: () => null,
      // Simulate post-reload: in-memory delivery state is null.
      humanResponseDeliveryState: () => null,
    },
  );
  const html = mounted.html();
  assert.match(html, /<details class="hc-request-details">/);
  assert.match(html, /<dt>Response receipts<\/dt><dd>formal_receipt_abc123<\/dd>/, "formal receipt.receipt_ref must not be shadowed by response_ref");
  assert.match(html, /<dt>Response refs<\/dt><dd>human_response_8c94603dc90a4118a4fcc0f75f22f6df<\/dd>/, "response_ref must remain available in a separate debug row post-reload");
});

test("humanRequestReceiptPresentation never renders false-positive green on stale retryStatus or contradictory decisions", () => {
  const { humanRequestReceiptPresentation, requestStatusTone } = loadReceiptHelpers();

  // Stale retryStatus === "succeeded" after reload moves status away from open/satisfied
  for (const [status, expectedTone] of [
    ["unsatisfied", "warning"],
    ["expired", "warning"],
    ["declined", "neutral"],
    ["withdrawn", "neutral"],
    ["superseded", "neutral"],
  ]) {
    const receipt = humanRequestReceiptPresentation(
      makeRequest({ status, evaluation: null, disposition: null }),
      { recorded: true, retryStatus: "succeeded" },
    );
    assert.equal(receipt.tone, expectedTone, `retryStatus=succeeded with status=${status} must be ${expectedTone}, not success`);
    assert.notEqual(receipt.tone, "success");
    assert.notEqual(receipt.title, "原任务已收到重试请求");
  }

  // Genuine retry success on open or satisfied without contradictory decisions stays green
  for (const status of ["open", "satisfied"]) {
    const receipt = humanRequestReceiptPresentation(
      makeRequest({ status, evaluation: null, disposition: null }),
      { recorded: false, retryStatus: "succeeded" },
    );
    assert.equal(receipt.tone, "success");
    assert.equal(receipt.title, "原任务已收到重试请求");
  }

  // retryStatus=succeeded on open with contradictory evaluation must downgrade from green
  const openContradicted = humanRequestReceiptPresentation(
    makeRequest({ status: "open", evaluation: { decision: "needs_input" }, disposition: null }),
    { recorded: false, retryStatus: "succeeded" },
  );
  assert.equal(openContradicted.tone, "warning");

  // status=satisfied with contradictory evaluation/disposition must downgrade from green
  const satisfiedNeedsInput = humanRequestReceiptPresentation(
    makeRequest({ status: "satisfied", evaluation: { decision: "needs_input" }, disposition: { decision: "satisfied" } }),
    { recorded: true, retryStatus: null },
  );
  assert.equal(satisfiedNeedsInput.tone, "warning");
  assert.equal(
    requestStatusTone("satisfied", { evaluation: { decision: "needs_input" }, disposition: { decision: "satisfied" } }),
    "warning",
  );

  const satisfiedUnsatisfiedDisp = humanRequestReceiptPresentation(
    makeRequest({ status: "satisfied", evaluation: { decision: "satisfied" }, disposition: { decision: "unsatisfied" } }),
    { recorded: true, retryStatus: null },
  );
  assert.equal(satisfiedUnsatisfiedDisp.tone, "warning");

  const satisfiedDeclined = humanRequestReceiptPresentation(
    makeRequest({ status: "satisfied", evaluation: { decision: "declined" }, disposition: { decision: "declined" } }),
    { recorded: true, retryStatus: null },
  );
  assert.equal(satisfiedDeclined.tone, "neutral");
  assert.equal(
    requestStatusTone("satisfied", { evaluation: { decision: "declined" }, disposition: { decision: "declined" } }),
    "neutral",
  );

  // Direct expired / withdrawn / superseded tones without retryStatus
  assert.equal(
    humanRequestReceiptPresentation(makeRequest({ status: "expired", evaluation: null, disposition: null }), { recorded: false, retryStatus: null }).tone,
    "warning",
  );
  assert.equal(
    humanRequestReceiptPresentation(makeRequest({ status: "withdrawn", evaluation: null, disposition: null }), { recorded: false, retryStatus: null }).tone,
    "neutral",
  );
  assert.equal(
    humanRequestReceiptPresentation(makeRequest({ status: "superseded", evaluation: null, disposition: null }), { recorded: false, retryStatus: null }).tone,
    "neutral",
  );
});

test("RequestForm renders warning styling (not green) when satisfied status contradicts explicit evaluation", async () => {
  const mounted = await mountWithHooks(
    "RequestForm",
    formExtraDecls,
    {
      request: makeRequest({
        status: "satisfied",
        evaluation: { decision: "needs_input" },
        disposition: { decision: "satisfied" },
      }),
      commands: [],
      authorizations: [],
      selectedReplies: [],
      onChanged: () => {},
    },
    requestFormOverrides("human_response_contradicted_789"),
  );
  const html = mounted.html();
  assert.match(html, /hc-response-boundary--warning/);
  assert.match(html, /data-receipt-tone="warning"/);
  assert.doesNotMatch(html, /hc-response-boundary--success/);
  assert.doesNotMatch(html, /human_response_contradicted_789/);
});

test("HumanRequestList status pills align with requestStatusTone across open, satisfied, unsatisfied, expired, and declined", async () => {
  const items = [
    makeRequest({ request_ref: "r:open", status: "open", evaluation: null, disposition: null }),
    makeRequest({ request_ref: "r:sat", status: "satisfied" }),
    makeRequest({ request_ref: "r:unsat", status: "unsatisfied", evaluation: { decision: "needs_input" }, disposition: { decision: "unsatisfied" } }),
    makeRequest({ request_ref: "r:exp", status: "expired", evaluation: null, disposition: null }),
    makeRequest({ request_ref: "r:dec", status: "declined", evaluation: { decision: "declined" }, disposition: { decision: "declined" } }),
  ];
  const mounted = await mountWithHooks(
    "HumanRequestList",
    viewExtraDecls,
    { items, onSelect: () => {}, onClose: () => {}, blocking: false },
    { MetaTrace: () => null },
  );
  const html = mounted.html();
  assert.match(html, /<i class="current">待处理<\/i>/);
  assert.match(html, /<i class="success">已处理<\/i>/);
  assert.match(html, /<i class="warning">已回应，未满足<\/i>/);
  assert.match(html, /<i class="warning">已过期<\/i>/);
  assert.match(html, /<i class="neutral">已拒绝<\/i>/);
});

test("HumanRequestView simplifies crowded top header and puts spacious primary area on left with collapsible right dialogue pane", async () => {
  const openRequest = makeRequest({
    status: "open",
    responses: [],
    evaluation: null,
    disposition: null,
  });
  const mounted = await mountWithHooks(
    "HumanRequestView",
    viewExtraDecls,
    {
      request: openRequest,
      requests: [openRequest],
      onSelectRequest: () => {},
      collaboration: {
        companion: { status: "ready", scope_ref: "quest:quest_real_run_1", messages: [], soft_constraints: [], agent_proposals: [] },
        human_requests: { status: "ready", items: [openRequest], waiting: { scope: "local", safe_meaningful_runnable_exists: true, other_blockers: [] } },
        commands: { status: "ready", items: [], authorizations: [] },
      },
      onBack: () => {},
      onClose: () => {},
      onChanged: () => {},
      blocking: false,
      queuePosition: 1,
      queueSize: 1,
      onPrevious: () => {},
      onNext: () => {},
      hasPrevious: false,
      hasNext: false,
    },
    {
      MetaTrace: () => React.createElement("span", { className: "stub-trace" }),
      IntentDraftingSession: (props) => React.createElement(
        "aside",
        {
          id: props.id,
          className: "hc-request-draft",
          hidden: props.hidden,
          "aria-label": "dialogue",
        },
        "dialogue-content",
      ),
      RequestHandoff: () => React.createElement("section", { className: "hc-handoff" }, "handoff"),
      QuestResearchTime: () => null,
      TimeExtensionReceipt: () => null,
      RequestForm: () => React.createElement("div", { className: "stub-form" }, "form"),
      RequestDetails: () => React.createElement("details", { className: "hc-request-details" }, "details"),
    },
  );

  const expandedHtml = mounted.html();
  // Simplified header: no redundant stage track with '原根核验', no formal-head jargon, and no empty 6-tab category strip for single request
  assert.doesNotMatch(expandedHtml, /workspace-stage-track/, "redundant stage track strip should be removed from HumanRequestView");
  assert.doesNotMatch(expandedHtml, /hc-formal-head/, "redundant formal-head banner should be removed");
  assert.doesNotMatch(expandedHtml, /当前无请求/, "empty disabled category tabs should not clutter the header");
  assert.doesNotMatch(expandedHtml, /原根核验/, "header must not expose protocol jargon");

  // Spacious left primary action area before right collapsible dialogue pane
  const coreIdx = expandedHtml.indexOf('class="hc-request-core"');
  const draftIdx = expandedHtml.indexOf('class="hc-request-draft"');
  assert.ok(coreIdx >= 0 && draftIdx >= 0, "both primary core and dialogue pane should render");
  assert.ok(coreIdx < draftIdx, "primary action/detail area (.hc-request-core) must come BEFORE right dialogue pane (.hc-request-draft)");
  assert.match(expandedHtml, /data-dialogue-open="true"/, "workspace should reflect expanded dialogue state");

  // Collapsible toggle behavior
  const toggleBtn = findElement(
    mounted.tree,
    (el) => el.type === "button" && typeof el.props?.className === "string" && el.props.className.includes("hc-dialogue-toggle"),
  );
  assert.ok(toggleBtn, "header must include a dialogue collapse/expand toggle button (.hc-dialogue-toggle)");
  assert.equal(toggleBtn.props["aria-expanded"], true);
  assert.ok(toggleBtn.props["aria-controls"], "toggle button must declare aria-controls");

  toggleBtn.props.onClick();
  await mounted.rerender();
  const collapsedHtml = mounted.html();
  assert.match(collapsedHtml, /data-dialogue-open="false"/, "clicking toggle must collapse the right dialogue pane");
  assert.match(collapsedHtml, hiddenDialogueAsidePattern, "collapsed dialogue pane must be hidden");
  const toggleBtnAfter = findElement(
    mounted.tree,
    (el) => el.type === "button" && typeof el.props?.className === "string" && el.props.className.includes("hc-dialogue-toggle"),
  );
  assert.equal(toggleBtnAfter.props["aria-expanded"], false);
});

test("HumanRequestView shows only populated category pills when multiple categories exist", async () => {
  const reqA = makeRequest({ request_ref: "r:1:r1", kind: "offline_action", status: "open", responses: [], evaluation: null, disposition: null });
  const reqB = makeRequest({ request_ref: "r:2:r1", kind: "library_reconnect", status: "open", responses: [], evaluation: null, disposition: null });
  const mounted = await mountWithHooks(
    "HumanRequestView",
    viewExtraDecls,
    {
      request: reqA,
      requests: [reqA, reqB],
      onSelectRequest: () => {},
      collaboration: {
        companion: { status: "ready", scope_ref: "quest:quest_real_run_1", messages: [], soft_constraints: [], agent_proposals: [] },
        human_requests: { status: "ready", items: [reqA, reqB], waiting: { scope: "local", safe_meaningful_runnable_exists: true, other_blockers: [] } },
        commands: { status: "ready", items: [], authorizations: [] },
      },
      onBack: () => {},
      onClose: () => {},
      onChanged: () => {},
      blocking: false,
      queuePosition: 1,
      queueSize: 2,
      onPrevious: () => {},
      onNext: () => {},
      hasPrevious: false,
      hasNext: true,
    },
    {
      MetaTrace: () => null,
      IntentDraftingSession: () => React.createElement("aside", { className: "hc-request-draft" }),
      RequestHandoff: () => null,
      QuestResearchTime: () => null,
      TimeExtensionReceipt: () => null,
      RequestForm: () => null,
      RequestDetails: () => null,
    },
  );
  const html = mounted.html();
  const navMatch = html.match(/<nav[^>]*class="hc-kind-navigation"[^>]*>([\s\S]*?)<\/nav>/);
  assert.ok(navMatch, "populated multi-category view should show compact category switcher");
  const buttonMatches = navMatch[1].match(/<button\b/g) ?? [];
  assert.equal(buttonMatches.length, 2, "category switcher must render only the 2 populated categories, not 6");
  assert.doesNotMatch(navMatch[1], /当前无请求/);
});

test("CSS defines calm green success receipt styling and spacious left-primary + collapsible right-dialogue layout", () => {
  // 1. Calm green success receipt styles in shell.css
  const successRule = shellCss.match(/\.hc-response-boundary--success\s*\{[^}]+\}/);
  assert.ok(successRule, "shell.css must define .hc-response-boundary--success");
  assert.match(successRule[0], /var\(--jade\)|var\(--aqua\)|#159987|#1[0-9a-f]{5}/i, "success receipt must use green accent color");
  assert.match(successRule[0], /#eff9f|#eef9f|#ecfdf|#f0fdf/i, "success receipt must use soft green surface tint");
  assert.doesNotMatch(successRule[0], /--amber|#fff3ef|#f27858/i, "success receipt must not use red/orange warning colors");

  // 2. Distinct warning state remains present
  assert.match(shellCss, /\.hc-response-boundary--warning\s*\{/, "shell.css must keep distinct warning receipt state");

  // 3. Collapsible dialogue layout rules
  assert.match(shellCss, /\.hc-request-workspace\[data-dialogue-open="false"\]/, "shell.css must handle collapsed single-column workspace");
  assert.match(shellCss, /\.hc-request-draft\[hidden\]\s*\{\s*display:\s*none/i, "shell.css must cleanly hide collapsed dialogue pane");

  // 4. workspace-modal.css must also respect left-wide primary + right collapsible pane (no stale 1.9fr left-draft override!)
  assert.match(modalCss, /\.workspace-modal\s+\.hc-request-workspace\[data-dialogue-open="false"\]/, "workspace-modal.css must allow single-column collapse");
  const modalWorkspaceRule = modalCss.match(/\.workspace-modal\s+\.hc-request-workspace\s*\{[^}]+\}/);
  assert.ok(modalWorkspaceRule, "workspace-modal.css should define .workspace-modal .hc-request-workspace");
  assert.match(modalWorkspaceRule[0], /grid-template-columns:\s*minmax\(0,\s*1\.[5-9]\d*fr\)\s+minmax\(/, "left primary column must be wider than right dialogue column");

  // 5. workspace-modal.css must override `.workspace-modal :is(button, ...)` font reset for HR header & collapse buttons
  assert.match(modalCss, /\.workspace-modal\s+\.hc-back-button,\s*\.workspace-modal\s+\.hc-dialogue-toggle\s*\{[^}]*font-size:\s*12px/, "workspace-modal.css must preserve header button typography");
  assert.match(modalCss, /\.workspace-modal\s+\.hc-draft-collapse\s*\{[^}]*font-size:\s*11px/, "workspace-modal.css must preserve dialogue collapse button typography");
  assert.match(modalCss, /\.workspace-modal\s+\.hc-queue-nav\s+button\s*\{[^}]*font-size:\s*11px/, "workspace-modal.css must preserve queue nav button typography");
  assert.match(modalCss, /@media\s*\(max-width:\s*600px\)\s*\{[\s\S]*\.workspace-modal\s+\.hc-dialogue-toggle\s*\{[^}]*font-size:\s*11px/, "workspace-modal.css must include mobile header toggle typography override");

  // 6. library_reconnect desktop min-height specificity guard: no desktop 360px override in shell.css; workspace-modal enforces min-height: 0 on desktop
  assert.doesNotMatch(shellCss, /\.hc-dialog-library\s+\.hc-request-draft/, "shell.css must not force desktop min-height on library dialogue pane");
  assert.match(modalCss, /\.workspace-modal\s+\.hc-dialog\s+\.hc-request-draft:not\(\[hidden\]\)\s*\{[^}]*min-height:\s*0/, "workspace-modal.css must enforce min-height: 0 on desktop dialogue pane");
  assert.match(modalCss, /@media\s*\(max-width:\s*900px\)\s*\{[\s\S]*\.workspace-modal\s+\.hc-dialog\s+\.hc-request-draft:not\(\[hidden\]\)\s*\{[^}]*min-height:\s*360px/, "workspace-modal.css must scope min-height: 360px to narrow stacking viewports");

  // 7. Removed dead HR layout classes stay cleaned up
  assert.doesNotMatch(shellCss, /\.hc-formal-head\b/);
  assert.doesNotMatch(modalCss, /\.workspace-stage-track\b/);
});

test("satisfied request without messages starts with collapsed dialogue pane and can expand on demand", async () => {
  const satisfiedRequest = makeRequest({ status: "satisfied" });
  const mounted = await mountWithHooks(
    "HumanRequestView",
    viewExtraDecls,
    {
      request: satisfiedRequest,
      requests: [satisfiedRequest],
      onSelectRequest: () => {},
      collaboration: {
        companion: { status: "ready", scope_ref: "quest:quest_real_run_1", messages: [], soft_constraints: [], agent_proposals: [] },
        human_requests: { status: "ready", items: [satisfiedRequest], waiting: { scope: "local", safe_meaningful_runnable_exists: true, other_blockers: [] } },
        commands: { status: "ready", items: [], authorizations: [] },
      },
      onBack: () => {},
      onClose: () => {},
      onChanged: () => {},
      blocking: false,
      queuePosition: 0,
      queueSize: 0,
      onPrevious: () => {},
      onNext: () => {},
      hasPrevious: false,
      hasNext: false,
    },
    {
      MetaTrace: () => null,
      IntentDraftingSession: (props) => React.createElement(
        "aside",
        { id: props.id, className: "hc-request-draft", hidden: props.hidden },
        "dialogue",
      ),
      RequestHandoff: () => null,
      QuestResearchTime: () => null,
      TimeExtensionReceipt: () => null,
      RequestForm: () => null,
      RequestDetails: () => null,
    },
  );
  const initialHtml = mounted.html();
  assert.match(initialHtml, /hc-status-badge--success/, "header status pill should use calm green for satisfied request");
  assert.match(initialHtml, /data-dialogue-open="false"/, "handled request without messages should default to spacious collapsed dialogue");
  assert.match(initialHtml, hiddenDialogueAsidePattern);

  const toggleBtn = findElement(
    mounted.tree,
    (el) => el.type === "button" && typeof el.props?.className === "string" && el.props.className.includes("hc-dialogue-toggle"),
  );
  assert.ok(toggleBtn);
  assert.equal(toggleBtn.props["aria-expanded"], false);
  toggleBtn.props.onClick();
  await mounted.rerender();
  assert.match(mounted.html(), /data-dialogue-open="true"/);
});

test("IntentDraftingSession exposes collapse button and clean natural Chinese without protocol jargon", async () => {
  let collapsed = false;
  const openRequest = makeRequest({ status: "open", responses: [], evaluation: null, disposition: null });
  const mounted = await mountWithHooks(
    "IntentDraftingSession",
    [
      "requestCopy",
      "messageText",
      "optimisticMessageIsProjected",
      "optimisticReplyIsProjected",
      "optimisticTurnIsProjected",
      "reasonCode",
      "companionError",
      "submitTextareaOnEnter",
    ],
    {
      id: "hc-dialogue-HR-88-r1",
      hidden: false,
      onCollapse: () => { collapsed = true; },
      request: openRequest,
      scopeRef: "quest:quest_real_run_1",
      messages: [],
      companion: { status: "ready", scope_ref: "quest:quest_real_run_1", messages: [], soft_constraints: [], agent_proposals: [] },
      selectedReplyRefs: [],
      onSelectReply: () => {},
      onChanged: () => {},
    },
    {
      MetaTrace: () => null,
      CompanionSessionState: () => null,
      CompanionReplyContent: () => null,
      CompanionHumanMessage: () => null,
      PendingCompanionReply: () => null,
      useCompanionSessionSwitch: () => ({ session: undefined, switching: false, busy: false, start: async () => {} }),
      scrollToConversationMessage: () => {},
      sendCompanionMessage: async () => ({ interaction_ref: "i-1" }),
    },
  );
  const html = mounted.html();
  assert.match(html, /class="hc-draft-collapse"/);
  assert.doesNotMatch(html, /原根/);
  assert.doesNotMatch(html, /hc-draft-obligation/, "empty dialogue pane should not duplicate the full obligation heading");
  const collapseBtn = findElement(
    mounted.tree,
    (el) => el.type === "button" && typeof el.props?.className === "string" && el.props.className.includes("hc-draft-collapse"),
  );
  assert.ok(collapseBtn);
  collapseBtn.props.onClick();
  assert.equal(collapsed, true);
});
