export type ReadinessCheck = {
  name: string;
  status: "ready" | "stale" | "unavailable";
  revision?: number;
  count?: number;
  reason?: { code?: string; message?: string };
};

export type OwnerSnapshot = {
  status: "ready" | "unavailable";
  revision: number;
  facts: Record<string, string | number | boolean | null>;
};

export type UnavailableCapability = {
  capability: string;
  status: "capability_unavailable";
  reason: { code: string; message?: string };
};

export type AssetReceipt = {
  issuer: string;
  kind: string;
  receipt_ref: string;
  subject_ref: string;
  payload_hash: string;
};

export type ResearchAssetItem = {
  asset_ref: string;
  version_ref: string;
  memory_ref: string;
  version_number: number;
  source_kind: string;
  display_name: string;
  media_type: string;
  content_hash: string;
  manifest_hash: string;
  byte_count: number;
  provenance: Record<string, unknown>;
  custody_modes: string[];
  integrity: string;
  availability: string;
  verification_observed_at: number | null;
  verification_pending: boolean;
  accepted_at: number;
  receipt: AssetReceipt;
};

export type ResearchAssetCustody = {
  version_ref: string;
  custody_ref: string;
  custody_mode: "managed" | "linked_local";
  source_locator: string | null;
  locator_receipted: boolean;
  locator_bound_at: number | null;
  locator_receipt: AssetReceipt | null;
  established_at: number;
  receipt: AssetReceipt;
};

export type ResearchAssetRole = {
  role_ref: string;
  version_ref: string;
  asset_ref: string;
  asset_hash: string;
  manifest_hash: string;
  role: "evidence" | "quest_source_material";
  quest_ref: string;
  accepted_at: number;
  asset_receipt: AssetReceipt;
  receipt: AssetReceipt;
};

export type ResearchAssetHold = {
  hold_ref: string;
  version_ref: string;
  reason: string;
  active: boolean;
  placed_at: number;
  released_at: number | null;
  placement_receipt: AssetReceipt;
  release_receipt: AssetReceipt | null;
};

export type AssetReleaseAssessment = {
  assessment_ref: string;
  version_ref: string;
  expected_reference_revision: number | null;
  observed_reference_revision: number | null;
  active_reference_refs: string[];
  active_hold_refs: string[];
  eligible: boolean;
  reason_codes: string[];
  assessed_at: number;
  receipt: AssetReceipt;
};

export type ResearchAssetsView = {
  status: "ready";
  revision: number;
  inventory_revision: number;
  items: ResearchAssetItem[];
  custodies: ResearchAssetCustody[];
  roles: ResearchAssetRole[];
  holds: ResearchAssetHold[];
  release_assessments: AssetReleaseAssessment[];
  reference_revision: number;
  offset: number;
  limit: number;
  total_count: number;
  has_more: boolean;
};

export type ResearchAssetDetail = ResearchAssetItem & {
  revision: number;
  inventory_revision: number;
  custodies: ResearchAssetCustody[];
  roles: ResearchAssetRole[];
  holds: ResearchAssetHold[];
  release_assessments: AssetReleaseAssessment[];
  reference_revision: number;
};

export type AssetHistoryPage<T> = {
  items: T[];
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
};

export type AssetIntakeRequest = {
  source_kind:
    | "text"
    | "file"
    | "directory"
    | "local_path"
    | "repository"
    | "link"
    | "system_artifact";
  custody_mode: "managed" | "linked_local";
  display_name: string;
  media_type: string;
  text?: string;
  content_base64?: string;
  source_locator?: string;
  provenance?: Record<string, unknown>;
  asset_ref?: string;
  asynchronous?: boolean;
};

export type AssetIntakeResult = {
  job_ref: string;
  status: "queued" | "processing" | "accepted" | "failed";
  source_kind: string;
  custody_mode: string;
  attempt_count: number;
  asset: null | (ResearchAssetItem & { provenance?: Record<string, unknown> });
  failure: null | { code: string };
};

export type LiteratureMode = "oa_then_institution" | "oa_only" | "provided_only";

export type QuestDraft = {
  goal: string;
  completion_criteria: string;
  time_budget: "7d" | "30d" | "90d" | "open";
  route: "direct" | "deepfetch";
  resource_envelope_ref: string | null;
  resource_envelope_hash: string | null;
  literature: {
    mode: LiteratureMode;
    library_entry_url: string;
    scope_exclusions: string;
    accepted_material_bindings: Array<Record<string, unknown>>;
  };
  background_and_initial_direction: string;
};

export type LegacyQuestDraft = {
  goal: string;
  completion_criteria: string;
  key_configuration: string;
  literature_scope: "comprehensive" | "open_access" | "provided_materials";
  initial_question_direction: string;
  material_receipts: string[];
};

export type QuestDraftValue = QuestDraft | LegacyQuestDraft;

export type QuestDraftWriteBasis = {
  revision: number;
  hash: string;
};

export type QuestionProposalWriteBasis = {
  draftRevision: number;
  draftHash: string;
  proposalRef: string | null;
  proposalHash: string | null;
};

export type QuestionContent = {
  title: string;
  unknown_statement: string;
  answer_shape: string;
  applicability_scope: string;
  background_context: string;
  requirements_constraints: string;
};

export type ReceiptState =
  | {
      status: "not_attempted";
      reason?: { code: string; upstream_step?: string };
    }
  | {
      status: "accepted";
      issuer: string;
      kind: string;
      receipt_ref: string;
      subject_ref: string;
      payload_hash: string;
    }
  | {
      status: "rejected" | "stale";
      reason: { code: string };
    };

export type AggregateReceiptState = {
  status: "accepted";
  issuer: string;
  kind: "quest_source_material_role_set";
  role_refs: string[];
  receipts: Array<Extract<ReceiptState, { status: "accepted" }>>;
};

export type QuestReceiptState = ReceiptState | AggregateReceiptState;

export type ComputeDevice = {
  uuid: string;
  name: string;
  memory_total_mib: number;
};

export type QuestComputeSnapshot = {
  snapshot_ref: string;
  status: "ready" | "unavailable";
  adapter_kind: string;
  observed_at: number;
  devices: ComputeDevice[];
  reason: null | { code: string };
};

export type QuestResourceEnvelope = {
  ref: string;
  hash: string;
  schema_ref: string;
  status: "current" | "stale";
  host_snapshot_ref: string;
  time_budget: QuestDraft["time_budget"];
  hard_ceiling: {
    kind: "wall_clock" | "open_ended";
    seconds: number | null;
  };
  selected_device_uuids: string[];
};

export type DeepFetchRun = {
  run_ref: string;
  status: "admitted" | "running" | "executed" | "failed" | "cancelled";
  attempt_ref: string | null;
  attempt_generation: number;
  root_session_ref: string;
  native_session_ref: string | null;
  fence_ref: string | null;
  runtime_binding_hash: string;
  execution_receipt: Extract<ReceiptState, { status: "accepted" }> | null;
  failure: null | { code: string };
};

export type AcquisitionSessionProjection = {
  session_ref: string;
  status:
    | "probing"
    | "ready"
    | "waiting_user"
    | "unavailable"
    | "acquiring"
    | "cancelled";
  freshness: "current" | "stale";
  mode: QuestDraft["literature"]["mode"];
  preflight_generation: number;
  request_count: number;
  current_request_id: string | null;
  slot_held: boolean;
  browser_context: "verified" | "unavailable";
  reason: null | { code: string };
};

export type LiteratureSnapshot = {
  status: "accepted";
  snapshot_ref: string;
  request_ref: string;
  initialization_id: string;
  draft_revision: number;
  draft_hash: string;
  scope_hash: string;
  completion: "complete" | "limited" | "honest_empty";
  summary_ref: string;
  summary_hash: string;
  papers_ref: string;
  papers_hash: string;
  fulltexts_ref: string;
  fulltexts_hash: string;
  limitations: string[];
  snapshot_hash: string;
  paper_count: number;
  fulltext_count: number;
  receipt: Extract<ReceiptState, { status: "accepted" }>;
};

export type DeepFetchProjection = {
  request_ref: string;
  correlation_ref: string;
  basis_revision: number;
  basis_hash: string;
  scope_hash: string;
  status: "queued" | "running" | "accepting" | "succeeded" | "failed" | "cancelled";
  activity:
    | "waiting_for_runtime"
    | "web_research"
    | "accepting_assets"
    | "proposal_drafting"
    | "complete"
    | "needs_retry"
    | "cancelled";
  progress: { completed: number; total: number };
  freshness: "current" | "stale";
  authorization_receipt: Extract<ReceiptState, { status: "accepted" }>;
  run: DeepFetchRun | null;
  literature_snapshot: LiteratureSnapshot | null;
  failure: null | { code: string };
};

export type IntentSessionTurn = {
  ref: string;
  ordinal: number;
  basis_revision: number;
  basis_hash: string;
  user_content: string;
  user_content_hash: string;
  assistant_status: "queued" | "running" | "completed" | "unavailable" | "failed";
  assistant_content: string | null;
  assistant_content_hash: string | null;
  reason: null | { code: string };
};

export type TargetAssertion = {
  owner: string;
  operation: string;
  may_change: string[];
  will_not_change: string[];
  preconditions: string[];
  risks: string[];
  stale_if: string[];
  bindings: Record<string, unknown>;
  target_hash: string;
};

export type QuestCapability =
  | { status: "ready" }
  | Omit<UnavailableCapability, "capability">;

export type QuestCreationView = {
  initialization_id: string;
  creation_context: "quest_initialization";
  route: "direct" | "deepfetch";
  status:
    | "draft"
    | "proposal_generating"
    | "proposal_ready"
    | "proposal_stale"
    | "dispatching"
    | "partial"
    | "recovering"
    | "unavailable"
    | "completed"
    | "cancelled";
  quest_draft: {
    revision: number;
    hash: string;
    schema_ref: string;
    value: QuestDraftValue;
  };
  compute: QuestComputeSnapshot | null;
  resource_envelope: QuestResourceEnvelope | null;
  proposal_generation: null | {
    ref: string;
    basis_revision: number;
    basis_hash: string;
    status:
      | "queued"
      | "running"
      | "succeeded"
      | "capability_unavailable"
      | "failed";
    adapter_kind: string;
    attempt_count: number;
    proposal_ref?: string;
    proposal_hash?: string;
    failure: null | { code: string };
    literature_snapshot_ref: string | null;
  };
  proposal: null | {
    ref: string;
    revision: number;
    hash: string;
    basis_revision: number;
    basis_hash: string;
    status: "current" | "incomplete" | "stale";
    content: QuestionContent;
    literature_snapshot_ref: string | null;
  };
  confirmation_preview: null | {
    ref: string;
    hash: string;
    schema_ref: string;
    basis_revision: number;
    basis_hash: string;
    proposal_ref: string;
    proposal_hash: string;
    status: "current" | "stale" | "consumed";
    target_assertions: TargetAssertion[];
    will_happen: string[];
    will_not_happen: string[];
    feed_revision: number | null;
  };
  intent_session: null | {
    ref: string;
    status: "open" | "closed";
    turns: IntentSessionTurn[];
  };
  acquisition_session: AcquisitionSessionProjection | null;
  deepfetch: DeepFetchProjection | null;
  capabilities: {
    direct: QuestCapability;
    first_question_deepfetch: QuestCapability;
    accepted_material_basis: QuestCapability;
  };
  receipts: Record<
    | "human_confirmation"
    | "quest_goal"
    | "question_content"
    | "question_identity"
    | "cycle_activation",
    ReceiptState
  > & {
    quest_source_material?: QuestReceiptState;
  };
  recovery: null | {
    state: string;
    first_missing_step: string | null;
    attempt_count: number;
    reason: null | { code: string };
    next_retry_at: number | null;
  };
  canonical_empty_advancement: boolean;
  quest_ref?: string;
  memory_ref?: string;
  question_ref?: string;
  cycle_ref?: string;
};

export type IdeaReceipt = {
  status?: string;
  issuer?: string;
  kind?: string;
  receipt_ref?: string;
  subject_ref?: string;
  payload_hash?: string;
  [key: string]: unknown;
};

export type IdeaAcceptanceFact = {
  status: string;
  receipt?: IdeaReceipt | null;
  reason?: null | { code?: string; message?: string; [key: string]: unknown };
  [key: string]: unknown;
};

export type IdeaQuestionSummary = {
  quest_ref?: string;
  question_ref?: string;
  graph_revision?: number;
  title?: string;
  unknown_statement?: string;
  answer_shape?: string;
  applicability_scope?: string;
  [key: string]: unknown;
};

export type IdeaStageProjection = {
  eligibility: {
    status: string;
    cycle_ref?: string;
    question_ref?: string;
    reason?: null | { code?: string; message?: string; [key: string]: unknown };
    [key: string]: unknown;
  };
  stage_run_request: null | {
    status?: string;
    request_ref?: string;
    stage_run_request_ref?: string;
    cycle_ref?: string;
    stage?: string;
    epoch?: number;
    foreground_epoch_ref?: string;
    accepted_question_binding?: null | (IdeaQuestionSummary & {
      initialization_id?: string;
      ref?: string;
      binding_ref?: string;
      content_ref?: string;
      content_hash?: string;
      schema_ref?: string;
      question_content_ref?: string;
      question_content_hash?: string;
      content_receipt?: IdeaReceipt | null;
      question_receipt?: IdeaReceipt | null;
    });
    context_pack_ref?: string;
    context_pack_hash?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  run: null | {
    status: string;
    run_ref?: string;
    attempt_ref?: string;
    attempt_generation?: number;
    submission_ref?: string | null;
    root_session_ref?: string;
    native_session_ref?: string | null;
    provider_operations?: {
      primary?: {
        invocation_ref?: string;
        status?: string;
        request_hash?: string;
        response_hash?: string | null;
      };
      review?: {
        invocation_ref?: string;
        status?: string;
        request_hash?: string;
        response_hash?: string | null;
      };
    };
    primary_draft_checkpoint?: null | {
      status: "recorded";
      draft_hash: string;
      adapter_kind: string;
    };
    fence_ref?: string;
    fence_status?: string;
    attempt_execution_receipt?: IdeaReceipt | null;
    completion_receipt?: IdeaReceipt | null;
    blocker?: null | {
      code?: string;
      message?: string;
      [key: string]: unknown;
    };
    review?: null | {
      status?: string;
      review_mode?: "harness_child_agent" | "legacy_external_session";
      reviewer_agent_ref?: string;
      reviewer_session_ref?: string;
      finding_count?: number;
      disposition_count?: number;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  outcome_acceptance: {
    status: string;
    outcome_kind?: string;
    outcome_ref?: string | null;
    content: IdeaAcceptanceFact;
    domain: IdeaAcceptanceFact;
    rejection?: null | {
      code?: string;
      message?: string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  stage_commit: null | {
    status: string;
    commit_ref?: string;
    stage_commit_ref?: string;
    request_ref?: string;
    cycle_ref?: string;
    stage?: string;
    epoch?: number;
    run_ref?: string;
    outcome_ref?: string;
    outcome_kind?: string;
    run_completion_receipt?: IdeaReceipt | null;
    outcome_receipt?: IdeaReceipt | null;
    receipt?: IdeaReceipt | null;
    next_stage?: string;
    [key: string]: unknown;
  };
  [key: string]: unknown;
};

export type PublicSnapshot = {
  product: { name: string; version: string };
  revision: number;
  readiness: { status: "ready" | "unavailable"; checks: ReadinessCheck[] };
  research_space: {
    status: "empty" | "active";
    quest_count: number;
    question_count: number;
    foreground_cycle_count: number;
    current_question?: IdeaQuestionSummary | null;
  };
  owners: Record<string, OwnerSnapshot>;
  quest_creation: {
    status: "ready";
    route: "direct_or_deepfetch";
    current: QuestCreationView | null;
    accepted_material_basis: QuestCapability;
    first_question_deepfetch: QuestCapability;
  };
  research_assets: ResearchAssetsView;
  idea_stage?: IdeaStageProjection | null;
  unavailable: UnavailableCapability[];
};

export class ProductError extends Error {
  constructor(public readonly code: string) {
    super(code);
  }
}

export async function fetchSnapshot(signal?: AbortSignal): Promise<PublicSnapshot> {
  const response = await fetch("/api/v1/snapshot", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new ProductError(`snapshot_unavailable:${response.status}`);
  }
  return (await response.json()) as PublicSnapshot;
}

export async function fetchResearchAssets(
  signal?: AbortSignal,
  offset = 0,
  limit = 50,
): Promise<ResearchAssetsView> {
  const parameters = new URLSearchParams({
    offset: String(offset),
    limit: String(limit),
  });
  return readJson(`/api/v1/research-assets?${parameters}`, signal);
}

export function fetchResearchAsset(
  memoryRef: string,
  signal?: AbortSignal,
): Promise<ResearchAssetDetail> {
  return readJson(
    `/api/v1/research-assets/${encodeURIComponent(memoryRef)}`,
    signal,
  );
}

function fetchAssetHistory<T>(
  memoryRef: string,
  kind: "roles" | "holds" | "release-assessments",
  cursor: string | null,
  limit = 50,
): Promise<AssetHistoryPage<T>> {
  const parameters = new URLSearchParams({ limit: String(limit) });
  if (cursor !== null) parameters.set("cursor", cursor);
  return readJson(
    `/api/v1/research-assets/${encodeURIComponent(memoryRef)}/${kind}?${parameters}`,
  );
}

export function fetchAssetRoleHistory(
  memoryRef: string,
  cursor: string | null,
): Promise<AssetHistoryPage<ResearchAssetRole>> {
  return fetchAssetHistory(memoryRef, "roles", cursor);
}

export function fetchAssetHoldHistory(
  memoryRef: string,
  cursor: string | null,
): Promise<AssetHistoryPage<ResearchAssetHold>> {
  return fetchAssetHistory(memoryRef, "holds", cursor);
}

export function fetchAssetReleaseHistory(
  memoryRef: string,
  cursor: string | null,
): Promise<AssetHistoryPage<AssetReleaseAssessment>> {
  return fetchAssetHistory(memoryRef, "release-assessments", cursor);
}

export async function submitAssetIntake(
  intake: AssetIntakeRequest,
): Promise<AssetIntakeResult> {
  const result = await writeJson<AssetIntakeResult>(
    "/api/v1/research-assets/intakes",
    "POST",
    intake,
    {
      retainPending: () => true,
      onRetained: (result, pendingWrite) => {
        writeSessionValue(
          pendingAssetIntakeSlot,
          JSON.stringify({ job_ref: result.job_ref, write_slot: pendingWrite.slot }),
        );
      },
    },
  );
  return result;
}

export async function fetchAssetIntake(
  jobRef: string,
  signal?: AbortSignal,
): Promise<AssetIntakeResult> {
  const result = await readJson<AssetIntakeResult>(
    `/api/v1/research-assets/intakes/${jobRef}`,
    signal,
  );
  return result;
}

export function pendingAssetIntakeJobRef(): string | null {
  const record = readPendingAssetIntake();
  return record?.job_ref ?? null;
}

export function acknowledgeAssetIntake(jobRef: string): void {
  clearPendingAssetIntake(jobRef);
}

export function handoffAssetToManaged(memoryRef: string): Promise<{
  version_ref: string;
  custody_ref: string;
  custody_mode: "managed";
  established_at: number;
  receipt: AssetReceipt;
}> {
  return writeJson(
    `/api/v1/research-assets/${memoryRef}/custody/managed`,
    "POST",
    {},
  );
}

export function placeAssetHold(
  memoryRef: string,
  reason: string,
): Promise<ResearchAssetHold> {
  return writeJson(`/api/v1/research-assets/${memoryRef}/holds`, "POST", { reason });
}

export function releaseAssetHold(holdRef: string): Promise<ResearchAssetHold> {
  return writeJson(`/api/v1/research-assets/holds/${holdRef}/release`, "POST", {});
}

export function assessAssetRelease(
  memoryRef: string,
  expectedReferenceRevision: number,
): Promise<AssetReleaseAssessment> {
  return writeJson(
    `/api/v1/research-assets/${memoryRef}/release-eligibility`,
    "POST",
    { expected_reference_revision: expectedReferenceRevision },
  );
}

export function acceptAssetRole(
  memoryRef: string,
  role: "evidence" | "quest_source_material",
  questRef: string,
): Promise<ResearchAssetRole> {
  return writeJson(`/api/v1/research-assets/${memoryRef}/roles`, "POST", {
    role,
    quest_ref: questRef,
  });
}

export function createQuest(): Promise<QuestCreationView> {
  return writeJson("/api/v1/quest-initializations", "POST", {});
}

export function reviseQuestDraft(
  creation: QuestCreationView,
  draft: QuestDraft,
  expected: QuestDraftWriteBasis = creation.quest_draft,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/draft`,
    "PUT",
    {
      expected_draft_revision: expected.revision,
      expected_draft_hash: expected.hash,
      draft,
    },
  );
}

export function observeHostCompute(
  creation: QuestCreationView,
  selectedDeviceUuids: string[] = [],
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/compute-probe`,
    "POST",
    { selected_device_uuids: selectedDeviceUuids },
  );
}

export function prepareAcquisitionSession(
  creation: QuestCreationView,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/acquisition-session`,
    "POST",
    {
      expected_draft_revision: creation.quest_draft.revision,
      expected_draft_hash: creation.quest_draft.hash,
    },
  );
}

export function generateQuestionProposal(
  creation: QuestCreationView,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/proposal-generations`,
    "POST",
    {
      expected_draft_revision: creation.quest_draft.revision,
      expected_draft_hash: creation.quest_draft.hash,
    },
  );
}

export function saveQuestionProposal(
  creation: QuestCreationView,
  content: QuestionContent,
  explicitReview = false,
  expected: QuestionProposalWriteBasis = {
    draftRevision: creation.quest_draft.revision,
    draftHash: creation.quest_draft.hash,
    proposalRef: creation.proposal?.ref ?? null,
    proposalHash: creation.proposal?.hash ?? null,
  },
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/proposal`,
    "PUT",
    {
      expected_draft_revision: expected.draftRevision,
      expected_draft_hash: expected.draftHash,
      expected_proposal_ref: expected.proposalRef,
      expected_proposal_hash: expected.proposalHash,
      explicit_review: explicitReview,
      content,
    },
  );
}

export function sendIntentMessage(
  creation: QuestCreationView,
  message: string,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/intent-session/messages`,
    "POST",
    {
      expected_draft_revision: creation.quest_draft.revision,
      expected_draft_hash: creation.quest_draft.hash,
      message,
    },
  );
}

export async function fetchQuestCreation(
  initializationId: string,
  signal?: AbortSignal,
): Promise<QuestCreationView> {
  const response = await fetch(
    `/api/v1/quest-initializations/${initializationId}`,
    {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) {
    throw new ProductError(`quest_initialization_unavailable:${response.status}`);
  }
  return (await response.json()) as QuestCreationView;
}

export function confirmQuest(
  creation: QuestCreationView,
): Promise<QuestCreationView> {
  if (!creation.proposal) throw new ProductError("question_proposal_missing");
  if (!creation.confirmation_preview) {
    throw new ProductError("confirmation_preview_required");
  }
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/confirmation`,
    "POST",
    {
      quest_draft_revision: creation.quest_draft.revision,
      quest_draft_hash: creation.quest_draft.hash,
      proposal_ref: creation.proposal.ref,
      proposal_hash: creation.proposal.hash,
      preview_ref: creation.confirmation_preview.ref,
      preview_hash: creation.confirmation_preview.hash,
    },
  );
}

export function cancelQuest(creation: QuestCreationView): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/cancel`,
    "POST",
    {},
  );
}

async function readJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) {
    throw new ProductError(`request_failed:${response.status}`);
  }
  return (await response.json()) as T;
}

async function writeJson<T>(
  path: string,
  method: "POST" | "PUT",
  body: object,
  options?: {
    retainPending: (payload: T) => boolean;
    onRetained: (payload: T, pendingWrite: PendingWrite) => void;
  },
): Promise<T> {
  const csrfToken = readCookie("meta_research_csrf");
  if (!csrfToken) throw new ProductError("csrf_token_unavailable");
  const bodyJson = JSON.stringify(body);
  const pendingWrite = await reserveIdempotencyKey(method, path, bodyJson);
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": pendingWrite.key,
      "X-CSRF-Token": csrfToken,
    },
    body: bodyJson,
  });
  if (!response.ok) {
    let code = `request_failed:${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: { code?: string } };
      code = payload.detail?.code ?? code;
    } catch {
      // The status remains an actionable fallback when the daemon cannot return JSON.
    }
    throw new ProductError(code);
  }
  const payload = (await response.json()) as T;
  if (options?.retainPending(payload)) options.onRetained(payload, pendingWrite);
  else pendingWrite.clear();
  return payload;
}

const inMemoryPendingWrites = new Map<string, string>();
const pendingAssetIntakeSlot = "meta_research_pending_asset_intake";

type PendingWrite = { key: string; slot: string; clear: () => void };
type PendingAssetIntake = { job_ref: string; write_slot: string };

async function reserveIdempotencyKey(
  method: string,
  path: string,
  bodyJson: string,
): Promise<PendingWrite> {
  const bytes = new TextEncoder().encode(`${method}\n${path}\n${bodyJson}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const fingerprint = Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
  const slot = `meta_research_pending_write:${fingerprint}`;
  const existing = inMemoryPendingWrites.get(slot) ?? readSessionValue(slot);
  const key = existing ?? crypto.randomUUID();
  inMemoryPendingWrites.set(slot, key);
  writeSessionValue(slot, key);
  return {
    key,
    slot,
    clear: () => {
      inMemoryPendingWrites.delete(slot);
      removeSessionValue(slot);
    },
  };
}

function readPendingAssetIntake(): PendingAssetIntake | null {
  const value = readSessionValue(pendingAssetIntakeSlot);
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PendingAssetIntake>;
    return typeof parsed.job_ref === "string" && typeof parsed.write_slot === "string"
      ? { job_ref: parsed.job_ref, write_slot: parsed.write_slot }
      : null;
  } catch {
    return null;
  }
}

function clearPendingAssetIntake(jobRef: string): void {
  const record = readPendingAssetIntake();
  if (!record || record.job_ref !== jobRef) return;
  inMemoryPendingWrites.delete(record.write_slot);
  removeSessionValue(record.write_slot);
  removeSessionValue(pendingAssetIntakeSlot);
}

function readSessionValue(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSessionValue(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // The in-memory copy still protects retries within this page lifecycle.
  }
}

function removeSessionValue(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // No persistent copy was available to clear.
  }
}

function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  const value = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(prefix));
  return value ? decodeURIComponent(value.slice(prefix.length)) : null;
}

export function followProjection(
  afterRevision: number,
  onRevision: (revision: number) => void,
  onSnapshotRequired: () => void,
  onConnection: (connected: boolean) => void,
): () => void {
  const eventTypes = [
    "system.ready",
    "human_collaboration.quest_draft_created",
    "human_collaboration.quest_draft_revised",
    "human_collaboration.host_compute_observed",
    "human_collaboration.question_proposal_generation_queued",
    "human_collaboration.question_proposal_generation_failed",
    "human_collaboration.question_proposal_recorded",
    "human_collaboration.intent_message_queued",
    "human_collaboration.intent_reply_failed",
    "human_collaboration.intent_reply_recorded",
    "human_collaboration.confirmation_preview_recorded",
    "human_collaboration.quest_bundle_confirmed",
    "human_collaboration.bundle_confirmation_not_accepted",
    "human_collaboration.quest_initialization_cancelled",
    "human_collaboration.quest_initialization_completed",
    "human_collaboration.quest_dispatch_rejected",
    "human_collaboration.quest_dispatch_recovery_started",
    "research_graph.quest_accepted",
    "research_memory.question_content_accepted",
    "research_graph.root_question_accepted",
    "advancement_engine.initial_cycle_activated",
    "advancement_engine.stage_run_requested",
    "agent_runtime.stage_run_admitted",
    "agent_runtime.attempt_executed",
    "agent_runtime.attempt_rejected",
    "agent_runtime.stage_run_completed",
    "research_memory.idea_outcome_content_accepted",
    "research_graph.idea_outcome_accepted",
    "research_graph.idea_outcome_rejected",
    "advancement_engine.stage_committed",
    "research_memory.asset_intake_queued",
    "research_memory.asset_accepted",
    "research_memory.asset_intake_failed",
    "research_memory.asset_custody_handed_off",
    "research_memory.asset_hold_placed",
    "research_memory.asset_hold_released",
    "research_memory.release_eligibility_assessed",
    "research_graph.asset_role_accepted",
  ];
  let cursor = afterRevision;
  let stream: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reloadTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectAttempt = 0;
  let stopped = false;

  const acceptCursor = (event: Event) => {
    const revision = Number((event as MessageEvent).lastEventId);
    if (!Number.isSafeInteger(revision) || revision <= cursor) return false;
    cursor = revision;
    return true;
  };

  const scheduleSnapshotReload = () => {
    if (reloadTimer !== null) return;
    reloadTimer = setTimeout(() => {
      reloadTimer = null;
      onRevision(cursor);
    }, 50);
  };

  const connect = () => {
    if (stopped) return;
    const next = new EventSource(`/api/v1/events?after=${cursor}`);
    stream = next;
    next.onopen = () => {
      reconnectAttempt = 0;
      onConnection(true);
    };
    next.onerror = () => {
      if (stopped || stream !== next) return;
      onConnection(false);
      next.close();
      stream = null;
      const delay = Math.min(250 * 2 ** reconnectAttempt, 4_000);
      reconnectAttempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };
    const update = (event: Event) => {
      if (!acceptCursor(event)) return;
      scheduleSnapshotReload();
    };
    next.addEventListener("projection.updated", update);
    for (const eventType of eventTypes) next.addEventListener(eventType, update);
    next.addEventListener("snapshot.required", (event) => {
      acceptCursor(event);
      if (reloadTimer !== null) {
        clearTimeout(reloadTimer);
        reloadTimer = null;
      }
      onSnapshotRequired();
    });
  };

  connect();
  return () => {
    stopped = true;
    if (reconnectTimer !== null) clearTimeout(reconnectTimer);
    if (reloadTimer !== null) clearTimeout(reloadTimer);
    stream?.close();
  };
}
