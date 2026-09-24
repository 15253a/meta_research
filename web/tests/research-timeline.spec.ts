import { expect, test, type Locator, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import type { RootSession, RootSessions } from "../src/rootSessionsApi.js";
import type { ResearchOverviewData } from "../src/ResearchOverview.js";
import type { TimelineSummaries, TimelineSummaryNode } from "../src/timelineSummaries.js";

type JsonRecord = Record<string, any>;
const QUEST_REF = "quest-143";
const CYCLE_REF = "cycle-143";
const QUESTION_REF = "question-143-current";
const SOURCE_TIME = Date.parse("2026-09-09T18:47:15+08:00") / 1_000;
test.describe.configure({ timeout: 120_000 });
function currentCycleSnapshot(base: JsonRecord): JsonRecord {
  const foreground = {
    quest_ref: QUEST_REF,
    cycle_ref: CYCLE_REF,
    question_ref: QUESTION_REF,
    stage: "reasoning",
    epoch: 4,
    status: "active",
    grant_ref: "foreground-grant-143",
    grant_status: "active",
    safe_point_ref: null,
    pending_operation_ref: null,
    owner_revision: 143,
  };
  return {
    ...base,
    revision: Number(base.revision) + 143,
    owners: {
      ...base.owners,
      research_graph: {
        ...base.owners.research_graph,
        status: "ready",
        revision: 143,
      },
    },
    research_space: {
      ...base.research_space,
      status: "active",
      quest_count: 1,
      question_count: 2,
      foreground_cycle_count: 1,
      current_quest: {
        status: "ready",
        quest_ref: QUEST_REF,
        goal_revision_ref: "goal-revision-143",
        draft_revision: 2,
        draft_hash: "1".repeat(64),
        goal: "把当前 Cycle 的真实研究事实放在同一个界面",
        completion_criteria: "四个可能 Stage 与当前 Question 保持精确绑定",
        projection_digest: "2".repeat(64),
        reason: null,
      },
      current_question: {
        quest_ref: QUEST_REF,
        question_ref: QUESTION_REF,
        graph_revision: 143,
        title: "非完整路径的可信研究问题",
        unknown_statement: "非完整 Stage 路径下，哪些事实已经被正式接纳？",
        answer_shape: "按来源分离事实、活动和不可用边界。",
        applicability_scope: "仅覆盖当前 foreground Cycle。",
      },
    },
    research_control: {
      status: "ready",
      quest_ref: QUEST_REF,
      foreground,
      managed_runs: [{
        run_ref: "reasoning-run-143",
        run_kind: "reasoning_stage",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "running",
        attempt_ref: "reasoning-attempt-143",
        root_session_ref: "reasoning-root-session-143",
        fence_ref: "reasoning-fence-143",
        control_revision: 3,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_788_200_100,
      }, {
        run_ref: "acquisition-run-143",
        run_kind: "acquisition",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "completed",
        attempt_ref: "acquisition-attempt-143",
        root_session_ref: "acquisition-root-session-143",
        fence_ref: "acquisition-fence-143",
        control_revision: 1,
        safe_point_ref: null,
        terminal_reason: "completed",
        cleanup_status: "completed",
        updated_at: 1_788_200_000,
      }, {
        run_ref: "deepfetch-run-143",
        run_kind: "deepfetch",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 4,
        status: "running",
        attempt_ref: "deepfetch-attempt-143",
        root_session_ref: "deepfetch-root-session-143",
        fence_ref: "deepfetch-fence-143",
        control_revision: 2,
        safe_point_ref: null,
        terminal_reason: null,
        cleanup_status: "none",
        updated_at: 1_788_200_025,
      }, {
        run_ref: "bundle-run-143",
        run_kind: "bundle_stage",
        quest_ref: QUEST_REF,
        cycle_ref: CYCLE_REF,
        epoch: 3,
        status: "completed",
        attempt_ref: "bundle-attempt-143",
        root_session_ref: "bundle-root-session-143",
        fence_ref: "bundle-fence-143",
        control_revision: 2,
        safe_point_ref: null,
        terminal_reason: "completed",
        cleanup_status: "completed",
        updated_at: 1_788_200_075,
      }],
      recovery_records: [],
      actions: [],
    },
    question_tree: {
      status: "ready",
      reason: null,
      items: [{
        question_ref: QUESTION_REF,
        quest_ref: QUEST_REF,
        parent_question_ref: null,
        title: "非完整路径的可信研究问题",
        unknown_statement: "非完整 Stage 路径下，哪些事实已经被正式接纳？",
        content_ref: "question-content-143-current",
        content_hash: "3".repeat(64),
        schema_ref: "meta-research/question/v1",
        question_receipt_ref: "question-receipt-143-current",
        lifecycle_status: "active",
        lifecycle_revision: 7,
        furthest_accepted_stage_result: {
          status: "accepted",
          source: "stage_projection",
          stage: "Bundle",
          kind: "BundleReport",
          result_ref: "bundle-report-143",
          disposition: "realized",
        },
        cycle_binding: {
          status: "bound",
          cycle_ref: CYCLE_REF,
          foreground,
          reason: null,
        },
        related_human_requests: {
          status: "ready",
          items: [{
            request_ref: "human-request-143-current",
            issuer: "agent_runtime",
            kind: "offline_action",
            status: "open",
            revision: 1,
            bindings: [{
              source: "direct_waiter",
              waiter_ref: "waiter-143-current",
              field: "question_ref",
              ref: QUESTION_REF,
            }],
          }],
          reason: null,
        },
      }, {
        question_ref: "question-143-sibling",
        quest_ref: QUEST_REF,
        parent_question_ref: QUESTION_REF,
        title: "尚未绑定的旁支问题",
        unknown_statement: "这个旁支不应被误标为当前攻克。",
        content_ref: "question-content-143-sibling",
        content_hash: "4".repeat(64),
        schema_ref: "meta-research/question/v1",
        question_receipt_ref: "question-receipt-143-sibling",
        lifecycle_status: "active",
        lifecycle_revision: 1,
        cycle_binding: {
          status: "not_bound",
          cycle_ref: null,
          foreground: null,
          reason: { code: "current_foreground_not_bound" },
        },
        related_human_requests: { status: "ready", items: [], reason: null },
      }],
    },
    human_collaboration: {
      ...base.human_collaboration,
      companion: {
        ...base.human_collaboration.companion,
        status: "ready",
        scope_ref: `quest:${QUEST_REF}`,
      },
      human_requests: {
        ...base.human_collaboration.human_requests,
        status: "ready",
        waiting: {
          scope: "local",
          safe_meaningful_runnable_exists: true,
          other_blockers: [],
        },
        items: [],
      },
    },
    idea_stage: {
      eligibility: {
        status: "not_eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: { code: "idea_route_unavailable" },
      },
      stage_run_request: null,
      run: null,
      outcome_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      stage_commit: null,
      typed_skip: { status: "skipped", basis_refs: ["idea-set-143"] },
    },
    plan_stage: {
      eligibility: {
        status: "not_eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: { code: "plan_route_unavailable" },
      },
      stage_run_request: null,
      run: null,
      plan_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      stage_commit: null,
      typed_skip: { status: "skipped", basis_refs: ["formal-plan-143"] },
    },
    bundle_stage: {
      eligibility: {
        status: "consumed",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        formal_plan_ref: "formal-plan-143",
        reason: null,
      },
      stage_run_request: null,
      run: null,
      target_graph: {
        status: "accepted",
        targets: [{
          target_ref: "target-143-a",
          target_key: "检验关键假设 A",
          spec_hash: "4".repeat(64),
          dependency_refs: [],
          target_run_ref: "target-run-143-a",
          status: "running",
        }],
        frontier: ["target-143-a"],
      },
      target_commits: [],
      baseline_pool: [],
      disposition: {
        status: "realized",
        target_count: 1,
        target_commit_count: 0,
        reason: null,
      },
      bundle_report: {
        status: "accepted",
        report_ref: "bundle-report-143",
        disposition: "realized",
      },
      stage_commit: {
        status: "committed",
        commit_ref: "bundle-stage-commit-143",
        cycle_ref: CYCLE_REF,
        stage: "Bundle",
        disposition: "completed",
        next_stage: "Reasoning",
      },
    },
    reasoning_stage: {
      eligibility: {
        status: "eligible",
        cycle_ref: CYCLE_REF,
        question_ref: QUESTION_REF,
        reason: null,
      },
      stage_run_request: null,
      run: null,
      reasoning_acceptance: {
        status: "not_attempted",
        content: { status: "not_attempted" },
        domain: { status: "not_attempted" },
      },
      transition: { status: "not_attempted" },
      stage_commit: null,
    },
  };
}

// Exercise the research timeline over the same read-only public contracts.
function timelineSession(ref: string, kind: RootSession["kind"], title: string, cycleRef: string | null,
  status: RootSession["status"], extras: Partial<RootSession> = {}): RootSession {
  return {
    session_ref: ref, root_session_ref: ref, kind, title, stage: kind === "target" ? "bundle" : null,
    related_stages: kind === "target" ? ["bundle"] : [], scope_label: cycleRef ? "当前轮" : "Quest 共享",
    status, is_executing: status === "executing", is_current: null, owner_session_ref: null,
    run_ref: `run-${ref}`, target_ref: kind === "target" ? `target-${ref}` : null,
    cycle_ref: cycleRef, question_ref: cycleRef ? `question-${cycleRef}` : null,
    created_at: SOURCE_TIME, updated_at: SOURCE_TIME, operations: [], ...extras,
  };
}

function timelineArtifact(stage: "idea" | "plan" | "bundle" | "reasoning",
  epoch: number, content: Record<string, unknown> | null, status: "accepted" | "skipped" = "accepted") {
  return { stage, status, epoch, source: { outcome_ref: `outcome-${stage}-${epoch}` }, content, reason: null };
}

const CURRENT_QUESTION_SUMMARY = "确认跨模态一致性约束在异质基准上的迁移增益能否复现。";
const CURRENT_PLAN_SUMMARY = "在五个异质基准上验证迁移增益，并通过消融实验辨别一致性约束的作用。";
const CURRENT_BUNDLE_SUMMARY = "核验被试划分和预处理口径，排除实验评测中的数据泄漏。";
const CURRENT_TARGET_SUMMARY = "使用被试独立划分复核脑电评测，优先核验训练集与测试集的边界。";

function recorderNode(kind: TimelineSummaryNode["kind"], questionRef: string, cycleRef: string | null,
  summary: string | null, extra: Partial<TimelineSummaryNode> = {}): TimelineSummaryNode {
  const node = { kind, question_ref: questionRef, cycle_ref: cycleRef, stage: null, target_ref: null, summary,
    status: "ready" as const, source_hash: "source-v1", summarized_source_hash: "source-v1", updated_at: SOURCE_TIME,
    sources: [{ ref: `record-${cycleRef ?? questionRef}`, label: "研究记录与已接纳结果" }], ...extra };
  const node_key = kind === "question" ? `question:${questionRef}` : kind === "cycle" ? `cycle:${cycleRef}`
    : kind === "stage" ? `stage:${cycleRef}:${node.stage}` : `target:${cycleRef}:${node.target_ref}`;
  return { ...node, node_key };
}

async function openTimeline(page: Page, { runningBundle = false, secondTargetFirst = false, summaryMode = "ready" as "ready" | "pending" } = {}) {
  await page.setViewportSize({ width: 1440, height: 900 });
  const snapshot = currentCycleSnapshot(JSON.parse(await readFile(new URL("./snapshot-before.json", import.meta.url), "utf8")));
  const foreground = snapshot.research_control.foreground;
  const currentQuestionCopy = { title: "异质基准下的一致性约束增益能否复现？", unknown_statement: "在五个异质基准上，跨模态一致性约束的迁移增益能否复现？" };
  Object.assign(snapshot.research_space.current_question, currentQuestionCopy);
  Object.assign(snapshot.question_tree.items[0], currentQuestionCopy);
  snapshot.bundle_stage.target_graph.targets[0].target_key = "五基准扩展评测";
  if (runningBundle) {
    foreground.stage = "bundle";
    foreground.epoch = 3;
    snapshot.idea_stage.typed_skip = null;
    snapshot.plan_stage.typed_skip = null;
    snapshot.bundle_stage.stage_commit = null;
    snapshot.bundle_stage.bundle_report = null;
    snapshot.bundle_stage.disposition.status = "running";
    snapshot.bundle_stage.run = { status: "running", run_ref: "bundle-run-143" };
    snapshot.bundle_stage.target_graph.targets[0].target_key = "pilot_subject_level_eeg";
    snapshot.reasoning_stage.eligibility.status = "not_eligible";
    snapshot.research_control.managed_runs = [{
      ...snapshot.research_control.managed_runs.find((run: JsonRecord) => run.run_kind === "bundle_stage"),
      status: "running", terminal_reason: null, cleanup_status: "none",
    }];
  }
  const overview: ResearchOverviewData = {
    schema_ref: "meta-research/research-overview/v1", status: "ready", quest_ref: QUEST_REF, question_ref: QUESTION_REF,
    cycle_ref: CYCLE_REF, cycle_ordinal: 3, foreground, findings: { quest: [], question: [], cycle: [] }, reason: null,
    cycles: [
      { cycle_ref: "cycle-r1", question_ref: "question-r1", ordinal: 1, stages: {
        idea: [timelineArtifact("idea", 1, { candidates: [
          { candidate_key: "C1", direction: "以跨模态一致性损失对齐表征", rationale: "已有工作缺少跨模态一致性的可控检验，先在小规模基准上验证增益是否稳定。", assumptions: ["表征空间近似线性可控"], risks: ["一致性约束可能损害模态内判别"] },
          { candidate_key: "C2", direction: "仅微调投影层", rationale: "改动最小，作为对照组保留。" }],
          recommendation: { note: "优先验证候选一，保留候选二作为对照，两周内给出初步结论。" } })],
        plan: [timelineArtifact("plan", 2, { answer_contract: { obligations: [
          { obligation_key: "O1", statement: "对齐后的表征是否带来稳定的零样本迁移增益？", minimum_support: "两个基准同时为正" },
          { obligation_key: "O2", statement: "增益是否主要来自一致性约束本身？" }] },
          experiment_briefs: [
            { experiment_key: "E1", goal: "在两个公开基准上评测对齐增益与方差", characteristics: "固定种子三组" },
            { experiment_key: "E2", goal: "消融一致性损失项，定位增益来源" }] })],
        bundle: [timelineArtifact("bundle", 3, { disposition: "completed", realized_experiment_keys: ["E1", "E2"], remaining_experiment_keys: [] })],
        reasoning: [timelineArtifact("reasoning", 4, { claim: "跨模态一致性约束带来稳定的零样本迁移增益，且增益主要来自该约束本身。", disposition: "affirmed", research_synthesis: { cycle: { impact: "为扩大验证范围提供直接依据。" } } })],
      } },
      { cycle_ref: "cycle-r2", question_ref: "question-r2", ordinal: 2, stages: {
        idea: [timelineArtifact("idea", 1, null, "skipped")],
        plan: [timelineArtifact("plan", 2, { answer_contract: { obligations: [{ obligation_key: "O3", statement: "更大样本下增益是否保持？" }] },
          experiment_briefs: [{ experiment_key: "E3", goal: "扩大到十个基准的一致性评测" }] })],
        bundle: [timelineArtifact("bundle", 3, { disposition: "partial", realized_experiment_keys: ["E3"], remaining_experiment_keys: ["E4"] })],
        reasoning: [timelineArtifact("reasoning", 4, { claim: "多数基准增益为正，但小样本基准出现回落，证据尚不充分。", disposition: "insufficient_evidence" })],
      } },
      { cycle_ref: CYCLE_REF, question_ref: QUESTION_REF, ordinal: 3, stages: {
        idea: [timelineArtifact("idea", 1, { candidates: [
          { candidate_key: "C3", direction: "延续第二轮方向，补足样本规模并控制基准异质性", rationale: "直接回应上一轮证据不足的问题。" }],
          recommendation: { note: "本轮先完成扩展评测，再决定是否调整一致性约束形式。" } })],
        plan: [timelineArtifact("plan", 2, { answer_contract: { obligations: [{ obligation_key: "O4", statement: "异质基准上的增益是否可复现？" }] },
          experiment_briefs: [{ experiment_key: "E5", goal: "五个异质基准上的扩展评测" }, { experiment_key: "E6", goal: "约束形式的敏感性分析" }] })],
        bundle: [timelineArtifact("bundle", 3, { disposition: "partial", realized_experiment_keys: ["E5"], remaining_experiment_keys: ["E6", "E7"] })],
      } },
    ],
  };
  const catalog: RootSessions = {
    schema_ref: "meta-research/root-sessions/v1", quest_ref: QUEST_REF, observed_at: SOURCE_TIME + 60,
    sessions: [
      timelineSession("target-r1", "target", "Target T1 · 小规模基准验证", "cycle-r1", "completed", { short_title: "Target T1", question_ref: "question-r1" }),
      timelineSession("target-r2", "target", "Target T2 · 扩大样本试点", "cycle-r2", "completed", { short_title: "Target T2", question_ref: "question-r2" }),
      timelineSession("target-r3a", "target", "Target T3 · 五基准扩展评测", CYCLE_REF, "executing", { short_title: "Target T3", question_ref: QUESTION_REF, target_ref: "target-143-a", run_ref: "target-run-143-a" }),
      timelineSession("target-r3b", "target", "Target T4 · 约束敏感性补测", CYCLE_REF, "pending", { short_title: "Target T4", question_ref: QUESTION_REF }),
    ],
    active_session_refs: ["target-r3a"], limited: false, reasons: [],
  };
  if (runningBundle) {
    delete overview.cycles[2].stages.bundle;
    catalog.sessions = [timelineSession("target-r3a", "target", "Target T1 · pilot_subject_level_eeg", CYCLE_REF, "executing", {
      short_title: "Target T1", question_ref: QUESTION_REF,
      target_ref: "target-143-a", run_ref: "target-run-143-a", is_current: true,
    })];
  }
  if (secondTargetFirst) {
    snapshot.bundle_stage.target_graph.targets[0].status = "pending";
    snapshot.bundle_stage.target_graph.targets[0].target_run_ref = null;
    snapshot.bundle_stage.target_graph.targets.push({
      target_ref: "target-143-b", target_key: "约束敏感性补测", spec_hash: "5".repeat(64), dependency_refs: [], target_run_ref: "target-run-143-b", status: "running",
    });
    catalog.sessions = [timelineSession("target-r3b", "target", "Target T2 · 约束敏感性补测", CYCLE_REF, "executing", {
      short_title: "Target T2", question_ref: QUESTION_REF, target_ref: "target-143-b", run_ref: "target-run-143-b", is_current: true,
    })];
    catalog.active_session_refs = ["target-r3b"];
  }
  const recorder: { data: TimelineSummaries; httpStatus: number } = {
    httpStatus: 200,
    data: { schema_ref: "meta-research/timeline-summaries/v1", quest_ref: QUEST_REF, revision: 1, observed_at: SOURCE_TIME,
      nodes: overview.cycles.flatMap(cycle => [
        recorderNode("question", cycle.question_ref, null, cycle.question_ref === QUESTION_REF ? CURRENT_QUESTION_SUMMARY
          : cycle.ordinal === 1 ? "验证跨模态一致性约束能否稳定改善零样本迁移。" : "考察扩大样本规模后迁移增益的稳定性。"),
        recorderNode("cycle", cycle.question_ref, cycle.cycle_ref, cycle.ordinal === 3 ? "扩展至五个异质基准，并补充约束形式的敏感性验证。" : `本轮已积累实验依据，继续检验跨模态增益的适用范围。`),
        ...(["idea", "plan", "bundle", "reasoning"] as const).map(stage => recorderNode("stage", cycle.question_ref, cycle.cycle_ref,
          stage === "idea" ? "保留一致性约束方向，优先补足样本规模和基准异质性的证据。"
            : stage === "plan" ? CURRENT_PLAN_SUMMARY : stage === "bundle" ? CURRENT_BUNDLE_SUMMARY
              : "结合实验发现辨别稳定增益与证据缺口，避免提前作出确定结论。", { stage })),
      ]) },
  };
  const targetRows = new Map<string, TimelineSummaryNode>();
  for (const session of catalog.sessions) {
    if (!session.target_ref || !session.cycle_ref || !session.question_ref) continue;
    targetRows.set(session.target_ref, recorderNode("target", session.question_ref, session.cycle_ref,
      session.target_ref === "target-143-a" ? CURRENT_TARGET_SUMMARY : `围绕${session.title.replace(/^Target\s+T\d+\s*[·:：]\s*/, "")}保留可核验的实验记录。`, { target_ref: session.target_ref }));
  }
  for (const target of snapshot.bundle_stage.target_graph.targets) {
    if (!targetRows.has(target.target_ref)) targetRows.set(target.target_ref, recorderNode("target", QUESTION_REF, CYCLE_REF,
      target.target_ref === "target-143-a" ? CURRENT_TARGET_SUMMARY : "补充约束敏感性实验，检验评测结论对方法选择的依赖。", { target_ref: target.target_ref }));
  }
  recorder.data.nodes.push(...targetRows.values());
  if (summaryMode === "pending") recorder.data.nodes = recorder.data.nodes.map(node => ({ ...node, summary: null, status: "pending", summarized_source_hash: null, updated_at: null }));
  const errors: string[] = [];
  const reads = { overview: 0, summaries: 0, roots: 0, summaryQuests: [] as string[] };
  const runtime: { status: JsonRecord | null } = { status: null };
  page.on("pageerror", error => errors.push(error.message));
  const webRoot = process.env.META_RESEARCH_WEB_DIST ?? resolve(import.meta.dirname, "../../src/meta_research/web_dist");
  await page.route("**/*", async route => {
    const request = route.request(), url = new URL(request.url());
    if (request.method() !== "GET") return route.abort();
    if (url.origin !== "http://timeline.test") return route.abort();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (url.pathname === "/api/v1/preferences") return json({ output_language: "zh" });
    if (url.pathname === "/api/v1/status" && runtime.status) return json(runtime.status);
    if (url.pathname === "/api/v1/snapshot") return json(snapshot);
    if (url.pathname === "/api/v1/events") return route.fulfill({ contentType: "text/event-stream", body: "event: snapshot.required\ndata: {}\n\n" });
    if (url.pathname === "/api/v1/research-overview") { reads.overview += 1; return json(overview); }
    if (/^\/api\/v1\/quests\/[^/]+\/timeline-summaries$/.test(url.pathname)) {
      reads.summaries += 1;
      reads.summaryQuests.push(decodeURIComponent(url.pathname.split("/")[4]));
      return json(recorder.data, recorder.httpStatus);
    }
    if (url.pathname === `/api/v1/quests/${QUEST_REF}/root-sessions`) { reads.roots += 1; return json(catalog); }
    if (url.pathname.startsWith("/api/")) return route.abort();
    if (url.pathname !== "/" && !/^\/assets\/[\w.-]+$/.test(url.pathname)) return route.abort();
    const file = resolve(webRoot, url.pathname === "/" ? "index.html" : `.${url.pathname}`);
    return route.fulfill({ contentType: extname(file) === ".js" ? "application/javascript" : extname(file) === ".css" ? "text/css" : "text/html", body: await readFile(file) });
  });
  await page.goto("http://timeline.test/?workspace=1", { waitUntil: "domcontentloaded" });
  const timeline = page.getByRole("region", { name: "研究时间线", exact: true });
  return { timeline, errors, snapshot, overview, catalog, recorder, reads, runtime };
}

const cycleNode = (timeline: Locator, cycleRef = CYCLE_REF) => timeline.locator(`.research-timeline-cycle[data-cycle-ref="${cycleRef}"]`);
const stageSummary = (timeline: Locator, stage: string, cycleRef = CYCLE_REF) => cycleNode(timeline, cycleRef).locator(`.research-timeline-stage[data-stage="${stage}"] .research-timeline-stage-summary .timeline-summary-text`);
const summaryNode = (timeline: Locator, key: string) => timeline.locator(`.timeline-summary[data-summary-key="${key}"]`);

function updateSummary(recorder: { data: TimelineSummaries }, key: string, patch: Partial<TimelineSummaryNode>) {
  const node = recorder.data.nodes.find(item => item.node_key === key);
  if (!node) throw new Error(`Missing summary fixture: ${key}`);
  Object.assign(node, patch);
  recorder.data.revision += 1;
}

async function advanceSnapshot(page: Page, snapshot: JsonRecord) {
  snapshot.revision += 1;
  await expect(page.locator(".lumen-connection code")).toHaveText(`状态 ${snapshot.revision}`);
}

async function expandHistory(timeline: Locator) {
  for (const question of await timeline.locator(".research-timeline-question").all()) {
    const details = question.locator(":scope > details");
    if (await details.count() && await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
    await expect(question.locator(":scope > details > .research-timeline-cycles")).toBeVisible();
  }
  for (const cycle of await timeline.locator(".research-timeline-cycle").all()) {
    const details = cycle.locator(":scope > details");
    if (await details.count() && await details.getAttribute("open") === null) await details.locator(":scope > summary").click();
    await expect(cycle.locator(":scope > details > .research-timeline-stages")).toBeVisible();
  }
}

test("the independent tree displays saved recorder sentences verbatim and keeps original results available", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page, { runningBundle: true });
  await expect(timeline).toBeVisible();
  await expect(page.locator(".spectrum-stage-summary")).toHaveCount(0);
  await expect(page.locator(".research-current-heading .research-timeline")).toHaveCount(0);
  const questions = timeline.locator(".research-timeline-questions > .research-timeline-question");
  await expect(questions).toHaveCount(3);
  await expect(summaryNode(timeline, `question:${QUESTION_REF}`).locator(".timeline-summary-text")).toHaveText(CURRENT_QUESTION_SUMMARY);
  const current = cycleNode(timeline);
  await expect(current).toHaveAttribute("data-current", "true");
  await expect(current.locator(":scope > details")).toHaveAttribute("open", "");
  await expect(current.locator(".research-timeline-stage-summary")).toHaveCount(4);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(stageSummary(timeline, "plan")).toHaveAttribute("title", /研究记录与已接纳结果/);
  const target = current.locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await expect(target.locator(".research-timeline-target-state")).toContainText("正在执行");
  await expect(stageSummary(timeline, "bundle")).not.toContainText(/阶段结果尚未接纳|正在开展「/);

  await current.locator('.research-timeline-stage[data-stage="idea"] .research-timeline-stage-open').click();
  const ideaDialog = page.getByRole("dialog");
  await expect(ideaDialog).toContainText("延续第二轮方向，补足样本规模并控制基准异质性");
  await expect(ideaDialog).toContainText("直接回应上一轮证据不足的问题。");
  await ideaDialog.getByLabel("关闭研究思路").click();
  await current.locator('.research-timeline-stage[data-stage="plan"] .research-timeline-stage-open').click();
  const planDialog = page.getByRole("dialog");
  await expect(planDialog).toContainText("异质基准上的增益是否可复现？");
  await expect(planDialog).toContainText("约束形式的敏感性分析");
  await planDialog.getByLabel("关闭验证计划").click();
  expect(errors).toEqual([]);
});

test("missing recorder sentences never fall back to clipped source text or assembled status strings", async ({ page }) => {
  const { timeline, recorder, errors } = await openTimeline(page, { runningBundle: true, summaryMode: "pending" });
  const plan = summaryNode(timeline, `stage:${CYCLE_REF}:plan`);
  await expect(plan.locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  for (const summary of await timeline.locator(".timeline-summary-text").all()) await expect(summary).toHaveText("记录员正在整理…");
  await expect(timeline).not.toContainText(/研究建议：|首个实验目标：|本轮重点：|阶段结果尚未接纳|本轮先完成扩展评测/);
  updateSummary(recorder, `stage:${CYCLE_REF}:plan`, { status: "failed" });
  await expect(plan.locator(".timeline-summary-text")).toHaveText("总结暂未生成");
  updateSummary(recorder, `stage:${CYCLE_REF}:plan`, { status: "ready", summary: CURRENT_PLAN_SUMMARY, summarized_source_hash: "source-v1", updated_at: SOURCE_TIME + 60 });
  await expect(plan.locator(".timeline-summary-text")).toHaveText(CURRENT_PLAN_SUMMARY);
  const facts = page.getByRole("region", { name: "当前研究工作状态", exact: true });
  await expect(facts.locator(".target-research-facts")).not.toBeVisible();
  await facts.getByText("查看输入、产物与交接", { exact: true }).click();
  await expect(facts.locator(".target-research-facts dt")).toHaveCount(7);
  expect(errors).toEqual([]);
});

test("recorder revisions update without snapshot changes and preserve old sentences through generation failures", async ({ page }) => {
  const { timeline, snapshot, recorder, reads, errors } = await openTimeline(page, { runningBundle: true });
  const key = `stage:${CYCLE_REF}:bundle`;
  const bundle = summaryNode(timeline, key);
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const initialRevision = snapshot.revision, initialReads = reads.overview;
  const savedSnapshot = JSON.stringify(snapshot);
  updateSummary(recorder, key, { status: "updating", source_hash: "source-v2", summarized_source_hash: null, summary: null });
  await expect(bundle.locator(".timeline-summary-status")).toHaveText("正在更新总结");
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(bundle).toHaveAttribute("data-summary-stale", "true");
  updateSummary(recorder, key, { status: "failed" });
  await expect(bundle.locator(".timeline-summary-status")).toHaveText("更新暂不可用");
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const nextSentence = "被试划分已核验，下一步比较预处理设置对评测结果的影响。";
  updateSummary(recorder, key, { status: "ready", summary: nextSentence, summarized_source_hash: "source-v2", updated_at: SOURCE_TIME + 120 });
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(nextSentence);
  await expect(bundle.locator(".timeline-summary-status")).toHaveCount(0);
  expect(JSON.stringify(snapshot)).toBe(savedSnapshot);
  await expect(page.locator(".lumen-connection code")).toHaveText(`状态 ${initialRevision}`);
  expect(reads.overview).toBe(initialReads);
  expect(reads.summaries).toBeGreaterThan(1);
  await advanceSnapshot(page, snapshot);
  await advanceSnapshot(page, snapshot);
  expect(reads.overview).toBe(initialReads);
  await expect(bundle.locator(".timeline-summary-text")).toHaveText(nextSentence);
  expect(errors).toEqual([]);
});

test("switching Quests clears old sentences and rejects an answer carrying the previous Quest identity", async ({ page }) => {
  const { timeline, snapshot, overview, recorder, reads, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const nextQuest = "quest-recorder-next", nextQuestion = "question-recorder-next", nextCycle = "cycle-recorder-next";
  Object.assign(snapshot.research_space.current_quest, { quest_ref: nextQuest });
  snapshot.research_control.quest_ref = nextQuest;
  Object.assign(snapshot.research_control.foreground, { quest_ref: nextQuest, question_ref: nextQuestion, cycle_ref: nextCycle });
  Object.assign(snapshot.research_space.current_question, { quest_ref: nextQuest, question_ref: nextQuestion });
  snapshot.question_tree.items = [{ ...snapshot.question_tree.items[0], quest_ref: nextQuest, question_ref: nextQuestion }];
  Object.assign(overview, { quest_ref: nextQuest, question_ref: nextQuestion, cycle_ref: nextCycle, cycle_ordinal: 1,
    cycles: [{ cycle_ref: nextCycle, question_ref: nextQuestion, ordinal: 1, stages: {} }] });
  const forbiddenSentence = "上一研究的回答不能出现在新研究里。";
  recorder.data.nodes = [recorderNode("question", nextQuestion, null, forbiddenSentence)];
  recorder.data.revision += 1;
  // Its node key deliberately matches the new tree, but its Quest envelope is wrong.
  await advanceSnapshot(page, snapshot);
  await expect(summaryNode(timeline, `question:${nextQuestion}`).locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  await expect.poll(() => reads.summaryQuests.includes(nextQuest)).toBe(true);
  await expect(timeline.locator(".timeline-summary-availability")).toBeVisible();
  await expect(timeline).not.toContainText(CURRENT_PLAN_SUMMARY);
  await expect(timeline).not.toContainText(forbiddenSentence);
  recorder.data.quest_ref = nextQuest;
  recorder.data.nodes[0].summary = "检验新研究问题的关键假设，明确需要补足的证据。";
  recorder.data.revision += 1;
  await expect(summaryNode(timeline, `question:${nextQuestion}`).locator(".timeline-summary-text")).toHaveText(recorder.data.nodes[0].summary!);
  await expect(timeline.locator(".timeline-summary-availability")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a paused runtime changes only the live badges while saved recorder sentences remain readable", async ({ page }) => {
  const { timeline, snapshot, runtime, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  const observedAt = new Date().toISOString();
  runtime.status = {
    schema_ref: "meta-research/runtime-status/v1", revision: snapshot.revision + 1,
    observed_at: observedAt, updated_at: observedAt, state: "paused",
    current_task: { kind: "target", title: "pilot_subject_level_eeg", run_ref: "target-run-143-a", target_ref: "target-143-a", status: "paused" },
    waiting_reason: null, pending_requests: 0,
    foreground: { ...snapshot.research_control.foreground, status: "paused", grant_status: "suspended" },
    health: { status: "ready", checks: [] },
  };
  await expect(cycleNode(timeline).locator('.research-timeline-stage[data-stage="bundle"] .research-timeline-stage-state')).toContainText("已暂停");
  const target = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".research-timeline-target-state")).toHaveText("已暂停");
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  expect(snapshot.research_control.foreground.status).toBe("active");
  expect(errors).toEqual([]);
});

test("historical recorder sentences retain question cycle stage and Target identities with source details", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page);
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expandHistory(timeline);
  expect(await timeline.locator(".research-timeline-cycle").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-cycle-ref")))).toEqual(["cycle-r1", "cycle-r2", CYCLE_REF]);
  await expect(summaryNode(timeline, "question:question-r1").locator(".timeline-summary-text")).toHaveText("验证跨模态一致性约束能否稳定改善零样本迁移。");
  await expect(summaryNode(timeline, "question:question-r2").locator(".timeline-summary-text")).toHaveText("考察扩大样本规模后迁移增益的稳定性。");
  await expect(cycleNode(timeline).locator(".research-timeline-target")).toHaveCount(2);
  await expect(cycleNode(timeline, "cycle-r1").locator(".research-timeline-target .timeline-summary-text")).toHaveText("围绕小规模基准验证保留可核验的实验记录。");
  await cycleNode(timeline, "cycle-r1").locator(".research-timeline-stage-open").first().click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("以跨模态一致性损失对齐表征");
  await dialog.getByLabel("关闭研究思路").click();
  const secondDetails = cycleNode(timeline, "cycle-r2").locator(":scope > details");
  await secondDetails.locator(":scope > summary").click();
  await expect(secondDetails).not.toHaveAttribute("open", "");
  await secondDetails.locator(":scope > summary").click();
  await expect(stageSummary(timeline, "plan", "cycle-r2")).toHaveText(CURRENT_PLAN_SUMMARY);
  expect(errors).toEqual([]);
});

test("new cycles and revisited questions retain chronological rows while their summaries arrive independently", async ({ page }) => {
  const { timeline, snapshot, overview, recorder, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const enterCycle = async (cycleRef: string, questionRef: string, ordinal: number) => {
    const question = snapshot.question_tree.items.find((item: JsonRecord) => item.question_ref === questionRef);
    Object.assign(snapshot.research_control.foreground, { cycle_ref: cycleRef, question_ref: questionRef, stage: "idea", epoch: 1 });
    Object.assign(snapshot.research_space.current_question, { ...question, graph_revision: snapshot.revision + 1 });
    Object.assign(snapshot.idea_stage.eligibility, { status: "eligible", cycle_ref: cycleRef, question_ref: questionRef });
    snapshot.idea_stage.stage_run_request = { cycle_ref: cycleRef, epoch: 1, accepted_question_binding: { quest_ref: QUEST_REF, question_ref: questionRef } };
    snapshot.idea_stage.run = { status: "running", run_ref: `idea-run-${cycleRef}` };
    snapshot.research_control.managed_runs = [];
    Object.assign(overview, { cycle_ref: cycleRef, question_ref: questionRef, cycle_ordinal: ordinal });
    overview.cycles.push({ cycle_ref: cycleRef, question_ref: questionRef, ordinal, stages: {} });
    await advanceSnapshot(page, snapshot);
    await expect(cycleNode(timeline, cycleRef)).toBeVisible();
    await expect(cycleNode(timeline, cycleRef)).toHaveAttribute("data-current", "true");
    await expect(stageSummary(timeline, "idea", cycleRef)).toHaveText("记录员正在整理…");
  };
  await enterCycle("cycle-r4", QUESTION_REF, 4);
  await expect(timeline.locator(`.research-timeline-question[data-question-ref="${QUESTION_REF}"]`)).toHaveCount(1);
  await enterCycle("cycle-r5", "question-143-sibling", 5);
  await enterCycle("cycle-r6", QUESTION_REF, 6);
  const newSentence = "沿用已有证据重新审视假设，优先澄清跨基准差异的来源。";
  recorder.data.nodes.push(recorderNode("stage", QUESTION_REF, "cycle-r6", newSentence, { stage: "idea" }));
  recorder.data.revision += 1;
  await expect(stageSummary(timeline, "idea", "cycle-r6")).toHaveText(newSentence);
  await expandHistory(timeline);
  expect(await timeline.locator(".research-timeline-question").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-question-ref")))).toEqual([
    "question-r1", "question-r2", QUESTION_REF, "question-143-sibling", QUESTION_REF,
  ]);
  expect(await timeline.locator(".research-timeline-cycle").evaluateAll(nodes => nodes.map(node => node.getAttribute("data-cycle-ref")))).toEqual([
    "cycle-r1", "cycle-r2", CYCLE_REF, "cycle-r4", "cycle-r5", "cycle-r6",
  ]);
  await expect(stageSummary(timeline, "plan", CYCLE_REF)).toHaveText(CURRENT_PLAN_SUMMARY);
  await expect(timeline.locator('.research-timeline-cycle[data-current="true"]')).toHaveCount(1);
  expect(errors).toEqual([]);
});

test("Target runtime changes and new recorder sentences preserve graph order even when Target two starts first", async ({ page }) => {
  const { timeline, snapshot, catalog, recorder, reads, errors } = await openTimeline(page, { runningBundle: true, secondTargetFirst: true });
  const targets = cycleNode(timeline).locator('.research-timeline-stage[data-stage="bundle"] .research-timeline-target');
  const first = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  const targetRefs = () => targets.evaluateAll(nodes => nodes.map(node => node.getAttribute("data-target-ref")));
  const targetNumbers = () => targets.locator(".research-timeline-target-open > b").evaluateAll(nodes => nodes.map(node => node.textContent?.match(/(\d+)$/)?.[1]));
  await expect(targets).toHaveCount(2);
  await expect(first.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  expect(await targetRefs()).toEqual(["target-143-a", "target-143-b"]);
  expect(await targetNumbers()).toEqual(["1", "2"]);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("等待启动");
  const initialReads = reads.overview;
  catalog.sessions.unshift(timelineSession("target-r3a", "target", "Target T1 · pilot_subject_level_eeg", CYCLE_REF, "executing", {
    short_title: "Target T1", question_ref: QUESTION_REF, target_ref: "target-143-a", run_ref: "target-run-143-a", is_current: true, created_at: SOURCE_TIME + 60,
  }));
  snapshot.bundle_stage.target_graph.targets[0].status = "running";
  snapshot.bundle_stage.target_graph.targets[0].target_run_ref = "target-run-143-a";
  await advanceSnapshot(page, snapshot);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("正在执行");
  await expect(first.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  catalog.sessions[0].status = "completed";
  catalog.sessions[0].is_executing = false;
  snapshot.bundle_stage.target_graph.targets[0].status = "completed";
  await advanceSnapshot(page, snapshot);
  await expect(first.locator(".research-timeline-target-state")).toHaveText("已完成");
  const completedSummary = "被试独立划分的复核已完成，现有评测记录可用于后续证据核验。";
  updateSummary(recorder, `target:${CYCLE_REF}:target-143-a`, { summary: completedSummary, source_hash: "target-v2", summarized_source_hash: "target-v2" });
  await expect(first.locator(".timeline-summary-text")).toHaveText(completedSummary);
  catalog.sessions.push(timelineSession("target-r3c", "target", "Target T3 · 独立数据复核", CYCLE_REF, "pending", {
    short_title: null, question_ref: QUESTION_REF, target_ref: "target-143-c", run_ref: "target-run-143-c", is_current: true, created_at: SOURCE_TIME + 121,
  }));
  await expect(targets).toHaveCount(3);
  expect(await targetRefs()).toEqual(["target-143-a", "target-143-b", "target-143-c"]);
  expect(await targetNumbers()).toEqual(["1", "2", "3"]);
  await expect(summaryNode(timeline, `target:${CYCLE_REF}:target-143-c`).locator(".timeline-summary-text")).toHaveText("记录员正在整理…");
  expect(reads.overview).toBe(initialReads);
  expect(errors).toEqual([]);
});

test("a resumed Target keeps its current root and saved sentence when a superseded root was recorded later", async ({ page }) => {
  const { timeline, catalog, reads, errors } = await openTimeline(page, { runningBundle: true });
  const target = cycleNode(timeline).locator('.research-timeline-target[data-target-ref="target-143-a"]');
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  const previousReads = reads.roots;
  catalog.sessions.push(timelineSession("target-r3a-old", "target", "Target T3 · 恢复前的历史会话", CYCLE_REF, "waiting", {
    short_title: "Target T3 历史", question_ref: QUESTION_REF, target_ref: "target-143-a", run_ref: "target-run-143-a",
    is_current: false, is_executing: false, activity_label: "已由新会话接续", created_at: SOURCE_TIME + 900, updated_at: SOURCE_TIME + 900,
  }));
  await expect.poll(() => reads.roots).toBeGreaterThan(previousReads);
  await expect(page.locator('.stage-root-links [data-session-ref="target-r3a-old"]')).toBeVisible();
  await expect(target).toHaveCount(1);
  await expect(target.locator(".research-timeline-target-state")).toHaveText("正在执行");
  await expect(target.locator(".timeline-summary-text")).toHaveText(CURRENT_TARGET_SUMMARY);
  await target.locator(".research-timeline-target-open").click();
  await expect(page.locator('.root-session-conversation[data-session-ref="target-r3a"]')).toBeVisible();
  await expect(page.locator('.root-session-conversation[data-session-ref="target-r3a-old"]')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("saved recorder sentences retain readable tree indentation on desktop and mobile", async ({ page }) => {
  const { timeline, errors } = await openTimeline(page, { runningBundle: true });
  await expect(stageSummary(timeline, "bundle")).toHaveText(CURRENT_BUNDLE_SUMMARY);
  await expect(summaryNode(timeline, "question:question-r1").locator(".timeline-summary-text")).toHaveText("验证跨模态一致性约束能否稳定改善零样本迁移。");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await expect(timeline).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    const question = timeline.locator(`.research-timeline-question[data-question-ref="${QUESTION_REF}"]`);
    const cycle = cycleNode(timeline);
    const stage = cycle.locator('.research-timeline-stage[data-stage="bundle"]');
    const target = stage.locator('.research-timeline-target[data-target-ref="target-143-a"]');
    const [questionBox, cycleBox, stageBox, targetBox] = await Promise.all([question.boundingBox(), cycle.boundingBox(), stage.boundingBox(), target.boundingBox()]);
    expect(cycleBox!.x).toBeGreaterThan(questionBox!.x);
    expect(stageBox!.x).toBeGreaterThan(cycleBox!.x);
    expect(targetBox!.x).toBeGreaterThan(stageBox!.x);
    for (const summary of await cycle.locator(".timeline-summary-text").all()) await expect(summary).toBeVisible();
    await timeline.locator(".research-timeline-scroll").evaluate(node => { node.scrollTop = 0; });
    const screenshotStyle = ".output-language-control.floating { visibility: hidden !important; }";
    await timeline.screenshot({ path: `test-results/timeline-tree-${width}.png`, style: screenshotStyle });
    if (width === 390) {
      await timeline.locator(".research-timeline-scroll").evaluate(node => { node.scrollTop = node.scrollHeight; });
      await timeline.screenshot({ path: "test-results/timeline-tree-390-lower.png", style: screenshotStyle });
    }
  }
  expect(errors).toEqual([]);
});

test("the Quest timeline remains available after the foreground ends and on a fresh page load", async ({ page }) => {
  const { timeline, snapshot, overview, catalog, errors } = await openTimeline(page);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  const historicalRecord = "历史验证完成：两个公开基准均观察到稳定的迁移增益。";
  const historicalStream = JSON.stringify({ type: "item.completed", thread_id: "native-target-r1", item: { type: "agent_message", text: historicalRecord } }) + "\n";
  catalog.sessions[0].operations = [{ operation_ref: "historical-target-operation", label: "历史验证", phase: "work", status: "completed", created_at: SOURCE_TIME, updated_at: SOURCE_TIME }];
  await page.route("**/root-sessions/target-r1/output?*", route => {
    const offset = Number(new URL(route.request().url()).searchParams.get("after") ?? 0);
    const bytes = new TextEncoder().encode(historicalStream).length;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify({
      schema_ref: "meta-research/root-session-output/v1", quest_ref: QUEST_REF, session_ref: "target-r1", operation_ref: "historical-target-operation",
      stream_ref: "historical-target-stream", text: offset === 0 ? historicalStream : "", offset, next_offset: bytes, source_bytes: bytes,
      has_more: false, source_caught_up: true, source_updated_at: SOURCE_TIME, observed_at: SOURCE_TIME, native_session_ref: "native-target-r1", status: "terminal",
    }) });
  });
  snapshot.research_control.foreground = null;
  snapshot.research_space.foreground_cycle_count = 0;
  overview.foreground = null;
  overview.cycle_ref = null;
  overview.question_ref = null;
  overview.cycle_ordinal = null;
  overview.status = "idle";
  await advanceSnapshot(page, snapshot);
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expect(timeline.locator('.research-timeline-cycle[data-current="true"]')).toHaveCount(0);
  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(timeline).toBeVisible();
  await expect(timeline.locator(".research-timeline-question")).toHaveCount(3);
  await expandHistory(timeline);
  await expect(timeline.locator(".research-timeline-cycle")).toHaveCount(3);
  await expect(stageSummary(timeline, "plan")).toHaveText(CURRENT_PLAN_SUMMARY);
  await cycleNode(timeline, "cycle-r1").locator('.research-timeline-target[data-target-ref="target-target-r1"] .research-timeline-target-open').click();
  const historicalConversation = page.locator('.root-session-conversation[data-session-ref="target-r1"]');
  await expect(historicalConversation).toBeVisible();
  await expect(historicalConversation).toContainText("历史轮次");
  await expect(historicalConversation).toContainText(historicalRecord);
  expect(errors).toEqual([]);
});
