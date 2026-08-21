import { StrictMode, useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  fetchSnapshot,
  followProjection,
  type IdeaQuestionSummary,
  type IdeaStageProjection,
  type PublicSnapshot,
  type UnavailableCapability,
} from "./api";
import { QuestCreationWorkbench } from "./QuestCreation";
import "./shell.css";

const capabilityLabels: Record<string, string> = {
  accepted_material_basis: "Research Asset",
  first_question_deepfetch: "首问题 DeepFetch",
  quest_creation: "创建 Quest",
  quest_companion: "Quest Companion",
  stage_execution: "Stage 执行",
  writing: "Writing",
};

const ownerLabels: Record<string, string> = {
  research_graph: "研究图谱",
  advancement_engine: "推进引擎",
  research_memory: "研究记忆",
  agent_runtime: "智能体运行时",
  human_collaboration: "人机协作",
};

type ShellState =
  | "loading"
  | "first-error"
  | "readiness-unavailable"
  | "ready-empty"
  | "ready-active";

if (window.location.pathname === "/auth/launch") {
  window.history.replaceState(null, "", "/");
}

function uniqueUnavailable(snapshot: PublicSnapshot | null): UnavailableCapability[] {
  if (!snapshot) return [];
  const entries = [
    {
      capability: "accepted_material_basis",
      ...snapshot.quest_creation.accepted_material_basis,
    },
    {
      capability: "first_question_deepfetch",
      ...snapshot.quest_creation.first_question_deepfetch,
    },
    ...snapshot.unavailable,
  ];
  return entries.filter(
    (entry, index) =>
      entries.findIndex((candidate) => candidate.capability === entry.capability) === index,
  );
}

function shellState(snapshot: PublicSnapshot | null, error: string | null): ShellState {
  if (!snapshot) return error ? "first-error" : "loading";
  if (snapshot.readiness.status !== "ready") return "readiness-unavailable";
  return snapshot.research_space.status === "empty" ? "ready-empty" : "ready-active";
}

function questCreationReady(snapshot: PublicSnapshot | null): boolean {
  if (!snapshot) return false;
  const requiredChecks = snapshot.readiness.checks.filter(
    (check) => check.name !== "idea_stage_worker",
  );
  return requiredChecks.length > 0
    ? requiredChecks.every((check) => check.status === "ready")
    : snapshot.readiness.status === "ready";
}

function RailButton({
  label,
  glyph,
  active = false,
  unavailable = false,
  onClick,
}: {
  label: string;
  glyph: string;
  active?: boolean;
  unavailable?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      className={active ? "lumen-rail-button active" : "lumen-rail-button"}
      aria-label={label}
      title={unavailable ? `${label} · capability_unavailable` : label}
      disabled={unavailable}
      onClick={onClick}
    >
      <span aria-hidden="true">{glyph}</span>
      {unavailable ? <i aria-hidden="true" /> : null}
    </button>
  );
}

function LumenRail({
  canCreate,
  onCreate,
}: {
  canCreate: boolean;
  onCreate: () => void;
}) {
  return (
    <nav className="lumen-rail" aria-label="主导航" data-shell-region="rail">
      <RailButton label="Quest 总览" glyph="⌂" active />
      <RailButton label="问题树" glyph="树" unavailable />
      <RailButton label="Research Asset" glyph="▤" unavailable />
      <RailButton label="Writing" glyph="✎" unavailable />
      <RailButton label="历史" glyph="↺" unavailable />
      <RailButton label="HumanRequest" glyph="!" unavailable />
      <RailButton
        label="创建 Quest"
        glyph="＋"
        unavailable={!canCreate}
        onClick={onCreate}
      />
      <span className="lumen-rail-spacer" aria-hidden="true" />
      <RailButton label="用户入口" glyph="M" unavailable />
    </nav>
  );
}

function LoadingHero() {
  return (
    <>
      <p className="lumen-eyebrow">Production Snapshot · loading</p>
      <h1 id="workspace-title">
        正在连接本地研究空间。<br />
        <em>光谱台会留在这里。</em>
      </h1>
      <p>版本、readiness 与 canonical space 会从认证后的公开 Snapshot 进入同一个窗口。</p>
      <div className="lumen-inline-state" role="status">
        <span className="lumen-spinner" aria-hidden="true" />
        <div>
          <b>读取生产 Projection</b>
          <small>不会用浏览器 fixture 填充研究状态</small>
        </div>
      </div>
    </>
  );
}

function FirstErrorHero({ retry }: { retry: () => void }) {
  return (
    <>
      <p className="lumen-eyebrow coral">Snapshot · unavailable</p>
      <h1 id="workspace-title">
        研究空间暂时无法读取。<br />
        <em>Shell 仍然可辨认。</em>
      </h1>
      <p>daemon 没有返回首个 Snapshot。检查本地服务后，从这里重新读取。</p>
      <button className="lumen-primary" type="button" onClick={retry}>
        重新读取 Snapshot
      </button>
    </>
  );
}

function SnapshotHero({ snapshot }: { snapshot: PublicSnapshot }) {
  const ready = snapshot.readiness.status === "ready";
  const empty = snapshot.research_space.status === "empty";
  const creation = snapshot.quest_creation.current;
  const ideaStage = snapshot.idea_stage ?? null;

  if (!ready) {
    const failedChecks = snapshot.readiness.checks
      .filter((check) => check.status !== "ready")
      .map((check) => `${check.name}:${check.status}`);
    return (
      <>
        <p className="lumen-eyebrow coral">Readiness · typed unavailable</p>
        <h1 id="workspace-title">
          Snapshot 已返回。<br />
          <em>本地底座还未就绪。</em>
        </h1>
        <p>研究空间保持只读；修复 readiness 后，这个窗口会继续接收同一条 Projection。</p>
        <div className="lumen-inline-state unavailable" role="status">
          <span aria-hidden="true">!</span>
          <div>
            <b>readiness_unavailable</b>
            <small>{failedChecks.join(" · ") || "readiness:unavailable"}</small>
          </div>
        </div>
      </>
    );
  }

  if (empty) {
    return (
      <>
        <p className="lumen-eyebrow">Canonical empty · production Snapshot</p>
        <h1 id="workspace-title">
          {creation ? "首个 Quest 正在形成。" : "这里还没有 Quest。"}
          <br />
          <em>{creation ? "从同一个草案继续。" : "从一个清楚的问题开始。"}</em>
        </h1>
        <p>
          {creation
            ? "当前 initialization 已持久保存；使用左侧 ＋ 回到连续创建窗口。"
            : "使用左侧固定的 ＋ 创建入口，设定 Quest，并决定第一个研究问题。"}
        </p>
        <div className="lumen-inline-state ready" role="status">
          <span aria-hidden="true">✓</span>
          <div>
            <b>本地研究空间已就绪</b>
            <small>0 Quest · 0 Question · direct {snapshot.quest_creation.status}</small>
          </div>
        </div>
      </>
    );
  }

  if (ideaStage) {
    return (
      <IdeaStageHero
        ideaStage={ideaStage}
        question={ideaQuestion(ideaStage, snapshot)}
      />
    );
  }

  return (
    <>
      <p className="lumen-eyebrow">Research space · current Projection</p>
      <h1 id="workspace-title">
        研究已经在这里。<br />
        <em>从当前问题继续。</em>
      </h1>
      <p>Snapshot 只报告已接纳的研究事实；执行完成、资产接纳和 Stage 推进仍保持分离。</p>
      <div className="lumen-inline-state ready" role="status">
        <span aria-hidden="true">✓</span>
        <div>
          <b>{snapshot.research_space.quest_count} 个 Quest</b>
          <small>
            {snapshot.research_space.question_count} Question · {snapshot.research_space.foreground_cycle_count} foreground cycle
          </small>
        </div>
      </div>
    </>
  );
}

type IdeaStageState =
  | "eligibility"
  | "stage-run-request"
  | "run"
  | "awaiting-acceptance"
  | "stage-commit";

type IdeaFactState = "pending" | "current" | "done" | "blocked";

type IdeaStageHealthBlocker = {
  code: string;
};

function ideaStageHealthBlocker(
  snapshot: PublicSnapshot | null,
): IdeaStageHealthBlocker | null {
  const worker = snapshot?.readiness.checks.find(
    (check) => check.name === "idea_stage_worker" && check.status !== "ready",
  );
  if (!worker) return null;
  return { code: worker.reason?.code ?? `idea_stage_worker_${worker.status}` };
}

function currentIdeaStageState(ideaStage: IdeaStageProjection): IdeaStageState {
  if (ideaStage.stage_commit) return "stage-commit";
  if (ideaStage.outcome_acceptance.status !== "not_attempted") {
    return "awaiting-acceptance";
  }
  if (ideaStage.run) return "run";
  if (ideaStage.stage_run_request) return "stage-run-request";
  return "eligibility";
}

function ideaQuestion(
  ideaStage: IdeaStageProjection,
  snapshot?: PublicSnapshot,
): IdeaQuestionSummary {
  const creation = snapshot?.quest_creation.current;
  return {
    quest_ref: creation?.quest_ref,
    question_ref: ideaStage.eligibility.question_ref ?? creation?.question_ref,
    graph_revision: snapshot?.owners.research_graph?.revision,
    ...(creation?.proposal?.content ?? {}),
    ...(ideaStage.stage_run_request?.accepted_question_binding ?? {}),
    ...(snapshot?.research_space.current_question ?? {}),
  };
}

function IdeaStageHero({
  ideaStage,
  question,
}: {
  ideaStage: IdeaStageProjection;
  question: IdeaQuestionSummary;
}) {
  const committed = Boolean(ideaStage.stage_commit);
  const nextStage = ideaStage.stage_commit?.next_stage?.toLowerCase();
  const headline = committed
    ? "Idea 已形成正式交接。"
    : "从已接纳的问题出发。";
  const emphasis = committed
    ? "执行、接纳与推进仍然分开。"
    : "Idea 正在形成。";

  return (
    <>
      <p className="lumen-eyebrow">Research cycle · current Projection</p>
      <h1 id="workspace-title">
        {headline}<br />
        <em>{emphasis}</em>
      </h1>
      <p>
        {question.unknown_statement
          ?? "当前 Idea Stage 只消费已接纳 Question 与冻结 ContextPack，不创建 Question 或选择 canonical Idea。"}
      </p>
      <ol className="lumen-stage-strip" aria-label="当前研究周期的四个 Stage">
        <li
          className={committed ? "done" : "current"}
          aria-current={committed ? undefined : "step"}
        >
          <small>{committed ? "01 · COMMITTED" : "01 · NOW"}</small>
          <b>Idea</b>
        </li>
        <li
          className={nextStage === "plan" ? "current" : undefined}
          aria-current={nextStage === "plan" ? "step" : undefined}
        >
          <small>02 · {nextStage === "plan" ? "NOW" : "NEXT"}</small>
          <b>Plan</b>
        </li>
        <li
          className={nextStage === "bundle" ? "current" : undefined}
          aria-current={nextStage === "bundle" ? "step" : undefined}
        >
          <small>03 · {nextStage === "bundle" ? "NOW" : "LATER"}</small>
          <b>Bundle</b>
        </li>
        <li
          className={nextStage === "reasoning" ? "current" : undefined}
          aria-current={nextStage === "reasoning" ? "step" : undefined}
        >
          <small>04 · {nextStage === "reasoning" ? "NOW" : "REQUIRED"}</small>
          <b>Reasoning</b>
        </li>
      </ol>
    </>
  );
}

function CurrentQuestionCard({
  ideaStage,
  question,
}: {
  ideaStage: IdeaStageProjection;
  question: IdeaQuestionSummary;
}) {
  const questionRef = question.question_ref
    ?? ideaStage.stage_run_request?.accepted_question_binding?.question_ref
    ?? "accepted Question";
  const graphRevision = question.graph_revision;

  return (
    <section
      className="lumen-card lumen-question-card"
      aria-labelledby="current-question-title"
      data-testid="current-question-card"
    >
      <header className="lumen-card-head">
        <b id="current-question-title">当前 Question</b>
        <small>
          {graphRevision === undefined ? "Research Graph · 只读投影" : `Graph r${graphRevision} · 只读投影`}
        </small>
      </header>
      <div className="lumen-question-path" aria-label="当前 Question 与 Idea Stage 路径">
        <span className="quest"><small>Quest</small><b>{question.quest_ref ?? "current"}</b></span>
        <i aria-hidden="true" />
        <span className="question"><small>Formal Question</small><b>{questionRef}</b></span>
        <i aria-hidden="true" />
        <span className="idea"><small>Current Stage</small><b>Idea</b></span>
      </div>
      <div className="lumen-question-copy">
        <small>Unknown / answer shape / scope</small>
        <h2>{question.unknown_statement ?? question.title ?? "当前已接纳 Question"}</h2>
        <p>
          {question.applicability_scope
            ?? question.answer_shape
            ?? "Question 内容由 Research Graph 拥有；这个页面只消费公开 Projection。"}
        </p>
      </div>
    </section>
  );
}

function reasonCode(reason: unknown): string | null {
  if (typeof reason === "string" && reason) return reason;
  if (!reason || typeof reason !== "object" || !("code" in reason)) return null;
  return typeof reason.code === "string" ? reason.code : null;
}

function receiptRef(value: unknown): string | null {
  if (!value || typeof value !== "object" || !("receipt_ref" in value)) return null;
  return typeof value.receipt_ref === "string" ? value.receipt_ref : null;
}

function receiptKind(value: unknown): string | null {
  if (!value || typeof value !== "object" || !("kind" in value)) return null;
  return typeof value.kind === "string" ? value.kind : null;
}

function receiptSubject(value: unknown): string | null {
  if (!value || typeof value !== "object" || !("subject_ref" in value)) return null;
  return typeof value.subject_ref === "string" ? value.subject_ref : null;
}

function isRunBlocked(status: string): boolean {
  return ["blocked", "unavailable", "failed", "fenced", "outcome_unknown"].includes(
    status,
  );
}

function ideaFactRows(
  ideaStage: IdeaStageProjection,
  phase: IdeaStageState,
): Array<{
  slot: string;
  label: string;
  owner: string;
  state: IdeaFactState;
  title: string;
  status: string;
}> {
  const eligibility = ideaStage.eligibility;
  const request = ideaStage.stage_run_request;
  const run = ideaStage.run;
  const acceptance = ideaStage.outcome_acceptance;
  const commit = ideaStage.stage_commit;
  const outcomeKind = acceptance.outcome_kind ?? commit?.outcome_kind ?? "Idea outcome";
  const eligibilityBlocked = !["eligible", "requested", "consumed"].includes(
    eligibility.status,
  );
  const acceptanceBlocked = ["rejected", "stale", "needs_input"].includes(
    acceptance.status,
  );

  let acceptanceTitle = "尚未提交 Owner 接纳";
  if (acceptance.status === "awaiting_content") {
    acceptanceTitle = `Attempt 执行证据已形成；${outcomeKind} 正等待 Research Memory 接纳内容`;
  } else if (acceptance.status === "awaiting_domain") {
    acceptanceTitle = `Attempt 执行证据已形成；${outcomeKind} 正等待 Research Graph 接纳`;
  } else if (acceptance.status === "accepted") {
    acceptanceTitle = run?.status === "completed"
      ? `${outcomeKind} 已由 Research Graph 接纳；Run completion 已独立形成`
      : `${outcomeKind} 已由 Research Graph 接纳；仍未等于 Run completed 或 Stage 推进`;
  } else if (acceptance.status === "rejected") {
    acceptanceTitle = `${outcomeKind} 已被退回；current Session 将依据反馈修订重提`;
  } else if (acceptance.status === "stale") {
    acceptanceTitle = `${outcomeKind} 的 frozen basis 已陈旧，不能继续推进`;
  } else if (acceptance.status === "needs_input") {
    acceptanceTitle = `${outcomeKind} 需要精确输入；相关工作保持等待`;
  }

  return [
    {
      slot: "eligibility",
      label: "Idea eligibility",
      owner: "AE",
      state: eligibilityBlocked
        ? "blocked"
        : phase === "eligibility" ? "current" : "done",
      title: eligibility.status === "eligible"
        ? "首个 Idea Stage 已具备启动资格"
        : eligibility.status === "requested"
          ? "启动资格已由 current StageRunRequest 消费"
          : `Idea eligibility · ${eligibility.status}`,
      status: eligibility.status,
    },
    {
      slot: "stage-run-request",
      label: "StageRunRequest",
      owner: "AE",
      state: request
        ? phase === "stage-run-request" ? "current" : "done"
        : "pending",
      title: request
        ? "已冻结 AcceptedQuestionBinding 与 Idea ContextPack"
        : "等待 Advancement Engine 签发冻结请求",
      status: request ? request.status ?? "issued" : "not_issued",
    },
    {
      slot: "run",
      label: "Run",
      owner: "AR",
      state: run
        ? isRunBlocked(run.status)
          ? "blocked"
          : run.status === "completed"
            ? "done"
            : run.status === "awaiting_acceptance" || phase === "run"
              ? "current"
              : "done"
        : "pending",
      title: !run
        ? "等待 Agent Runtime admission"
        : isRunBlocked(run.status)
          ? "Run 被类型化 blocker 阻塞；不会伪造 Idea outcome"
          : run.status === "completed"
            ? "Owner 接纳已验证，Run 已正式完成"
            : run.status === "awaiting_acceptance"
              ? "Attempt 执行证据已形成；Run 等待 Owner 接纳后完成"
              : run.status === "admitted"
                ? "Agent Runtime 已 admission；实际 Idea Skill 尚未形成 Attempt 执行证据"
                : "Run 正在执行实际 Idea Skill",
      status: run?.status ?? "not_created",
    },
    {
      slot: "outcome-acceptance",
      label: "awaiting acceptance",
      owner: "RM / RG",
      state: acceptanceBlocked
        ? "blocked"
        : acceptance.status === "accepted"
          ? "done"
          : phase === "awaiting-acceptance" ? "current" : "pending",
      title: acceptanceTitle,
      status: acceptance.status,
    },
    {
      slot: "stage-commit",
      label: "StageCommit",
      owner: "AE",
      state: commit
        ? "done"
        : acceptance.status === "accepted" ? "current" : "pending",
      title: commit
        ? `StageCommit(${commit.status}) 已形成`
        : "尚无 StageCommit；不会把前四项合并为 success",
      status: commit?.status ?? "not_committed",
    },
  ];
}

function IdeaDetail({ label, value }: { label: string; value?: string | number | null }) {
  if (value === undefined || value === null || value === "") return null;
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function IdeaStageCard({
  ideaStage,
  healthBlocker,
}: {
  ideaStage: IdeaStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
}) {
  const phase = currentIdeaStageState(ideaStage);
  const rows = ideaFactRows(ideaStage, phase);
  const request = ideaStage.stage_run_request;
  const run = ideaStage.run;
  const acceptance = ideaStage.outcome_acceptance;
  const commit = ideaStage.stage_commit;

  return (
    <section
      className="lumen-card lumen-idea-card"
      aria-labelledby="idea-stage-title"
      data-testid="idea-stage-card"
      data-idea-stage-state={phase}
    >
      <header className="lumen-card-head">
        <b id="idea-stage-title">Idea 的五层事实</b>
        <small>execution ≠ acceptance ≠ advancement</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="idea-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>Idea 自动推进暂时不可用</b>
            <small>已完成的请求和运行记录仍在；worker 恢复后会从当前位置继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      <div className="lumen-idea-facts" role="list">
        {rows.map((row) => (
          <article
            key={row.slot}
            className="lumen-idea-fact"
            data-idea-slot={row.slot}
            data-state={row.state}
            role="listitem"
          >
            <span className="lumen-idea-fact-mark" aria-hidden="true">
              {row.state === "done" ? "✓" : row.state === "blocked" ? "!" : "→"}
            </span>
            <div>
              <small>{row.label}</small>
              <b>{row.title}</b>
              <code>{row.status}</code>
            </div>
            <span>{row.owner}</span>
          </article>
        ))}
      </div>
      <details className="lumen-idea-details">
        <summary>查看 Idea 运行身份与 receipt</summary>
        <dl>
          <IdeaDetail label="Cycle" value={ideaStage.eligibility.cycle_ref} />
          <IdeaDetail
            label="Eligibility reason"
            value={reasonCode(ideaStage.eligibility.reason)}
          />
          <IdeaDetail
            label="StageRunRequest"
            value={request?.request_ref ?? request?.stage_run_request_ref}
          />
          <IdeaDetail
            label="StageRunRequest receipt"
            value={receiptRef(request?.receipt)}
          />
          <IdeaDetail
            label="StageRunRequest receipt kind"
            value={receiptKind(request?.receipt)}
          />
          <IdeaDetail
            label="AcceptedQuestionBinding"
            value={request?.accepted_question_binding?.ref
              ?? request?.accepted_question_binding?.binding_ref
              ?? request?.accepted_question_binding?.question_ref}
          />
          <IdeaDetail
            label="Accepted Question content"
            value={request?.accepted_question_binding?.content_ref
              ?? request?.accepted_question_binding?.question_content_ref}
          />
          <IdeaDetail
            label="Question content receipt"
            value={receiptRef(request?.accepted_question_binding?.content_receipt)}
          />
          <IdeaDetail
            label="Question identity receipt"
            value={receiptRef(request?.accepted_question_binding?.question_receipt)}
          />
          <IdeaDetail label="ContextPack" value={request?.context_pack_ref} />
          <IdeaDetail label="ContextPack hash" value={request?.context_pack_hash} />
          <IdeaDetail label="Run" value={run?.run_ref} />
          <IdeaDetail
            label="Attempt"
            value={run?.attempt_ref
              ? `${run.attempt_ref}${run.attempt_generation === undefined ? "" : ` · generation ${run.attempt_generation}`}`
              : null}
          />
          <IdeaDetail label="Submission" value={run?.submission_ref} />
          <IdeaDetail label="Root Session" value={run?.root_session_ref} />
          <IdeaDetail label="Native Session" value={run?.native_session_ref} />
          <IdeaDetail
            label="Primary provider operation"
            value={run?.provider_operations?.primary?.invocation_ref
              ? `${run.provider_operations.primary.invocation_ref} · ${run.provider_operations.primary.status ?? "unknown"}`
              : null}
          />
          <IdeaDetail
            label="Review provider operation"
            value={run?.provider_operations?.review?.invocation_ref
              ? `${run.provider_operations.review.invocation_ref} · ${run.provider_operations.review.status ?? "unknown"}`
              : null}
          />
          <IdeaDetail
            label="Primary draft checkpoint"
            value={run?.primary_draft_checkpoint?.status}
          />
          <IdeaDetail
            label="Primary draft hash"
            value={run?.primary_draft_checkpoint?.draft_hash}
          />
          <IdeaDetail
            label="Primary adapter"
            value={run?.primary_draft_checkpoint?.adapter_kind}
          />
          <IdeaDetail
            label="Execution Fence"
            value={run?.fence_ref
              ? `${run.fence_ref}${run.fence_status ? ` · ${run.fence_status}` : ""}`
              : null}
          />
          <IdeaDetail label="Run blocker" value={reasonCode(run?.blocker)} />
          <IdeaDetail
            label="Attempt execution receipt"
            value={receiptRef(run?.attempt_execution_receipt)}
          />
          <IdeaDetail
            label="Attempt execution receipt kind"
            value={receiptKind(run?.attempt_execution_receipt)}
          />
          <IdeaDetail
            label="Attempt execution subject"
            value={receiptSubject(run?.attempt_execution_receipt)}
          />
          <IdeaDetail
            label="Run completion receipt"
            value={receiptRef(run?.completion_receipt)}
          />
          <IdeaDetail
            label="Run completion receipt kind"
            value={receiptKind(run?.completion_receipt)}
          />
          <IdeaDetail
            label="Independent reviewer"
            value={run?.review?.reviewer_session_ref}
          />
          <IdeaDetail label="Outcome" value={acceptance.outcome_ref} />
          <IdeaDetail
            label="Outcome rejection"
            value={reasonCode(acceptance.rejection)}
          />
          <IdeaDetail
            label="Content acceptance reason"
            value={reasonCode(acceptance.content.reason)}
          />
          <IdeaDetail
            label="Content receipt"
            value={receiptRef(acceptance.content.receipt ?? acceptance.content)}
          />
          <IdeaDetail
            label="Domain acceptance reason"
            value={reasonCode(acceptance.domain.reason)}
          />
          <IdeaDetail
            label="Domain receipt"
            value={receiptRef(acceptance.domain.receipt ?? acceptance.domain)}
          />
          <IdeaDetail
            label="Domain receipt kind"
            value={receiptKind(acceptance.domain.receipt ?? acceptance.domain)}
          />
          <IdeaDetail
            label="StageCommit"
            value={commit?.commit_ref ?? commit?.stage_commit_ref}
          />
          <IdeaDetail label="StageCommit receipt" value={receiptRef(commit?.receipt)} />
          <IdeaDetail
            label="StageCommit receipt kind"
            value={receiptKind(commit?.receipt)}
          />
        </dl>
      </details>
    </section>
  );
}

function WorkspaceMain({
  snapshot,
  state,
  error,
  streamInterrupted,
  retry,
}: {
  snapshot: PublicSnapshot | null;
  state: ShellState;
  error: string | null;
  streamInterrupted: boolean;
  retry: () => void;
}) {
  const unavailable = uniqueUnavailable(snapshot);
  const ideaStage = snapshot?.research_space.status === "active"
    ? snapshot.idea_stage ?? null
    : null;
  const ideaHealthBlocker = ideaStageHealthBlocker(snapshot);
  return (
    <main
      id="main-content"
      className="lumen-main"
      data-shell-region="main"
      tabIndex={0}
      aria-labelledby="workspace-title"
      aria-busy={state === "loading"}
    >
      {(error || streamInterrupted) && snapshot ? (
        <div className="lumen-reconnect-warning" role="alert">
          <span aria-hidden="true">↺</span>
          <p>
            <b>Projection 连接中断，正在重连。</b>
            <small>继续显示最后一次可用的单调 Snapshot · rev {snapshot.revision}</small>
          </p>
        </div>
      ) : null}

      <section
        className={`lumen-hero ${state}`}
        role={state === "first-error" ? "alert" : undefined}
      >
        <span className="lumen-glow" aria-hidden="true" />
        <span className="lumen-comet" aria-hidden="true" />
        {state === "loading" ? <LoadingHero /> : null}
        {state === "first-error" ? <FirstErrorHero retry={retry} /> : null}
        {snapshot ? <SnapshotHero snapshot={snapshot} /> : null}
      </section>

      <div className="lumen-lower">
        {ideaStage ? (
          <CurrentQuestionCard
            ideaStage={ideaStage}
            question={ideaQuestion(ideaStage, snapshot ?? undefined)}
          />
        ) : (
          <section className="lumen-card lumen-next-card" aria-labelledby="next-title">
            <header className="lumen-card-head">
              <b id="next-title">当前空间</b>
              <small>{snapshot ? `rev ${snapshot.revision}` : "等待 Snapshot"}</small>
            </header>
            <div className="lumen-path" aria-hidden="true">
              <span className="origin">MR</span>
              <i />
              <span className="destination">＋</span>
            </div>
            <h2>{state === "ready-empty" ? "第一个 Quest 从左侧入口开始" : "公开 Projection 决定这里显示什么"}</h2>
            <p>浏览不会写入 Owner。创建、授权与接纳始终经过各自的公开产品流程。</p>
          </section>
        )}

        {ideaStage ? (
          <IdeaStageCard
            ideaStage={ideaStage}
            healthBlocker={ideaHealthBlocker}
          />
        ) : (
          <section className="lumen-card lumen-availability" aria-labelledby="availability-title">
            <header className="lumen-card-head">
              <b id="availability-title">能力可用性</b>
              <small>公开 Snapshot</small>
            </header>
            {unavailable.length ? (
              <ul>
                {unavailable.map((item) => (
                  <li key={item.capability}>
                    <span>{capabilityLabels[item.capability] ?? item.capability}</span>
                    <code>{item.status}</code>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="lumen-card-empty">
                {snapshot ? "Snapshot 没有报告 typed unavailable。" : "首个 Snapshot 返回后显示。"}
              </p>
            )}
            {snapshot ? (
              <details className="lumen-technical-details">
                <summary>查看运行详情</summary>
                <dl>
                  <div><dt>版本</dt><dd>{snapshot.product.version}</dd></div>
                  <div><dt>Readiness</dt><dd>{snapshot.readiness.status}</dd></div>
                  {Object.entries(snapshot.owners).map(([name, owner]) => (
                    <div key={name}>
                      <dt>{ownerLabels[name] ?? name}</dt>
                      <dd>{owner.status} · r{owner.revision}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            ) : null}
          </section>
        )}
      </div>
    </main>
  );
}

function CompanionShell({ state }: { state: ShellState }) {
  const copy: Record<ShellState, { label: string; message: string }> = {
    loading: {
      label: "正在建立上下文",
      message: "我会在首个 Snapshot 返回后，用同一个窗口解释研究空间。",
    },
    "first-error": {
      label: "本地连接不可用",
      message: "首个 Snapshot 尚未返回；Shell 保持在位，修复 daemon 后可以重新读取。",
    },
    "readiness-unavailable": {
      label: "底座尚未就绪",
      message: "readiness 当前不可用。研究浏览保持只读，不会猜测或补写 Owner 状态。",
    },
    "ready-empty": {
      label: "研究空间已就绪",
      message: "这里还没有 Quest。使用左侧 ＋ 后，我会继续留在这个位置。",
    },
    "ready-active": {
      label: "跟随当前 Projection",
      message: "我会在这里解释研究状态；普通聊天不会直接写入领域事实。",
    },
  };

  return (
    <aside
      className="lumen-companion"
      aria-label="Quest Companion"
      data-shell-region="companion"
      tabIndex={0}
    >
      <header className="lumen-companion-head">
        <span className="lumen-orb" aria-hidden="true" />
        <div>
          <b>Quest Companion</b>
          <small>贯穿研究空间的高频入口</small>
        </div>
        <code>capability_unavailable</code>
      </header>
      <div className="lumen-chat" aria-live="polite">
        <article className="lumen-message">
          <small>{copy[state].label}</small>
          {copy[state].message}
        </article>
        <article className="lumen-proposal">
          <small>当前边界 · 无写入</small>
          <b>对话能力尚未启用</b>
          <p>这个固定位置不会被 capability list、Owner revision 或 receipt rail 取代。</p>
        </article>
      </div>
      <div className="lumen-compose">
        <div>
          <input aria-label="给 Quest Companion 发消息" disabled placeholder="Quest Companion 尚未启用" />
          <button type="button" disabled aria-label="发送消息">↑</button>
        </div>
        <small>普通聊天不会被猜成硬命令</small>
      </div>
    </aside>
  );
}

function App() {
  const [snapshot, setSnapshot] = useState<PublicSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [streamInterrupted, setStreamInterrupted] = useState(false);
  const [creationMode, setCreationMode] = useState<"current" | "new" | null>(
    () => {
      const panel = new URLSearchParams(window.location.search).get("panel");
      return panel === "new-quest" ? "new" : panel === "create-quest" ? "current" : null;
    },
  );
  const [streamCursor, setStreamCursor] = useState<number | null>(null);
  const [snapshotRetrySequence, setSnapshotRetrySequence] = useState(0);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);

  const handleConnection = useCallback((next: boolean) => {
    setConnected(next);
    if (next) {
      setStreamInterrupted(false);
    } else {
      setStreamInterrupted(true);
    }
  }, []);

  const reload = useCallback(async (signal?: AbortSignal) => {
    if (reloadInFlight.current) {
      reloadQueued.current = true;
      return;
    }
    reloadInFlight.current = true;
    try {
      do {
        reloadQueued.current = false;
        try {
          const next = await fetchSnapshot(signal);
          setSnapshot((current) =>
            current && current.revision > next.revision ? current : next,
          );
          setStreamCursor((current) =>
            current === null ? next.revision : Math.max(current, next.revision),
          );
          setSnapshotRetrySequence(0);
          setError(null);
        } catch (caught) {
          if ((caught as Error).name !== "AbortError") {
            setSnapshotRetrySequence((current) => current + 1);
            setError("无法读取本地 Snapshot。请确认 daemon 仍在运行，然后刷新页面。");
          }
        }
      } while (reloadQueued.current && !signal?.aborted);
    } finally {
      reloadInFlight.current = false;
      if (reloadQueued.current && !signal?.aborted) void reload();
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void reload(controller.signal);
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!error || snapshotRetrySequence === 0) return;
    const exponent = Math.min(snapshotRetrySequence - 1, 4);
    const timer = window.setTimeout(
      () => void reload(),
      Math.min(250 * 2 ** exponent, 4_000),
    );
    return () => window.clearTimeout(timer);
  }, [error, reload, snapshotRetrySequence]);

  useEffect(() => {
    if (streamCursor === null) return;
    return followProjection(
      streamCursor,
      () => void reload(),
      () => void reload(),
      handleConnection,
    );
  }, [handleConnection, reload, streamCursor]);

  const state = shellState(snapshot, error);
  const canCreate = questCreationReady(snapshot);
  const openCreation = () => {
    if (!canCreate) return;
    window.history.replaceState(null, "", "/?panel=create-quest");
    setCreationMode("current");
  };
  const closeCreation = () => {
    window.history.replaceState(null, "", "/");
    setCreationMode(null);
  };

  return (
    <>
      <a className="lumen-skip" href="#main-content">跳到主要内容</a>
      <div className="lumen-shell" data-testid="product-shell" data-shell-state={state}>
        <header className="lumen-header" data-shell-region="header">
          <div className="lumen-brand" aria-label="Meta-research">
            <span className="lumen-logo" aria-hidden="true">MR</span>
            <div><b>Meta Research</b><small>Lumen workspace</small></div>
          </div>
          <div className="lumen-quest-context">
            <small>{snapshot?.research_space.status === "active" ? "QUEST / CURRENT" : "QUEST / NEW SPACE"}</small>
            <b>{snapshot?.research_space.status === "active" ? "当前研究空间" : "等待第一个 Quest"}</b>
          </div>
          <div className={`lumen-connection ${connected ? "connected" : ""}`} aria-live="polite">
            <i aria-hidden="true" />
            <span>
              {error || streamInterrupted ? "Projection 正在重连" : connected ? "Projection 实时连接" : snapshot ? "正在连接 Projection" : "读取 Snapshot"}
            </span>
            {snapshot ? <code>rev {snapshot.revision}</code> : null}
          </div>
        </header>
        <LumenRail canCreate={Boolean(canCreate)} onCreate={openCreation} />
        <WorkspaceMain
          snapshot={snapshot}
          state={state}
          error={error}
          streamInterrupted={streamInterrupted}
          retry={() => void reload()}
        />
        <CompanionShell state={state} />
      </div>
      {creationMode && snapshot ? (
        <QuestCreationWorkbench
          current={creationMode === "new" ? null : snapshot.quest_creation.current}
          onClose={closeCreation}
          onChanged={() => void reload()}
        />
      ) : null}
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
