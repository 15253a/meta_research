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
import { StatusHome } from "./StatusHome";
import { OutputLanguageProvider, OutputLanguageControl } from "./OutputLanguage";
import { observedActiveTarget } from "./activeTargetStatus";
import { targetResearchFacts } from "./targetResearchFacts";
import { BoundedDetails, PageWindow } from "./BoundedDetails";
import { ResearchIcon, SpectrumStages, spectrumStage } from "./Spectrum";
import { conversationContext, useWorkspaceStatus } from "./workspaceStatus";
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
  fetchStageRawOutput,
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
  type StageRawOutputPage,
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
  ForegroundResearchControlShortcut,
  HumanRequestSurface,
  QuestCompanion,
  TelemetryAuthorizationCard,
} from "./HumanCollaboration";
import "./shell.css";
import { ResearchOverview, ResearchTimeline, StageHistoryStrip, cycleOrdinalLabel, overviewQuestRef, useResearchOverview } from "./ResearchOverview";
import { StageReadableOutput, TargetCommandOutput } from "./ReadableOutput";
import { ExperimentLogs, ExperimentOutputViews } from "./ExperimentLogs";
import { ExecutionElapsed, type ExecutionClockSample } from "./ExecutionElapsed";
import { RootConversations, StageRootSessions, useRootConversations } from "./RootConversations";
import "./research-interaction.css";
import "./spectrum-workspace.css";

const capabilityLabels: Record<string, string> = {
  accepted_material_basis: "研究资料",
  first_question_deepfetch: "文献检索",
  quest_creation: "创建研究任务",
  quest_companion: "研究助手",
  stage_execution: "研究执行",
  writing: "报告写作",
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
  system_operation_help: "system-operation-help",
};

const humanRequestPresentedPrefix = "meta_research_human_request_presented:";
const humanRequestPresentedFallback = new Set<string>();

function humanRequestPresentationKey(request: HumanRequestItem): string {
  return `${humanRequestPresentedPrefix}${request.request_ref}:${request.revision}`;
}

function humanRequestWasPresented(request: HumanRequestItem): boolean {
  const key = humanRequestPresentationKey(request);
  if (humanRequestPresentedFallback.has(key)) return true;
  try {
    return window.sessionStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function markHumanRequestPresented(request: HumanRequestItem): void {
  const key = humanRequestPresentationKey(request);
  humanRequestPresentedFallback.add(key);
  try {
    window.sessionStorage.setItem(key, "1");
  } catch {
    // An unavailable session store must not make the request impossible to dismiss.
  }
}

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
  caption = label,
  active = false,
  unavailable = false,
  unavailableReason = "capability_unavailable",
  buttonRef,
  attentionCount = 0,
  onClick,
}: {
  label: string;
  glyph: string;
  caption?: string;
  active?: boolean;
  unavailable?: boolean;
  unavailableReason?: string;
  buttonRef?: Ref<HTMLButtonElement>;
  attentionCount?: number;
  onClick?: () => void;
}) {
  return (
    <button
      ref={buttonRef}
      type="button"
      className={active ? "lumen-rail-button active" : "lumen-rail-button"}
      aria-label={label}
      aria-current={active ? "page" : undefined}
      title={unavailable
        ? `${label} · ${unavailableReason}`
        : attentionCount > 0
          ? `${label} · ${attentionCount} 项待处理`
          : label}
      disabled={unavailable}
      onClick={onClick}
    >
      <ResearchIcon glyph={glyph} />
      <small aria-hidden="true">{caption}</small>
      {attentionCount > 0 ? <i aria-label={`${attentionCount} 项待处理`}>{attentionCount}</i> : null}
    </button>
  );
}

function LumenRail({
  activeSection,
  canCreate,
  canBrowseAssets,
  canBrowseQuestions,
  canBrowseHistory,
  canBrowseWriting,
  questionUnavailableReason,
  questionButtonRef,
  historyButtonRef,
  writingButtonRef,
  onBrowseQuestions,
  onBrowseHistory,
  canBrowseHumanRequests,
  humanRequestCount,
  onCreate,
  onBrowseAssets,
  onBrowseWriting,
  onBrowseHumanRequests,
  onOverview,
}: {
  activeSection: "overview" | "questions" | "assets" | "writing" | "history" | "requests" | "creation";
  canCreate: boolean;
  canBrowseAssets: boolean;
  canBrowseQuestions: boolean;
  canBrowseHistory: boolean;
  canBrowseWriting: boolean;
  questionUnavailableReason: string;
  questionButtonRef: Ref<HTMLButtonElement>;
  historyButtonRef: Ref<HTMLButtonElement>;
  writingButtonRef: Ref<HTMLButtonElement>;
  canBrowseHumanRequests: boolean;
  humanRequestCount: number;
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
        label="研究总览"
        caption="总览"
        glyph="⌂"
        active={activeSection === "overview"}
        onClick={onOverview}
      />
      <RailButton
        label="问题树"
        glyph="树"
        active={activeSection === "questions"}
        unavailable={!canBrowseQuestions}
        unavailableReason={questionUnavailableReason}
        buttonRef={questionButtonRef}
        onClick={onBrowseQuestions}
      />
      <RailButton
        label="研究资料"
        active={activeSection === "assets"}
        glyph="▤"
        unavailable={!canBrowseAssets}
        onClick={onBrowseAssets}
      />
      <RailButton
        label="写作"
        glyph="✎"
        active={activeSection === "writing"}
        unavailable={!canBrowseWriting}
        buttonRef={writingButtonRef}
        onClick={onBrowseWriting}
      />
      <RailButton
        label="历史"
        glyph="↺"
        active={activeSection === "history"}
        unavailable={!canBrowseHistory}
        unavailableReason="当前 Quest 没有可下钻的已接纳 Question"
        buttonRef={historyButtonRef}
        onClick={onBrowseHistory}
      />
      <RailButton
        label="需要你"
        glyph="!"
        active={activeSection === "requests"}
        unavailable={!canBrowseHumanRequests}
        attentionCount={humanRequestCount}
        onClick={onBrowseHumanRequests}
      />
      <RailButton
        label="创建研究任务"
        active={activeSection === "creation"}
        caption="新建任务"
        glyph="＋"
        unavailable={!canCreate}
        onClick={onCreate}
      />
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
  skippedByCurrentStage: boolean,
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
    skippedByCurrentStage
    || projection.typed_skip?.status === "skipped"
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
    skipped: "本轮明确跳过",
    recorded: "已有运行记录",
    "not-entered": "本轮尚未进入",
    "no-record": "本轮没有记录",
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
  idea_stage: { lane: "stage", label: "研究思路" },
  plan_stage: { lane: "stage", label: "验证计划" },
  bundle_stage: { lane: "bundle", label: "实验策略" },
  reasoning_stage: { lane: "stage", label: "研究判断" },
  deepfetch: { lane: "acquisition", label: "文献检索" },
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

const stageRawOutputPageSize = 64 * 1024;
const stageRawOutputPollMilliseconds = 750;

function validateStageRawOutputPage(
  page: StageRawOutputPage,
  runRef: string,
  expectedOffset: number,
): void {
  const textBytes = typeof page.text === "string"
    ? new TextEncoder().encode(page.text).byteLength
    : -1;
  if (
    page.schema_ref !== "meta-research/stage-raw-output-page/v1"
    || page.run_ref !== runRef
    || typeof page.run_kind !== "string"
    || page.run_kind.length === 0
    || typeof page.attempt_ref !== "string"
    || page.attempt_ref.length === 0
    || !Number.isSafeInteger(page.attempt_generation)
    || page.attempt_generation < 1
    || typeof page.root_session_ref !== "string"
    || page.root_session_ref.length === 0
    || typeof page.fence_ref !== "string"
    || page.fence_ref.length === 0
    || (page.operation_ref !== null && !page.operation_ref)
    || (page.phase !== null && !page.phase)
    || (page.native_session_ref !== null && !page.native_session_ref)
    || (
      page.transport_invocation_hash !== null
      && !/^[0-9a-f]{64}$/.test(page.transport_invocation_hash)
    )
    || typeof page.stream_ref !== "string"
    || !page.stream_ref.startsWith("stage-raw-output:")
    || !["live", "waiting", "replaced", "terminal"].includes(page.status)
    || typeof page.text !== "string"
    || !Number.isSafeInteger(page.offset)
    || page.offset !== expectedOffset
    || !Number.isSafeInteger(page.next_offset)
    || page.next_offset < page.offset
    || !Number.isSafeInteger(page.source_bytes)
    || page.source_bytes < page.next_offset
    || page.next_offset - page.offset !== textBytes
    || page.has_more !== (page.next_offset < page.source_bytes)
    || page.source_caught_up !== !page.has_more
    || page.exact !== true
    || page.unredacted !== true
  ) {
    throw new ProductError("stage_raw_output_identity_invalid");
  }
}

type OutputChunk = { offset: number; text: string };
function collectOutputPages(chunks: OutputChunk[], next: {offset: number; text: string}, reset: boolean): OutputChunk[] {
  const result = reset ? [next] : [...chunks.filter(chunk => chunk.offset < next.offset), next];
  let bytes = result.reduce((total, chunk) => total + new TextEncoder().encode(chunk.text).length, 0);
  while (bytes > 8 * 1024 * 1024 && result.length > 1) {
    bytes -= new TextEncoder().encode(result.shift()!.text).length;
  }
  return result;
}

function StageRawOutputFeed({ item }: { item: ResearchActivityItem }) {
  const [selectedPhase, setSelectedPhase] = useState<"current" | "primary" | "review">("current");
  const [followLive, setFollowLive] = useState(true);
  const expanded = true;
  const [chunks, setChunks] = useState<OutputChunk[]>([]);
  const [terminal, setTerminal] = useState<StageRawOutputPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previousOffsets, setPreviousOffsets] = useState<number[]>([]);
  const [retryEpoch, setRetryEpoch] = useState(0);
  const [executionClock, setExecutionClock] = useState<ExecutionClockSample | null>(null);
  const [previousOutput, setPreviousOutput] = useState<{ page: StageRawOutputPage; chunks: OutputChunk[] } | null>(null);
  const [documentVisible, setDocumentVisible] = useState(
    () => document.visibilityState !== "hidden",
  );
  const terminalRef = useRef<StageRawOutputPage | null>(null);
  const chunksRef = useRef<OutputChunk[]>([]);
  const failuresRef = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async (
    after: number,
    direction: "reset" | "next" | "previous" | "refresh",
  ) => {
    if (activeRequest.current !== null) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    const current = terminalRef.current;
    setLoading(true);
    let timedOut = false;
    const deadline = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 8_000);
    try {
      let next: StageRawOutputPage;
      try {
        next = await fetchStageRawOutput(item.ref, {
          after,
          limit: stageRawOutputPageSize,
          signal: controller.signal,
          phase: selectedPhase === "current" ? undefined : selectedPhase,
        });
      } catch (caught) {
        if (
          after === 0
          || !(caught instanceof ProductError)
          || caught.code !== "stage_raw_output_cursor_stale"
        ) throw caught;
        after = 0;
        direction = "reset";
        next = await fetchStageRawOutput(item.ref, {
          after,
          limit: stageRawOutputPageSize,
          signal: controller.signal,
          phase: selectedPhase === "current" ? undefined : selectedPhase,
        });
      }
      if (current && next.stream_ref !== current.stream_ref) {
        if (after !== 0) {
          after = 0;
          next = await fetchStageRawOutput(item.ref, {
            after,
            limit: stageRawOutputPageSize,
            signal: controller.signal,
            phase: selectedPhase === "current" ? undefined : selectedPhase,
          });
        }
        direction = "reset";
      }
      validateStageRawOutputPage(next, item.ref, after);
      if (controller.signal.aborted) return;
      if (current && next.stream_ref !== current.stream_ref && chunksRef.current.some(chunk => chunk.text)) {
        setPreviousOutput({ page: current, chunks: chunksRef.current });
      }
      if (direction === "next" && current) {
        setPreviousOffsets((offsets) => [...offsets, current.offset]);
      } else if (direction === "previous") {
        setPreviousOffsets((offsets) => offsets.slice(0, -1));
      } else if (direction === "reset") {
        setPreviousOffsets([]);
      }
      const collected = collectOutputPages(chunksRef.current, next, direction === "reset" || direction === "previous");
      chunksRef.current = collected;
      setChunks(collected);
      terminalRef.current = next;
      setTerminal(next);
      const observedAt = next.observed_at;
      const sourceUpdatedAt = next.source_updated_at;
      if (typeof observedAt === "number" && Number.isFinite(observedAt) && observedAt > 0) {
        setExecutionClock(previous => {
          if (current?.stream_ref !== next.stream_ref) previous = null;
          const source = typeof sourceUpdatedAt === "number" && Number.isFinite(sourceUpdatedAt) && sourceUpdatedAt > 0
            ? Math.max(sourceUpdatedAt, previous?.sourceUpdatedAt ?? 0) : previous?.sourceUpdatedAt;
          // A delayed poll must not rewind a clock already ticking for this record.
          if (previous && source === previous.sourceUpdatedAt) return previous;
          return source === undefined ? null : { sourceUpdatedAt: source, observedAt, receivedAt: performance.now() };
        });
      }
      failuresRef.current = 0;
      setError(null);
    } catch (caught) {
      if (controller.signal.aborted && !timedOut) return;
      failuresRef.current += 1;
      setError(timedOut ? "stage_raw_output_timeout" : caught instanceof ProductError
        ? caught.code
        : "stage_raw_output_unavailable");
    } finally {
      window.clearTimeout(deadline);
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setLoading(false);
      }
    }
  }, [item.ref, selectedPhase]);

  useEffect(() => {
    const handleVisibility = () => {
      setDocumentVisible(document.visibilityState !== "hidden");
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    activeRequest.current?.abort();
    activeRequest.current = null;
    terminalRef.current = null;
    chunksRef.current = [];
    failuresRef.current = 0;
    setTerminal(null);
    setExecutionClock(null);
    setChunks([]);
    setPreviousOutput(null);
    setFollowLive(true);
    setPreviousOffsets([]);
    setError(null);
    return () => {
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [item.ref, selectedPhase]);

  useEffect(() => {
    if (!expanded || !documentVisible) {
      activeRequest.current?.abort();
      activeRequest.current = null;
      setLoading(false);
      return;
    }
    const current = terminalRef.current;
    if (followLive || !current) void load(current?.offset ?? 0, current ? "refresh" : "reset");
  }, [documentVisible, expanded, followLive, load, retryEpoch, item.status, item.updatedAt]);

  useEffect(() => {
    if (
      !expanded
      || !documentVisible
      || !followLive
      || loading
    ) return;
    const timer = window.setTimeout(() => {
      const current = terminalRef.current;
      if (current?.has_more && current.next_offset > current.offset) {
        void load(current.next_offset, "next");
      } else {
        void load(current?.offset ?? 0, current ? "refresh" : "reset");
      }
    }, error ? Math.min(10_000, stageRawOutputPollMilliseconds * 2 ** Math.min(4, failuresRef.current - 1))
      : terminal?.has_more ? 150 : terminal?.status === "terminal" || terminal?.status === "replaced" ? 3_000
        : stageRawOutputPollMilliseconds);
    return () => window.clearTimeout(timer);
  }, [documentVisible, error, expanded, followLive, load, loading, terminal]);

  const atHead = Boolean(terminal?.source_caught_up && !terminal.has_more);
  const phaseLabel = terminal?.phase === "review" ? "审查"
    : terminal?.phase === "primary" ? "主任务"
      : terminal?.phase === "autonomous-resume" ? "审查后续处理"
        : terminal?.phase?.startsWith("target-batch-") ? `第 ${terminal.phase.slice(13)} 批实验调度`
          : terminal?.phase?.startsWith("dispatch-") ? `第 ${terminal.phase.slice(9)} 次调度`
            : terminal?.phase ?? (selectedPhase === "review" ? "审查与后续" : selectedPhase === "primary" ? "主任务" : "当前任务");
  useEffect(() => {
    if (followLive && atHead && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [atHead, followLive, terminal?.text]);

  return (
    <section className="research-conversation" data-testid={`stage-root-observations-${item.ref}`}>
      <header className="research-output-toolbar">
        <div role="group" aria-label="选择输出">
          {([['current', '当前输出'], ['primary', '主任务输出'], ['review', '审查输出']] as const).map(([phase, label]) => (
            <button key={phase} type="button" aria-pressed={selectedPhase === phase}
              onClick={() => setSelectedPhase(phase)}>{phase === "review" && item.source === "bundle_stage" ? "审查与调度" : label}</button>
          ))}
        </div>
        <span>{activityStatusCopy(item.status)}</span>
      </header>
      <div className="research-output-context">
        <div className="research-output-meta">
          <span>{phaseLabel} · {error ? "读取中断 · 自动重试" : terminal?.status === "terminal" ? "本次输出结束" : terminal?.status === "replaced" ? "本次输出已替换" : loading && !terminal ? "正在读取" : "按实际记录更新"}</span>
          <ExecutionElapsed sample={executionClock} ended={terminal?.status === "terminal" || terminal?.status === "replaced"} />
        </div>
        <details><summary>来源</summary><p>运行 {item.ref}</p><p>执行 {terminal?.operation_ref ?? "等待绑定"}</p><p>流 {terminal?.stream_ref ?? "等待记录"}</p></details>
      </div>
      <div className="research-conversation-scroll" role="log" aria-live="off" aria-label={`${item.label} 对话输出`}>
        {loading && !terminal ? <p className="research-output-empty">正在读取主智能体的工作记录…</p> : null}
        {error ? <div className="research-output-error" role="status">工作记录暂时无法读取，已保留上次内容。{followLive ? "正在自动重试。" : "继续跟随后会自动重试。"}<button onClick={() => { setFollowLive(true); setRetryEpoch(value => value + 1); }}>立即重试</button><details><summary>错误详情</summary><code>{error}</code></details></div> : null}
        {terminal && chunks.some(chunk => chunk.text) ? <div data-testid="stage-current-output"><StageReadableOutput rawText={terminal.text} chunks={chunks} startOffset={terminal.offset}
          streamKey={terminal.stream_ref} rootNativeSessionRef={terminal.native_session_ref}
          isTerminal={terminal.status === "terminal"} scrollRef={logRef} followLive={followLive} onPauseFollow={() => setFollowLive(false)} /></div> : null}
        {terminal && !chunks.some(chunk => chunk.text) && !error ? <p className="research-output-empty">{terminal.status === "terminal" ? "这次执行没有留下公开输出。" : previousOutput ? "新的执行已切换，等待新记录；上一次输出保留在下方。" : "等待主智能体发布工作记录，系统会自动检查更新。"}</p> : null}
        {previousOutput ? <details className="research-previous-output" data-testid="stage-previous-output" open={!terminal?.source_bytes}>
          <summary>上一次执行的输出 · {previousOutput.page.phase ?? "工作记录"}</summary>
          <p>执行 <code>{previousOutput.page.operation_ref}</code></p>
          <StageReadableOutput rawText={previousOutput.page.text} chunks={previousOutput.chunks}
            startOffset={previousOutput.page.offset} streamKey={previousOutput.page.stream_ref}
            rootNativeSessionRef={previousOutput.page.native_session_ref} isTerminal={previousOutput.page.status === "terminal"} />
        </details> : null}
      </div>
      <footer className="research-output-footer">
        <button type="button" aria-pressed={followLive} onClick={() => setFollowLive(value => !value)}>{followLive ? "跟随最新 ✓" : "继续跟随 ↓"}</button>
        <span>{!followLive ? "已暂停跟随，可以安心回看" : error ? "连接恢复后会自动补读" : terminal?.status === "terminal" ? "本次输出结束，继续检查后续执行" : atHead ? "已显示当前记录" : "正在读取后续记录"}</span>
        {terminal ? <details><summary>记录分页</summary><nav aria-label="Stage 原始 stdout 分页">
          <button disabled={loading || !previousOffsets.length} onClick={() => { setFollowLive(false); const previous = previousOffsets.at(-1); if (previous !== undefined) void load(previous, "previous"); }}>返回上一页</button>
          <code>{chunks[0]?.offset ?? terminal.offset}–{terminal.next_offset} / {terminal.source_bytes} bytes</code>
          <button disabled={loading || !terminal.has_more} onClick={() => {setFollowLive(false); void load(terminal.next_offset, "next");}}>读取下一页</button>
        </nav></details> : null}
      </footer>
    </section>
  );
}

function ResearchActivityLane({
  name,
  items,
  emptyCopy,
  enableStageRootObservations = false,
}: {
  name: string;
  items: ResearchActivityItem[];
  emptyCopy: string;
  enableStageRootObservations?: boolean;
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
              {enableStageRootObservations ? <StageRawOutputFeed item={item} /> : null}
            </li>
          ))}
        </ul>
      ) : <p>{emptyCopy}</p>}
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

function ActivityElapsed({ observedAt }: { observedAt: number }) {
  const [clock, setClock] = useState(() => Date.now());
  useEffect(() => {
    let timer: number | undefined;
    const updateVisibility = () => {
      window.clearInterval(timer);
      if (document.visibilityState === "hidden") return;
      setClock(Date.now());
      timer = window.setInterval(() => setClock(Date.now()), 1_000);
    };
    updateVisibility();
    document.addEventListener("visibilitychange", updateVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", updateVisibility);
    };
  }, []);
  return <b>{elapsedCopy(clock - observedAt)}</b>;
}

function ResearchTracePanel({snapshot, connected}: {
  snapshot: PublicSnapshot; latestActivity: ResearchActivitySignal | null; observedSince: number; connected: boolean;
}) {
  const foreground = snapshot.research_control.foreground;
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const items = snapshot.research_control.managed_runs
    .filter(run => run.quest_ref === foreground?.quest_ref && run.cycle_ref === foreground?.cycle_ref)
    .map(managedRunActivity).filter((item): item is ResearchActivityItem => item !== null)
    .sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0) || a.ref.localeCompare(b.ref));
  const currentKind = stageManagedRunKinds[foreground?.stage.toLowerCase() as StagePosition];
  const currentLabel = currentKind ? managedActivityKinds[currentKind].label : "当前阶段";
  const currentWorkerFailure = foreground && snapshot.readiness.checks.find(check => (
    check.name === `${foreground.stage.toLowerCase()}_stage_worker` && check.status === "unavailable"
  ));
  const current = items.find(item => item.source === currentKind) ?? null;
  const selected = items.find(item => item.ref === selectedRef) ?? current;
  useEffect(() => setSelectedRef(null), [foreground?.cycle_ref, foreground?.stage]);
  return <section className="research-flow" id="research-activity" tabIndex={-1} aria-labelledby="research-trace-title">
    <header className="research-flow-heading">
      <h2 id="research-trace-title">研究过程</h2>
    {items.length > 1 || (!current && items.length > 0) ? <label className="research-source-picker">查看记录 <select aria-label="研究记录来源" value={selected?.ref ?? ""} onChange={event => setSelectedRef(event.currentTarget.value || null)}>
      {!current ? <option value="">当前 · {currentLabel} · 尚未启动</option> : null}
      {items.map(item => <option key={item.ref} value={item.ref}>{item.ref === current?.ref ? "当前 · " : ""}{item.label} · {activityStatusCopy(item.status)}</option>)}
    </select>{selected?.ref !== current?.ref ? <button onClick={() => setSelectedRef(null)}>返回当前工作 ↗</button> : null}</label> : null}
      <span className="research-connection" data-connected={connected}><i />{connected ? "实时连接" : "连接中断 · 保留上次记录"}</span>
    </header>
    {currentWorkerFailure ? <div className="lumen-idea-health-blocker" data-testid="research-current-worker-blocker" role="status">
      <span aria-hidden="true">!</span>
      <div><b>当前{currentLabel}暂时无法继续</b><small>当前阶段推进受阻；已完成的研究记录仍可查看。</small></div>
      <code>{currentWorkerFailure.reason?.code ?? "worker_unavailable"}</code>
    </div> : null}
    {selected ? <StageRawOutputFeed key={selected.ref} item={selected} /> : <p className="research-output-empty">本阶段还没有公开的主智能体记录。产生后会自动显示在这里。</p>}
  </section>;
}

function CurrentCycleOverview({ snapshot }: { snapshot: PublicSnapshot }) {
  const foreground = snapshot.research_control.foreground;
  if (!foreground) return null;
  const question = exactForegroundQuestion(snapshot);
  const surfaces = allStageSurfaces(snapshot);
  const currentSurface = currentStageSurface(snapshot);
  const skippedStages = new Set(
    currentSurface?.kind === "Reasoning"
      ? currentSurface.projection.stage_run_request?.context_pack
        ?.upstream_stage_closure
        ?.filter((item) => item.disposition === "skipped")
        .map((item) => String(item.stage).toLowerCase())
      : [],
  );
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
      <p className="lumen-eyebrow">当前攻克 · 本轮研究</p>
      <h1 id="workspace-title">
        {question?.title ?? question?.unknown_statement ?? "当前研究问题"}<br />
        <em>{currentPurpose}</em>
      </h1>
      <p>围绕同一个研究问题展开；以下四类工作按实际需要进入或跳过。</p>
      <details className="lumen-cycle-identity">
        <summary>查看本轮研究标识</summary>
        <p>Question <code>{foreground.question_ref}</code> · Cycle <code>{foreground.cycle_ref}</code></p>
      </details>
      <ol
        className="lumen-cycle-stage-map"
        aria-label="当前 Cycle 的四个可能 Stage"
      >
        {stagePositions.map(({ kind, position, purpose }) => {
          const surface = surfaces.find((candidate) => candidate.kind === kind) ?? null;
          const state = stagePositionState(
            surface,
            foreground,
            position,
            skippedStages.has(position),
          );
          return (
            <li
              key={position}
              data-stage-position={position}
              data-stage-state={state}
              data-cycle-ref={foreground.cycle_ref}
            >
              <small>{kind}</small>
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
  const creation = restorableQuestCreation(snapshot.quest_creation.current, null);

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
          <em>{creation ? "从同一个草案继续。" : "从一个问题开始。"}</em>
        </h1>
        <p>
          {creation
            ? "草案已自动保存，可以继续完善研究目标与第一个问题。"
            : "设定研究目标，提供已有线索，让研究从第一个问题展开。"}
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

function stageQuestion(
  stage: CurrentStageSurface["projection"],
  snapshot?: PublicSnapshot,
): IdeaQuestionSummary {
  const creation = snapshot?.quest_creation.current;
  return {
    quest_ref: creation?.quest_ref,
    question_ref: stage.eligibility.question_ref ?? creation?.question_ref,
    graph_revision: snapshot?.owners.research_graph?.revision,
    ...(creation?.proposal?.content ?? {}),
    ...(stage.stage_run_request?.accepted_question_binding ?? {}),
    ...(snapshot?.research_space.current_question ?? {}),
  };
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
      <div className="lumen-question-copy">
        <small>未知点 · 回答形式 · 适用范围</small>
        <h2>{question.unknown_statement ?? question.title ?? "当前已接纳 Question"}</h2>
        <p>
          {question.applicability_scope
            ?? question.answer_shape
            ?? "根 Agent 围绕这个问题读取已有材料、形成产物并接受核验。"}
        </p>
      </div>
      <details className="lumen-cycle-identity">
        <summary>查看问题标识</summary>
        <p>研究任务 <code>{question.quest_ref ?? "未记录"}</code> · 问题 <code>{questionRef}</code> · {stage}</p>
      </details>
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

function TargetResearchFactsView({ facts, label }: { facts: ReturnType<typeof targetResearchFacts>; label: string }) {
  return <>
    <dl className="target-research-facts" aria-label={`${label} 研究状态`}>
      {facts.facts.map(fact => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.text}</dd></div>)}
    </dl>
    {facts.readableAssets.length ? <nav className="target-research-asset-links" aria-label="精确资产读取入口"><PageWindow items={facts.readableAssets} size={3} label="产物引用分页" render={asset => <a key={asset.versionRef} href={`/api/v1/research-assets/${encodeURIComponent(asset.versionRef)}/content`} title={asset.versionRef}>{asset.label}</a>} /></nav> : null}
    <BoundedDetails className="lumen-bundle-target-technical" summary="查看精确输入、产物与交接来源">{() => <>
      <pre>{JSON.stringify({ ...facts.sources, research_notes: undefined }, null, 2)}</pre>
      {facts.notes.length ? <PageWindow items={facts.notes} size={4} label="交接说明分页" render={(note, index) => <pre key={index}>{JSON.stringify(note, null, 2)}</pre>} /> : null}
    </>}</BoundedDetails>
  </>;
}

function BundleTargetCard({
  target,
  targetCommit,
  humanRequests,
  terminalOpen,
  onOpenTerminal,
}: {
  target: BundleTargetProjection;
  targetCommit: BundleTargetCommitProjection | undefined;
  humanRequests: readonly HumanRequestItem[];
  terminalOpen: boolean;
  onOpenTerminal: () => void;
}) {
  const facts = targetResearchFacts(target, targetCommit, humanRequests);

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
          <small>{facts.summary}</small>
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
      <TargetResearchFactsView facts={facts} label={target.target_key} />
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
  onShowLogFiles,
}: {
  target: BundleTargetProjection;
  observationPointer: TargetRootObservationPointer | null;
  minimized: boolean;
  blockedByHumanRequest: boolean;
  activityPaused: boolean;
  onMinimize: () => void;
  onClose: () => void;
  onShowLogFiles?: () => void;
}) {
  const [terminal, setTerminal] = useState<TargetRawTerminalState | null>(null);
  const [chunks, setChunks] = useState<OutputChunk[]>([]);
  const [followLive, setFollowLive] = useState(true);
  const [loading, setLoading] = useState(false);
  const [terminalError, setTerminalError] = useState<string | null>(null);
  const [previousOffsets, setPreviousOffsets] = useState<number[]>([]);
  const [documentVisible, setDocumentVisible] = useState(
    () => document.visibilityState !== "hidden",
  );
  const terminalRef = useRef<TargetRawTerminalState | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const requestSequence = useRef(0);
  const terminalLogRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
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
    let timedOut = false;
    const deadline = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, 8000);
    const current = terminalRef.current;
    // Every read gets a loading transition so repeated identical errors still schedule another poll.
    setLoading(true);
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
      setChunks(chunks => collectOutputPages(chunks, page, direction === "reset" || direction === "previous"));
      terminalRef.current = page;
      setTerminal(page);
      setTerminalError(null);
    } catch (caught) {
      if (controller.signal.aborted && !timedOut) return;
      if (request !== requestSequence.current) return;
      const code = timedOut ? "target_raw_output_timeout"
        : caught instanceof ProductError ? caught.code : "target_raw_output_unavailable";
      setTerminalError(code);
    } finally {
      window.clearTimeout(deadline);
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
    setChunks([]);
    setFollowLive(true);
    setPreviousOffsets([]);
    setTerminalError(null);
    return () => {
      requestSequence.current += 1;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [target.target_ref, target.target_run_ref]);

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
      || !followLive
      || loading
      || !(terminalError || terminal?.has_more || terminal?.status === "live" || (!terminal && targetMayStillProduceOutput))
    ) return;
    const timer = window.setTimeout(() => {
      const current = terminalRef.current;
      if (current?.has_more && current.next_offset > current.offset) {
        void loadRawOutput(current.next_offset, "next");
      } else {
        void loadRawOutput(current?.offset ?? 0, current ? "refresh" : "reset");
      }
    }, terminalError ? 2000 : terminal?.has_more ? 150 : targetRawOutputPollMilliseconds);
    return () => window.clearTimeout(timer);
  }, [
    loadRawOutput,
    followLive,
    loading,
    paused,
    targetMayStillProduceOutput,
    terminal,
    terminalError,
  ]);

  const atHead = Boolean(
    terminal
    && terminal.source_caught_up
    && !terminal.has_more,
  );
  useEffect(() => {
    if (followLive && atHead && terminalLogRef.current) {
      terminalLogRef.current.scrollTop = terminalLogRef.current.scrollHeight;
    }
  }, [atHead, followLive, terminal?.text]);

  useEffect(() => { if (!minimized && !blockedByHumanRequest) closeRef.current?.focus(); }, [minimized, blockedByHumanRequest]);
  return createPortal(
    <section
      id="target-output-dialog"
      onKeyDown={event => {if (event.key === "Escape") {event.stopPropagation(); onClose();}}}
      className="lumen-target-terminal lumen-target-terminal-dialog"
      role="dialog"
      aria-modal="false"
      aria-hidden={blockedByHumanRequest ? true : undefined}
      aria-label={`${target.target_key} 任务执行记录`}
      inert={blockedByHumanRequest}
      data-hc-background
      data-hc-inert-owner="terminal"
      data-terminal-state={terminal?.status ?? (loading ? "loading" : "unavailable")}
      data-minimized={minimized ? "true" : "false"}
    >
      <header>
        <div>
          <small>任务执行记录 · 命令与 Agent 输出</small>
          <b>{target.target_key}</b>
        </div>
        <div className="lumen-target-terminal-actions">
          <span data-live={terminal?.status === "live" ? "true" : "false"}>
            {loading && !terminal
              ? "读取中"
              : terminal?.status === "live"
                ? "尚未收到结束记录"
                : terminal?.status === "complete" ? "本次输出结束" : "等待读取记录"}
          </span>
          <button type="button" onClick={onMinimize}>
            {minimized ? "展开" : "最小化"}
          </button>
          <button ref={closeRef} type="button" onClick={onClose} aria-label="关闭任务执行记录">×</button>
        </div>
      </header>
      {!minimized ? (
        <div className="lumen-target-terminal-body">
          {onShowLogFiles ? <ExperimentOutputViews view="records" onChange={view => { if (view === "files") onShowLogFiles(); }} /> : null}
          <p className="lumen-target-terminal-boundary">
            显示任务准备、命令与 Agent 的实际输出。{onShowLogFiles ? "训练与评估文件可在对应页签查看。" : ""}
          </p>
          {terminal ? (
            <details className="research-terminal-source"><summary>执行来源</summary><dl className="lumen-target-terminal-identity">
              <div><dt>Target run</dt><dd>{terminal.target_run_ref ?? target.target_run_ref ?? "正在绑定"}</dd></div>
              <div><dt>Provider operation</dt><dd>{terminal.operation_ref}</dd></div>
              <div><dt>Transport</dt><dd>{terminal.transport_invocation_hash.slice(0, 16)}</dd></div>
            </dl></details>
          ) : null}
          {terminalError ? (
            <div className="lumen-target-terminal-error" role="alert">
              <b>{terminalError === "target_raw_output_timeout" ? "任务记录读取超时，正在自动重试" : "原始输出暂不可读"}</b>
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
          {terminal ? (
            <div ref={terminalLogRef} className="research-terminal-scroll" onScroll={event => {
              const view = event.currentTarget;
              if (view.scrollHeight - view.scrollTop - view.clientHeight > 48) setFollowLive(false);
            }}>
              <TargetCommandOutput rawText={terminal.text} chunks={chunks} startOffset={terminal.offset} streamKey={terminal.stream_ref}
                rootNativeSessionRef={terminal.native_session_ref ?? terminal.root_native_session_ref} isTerminal={terminal.status !== "live"}
                followLive={followLive} onPauseFollow={() => setFollowLive(false)} />
            </div>
          ) : null}
          {terminal && terminal.text.length === 0 ? (
            <p className="lumen-target-terminal-empty">
              {terminal.status === "live"
                ? "当前记录流暂时没有可读取输出，自动刷新中。"
                : "本次记录流已结束，没有可读取输出。"}
            </p>
          ) : null}
          {terminal ? (
            <footer>
              <button type="button" aria-pressed={followLive} onClick={() => setFollowLive(value => !value)}>
                {followLive ? "实时跟随中" : "跟随最新输出"}
              </button>
              <small>
                {atHead ? "已追到当前 stdout" : "还有后续页，可按需读取"}
                {` · ${terminal.mapped_bytes} / ${terminal.source_bytes} bytes`}
              </small>
              <nav aria-label="原始输出分页">
                <button
                  type="button"
                  disabled={loading || previousOffsets.length === 0}
                  onClick={() => {
                    setFollowLive(false);
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
                  onClick={() => { setFollowLive(false); void loadRawOutput(terminal.next_offset, "next"); }}
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
  humanRequests,
  healthBlocker,
  observationPointers,
  humanRequestModalOpen,
  activityPaused,
  runtimeControl,
}: {
  bundleStage: BundleStageProjection;
  humanRequests: readonly HumanRequestItem[];
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
        <b id="bundle-stage-title">研究实施与评价</b>
        <small>执行、评价与接纳分别记录</small>
      </header>
      <p className="target-research-guidance">负面结果、不确定或没有新认识都可以成为本轮记录。执行产物可先交接，评价可在后续继续。</p>
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
                humanRequests={humanRequests}
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

function ExperimentLogLauncher({snapshot, blocked, paused, observationPointers}: {
  snapshot: PublicSnapshot; blocked: boolean; paused: boolean;
  observationPointers: Record<string, TargetRootObservationPointer>;
}) {
  const foreground = snapshot.research_control.foreground;
  const bundle = allStageSurfaces(snapshot).find(surface => surface.kind === "Bundle");
  const targets = bundle?.kind === "Bundle" ? bundle.projection.target_graph.targets : [];
  const available = targets.filter(target => target.target_run_ref);
  const [selected, setSelected] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [view, setView] = useState<"records" | "files">("records");
  const opener = useRef<HTMLButtonElement>(null);
  const target = available.find(item => item.target_ref === selected) ?? available.find(item => item.status === "running") ?? available[0];
  useEffect(() => {setOpen(false); setSelected(null); setView("records");}, [foreground?.cycle_ref]);
  const close = () => {setOpen(false); opener.current?.focus();};
  return <div className="research-experiment-entry">
    <button ref={opener} type="button" className="research-log-button" aria-haspopup="dialog" disabled={!target}
      onClick={() => {if (!open) setView("records"); setOpen(true); setMinimized(false);}}>⌘ 实验日志 <span>↗</span></button>
    {available.length > 1 ? <select aria-label="选择实验任务" value={target?.target_ref ?? ""} onChange={event => {setSelected(event.currentTarget.value); setMinimized(false);}}>
      {available.map(item => <option key={item.target_ref} value={item.target_ref}>{item.target_key}</option>)}
    </select> : <small>{target ? target.target_key : targets.length ? "实验尚未开始执行" : "本轮尚无实验日志"}</small>}
    {open && target ? view === "records" ? <TargetTerminalDialog key={`records:${target.target_ref}:${target.target_run_ref}`} target={target}
      observationPointer={observationPointers[target.target_ref] ?? null}
      minimized={minimized} blockedByHumanRequest={blocked} activityPaused={paused}
      onMinimize={() => setMinimized(value => !value)} onClose={close}
      onShowLogFiles={() => {setView("files"); setMinimized(false);}} />
      : <ExperimentLogs key={`files:${target.target_ref}:${target.target_run_ref}`} target={target}
        minimized={minimized} blockedByHumanRequest={blocked} activityPaused={paused}
        onMinimize={() => setMinimized(value => !value)} onClose={close}
        onShowExecution={() => {setView("records"); setMinimized(false);}} /> : null}
  </div>;
}

function WorkspaceMain({
  snapshot,
  runtimeStatus,
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
  onCreate,
  onBrowseQuestions,
  onBrowseAssets,
  onBrowseWriting,
  onBrowseHumanRequests,
}: {
  snapshot: PublicSnapshot | null;
  runtimeStatus: ReturnType<typeof useWorkspaceStatus>;
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
  onCreate: () => void;
  onBrowseQuestions: () => void;
  onBrowseAssets: () => void;
  onBrowseWriting: () => void;
  onBrowseHumanRequests: (requestRef?: string) => void;
}) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const liveContext = conversationContext(snapshot, runtimeStatus.status, runtimeStatus.error);
  const rootConversations = useRootConversations({ ...liveContext, questRef: overviewQuestRef(snapshot) }, !hidden);
  // conversationContext may select a newer snapshot, including a pause or
  // failure. Only supplement the exact status observation it selected.
  const targetObservation = observedActiveTarget(!runtimeStatus.error
    && liveContext.foreground === runtimeStatus.status?.foreground ? runtimeStatus.status : null,
    !liveContext.stale && !rootConversations.error ? rootConversations.data : null);
  const targetActivity = targetObservation ? <p role="status" data-testid="current-target-activity">
    {targetObservation.target.title} · 执行中 · 观测于 <time dateTime={targetObservation.observedAt}>
      {new Date(targetObservation.observedAt).toLocaleString("zh-CN", { hour12: false })}</time>
  </p> : null;
  const [stageResultsRequested, setStageResultsRequested] = useState(() => Boolean(spectrumStage(new URLSearchParams(window.location.search).get("stage"))));
  const overview = useResearchOverview(snapshot, !hidden && Boolean(overviewQuestRef(snapshot)), historyOpen || stageResultsRequested);
  const unavailable = uniqueCapabilities(snapshot);
  const foreground = snapshot?.research_control.foreground;
  const showingLiveOverview = !snapshot ? runtimeStatus.status !== null
    : foreground?.quest_ref !== liveContext.foreground?.quest_ref
    || foreground?.cycle_ref !== liveContext.foreground?.cycle_ref
    || foreground?.question_ref !== liveContext.foreground?.question_ref
    || foreground?.stage.toLowerCase() !== liveContext.foreground?.stage.toLowerCase();
  const currentQuestion = snapshot ? exactForegroundQuestion(snapshot) : null;
  const requests = snapshot ? currentOpenHumanRequests(snapshot) : [];
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
  const displayedTarget = bundleStage?.target_graph.targets.find(target => target.target_ref === rootConversations.selected?.target_ref)
    ?? bundleStage?.target_graph.targets.find(target => target.status === "running") ?? bundleStage?.target_graph.targets.at(-1);
  const displayedTargetFacts = displayedTarget ? targetResearchFacts(displayedTarget,
    bundleStage?.target_commits.find(commit => commit.target_ref === displayedTarget.target_ref), snapshot?.human_collaboration?.human_requests.items ?? []) : null;
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
  const question = stageSurface
    ? stageQuestion(stageSurface.projection, snapshot ?? undefined)
    : null;
  return (
    <main
      id={hidden ? undefined : "main-content"}
      className="lumen-main"
      data-shell-region="main"
      hidden={hidden}
      tabIndex={hidden ? -1 : 0}
      aria-labelledby="workspace-title"
      aria-busy={state === "loading" && !liveContext.foreground}
    >
      {snapshot?.query_warnings?.length ? (
        <div className="lumen-reconnect-warning" role="status" data-testid="workspace-partial-warning">
          <span aria-hidden="true">!</span>
          <div>
            <p>
            <b>部分执行状态暂不可用</b>
            <small>研究内容已载入。可以继续查看阶段成果、历史和研究资料。</small>
            </p>
            <details><summary>查看受影响的状态</summary>
              {snapshot.query_warnings.map((warning, index) => <div key={`${warning.section}-${index}`}>
                {warning.section === "bundle_stage" ? "Bundle · 实验与证据" : warning.section} · {warning.code}
              </div>)}
            </details>
          </div>
        </div>
      ) : null}
      {(error || streamInterrupted) && snapshot ? (
        <div className="lumen-reconnect-warning" role="alert">
          <span aria-hidden="true">↺</span>
          <p>
            <b>{error ? "详细状态刷新失败，正在重试。" : "研究活动连接中断，正在重连。"}</b>
            <small>保留上次成功读取的状态 · {snapshot.observed_at ? new Date(snapshot.observed_at).toLocaleString("zh-CN", { hour12: false }) : "查询时间未记录"} · 版本 {snapshot.revision} · <a href="/">查看运行摘要</a></small>
          </p>
        </div>
      ) : null}

      <section
        className={`lumen-hero ${state}${snapshot?.research_space.status === "active" ? " has-research" : ""}`}
        role={state === "first-error" ? "alert" : undefined}
      >
        <span className="lumen-glow" aria-hidden="true" />
        <span className="lumen-comet" aria-hidden="true" />
        {state === "loading" && !showingLiveOverview ? <LoadingHero /> : null}
        {state === "first-error" && !showingLiveOverview ? <FirstErrorHero retry={retry} /> : null}
        {showingLiveOverview ? <div className="research-current-heading" data-testid="workspace-live-overview" data-cycle-ref={liveContext.foreground?.cycle_ref}>
          <p className="lumen-eyebrow">当前研究现场</p>
          <h1 id="workspace-title">{targetObservation?.status.current_task?.title ?? runtimeStatus.status?.current_task?.title ?? (liveContext.foreground ? "当前研究" : "当前没有运行中的阶段")}</h1>
          {targetActivity}
          <SpectrumStages compact current={rootConversations.currentStage} selected={rootConversations.selectedStage}
            onSelect={stage => rootConversations.selectStage(stage)} stale={liveContext.stale}
            stageContent={stage => <StageRootSessions model={rootConversations} stage={stage} />} />
          <p role="status">{liveContext.stale ? "运行状态正在重新读取，以下保留上次记录。" : "当前运行状态已载入。"}
            {error ? "详细成果读取失败，正在重试。" : "详细成果仍在加载，会自动补充。"}</p>
        </div> : null}
        {snapshot && foreground && !showingLiveOverview ? <div className="research-current-heading" data-testid="current-cycle-overview" data-cycle-ref={foreground.cycle_ref} data-question-ref={foreground.question_ref}>
          <div className="research-current-topline">
            <p className="lumen-eyebrow">当前研究问题</p>
            <div className="research-cycle-badge" aria-live="polite" data-cycle-ref={foreground.cycle_ref}>
              <span>当前 Cycle</span>
              <b>{overview.data ? cycleOrdinalLabel(overview.data.cycle_ordinal) : overview.error ? "轮次暂不可用" : "正在读取轮次…"}</b>
              {overview.error && !overview.data ? <button type="button" onClick={overview.retry} aria-label="重新读取 Cycle 轮次">↻</button> : null}
            </div>
          </div>
          <h1 id="workspace-title">{currentQuestion?.title ?? currentQuestion?.unknown_statement ?? "当前研究问题"}</h1>
          {targetActivity}
          <StageHistoryStrip snapshot={snapshot} overview={overview.data} error={overview.error} loading={overview.loading} onRetry={overview.retry} onRequestOverview={() => setStageResultsRequested(true)} rootConversations={rootConversations} />
          <button className="research-history-toggle" aria-expanded={historyOpen} onClick={() => setHistoryOpen(open => !open)}>{historyOpen ? "收起本轮记录与历史结果" : "查看本轮记录与历史结果"}<span aria-hidden="true">{historyOpen ? "−" : "+"}</span></button>
        </div> : snapshot && !showingLiveOverview ? <SnapshotHero snapshot={snapshot} /> : null}
        {snapshot && !foreground && state !== "loading" && state !== "first-error" ? (
          <div className="lumen-workspace-actions" aria-label="研究快捷操作">
            {snapshot.research_space.status === "empty" ? (
              <button className="lumen-primary" type="button" disabled={!questCreationReady(snapshot)} onClick={onCreate}>
                {restorableQuestCreation(snapshot.quest_creation.current, null) ? "继续草稿" : "开始研究"}
                <span aria-hidden="true">↗</span>
              </button>
            ) : (
              <button className="lumen-primary" type="button" disabled={snapshot.question_tree.status !== "ready"} onClick={onBrowseQuestions}>
                查看问题树 <span aria-hidden="true">↗</span>
              </button>
            )}
            <button className="lumen-secondary" type="button" disabled={snapshot.research_assets.status !== "ready"} onClick={onBrowseAssets}>
              查看研究资料
            </button>
            {snapshot.research_space.status === "active" ? (
              <button className="lumen-secondary" type="button" onClick={() => {
                const activity = document.getElementById("research-activity");
                activity?.focus({ preventScroll: true });
                activity?.scrollIntoView({ block: "start" });
              }}>查看本轮活动 ↓</button>
            ) : null}
          </div>
        ) : null}
      </section>

      {snapshot && overviewQuestRef(snapshot) && !showingLiveOverview ? <ResearchTimeline snapshot={snapshot} overview={overview.data} error={overview.error} onRetry={overview.retry} rootConversations={rootConversations} /> : null}
      {snapshot && foreground && !showingLiveOverview ? <>
        {historyOpen ? <>
          <ResearchOverview snapshot={snapshot} overview={overview.data} error={overview.error} onRetry={overview.retry} onOpenWriting={onBrowseWriting} connected={connected} />
        </> : null}
        <div className="research-primary-actions">
          <ExperimentLogLauncher snapshot={snapshot} blocked={humanRequestModalOpen} paused={hidden} observationPointers={targetRootObservationPointers} />
          <div><button onClick={onBrowseAssets}>研究资料 ↗</button><button onClick={onBrowseQuestions}>问题树 ↗</button></div>
        </div>
        {requests.length ? <button className="research-human-request" onClick={() => onBrowseHumanRequests(requests[0].request_ref)}><span><b>需要你回应 · {requests.length} 项</b><span>{requests[0].obligation}</span></span><b>查看并回应 ↗</b></button> : null}
        {rootConversations.selectedStage === "bundle" && displayedTarget && displayedTargetFacts ? <section className="research-target-status" aria-label="当前研究工作状态">
          <header><h2>{displayedTarget.target_key}</h2><p>{displayedTargetFacts.summary}</p></header>
          <BoundedDetails key={displayedTarget.target_ref} className="research-target-details" summary="查看输入、产物与交接">{() => <TargetResearchFactsView facts={displayedTargetFacts} label={displayedTarget.target_key} />}</BoundedDetails>
        </section> : null}
      </> : null}
      {(liveContext.foreground || rootConversations.selectedRef) && !hidden ? <RootConversations model={rootConversations} connected={connected} polling={Boolean(runtimeStatus.status && !runtimeStatus.error)} /> : null}
      <details className="research-existing-details"><summary>研究材料与阶段详情</summary>
      {showingLiveOverview && snapshot ? <p role="status">以下保留上次读取的阶段详情；当前轮次的详细结果仍在加载。</p> : null}
      <div className="lumen-lower">
        {stageSurface && question ? (
          <CurrentQuestionCard stage={stageSurface.kind} question={question} />
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
            <h2>{state === "ready-empty" ? "设定目标，逐步形成研究问题" : "这里只显示已经确认的研究事实"}</h2>
            <p>描述想解决的问题、预期成果与可用资料，即可开始一项研究。</p>
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
            humanRequests={snapshot?.human_collaboration?.human_requests.items ?? []}
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
              <b id="availability-title">研究服务</b>
              <small>当前研究服务</small>
            </header>
            {unavailable.length ? (
              <ul>
                {unavailable.map((item) => (
                  <li key={item.capability} data-capability={item.capability}>
                    <span>{capabilityLabels[item.capability] ?? item.capability}</span>
                    <span className="lumen-service-status" data-status={item.status}>
                      {item.status === "ready" ? "可使用" : "暂不可用"}
                    </span>
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
                  {unavailable.map((item) => (
                    <div key={item.capability}><dt>{item.capability}</dt><dd>{item.status}</dd></div>
                  ))}
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
      </details>
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
  const parameters = new URLSearchParams(window.location.search);
  const detailsRequested = parameters.has("workspace") || parameters.has("panel")
    || parameters.has("view") || parameters.has("inspector") || parameters.has("companion");
  return detailsRequested ? <DetailedApp /> : <StatusHome />;
}

function DetailedApp() {
  const initialParameters = useMemo(
    () => new URLSearchParams(window.location.search),
    [],
  );
  const [compactLayout, setCompactLayout] = useState(
    () => window.matchMedia("(max-width: 880px)").matches,
  );
  const [companionOpen, setCompanionOpen] = useState(() => initialParameters.get("companion") === "1");
  const companionReturnFocusRef = useRef<HTMLElement | null>(null);
  const companionScrollRef = useRef(0);
  const companionToggleRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 880px)");
    const update = () => setCompactLayout(media.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  const showCompanion = compactLayout && companionOpen;
  const toggleCompanion = () => {
    if (showCompanion) {
      setCompanionOpen(false);
      requestAnimationFrame(() => {
        window.scrollTo({ top: companionScrollRef.current });
        const target = companionReturnFocusRef.current;
        if (target?.isConnected && target.getClientRects().length) {
          target.focus({ preventScroll: true });
        } else companionToggleRef.current?.focus({ preventScroll: true });
      });
    } else {
      companionReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement : null;
      companionScrollRef.current = window.scrollY;
      setCompanionOpen(true);
    }
  };
  const [snapshot, setSnapshot] = useState<PublicSnapshot | null>(null);
  const runtimeStatus = useWorkspaceStatus(true);
  const observedForeground = conversationContext(snapshot, runtimeStatus.status, runtimeStatus.error).foreground;
  const snapshotForeground = snapshot?.research_control.foreground;
  const detailsScopeChanged = Boolean(snapshot && (
    snapshotForeground?.quest_ref !== observedForeground?.quest_ref
    || snapshotForeground?.cycle_ref !== observedForeground?.cycle_ref
    || snapshotForeground?.question_ref !== observedForeground?.question_ref
    || snapshotForeground?.stage.toLowerCase() !== observedForeground?.stage.toLowerCase()));
  useEffect(() => {
    if (!showCompanion) return;
    window.scrollTo({ top: 0 });
    const frame = requestAnimationFrame(() => {
      const input = document.querySelector<HTMLTextAreaElement>(
        "[aria-label='给研究助手发消息']",
      );
      if (input && !input.disabled) input.focus({ preventScroll: true });
      else document.querySelector<HTMLElement>(".lumen-companion")?.focus();
    });
    return () => cancelAnimationFrame(frame);
  }, [
    showCompanion,
    snapshot?.human_collaboration?.companion.status,
    snapshot?.human_collaboration?.companion.scope_ref,
  ]);
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
  const reloadRequest = useRef<Promise<void> | null>(null);
  const reloadController = useRef<AbortController | null>(null);
  const reloadQueued = useRef(false);
  const snapshotSections = useRef({ includeAssets: false, includeHistory: false });
  const requestedSections = useRef<{ includeAssets: boolean; includeHistory: boolean } | null>(null);
  const includeAssets = assetsOpen || creationMode !== null || manualPanel !== null;
  const includeHistory = questionTreeOpen || writingOpen || showCompanion;
  snapshotSections.current = { includeAssets, includeHistory };
  const eventRefreshTimer = useRef<number | null>(null);
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
  const humanRequestSurfaceOpen = humanRequestsOpen;

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

  const reload = useCallback((): Promise<void> => {
    const controller = reloadController.current;
    if (!controller || controller.signal.aborted) return Promise.resolve();
    reloadQueued.current = true;
    if (reloadRequest.current) return reloadRequest.current;
    const request = (async () => {
      do {
        reloadQueued.current = false;
        // A new read supersedes the previous failure while preserving cached data.
        setError(null);
        try {
          const sections = snapshotSections.current;
          requestedSections.current = sections;
          const next = await fetchSnapshot(controller.signal, sections);
          if (controller.signal.aborted) return;
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
          if (!controller.signal.aborted) {
            setSnapshotRetrySequence((current) => current + 1);
            setError("详细状态刷新失败，页面保留上次结果；可返回首页查看运行摘要。");
          }
        }
        if (reloadQueued.current && !controller.signal.aborted) {
          await new Promise<void>(resolve => {
            const done = () => { window.clearTimeout(timer); controller.signal.removeEventListener("abort", done); resolve(); };
            const timer = window.setTimeout(done, 2_000);
            controller.signal.addEventListener("abort", done, { once: true });
          });
        }
      } while (reloadQueued.current && !controller.signal.aborted);
    })().finally(() => {
      if (reloadRequest.current === request) reloadRequest.current = null;
    });
    reloadRequest.current = request;
    return request;
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    reloadController.current = controller;
    void reload();
    return () => {
      controller.abort();
      reloadController.current = null;
      reloadRequest.current = null;
      reloadQueued.current = false;
    };
  }, [reload]);

  useEffect(() => {
    if (!error || snapshotRetrySequence === 0) return;
    const exponent = Math.min(snapshotRetrySequence - 1, 4);
    const timer = window.setTimeout(
      () => void reload(),
      Math.min(5_000 * 2 ** exponent, 30_000),
    );
    return () => window.clearTimeout(timer);
  }, [error, reload, snapshotRetrySequence]);

  useEffect(() => {
    const requested = requestedSections.current;
    if (requested && (requested.includeAssets !== includeAssets || requested.includeHistory !== includeHistory)) {
      void reload();
    }
  }, [includeAssets, includeHistory, reload]);

  const scheduleEventReload = useCallback(() => {
    if (eventRefreshTimer.current !== null) return;
    eventRefreshTimer.current = window.setTimeout(() => {
      eventRefreshTimer.current = null;
      void reload();
    }, 2_000);
  }, [reload]);
  useEffect(() => () => {
    if (eventRefreshTimer.current !== null) window.clearTimeout(eventRefreshTimer.current);
  }, []);

  const streamReady = streamCursor !== null;
  useEffect(() => {
    if (!streamReady) return;
    return followProjection(
      streamCursor ?? 0,
      scheduleEventReload,
      scheduleEventReload,
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
  }, [handleConnection, handleResearchActivity, scheduleEventReload, streamReady]);

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
    if (selected.status === "open") markHumanRequestPresented(selected);
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
    const selectedHistory = snapshot?.human_collaboration?.human_requests.items.find(
      (item) => item.request_ref === selectedHumanRequestRef,
    );
    const nextRequest = currentOpenRequests.find(
      (item) => item.request_ref === selectedHistory?.successor_request_ref,
    ) ?? currentOpenRequests[0];
    markHumanRequestPresented(nextRequest);
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
    snapshot?.human_collaboration?.human_requests.items,
  ]);

  useEffect(() => {
    if (humanRequestsOpen) return;
    const currentRequest = currentOpenRequests.find(
      (request) => !humanRequestWasPresented(request),
    );
    if (!currentRequest) return;
    markHumanRequestPresented(currentRequest);
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
  )?.question_ref ?? scopedHistoryQuestions[0]?.question_ref ?? projectedCurrentQuestionRef ?? null;
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
    setCompanionOpen(false);
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
    setCompanionOpen(false);
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
    setCompanionOpen(false);
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
    setCompanionOpen(false);
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
    setCompanionOpen(false);
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
    opener: HTMLButtonElement,
  ) => {
    companionReturnFocusRef.current = opener;
    companionScrollRef.current = window.scrollY;
    setCompanionOpen(true);
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
    setCompanionOpen(false);
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
    if (request?.status === "open") markHumanRequestPresented(request);
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
      if (request.status === "open") markHumanRequestPresented(request);
      window.history.replaceState(
        null,
        "",
        `/?panel=${humanRequestPanelByKind[request.kind]}`,
      );
    }
  };
  const closeHumanRequests = () => {
    currentOpenRequests.forEach(markHumanRequestPresented);
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
      <a className="lumen-skip" href={showCompanion ? "#research-companion" : "#main-content"} data-hc-background>
        {showCompanion ? "跳到研究助手" : "跳到主要内容"}
      </a>
      <div className="lumen-shell" data-testid="product-shell" data-shell-state={state} data-companion-open={showCompanion} data-hc-background>
        <header className="lumen-header" data-shell-region="header">
          <a className="lumen-brand" aria-label="Meta-research 首页" href="/" style={{ color: "inherit", textDecoration: "none" }}>
            <span className="lumen-logo" aria-hidden="true">MR</span>
            <div><b>Meta Research</b><small>Lumen workspace</small></div>
          </a>
          <div className="lumen-quest-context">
            <small>{detailsScopeChanged ? "研究详情更新中" : observedForeground || snapshot?.research_space.status === "active" ? "当前研究" : "新的研究空间"}</small>
            <b>{detailsScopeChanged || !snapshot ? runtimeStatus.status?.current_task?.title ?? "正在读取研究空间" : exactForegroundQuestion(snapshot)?.title ?? (snapshot.research_space.status === "active" ? "当前研究现场" : "等待第一个研究问题")}</b>
          </div>
          <div className={`lumen-connection ${connected ? "connected" : ""}`} aria-live="polite">
            <i aria-hidden="true" />
            <span>
              {error || streamInterrupted ? "研究活动正在重连" : connected ? "持续接收真实活动" : snapshot ? "正在连接研究活动" : "读取研究状态"}
            </span>
            {snapshot ? <code>状态 {snapshot.revision}</code> : null}
          </div>
          <ForegroundResearchControlShortcut
            control={snapshot?.research_control}
            commands={snapshot?.human_collaboration?.commands.items}
            disabled={humanRequestSurfaceOpen || detailsScopeChanged}
            onChanged={() => void reload()}
          />
        </header>
        <LumenRail
          activeSection={humanRequestSurfaceOpen ? "requests"
            : creationMode ? "creation"
            : assetsOpen ? "assets"
            : writingOpen ? "writing"
            : questionTreeOpen ? questionInspectorMode === "history" ? "history" : "questions"
            : "overview"}
          canCreate={Boolean(canCreate)}
          canBrowseAssets={canBrowseAssets}
          canBrowseQuestions={canBrowseQuestions}
          canBrowseHistory={canBrowseHistory && manualPanel === null}
          canBrowseWriting={Boolean(canBrowseWriting)}
          questionUnavailableReason={questionUnavailableReason}
          questionButtonRef={questionTreeButtonRef}
          historyButtonRef={historyButtonRef}
          writingButtonRef={writingButtonRef}
          onBrowseQuestions={openQuestionTree}
          onBrowseHistory={openQuestionHistory}
          canBrowseHumanRequests={canBrowseHumanRequests}
          humanRequestCount={humanRequestCount}
          onCreate={openCreation}
          onBrowseAssets={openAssets}
          onBrowseWriting={openWriting}
          onBrowseHumanRequests={() => openHumanRequests()}
          onOverview={openOverview}
        />
        <WorkspaceMain
          snapshot={snapshot}
          runtimeStatus={runtimeStatus}
          state={state}
          error={error}
          streamInterrupted={streamInterrupted}
          connected={connected}
          latestActivity={latestResearchActivity}
          observedSince={observedSinceRef.current}
          hidden={Boolean((questionTreeOpen && snapshot) || showCompanion)}
          targetRootObservationPointers={targetRootObservationPointers}
          humanRequestModalOpen={humanRequestSurfaceOpen}
          retry={() => void reload()}
          onCreate={openCreation}
          onBrowseQuestions={openQuestionTree}
          onBrowseAssets={openAssets}
          onBrowseWriting={openWriting}
          onBrowseHumanRequests={(requestRef) => openHumanRequests(requestRef ?? null)}
        />
        {questionTreeOpen && snapshot?.question_tree.loaded === false ? (
          <main className="lumen-main" role="status"><p>{error ? "问题树读取失败，保留当前运行摘要。" : "正在读取问题树与历史…"}</p><button onClick={() => void reload()}>刷新详情</button> <a href="/">返回运行摘要</a></main>
        ) : null}
        {questionTreeOpen && snapshot && snapshot.question_tree.loaded !== false ? (
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
        <div id="research-companion" className="lumen-companion-panel" tabIndex={-1}>
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
        {!creationMode && !assetsOpen && !writingOpen && !manualPanel && !humanRequestSurfaceOpen ? (
          <button
            ref={companionToggleRef}
            className="lumen-companion-toggle"
            type="button"
            aria-controls="research-companion"
            aria-expanded={showCompanion}
            onClick={toggleCompanion}
          >
            <span aria-hidden="true">{showCompanion ? "←" : "✦"}</span>
            {showCompanion ? "返回研究" : "研究助手"}
          </button>
        ) : null}
      </div>
      {((creationMode && snapshot?.research_assets.loaded === false) || (writingOpen && snapshot?.writing.loaded === false)) ? (
        <div className="lumen-reconnect-warning" role="status"><p>{error ? "详情读取失败，请重试。" : "正在读取所需的研究资料…"}</p><button onClick={() => void reload()}>刷新详情</button></div>
      ) : null}
      {creationMode && snapshot && snapshot.research_assets.loaded !== false ? (
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
          currentQuestRef={snapshot.research_space.current_question?.quest_ref
            ?? snapshot.question_tree.items[0]?.quest_ref
            ?? snapshot.quest_creation.current?.quest_ref ?? null}
          currentQuestionRef={snapshot.research_space.current_question?.question_ref ?? null}
          intakeWorkerReady={Boolean(intakeWorkerReady)}
          verificationWorkerReady={Boolean(verificationWorkerReady)}
          onClose={closeAssets}
          onChanged={() => void reload()}
        />
      ) : null}
      {writingOpen && snapshot && snapshot.writing.loaded !== false ? (
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
            // The accepted turn starts its reply stream immediately. Snapshot
            // refresh errors must not turn a successful send into a retry.
            manualDetailSequence.current += 1;
            setManualPanel((current) => current?.raw.context_ref === creation_id
              ? { ...current, raw }
              : current);
            void reload();
          }}
          onRefreshDraftSession={async () => {
            const contextRef = manualPanel.raw.context_ref;
            const sequence = ++manualDetailSequence.current;
            const raw = await fetchManualQuestionCreation(contextRef);
            if (sequence !== manualDetailSequence.current) return;
            setManualPanel((current) => current?.raw.context_ref === contextRef
              ? { ...current, raw }
              : current);
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
    <OutputLanguageProvider><OutputLanguageControl floating /><App /></OutputLanguageProvider>
  </StrictMode>,
);
