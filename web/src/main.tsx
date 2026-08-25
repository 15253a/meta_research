import {
  StrictMode,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
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
  createHumanCommand,
  decideQuestCompletion,
  fetchAssetIntake,
  fetchCurrentManualQuestionCreation,
  fetchLiteratureSnapshot,
  fetchManualQuestionCreation,
  fetchSnapshot,
  fetchTargetRootObservations,
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
  type ExperimentProjection,
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
  type TargetRootObservationPage,
  type TargetRootObservationPointer,
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

function questWideBlockingHumanRequests(
  snapshot: PublicSnapshot | null,
): HumanRequestItem[] {
  const humanRequests = snapshot?.human_collaboration?.human_requests;
  const scopeRef = snapshot?.human_collaboration?.companion.scope_ref;
  if (
    humanRequests?.status !== "ready" ||
    !scopeRef ||
    humanRequests.waiting.safe_meaningful_runnable_exists
  ) {
    return [];
  }
  const questRef = scopeRef.startsWith("quest:")
    ? scopeRef.slice("quest:".length)
    : scopeRef;
  return humanRequests.items.filter((item) => (
    item.status === "open" &&
    item.quest_ref === questRef &&
    item.direct_waiters?.some((waiter) => waiter.wait_scope === "quest")
  ));
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
        "bundle_stage_worker",
        "reasoning_stage_worker",
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

type CurrentStageSurface =
  | { kind: "Idea"; projection: IdeaStageProjection }
  | { kind: "Plan"; projection: PlanStageProjection }
  | { kind: "Bundle"; projection: BundleStageProjection }
  | { kind: "Reasoning"; projection: ReasoningStageProjection };

function currentStageSurface(snapshot: PublicSnapshot): CurrentStageSurface | null {
  const candidates: CurrentStageSurface[] = [];
  if (snapshot.reasoning_stage) {
    candidates.push({ kind: "Reasoning", projection: snapshot.reasoning_stage });
  }
  if (snapshot.bundle_stage) {
    candidates.push({ kind: "Bundle", projection: snapshot.bundle_stage });
  }
  if (snapshot.plan_stage) {
    candidates.push({ kind: "Plan", projection: snapshot.plan_stage });
  }
  if (snapshot.idea_stage) {
    candidates.push({ kind: "Idea", projection: snapshot.idea_stage });
  }
  return candidates.find(
    (candidate) => ["eligible", "requested"].includes(
      candidate.projection.eligibility.status,
    ),
  ) ?? candidates[0] ?? null;
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
  const global = questWideBlockingHumanRequests(snapshot)[0];
  if (global) return `Quest wait · ${global.kind} · ${global.request_ref}`;
  const openLocal = snapshot.human_collaboration?.human_requests.status === "ready"
    ? snapshot.human_collaboration.human_requests.items.find((item) => item.status === "open")
    : null;
  if (openLocal) return `Local wait · ${openLocal.kind} · 其余安全工作可继续`;
  const unavailable = snapshot.readiness.checks.find((check) => check.status !== "ready");
  if (unavailable) {
    return `${unavailable.name} · ${unavailable.reason?.code ?? unavailable.status}`;
  }
  return "无公开 blocker · safe meaningful work remains";
}

function nextStepSummary(snapshot: PublicSnapshot): string {
  if (questWideBlockingHumanRequests(snapshot).length) {
    return "先处理 Quest-wide HumanRequest；stdout 保留为手动入口";
  }
  const stage = currentStageSurface(snapshot);
  if (!stage) return "从当前已接纳 Question 继续";
  const nextStage = stage.projection.stage_commit?.next_stage;
  if (nextStage) return `由 Advancement Engine 进入 ${nextStage}`;
  if (stage.projection.run) {
    return `等待 ${stage.kind} 的执行、内容接纳、领域接纳与推进各自完成`;
  }
  return `等待 ${stage.kind} 的公开 Owner 请求与 receipt`;
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
        <span>只报告 Owner receipt 可追溯的当前事实</span>
      </article>
      <article>
        <small>当前阻塞 · typed</small>
        <b>{currentBlockerSummary(snapshot)}</b>
        <span>局部等待与 Quest-wide wait 不合并</span>
      </article>
      <article>
        <small>下一步 · AE / Owner</small>
        <b>{nextStepSummary(snapshot)}</b>
        <span>Web 只解释公开 Projection，不代替 Owner 推进</span>
      </article>
    </section>
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

function BundleStageHero({
  bundleStage,
  question,
}: {
  bundleStage: BundleStageProjection;
  question: IdeaQuestionSummary;
}) {
  const committed = Boolean(bundleStage.stage_commit);
  const skipped = bundleStage.disposition.status === "skipped";
  const exhausted = bundleStage.disposition.report_disposition === "exhausted" &&
    bundleStage.bundle_exhaustion?.kind === "BundleExhaustion";
  const realized = bundleStage.target_commits.length;
  const total = bundleStage.target_graph.targets.length;

  return (
    <>
      <p className="lumen-eyebrow">Research cycle · Bundle Projection</p>
      <h1 id="workspace-title">
        {exhausted
          ? "Bundle 探索已按正式 basis 穷尽。"
          : skipped
          ? "Bundle 已按精确 basis 跳过。"
          : committed
            ? "Target closure 已形成正式交接。"
            : "从 FormalPlan 的 GapSet 出发。"}<br />
        <em>
          {exhausted
            ? "没有伪造 Target closure；穷尽结论与 basis receipt 保持可追溯。"
            : skipped
            ? "没有空 Run、伪 Target 或伪 TargetCommit。"
            : committed
              ? "负面结果也是可复用的已实现事实。"
              : `${realized}/${total || "—"} TargetCommit 已冻结。`}
        </em>
      </h1>
      <p>
        {question.unknown_statement
          ?? "Bundle root Session 负责调度；Target DAG、TargetRun 和 TargetCommit 仍由各自 Owner 独立拥有。"}
      </p>
      <ol className="lumen-stage-strip" aria-label="当前研究周期的四个 Stage">
        <li className="done"><small>01 · COMMITTED</small><b>Idea</b></li>
        <li className="done"><small>02 · COMMITTED</small><b>Plan</b></li>
        <li
          className={committed ? "done" : "current"}
          aria-current={committed ? undefined : "step"}
        >
          <small>{exhausted ? "03 · EXHAUSTED" : skipped ? "03 · SKIPPED" : committed ? "03 · COMMITTED" : "03 · NOW"}</small>
          <b>Bundle</b>
        </li>
        <li
          className={committed ? "current" : undefined}
          aria-current={committed ? "step" : undefined}
        >
          <small>04 · {committed ? "NOW" : "REQUIRED"}</small>
          <b>Reasoning</b>
        </li>
      </ol>
    </>
  );
}

function ReasoningStageHero({
  reasoningStage,
  question,
}: {
  reasoningStage: ReasoningStageProjection;
  question: IdeaQuestionSummary;
}) {
  const committed = Boolean(reasoningStage.stage_commit);
  const disposition = reasoningStage.reasoning_acceptance.disposition;
  const transitionKind = reasoningStage.transition.kind;
  const upstreamClosure = reasoningStage.stage_run_request?.context_pack
    ?.upstream_stage_closure;
  const upstreamDisposition = (stage: string): string => {
    const closure = upstreamClosure?.find((item) => item.stage === stage);
    return typeof closure?.disposition === "string"
      ? closure.disposition.toUpperCase()
      : "CLOSED";
  };

  return (
    <>
      <p className="lumen-eyebrow">Research cycle · Reasoning Projection</p>
      <h1 id="workspace-title">
        {committed
          ? "Reasoning 已形成正式交接。"
          : "从冻结的路线与证据 closure 出发。"}<br />
        <em>
          {committed
            ? `${disposition ?? "ScientificOutcome"} 与 ${transitionKind ?? "successor"} 仍是分层事实。`
            : "科学判断正在形成。"}
        </em>
      </h1>
      <p>
        {question.unknown_statement
          ?? "Reasoning 只提出 ScientificOutcome 与唯一后继候选；RM、RG、AR、AE 分别接纳和推进。"}
      </p>
      <ol className="lumen-stage-strip" aria-label="当前研究周期的四个 Stage">
        <li className="done"><small>01 · {upstreamDisposition("idea")}</small><b>Idea</b></li>
        <li className="done"><small>02 · {upstreamDisposition("plan")}</small><b>Plan</b></li>
        <li className="done"><small>03 · {upstreamDisposition("bundle")}</small><b>Bundle</b></li>
        <li
          className={committed ? "done" : "current"}
          aria-current={committed ? undefined : "step"}
        >
          <small>{committed ? "04 · COMMITTED" : "04 · NOW"}</small>
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
  stage: "Idea" | "Plan" | "Bundle" | "Reasoning";
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
  runtimeControl,
}: {
  ideaStage: IdeaStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  experiment: ExperimentProjection | null;
  questRef: string | null;
  onOpenExperiment: (trigger: HTMLElement) => void;
  onExperimentStarted: (experiment: ExperimentProjection) => void;
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
      state: skipped || exhausted
        ? "done"
        : run
          ? isRunBlocked(run.status)
            ? "blocked"
            : run.status === "completed" ? "done" : "current"
          : "pending",
      title: skipped || exhausted
        ? exhausted
          ? "BundleExhaustion 已接纳；没有制造空 root Run"
          : "GapSet 为空；未创建 Bundle Run"
        : run
          ? "一个 root/native Session 调度正式 Target；child agent 不进入 Target DAG"
          : "等待 Agent Runtime admission",
      status: skipped ? "not_created_by_design" : exhausted ? "exhausted" : run?.status ?? "not_created",
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

const targetRootObservationPageSize = 128;
const targetRootObservationRenderLimit = 512;

type TargetRootTerminalState = TargetRootObservationPage & {
  trimmed_count: number;
};

function observationClock(recordedAt: number): string {
  if (!Number.isFinite(recordedAt)) return "time unavailable";
  return new Date(recordedAt * 1_000).toISOString().slice(11, 19);
}

function mergeTargetRootObservationPage(
  current: TargetRootTerminalState | null,
  incoming: TargetRootObservationPage,
): TargetRootTerminalState {
  if (!current || current.stream_ref !== incoming.stream_ref) {
    const visible = incoming.items.slice(-targetRootObservationRenderLimit);
    return {
      ...incoming,
      items: visible,
      trimmed_count: Math.max(0, incoming.items.length - visible.length),
    };
  }
  const eventRefs = new Set(current.items.map((item) => item.event_ref));
  const appended = incoming.items.filter((item) => !eventRefs.has(item.event_ref));
  const combined = [...current.items, ...appended].sort((left, right) =>
    left.operation_generation - right.operation_generation
    || left.sequence - right.sequence
    || left.event_ref.localeCompare(right.event_ref)
  );
  const visible = combined.slice(-targetRootObservationRenderLimit);
  return {
    ...incoming,
    items: visible,
    trimmed_count:
      current.trimmed_count + Math.max(0, combined.length - visible.length),
  };
}

function BundleTargetCard({
  target,
  targetCommit,
  observationPointer,
}: {
  target: BundleTargetProjection;
  targetCommit: BundleTargetCommitProjection | undefined;
  observationPointer: TargetRootObservationPointer | null;
}) {
  const terminalId = useId();
  const [open, setOpen] = useState(false);
  const [terminal, setTerminal] = useState<TargetRootTerminalState | null>(null);
  const [loading, setLoading] = useState(false);
  const [terminalError, setTerminalError] = useState<string | null>(null);
  const terminalRef = useRef<TargetRootTerminalState | null>(null);
  const activeRequest = useRef<AbortController | null>(null);
  const requestSequence = useRef(0);

  const loadObservations = useCallback(async (restart: boolean) => {
    const request = requestSequence.current + 1;
    requestSequence.current = request;
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    const current = terminalRef.current;
    const after = restart || !current ? null : current.next_cursor;
    setLoading(true);
    setTerminalError(null);
    try {
      const page = await fetchTargetRootObservations(target.target_ref, {
        after,
        limit: targetRootObservationPageSize,
        signal: controller.signal,
      });
      if (
        page.target_ref !== target.target_ref
        || page.observation_only !== true
      ) {
        throw new ProductError("target_root_observation_identity_invalid");
      }
      if (request !== requestSequence.current) return;
      setTerminal((previous) => {
        const next = mergeTargetRootObservationPage(previous, page);
        terminalRef.current = next;
        return next;
      });
    } catch (caught) {
      if ((caught as Error).name === "AbortError") return;
      if (request !== requestSequence.current) return;
      const code = caught instanceof ProductError
        ? caught.code
        : "target_root_observation_unavailable";
      setTerminalError(code);
    } finally {
      if (request === requestSequence.current) {
        activeRequest.current = null;
        setLoading(false);
      }
    }
  }, [target.target_ref]);

  useEffect(() => {
    if (!open) {
      activeRequest.current?.abort();
      return;
    }
    void loadObservations(terminalRef.current === null);
    return () => activeRequest.current?.abort();
  }, [loadObservations, open]);

  useEffect(() => {
    if (
      !open
      || observationPointer?.target_ref !== target.target_ref
    ) return;
    const current = terminalRef.current;
    if (current === null) return;
    if (
      current.stream_ref === observationPointer.stream_ref
      && current.head_cursor === observationPointer.head_cursor
    ) return;
    void loadObservations(
      current.stream_ref !== observationPointer.stream_ref,
    );
  }, [
    loadObservations,
    observationPointer,
    open,
    target.target_ref,
    terminal?.head_cursor,
    terminal?.stream_ref,
  ]);

  useEffect(() => () => {
    requestSequence.current += 1;
    activeRequest.current?.abort();
  }, []);

  const result = targetCommit?.result_disposition
    ?? target.blocker?.code
    ?? target.status;
  const atHead = Boolean(
    terminal
    && terminal.next_cursor === terminal.head_cursor
    && !terminal.has_more,
  );

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
          <small>
            {target.target_ref} · {target.target_run_ref ?? "TargetRun pending"}
          </small>
        </p>
        <div className="lumen-bundle-target-actions">
          <code>{result}</code>
          {target.target_run_ref ? (
            <button
              type="button"
              aria-expanded={open}
              aria-controls={terminalId}
              onClick={() => setOpen((current) => !current)}
            >
              {open ? "收起根 Session" : "查看根 Session"}
            </button>
          ) : null}
        </div>
      </div>
      {open ? (
        <section
          id={terminalId}
          className="lumen-target-terminal"
          aria-label={`${target.target_key} 根 Session 输出`}
          data-terminal-state={terminal?.status ?? (loading ? "loading" : "unavailable")}
        >
          <header>
            <div>
              <small>ROOT SESSION / REDACTED OUTPUT</small>
              <b>已脱敏的根命令输出</b>
            </div>
            <span data-live={terminal?.status === "live" ? "true" : "false"}>
              {loading ? "读取中" : terminal?.status ?? "等待连接"}
            </span>
          </header>
          <p className="lumen-target-terminal-boundary">
            仅观察 · 不作为 TargetCommit、measurement 或 Stage 推进依据
          </p>
          {terminal ? (
            <dl className="lumen-target-terminal-identity">
              <div><dt>Attempt</dt><dd>{terminal.attempt_ref} · g{terminal.attempt_generation}</dd></div>
              <div><dt>Root</dt><dd>{terminal.root_session_ref}</dd></div>
              <div><dt>Native</dt><dd>{terminal.native_session_ref ?? "connecting"}</dd></div>
            </dl>
          ) : null}
          {terminalError ? (
            <div className="lumen-target-terminal-error" role="alert">
              <b>根 Session 输出暂不可读</b>
              <code>{terminalError}</code>
              <button type="button" onClick={() => void loadObservations(true)}>
                重新读取
              </button>
            </div>
          ) : null}
          {!terminalError && terminal?.items.length ? (
            <ol className="lumen-target-terminal-log" role="log" aria-live="off">
              {terminal.items.map((item) => (
                <li
                  key={item.event_ref}
                  data-output-gap={item.kind === "output_gap" ? "true" : "false"}
                >
                  <span>
                    {observationClock(item.recorded_at)} · g{item.operation_generation}/#{item.sequence}
                    {item.truncated ? " · TRUNCATED" : ""}
                    {item.kind === "output_gap"
                      ? ` · OUTPUT GAP · ${item.dropped_bytes} bytes / ${item.dropped_events} events`
                      : ""}
                  </span>
                  <pre>{item.text}</pre>
                </li>
              ))}
            </ol>
          ) : null}
          {!terminalError && terminal && terminal.items.length === 0 ? (
            <p className="lumen-target-terminal-empty">
              当前还没有可观察的根命令输出；保持打开会随全局 Projection 流读取。
            </p>
          ) : null}
          {terminal ? (
            <footer>
              <small>
                {terminal.trimmed_count > 0
                  ? `界面已收束 ${terminal.trimmed_count} 条较早片段 · `
                  : ""}
                {atHead ? "已到当前流头" : "还有后续片段"}
              </small>
              {terminal.has_more ? (
                <button
                  type="button"
                  disabled={loading}
                  onClick={() => void loadObservations(false)}
                >
                  读取下一页
                </button>
              ) : null}
            </footer>
          ) : null}
        </section>
      ) : null}
    </article>
  );
}

function BundleStageCard({
  bundleStage,
  healthBlocker,
  observationPointers,
  runtimeControl,
}: {
  bundleStage: BundleStageProjection;
  healthBlocker: IdeaStageHealthBlocker | null;
  observationPointers: Record<string, TargetRootObservationPointer>;
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
        <b id="bundle-stage-title">Bundle 的六层事实</b>
        <small>root Session ≠ Target DAG ≠ TargetRun ≠ TargetCommit</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="bundle-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>Bundle 自动推进暂时不可用</b>
            <small>已接纳的 TargetCommit 保持可见；worker 恢复后按 Target identity 继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
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
      {graph.targets.length ? (
        <div className="lumen-bundle-targets" data-testid="bundle-target-list">
          {graph.targets.map((target) => {
            const targetCommit = bundleStage.target_commits.find(
              (candidate) => candidate.target_ref === target.target_ref,
            );
            return (
              <BundleTargetCard
                key={target.target_ref}
                target={target}
                targetCommit={targetCommit}
                observationPointer={
                  observationPointers[target.target_ref] ?? null
                }
              />
            );
          })}
        </div>
      ) : null}
      <details className="lumen-idea-details">
        <summary>查看 Bundle、Target 与 receipt 身份</summary>
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
        <b id="reasoning-stage-title">Reasoning 的七层事实</b>
        <small>execution ≠ content ≠ domain ≠ advancement</small>
      </header>
      {healthBlocker ? (
        <div
          className="lumen-idea-health-blocker"
          data-testid="reasoning-stage-health-blocker"
          role="status"
        >
          <span aria-hidden="true">!</span>
          <div>
            <b>Reasoning 自动推进暂时不可用</b>
            <small>已形成的 request、execution 与 Owner receipt 保持可见；恢复后从首个缺口继续。</small>
          </div>
          <code>{healthBlocker.code}</code>
        </div>
      ) : null}
      {acceptance.disposition || transition.kind ? (
        <div className="lumen-reasoning-summary">
          <span data-testid="reasoning-outcome">
            <small>ScientificOutcome</small>
            <b>{acceptance.disposition ?? "pending"}</b>
          </span>
          <i aria-hidden="true">→</i>
          <span data-testid="reasoning-transition">
            <small>唯一后继候选</small>
            <b>{transition.kind ?? "pending"}</b>
          </span>
        </div>
      ) : null}
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
      <details className="lumen-idea-details">
        <summary>查看 Reasoning closure、运行身份与 receipt</summary>
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
          <small>REASONING / VERIFIED FOLLOW-UP</small>
          <b id="reasoning-followup-title">唯一后继正在跨过自己的 Owner 边界</b>
        </div>
        <p>StageCommit 不代替 Question 接纳、人类确认或 Quest ending。</p>
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
              <code>{autonomousCreation?.status ?? checkpointStatus}</code>
            </header>
            <p>{autonomousStatusCopy(autonomousCreation)}</p>
            <ol className="lumen-boundary-beam" aria-label="自动创建的 Owner 边界">
              <li data-state={checkpoint ? "done" : "current"}><b>HC</b><small>scope</small></li>
              <li data-state={autonomousRequested ? "done" : "current"}><b>AE</b><small>request</small></li>
              <li data-state={autonomousFetched ? "done" : autonomousRequested ? "current" : "pending"}><b>AR</b><small>DeepFetch</small></li>
              <li data-state={autonomousContentAccepted ? "done" : autonomousFetched ? "current" : "pending"}><b>RM</b><small>content</small></li>
              <li data-state={anchor ? "done" : autonomousContentAccepted ? "current" : "pending"}><b>RG</b><small>identity</small></li>
            </ol>
            <div className="lumen-followup-rule">
              <span aria-hidden="true">✓</span>
              <p><b>DeepFetch 必须执行</b><small>无额外人工确认 · 无 waiver</small></p>
            </div>
            <details>
              <summary>查看创建来源、DeepFetch 与 Owner receipt</summary>
              <dl>
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
              <code>{questCompletion?.status ?? "candidate"}</code>
            </header>
            <p>{completionStatusCopy(questCompletion)}</p>
            <ol className="lumen-boundary-beam compact" aria-label="Quest 结束的 Owner 边界">
              <li data-state={decision ? "done" : preview ? "current" : "pending"}><b>HC</b><small>decision</small></li>
              <li data-state={completionDomainAccepted ? "done" : completionConfirmed ? "current" : "pending"}><b>RG</b><small>semantics</small></li>
              <li data-state={questCompletion?.ending_transition ? "done" : completionDomainAccepted ? "current" : "pending"}><b>AE</b><small>ending</small></li>
            </ol>

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
              <summary>查看 Goal、里程碑与 Owner receipt</summary>
              <dl>
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
  targetRootObservationPointers,
  retry,
  onOpenExperiment,
  onExperimentStarted,
}: {
  snapshot: PublicSnapshot | null;
  state: ShellState;
  error: string | null;
  streamInterrupted: boolean;
  targetRootObservationPointers: Record<string, TargetRootObservationPointer>;
  retry: () => void;
  onOpenExperiment: (trigger: HTMLElement) => void;
  onExperimentStarted: (experiment: ExperimentProjection) => void;
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
  const experiment = snapshot?.experiment?.current ?? null;
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
        {snapshot?.research_space.status === "active" ? (
          <ReturnSummary snapshot={snapshot} />
        ) : null}
      </section>

      <div className="lumen-lower">
        {reasoningStage ? (
          <CurrentQuestionCard
            stage="Reasoning"
            question={question!}
            experiment={experiment}
            onOpenExperiment={onOpenExperiment}
          />
        ) : bundleStage ? (
          <CurrentQuestionCard
            stage="Bundle"
            question={question!}
            experiment={experiment}
            onOpenExperiment={onOpenExperiment}
          />
        ) : planStage ? (
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
            experiment={experiment}
            questRef={question?.quest_ref ?? null}
            onOpenExperiment={onOpenExperiment}
            onExperimentStarted={onExperimentStarted}
            runtimeControl={runtimeControl}
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
    ) || initialParameters.get("view") === "questions",
  );
  const [questionRouteNodeRef, setQuestionRouteNodeRef] = useState<string | null>(
    () => initialParameters.get("node"),
  );
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
  const [targetRootObservationPointers, setTargetRootObservationPointers] =
    useState<Record<string, TargetRootObservationPointer>>({});
  const [snapshotRetrySequence, setSnapshotRetrySequence] = useState(0);
  const reloadInFlight = useRef(false);
  const reloadQueued = useRef(false);
  const streamCursorRef = useRef<number | null>(null);
  const manualDetailSequence = useRef(0);
  const questionTreeButtonRef = useRef<HTMLButtonElement>(null);
  const writingButtonRef = useRef<HTMLButtonElement>(null);
  const humanRequestReturnFocusRef = useRef<HTMLElement | null>(null);
  const questWideRequests = useMemo(
    () => questWideBlockingHumanRequests(snapshot),
    [
      snapshot?.human_collaboration?.companion.scope_ref,
      snapshot?.human_collaboration?.human_requests,
    ],
  );
  const executionObserver = useExecutionObserver(
    snapshot?.experiment?.current ?? null,
    Boolean(
      creationMode ||
      assetsOpen ||
      writingOpen ||
      manualPanel ||
      humanRequestsOpen ||
      questWideRequests.length,
    ),
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
      (pointer) => setTargetRootObservationPointers((current) => ({
        ...current,
        [pointer.target_ref]: pointer,
      })),
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
    const globalRequest = questWideRequests.find((item) => {
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
    executionObserver.deferForPrioritySurface();
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
  }, [executionObserver.deferForPrioritySurface, questWideRequests]);

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
    setSelectedQuestionContext(null);
    setPendingDirectManualParentRef(null);
    window.history.replaceState(null, "", questionTreeUrl());
    setQuestionTreeOpen(true);
  };
  const closeQuestionTree = () => {
    setManualPanel(null);
    setQuestionTreeOpen(false);
    setQuestionRouteNodeRef(null);
    setSelectedQuestionContext(null);
    setPendingDirectManualParentRef(null);
    setManualOpenError(null);
    window.history.replaceState(null, "", "/");
    requestAnimationFrame(() => {
      questionTreeButtonRef.current?.focus({ preventScroll: true });
    });
  };

  const selectQuestionTreeContext = useCallback((question: QuestionTreeItem | null) => {
    setSelectedQuestionContext((current) => (
      current?.question_ref === question?.question_ref &&
      current?.content_hash === question?.content_hash &&
      current?.lifecycle_revision === question?.lifecycle_revision
        ? current
        : question
    ));
    setQuestionRouteNodeRef((current) => {
      const next = question?.question_ref ?? null;
      return current === next ? current : next;
    });
    if (question && !manualPanel) {
      window.history.replaceState(null, "", questionTreeUrl(question.question_ref));
    }
  }, [manualPanel]);

  const discussQuestionWithCompanion = useCallback((
    question: QuestionTreeItem,
    _opener: HTMLButtonElement,
  ) => {
    setSelectedQuestionContext(question);
    setQuestionRouteNodeRef(question.question_ref);
    window.history.replaceState(null, "", questionTreeUrl(question.question_ref));
    requestAnimationFrame(() => {
      document.querySelector<HTMLInputElement>(
        "[aria-label='给 Quest Companion 发消息']",
      )?.focus({ preventScroll: false });
    });
  }, []);

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
            controlsInert={manualPanel !== null}
            openingParentRef={manualOpeningParentRef}
            openError={manualOpenError}
            currentExperiment={executionObserver.current}
            onClose={closeQuestionTree}
            onOpenExperiment={executionObserver.open}
            onSelectionChange={selectQuestionTreeContext}
            onDiscussQuestion={discussQuestionWithCompanion}
            onCreateQuestion={openManualCreation}
            onControlQuestion={controlQuestionLifecycle}
          />
        ) : (
          <WorkspaceMain
            snapshot={snapshot}
            state={state}
            error={error}
            streamInterrupted={streamInterrupted}
            targetRootObservationPointers={targetRootObservationPointers}
            retry={() => void reload()}
            onOpenExperiment={executionObserver.open}
            onExperimentStarted={handleExperimentStarted}
          />
        )}
        <QuestCompanion
          state={state}
          collaboration={snapshot?.human_collaboration}
          researchControl={manualPanel ? undefined : snapshot?.research_control}
          questions={snapshot?.question_tree.items ?? []}
          questionContext={questionTreeOpen && manualPanel === null
            ? selectedQuestionContext
            : null}
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
