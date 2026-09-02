import {
  StrictMode,
  startTransition,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type Ref,
} from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import {
  acknowledgeAssetIntake,
  adaptManualQuestionCreation,
  cancelManualQuestionCreation,
  confirmManualCreationSeed,
  confirmManualDeepFetchWaiver,
  confirmManualQuestionProposal,
  createHumanCommand,
  decideQuestCompletion,
  fetchAssetIntake,
  fetchCurrentManualQuestionCreation,
  fetchLiteratureSnapshot,
  fetchManualQuestionCreation,
  fetchSnapshot,
  fetchTargetRawOutput,
  followProjection,
  openManualQuestionCreation,
  ProductError,
  saveManualQuestionProposal,
  sendManualDraftingMessage,
  startManualCreationDeepFetch,
  startQuestCompletion,
  submitAssetIntake,
  type AssetIntakeRequest,
  type AssetIntakeResult,
  type AssetReceipt,
  type AutonomousCreationView,
  type BundleStageProjection,
  type BundleTargetCommitProjection,
  type BundleTargetProjection,
  type IdeaQuestionSummary,
  type IdeaStageProjection,
  type ManualAcceptedMaterialBinding,
  type ManualQuestionCreationRawView,
  type HumanRequestItem,
  type PlanStageProjection,
  type PublicSnapshot,
  type QuestionTreeItem,
  type QuestCompletionView,
  type ReasoningStageProjection,
  type ResearchControlAction,
  type TargetRawOutputPage,
  type TargetRootObservationPointer,
  type UnavailableCapability,
} from "./api";
import {
  ManualCreation,
  type ManualCreationMaterialDraft,
  type ManualQuestionCreationView,
} from "./ManualCreation";
import {
  QuestCreationWorkbench,
  type QuestCompletionHandoff,
} from "./QuestCreation";
import { QuestionTree } from "./QuestionTree";
import { ResearchAssetsWorkbench } from "./ResearchAssets";
import { WritingReportWorkbench } from "./WritingReport";
import {
  HumanRequestSurface,
  QuestCompanion,
  TelemetryAuthorizationCard,
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

function runtimeTypedReason(reason: { code: string } | null | undefined): string {
  return reason?.code ?? "none";
}

function runtimeResponsibilitySummary(
  responsibilities: NonNullable<PublicSnapshot["runtime_observability"]>["responsibilities"],
  totalCount?: number,
): string {
  const exactCount = Number.isInteger(totalCount) && (totalCount ?? -1) >= 0
    ? totalCount as number
    : responsibilities?.length ?? 0;
  if (!responsibilities?.length) {
    return `${exactCount} 项未结责任 · owners none · effects none`;
  }
  const ownerScopes = [
    ...new Set(responsibilities.map((item) => item.owner_scope)),
  ].join(", ");
  const effectKinds = [
    ...new Set(responsibilities.map((item) => item.effect_kind)),
  ].join(", ");
  const sample = exactCount > responsibilities.length
    ? ` · 当前样本 ${responsibilities.length}`
    : "";
  return `${exactCount} 项未结责任${sample} · owners ${ownerScopes} · effects ${effectKinds}`;
}

function runtimeDurableWaitingSummary(
  durableWaiting: NonNullable<PublicSnapshot["runtime_observability"]>["durable_waiting"],
  totalCount?: number,
  pageTruncated?: boolean,
): string {
  const exactCount = Number.isInteger(totalCount) && (totalCount ?? -1) >= 0
    ? totalCount as number
    : durableWaiting?.length ?? 0;
  if (!durableWaiting?.length) return `${exactCount} · none`;
  const effectKinds = [
    ...new Set(durableWaiting.map((item) => item.effect_kind)),
  ].join(", ");
  const reasons = [
    ...new Set(durableWaiting.map((item) => item.reason.code)),
  ].join(", ");
  const sample = pageTruncated || exactCount > durableWaiting.length
    ? ` · 当前样本 ${durableWaiting.length}`
    : "";
  return `${exactCount}${sample} · effects ${effectKinds} · reasons ${reasons}`;
}

function runtimeInterruptionSummary(
  interruptions: NonNullable<PublicSnapshot["runtime_observability"]>["interruptions"],
  totalCount?: number,
  pageTruncated?: boolean,
): string {
  const exactCount = Number.isInteger(totalCount) && (totalCount ?? -1) >= 0
    ? totalCount as number
    : interruptions?.length ?? 0;
  if (!interruptions?.length) return `${exactCount} · none · reconciled`;
  const kinds = [...new Set(interruptions.map((item) => item.kind))].join(", ");
  const reasons = [
    ...new Set(interruptions.map((item) => item.reason.code)),
  ].join(", ");
  const reconciliation = [
    ...new Set(interruptions.map((item) => item.reconciliation_status)),
  ].join(", ");
  const sample = pageTruncated || exactCount > interruptions.length
    ? ` · 当前样本 ${interruptions.length}`
    : "";
  return `${exactCount}${sample} · kinds ${kinds} · reasons ${reasons} · reconciliation ${reconciliation}`;
}

function runtimeLogSummary(
  log: NonNullable<PublicSnapshot["runtime_observability"]>["log"],
): string {
  if (!log) return "unavailable";
  if (log.age_seconds === undefined) return log.status;
  return `${log.status} · ${Math.max(0, Math.round(log.age_seconds))}s`;
}

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

function currentQuestRef(snapshot: PublicSnapshot | null): string | null {
  const scopeRef = snapshot?.human_collaboration?.companion.scope_ref;
  if (!scopeRef) return null;
  if (scopeRef.startsWith("quest:")) {
    const questRef = scopeRef.slice("quest:".length);
    return questRef || null;
  }
  const projectedQuest = snapshot?.research_space.current_quest;
  if (projectedQuest?.status === "ready" && projectedQuest.quest_ref === scopeRef) {
    return scopeRef;
  }
  return snapshot?.human_collaboration?.human_requests.items.some(
    (item) => item.quest_ref === scopeRef,
  ) ? scopeRef : null;
}

function currentOpenHumanRequests(
  snapshot: PublicSnapshot | null,
): HumanRequestItem[] {
  const humanRequests = snapshot?.human_collaboration?.human_requests;
  if (humanRequests?.status !== "ready") return [];
  return humanRequests.items.filter((item) => item.status === "open");
}

if (window.location.pathname === "/auth/launch") {
  window.history.replaceState(null, "", "/");
}

type QuestionInspectorMode = "evidence" | "history" | null;

function questionTreeUrl(
  questionRef?: string | null,
  inspectorMode: QuestionInspectorMode = null,
): string {
  const parameters = new URLSearchParams({
    variant: "A",
    view: "questions",
  });
  if (questionRef) parameters.set("node", questionRef);
  if (inspectorMode) parameters.set("inspector", inspectorMode);
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

function useStableQuestionTreeItems(
  items: readonly QuestionTreeItem[],
  graphRevision: number | null,
): readonly QuestionTreeItem[] {
  const cache = useRef<{ key: string; items: readonly QuestionTreeItem[] } | null>(null);
  const key = JSON.stringify({
    graphRevision,
    items: items.map((item) => ({
      questionRef: item.question_ref,
      parentQuestionRef: item.parent_question_ref,
      lifecycleStatus: item.lifecycle_status,
      lifecycleRevision: item.lifecycle_revision,
      cycleBinding: item.cycle_binding,
      relatedHumanRequests: item.related_human_requests,
      furthestAcceptedStageResult: item.furthest_accepted_stage_result,
    })),
  });
  if (cache.current?.key !== key) cache.current = { key, items };
  return cache.current?.items ?? items;
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
        "bundle_stage_worker",
        "reasoning_stage_worker",
        "writing_worker",
        "research_asset_intake_worker",
        "research_asset_verification_worker",
      ].includes(check.name),
  );
  return requiredChecks.length > 0
    ? requiredChecks.every((check) => check.status === "ready")
    : snapshot.readiness.status === "ready";
}

function restorableQuestCreation(
  current: PublicSnapshot["quest_creation"]["current"],
  completedInitializationId: string | null,
): PublicSnapshot["quest_creation"]["current"] {
  if (!current) return null;
  if (["completed", "cancelled"].includes(current.status)) return null;
  return current.initialization_id === completedInitializationId ? null : current;
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
  canBrowseHistory,
  historyActive,
  canBrowseWriting,
  writingOpen,
  questionUnavailableReason,
  questionButtonRef,
  historyButtonRef,
  writingButtonRef,
  onBrowseQuestions,
  onBrowseHistory,
  canBrowseHumanRequests,
  humanRequestCount,
  humanRequestsOpen,
  onCreate,
  onBrowseAssets,
  onBrowseWriting,
  onBrowseHumanRequests,
  onOverview,
}: {
  canCreate: boolean;
  canBrowseAssets: boolean;
  canBrowseQuestions: boolean;
  questionsActive: boolean;
  canBrowseHistory: boolean;
  historyActive: boolean;
  canBrowseWriting: boolean;
  writingOpen: boolean;
  questionUnavailableReason: string;
  questionButtonRef: Ref<HTMLButtonElement>;
  historyButtonRef: Ref<HTMLButtonElement>;
  writingButtonRef: Ref<HTMLButtonElement>;
  canBrowseHumanRequests: boolean;
  humanRequestCount: number;
  humanRequestsOpen: boolean;
  onCreate: () => void;
  onBrowseAssets: () => void;
  onBrowseWriting: () => void;
  onBrowseQuestions: () => void;
  onBrowseHistory: () => void;
  onBrowseHumanRequests: () => void;
  onOverview: () => void;
}) {
  return (
    <nav className="lumen-rail" aria-label="主导航" data-shell-region="rail">
      <RailButton
        label="Quest 总览"
        glyph="⌂"
        active={!questionsActive && !writingOpen}
        onClick={onOverview}
      />
      <RailButton
        label="问题树"
        glyph="树"
        active={questionsActive && !historyActive}
        unavailable={!canBrowseQuestions}
        unavailableReason={questionUnavailableReason}
        buttonRef={questionButtonRef}
        onClick={onBrowseQuestions}
      />
      <RailButton
        label="研究资料"
        glyph="▤"
        unavailable={!canBrowseAssets}
        onClick={onBrowseAssets}
      />
      <RailButton
        label="写作"
        glyph="✎"
        active={writingOpen}
        unavailable={!canBrowseWriting}
        buttonRef={writingButtonRef}
        onClick={onBrowseWriting}
      />
      <RailButton
        label="历史"
        glyph="↺"
        active={historyActive}
        unavailable={!canBrowseHistory}
        unavailableReason="当前 Quest 没有可下钻的已接纳 Question"
        buttonRef={historyButtonRef}
        onClick={onBrowseHistory}
      />
      <RailButton
        label="需要你"
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
      <p className="lumen-eyebrow">正在载入研究现场</p>
      <h1 id="workspace-title">
        正在连接本地研究空间。<br />
        <em>研究轨迹会留在这里。</em>
      </h1>
      <p>研究问题、根 Agent 活动与真实产物会进入同一个窗口。</p>
      <div className="lumen-inline-state" role="status">
        <span className="lumen-spinner" aria-hidden="true" />
        <div>
          <b>读取真实研究状态</b>
          <small>不会用模拟进度填充等待时间</small>
        </div>
      </div>
    </>
  );
}

function FirstErrorHero({ retry }: { retry: () => void }) {
  return (
    <>
      <p className="lumen-eyebrow coral">研究状态暂不可用</p>
      <h1 id="workspace-title">
        研究空间暂时无法读取。<br />
        <em>页面仍然保持在原处。</em>
      </h1>
      <p>本地研究服务尚未返回状态。检查服务后，可以从这里重新读取。</p>
      <button className="lumen-primary" type="button" onClick={retry}>
        重新读取研究状态
      </button>
    </>
  );
}

type CurrentStageSurface =
  | { kind: "Idea"; projection: IdeaStageProjection }
  | { kind: "Plan"; projection: PlanStageProjection }
  | { kind: "Bundle"; projection: BundleStageProjection }
  | { kind: "Reasoning"; projection: ReasoningStageProjection };

type StagePosition = Lowercase<CurrentStageSurface["kind"]>;
type StagePositionState =
  | "current"
  | "result"
  | "skipped"
  | "recorded"
  | "not-entered"
  | "no-record"
  | "unavailable";

const stagePositions: Array<{
  kind: CurrentStageSurface["kind"];
  position: StagePosition;
  purpose: string;
}> = [
  { kind: "Idea", position: "idea", purpose: "形成候选解释" },
  { kind: "Plan", position: "plan", purpose: "设计验证路线" },
  { kind: "Bundle", position: "bundle", purpose: "收集实验与证据" },
  { kind: "Reasoning", position: "reasoning", purpose: "综合证据形成判断" },
];

type ResearchActivitySignal = {
  eventType: string;
  revision: number;
  observedAt: number;
};

function stageProjectionMatchesForeground(
  projection: CurrentStageSurface["projection"],
  foreground: NonNullable<PublicSnapshot["research_control"]["foreground"]>,
): boolean {
  const eligibility = projection.eligibility;
  if (
    eligibility.cycle_ref != null
    && eligibility.cycle_ref !== foreground.cycle_ref
  ) return false;
  if (
    eligibility.question_ref != null
    && eligibility.question_ref !== foreground.question_ref
  ) return false;
  const request = projection.stage_run_request;
  if (request?.cycle_ref && request.cycle_ref !== foreground.cycle_ref) return false;
  const binding = request?.accepted_question_binding;
  if (binding?.question_ref && binding.question_ref !== foreground.question_ref) {
    return false;
  }
  if (binding?.quest_ref && binding.quest_ref !== foreground.quest_ref) return false;
  return true;
}

function allStageSurfaces(snapshot: PublicSnapshot): CurrentStageSurface[] {
  const candidates: CurrentStageSurface[] = [];
  if (snapshot.idea_stage) {
    candidates.push({ kind: "Idea", projection: snapshot.idea_stage });
  }
  if (snapshot.plan_stage) {
    candidates.push({ kind: "Plan", projection: snapshot.plan_stage });
  }
  if (snapshot.bundle_stage) {
    candidates.push({ kind: "Bundle", projection: snapshot.bundle_stage });
  }
  if (snapshot.reasoning_stage) {
    candidates.push({ kind: "Reasoning", projection: snapshot.reasoning_stage });
  }
  const foreground = snapshot.research_control.foreground;
  return foreground
    ? candidates.filter((candidate) => (
        stageProjectionMatchesForeground(candidate.projection, foreground)
      ))
    : candidates;
}

function currentStageSurface(snapshot: PublicSnapshot): CurrentStageSurface | null {
  const candidates = allStageSurfaces(snapshot);
  const foreground = snapshot.research_control.foreground;
  if (snapshot.research_control.status !== "ready" || !foreground) return null;
  return candidates.find(
    (candidate) => candidate.kind.toLowerCase() === foreground.stage.toLowerCase(),
  ) ?? null;
}

function exactForegroundQuestion(snapshot: PublicSnapshot): IdeaQuestionSummary | null {
  const foreground = snapshot.research_control.foreground;
  if (!foreground) return null;
  const projected = snapshot.research_space.current_question;
  if (
    projected?.question_ref === foreground.question_ref
    && (!projected.quest_ref || projected.quest_ref === foreground.quest_ref)
  ) {
    return {
      ...projected,
      quest_ref: foreground.quest_ref,
      question_ref: foreground.question_ref,
    };
  }
  if (snapshot.question_tree.status !== "ready") return null;
  const item = snapshot.question_tree.items.find(
    (candidate) => (
      candidate.quest_ref === foreground.quest_ref
      && candidate.question_ref === foreground.question_ref
    ),
  );
  return item ? {
    quest_ref: item.quest_ref,
    question_ref: item.question_ref,
    graph_revision: snapshot.owners.research_graph?.revision,
    title: item.title ?? undefined,
    unknown_statement: item.unknown_statement ?? undefined,
  } : null;
}

function stagePositionState(
  surface: CurrentStageSurface | null,
  foreground: NonNullable<PublicSnapshot["research_control"]["foreground"]>,
  position: StagePosition,
): StagePositionState {
  if (!surface) return "unavailable";
  if (foreground.stage.toLowerCase() === position) return "current";
  const projection = surface.projection;
  const reason = projection.eligibility.reason?.code ?? "";
  const bundleDisposition = surface.kind === "Bundle"
    ? surface.projection.disposition
    : null;
  const commitDisposition = surface.kind === "Bundle"
    ? surface.projection.stage_commit?.disposition
    : surface.kind === "Reasoning"
      ? surface.projection.stage_commit?.disposition
      : undefined;
  if (
    projection.typed_skip?.status === "skipped"
    || projection.eligibility.status === "skipped"
    || reason === "no_new_experiment_required"
    || bundleDisposition?.status === "skipped"
    || commitDisposition === "skipped"
  ) return "skipped";
  if (projection.stage_commit) return "result";
  if (
    (surface.kind === "Idea" && surface.projection.outcome_acceptance.status === "accepted")
    || (surface.kind === "Plan" && surface.projection.plan_acceptance.status === "accepted")
    || (surface.kind === "Reasoning" && surface.projection.reasoning_acceptance.status === "accepted")
    || (surface.kind === "Bundle" && surface.projection.target_commits.length > 0)
  ) return "result";
  if (projection.run || projection.stage_run_request) return "recorded";
  const foregroundIndex = stagePositions.findIndex(
    (item) => item.position === foreground.stage.toLowerCase(),
  );
  const positionIndex = stagePositions.findIndex((item) => item.position === position);
  if (
    projection.eligibility.status === "not_eligible"
    && foregroundIndex >= 0
    && positionIndex > foregroundIndex
  ) return "not-entered";
  return "no-record";
}

function stagePositionCopy(state: StagePositionState): string {
  return {
    current: "当前研究位置",
    result: "已有正式结果",
    skipped: "本 Cycle 明确跳过",
    recorded: "已有运行记录",
    "not-entered": "本 Cycle 尚未进入",
    "no-record": "本 Cycle 没有记录",
    unavailable: "事实暂不可用",
  }[state];
}

function researchWorkCopy(stage: CurrentStageSurface | null): {
  title: string;
  detail: string;
  state: "active" | "checking" | "waiting" | "blocked" | "done";
} {
  if (!stage) {
    return {
      title: "等待下一段研究工作",
      detail: "当前没有正在运行的根 Agent。",
      state: "waiting",
    };
  }
  const run = stage.projection.run;
  if (run && isRunBlocked(run.status)) {
    return {
      title: "研究遇到问题",
      detail: "已有材料仍然保留；展开技术细节可查看精确原因。",
      state: "blocked",
    };
  }
  if (stage.projection.stage_commit) {
    return {
      title: "这段研究已经收口",
      detail: "已形成可继续使用的研究材料，系统正在衔接下一段工作。",
      state: "done",
    };
  }
  if (run?.status === "awaiting_acceptance" || run?.attempt_execution_receipt) {
    return {
      title: "根 Agent 已交出研究材料",
      detail: "材料正在接受完整性与研究语义核验。",
      state: "checking",
    };
  }
  if (run) {
    const work = stage.kind === "Idea"
      ? "形成候选解释"
      : stage.kind === "Plan"
        ? "设计验证路线"
        : stage.kind === "Bundle"
          ? "运行实验并收集证据"
          : "综合证据并形成判断";
    const reviewing = run.provider_operations?.review?.status === "prepared";
    return {
      title: reviewing ? "根 Agent 正在复核研究材料" : `根 Agent 正在${work}`,
      detail: run.primary_draft_checkpoint
        ? "已形成中间草稿，仍在继续工作；这不代表最终完成。"
        : "长时间没有新输出不等于停止，页面只报告可观察事实。",
      state: "active",
    };
  }
  return {
    title: "正在准备研究上下文",
    detail: "研究问题与已有材料已经选定，等待根 Agent 接手。",
    state: "waiting",
  };
}

function researchProductCopy(stage: CurrentStageSurface | null): {
  title: string;
  detail: string;
} {
  if (!stage) return { title: "暂无新产物", detail: "研究开始后会在这里累计真实材料。" };
  if (stage.kind === "Idea") {
    if (stage.projection.outcome_acceptance.outcome_ref) {
      return { title: "候选解释已经保存", detail: "可供后续验证路线继续使用。" };
    }
    if (stage.projection.run?.primary_draft_checkpoint) {
      return { title: "已形成一份中间草稿", detail: "草稿仍可能被根 Agent 修订。" };
    }
    return { title: "等待首份候选解释", detail: "尚未把运行活动冒充成研究产物。" };
  }
  if (stage.kind === "Plan") {
    const acceptance = stage.projection.plan_acceptance;
    if (acceptance.formal_plan_ref || acceptance.plan_document_ref) {
      return {
        title: "验证方案已经形成",
        detail: `${acceptance.gap_count ?? 0} 个待验证缺口 · ${acceptance.experiment_brief_count ?? 0} 个实验任务`,
      };
    }
    return { title: "等待验证方案", detail: "根 Agent 仍在把候选解释转成可执行研究路线。" };
  }
  if (stage.kind === "Bundle") {
    const complete = stage.projection.target_commits.length;
    const total = stage.projection.target_graph.targets.length;
    return {
      title: `${complete} 份实验结果已冻结`,
      detail: total ? `${total - complete} 个研究目标仍未形成最终结果。` : "尚未建立实验目标。",
    };
  }
  if (stage.projection.reasoning_acceptance.outcome_ref) {
    return {
      title: "综合研究判断已经形成",
      detail: "判断与下一步候选保持分离，仍可继续审阅。",
    };
  }
  return { title: "等待综合判断", detail: "已有证据正在被交叉检查。" };
}

function researchEventCopy(eventType: string): string {
  if (eventType === "agent_runtime.target_root_observations_available") {
    return "实验任务产生了新的命令输出";
  }
  if (eventType.includes("stage_run_admitted")) {
    return "根 Agent 已开始一段真实工作";
  }
  if (eventType.includes("attempt_executed")) {
    return "根 Agent 形成了新的可核验材料";
  }
  if (eventType.includes("asset_accepted") || eventType.includes("content_accepted")) {
    return "新的研究材料已经保存";
  }
  if (
    eventType.includes("outcome_accepted")
    || eventType === "research_graph.target_formal_measurement_accepted"
  ) {
    return "新的研究结论已经通过核验";
  }
  if (eventType.includes("stage_committed") || eventType.includes("stage_run_completed")) {
    return "一段研究工作已经正式收口";
  }
  if (eventType.includes("failed") || eventType.includes("rejected")) {
    return "研究运行报告了需要处理的问题";
  }
  return "研究状态出现了新的可验证变化";
}

function elapsedCopy(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1_000));
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes} 分 ${totalSeconds % 60} 秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours} 小时 ${minutes % 60} 分`;
}

function acceptedChangeSummary(snapshot: PublicSnapshot): string {
  if (snapshot.quest_completion.current?.status === "ended") {
    return "Quest completion 已由 RG 接纳，AE 已正式结束当前 Quest";
  }
  if (
    snapshot.bundle_stage?.disposition.report_disposition === "exhausted" &&
    snapshot.bundle_stage.bundle_exhaustion?.kind === "BundleExhaustion"
  ) {
    return `BundleExhaustion 已接纳 · ${snapshot.bundle_stage.bundle_exhaustion.basis_ref ?? "basis ref unavailable"}`;
  }
  for (const [name, projection] of [
    ["Reasoning", snapshot.reasoning_stage],
    ["Bundle", snapshot.bundle_stage],
    ["Plan", snapshot.plan_stage],
    ["Idea", snapshot.idea_stage],
  ] as const) {
    if (projection?.stage_commit) {
      return `${name} StageCommit · ${projection.stage_commit.status} · next ${projection.stage_commit.next_stage ?? "unavailable"}`;
    }
  }
  const stage = currentStageSurface(snapshot);
  return stage
    ? `${stage.kind} 正在消费 rev ${snapshot.revision} 的公开 Projection`
    : `公开 Projection 已到 rev ${snapshot.revision}`;
}

function currentBlockerSummary(snapshot: PublicSnapshot): string {
  const currentRequest = currentOpenHumanRequests(snapshot)[0];
  if (currentRequest) {
    return currentRequest.obligation;
  }
  const unavailable = snapshot.readiness.checks.find((check) => check.status !== "ready");
  if (unavailable) {
    return `${unavailable.name} · ${unavailable.reason?.code ?? unavailable.status}`;
  }
  return "无公开 blocker · safe meaningful work remains";
}

function nextStepSummary(snapshot: PublicSnapshot): string {
  if (currentOpenHumanRequests(snapshot).length) {
    return "请处理当前待办；不依赖它的研究仍可在后台继续";
  }
  const stage = currentStageSurface(snapshot);
  if (!stage) return "从当前已接纳 Question 继续";
  const nextStage = stage.projection.stage_commit?.next_stage;
  if (nextStage) return `由 Advancement Engine 进入 ${nextStage}`;
  if (stage.projection.run) {
    return `等待 ${stage.kind} 的执行、内容接纳、领域接纳与推进各自完成`;
  }
  return `等待 ${stage.kind} 当前步骤形成可确认结果`;
}

function ReturnSummary({ snapshot }: { snapshot: PublicSnapshot }) {
  const quest = snapshot.research_space.current_quest ?? {
    status: "unavailable" as const,
    goal: null,
    completion_criteria: null,
    goal_revision_ref: null,
    reason: { code: "quest_goal_projection_unavailable" },
  };
  return (
    <section
      className="lumen-return-summary"
      aria-label="低密度返场摘要"
      data-testid="return-summary"
    >
      <article>
        <small>Goal 对齐 · RG</small>
        <b>{quest.status === "ready" ? quest.goal : quest.reason?.code ?? "unavailable"}</b>
        <span>{quest.status === "ready"
          ? `完成标准：${quest.completion_criteria} · ${quest.goal_revision_ref}`
          : "不会从浏览器草案推断 Goal"}</span>
      </article>
      <article>
        <small>关键变化 · accepted state</small>
        <b>{acceptedChangeSummary(snapshot)}</b>
        <span>只报告已经确认的当前事实</span>
      </article>
      <article>
        <small>当前阻塞</small>
        <b>{currentBlockerSummary(snapshot)}</b>
        <span>局部等待与 Quest-wide wait 不合并</span>
      </article>
      <article>
        <small>下一步</small>
        <b>{nextStepSummary(snapshot)}</b>
        <span>页面只解释已经确认的状态，不替研究流程作决定</span>
      </article>
    </section>
  );
}

type ResearchActivityItem = {
  ref: string;
  source: string;
  label: string;
  lane: "stage" | "bundle" | "acquisition" | "target";
  status: string;
  updatedAt: number | null;
};

const managedActivityKinds = {
  idea_stage: { lane: "stage", label: "Idea 阶段主智能体" },
  plan_stage: { lane: "stage", label: "Plan 阶段主智能体" },
  bundle_stage: { lane: "bundle", label: "Bundle 策略主智能体" },
  reasoning_stage: { lane: "stage", label: "Reasoning 阶段主智能体" },
  deepfetch: { lane: "acquisition", label: "DeepFetch 文献检索" },
  acquisition: { lane: "acquisition", label: "资料获取任务" },
} as const;

type ManagedActivityKind = keyof typeof managedActivityKinds;

const stageManagedRunKinds: Record<StagePosition, ManagedActivityKind> = {
  idea: "idea_stage",
  plan: "plan_stage",
  bundle: "bundle_stage",
  reasoning: "reasoning_stage",
};

function managedRunActivity(
  run: PublicSnapshot["research_control"]["managed_runs"][number],
): ResearchActivityItem | null {
  const metadata = managedActivityKinds[run.run_kind as ManagedActivityKind];
  if (!metadata) return null;
  return {
    ref: run.run_ref,
    source: run.run_kind,
    label: metadata.label,
    lane: metadata.lane,
    status: run.status,
    updatedAt: run.updated_at,
  };
}

function targetActivity(
  target: BundleStageProjection["target_graph"]["targets"][number],
): ResearchActivityItem {
  return {
    ref: target.target_ref,
    source: "bundle_target",
    label: target.target_key,
    lane: "target",
    status: target.status,
    updatedAt: null,
  };
}

function activityStatusCopy(status: string): string {
  const labels: Record<string, string> = {
    running: "正在运行",
    active: "正在运行",
    completed: "已完成",
    committed: "已完成",
    realized: "已形成结果",
    suspended: "已暂停",
    paused: "已暂停",
    blocked: "等待处理",
    failed: "运行失败",
    cancelled: "已取消",
    fenced: "已停止",
  };
  return labels[status.toLowerCase()] ?? "状态已记录";
}

function activityObservedAt(updatedAt: number | null): string {
  if (updatedAt === null) return "更新时间未记录";
  const milliseconds = updatedAt < 10_000_000_000 ? updatedAt * 1_000 : updatedAt;
  return `更新 ${new Date(milliseconds).toLocaleString()}`;
}

function ResearchActivityLane({
  name,
  items,
  emptyCopy,
}: {
  name: string;
  items: ResearchActivityItem[];
  emptyCopy: string;
}) {
  const pageSize = 3;
  const [page, setPage] = useState(0);
  const maxPage = Math.max(0, Math.ceil(items.length / pageSize) - 1);
  const boundedPage = Math.min(page, maxPage);
  const visible = items.slice(boundedPage * pageSize, (boundedPage + 1) * pageSize);

  useEffect(() => {
    if (page > maxPage) setPage(maxPage);
  }, [maxPage, page]);

  return (
    <section className="lumen-activity-lane" aria-label={name}>
      <header>
        <b>{name}</b>
        <small>{items.length ? `${items.length} 条来源事实` : "没有来源事实"}</small>
      </header>
      {visible.length ? (
        <ul>
          {visible.map((item) => (
            <li key={`${item.source}:${item.ref}`}>
              <span><b>{item.label}</b><small>{activityStatusCopy(item.status)}</small></span>
              <time>{activityObservedAt(item.updatedAt)}</time>
            </li>
          ))}
        </ul>
      ) : <p>{emptyCopy}</p>}
      <p className="lumen-activity-capture-note">
        未捕获正文时显示暂无可观察输出；静默不等于根 Agent 已停止。
      </p>
      {visible.length ? (
        <details className="lumen-activity-technical">
          <summary>查看运行详情</summary>
          <ul>
            {visible.map((item) => (
              <li key={`detail:${item.source}:${item.ref}`}>
                <code>{item.ref}</code>
                <small>{item.source} · {item.status}</small>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {items.length > pageSize ? (
        <nav aria-label={`${name}分页`}>
          <button
            type="button"
            disabled={boundedPage === 0}
            onClick={() => setPage((current) => Math.max(0, current - 1))}
          >返回上一页</button>
          <small>{boundedPage + 1} / {maxPage + 1}</small>
          <button
            type="button"
            disabled={boundedPage >= maxPage}
            onClick={() => setPage((current) => Math.min(maxPage, current + 1))}
          >读取下一页</button>
        </nav>
      ) : null}
    </section>
  );
}

function ResearchTracePanel({
  snapshot,
  latestActivity,
  observedSince,
  connected,
}: {
  snapshot: PublicSnapshot;
  latestActivity: ResearchActivitySignal | null;
  observedSince: number;
  connected: boolean;
}) {
  const [clock, setClock] = useState(() => Date.now());
  const stage = currentStageSurface(snapshot);
  const work = researchWorkCopy(stage);
  const product = researchProductCopy(stage);
  const lastObservedAt = latestActivity?.observedAt ?? observedSince;
  const foreground = snapshot.research_control.foreground;
  const scopedRuns = foreground
    ? snapshot.research_control.managed_runs.filter((run) => (
        run.quest_ref === foreground.quest_ref
        && run.cycle_ref === foreground.cycle_ref
      ))
    : [];
  const managedActivity = scopedRuns
    .map(managedRunActivity)
    .filter((item): item is ResearchActivityItem => item !== null);
  const foregroundPosition = foreground?.stage.toLowerCase() as StagePosition | undefined;
  const foregroundRunKind = foregroundPosition
    ? stageManagedRunKinds[foregroundPosition]
    : undefined;
  const currentStageRuns = managedActivity.filter(
    (item) => item.source === foregroundRunKind,
  );
  const acquisitionRuns = managedActivity.filter((item) => item.lane === "acquisition");
  const bundleStrategyRuns = managedActivity.filter((item) => item.lane === "bundle");
  const bundleSurface = allStageSurfaces(snapshot).find(
    (candidate) => candidate.kind === "Bundle",
  );
  const targetRuns = bundleSurface?.kind === "Bundle"
    ? bundleSurface.projection.target_graph.targets.map(targetActivity)
    : [];
  const bundleIsCurrentStage = foregroundRunKind === "bundle_stage";
  const sortActivity = (left: ResearchActivityItem, right: ResearchActivityItem) => (
    (right.updatedAt ?? -1) - (left.updatedAt ?? -1)
    || left.ref.localeCompare(right.ref)
  );
  currentStageRuns.sort(sortActivity);
  acquisitionRuns.sort(sortActivity);
  bundleStrategyRuns.sort(sortActivity);
  targetRuns.sort(sortActivity);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  return (
    <section
      className="lumen-research-trace"
      aria-labelledby="research-trace-title"
      data-work-state={work.state}
    >
      <header>
        <div>
          <small>RESEARCH TRACE · 真实事件，不估算百分比</small>
          <h2 id="research-trace-title">研究正在发生</h2>
        </div>
        <span className="lumen-trace-live" data-connected={connected ? "true" : "false"}>
          <i aria-hidden="true" />
          {connected ? "持续观察中" : "等待重新连接"}
        </span>
      </header>
      <div className="lumen-trace-line" role="list" aria-label="当前研究轨迹">
        <article role="listitem" data-trace-kind="agent">
          <span aria-hidden="true">根</span>
          <div><small>根 Agent 正在做什么</small><b>{work.title}</b><p>{work.detail}</p></div>
        </article>
        <article role="listitem" data-trace-kind="activity">
          <span aria-hidden="true">脉</span>
          <div>
            <small>最近真实活动</small>
            <b>{latestActivity
              ? researchEventCopy(latestActivity.eventType)
              : "本页尚未观察到新的运行事件"}</b>
            <p>{latestActivity
              ? `于 ${new Date(latestActivity.observedAt).toLocaleTimeString()} 被本页观察到。`
              : "已载入当前事实；静默不等于根 Agent 已停止。"}</p>
          </div>
        </article>
        <article role="listitem" data-trace-kind="product">
          <span aria-hidden="true">稿</span>
          <div><small>已经形成的研究产物</small><b>{product.title}</b><p>{product.detail}</p></div>
        </article>
        <article role="listitem" data-trace-kind="silence">
          <span aria-hidden="true">静</span>
          <div>
            <small>距最近一次本页可观察活动</small>
            <b>{elapsedCopy(clock - lastObservedAt)}</b>
            <p>这是可观察静默时长，不是超时、失败或剩余时间。</p>
          </div>
        </article>
      </div>
      <section className="lumen-activity-sources" aria-label="研究活动来源">
        <header>
          <b>按来源查看研究活动</b>
          <small>各来源保留自己的边界，不建立跨来源总顺序</small>
        </header>
        <div>
          <ResearchActivityLane
            name={bundleIsCurrentStage
              ? "Bundle 策略（当前 Stage 主智能体）"
              : "Stage 主智能体"}
            items={currentStageRuns}
            emptyCopy="当前 Stage 暂无托管运行记录。"
          />
          <ResearchActivityLane
            name="资料获取"
            items={acquisitionRuns}
            emptyCopy="DeepFetch / Acquisition 暂无托管运行记录。"
          />
          {!bundleIsCurrentStage ? (
            <ResearchActivityLane
              name="Bundle 策略"
              items={bundleStrategyRuns}
              emptyCopy="当前 Cycle 暂无 Bundle 策略托管运行记录。"
            />
          ) : null}
          <ResearchActivityLane
            name="实验任务"
            items={targetRuns}
            emptyCopy="当前 Cycle 暂无 Target 托管运行记录。"
          />
        </div>
      </section>
      <details className="lumen-trace-technical">
        <summary>查看系统核验细节</summary>
        <ReturnSummary snapshot={snapshot} />
        {latestActivity ? (
          <code>{latestActivity.eventType} · revision {latestActivity.revision}</code>
        ) : null}
      </details>
    </section>
  );
}

function CurrentCycleOverview({ snapshot }: { snapshot: PublicSnapshot }) {
  const foreground = snapshot.research_control.foreground;
  if (!foreground) return null;
  const question = exactForegroundQuestion(snapshot);
  const surfaces = allStageSurfaces(snapshot);
  const currentPurpose = stagePositions.find(
    (item) => item.position === foreground.stage.toLowerCase(),
  )?.purpose ?? "继续当前研究";

  return (
    <div
      className="lumen-current-cycle"
      data-testid="current-cycle-overview"
      data-cycle-ref={foreground.cycle_ref}
      data-question-ref={foreground.question_ref}
    >
      <p className="lumen-eyebrow">当前 Cycle · 可信研究现场</p>
      <h1 id="workspace-title">
        {question?.title ?? question?.unknown_statement ?? "当前研究问题"}<br />
        <em>{currentPurpose}</em>
      </h1>
      <p>
        当前攻克 <b>{foreground.question_ref}</b> · Cycle <b>{foreground.cycle_ref}</b>
      </p>
      <ol
        className="lumen-cycle-stage-map"
        aria-label="当前 Cycle 的四个可能 Stage"
      >
        {stagePositions.map(({ kind, position, purpose }) => {
          const surface = surfaces.find((candidate) => candidate.kind === kind) ?? null;
          const state = stagePositionState(surface, foreground, position);
          return (
            <li
              key={position}
              data-stage-position={position}
              data-stage-state={state}
              data-cycle-ref={foreground.cycle_ref}
            >
              <small>{kind} · 可能位置</small>
              <b>{stagePositionCopy(state)}</b>
              <span>{purpose}</span>
              {surface ? (
                <details>
                  <summary>验证详情</summary>
                  <code>
                    {surface.projection.eligibility.status}
                    {surface.projection.stage_commit?.commit_ref
                      ? ` · ${surface.projection.stage_commit.commit_ref}`
                      : ""}
                  </code>
                </details>
              ) : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function SnapshotHero({ snapshot }: { snapshot: PublicSnapshot }) {
  const ready = snapshot.readiness.status === "ready";
  const empty = snapshot.research_space.status === "empty";
  const creation = snapshot.quest_creation.current;
  const stageSurface = currentStageSurface(snapshot);

  if (!ready) {
    const failedChecks = snapshot.readiness.checks
      .filter((check) => check.status !== "ready")
      .map((check) => `${check.name}:${check.status}`);
    return (
      <>
        <p className="lumen-eyebrow coral">研究服务尚未就绪</p>
        <h1 id="workspace-title">
          已经找到研究空间。<br />
          <em>本地底座还未就绪。</em>
        </h1>
        <p>页面暂时保持只读；服务恢复后，这个窗口会继续接收同一项研究。</p>
        <div className="lumen-inline-state unavailable" role="status">
          <span aria-hidden="true">!</span>
          <div>
            <b>部分研究能力暂不可用</b>
            <small>{failedChecks.join(" · ") || "readiness:unavailable"}</small>
          </div>
        </div>
      </>
    );
  }

  if (empty) {
    return (
      <>
        <p className="lumen-eyebrow">新的研究空间</p>
        <h1 id="workspace-title">
          {creation ? "第一个研究任务正在形成。" : "这里还没有研究任务。"}
          <br />
          <em>{creation ? "从同一个草案继续。" : "从一个清楚的问题开始。"}</em>
        </h1>
        <p>
          {creation
            ? "当前草案已经保存；使用左侧 ＋ 回到连续创建窗口。"
            : "使用左侧固定的 ＋ 创建入口，设定目标并决定第一个研究问题。"}
        </p>
        <div className="lumen-inline-state ready" role="status">
          <span aria-hidden="true">✓</span>
          <div>
            <b>本地研究空间已就绪</b>
            <small>0 个研究任务 · 0 个研究问题</small>
          </div>
        </div>
      </>
    );
  }

  if (
    snapshot.research_control.status === "ready"
    && snapshot.research_control.foreground
  ) {
    return <CurrentCycleOverview snapshot={snapshot} />;
  }

  if (stageSurface?.kind === "Reasoning") {
    return (
      <ReasoningStageHero
        reasoningStage={stageSurface.projection}
        question={reasoningQuestion(stageSurface.projection, snapshot)}
      />
    );
  }

  if (stageSurface?.kind === "Bundle") {
    return (
      <BundleStageHero
        bundleStage={stageSurface.projection}
        question={bundleQuestion(stageSurface.projection, snapshot)}
      />
    );
  }

  if (stageSurface?.kind === "Plan") {
    return (
      <PlanStageHero
        planStage={stageSurface.projection}
        question={planQuestion(stageSurface.projection, snapshot)}
      />
    );
  }

  if (stageSurface?.kind === "Idea") {
    return (
      <IdeaStageHero
        ideaStage={stageSurface.projection}
        question={ideaQuestion(stageSurface.projection, snapshot)}
      />
    );
  }

  return (
    <>
      <p className="lumen-eyebrow">当前研究现场</p>
      <h1 id="workspace-title">
        研究已经在这里。<br />
        <em>从当前问题继续。</em>
      </h1>
      <p>这里仅报告已经观察到的研究事实，不用阶段动画代替真实进展。</p>
      <div className="lumen-inline-state ready" role="status">
        <span aria-hidden="true">✓</span>
        <div>
          <b>{snapshot.research_space.quest_count} 个研究任务</b>
          <small>
            {snapshot.research_space.question_count} 个研究问题 · {snapshot.research_space.foreground_cycle_count} 项当前工作
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

function bundleQuestion(
  bundleStage: BundleStageProjection,
  snapshot?: PublicSnapshot,
): IdeaQuestionSummary {
  const creation = snapshot?.quest_creation.current;
  return {
    quest_ref: creation?.quest_ref,
    question_ref: bundleStage.eligibility.question_ref ?? creation?.question_ref,
    graph_revision: snapshot?.owners.research_graph?.revision,
    ...(creation?.proposal?.content ?? {}),
    ...(bundleStage.stage_run_request?.accepted_question_binding ?? {}),
    ...(snapshot?.research_space.current_question ?? {}),
  };
}

function reasoningQuestion(
  reasoningStage: ReasoningStageProjection,
  snapshot?: PublicSnapshot,
): IdeaQuestionSummary {
  const creation = snapshot?.quest_creation.current;
  return {
    quest_ref: creation?.quest_ref,
    question_ref: reasoningStage.eligibility.question_ref
      ?? creation?.question_ref,
    graph_revision: snapshot?.owners.research_graph?.revision,
    ...(creation?.proposal?.content ?? {}),
    ...(reasoningStage.stage_run_request?.accepted_question_binding ?? {}),
    ...(snapshot?.research_space.current_question ?? {}),
  };
}

function ResearchStageHero({
  question,
  stage,
  committed,
}: {
  question: IdeaQuestionSummary;
  stage: CurrentStageSurface["kind"];
  committed: boolean;
}) {
  const activity = stage === "Idea"
    ? "形成候选解释"
    : stage === "Plan"
      ? "设计验证路线"
      : stage === "Bundle"
        ? "运行实验并收集证据"
        : "综合证据并形成判断";
  return (
    <>
      <p className="lumen-eyebrow">ROOT AGENT · 研究现场</p>
      <h1 id="workspace-title">
        {committed ? "一段真实研究已经收口。" : "根 Agent 正在工作。"}<br />
        <em>{committed ? "产物已保存，等待下一段研究。" : activity}</em>
      </h1>
      <p>{question.unknown_statement ?? question.title ?? "当前研究问题正在被持续推进。"}</p>
    </>
  );
}

function IdeaStageHero({ ideaStage, question }: {
  ideaStage: IdeaStageProjection;
  question: IdeaQuestionSummary;
}) {
  return <ResearchStageHero stage="Idea" question={question} committed={Boolean(ideaStage.stage_commit)} />;
}

function PlanStageHero({ planStage, question }: {
  planStage: PlanStageProjection;
  question: IdeaQuestionSummary;
}) {
  return <ResearchStageHero stage="Plan" question={question} committed={Boolean(planStage.stage_commit)} />;
}

function BundleStageHero({ bundleStage, question }: {
  bundleStage: BundleStageProjection;
  question: IdeaQuestionSummary;
}) {
  return <ResearchStageHero stage="Bundle" question={question} committed={Boolean(bundleStage.stage_commit)} />;
}

function ReasoningStageHero({ reasoningStage, question }: {
  reasoningStage: ReasoningStageProjection;
  question: IdeaQuestionSummary;
}) {
  return <ResearchStageHero stage="Reasoning" question={question} committed={Boolean(reasoningStage.stage_commit)} />;
}

function CurrentQuestionCard({
  stage,
  question,
}: {
  stage: "Idea" | "Plan" | "Bundle" | "Reasoning";
  question: IdeaQuestionSummary;
}) {
  const questionRef = question.question_ref ?? "当前问题";
  const graphRevision = question.graph_revision;

  return (
    <section
      className="lumen-card lumen-question-card"
      aria-labelledby="current-question-title"
      data-testid="current-question-card"
    >
      <header className="lumen-card-head">
        <b id="current-question-title">当前研究问题</b>
        <small>
          {graphRevision === undefined ? "已同步" : `已同步 · 版本 ${graphRevision}`}
        </small>
      </header>
      <div className="lumen-question-path" aria-label="当前研究问题与根 Agent 工作路径">
        <span className="quest"><small>研究空间</small><b>{question.quest_ref ?? "当前"}</b></span>
        <i aria-hidden="true" />
        <span className="question"><small>研究问题</small><b>{questionRef}</b></span>
        <i aria-hidden="true" />
        <span className={stage.toLowerCase()}><small>根 Agent</small><b>工作中</b></span>
      </div>
      <div className="lumen-question-copy">
        <small>未知点 · 回答形式 · 适用范围</small>
        <h2>{question.unknown_statement ?? question.title ?? "当前已接纳 Question"}</h2>
        <p>
          {question.applicability_scope
            ?? question.answer_shape
            ?? "根 Agent 围绕这个问题读取已有材料、形成产物并接受核验。"}
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
  runtimeControl,
}: {
  ideaStage: IdeaStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  runtimeControl: ReactNode;
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
        <b id="idea-stage-title">研究工具与候选材料</b>
        <small>围绕当前问题继续探索</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="idea-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>候选解释暂时无法继续</b>
            <small>已形成的材料仍在；恢复后会从当前位置继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      <details className="lumen-stage-technical">
        <summary>系统如何核验这段研究</summary>
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
      </details>
      <details className="lumen-idea-details">
        <summary>技术身份与核验记录</summary>
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
        {runtimeControl}
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
  runtimeControl,
}: {
  planStage: PlanStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  runtimeControl: ReactNode;
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
        <b id="plan-stage-title">验证方案</b>
        <small>把候选解释转成可执行路线</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="plan-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>验证方案暂时无法继续</b>
            <small>已完成的材料仍在；恢复后会从第一个缺口继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      {noGap ? (
        <div className="lumen-plan-disposition" data-testid="plan-no-gap-disposition">
          <span aria-hidden="true">✓</span>
          <p>
            <b>现有证据已经覆盖问题</b>
            <small>没有新的验证缺口，不会启动无意义的实验。</small>
          </p>
        </div>
      ) : null}
      <details className="lumen-stage-technical">
        <summary>系统如何核验这段研究</summary>
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
      </details>
      <details className="lumen-idea-details">
        <summary>技术身份与核验记录</summary>
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
        {runtimeControl}
      </details>
    </section>
  );
}

type BundleStageState =
  | "eligibility"
  | "stage-run-request"
  | "root-run"
  | "target-work"
  | "stage-commit";

function bundleStageHealthBlocker(
  snapshot: PublicSnapshot | null,
): IdeaStageHealthBlocker | null {
  const worker = snapshot?.readiness.checks.find(
    (check) => check.name === "bundle_stage_worker" && check.status !== "ready",
  );
  if (!worker) return null;
  return { code: worker.reason?.code ?? `bundle_stage_worker_${worker.status}` };
}

function currentBundleStageState(
  bundleStage: BundleStageProjection,
): BundleStageState {
  if (bundleStage.stage_commit) return "stage-commit";
  if (bundleStage.target_graph.status !== "not_attempted") return "target-work";
  if (bundleStage.run) return "root-run";
  if (bundleStage.stage_run_request) return "stage-run-request";
  return "eligibility";
}

function bundleFactRows(
  bundleStage: BundleStageProjection,
  phase: BundleStageState,
): ReturnType<typeof ideaFactRows> {
  const request = bundleStage.stage_run_request;
  const run = bundleStage.run;
  const graph = bundleStage.target_graph;
  const commit = bundleStage.stage_commit;
  const skipped = bundleStage.disposition.status === "skipped";
  const exhaustion = bundleStage.bundle_exhaustion;
  const exhausted = bundleStage.disposition.report_disposition === "exhausted" &&
    exhaustion?.kind === "BundleExhaustion";
  const targetCount = graph.targets.length;
  const committedCount = bundleStage.target_commits.length;
  const blockedTargets = graph.targets.filter((target) =>
    ["blocked", "failed", "fenced", "replan_required"].includes(target.status)
  );

  return [
    {
      slot: "eligibility",
      label: "Bundle eligibility",
      owner: "AE",
      state: bundleStage.eligibility.status === "eligible"
        ? phase === "eligibility" ? "current" : "done"
        : "blocked",
      title: bundleStage.eligibility.status === "eligible"
        ? "已接纳 FormalPlan 与精确 GapSet，Bundle 具备处理资格"
        : `Bundle eligibility · ${bundleStage.eligibility.status}`,
      status: bundleStage.eligibility.status,
    },
    {
      slot: "stage-run-request",
      label: "StageRunRequest",
      owner: "AE",
      state: request
        ? phase === "stage-run-request" ? "current" : "done"
        : "pending",
      title: request
        ? "已冻结 AcceptedFormalPlanBinding、request epoch 与 ContextPack"
        : "等待 Advancement Engine 签发冻结请求",
      status: request ? request.status ?? "issued" : "not_issued",
    },
    {
      slot: "root-run",
      label: "Bundle root Run",
      owner: "AR",
      state: skipped
        ? "done"
        : run
          ? isRunBlocked(run.status)
            ? "blocked"
            : run.status === "completed" ? "done" : "current"
          : exhausted ? "blocked" : "pending",
      title: skipped
        ? "GapSet 为空；未创建 Bundle Run"
        : run
          ? exhausted
            ? `BundleExhaustion 由真实 root Run ${run.run_ref ?? "identity unavailable"} 的执行证据支撑`
            : "一个 root/native Session 调度正式 Target；child agent 不进入 Target DAG"
          : exhausted
            ? "BundleExhaustion 缺少公开 root Run；不会把缺失伪装成已执行"
            : "等待 Agent Runtime admission",
      status: skipped
        ? "not_created_by_design"
        : run?.status ?? (exhausted ? "run_projection_missing" : "not_created"),
    },
    {
      slot: "target-dag",
      label: "Target DAG / frontier",
      owner: "RG",
      state: skipped || exhausted
        ? "done"
        : graph.status === "accepted"
          ? blockedTargets.length ? "blocked" : "done"
          : "pending",
      title: skipped || exhausted
        ? exhausted
          ? "正式探索 basis 已穷尽；没有制造伪 Target identity"
          : "GapSet 为空；未制造伪 Target"
        : graph.status === "accepted"
          ? `${targetCount} Target · ${graph.frontier.length} frontier；身份与依赖由 RG 拥有`
          : "等待 RG 接纳正式 Target identity/spec/DAG",
      status: skipped ? "not_created_by_design" : exhausted ? "exhausted" : graph.status,
    },
    {
      slot: "target-closure",
      label: "TargetRun → TargetCommit",
      owner: "AR / RM / RG",
      state: skipped || exhausted
        ? "done"
        : blockedTargets.length
          ? "blocked"
          : targetCount > 0 && committedCount === targetCount
            ? "done"
            : graph.status === "accepted" ? "current" : "pending",
      title: skipped || exhausted
        ? exhausted
          ? `BundleExhaustion · ${exhaustion?.basis_kind ?? "formal basis"} 已由 decision receipt 接纳`
          : "没有 TargetRun 或 TargetCommit"
        : `${committedCount}/${targetCount} closure 已冻结；已接纳的局部结果不会被其他失败抹掉`,
      status: skipped
        ? "not_attempted"
        : exhausted
          ? "exhausted"
        : blockedTargets.length
          ? "partial_blocked"
          : bundleStage.disposition.status,
    },
    {
      slot: "stage-commit",
      label: "Bundle StageCommit",
      owner: "AE",
      state: commit ? "done" : committedCount === targetCount && targetCount > 0
        ? "current"
        : "pending",
      title: commit
        ? `StageCommit(${commit.status}) 已验证全部 closure 与 Owner receipts`
        : "尚无 StageCommit；Target Agent 与资产存在都不能提前推进",
      status: commit?.status ?? "not_committed",
    },
  ];
}

const targetRawOutputPageSize = 64 * 1024;
const targetRawOutputPollMilliseconds = 750;

type TargetRawTerminalState = TargetRawOutputPage;

function validateTargetRawOutputPage(
  page: TargetRawOutputPage,
  targetRef: string,
  targetRunRef: string | null | undefined,
  expectedOffset: number,
): void {
  const textBytes = typeof page.text === "string"
    ? new TextEncoder().encode(page.text).byteLength
    : -1;
  if (
    page.schema_ref !== "meta-research/target-raw-output-page/v1"
    || page.target_ref !== targetRef
    || typeof targetRunRef !== "string"
    || targetRunRef.length === 0
    || page.target_run_ref !== targetRunRef
    || page.exact !== true
    || page.unredacted !== true
    || typeof page.attempt_ref !== "string"
    || page.attempt_ref.length === 0
    || typeof page.root_session_ref !== "string"
    || page.root_session_ref.length === 0
    || typeof page.fence_ref !== "string"
    || page.fence_ref.length === 0
    || typeof page.operation_ref !== "string"
    || page.operation_ref.length === 0
    || !Number.isSafeInteger(page.attempt_generation)
    || page.attempt_generation < 1
    || !Number.isSafeInteger(page.operation_generation)
    || page.operation_generation < 1
    || !["running", "executed", "failed"].includes(page.operation_status)
    || (
      page.operation_outcome_code !== null
      && (
        typeof page.operation_outcome_code !== "string"
        || page.operation_outcome_code.length === 0
      )
    )
    || (
      page.native_session_ref !== null
      && (
        typeof page.native_session_ref !== "string"
        || page.native_session_ref.length === 0
      )
    )
    || (
      page.root_native_session_ref !== null
      && (
        typeof page.root_native_session_ref !== "string"
        || page.root_native_session_ref.length === 0
        || (
          page.native_session_ref !== null
          && page.root_native_session_ref !== page.native_session_ref
        )
      )
    )
    || !/^[0-9a-f]{64}$/.test(page.transport_invocation_hash)
    || page.stream_ref
    !== `target-raw-output:${page.transport_invocation_hash}`
    || !["live", "complete"].includes(page.status)
    || typeof page.text !== "string"
    || typeof page.has_more !== "boolean"
    || typeof page.source_caught_up !== "boolean"
    || !Number.isSafeInteger(page.offset)
    || !Number.isSafeInteger(page.next_offset)
    || !Number.isSafeInteger(page.mapped_bytes)
    || !Number.isSafeInteger(page.source_bytes)
    || page.offset < 0
    || page.offset !== expectedOffset
    || page.next_offset < page.offset
    || page.mapped_bytes < page.next_offset
    || page.source_bytes < 0
    || page.next_offset - page.offset !== textBytes
    || page.has_more
    !== (page.next_offset < page.mapped_bytes || !page.source_caught_up)
    || (
      page.status === "complete"
      && (
        !page.source_caught_up
        || !["executed", "failed"].includes(page.operation_status)
      )
    )
  ) {
    throw new ProductError("target_raw_output_identity_invalid");
  }
}

function BundleTargetCard({
  target,
  targetCommit,
  terminalOpen,
  onOpenTerminal,
}: {
  target: BundleTargetProjection;
  targetCommit: BundleTargetCommitProjection | undefined;
  terminalOpen: boolean;
  onOpenTerminal: () => void;
}) {
  const result = targetCommit?.result_disposition
    ?? target.blocker?.code
    ?? target.status;
  const statusCopy = targetCommit
    ? "实验结果已接纳"
    : target.blocker
      ? "等待处理"
      : activityStatusCopy(target.status);

  return (
    <article
      className="lumen-bundle-target"
      data-target-status={target.status}
      data-target-ref={target.target_ref}
    >
      <div className="lumen-bundle-target-summary">
        <span className="lumen-bundle-target-mark" aria-hidden="true">
          {target.status === "committed" ? "✓" : target.status === "ready" ? "→" : "·"}
        </span>
        <p>
          <b>{target.target_key}</b>
          <small>{statusCopy}</small>
        </p>
        <div className="lumen-bundle-target-actions">
          {target.target_run_ref ? (
            <button
              type="button"
              aria-pressed={terminalOpen}
              aria-controls="target-output-dialog"
              onClick={onOpenTerminal}
            >
              {terminalOpen ? "输出窗口已打开" : "查看原始输出"}
            </button>
          ) : null}
        </div>
      </div>
      <details className="lumen-bundle-target-technical">
        <summary>查看核验详情</summary>
        <dl>
          <div><dt>Target</dt><dd>{target.target_ref}</dd></div>
          <div><dt>Target run</dt><dd>{target.target_run_ref ?? "not recorded"}</dd></div>
          <div><dt>Result</dt><dd>{result}</dd></div>
        </dl>
      </details>
    </article>
  );
}

function TargetTerminalDialog({
  target,
  observationPointer,
  minimized,
  blockedByHumanRequest,
  activityPaused,
  onMinimize,
  onClose,
}: {
  target: BundleTargetProjection;
  observationPointer: TargetRootObservationPointer | null;
  minimized: boolean;
  blockedByHumanRequest: boolean;
  activityPaused: boolean;
  onMinimize: () => void;
  onClose: () => void;
}) {
  const [terminal, setTerminal] = useState<TargetRawTerminalState | null>(null);
  const [loading, setLoading] = useState(false);
  const [terminalError, setTerminalError] = useState<string | null>(null);
  const [previousOffsets, setPreviousOffsets] = useState<number[]>([]);
  const [documentVisible, setDocumentVisible] = useState(
    () => document.visibilityState !== "hidden",
  );
  const terminalRef = useRef<TargetRawTerminalState | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const requestSequence = useRef(0);
  const paused = minimized
    || blockedByHumanRequest
    || activityPaused
    || !documentVisible;

  const loadRawOutput = useCallback(async (
    after: number,
    direction: "reset" | "next" | "previous" | "refresh",
  ) => {
    if (activeRequest.current !== null) return;
    const request = requestSequence.current + 1;
    requestSequence.current = request;
    const controller = new AbortController();
    activeRequest.current = controller;
    const current = terminalRef.current;
    setLoading(current === null || direction !== "refresh");
    try {
      let page: TargetRawOutputPage;
      try {
        page = await fetchTargetRawOutput(target.target_ref, {
          after,
          limit: targetRawOutputPageSize,
          signal: controller.signal,
        });
      } catch (caught) {
        if (
          after === 0
          || !(caught instanceof ProductError)
          || caught.code !== "target_raw_output_cursor_stale"
        ) throw caught;
        after = 0;
        direction = "reset";
        page = await fetchTargetRawOutput(target.target_ref, {
          after,
          limit: targetRawOutputPageSize,
          signal: controller.signal,
        });
      }
      if (current && page.stream_ref !== current.stream_ref) {
        if (after !== 0) {
          after = 0;
          page = await fetchTargetRawOutput(target.target_ref, {
            after,
            limit: targetRawOutputPageSize,
            signal: controller.signal,
          });
        }
        direction = "reset";
      }
      validateTargetRawOutputPage(
        page,
        target.target_ref,
        target.target_run_ref,
        after,
      );
      if (request !== requestSequence.current) return;
      if (direction === "next" && current) {
        setPreviousOffsets((offsets) => [...offsets, current.offset]);
      } else if (direction === "previous") {
        setPreviousOffsets((offsets) => offsets.slice(0, -1));
      } else if (direction === "reset") {
        setPreviousOffsets([]);
      }
      terminalRef.current = page;
      setTerminal(page);
      setTerminalError(null);
    } catch (caught) {
      if ((caught as Error).name === "AbortError") return;
      if (request !== requestSequence.current) return;
      const code = caught instanceof ProductError
        ? caught.code
        : "target_raw_output_unavailable";
      setTerminalError(code);
    } finally {
      if (request === requestSequence.current) {
        activeRequest.current = null;
        setLoading(false);
      }
    }
  }, [target.target_ref, target.target_run_ref]);

  useEffect(() => {
    const handleVisibility = () => {
      setDocumentVisible(document.visibilityState !== "hidden");
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    terminalRef.current = null;
    setTerminal(null);
    setPreviousOffsets([]);
    setTerminalError(null);
    return () => {
      requestSequence.current += 1;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [target.target_ref]);

  useEffect(() => {
    if (paused) {
      requestSequence.current += 1;
      activeRequest.current?.abort();
      activeRequest.current = null;
      setLoading(false);
      return;
    }
    const current = terminalRef.current;
    void loadRawOutput(current?.offset ?? 0, current ? "refresh" : "reset");
  }, [loadRawOutput, paused]);

  useEffect(() => {
    if (paused) return;
    if (observationPointer?.target_ref !== target.target_ref) return;
    const current = terminalRef.current;
    if (!current || current.has_more) return;
    void loadRawOutput(current.offset, "refresh");
  }, [
    loadRawOutput,
    observationPointer?.head_cursor,
    observationPointer?.target_ref,
    paused,
    target.target_ref,
  ]);

  const targetMayStillProduceOutput = ![
    "committed",
    "failed",
    "blocked",
    "fenced",
    "cancelled",
  ].includes(target.status.toLowerCase());
  useEffect(() => {
    if (
      paused
      || loading
      || terminal?.has_more
      || !(terminal?.status === "live" || (!terminal && targetMayStillProduceOutput))
    ) return;
    const timer = window.setTimeout(() => {
      const current = terminalRef.current;
      void loadRawOutput(current?.offset ?? 0, current ? "refresh" : "reset");
    }, targetRawOutputPollMilliseconds);
    return () => window.clearTimeout(timer);
  }, [
    loadRawOutput,
    loading,
    paused,
    targetMayStillProduceOutput,
    terminal?.has_more,
    terminal?.offset,
    terminal?.status,
    terminalError,
  ]);

  const atHead = Boolean(
    terminal
    && terminal.source_caught_up
    && !terminal.has_more,
  );

  return createPortal(
    <section
      id="target-output-dialog"
      className="lumen-target-terminal lumen-target-terminal-dialog"
      role="dialog"
      aria-modal="false"
      aria-hidden={blockedByHumanRequest ? true : undefined}
      aria-label={`${target.target_key} 实验原始输出`}
      inert={blockedByHumanRequest}
      data-hc-background
      data-hc-inert-owner="terminal"
      data-terminal-state={terminal?.status ?? (loading ? "loading" : "unavailable")}
      data-minimized={minimized ? "true" : "false"}
    >
      <header>
        <div>
          <small>PRIVATE PROVIDER SPOOL / RAW STDOUT + STDERR</small>
          <b>{target.target_key} · 原始输出</b>
        </div>
        <div className="lumen-target-terminal-actions">
          <span data-live={terminal?.status === "live" ? "true" : "false"}>
            {loading && !terminal
              ? "读取中"
              : terminal?.status === "live"
                ? "持续更新"
                : terminal?.status ?? "等待输出"}
          </span>
          <button type="button" onClick={onMinimize}>
            {minimized ? "展开" : "最小化"}
          </button>
          <button type="button" onClick={onClose} aria-label="关闭原始输出窗口">×</button>
        </div>
      </header>
      {!minimized ? (
        <div className="lumen-target-terminal-body">
          <p className="lumen-target-terminal-boundary">
            这里按原顺序展示未经脱敏的根命令 stdout/stderr，可能包含敏感信息；展示不会修改 Agent workspace，也不决定研究结果。
          </p>
          {terminal ? (
            <dl className="lumen-target-terminal-identity">
              <div><dt>Target run</dt><dd>{terminal.target_run_ref ?? target.target_run_ref ?? "正在绑定"}</dd></div>
              <div><dt>Provider operation</dt><dd>{terminal.operation_ref}</dd></div>
              <div><dt>Transport</dt><dd>{terminal.transport_invocation_hash.slice(0, 16)}</dd></div>
            </dl>
          ) : null}
          {terminalError ? (
            <div className="lumen-target-terminal-error" role="alert">
              <b>原始输出暂不可读</b>
              <code>{terminalError}</code>
              <button
                type="button"
                onClick={() => {
                  const current = terminalRef.current;
                  void loadRawOutput(
                    current?.offset ?? 0,
                    current ? "refresh" : "reset",
                  );
                }}
              >
                继续读取
              </button>
            </div>
          ) : null}
          {terminal?.text ? (
            <pre
              className="lumen-target-terminal-log"
              role="log"
              aria-live="off"
              aria-label={`${target.target_key} 未经脱敏的原始 stdout/stderr`}
            >{terminal.text}</pre>
          ) : null}
          {terminal && terminal.text.length === 0 ? (
            <p className="lumen-target-terminal-empty">
              当前还没有根命令 stdout/stderr；运行仍在进行时，窗口会继续读取私有 spool。
            </p>
          ) : null}
          {terminal ? (
            <footer>
              <small>
                {atHead ? "已追到当前 spool" : "还有后续页，可按需读取"}
                {` · ${terminal.mapped_bytes} mapped / ${terminal.source_bytes} source bytes`}
              </small>
              <nav aria-label="原始输出分页">
                <button
                  type="button"
                  disabled={loading || previousOffsets.length === 0}
                  onClick={() => {
                    const previous = previousOffsets.at(-1);
                    if (previous !== undefined) {
                      void loadRawOutput(previous, "previous");
                    }
                  }}
                >返回上一页</button>
                <code>{terminal.offset}–{terminal.next_offset}</code>
                <button
                  type="button"
                  disabled={loading || !terminal.has_more}
                  onClick={() => void loadRawOutput(terminal.next_offset, "next")}
                >读取下一页</button>
              </nav>
            </footer>
          ) : null}
        </div>
      ) : null}
    </section>,
    document.body,
  );
}

function BundleStageCard({
  bundleStage,
  healthBlocker,
  observationPointers,
  humanRequestModalOpen,
  activityPaused,
  runtimeControl,
}: {
  bundleStage: BundleStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  observationPointers: Record<string, TargetRootObservationPointer>;
  humanRequestModalOpen: boolean;
  activityPaused: boolean;
  runtimeControl: ReactNode;
}) {
  const phase = currentBundleStageState(bundleStage);
  const rows = bundleFactRows(bundleStage, phase);
  const request = bundleStage.stage_run_request;
  const run = bundleStage.run;
  const graph = bundleStage.target_graph;
  const commit = bundleStage.stage_commit;
  const exhaustion = bundleStage.bundle_exhaustion;
  const formalPlan = request?.accepted_formal_plan_binding ?? {};
  const [terminalTargetRef, setTerminalTargetRef] = useState<string | null>(null);
  const [terminalMinimized, setTerminalMinimized] = useState(false);
  const [targetPage, setTargetPage] = useState(0);
  const targetPageSize = 6;
  const targetPageCount = Math.max(1, Math.ceil(graph.targets.length / targetPageSize));
  const boundedTargetPage = Math.min(targetPage, targetPageCount - 1);
  const visibleTargets = graph.targets.slice(
    boundedTargetPage * targetPageSize,
    (boundedTargetPage + 1) * targetPageSize,
  );
  const terminalTarget = graph.targets.find(
    (target) => target.target_ref === terminalTargetRef,
  ) ?? null;

  useEffect(() => {
    if (terminalTargetRef && !terminalTarget) setTerminalTargetRef(null);
  }, [terminalTarget, terminalTargetRef]);

  useEffect(() => {
    if (targetPage >= targetPageCount) setTargetPage(targetPageCount - 1);
  }, [targetPage, targetPageCount]);

  return (
    <section
      className="lumen-card lumen-idea-card lumen-plan-card lumen-bundle-card"
      aria-labelledby="bundle-stage-title"
      data-testid="bundle-stage-card"
      data-bundle-stage-state={phase}
      data-bundle-disposition={bundleStage.disposition.report_disposition ?? (
        bundleStage.disposition.status === "skipped" ? "skipped" : "targeted"
      )}
    >
      <header className="lumen-card-head">
        <b id="bundle-stage-title">实验与训练</b>
        <small>真实命令、真实输出、真实结果</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="bundle-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>实验调度暂时无法继续</b>
            <small>已形成的实验结果保持可见；恢复后会从原任务继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      <details className="lumen-stage-technical">
        <summary>系统如何核验这段研究</summary>
        <div className="lumen-idea-facts" role="list">
          {rows.map((row) => (
          <article
            key={row.slot}
            className="lumen-idea-fact"
            data-bundle-slot={row.slot}
            data-state={row.state}
            role="listitem"
          >
            <span className="lumen-idea-fact-mark" aria-hidden="true">
              {row.state === "done" ? "✓" : row.state === "blocked" ? "!" : "→"}
            </span>
            <div><small>{row.label}</small><b>{row.title}</b><code>{row.status}</code></div>
            <span>{row.owner}</span>
          </article>
          ))}
        </div>
      </details>
      {graph.targets.length ? (
        <div className="lumen-bundle-targets" data-testid="bundle-target-list">
          {visibleTargets.map((target) => {
            const targetCommit = bundleStage.target_commits.find(
              (candidate) => candidate.target_ref === target.target_ref,
            );
            return (
              <BundleTargetCard
                key={target.target_ref}
                target={target}
                targetCommit={targetCommit}
                terminalOpen={target.target_ref === terminalTargetRef}
                onOpenTerminal={() => {
                  setTerminalTargetRef(target.target_ref);
                  setTerminalMinimized(false);
                }}
              />
            );
          })}
          {targetPageCount > 1 ? (
            <nav className="lumen-bundle-target-pagination" aria-label="实验任务分页">
              <button
                type="button"
                disabled={boundedTargetPage === 0}
                onClick={() => setTargetPage((current) => Math.max(0, current - 1))}
              >返回上一页</button>
              <small>{boundedTargetPage + 1} / {targetPageCount}</small>
              <button
                type="button"
                disabled={boundedTargetPage === targetPageCount - 1}
                onClick={() => setTargetPage((current) => Math.min(targetPageCount - 1, current + 1))}
              >读取下一页</button>
            </nav>
          ) : null}
        </div>
      ) : null}
      {terminalTarget ? (
        <TargetTerminalDialog
          key={terminalTarget.target_ref}
          target={terminalTarget}
          observationPointer={observationPointers[terminalTarget.target_ref] ?? null}
          minimized={terminalMinimized}
          blockedByHumanRequest={humanRequestModalOpen}
          activityPaused={activityPaused}
          onMinimize={() => setTerminalMinimized((current) => !current)}
          onClose={() => setTerminalTargetRef(null)}
        />
      ) : null}
      <details className="lumen-idea-details">
        <summary>技术身份与核验记录</summary>
        <dl>
          <IdeaDetail label="Cycle" value={bundleStage.eligibility.cycle_ref} />
          <IdeaDetail label="FormalPlan" value={bundleStage.eligibility.formal_plan_ref} />
          <IdeaDetail label="StageRunRequest" value={request?.request_ref} />
          <IdeaDetail label="StageRunRequest receipt" value={receiptRef(request?.receipt)} />
          <IdeaDetail label="AcceptedFormalPlanBinding" value={recordText(formalPlan, "formal_plan_ref")} />
          <IdeaDetail label="Plan StageCommit" value={recordText(formalPlan, "stage_commit_ref")} />
          <IdeaDetail label="ContextPack" value={request?.context_pack_ref} />
          <IdeaDetail label="Bundle Run" value={run?.run_ref} />
          <IdeaDetail label="Root Session" value={run?.root_session_ref} />
          <IdeaDetail label="Native Session" value={run?.native_session_ref} />
          <IdeaDetail label="Attempt" value={run?.attempt_ref} />
          <IdeaDetail label="Fence" value={run?.fence_ref} />
          <IdeaDetail label="Child reviewer agent" value={run?.review?.reviewer_agent_ref} />
          <IdeaDetail label="TargetGraph" value={graph.graph_ref} />
          <IdeaDetail label="TargetGraph receipt" value={receiptRef(graph.receipt)} />
          <IdeaDetail label="Target count" value={graph.targets.length} />
          <IdeaDetail label="Frontier count" value={graph.frontier.length} />
          <IdeaDetail label="TargetCommit count" value={bundleStage.target_commits.length} />
          <IdeaDetail label="Baseline Pool count" value={bundleStage.baseline_pool.length} />
          <IdeaDetail label="BundleExhaustion" value={exhaustion?.proposal_ref} />
          <IdeaDetail label="Exhaustion basis kind" value={exhaustion?.basis_kind} />
          <IdeaDetail label="Exhaustion basis" value={exhaustion?.basis_ref} />
          <IdeaDetail label="Exhaustion basis receipt" value={receiptRef(exhaustion?.basis_receipt)} />
          <IdeaDetail label="Exhaustion decision receipt" value={receiptRef(exhaustion?.decision_receipt)} />
          <IdeaDetail label="StageCommit" value={commit?.commit_ref ?? commit?.stage_commit_ref} />
          <IdeaDetail label="StageCommit receipt" value={receiptRef(commit?.receipt)} />
          <IdeaDetail label="Next Stage" value={commit?.next_stage} />
        </dl>
        {runtimeControl}
      </details>
    </section>
  );
}

type ReasoningStageState =
  | "eligibility"
  | "stage-run-request"
  | "run"
  | "content-acceptance"
  | "domain-acceptance"
  | "successor"
  | "stage-commit";

function reasoningStageHealthBlocker(
  snapshot: PublicSnapshot | null,
): IdeaStageHealthBlocker | null {
  const worker = snapshot?.readiness.checks.find(
    (check) => check.name === "reasoning_stage_worker" && check.status !== "ready",
  );
  if (!worker) return null;
  return {
    code: worker.reason?.code ?? `reasoning_stage_worker_${worker.status}`,
  };
}

function currentReasoningStageState(
  reasoningStage: ReasoningStageProjection,
): ReasoningStageState {
  if (reasoningStage.stage_commit) return "stage-commit";
  if (reasoningStage.transition.status !== "not_attempted") return "successor";
  if (reasoningStage.reasoning_acceptance.status === "accepted") {
    return "successor";
  }
  if (reasoningStage.reasoning_acceptance.status === "awaiting_domain") {
    return "domain-acceptance";
  }
  if (reasoningStage.reasoning_acceptance.status === "awaiting_content") {
    return "content-acceptance";
  }
  if (reasoningStage.run) return "run";
  if (reasoningStage.stage_run_request) return "stage-run-request";
  return "eligibility";
}

function reasoningFactRows(
  reasoningStage: ReasoningStageProjection,
  phase: ReasoningStageState,
): ReturnType<typeof ideaFactRows> {
  const request = reasoningStage.stage_run_request;
  const run = reasoningStage.run;
  const acceptance = reasoningStage.reasoning_acceptance;
  const content = acceptance.content;
  const domain = acceptance.domain;
  const transition = reasoningStage.transition;
  const commit = reasoningStage.stage_commit;
  const eligibilityBlocked = !["eligible", "requested", "consumed"].includes(
    reasoningStage.eligibility.status,
  );
  const contentAccepted = content.status === "accepted";
  const domainAccepted = domain.status === "accepted";
  const domainBlocked = ["rejected", "stale", "needs_input"].includes(
    domain.status,
  );

  return [
    {
      slot: "eligibility",
      label: "Reasoning eligibility",
      owner: "AE",
      state: eligibilityBlocked
        ? "blocked"
        : phase === "eligibility" ? "current" : "done",
      title: reasoningStage.eligibility.status === "eligible"
        ? "上游路线 closure 已由 Advancement Engine 证明可收口"
        : reasoningStage.eligibility.status === "requested"
          ? "资格已由 current Reasoning StageRunRequest 消费"
          : `Reasoning eligibility · ${reasoningStage.eligibility.status}`,
      status: reasoningStage.eligibility.status,
    },
    {
      slot: "stage-run-request",
      label: "StageRunRequest",
      owner: "AE",
      state: request
        ? phase === "stage-run-request" ? "current" : "done"
        : "pending",
      title: request
        ? "已冻结 AcceptedQuestion、Foreground epoch、route closure 与证据输入"
        : "等待 Advancement Engine 签发冻结请求",
      status: request ? request.status ?? "issued" : "not_issued",
    },
    {
      slot: "run",
      label: "Run / Attempt",
      owner: "AR",
      state: run
        ? isRunBlocked(run.status)
          ? "blocked"
          : run.status === "completed" ? "done" : "current"
        : "pending",
      title: !run
        ? "等待 Agent Runtime admission"
        : isRunBlocked(run.status)
          ? "Run 被类型化 blocker 阻塞；不会伪造 ScientificOutcome"
          : run.status === "admitted" && !run.attempt_execution_receipt
            ? "Agent Runtime 已 admission；实际 Reasoning Skill 尚未形成 Attempt 执行证据"
            : run.status === "completed"
              ? "领域接纳已验证，Run completion 已独立形成"
              : "Attempt 执行证据已形成；等待 Owner 分层接纳",
      status: run?.status ?? "not_created",
    },
    {
      slot: "content-acceptance",
      label: "Content acceptance",
      owner: "RM",
      state: contentAccepted
        ? "done"
        : phase === "content-acceptance" ? "current" : "pending",
      title: contentAccepted
        ? "ScientificOutcome 与唯一 transition 已作为不可变内容接纳"
        : "等待 Research Memory 校验 evidence role 与闭合内容",
      status: content.status,
    },
    {
      slot: "domain-acceptance",
      label: "Domain acceptance",
      owner: "RG",
      state: domainBlocked
        ? "blocked"
        : domainAccepted
          ? "done"
          : phase === "domain-acceptance" ? "current" : "pending",
      title: domainBlocked
        ? "正式科学语义被拒绝；current Session 将按结构化依据修订"
        : domainAccepted
          ? "Research Graph 已独立接纳正式科学语义"
          : "等待 Research Graph 形成独立 domain decision",
      status: domain.status,
    },
    {
      slot: "successor",
      label: "Successor candidate",
      owner: "RG / AE / HC",
      state: commit
        ? "done"
        : transition.status === "proposed" ? "current" : "pending",
      title: transition.status === "proposed"
        ? `${transition.kind ?? "Transition"} 已提出，但尚不等于后继已接纳或 Quest 已结束`
        : "等待唯一 NextCycleProposal 或 CandidateCompletion",
      status: transition.status,
    },
    {
      slot: "stage-commit",
      label: "StageCommit",
      owner: "AE",
      state: commit
        ? "done"
        : domainAccepted ? "current" : "pending",
      title: commit
        ? "Reasoning StageCommit 已验证 execution、content 与 domain receipts"
        : "尚无 StageCommit；前三层完成不等于 Stage 已推进",
      status: commit?.status ?? "not_committed",
    },
  ];
}

function reasoningRouteClosure(
  request: ReasoningStageProjection["stage_run_request"],
): string | null {
  const closure = request?.context_pack?.upstream_stage_closure;
  if (!Array.isArray(closure) || !closure.length) return null;
  return closure.map((item) => {
    const stage = typeof item.stage === "string" ? item.stage : "unknown";
    const disposition = typeof item.disposition === "string"
      ? item.disposition
      : "unknown";
    return `${stage}:${disposition}`;
  }).join(" · ");
}

function ReasoningStageCard({
  reasoningStage,
  healthBlocker,
  runtimeControl,
}: {
  reasoningStage: ReasoningStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  runtimeControl: ReactNode;
}) {
  const phase = currentReasoningStageState(reasoningStage);
  const rows = reasoningFactRows(reasoningStage, phase);
  const request = reasoningStage.stage_run_request;
  const run = reasoningStage.run;
  const acceptance = reasoningStage.reasoning_acceptance;
  const transition = reasoningStage.transition;
  const commit = reasoningStage.stage_commit;
  const literature = request?.context_pack?.question_literature_input;
  const planEvidence = request?.context_pack?.plan_evidence_input;
  const targetClosures = request?.context_pack?.accepted_target_commit_closures;

  return (
    <section
      className="lumen-card lumen-idea-card lumen-reasoning-card"
      aria-labelledby="reasoning-stage-title"
      data-testid="reasoning-stage-card"
      data-reasoning-stage-state={phase}
    >
      <header className="lumen-card-head">
        <b id="reasoning-stage-title">综合判断</b>
        <small>交叉检查证据与下一步</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="reasoning-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>综合判断暂时无法继续</b>
            <small>已形成的研究材料保持可见；恢复后从第一个缺口继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      {acceptance.disposition || transition.kind ? (
        <div className="lumen-reasoning-summary">
          <span data-testid="reasoning-outcome">
            <small>当前研究判断</small>
            <b>{acceptance.disposition ? "已经形成可审阅判断" : "等待研究判断"}</b>
          </span>
          <i aria-hidden="true">→</i>
          <span data-testid="reasoning-transition">
            <small>下一项研究动作</small>
            <b>{transition.kind === "CandidateCompletion"
              ? "审阅是否结束当前研究"
              : transition.kind
                ? "继续形成下一个研究问题"
                : "等待下一步"}</b>
          </span>
        </div>
      ) : null}
      <details className="lumen-stage-technical">
        <summary>系统如何核验这段研究</summary>
        <div className="lumen-idea-facts" role="list">
          {rows.map((row) => (
          <article
            key={row.slot}
            className="lumen-idea-fact"
            data-reasoning-slot={row.slot}
            data-state={row.state}
            role="listitem"
          >
            <span className="lumen-idea-fact-mark" aria-hidden="true">
              {row.state === "done" ? "✓" : row.state === "blocked" ? "!" : "→"}
            </span>
            <div><small>{row.label}</small><b>{row.title}</b><code>{row.status}</code></div>
            <span>{row.owner}</span>
          </article>
          ))}
        </div>
      </details>
      <details className="lumen-idea-details">
        <summary>技术身份与核验记录</summary>
        <dl>
          <IdeaDetail label="Cycle" value={reasoningStage.eligibility.cycle_ref} />
          <IdeaDetail label="StageRunRequest" value={request?.request_ref} />
          <IdeaDetail label="StageRunRequest receipt" value={receiptRef(request?.receipt)} />
          <IdeaDetail label="Foreground epoch" value={request?.epoch} />
          <IdeaDetail label="Accepted Question" value={request?.accepted_question_binding?.question_ref} />
          <IdeaDetail label="Question content receipt" value={receiptRef(
            request?.accepted_question_binding?.content_receipt,
          )} />
          <IdeaDetail label="Question identity receipt" value={receiptRef(
            request?.accepted_question_binding?.question_receipt,
          )} />
          <IdeaDetail label="Question literature" value={recordText(
            literature ?? {},
            "kind",
          )} />
          <IdeaDetail label="Question literature revision" value={recordText(
            literature ?? {},
            "revision_ref",
          )} />
          <IdeaDetail label="Upstream route closure" value={reasoningRouteClosure(request)} />
          <IdeaDetail label="Plan evidence" value={recordText(planEvidence ?? {}, "kind")} />
          <IdeaDetail label="Accepted TargetCommit closures" value={
            Array.isArray(targetClosures) ? targetClosures.length : undefined
          } />
          <IdeaDetail label="ContextPack" value={request?.context_pack_ref} />
          <IdeaDetail label="ContextPack hash" value={request?.context_pack_hash} />
          <IdeaDetail label="Run" value={run?.run_ref} />
          <IdeaDetail label="Attempt" value={run?.attempt_ref
            ? `${run.attempt_ref}${run.attempt_generation === undefined ? "" : ` · generation ${run.attempt_generation}`}`
            : null
          } />
          <IdeaDetail label="Root Session" value={run?.root_session_ref} />
          <IdeaDetail label="Native Session" value={run?.native_session_ref} />
          <IdeaDetail label="Execution Fence" value={run?.fence_ref} />
          <IdeaDetail label="Attempt execution receipt" value={receiptRef(run?.attempt_execution_receipt)} />
          <IdeaDetail label="Run completion receipt" value={receiptRef(run?.completion_receipt)} />
          <IdeaDetail label="Child reviewer agent" value={run?.review?.reviewer_agent_ref} />
          <IdeaDetail label="Scientific disposition" value={acceptance.disposition} />
          <IdeaDetail label="Reasoning content" value={recordText(acceptance.content, "content_ref")} />
          <IdeaDetail label="Content receipt" value={receiptRef(
            acceptance.content.receipt ?? acceptance.content,
          )} />
          <IdeaDetail label="Scientific outcome" value={acceptance.outcome_ref} />
          <IdeaDetail label="Domain receipt" value={receiptRef(
            acceptance.domain.receipt ?? acceptance.domain,
          )} />
          <IdeaDetail label="Transition" value={transition.ref} />
          <IdeaDetail label="Transition kind" value={transition.kind} />
          <IdeaDetail label="StageCommit" value={commit?.commit_ref ?? commit?.stage_commit_ref} />
          <IdeaDetail label="StageCommit receipt" value={receiptRef(commit?.receipt)} />
        </dl>
        {runtimeControl}
      </details>
    </section>
  );
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function autonomousCheckpoint(
  reasoningStage: ReasoningStageProjection | null,
): Record<string, unknown> | null {
  return objectValue(reasoningStage?.autonomous_creation_checkpoint);
}

function autonomousStatusCopy(current: AutonomousCreationView | null): string {
  if (!current) return "等待 preliminary science 与自动创建范围完成 Owner 接纳";
  if (current.human_request) return "自动路径已停止；类型化 HumanRequest 保持可见";
  if (current.status === "ready_for_reasoning_resume") {
    return "新 Question 与文献 revision 已接纳；同一 Reasoning Run 可以恢复";
  }
  if (current.content_acceptance.status === "accepted") {
    return "Question 内容已接纳；等待 RG identity 与 RM literature revision";
  }
  if (current.deepfetch.status === "succeeded") {
    return "DeepFetch 已形成快照；正在接纳自动生成的正式 Question";
  }
  if (["queued", "running"].includes(current.deepfetch.status)) {
    return "强制 DeepFetch 正在运行；这条路径没有人工确认或 waiver";
  }
  return "已固定自动创建范围；等待强制 DeepFetch";
}

function completionStatusCopy(current: QuestCompletionView | null): string {
  if (!current) return "CandidateCompletion 只是候选；需要人类确认才可进入领域接纳";
  if (current.status === "ended") return "RG 已接纳完成语义，AE 已正式结束当前 Quest";
  if (current.status === "rejected") return "人类已选择暂不结束；不会形成 RG completion 或 AE ending";
  if (current.status === "stale") return "Goal 或来源已变化；陈旧候选不能结束 Quest";
  if (current.human_confirmation.status === "confirmed") {
    return "人类确认已记录；RG 与 AE 将在各自边界继续核验";
  }
  if (current.human_confirmation.preview?.status === "current") {
    return "当前 Goal、里程碑与候选已冻结，等待你的明确决定";
  }
  return "结束上下文已准备；daemon 正在形成可核验的 Impact Preview";
}

function ReasoningFollowupDock({
  reasoningStage,
  autonomousCreation,
  questCompletion,
  onChanged,
}: {
  reasoningStage: ReasoningStageProjection | null;
  autonomousCreation: AutonomousCreationView | null;
  questCompletion: QuestCompletionView | null;
  onChanged: () => void;
}) {
  const checkpoint = autonomousCheckpoint(reasoningStage);
  const transition = reasoningStage?.transition;
  const completionCandidate = transition?.kind === "CandidateCompletion"
    && typeof transition.ref === "string"
    && typeof reasoningStage?.reasoning_acceptance.outcome_ref === "string";
  const showAutonomous = autonomousCreation !== null || checkpoint !== null;
  const showCompletion = questCompletion !== null || completionCandidate;
  const [busy, setBusy] = useState<"start" | "confirmed" | "rejected" | null>(null);
  const [commandError, setCommandError] = useState<string | null>(null);

  if (!showAutonomous && !showCompletion) return null;

  const beginCompletion = async () => {
    if (
      !completionCandidate
      || typeof transition?.ref !== "string"
      || typeof reasoningStage?.reasoning_acceptance.outcome_ref !== "string"
    ) return;
    setBusy("start");
    setCommandError(null);
    try {
      await startQuestCompletion({
        source_outcome_ref: reasoningStage.reasoning_acceptance.outcome_ref,
        candidate_completion_ref: transition.ref,
      });
      onChanged();
    } catch (caught) {
      setCommandError(
        caught instanceof ProductError
          ? caught.code
          : "quest_completion_start_unavailable",
      );
    } finally {
      setBusy(null);
    }
  };

  const decide = async (decision: "confirmed" | "rejected") => {
    const preview = questCompletion?.human_confirmation.preview;
    if (!questCompletion || !preview || preview.status !== "current") return;
    setBusy(decision);
    setCommandError(null);
    try {
      await decideQuestCompletion(questCompletion.context_ref, {
        preview_ref: preview.ref,
        preview_hash: preview.hash,
        decision,
      });
      onChanged();
    } catch (caught) {
      setCommandError(
        caught instanceof ProductError
          ? caught.code
          : "quest_completion_decision_unavailable",
      );
    } finally {
      setBusy(null);
    }
  };

  const preview = questCompletion?.human_confirmation.preview;
  const decision = questCompletion?.human_confirmation.decision;
  const checkpointStatus = recordText(checkpoint ?? {}, "status") ?? "checkpoint";
  const autonomousQuestion = objectValue(autonomousCreation?.proposal?.question);
  const anchor = objectValue(autonomousCreation?.question_anchor);
  const autonomousRequested = Boolean(autonomousCreation?.deepfetch.request_ref);
  const autonomousFetched = Boolean(
    autonomousCreation?.deepfetch.literature_snapshot_ref,
  );
  const autonomousContentAccepted =
    autonomousCreation?.content_acceptance.status === "accepted";
  const completionConfirmed = decision?.decision === "confirmed";
  const completionDomainAccepted =
    questCompletion?.domain_acceptance.status === "accepted";
  const milestoneBasis = questCompletion?.candidate_completion
    .completion_milestone_basis_refs ?? [];

  return (
    <section
      className="lumen-followup-dock"
      data-testid="reasoning-followup-dock"
      aria-labelledby="reasoning-followup-title"
    >
      <header className="lumen-followup-head">
        <span aria-hidden="true">↗</span>
        <div>
          <small>研究收口后的下一步</small>
          <b id="reasoning-followup-title">下一项工作正在独立形成</b>
        </div>
        <p>新的研究问题与结束当前任务，都保留各自的核验和人工决定。</p>
      </header>

      <div className="lumen-followup-grid">
        {showAutonomous ? (
          <article
            className="lumen-followup-card autonomous"
            data-testid="autonomous-creation-card"
            data-autonomous-creation-state={autonomousCreation?.status ?? checkpointStatus}
          >
            <header>
              <div>
                <small>AUTONOMOUS QUESTION</small>
                <h2>自动形成下一问题</h2>
              </div>
            </header>
            <p>{autonomousStatusCopy(autonomousCreation)}</p>
            <div className="lumen-followup-rule">
              <span aria-hidden="true">✓</span>
              <p><b>会先检索新的研究材料</b><small>不需要额外人工确认</small></p>
            </div>
            <details>
              <summary>查看技术阶段与核验记录</summary>
              <ol className="lumen-boundary-beam" aria-label="自动创建的技术阶段">
                <li data-state={checkpoint ? "done" : "current"}><b>HC</b><small>scope</small></li>
                <li data-state={autonomousRequested ? "done" : "current"}><b>AE</b><small>request</small></li>
                <li data-state={autonomousFetched ? "done" : autonomousRequested ? "current" : "pending"}><b>AR</b><small>DeepFetch</small></li>
                <li data-state={autonomousContentAccepted ? "done" : autonomousFetched ? "current" : "pending"}><b>RM</b><small>content</small></li>
                <li data-state={anchor ? "done" : autonomousContentAccepted ? "current" : "pending"}><b>RG</b><small>identity</small></li>
              </ol>
              <dl>
                <IdeaDetail label="Status" value={autonomousCreation?.status ?? checkpointStatus} />
                <IdeaDetail label="Creation mode" value="AutonomousCreation" />
                <IdeaDetail label="Checkpoint" value={autonomousCreation?.checkpoint.ref ?? recordText(checkpoint ?? {}, "checkpoint_ref")} />
                <IdeaDetail label="Checkpoint hash" value={autonomousCreation?.checkpoint.hash ?? recordText(checkpoint ?? {}, "checkpoint_hash")} />
                <IdeaDetail label="Source outcome" value={recordText(autonomousCreation?.source ?? {}, "scientific_outcome_ref")} />
                <IdeaDetail label="Preliminary science receipt" value={recordText(autonomousCreation?.source ?? {}, "preliminary_scientific_acceptance_receipt_ref")} />
                <IdeaDetail label="Question" value={recordText(autonomousQuestion ?? {}, "title", "unknown_statement")} />
                <IdeaDetail label="DeepFetch request" value={autonomousCreation?.deepfetch.request_ref} />
                <IdeaDetail label="DeepFetch run" value={autonomousCreation?.deepfetch.run_ref} />
                <IdeaDetail label="Literature snapshot" value={autonomousCreation?.deepfetch.literature_snapshot_ref} />
                <IdeaDetail label="Content receipt" value={receiptRef(autonomousCreation?.content_acceptance.receipt)} />
                <IdeaDetail label="Question anchor" value={recordText(anchor ?? {}, "ref", "question_ref")} />
                <IdeaDetail label="Literature revision" value={recordText(autonomousCreation?.literature_revision ?? {}, "revision_ref")} />
              </dl>
            </details>
          </article>
        ) : null}

        {showCompletion ? (
          <article
            className="lumen-followup-card completion"
            data-testid="quest-completion-card"
            data-quest-completion-state={questCompletion?.status ?? "candidate"}
          >
            <header>
              <div>
                <small>CANDIDATE COMPLETION</small>
                <h2>{questCompletion?.status === "ended" ? "Quest 已结束" : "审阅 Quest 结束提案"}</h2>
              </div>
            </header>
            <p>{completionStatusCopy(questCompletion)}</p>
            {!questCompletion && completionCandidate ? (
              <button
                className="lumen-followup-primary"
                type="button"
                disabled={busy !== null}
                onClick={() => void beginCompletion()}
              >
                {busy === "start" ? "正在准备审阅…" : "审阅 Quest 结束提案"}
              </button>
            ) : null}

            {questCompletion && preview?.status === "current" && !decision ? (
              <div className="lumen-completion-decision" role="group" aria-label="Quest 结束决定">
                <div>
                  <small>EXPLICIT HUMAN DECISION</small>
                  <b>当前 preview 已绑定 Goal 与里程碑</b>
                </div>
                <button
                  className="lumen-followup-primary"
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void decide("confirmed")}
                >
                  {busy === "confirmed" ? "正在确认…" : "确认结束 Quest"}
                </button>
                <button
                  className="lumen-followup-secondary"
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void decide("rejected")}
                >
                  {busy === "rejected" ? "正在记录…" : "暂不结束"}
                </button>
              </div>
            ) : null}

            {commandError ? <p className="lumen-followup-error" role="alert">{commandError}</p> : null}
            <details>
              <summary>查看技术阶段与核验记录</summary>
              <ol className="lumen-boundary-beam compact" aria-label="结束研究任务的技术阶段">
                <li data-state={decision ? "done" : preview ? "current" : "pending"}><b>HC</b><small>decision</small></li>
                <li data-state={completionDomainAccepted ? "done" : completionConfirmed ? "current" : "pending"}><b>RG</b><small>semantics</small></li>
                <li data-state={questCompletion?.ending_transition ? "done" : completionDomainAccepted ? "current" : "pending"}><b>AE</b><small>ending</small></li>
              </ol>
              <dl>
                <IdeaDetail label="Status" value={questCompletion?.status ?? "candidate"} />
                <IdeaDetail label="Completion context" value={questCompletion?.context_ref} />
                <IdeaDetail label="Candidate" value={questCompletion?.candidate_completion_ref ?? transition?.ref} />
                <IdeaDetail label="Candidate hash" value={questCompletion?.candidate_completion_hash ?? transition?.hash} />
                <IdeaDetail label="Source outcome" value={recordText(questCompletion?.source ?? {}, "scientific_outcome_ref") ?? reasoningStage?.reasoning_acceptance.outcome_ref} />
                <IdeaDetail label="Goal revision" value={recordText(questCompletion?.goal_revision ?? {}, "goal_revision_ref")} />
                <IdeaDetail label="Goal" value={recordText(questCompletion?.goal_revision ?? {}, "goal")} />
                <IdeaDetail label="Completion criteria" value={recordText(questCompletion?.goal_revision ?? {}, "completion_criteria")} />
                <IdeaDetail label="Milestone basis" value={milestoneBasis.length ? milestoneBasis.join(" · ") : null} />
                <IdeaDetail label="Preview" value={preview?.ref} />
                <IdeaDetail label="Human receipt" value={receiptRef(decision?.receipt)} />
                <IdeaDetail label="RG completion" value={recordText(questCompletion?.domain_acceptance ?? {}, "completion_ref")} />
                <IdeaDetail label="RG receipt" value={receiptRef(questCompletion?.domain_acceptance.receipt)} />
                <IdeaDetail label="AE ending" value={recordText(questCompletion?.ending_transition ?? {}, "transition_ref")} />
                <IdeaDetail label="AE receipt" value={receiptRef(questCompletion?.ending_transition?.receipt)} />
              </dl>
            </details>
          </article>
        ) : null}
      </div>
    </section>
  );
}

function WorkspaceMain({
  snapshot,
  state,
  error,
  streamInterrupted,
  connected,
  latestActivity,
  observedSince,
  hidden = false,
  targetRootObservationPointers,
  humanRequestModalOpen,
  retry,
}: {
  snapshot: PublicSnapshot | null;
  state: ShellState;
  error: string | null;
  streamInterrupted: boolean;
  connected: boolean;
  latestActivity: ResearchActivitySignal | null;
  observedSince: number;
  hidden?: boolean;
  targetRootObservationPointers: Record<string, TargetRootObservationPointer>;
  humanRequestModalOpen: boolean;
  retry: () => void;
}) {
  const unavailable = uniqueCapabilities(snapshot);
  const stageSurface = snapshot?.research_space.status === "active"
    ? currentStageSurface(snapshot)
    : null;
  const ideaStage = stageSurface?.kind === "Idea"
    ? stageSurface.projection
    : null;
  const planStage = stageSurface?.kind === "Plan"
    ? stageSurface.projection
    : null;
  const bundleStage = stageSurface?.kind === "Bundle"
    ? stageSurface.projection
    : null;
  const reasoningStage = stageSurface?.kind === "Reasoning"
    ? stageSurface.projection
    : null;
  const ideaHealthBlocker = ideaStageHealthBlocker(snapshot);
  const planHealthBlocker = planStageHealthBlocker(snapshot);
  const bundleHealthBlocker = bundleStageHealthBlocker(snapshot);
  const reasoningHealthBlocker = reasoningStageHealthBlocker(snapshot);
  const runtimeControl = snapshot ? (
    <TelemetryAuthorizationCard
      collaboration={snapshot.human_collaboration}
      onChanged={retry}
    />
  ) : null;
  const question = reasoningStage
    ? reasoningQuestion(reasoningStage, snapshot ?? undefined)
    : bundleStage
    ? bundleQuestion(bundleStage, snapshot ?? undefined)
    : planStage
    ? planQuestion(planStage, snapshot ?? undefined)
    : ideaStage
      ? ideaQuestion(ideaStage, snapshot ?? undefined)
      : null;
  return (
    <main
      id={hidden ? undefined : "main-content"}
      className="lumen-main"
      data-shell-region="main"
      hidden={hidden}
      tabIndex={hidden ? -1 : 0}
      aria-labelledby="workspace-title"
      aria-busy={state === "loading"}
    >
      {(error || streamInterrupted) && snapshot ? (
        <div className="lumen-reconnect-warning" role="alert">
          <span aria-hidden="true">↺</span>
          <p>
            <b>研究活动连接中断，正在重连。</b>
            <small>页面继续显示最后一次确认的研究状态 · {snapshot.revision}</small>
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
        {snapshot?.research_space.status === "active" && !hidden ? (
          <ResearchTracePanel
            snapshot={snapshot}
            latestActivity={latestActivity}
            observedSince={observedSince}
            connected={connected}
          />
        ) : null}
      </section>

      <div className="lumen-lower">
        {reasoningStage ? (
          <CurrentQuestionCard
            stage="Reasoning"
            question={question!}
          />
        ) : bundleStage ? (
          <CurrentQuestionCard
            stage="Bundle"
            question={question!}
          />
        ) : planStage ? (
          <CurrentQuestionCard
            stage="Plan"
            question={question!}
          />
        ) : ideaStage ? (
          <CurrentQuestionCard
            stage="Idea"
            question={question!}
          />
        ) : (
          <section className="lumen-card lumen-next-card" aria-labelledby="next-title">
            <header className="lumen-card-head">
              <b id="next-title">当前空间</b>
              <small>{snapshot ? `状态 ${snapshot.revision}` : "等待研究状态"}</small>
            </header>
            <div className="lumen-path" aria-hidden="true">
              <span className="origin">MR</span>
              <i />
              <span className="destination">＋</span>
            </div>
            <h2>{state === "ready-empty" ? "第一个研究任务从左侧入口开始" : "这里只显示已经确认的研究事实"}</h2>
            <p>单纯浏览不会改变研究；创建、授权与接纳都需要明确操作。</p>
          </section>
        )}

        {reasoningStage ? (
          <ReasoningStageCard
            reasoningStage={reasoningStage}
            healthBlocker={reasoningHealthBlocker}
            runtimeControl={runtimeControl}
          />
        ) : bundleStage ? (
          <BundleStageCard
            bundleStage={bundleStage}
            healthBlocker={bundleHealthBlocker}
            observationPointers={targetRootObservationPointers}
            humanRequestModalOpen={humanRequestModalOpen}
            activityPaused={hidden}
            runtimeControl={runtimeControl}
          />
        ) : planStage ? (
          <PlanStageCard
            planStage={planStage}
            healthBlocker={planHealthBlocker}
            runtimeControl={runtimeControl}
          />
        ) : ideaStage ? (
          <IdeaStageCard
            ideaStage={ideaStage}
            healthBlocker={ideaHealthBlocker}
            runtimeControl={runtimeControl}
          />
        ) : (
          <section className="lumen-card lumen-availability" aria-labelledby="availability-title">
            <header className="lumen-card-head">
              <b id="availability-title">能力可用性</b>
              <small>当前研究服务</small>
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
                {snapshot ? "当前没有报告不可用的研究能力。" : "研究状态返回后显示。"}
              </p>
            )}
            {snapshot ? (
              <details className="lumen-technical-details">
                <summary>查看运行详情</summary>
                <dl>
                  <div><dt>版本</dt><dd>{snapshot.product.version}</dd></div>
                  <div><dt>Readiness</dt><dd>{snapshot.readiness.status}</dd></div>
                  {snapshot.runtime_observability?.inhibitor ? (
                    <>
                      <div>
                        <dt>Power inhibitor</dt>
                        <dd>
                          {snapshot.runtime_observability.inhibitor.backend} · {snapshot.runtime_observability.inhibitor.status} · {snapshot.runtime_observability.inhibitor.scope}
                        </dd>
                      </div>
                      <div>
                        <dt>Inhibitor reason</dt>
                        <dd>{runtimeTypedReason(snapshot.runtime_observability.inhibitor.reason)}</dd>
                      </div>
                      <div>
                        <dt>执行责任</dt>
                        <dd>{runtimeResponsibilitySummary(
                          snapshot.runtime_observability.responsibilities,
                          snapshot.runtime_observability.inhibitor.active_count,
                        )}</dd>
                      </div>
                      <div>
                        <dt>Durable waiting</dt>
                        <dd>{runtimeDurableWaitingSummary(
                          snapshot.runtime_observability.durable_waiting,
                          snapshot.runtime_observability.durable_waiting_count,
                          snapshot.runtime_observability.durable_waiting_page_truncated,
                        )}</dd>
                      </div>
                      <div>
                        <dt>中断 / Reconciliation</dt>
                        <dd>{runtimeInterruptionSummary(
                          snapshot.runtime_observability.interruptions,
                          snapshot.runtime_observability.interruption_count,
                          snapshot.runtime_observability.interruption_page_truncated,
                        )}</dd>
                      </div>
                      <div>
                        <dt>本地日志</dt>
                        <dd>{runtimeLogSummary(snapshot.runtime_observability.log)}</dd>
                      </div>
                      <div>
                        <dt>Telemetry</dt>
                        <dd>
                          {snapshot.runtime_observability.telemetry?.mode === "active" ? "opt-in" : "local-only"} · {snapshot.runtime_observability.telemetry?.mode ?? "unavailable"}
                        </dd>
                      </div>
                    </>
                  ) : snapshot.runtime_observability?.status === "unavailable" ? (
                    <div><dt>Runtime protection</dt><dd>unavailable</dd></div>
                  ) : null}
                  <div>
                    <dt>Semantic MCP</dt>
                    <dd>
                      {snapshot.harnesses.status} · {snapshot.harnesses.gateway?.transport ?? snapshot.harnesses.reason?.code ?? "gateway_unavailable"}
                    </dd>
                  </div>
                  {snapshot.harnesses.adapters.map((adapter) => (
                    <div key={`harness:${adapter.harness_family}`}>
                      <dt>{adapter.harness_family} Harness</dt>
                      <dd>
                        lock {adapter.locked_version} · {adapter.status} · {adapter.capability_profile
                          ? `profile ${adapter.capability_profile.status}`
                          : `capability_unavailable · ${adapter.missing_reason?.code ?? adapter.reason?.code ?? "reason_unavailable"}`}
                        {adapter.provider_operation
                          ? ` · operation ${adapter.provider_operation.status}${adapter.provider_operation.outcome_code ? `/${adapter.provider_operation.outcome_code}` : ""}`
                          : ""}
                      </dd>
                    </div>
                  ))}
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
                {runtimeControl}
              </details>
            ) : null}
          </section>
        )}
      </div>
      {snapshot ? (
        <ReasoningFollowupDock
          reasoningStage={snapshot.reasoning_stage ?? null}
          autonomousCreation={snapshot.autonomous_creation.current}
          questCompletion={snapshot.quest_completion.current}
          onChanged={retry}
        />
      ) : null}
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
    ) || initialParameters.get("view") === "questions"
      || ["evidence", "history"].includes(initialParameters.get("inspector") ?? ""),
  );
  const [questionRouteNodeRef, setQuestionRouteNodeRef] = useState<string | null>(
    () => initialParameters.get("node"),
  );
  const [questCompletionLanding, setQuestCompletionLanding] = useState<
    QuestCompletionHandoff | null
  >(null);
  const [questionInspectorMode, setQuestionInspectorMode] = useState<
    QuestionInspectorMode
  >(() => {
    const mode = initialParameters.get("inspector");
    return mode === "evidence" || mode === "history" ? mode : null;
  });
  const [selectedQuestionContext, setSelectedQuestionContext] = useState<
    QuestionTreeItem | null
  >(null);
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
  const [latestResearchActivity, setLatestResearchActivity] = useState<
    ResearchActivitySignal | null
  >(null);
  const [targetRootObservationPointers, setTargetRootObservationPointers] =
    useState<Record<string, TargetRootObservationPointer>>({});
  const [snapshotRetrySequence, setSnapshotRetrySequence] = useState(0);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);
  const observedSinceRef = useRef(Date.now());
  const streamCursorRef = useRef<number | null>(null);
  const manualDetailSequence = useRef(0);
  const questionTreeButtonRef = useRef<HTMLButtonElement>(null);
  const historyButtonRef = useRef<HTMLButtonElement>(null);
  const questionTreeReturnFocusRef = useRef<HTMLButtonElement | null>(null);
  const completedHandoffInitializationRef = useRef<string | null>(null);
  const writingButtonRef = useRef<HTMLButtonElement>(null);
  const humanRequestReturnFocusRef = useRef<HTMLElement | null>(null);
  const humanRequestReturnUrlRef = useRef<string | null>(null);
  const prepareHumanRequestReturn = useCallback(() => {
    if (humanRequestReturnUrlRef.current !== null) return;
    const panel = new URLSearchParams(window.location.search).get("panel");
    if (isHumanRequestPanel(panel)) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement) humanRequestReturnFocusRef.current = active;
    humanRequestReturnUrlRef.current =
      `${window.location.pathname}${window.location.search}${window.location.hash}`;
  }, []);
  const questionTreeItems = useStableQuestionTreeItems(
    snapshot?.question_tree.items ?? [],
    snapshot?.owners.research_graph?.revision ?? null,
  );
  const currentOpenRequests = useMemo(
    () => currentOpenHumanRequests(snapshot),
    [snapshot?.human_collaboration?.human_requests],
  );
  const humanRequestSurfaceOpen = humanRequestsOpen || currentOpenRequests.length > 0;

  const handleConnection = useCallback((next: boolean) => {
    setConnected(next);
    if (next) {
      setStreamInterrupted(false);
    } else {
      setStreamInterrupted(true);
    }
  }, []);

  const handleResearchActivity = useCallback((activity: {
    event_type: string;
    revision: number;
    observed_at: number;
  }) => {
    if (!/^(agent_runtime|research_memory|research_graph|advancement_engine)\./.test(
      activity.event_type,
    )) return;
    setLatestResearchActivity((current) => (
      current && current.revision > activity.revision
        ? current
        : {
            eventType: activity.event_type,
            revision: activity.revision,
            observedAt: activity.observed_at,
          }
    ));
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
      (pointer) => setTargetRootObservationPointers((current) => ({
        ...current,
        [pointer.target_ref]: pointer,
      })),
      handleResearchActivity,
    );
    // followProjection advances its own monotonic cursor. Reconnecting this
    // long-lived stream for every Snapshot revision can briefly occupy every
    // browser connection slot and starve an Owner command.
  }, [handleConnection, handleResearchActivity, reload, streamReady]);

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
    if (
      !humanRequestsOpen
      || humanRequestRouteKind !== null
      || currentOpenRequests.length === 0
      || currentOpenRequests.some(
        (item) => item.request_ref === selectedHumanRequestRef,
      )
    ) return;
    const nextRequest = currentOpenRequests[0];
    setSelectedHumanRequestRef(nextRequest.request_ref);
    window.history.replaceState(
      null,
      "",
      `/?panel=${humanRequestPanelByKind[nextRequest.kind]}`,
    );
  }, [
    currentOpenRequests,
    humanRequestRouteKind,
    humanRequestsOpen,
    selectedHumanRequestRef,
  ]);

  useEffect(() => {
    // A current formal request takes the top layer, including a local wait.
    // Other workflow dialogs remain mounted beneath it so their drafts survive.
    if (humanRequestsOpen) return;
    const currentRequest = currentOpenRequests[0];
    if (!currentRequest) return;
    prepareHumanRequestReturn();
    setSelectedHumanRequestRef(currentRequest.request_ref);
    setHumanRequestRouteKind(null);
    window.history.replaceState(
      null,
      "",
      `/?panel=${humanRequestPanelByKind[currentRequest.kind]}`,
    );
    setHumanRequestsOpen(true);
  }, [
    currentOpenRequests,
    humanRequestsOpen,
    prepareHumanRequestReturn,
  ]);

  const state = shellState(snapshot, error);
  const canCreate = questCreationReady(snapshot);
  const canBrowseAssets = snapshot?.research_assets.status === "ready";
  const canBrowseQuestions = snapshot?.question_tree.status === "ready";
  const projectedCurrentQuestionRef = snapshot?.research_space.current_question
    ?.question_ref;
  const scopedQuestRef = currentQuestRef(snapshot);
  const scopedHistoryQuestions = questionTreeItems.filter(
    (item) => !scopedQuestRef || item.quest_ref === scopedQuestRef,
  ) ?? [];
  const historyQuestionRef = scopedHistoryQuestions.find(
    (item) => item.question_ref === projectedCurrentQuestionRef,
  )?.question_ref ?? scopedHistoryQuestions[0]?.question_ref ?? null;
  const canBrowseHistory = Boolean(canBrowseQuestions && historyQuestionRef);
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
  const openOverview = () => {
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setQuestionTreeOpen(false);
    setQuestCompletionLanding(null);
    setManualPanel(null);
    setManualOpenError(null);
    setQuestionInspectorMode(null);
    setQuestionRouteNodeRef(null);
    setSelectedQuestionContext(null);
    setPendingDirectManualParentRef(null);
    if (currentOpenRequests.length === 0) {
      setHumanRequestsOpen(false);
      setHumanRequestRouteKind(null);
      setSelectedHumanRequestRef(null);
    }
    window.history.replaceState(null, "", "/");
  };
  const openCreation = () => {
    if (!canCreate) return;
    const currentCreation = restorableQuestCreation(
      snapshot?.quest_creation.current ?? null,
      completedHandoffInitializationRef.current,
    );
    setQuestCompletionLanding(null);
    setQuestionTreeOpen(false);
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", "/?panel=create-quest");
    setCreationMode(currentCreation ? "current" : "new");
  };
  const closeCreation = () => {
    window.history.replaceState(null, "", "/");
    setCreationMode(null);
  };
  const completeCreation = useCallback((handoff: QuestCompletionHandoff) => {
    if (completedHandoffInitializationRef.current === handoff.initializationId) return;
    completedHandoffInitializationRef.current = handoff.initializationId;
    setSnapshot((current) => {
      if (
        !current
        || current.quest_creation.current?.initialization_id !== handoff.initializationId
      ) return current;
      return {
        ...current,
        quest_creation: { ...current.quest_creation, current: null },
      };
    });
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setManualOpenError(null);
    setQuestionInspectorMode(null);
    setQuestionRouteNodeRef(handoff.questionRef);
    setSelectedQuestionContext(null);
    setPendingDirectManualParentRef(null);
    setQuestCompletionLanding(handoff);
    window.history.replaceState(null, "", questionTreeUrl(handoff.questionRef));
    setCreationMode(null);
    setQuestionTreeOpen(true);
    void reload();
  }, [reload]);
  const openAssets = () => {
    if (!canBrowseAssets) return;
    setQuestCompletionLanding(null);
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
    setQuestCompletionLanding(null);
    questionTreeReturnFocusRef.current = questionTreeButtonRef.current;
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setManualOpenError(null);
    setQuestionInspectorMode(null);
    setQuestionRouteNodeRef(null);
    setSelectedQuestionContext(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", questionTreeUrl());
    setQuestionTreeOpen(true);
  };
  const closeQuestionTree = () => {
    window.history.replaceState(null, "", "/");
    setQuestionTreeOpen(false);
    startTransition(() => {
      setQuestCompletionLanding(null);
      setManualPanel(null);
      setQuestionInspectorMode(null);
      setQuestionRouteNodeRef(null);
      setSelectedQuestionContext(null);
      setPendingDirectManualParentRef(null);
      setManualOpenError(null);
    });
    requestAnimationFrame(() => {
      (questionTreeReturnFocusRef.current ?? questionTreeButtonRef.current)
        ?.focus({ preventScroll: true });
      questionTreeReturnFocusRef.current = null;
    });
  };

  const openQuestionHistory = () => {
    if (!canBrowseHistory || !historyQuestionRef) return;
    setQuestCompletionLanding(null);
    questionTreeReturnFocusRef.current = historyButtonRef.current;
    setCreationMode(null);
    setAssetsOpen(false);
    setWritingOpen(false);
    setManualPanel(null);
    setManualOpenError(null);
    setQuestionRouteNodeRef(historyQuestionRef);
    setSelectedQuestionContext(null);
    setQuestionInspectorMode("history");
    setPendingDirectManualParentRef(null);
    window.history.replaceState(
      null,
      "",
      questionTreeUrl(historyQuestionRef, "history"),
    );
    setQuestionTreeOpen(true);
  };

  const completionLandingProjected = Boolean(
    questCompletionLanding
    && snapshot?.question_tree.items.some(
      (item) => item.question_ref === questCompletionLanding.questionRef,
    ),
  );
  const selectQuestionTreeContext = useCallback((question: QuestionTreeItem | null) => {
    // Completion may lead the next full Snapshot. Keep the exact handoff route
    // while the mounted old tree reports its automatic fallback selection.
    if (
      questCompletionLanding
      && !completionLandingProjected
      && question?.question_ref !== questCompletionLanding.questionRef
    ) return;
    setQuestCompletionLanding((current) => (
      current && question && current.questionRef !== question.question_ref
        ? null
        : current
    ));
    setSelectedQuestionContext((current) => (
      current?.question_ref === question?.question_ref
        ? current
        : question
    ));
    setQuestionRouteNodeRef((current) => {
      const next = question?.question_ref ?? null;
      return current === next ? current : next;
    });
    if (question && !manualPanel) {
      window.history.replaceState(
        null,
        "",
        questionTreeUrl(question.question_ref, questionInspectorMode),
      );
    }
  }, [
    completionLandingProjected,
    manualPanel,
    questCompletionLanding,
    questionInspectorMode,
  ]);

  const discussQuestionWithCompanion = useCallback((
    question: QuestionTreeItem,
    _opener: HTMLButtonElement,
  ) => {
    setSelectedQuestionContext(question);
    setQuestionRouteNodeRef(question.question_ref);
    window.history.replaceState(
      null,
      "",
      questionTreeUrl(question.question_ref, questionInspectorMode),
    );
    requestAnimationFrame(() => {
      document.querySelector<HTMLTextAreaElement>(
        "[aria-label='给研究助手发消息']",
      )?.focus({ preventScroll: false });
    });
  }, [questionInspectorMode]);

  const changeQuestionInspectorMode = useCallback((
    mode: Exclude<QuestionInspectorMode, null> | null,
  ) => {
    setQuestionInspectorMode(mode);
    window.history.replaceState(
      null,
      "",
      questionTreeUrl(questionRouteNodeRef, mode),
    );
  }, [questionRouteNodeRef]);

  const openWriting = () => {
    if (!canBrowseWriting || !snapshot) return;
    setQuestCompletionLanding(null);
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

  const controlQuestionLifecycle = async (
    action: Extract<ResearchControlAction, "prune">,
    question: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => {
    const foreground = snapshot?.research_control.foreground;
    if (
      snapshot?.research_control.status !== "ready"
      || !foreground
      || question.quest_ref !== foreground.quest_ref
    ) {
      setManualOpenError("research_control_foreground_unavailable");
      return;
    }
    setManualOpenError(null);
    try {
      await createHumanCommand(`quest:${foreground.quest_ref}`, {
        command_kind: "research_control",
        payload: {
          action,
          target: {
            quest_ref: foreground.quest_ref,
            cycle_ref: foreground.cycle_ref,
            question_ref: foreground.question_ref,
            epoch: foreground.epoch,
            target_question_ref: question.question_ref,
          },
          reason: "operator_requested",
        },
      });
      await reload();
    } catch (caught) {
      setManualOpenError(
        caught instanceof ProductError ? caught.code : "research_control_failed",
      );
    } finally {
      requestAnimationFrame(() => opener.focus({ preventScroll: true }));
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
    if (!humanRequestsOpen && humanRequestReturnUrlRef.current === null) {
      if (active instanceof HTMLElement) humanRequestReturnFocusRef.current = active;
      humanRequestReturnUrlRef.current =
        `${window.location.pathname}${window.location.search}${window.location.hash}`;
    }
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
    const currentRequest = currentOpenRequests.find(
      (item) => item.request_ref === selectedHumanRequestRef,
    ) ?? currentOpenRequests[0];
    if (currentRequest) {
      setSelectedHumanRequestRef(currentRequest.request_ref);
      window.history.replaceState(
        null,
        "",
        `/?panel=${humanRequestPanelByKind[currentRequest.kind]}`,
      );
      return;
    }
    const returnFocus = humanRequestReturnFocusRef.current;
    const returnUrl = humanRequestReturnUrlRef.current ?? "/";
    setHumanRequestsOpen(false);
    setHumanRequestRouteKind(null);
    setSelectedHumanRequestRef(null);
    window.history.replaceState(null, "", returnUrl);
    humanRequestReturnFocusRef.current = null;
    humanRequestReturnUrlRef.current = null;
    window.requestAnimationFrame(() => {
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      else document.querySelector<HTMLButtonElement>("[aria-label='需要你']")?.focus();
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
            <small>{snapshot?.research_space.status === "active" ? "当前研究" : "新的研究空间"}</small>
            <b>{snapshot?.research_space.status === "active" ? "根 Agent 研究现场" : "等待第一个研究问题"}</b>
          </div>
          <div className={`lumen-connection ${connected ? "connected" : ""}`} aria-live="polite">
            <i aria-hidden="true" />
            <span>
              {error || streamInterrupted ? "研究活动正在重连" : connected ? "持续接收真实活动" : snapshot ? "正在连接研究活动" : "读取研究状态"}
            </span>
            {snapshot ? <code>状态 {snapshot.revision}</code> : null}
          </div>
        </header>
        <LumenRail
          canCreate={Boolean(canCreate)}
          canBrowseAssets={canBrowseAssets}
          canBrowseQuestions={canBrowseQuestions}
          questionsActive={questionTreeOpen}
          canBrowseHistory={canBrowseHistory && manualPanel === null}
          historyActive={questionTreeOpen && questionInspectorMode === "history"}
          canBrowseWriting={Boolean(canBrowseWriting)}
          writingOpen={writingOpen}
          questionUnavailableReason={questionUnavailableReason}
          questionButtonRef={questionTreeButtonRef}
          historyButtonRef={historyButtonRef}
          writingButtonRef={writingButtonRef}
          onBrowseQuestions={openQuestionTree}
          onBrowseHistory={openQuestionHistory}
          canBrowseHumanRequests={canBrowseHumanRequests}
          humanRequestCount={humanRequestCount}
          humanRequestsOpen={humanRequestSurfaceOpen}
          onCreate={openCreation}
          onBrowseAssets={openAssets}
          onBrowseWriting={openWriting}
          onBrowseHumanRequests={() => openHumanRequests()}
          onOverview={openOverview}
        />
        <WorkspaceMain
          snapshot={snapshot}
          state={state}
          error={error}
          streamInterrupted={streamInterrupted}
          connected={connected}
          latestActivity={latestResearchActivity}
          observedSince={observedSinceRef.current}
          hidden={Boolean(questionTreeOpen && snapshot)}
          targetRootObservationPointers={targetRootObservationPointers}
          humanRequestModalOpen={humanRequestSurfaceOpen}
          retry={() => void reload()}
        />
        {questionTreeOpen && snapshot ? (
          <QuestionTree
            items={questionTreeItems}
            graphRevision={snapshot.owners.research_graph?.revision ?? null}
            projectionStatus={snapshot.question_tree.status}
            projectionReason={snapshot.question_tree.reason?.code ?? null}
            initialQuestionRef={questionRouteNodeRef}
            completionLanding={questCompletionLanding}
            initialInspectorMode={questionInspectorMode}
            onInspectorModeChange={changeQuestionInspectorMode}
            manualCreationReady={Boolean(
              manualCreationReady && snapshot.question_tree.status === "ready",
            )}
            controlsInert={manualPanel !== null}
            openingParentRef={manualOpeningParentRef}
            openError={manualOpenError}
            onClose={closeQuestionTree}
            onSelectionChange={selectQuestionTreeContext}
            onDiscussQuestion={discussQuestionWithCompanion}
            onCreateQuestion={openManualCreation}
            onControlQuestion={controlQuestionLifecycle}
          />
        ) : null}
        <QuestCompanion
          state={state}
          collaboration={snapshot?.human_collaboration}
          researchControl={manualPanel ? undefined : snapshot?.research_control}
          questions={questionTreeItems}
          questionContext={questionTreeOpen && manualPanel === null
            ? selectedQuestionContext
            : null}
          onChanged={() => void reload()}
          onOpenRequest={(requestRef) => openHumanRequests(requestRef)}
        />
      </div>
      {creationMode && snapshot ? (
        <QuestCreationWorkbench
          current={restorableQuestCreation(
            snapshot.quest_creation.current,
            completedHandoffInitializationRef.current,
          )}
          researchAssets={snapshot.research_assets.items}
          onClose={closeCreation}
          onCompleted={completeCreation}
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
      <HumanRequestSurface
        open={humanRequestSurfaceOpen}
        blocking={currentOpenRequests.length > 0}
        selectedRef={selectedHumanRequestRef}
        collaboration={snapshot?.human_collaboration}
        onSelect={selectHumanRequest}
        onBeforeOpen={prepareHumanRequestReturn}
        onClose={closeHumanRequests}
        onChanged={() => void reload()}
      />
    </>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
