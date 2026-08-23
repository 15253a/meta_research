import {
  StrictMode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Ref,
} from "react";
import { createRoot } from "react-dom/client";
import {
  acknowledgeAssetIntake,
  adaptManualQuestionCreation,
  cancelManualQuestionCreation,
  confirmManualCreationSeed,
  confirmManualDeepFetchWaiver,
  confirmManualQuestionProposal,
  fetchAssetIntake,
  fetchCurrentManualQuestionCreation,
  fetchLiteratureSnapshot,
  fetchManualQuestionCreation,
  fetchSnapshot,
  followProjection,
  openManualQuestionCreation,
  ProductError,
  saveManualQuestionProposal,
  sendManualDraftingMessage,
  startManualCreationDeepFetch,
  submitAssetIntake,
  type AssetIntakeRequest,
  type AssetIntakeResult,
  type AssetReceipt,
  type ExperimentProjection,
  type IdeaQuestionSummary,
  type IdeaStageProjection,
  type ManualAcceptedMaterialBinding,
  type ManualQuestionCreationRawView,
  type HumanRequestItem,
  type PlanStageProjection,
  type PublicSnapshot,
  type QuestionTreeItem,
  type UnavailableCapability,
} from "./api";
import {
  ManualCreation,
  type ManualCreationMaterialDraft,
  type ManualQuestionCreationView,
} from "./ManualCreation";
import {
  CurrentExperimentSummary,
  ExecutionObserver,
  ExperimentToolbarEntry,
  useExecutionObserver,
} from "./ExecutionObserver";
import { ExperimentLauncher } from "./ExperimentLauncher";
import { QuestCreationWorkbench } from "./QuestCreation";
import { QuestionTree } from "./QuestionTree";
import { ResearchAssetsWorkbench } from "./ResearchAssets";
import { WritingReportWorkbench } from "./WritingReport";
import {
  HumanRequestSurface,
  QuestCompanion,
} from "./HumanCollaboration";
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

const MANUAL_MAX_MATERIALS = 100;
const MANUAL_MAX_ASSET_BYTES = 64 * 1024 * 1024;

type AcceptedAssetReceipt = AssetReceipt & { status: "accepted" };

type ManualPanelState = {
  raw: ManualQuestionCreationRawView;
  parent: QuestionTreeItem;
  opener: HTMLButtonElement;
  researchReceipt: AcceptedAssetReceipt | null;
};

function isAcceptedAssetReceipt(value: unknown): value is AcceptedAssetReceipt {
  if (!value || typeof value !== "object") return false;
  const receipt = value as Partial<AcceptedAssetReceipt>;
  return receipt.status === "accepted" &&
    typeof receipt.issuer === "string" &&
    typeof receipt.kind === "string" &&
    typeof receipt.receipt_ref === "string" &&
    typeof receipt.subject_ref === "string" &&
    typeof receipt.payload_hash === "string";
}

async function hydrateManualResearchReceipt(
  raw: ManualQuestionCreationRawView,
  signal?: AbortSignal,
): Promise<AcceptedAssetReceipt | null> {
  const deepfetch = raw.research_path.deepfetch;
  const embedded = deepfetch?.literature_snapshot;
  if (embedded && isAcceptedAssetReceipt(embedded.receipt)) {
    const identityMatches =
      (embedded.snapshot_ref === undefined || embedded.snapshot_ref === deepfetch.snapshot_ref) &&
      (embedded.request_ref === undefined || embedded.request_ref === deepfetch.request_ref) &&
      (embedded.creation_context_kind === undefined ||
        embedded.creation_context_kind === "manual_question_creation") &&
      (embedded.creation_context_ref === undefined ||
        embedded.creation_context_ref === raw.context_ref) &&
      (embedded.quest_ref === undefined || embedded.quest_ref === raw.quest_ref);
    if (identityMatches) return embedded.receipt;
  }
  if (deepfetch?.status !== "succeeded" || !deepfetch.snapshot_ref) return null;

  try {
    const snapshot = await fetchLiteratureSnapshot(deepfetch.snapshot_ref, signal);
    if (
      snapshot.snapshot_ref !== deepfetch.snapshot_ref ||
      snapshot.request_ref !== deepfetch.request_ref ||
      snapshot.creation_context_kind !== "manual_question_creation" ||
      snapshot.creation_context_ref !== raw.context_ref ||
      snapshot.quest_ref !== raw.quest_ref ||
      !isAcceptedAssetReceipt(snapshot.receipt) ||
      snapshot.receipt.subject_ref !== snapshot.snapshot_ref
    ) {
      return null;
    }
    return snapshot.receipt;
  } catch (caught) {
    if ((caught as Error).name === "AbortError") throw caught;
    return null;
  }
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function acceptedManualMaterialBinding(
  initial: AssetIntakeResult,
): Promise<ManualAcceptedMaterialBinding> {
  let result = initial;
  let retryCount = 0;
  while (["queued", "processing"].includes(result.status)) {
    await delay(Math.min(4_000, 250 * 2 ** Math.min(retryCount, 4)));
    result = await fetchAssetIntake(result.job_ref);
    retryCount += 1;
  }

  if (result.status === "failed") {
    acknowledgeAssetIntake(result.job_ref);
    throw new ProductError(result.failure?.code ?? "manual_material_intake_failed");
  }
  if (result.status !== "accepted" || !result.asset) {
    acknowledgeAssetIntake(result.job_ref);
    throw new ProductError("manual_material_intake_result_invalid");
  }
  if (!isAcceptedAssetReceipt(result.asset.receipt)) {
    acknowledgeAssetIntake(result.job_ref);
    throw new ProductError("manual_material_receipt_unavailable");
  }

  const binding: ManualAcceptedMaterialBinding = {
    asset_ref: result.asset.asset_ref,
    version_ref: result.asset.version_ref,
    content_hash: result.asset.content_hash,
    manifest_hash: result.asset.manifest_hash,
    receipt: result.asset.receipt,
  };
  acknowledgeAssetIntake(result.job_ref);
  return binding;
}

function manualMaterialDisplayName(value: string): string {
  const name = value.trim();
  if (!name || name.length > 512) {
    throw new ProductError("asset_display_name_invalid");
  }
  return name;
}

async function intakeManualMaterials(
  contextRef: string,
  draft: ManualCreationMaterialDraft,
): Promise<ManualAcceptedMaterialBinding[]> {
  if (draft.mode === "unprovided") return [];

  if (draft.mode === "path") {
    const localPath = draft.local_path.trim();
    const isAbsolute = localPath.startsWith("/") ||
      /^[A-Za-z]:[\\/]/.test(localPath) ||
      /^\\\\/.test(localPath);
    const pathParts = localPath.replaceAll("\\", "/").split("/").filter(Boolean);
    if (!isAbsolute || !pathParts.length || localPath.length > 16_000) {
      throw new ProductError("asset_source_locator_absolute_required");
    }
    const request: AssetIntakeRequest = {
      source_kind: "directory",
      custody_mode: "linked_local",
      display_name: manualMaterialDisplayName(pathParts.at(-1) ?? ""),
      media_type: "application/x-directory",
      source_locator: localPath,
      asynchronous: false,
      provenance: {
        submitted_via: "manual_question_creation",
        creation_context_ref: contextRef,
        selection_mode: "path",
      },
    };
    const result = await submitAssetIntake(request);
    return [await acceptedManualMaterialBinding(result)];
  }

  const files = [...draft.files];
  if (!files.length) throw new ProductError("manual_material_files_required");
  if (files.length > MANUAL_MAX_MATERIALS) {
    throw new ProductError("accepted_material_bindings_invalid");
  }

  const bindings: ManualAcceptedMaterialBinding[] = [];
  for (const file of files) {
    if (file.size > MANUAL_MAX_ASSET_BYTES) {
      throw new ProductError("asset_content_too_large");
    }
    const relativePath = (file as File & { webkitRelativePath?: string })
      .webkitRelativePath ?? "";
    const request: AssetIntakeRequest = {
      source_kind: "file",
      custody_mode: "managed",
      display_name: manualMaterialDisplayName(file.name),
      media_type: file.type || "application/octet-stream",
      content_base64: arrayBufferToBase64(await file.arrayBuffer()),
      asynchronous: false,
      provenance: {
        submitted_via: "manual_question_creation",
        creation_context_ref: contextRef,
        selection_mode: draft.mode,
        relative_path: relativePath || null,
      },
    };
    bindings.push(
      await acceptedManualMaterialBinding(await submitAssetIntake(request)),
    );
  }
  return bindings;
}

type ShellState =
  | "loading"
  | "first-error"
  | "readiness-unavailable"
  | "ready-empty"
  | "ready-active";

type CapabilityState =
  | UnavailableCapability
  | { capability: string; status: "ready" };

const humanRequestPanelByKind: Record<HumanRequestItem["kind"], string> = {
  library_reconnect: "human-request",
  external_material_api_access: "external-request",
  offline_action: "offline-operation",
  capability_authorization: "permission-request",
};

function humanRequestKindFromPanel(
  panel: string | null,
): HumanRequestItem["kind"] | null {
  const entry = Object.entries(humanRequestPanelByKind).find(
    ([, routePanel]) => routePanel === panel,
  );
  return (entry?.[0] as HumanRequestItem["kind"] | undefined) ?? null;
}

function isHumanRequestPanel(panel: string | null): boolean {
  return panel === "human-requests" || humanRequestKindFromPanel(panel) !== null;
}

if (window.location.pathname === "/auth/launch") {
  window.history.replaceState(null, "", "/");
}

function questionTreeUrl(questionRef?: string | null): string {
  const parameters = new URLSearchParams({
    variant: "A",
    view: "questions",
  });
  if (questionRef) parameters.set("node", questionRef);
  parameters.set("panel", "question-tree");
  return `/?${parameters}`;
}

function manualCreationUrl(parentQuestionRef: string): string {
  const parameters = new URLSearchParams({
    variant: "A",
    view: "questions",
    node: parentQuestionRef,
    panel: "create-question",
  });
  return `/?${parameters}`;
}

function uniqueCapabilities(snapshot: PublicSnapshot | null): CapabilityState[] {
  if (!snapshot) return [];
  const entries: CapabilityState[] = [
    {
      capability: "accepted_material_basis",
      ...snapshot.quest_creation.accepted_material_basis,
    },
    {
      capability: "first_question_deepfetch",
      ...snapshot.quest_creation.first_question_deepfetch,
    },
    ...snapshot.unavailable,
    {
      capability: "writing",
      status: snapshot.writing.status,
    },
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
    (check) =>
      ![
        "idea_stage_worker",
        "plan_stage_worker",
        "experiment_worker",
        "writing_worker",
        "research_asset_intake_worker",
        "research_asset_verification_worker",
      ].includes(check.name),
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
  unavailableReason = "capability_unavailable",
  buttonRef,
  attention = false,
  onClick,
}: {
  label: string;
  glyph: string;
  active?: boolean;
  unavailable?: boolean;
  unavailableReason?: string;
  buttonRef?: Ref<HTMLButtonElement>;
  attention?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className={active ? "lumen-rail-button active" : "lumen-rail-button"}
      aria-label={label}
      title={unavailable
        ? `${label} · ${unavailableReason}`
        : attention
          ? `${label} · needs you`
          : label}
      disabled={unavailable}
      onClick={onClick}
    >
      <span aria-hidden="true">{glyph}</span>
      {unavailable || attention ? <i aria-hidden="true" /> : null}
    </button>
  );
}

function LumenRail({
  canCreate,
  canBrowseAssets,
  canBrowseQuestions,
  questionsActive,
  canBrowseWriting,
  writingOpen,
  questionUnavailableReason,
  questionButtonRef,
  writingButtonRef,
  onBrowseQuestions,
  canBrowseHumanRequests,
  humanRequestCount,
  humanRequestsOpen,
  onCreate,
  onBrowseAssets,
  onBrowseWriting,
  onBrowseHumanRequests,
}: {
  canCreate: boolean;
  canBrowseAssets: boolean;
  canBrowseQuestions: boolean;
  questionsActive: boolean;
  canBrowseWriting: boolean;
  writingOpen: boolean;
  questionUnavailableReason: string;
  questionButtonRef: Ref<HTMLButtonElement>;
  writingButtonRef: Ref<HTMLButtonElement>;
  canBrowseHumanRequests: boolean;
  humanRequestCount: number;
  humanRequestsOpen: boolean;
  onCreate: () => void;
  onBrowseAssets: () => void;
  onBrowseWriting: () => void;
  onBrowseQuestions: () => void;
  onBrowseHumanRequests: () => void;
}) {
  return (
    <nav className="lumen-rail" aria-label="主导航" data-shell-region="rail">
      <RailButton label="Quest 总览" glyph="⌂" active={!questionsActive && !writingOpen} />
      <RailButton
        label="问题树"
        glyph="树"
        active={questionsActive}
        unavailable={!canBrowseQuestions}
        unavailableReason={questionUnavailableReason}
        buttonRef={questionButtonRef}
        onClick={onBrowseQuestions}
      />
      <RailButton
        label="Research Asset"
        glyph="▤"
        unavailable={!canBrowseAssets}
        onClick={onBrowseAssets}
      />
      <RailButton
        label="Writing"
        glyph="✎"
        active={writingOpen}
        unavailable={!canBrowseWriting}
        buttonRef={writingButtonRef}
        onClick={onBrowseWriting}
      />
      <RailButton label="历史" glyph="↺" unavailable />
      <RailButton
        label="HumanRequest"
        glyph="!"
        active={humanRequestsOpen}
        unavailable={!canBrowseHumanRequests}
        attention={humanRequestCount > 0}
        onClick={onBrowseHumanRequests}
      />
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
  const planStage = snapshot.plan_stage ?? null;

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
            <small>0 Quest · 0 Question · direct / DeepFetch {snapshot.quest_creation.status}</small>
          </div>
        </div>
      </>
    );
  }

  if (planStage) {
    return (
      <PlanStageHero
        planStage={planStage}
        question={planQuestion(planStage, snapshot)}
      />
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

function planQuestion(
  planStage: PlanStageProjection,
  snapshot?: PublicSnapshot,
): IdeaQuestionSummary {
  const creation = snapshot?.quest_creation.current;
  return {
    quest_ref: creation?.quest_ref,
    question_ref: planStage.eligibility.question_ref ?? creation?.question_ref,
    graph_revision: snapshot?.owners.research_graph?.revision,
    ...(creation?.proposal?.content ?? {}),
    ...(planStage.stage_run_request?.accepted_question_binding ?? {}),
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

function PlanStageHero({
  planStage,
  question,
}: {
  planStage: PlanStageProjection;
  question: IdeaQuestionSummary;
}) {
  const committed = Boolean(planStage.stage_commit);
  const nextStage = planStage.stage_commit?.next_stage?.toLowerCase();
  const noGap = planStage.plan_acceptance.bundle_disposition
    === "no_new_experiment_required";

  return (
    <>
      <p className="lumen-eyebrow">Research cycle · Plan Projection</p>
      <h1 id="workspace-title">
        {committed ? "Plan 已形成正式交接。" : "从已接纳的 IdeaSet 出发。"}<br />
        <em>{committed ? "资产、领域接纳与推进仍然分开。" : "Plan 正在形成。"}</em>
      </h1>
      <p>
        {noGap
          ? "所有 AnswerContract obligations 已覆盖；系统不会伪造 Bundle Run，后续由 Advancement Engine 显式处理 Bundle skip。"
          : question.unknown_statement
            ?? "Plan 只消费精确的 AcceptedQuestionBinding 与完整、已接纳的 IdeaSet；daemon 自动推进，Web 不提供逐 Run 启动或授权。"}
      </p>
      <ol className="lumen-stage-strip" aria-label="当前研究周期的四个 Stage">
        <li className="done">
          <small>01 · COMMITTED</small>
          <b>Idea</b>
        </li>
        <li
          className={committed ? "done" : "current"}
          aria-current={committed ? undefined : "step"}
        >
          <small>{committed ? "02 · COMMITTED" : "02 · NOW"}</small>
          <b>Plan</b>
        </li>
        <li
          className={nextStage === "bundle" ? "current" : undefined}
          aria-current={nextStage === "bundle" ? "step" : undefined}
        >
          <small>
            03 · {noGap ? "SKIP PATH" : nextStage === "bundle" ? "NEXT" : "LATER"}
          </small>
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
  stage,
  question,
  experiment,
  onOpenExperiment,
}: {
  stage: "Idea" | "Plan";
  question: IdeaQuestionSummary;
  experiment: ExperimentProjection | null;
  onOpenExperiment: (trigger: HTMLElement) => void;
}) {
  const questionRef = question.question_ref ?? "accepted Question";
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
        {experiment ? (
          <ExperimentToolbarEntry
            experiment={experiment}
            onOpen={onOpenExperiment}
          />
        ) : null}
      </header>
      <div className="lumen-question-path" aria-label={`当前 Question 与 ${stage} Stage 路径`}>
        <span className="quest"><small>Quest</small><b>{question.quest_ref ?? "current"}</b></span>
        <i aria-hidden="true" />
        <span className="question"><small>Formal Question</small><b>{questionRef}</b></span>
        <i aria-hidden="true" />
        <span className={stage.toLowerCase()}><small>Current Stage</small><b>{stage}</b></span>
      </div>
      <div className="lumen-question-copy">
        <small>Unknown / answer shape / scope</small>
        <h2>{question.unknown_statement ?? question.title ?? "当前已接纳 Question"}</h2>
        <p>
          {question.applicability_scope
            ?? question.answer_shape
            ?? `Question 内容由 Research Graph 拥有；${stage} 页面只消费公开 Projection。`}
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
  experiment,
  questRef,
  onOpenExperiment,
  onExperimentStarted,
}: {
  ideaStage: IdeaStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  experiment: ExperimentProjection | null;
  questRef: string | null;
  onOpenExperiment: (trigger: HTMLElement) => void;
  onExperimentStarted: (experiment: ExperimentProjection) => void;
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
      {experiment ? (
        <CurrentExperimentSummary
          experiment={experiment}
          onOpen={onOpenExperiment}
        />
      ) : null}
      {questRef ? (
        <ExperimentLauncher
          key={`${questRef}:${experiment?.identities.evaluation_attempt_ref ?? "first"}`}
          questRef={questRef}
          sourceExperiment={experiment}
          onStarted={onExperimentStarted}
        />
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
            label="Child-review provider turn"
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
            label="Child reviewer agent"
            value={run?.review?.reviewer_agent_ref}
          />
          <IdeaDetail
            label="Review mode"
            value={run?.review?.review_mode}
          />
          <IdeaDetail
            label="Legacy reviewer Session"
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

type PlanStageState = IdeaStageState;

function planStageHealthBlocker(
  snapshot: PublicSnapshot | null,
): IdeaStageHealthBlocker | null {
  const worker = snapshot?.readiness.checks.find(
    (check) => check.name === "plan_stage_worker" && check.status !== "ready",
  );
  if (!worker) return null;
  return { code: worker.reason?.code ?? `plan_stage_worker_${worker.status}` };
}

function currentPlanStageState(planStage: PlanStageProjection): PlanStageState {
  if (planStage.stage_commit) return "stage-commit";
  if (planStage.plan_acceptance.status !== "not_attempted") {
    return "awaiting-acceptance";
  }
  if (planStage.run) return "run";
  if (planStage.stage_run_request) return "stage-run-request";
  return "eligibility";
}

function planFactRows(
  planStage: PlanStageProjection,
  phase: PlanStageState,
): ReturnType<typeof ideaFactRows> {
  const eligibility = planStage.eligibility;
  const request = planStage.stage_run_request;
  const run = planStage.run;
  const acceptance = planStage.plan_acceptance;
  const commit = planStage.stage_commit;
  const eligibilityBlocked = !["eligible", "requested", "consumed"].includes(
    eligibility.status,
  );
  const acceptanceBlocked = ["rejected", "stale", "needs_input"].includes(
    acceptance.status,
  );

  let acceptanceTitle = "尚未提交 Owner 接纳";
  if (acceptance.status === "awaiting_content") {
    acceptanceTitle = "Attempt 执行证据已形成；PlanDocument 正等待 Research Memory 接纳";
  } else if (acceptance.status === "awaiting_domain") {
    acceptanceTitle = "PlanDocument 已接纳；FormalPlan 正等待 Research Graph 接纳";
  } else if (acceptance.status === "accepted") {
    acceptanceTitle = run?.status === "completed"
      ? "PlanDocument 与 FormalPlan 均已接纳；Run completion 已独立形成"
      : "FormalPlan 已接纳；仍未等于 Run completed 或 Stage 推进";
  } else if (acceptance.status === "rejected") {
    acceptanceTitle = "FormalPlan 已被退回；current Session 将依据结构化反馈修订重提";
  } else if (acceptance.status === "stale") {
    acceptanceTitle = "Plan 的 frozen basis 已陈旧，不能继续推进";
  } else if (acceptance.status === "needs_input") {
    acceptanceTitle = "Plan 需要精确输入；相关工作保持等待";
  }

  return [
    {
      slot: "eligibility",
      label: "Plan eligibility",
      owner: "AE",
      state: eligibilityBlocked
        ? "blocked"
        : phase === "eligibility" ? "current" : "done",
      title: eligibility.status === "eligible"
        ? "已接纳完整 IdeaSet，Plan Stage 具备启动资格"
        : eligibility.status === "requested"
          ? "启动资格已由 current Plan StageRunRequest 消费"
          : `Plan eligibility · ${eligibility.status}`,
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
        ? "已冻结 AcceptedQuestionBinding、AcceptedIdeaSetBinding 与 Plan ContextPack"
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
          ? "Run 被类型化 blocker 阻塞；不会伪造 PlanDocument 或 FormalPlan"
          : run.status === "completed"
            ? "Owner 接纳已验证，Run 已正式完成"
            : run.status === "awaiting_acceptance"
              ? "Attempt 执行证据已形成；Run 等待 Owner 接纳后完成"
              : run.status === "admitted"
                ? "Agent Runtime 已 admission；实际 Plan Skill 尚未形成 Attempt 执行证据"
                : "Run 正在执行实际 Plan Skill",
      status: run?.status ?? "not_created",
    },
    {
      slot: "plan-acceptance",
      label: "Plan acceptance",
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
        : "尚无 StageCommit；不会把执行、资产或领域接纳合并为 success",
      status: commit?.status ?? "not_committed",
    },
  ];
}

function recordText(
  value: Record<string, unknown>,
  ...keys: string[]
): string | null {
  for (const key of keys) {
    const candidate = value[key];
    if (typeof candidate === "string" && candidate) return candidate;
  }
  return null;
}

function PlanStageCard({
  planStage,
  healthBlocker,
}: {
  planStage: PlanStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
}) {
  const phase = currentPlanStageState(planStage);
  const rows = planFactRows(planStage, phase);
  const request = planStage.stage_run_request;
  const ideaSet = request?.accepted_idea_set_binding;
  const run = planStage.run;
  const acceptance = planStage.plan_acceptance;
  const commit = planStage.stage_commit;
  const noGap = acceptance.bundle_disposition === "no_new_experiment_required";

  return (
    <section
      className="lumen-card lumen-idea-card lumen-plan-card"
      aria-labelledby="plan-stage-title"
      data-testid="plan-stage-card"
      data-plan-stage-state={phase}
    >
      <header className="lumen-card-head">
        <b id="plan-stage-title">Plan 的五层事实</b>
        <small>execution ≠ asset ≠ domain ≠ advancement</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="plan-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>Plan 自动推进暂时不可用</b>
            <small>已完成的请求、运行与 Owner receipt 仍在；worker 恢复后会从首个缺口继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      {noGap ? (
        <div className="lumen-plan-disposition" data-testid="plan-no-gap-disposition">
          <span aria-hidden="true">✓</span>
          <p>
            <b>no new experiment required</b>
            <small>0 gap · 0 ExperimentBrief；不会创建伪造的 Bundle Run。</small>
          </p>
        </div>
      ) : null}
      <div className="lumen-idea-facts" role="list">
        {rows.map((row) => (
          <article
            key={row.slot}
            className="lumen-idea-fact"
            data-plan-slot={row.slot}
            data-state={row.state}
            role="listitem"
          >
            <span className="lumen-idea-fact-mark" aria-hidden="true">
              {row.state === "done" ? "✓" : row.state === "blocked" ? "!" : "→"}
            </span>
            <div>
              <small>{row.label}</small>
              <b>{row.title}</b>
              {row.slot === "plan-acceptance" ? (
                <span className="lumen-plan-acceptance-layers">
                  <span data-plan-owner-layer="content">
                    <small>PlanDocument · RM</small>
                    <code>{acceptance.content.status}</code>
                  </span>
                  <span data-plan-owner-layer="domain">
                    <small>FormalPlan · RG</small>
                    <code>{acceptance.domain.status}</code>
                  </span>
                </span>
              ) : <code>{row.status}</code>}
            </div>
            <span>{row.owner}</span>
          </article>
        ))}
      </div>
      <details className="lumen-idea-details">
        <summary>查看 Plan 运行身份与 receipt</summary>
        <dl>
          <IdeaDetail label="Cycle" value={planStage.eligibility.cycle_ref} />
          <IdeaDetail
            label="Eligibility reason"
            value={reasonCode(planStage.eligibility.reason)}
          />
          <IdeaDetail
            label="StageRunRequest"
            value={request?.request_ref ?? request?.stage_run_request_ref}
          />
          <IdeaDetail label="StageRunRequest receipt" value={receiptRef(request?.receipt)} />
          <IdeaDetail label="AcceptedQuestionBinding" value={
            request?.accepted_question_binding?.ref
              ?? request?.accepted_question_binding?.binding_ref
              ?? request?.accepted_question_binding?.question_ref
          } />
          <IdeaDetail label="AcceptedIdeaSetBinding" value={
            ideaSet?.ref ?? ideaSet?.binding_ref ?? ideaSet?.idea_set_ref ?? ideaSet?.outcome_ref
          } />
          <IdeaDetail label="IdeaSet content" value={ideaSet?.content_ref} />
          <IdeaDetail label="IdeaSet candidates" value={ideaSet?.candidate_count} />
          <IdeaDetail label="IdeaSet content receipt" value={receiptRef(ideaSet?.content_receipt)} />
          <IdeaDetail label="IdeaSet domain receipt" value={receiptRef(ideaSet?.domain_receipt)} />
          <IdeaDetail label="Idea StageCommit receipt" value={receiptRef(ideaSet?.stage_commit_receipt)} />
          <IdeaDetail label="ContextPack" value={request?.context_pack_ref} />
          <IdeaDetail label="ContextPack hash" value={request?.context_pack_hash} />
          <IdeaDetail label="Run" value={run?.run_ref} />
          <IdeaDetail label="Attempt" value={run?.attempt_ref
            ? `${run.attempt_ref}${run.attempt_generation === undefined ? "" : ` · generation ${run.attempt_generation}`}`
            : null
          } />
          <IdeaDetail label="Root Session" value={run?.root_session_ref} />
          <IdeaDetail label="Native Session" value={run?.native_session_ref} />
          <IdeaDetail label="Execution Fence" value={run?.fence_ref
            ? `${run.fence_ref}${run.fence_status ? ` · ${run.fence_status}` : ""}`
            : null
          } />
          <IdeaDetail label="Attempt execution receipt" value={receiptRef(run?.attempt_execution_receipt)} />
          <IdeaDetail label="Run completion receipt" value={receiptRef(run?.completion_receipt)} />
          <IdeaDetail label="Child reviewer agent" value={run?.review?.reviewer_agent_ref} />
          <IdeaDetail label="PlanDocument" value={
            acceptance.plan_document_ref
              ?? recordText(acceptance.content, "plan_document_ref", "content_ref")
          } />
          <IdeaDetail label="PlanDocument receipt" value={receiptRef(
            acceptance.content.receipt ?? acceptance.content,
          )} />
          <IdeaDetail label="FormalPlan" value={
            acceptance.formal_plan_ref
              ?? acceptance.outcome_ref
              ?? recordText(acceptance.domain, "formal_plan_ref", "outcome_ref")
          } />
          <IdeaDetail label="FormalPlan receipt" value={receiptRef(
            acceptance.domain.receipt ?? acceptance.domain,
          )} />
          <IdeaDetail label="Acceptance rejection" value={reasonCode(acceptance.rejection)} />
          <IdeaDetail label="AnswerContract hash" value={acceptance.answer_contract_hash} />
          <IdeaDetail label="Gap count" value={acceptance.gap_count} />
          <IdeaDetail label="ExperimentBrief count" value={acceptance.experiment_brief_count} />
          <IdeaDetail label="Bundle disposition" value={acceptance.bundle_disposition} />
          <IdeaDetail label="StageCommit" value={commit?.commit_ref ?? commit?.stage_commit_ref} />
          <IdeaDetail label="StageCommit receipt" value={receiptRef(commit?.receipt)} />
          <IdeaDetail label="Next Stage" value={commit?.next_stage} />
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
  onOpenExperiment,
  onExperimentStarted,
}: {
  snapshot: PublicSnapshot | null;
  state: ShellState;
  error: string | null;
  streamInterrupted: boolean;
  retry: () => void;
  onOpenExperiment: (trigger: HTMLElement) => void;
  onExperimentStarted: (experiment: ExperimentProjection) => void;
}) {
  const unavailable = uniqueCapabilities(snapshot);
  const ideaStage = snapshot?.research_space.status === "active"
    ? snapshot.idea_stage ?? null
    : null;
  const planStage = snapshot?.research_space.status === "active"
    ? snapshot.plan_stage ?? null
    : null;
  const ideaHealthBlocker = ideaStageHealthBlocker(snapshot);
  const planHealthBlocker = planStageHealthBlocker(snapshot);
  const experiment = snapshot?.experiment?.current ?? null;
  const question = planStage
    ? planQuestion(planStage, snapshot ?? undefined)
    : ideaStage
      ? ideaQuestion(ideaStage, snapshot ?? undefined)
      : null;
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
        {planStage ? (
          <CurrentQuestionCard
            stage="Plan"
            question={question!}
            experiment={experiment}
            onOpenExperiment={onOpenExperiment}
          />
        ) : ideaStage ? (
          <CurrentQuestionCard
            stage="Idea"
            question={question!}
            experiment={experiment}
            onOpenExperiment={onOpenExperiment}
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

        {planStage ? (
          <PlanStageCard
            planStage={planStage}
            healthBlocker={planHealthBlocker}
          />
        ) : ideaStage ? (
          <IdeaStageCard
            ideaStage={ideaStage}
            healthBlocker={ideaHealthBlocker}
            experiment={experiment}
            questRef={question?.quest_ref ?? null}
            onOpenExperiment={onOpenExperiment}
            onExperimentStarted={onExperimentStarted}
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
                  <li key={item.capability} data-capability={item.capability}>
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
                  {snapshot.readiness.checks.map((check) => (
                    <div key={check.name}>
                      <dt>{check.name}</dt>
                      <dd>{check.status}</dd>
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

function App() {
  const initialParameters = useMemo(
    () => new URLSearchParams(window.location.search),
    [],
  );
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
  const [assetsOpen, setAssetsOpen] = useState(
    () => new URLSearchParams(window.location.search).get("panel") === "research-assets",
  );
  const [writingOpen, setWritingOpen] = useState(
    () => new URLSearchParams(window.location.search).get("panel") === "writing",
  );
  const [questionTreeOpen, setQuestionTreeOpen] = useState(
    () => ["question-tree", "create-question"].includes(
      initialParameters.get("panel") ?? "",
    ) || initialParameters.get("view") === "questions",
  );
  const [questionRouteNodeRef, setQuestionRouteNodeRef] = useState<string | null>(
    () => initialParameters.get("node"),
  );
  const [pendingDirectManualParentRef, setPendingDirectManualParentRef] = useState<
    string | null
  >(() => initialParameters.get("panel") === "create-question"
    ? initialParameters.get("node")
    : null);
  const [manualPanel, setManualPanel] = useState<ManualPanelState | null>(null);
  const [manualOpeningParentRef, setManualOpeningParentRef] = useState<
    string | null
  >(null);
  const [manualOpenError, setManualOpenError] = useState<string | null>(null);
  const [humanRequestsOpen, setHumanRequestsOpen] = useState(
    () => isHumanRequestPanel(
      new URLSearchParams(window.location.search).get("panel"),
    ),
  );
  const [humanRequestRouteKind, setHumanRequestRouteKind] = useState<
    HumanRequestItem["kind"] | null
  >(
    () => humanRequestKindFromPanel(
      new URLSearchParams(window.location.search).get("panel"),
    ),
  );
  const [selectedHumanRequestRef, setSelectedHumanRequestRef] = useState<string | null>(null);
  const [streamCursor, setStreamCursor] = useState<number | null>(null);
  const [snapshotRetrySequence, setSnapshotRetrySequence] = useState(0);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);
  const streamCursorRef = useRef<number | null>(null);
  const manualDetailSequence = useRef(0);
  const questionTreeButtonRef = useRef<HTMLButtonElement>(null);
  const writingButtonRef = useRef<HTMLButtonElement>(null);
  const humanRequestReturnFocusRef = useRef<HTMLElement | null>(null);
  const executionObserver = useExecutionObserver(
    snapshot?.experiment?.current ?? null,
    Boolean(creationMode || assetsOpen || writingOpen),
  );

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
          streamCursorRef.current = Math.max(
            streamCursorRef.current ?? next.revision,
            next.revision,
          );
          setStreamCursor(streamCursorRef.current);
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

  const handleExperimentStarted = useCallback((experiment: ExperimentProjection) => {
    executionObserver.recordStarted(experiment);
    setSnapshot((current) => current ? {
      ...current,
      experiment: { status: "active", current: experiment },
    } : current);
    window.setTimeout(() => void reload(), 0);
  }, [executionObserver.recordStarted, reload]);

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

  const streamReady = streamCursor !== null;
  useEffect(() => {
    if (!streamReady) return;
    return followProjection(
      streamCursor ?? 0,
      () => void reload(),
      () => void reload(),
      handleConnection,
      () => streamCursorRef.current,
    );
    // followProjection advances its own monotonic cursor. Reconnecting this
    // long-lived stream for every Snapshot revision can briefly occupy every
    // browser connection slot and starve an Owner command.
  }, [handleConnection, reload, streamReady]);

  const manualContextRef = manualPanel?.raw.context_ref ?? null;
  useEffect(() => {
    if (!manualContextRef || !snapshot) return;
    const controller = new AbortController();
    const capturedSequence = manualDetailSequence.current;
    void (async () => {
      try {
        const raw = await fetchManualQuestionCreation(
          manualContextRef,
          controller.signal,
        );
        const researchReceipt = await hydrateManualResearchReceipt(
          raw,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          capturedSequence !== manualDetailSequence.current
        ) {
          return;
        }
        setManualPanel((current) =>
          current?.raw.context_ref === manualContextRef
            ? { ...current, raw, researchReceipt }
            : current,
        );
      } catch (caught) {
        if ((caught as Error).name === "AbortError") return;
        // Keep the last verified detail. A later Snapshot revision retries the
        // held context_ref; current-context queries are intentionally not used.
      }
    })();
    return () => controller.abort();
  }, [manualContextRef, snapshot?.revision]);

  useEffect(() => {
    const humanRequests = snapshot?.human_collaboration?.human_requests;
    if (!humanRequestsOpen || !humanRequestRouteKind || humanRequests?.status !== "ready") {
      return;
    }
    const selected = humanRequests.items.find(
      (item) => item.kind === humanRequestRouteKind && item.status === "open",
    ) ?? humanRequests.items.find((item) => item.kind === humanRequestRouteKind);
    if (!selected) return;
    setSelectedHumanRequestRef(selected.request_ref);
    setHumanRequestRouteKind(null);
  }, [humanRequestRouteKind, humanRequestsOpen, snapshot?.human_collaboration?.human_requests]);

  useEffect(() => {
    const humanRequests = snapshot?.human_collaboration?.human_requests;
    if (humanRequests?.status !== "ready") return;
    const companionScope = snapshot?.human_collaboration?.companion.scope_ref;
    if (!companionScope) return;
    const questRef = companionScope.startsWith("quest:")
      ? companionScope.slice("quest:".length)
      : companionScope;
    const candidates = humanRequests.items.filter((item) =>
      item.status === "open"
      && item.quest_ref === questRef
      && item.direct_waiters?.some((waiter) => waiter.wait_scope === "quest")
      && !humanRequests.waiting.safe_meaningful_runnable_exists,
    );
    const globalRequest = candidates.find((item) => {
      const key = `meta_research:human_request:auto_presented:${item.request_ref}`;
      try {
        return window.sessionStorage.getItem(key) === null;
      } catch {
        return true;
      }
    });
    if (!globalRequest) return;
    const presentationKey = `meta_research:human_request:auto_presented:${globalRequest.request_ref}`;
    try {
      if (window.sessionStorage.getItem(presentationKey)) return;
      window.sessionStorage.setItem(presentationKey, "presented");
    } catch {
      // The in-memory open still presents the current global request once this render.
    }
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setSelectedHumanRequestRef(globalRequest.request_ref);
    setHumanRequestRouteKind(null);
    window.history.replaceState(
      null,
      "",
      `/?panel=${humanRequestPanelByKind[globalRequest.kind]}`,
    );
    setHumanRequestsOpen(true);
  }, [snapshot?.human_collaboration?.human_requests]);

  const state = shellState(snapshot, error);
  const canCreate = questCreationReady(snapshot);
  const canBrowseAssets = snapshot?.research_assets.status === "ready";
  const canBrowseQuestions = snapshot?.question_tree.status === "ready";
  const canBrowseWriting = snapshot?.writing.status === "ready";
  const manualCreationReady =
    snapshot?.manual_question_creation.status === "ready";
  const manualView = useMemo(
    () => manualPanel
      ? adaptManualQuestionCreation(manualPanel.raw, {
          parent_question_title:
            manualPanel.parent.title ?? manualPanel.parent.unknown_statement,
          research_receipt: manualPanel.researchReceipt,
        })
      : null,
    [manualPanel],
  );
  const canBrowseHumanRequests = snapshot?.human_collaboration?.human_requests.status === "ready";
  const humanRequestCount = snapshot?.human_collaboration?.human_requests.items.filter(
    (item) => item.status === "open",
  ).length ?? 0;
  const intakeWorkerReady = snapshot?.readiness.checks.find(
    (check) => check.name === "research_asset_intake_worker",
  )?.status === "ready";
  const verificationWorkerReady = snapshot?.readiness.checks.find(
    (check) => check.name === "research_asset_verification_worker",
  )?.status === "ready";
  const openCreation = () => {
    if (!canCreate) return;
    setQuestionTreeOpen(false);
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", "/?panel=create-quest");
    setCreationMode("current");
  };
  const closeCreation = () => {
    window.history.replaceState(null, "", "/");
    setCreationMode(null);
  };
  const openAssets = () => {
    if (!canBrowseAssets) return;
    setCreationMode(null);
    setQuestionTreeOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", "/?panel=research-assets");
    setAssetsOpen(true);
  };
  const closeAssets = () => {
    window.history.replaceState(null, "", "/");
    setAssetsOpen(false);
  };
  const openQuestionTree = () => {
    if (!canBrowseQuestions) return;
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setManualOpenError(null);
    setQuestionRouteNodeRef(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", questionTreeUrl());
    setQuestionTreeOpen(true);
  };
  const closeQuestionTree = () => {
    setManualPanel(null);
    setQuestionTreeOpen(false);
    setQuestionRouteNodeRef(null);
    setPendingDirectManualParentRef(null);
    setManualOpenError(null);
    window.history.replaceState(null, "", "/");
    requestAnimationFrame(() => {
      questionTreeButtonRef.current?.focus({ preventScroll: true });
    });
  };

  const openWriting = () => {
    if (!canBrowseWriting || !snapshot) return;
    setCreationMode(null);
    setAssetsOpen(false);
    setQuestionTreeOpen(false);
    setManualPanel(null);
    setPendingDirectManualParentRef(null);
    setHumanRequestsOpen(false);
    setHumanRequestRouteKind(null);
    setSelectedHumanRequestRef(null);
    window.history.replaceState(null, "", "/?panel=writing");
    setWritingOpen(true);
  };
  const closeWriting = () => {
    window.history.replaceState(null, "", "/");
    setWritingOpen(false);
    requestAnimationFrame(() => {
      writingButtonRef.current?.focus({ preventScroll: true });
    });
  };

  const openManualCreation = async (
    parent: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => {
    if (!manualCreationReady || manualOpeningParentRef !== null) return;
    setQuestionRouteNodeRef(parent.question_ref);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(
      null,
      "",
      manualCreationUrl(parent.question_ref),
    );
    setManualOpeningParentRef(parent.question_ref);
    setManualOpenError(null);
    try {
      const current = await fetchCurrentManualQuestionCreation(
        parent.quest_ref,
        parent.question_ref,
      );
      const raw = current ?? await openManualQuestionCreation(
        parent.quest_ref,
        parent.question_ref,
      );
      if (
        raw.quest_ref !== parent.quest_ref ||
        raw.parent_question_ref !== parent.question_ref
      ) {
        throw new ProductError("manual_creation_target_mismatch");
      }
      const researchReceipt = await hydrateManualResearchReceipt(raw);
      manualDetailSequence.current += 1;
      setManualPanel({ raw, parent, opener, researchReceipt });
      await reload();
    } catch (caught) {
      const code = caught instanceof ProductError ? caught.code : "unknown_error";
      setManualOpenError(code);
      window.history.replaceState(
        null,
        "",
        questionTreeUrl(parent.question_ref),
      );
      requestAnimationFrame(() => opener.focus({ preventScroll: true }));
    } finally {
      setManualOpeningParentRef(null);
    }
  };

  const applyManualRaw = useCallback(async (
    raw: ManualQuestionCreationRawView,
    basis: Pick<ManualPanelState, "parent" | "opener">,
  ): Promise<ManualQuestionCreationView> => {
    const researchReceipt = await hydrateManualResearchReceipt(raw);
    manualDetailSequence.current += 1;
    setManualPanel((current) =>
      current?.raw.context_ref === raw.context_ref
        ? { ...current, raw, researchReceipt }
        : current,
    );
    await reload();
    return adaptManualQuestionCreation(raw, {
      parent_question_title: basis.parent.title ?? basis.parent.unknown_statement,
      research_receipt: researchReceipt,
    });
  }, [reload]);

  useEffect(() => {
    if (
      !pendingDirectManualParentRef ||
      manualPanel ||
      manualOpeningParentRef !== null ||
      !snapshot
    ) {
      return;
    }
    if (
      snapshot.question_tree.status !== "ready" ||
      snapshot.manual_question_creation.status !== "ready"
    ) {
      setPendingDirectManualParentRef(null);
      setManualOpenError("manual_creation_capability_unavailable");
      window.history.replaceState(
        null,
        "",
        questionTreeUrl(pendingDirectManualParentRef),
      );
      return;
    }
    const parent = snapshot.question_tree.items.find(
      (item) => item.question_ref === pendingDirectManualParentRef,
    );
    if (!parent) {
      setPendingDirectManualParentRef(null);
      setManualOpenError("manual_creation_parent_not_present");
      window.history.replaceState(null, "", questionTreeUrl());
      return;
    }
    const frame = requestAnimationFrame(() => {
      const opener = document.querySelector<HTMLButtonElement>(
        `[data-create-parent-ref="${CSS.escape(parent.question_ref)}"]`,
      );
      if (!opener) return;
      setPendingDirectManualParentRef(null);
      void openManualCreation(parent, opener);
    });
    return () => cancelAnimationFrame(frame);
  }, [
    manualOpeningParentRef,
    manualPanel,
    pendingDirectManualParentRef,
    snapshot?.revision,
  ]);

  const questionUnavailableReason = !snapshot
    ? "projection_loading"
    : snapshot.question_tree.status === "ready"
      ? ""
      : `${snapshot.question_tree.status} · ${snapshot.question_tree.reason.code}`;

  const openHumanRequests = (requestRef: string | null = null) => {
    if (!canBrowseHumanRequests) return;
    const active = document.activeElement;
    if (!humanRequestsOpen && active instanceof HTMLElement) {
      humanRequestReturnFocusRef.current = active;
    }
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setHumanRequestRouteKind(null);
    setSelectedHumanRequestRef(requestRef);
    const request = snapshot?.human_collaboration?.human_requests.items.find(
      (item) => item.request_ref === requestRef,
    );
    const panel = request ? humanRequestPanelByKind[request.kind] : "human-requests";
    window.history.replaceState(null, "", `/?panel=${panel}`);
    setHumanRequestsOpen(true);
  };
  const selectHumanRequest = (requestRef: string | null) => {
    setSelectedHumanRequestRef(requestRef);
    if (requestRef === null) {
      window.history.replaceState(null, "", "/?panel=human-requests");
      return;
    }
    const request = snapshot?.human_collaboration?.human_requests.items.find(
      (item) => item.request_ref === requestRef,
    );
    if (request) {
      window.history.replaceState(
        null,
        "",
        `/?panel=${humanRequestPanelByKind[request.kind]}`,
      );
    }
  };
  const closeHumanRequests = () => {
    const returnFocus = humanRequestReturnFocusRef.current;
    window.history.replaceState(null, "", "/");
    setHumanRequestsOpen(false);
    setHumanRequestRouteKind(null);
    setSelectedHumanRequestRef(null);
    humanRequestReturnFocusRef.current = null;
    window.requestAnimationFrame(() => {
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      else document.querySelector<HTMLButtonElement>("[aria-label='HumanRequest']")?.focus();
    });
  };

  return (
    <>
      <a className="lumen-skip" href="#main-content" data-hc-background>跳到主要内容</a>
      <div className="lumen-shell" data-testid="product-shell" data-shell-state={state} data-hc-background>
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
        <LumenRail
          canCreate={Boolean(canCreate)}
          canBrowseAssets={canBrowseAssets}
          canBrowseQuestions={canBrowseQuestions}
          questionsActive={questionTreeOpen}
          canBrowseWriting={Boolean(canBrowseWriting)}
          writingOpen={writingOpen}
          questionUnavailableReason={questionUnavailableReason}
          questionButtonRef={questionTreeButtonRef}
          writingButtonRef={writingButtonRef}
          onBrowseQuestions={openQuestionTree}
          canBrowseHumanRequests={canBrowseHumanRequests}
          humanRequestCount={humanRequestCount}
          humanRequestsOpen={humanRequestsOpen}
          onCreate={openCreation}
          onBrowseAssets={openAssets}
          onBrowseWriting={openWriting}
          onBrowseHumanRequests={() => openHumanRequests()}
        />
        {questionTreeOpen && snapshot ? (
          <QuestionTree
            items={snapshot.question_tree.items}
            graphRevision={snapshot.owners.research_graph?.revision ?? null}
            projectionStatus={snapshot.question_tree.status}
            projectionReason={snapshot.question_tree.reason?.code ?? null}
            initialQuestionRef={questionRouteNodeRef}
            manualCreationReady={Boolean(
              manualCreationReady && snapshot.question_tree.status === "ready",
            )}
            openingParentRef={manualOpeningParentRef}
            openError={manualOpenError}
            onClose={closeQuestionTree}
            onCreateQuestion={openManualCreation}
          />
        ) : (
          <WorkspaceMain
            snapshot={snapshot}
            state={state}
            error={error}
            streamInterrupted={streamInterrupted}
            retry={() => void reload()}
            onOpenExperiment={executionObserver.open}
            onExperimentStarted={handleExperimentStarted}
          />
        )}
        <QuestCompanion
          state={state}
          collaboration={snapshot?.human_collaboration}
          onChanged={() => void reload()}
          onOpenRequest={(requestRef) => openHumanRequests(requestRef)}
        />
      </div>
      <HumanRequestSurface
        open={humanRequestsOpen}
        selectedRef={selectedHumanRequestRef}
        collaboration={snapshot?.human_collaboration}
        onSelect={selectHumanRequest}
        onClose={closeHumanRequests}
        onChanged={() => void reload()}
      />
      {creationMode && snapshot ? (
        <QuestCreationWorkbench
          current={creationMode === "new" ? null : snapshot.quest_creation.current}
          researchAssets={snapshot.research_assets.items}
          researchAssetInventoryRevision={
            snapshot.research_assets.inventory_revision
          }
          researchAssetTotal={snapshot.research_assets.total_count}
          onClose={closeCreation}
          onChanged={() => void reload()}
        />
      ) : null}
      {assetsOpen && snapshot ? (
        <ResearchAssetsWorkbench
          initial={snapshot.research_assets}
          intakeWorkerReady={Boolean(intakeWorkerReady)}
          verificationWorkerReady={Boolean(verificationWorkerReady)}
          onClose={closeAssets}
          onChanged={() => void reload()}
        />
      ) : null}
      {writingOpen && snapshot ? (
        <WritingReportWorkbench
          initial={snapshot.writing}
          questRef={
            snapshot.research_space.current_question?.quest_ref
              ?? snapshot.question_tree.items[0]?.quest_ref
              ?? snapshot.quest_creation.current?.quest_ref
              ?? null
          }
          onClose={closeWriting}
          onChanged={() => void reload()}
        />
      ) : null}
      {manualPanel && manualView ? (
        <ManualCreation
          view={manualView}
          returnFocusTo={manualPanel.opener}
          onClose={() => {
            window.history.replaceState(
              null,
              "",
              questionTreeUrl(manualPanel.parent.question_ref),
            );
            setManualPanel(null);
            setManualOpenError(null);
          }}
          onCancel={async ({ creation_id }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const raw = await cancelManualQuestionCreation(creation_id);
            await applyManualRaw(raw, manualPanel);
          }}
          onConfirmSeed={async ({ creation_id, seed }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const acceptedBindings = await intakeManualMaterials(
              creation_id,
              seed.material_draft,
            );
            const raw = await confirmManualCreationSeed(creation_id, {
              intent: seed.intent,
              fields: seed.fields,
              accepted_material_bindings: acceptedBindings,
              deepfetch_preference: seed.deepfetch_preference,
            });
            await applyManualRaw(raw, manualPanel);
          }}
          onStartDeepFetch={async ({ creation_id, seed_ref, seed_hash }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const raw = await startManualCreationDeepFetch(
              creation_id,
              seed_ref,
              seed_hash,
            );
            await applyManualRaw(raw, manualPanel);
          }}
          onConfirmWaiver={async ({ creation_id, seed_ref, seed_hash }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const raw = await confirmManualDeepFetchWaiver(
              creation_id,
              seed_ref,
              seed_hash,
            );
            await applyManualRaw(raw, manualPanel);
          }}
          onSendDraftMessage={async ({
            creation_id,
            session_ref,
            expected_basis_hash,
            message,
          }) => {
            if (
              creation_id !== manualPanel.raw.context_ref ||
              session_ref !== manualPanel.raw.drafting_session?.ref
            ) {
              throw new ProductError("manual_drafting_session_stale");
            }
            const raw = await sendManualDraftingMessage(
              creation_id,
              expected_basis_hash,
              message,
            );
            await applyManualRaw(raw, manualPanel);
          }}
          onSaveProposal={async ({
            creation_id,
            expected_basis_hash,
            expected_proposal_ref,
            expected_proposal_hash,
            content,
          }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const raw = await saveManualQuestionProposal(creation_id, {
              expected_basis_hash,
              expected_proposal_ref,
              expected_proposal_hash,
              content,
            });
            const next = await applyManualRaw(raw, manualPanel);
            if (!next.proposal) {
              throw new ProductError("manual_question_proposal_missing");
            }
            return next.proposal;
          }}
          onConfirmProposal={async ({
            creation_id,
            proposal_ref,
            proposal_hash,
          }) => {
            if (creation_id !== manualPanel.raw.context_ref) {
              throw new ProductError("manual_creation_context_stale");
            }
            const raw = await confirmManualQuestionProposal(
              creation_id,
              proposal_ref,
              proposal_hash,
            );
            await applyManualRaw(raw, manualPanel);
          }}
        />
      ) : null}
      <ExecutionObserver controller={executionObserver} />
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
