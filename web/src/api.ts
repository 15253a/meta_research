import type {
  ManualCreationAcceptedMaterialBindingView,
  ManualCreationDraftingTurn,
  ManualCreationReceiptState,
  ManualQuestionContent,
  ManualQuestionCreationView,
} from "./ManualCreation";

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
  creation_context_kind?: "manual_question_creation";
  creation_context_ref?: string;
  quest_ref?: string;
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
    | "broad_research_authorization"
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

export type QuestionTreeItem = {
  question_ref: string;
  quest_ref: string;
  parent_question_ref: string | null;
  title: string | null;
  unknown_statement: string | null;
  content_ref: string;
  content_hash: string;
  schema_ref: string;
  question_receipt_ref: string;
  lifecycle_status: "active" | "pruned";
  lifecycle_revision: number;
  cycle_binding: {
    status: "bound" | "not_bound" | "unavailable";
    cycle_ref: string | null;
    foreground: null | {
      quest_ref: string;
      cycle_ref: string;
      question_ref: string;
      stage: string;
      epoch: number;
      status: string;
      grant_ref: string;
      grant_status: string;
      safe_point_ref: string | null;
      pending_operation_ref: string | null;
      owner_revision: number;
    };
    reason: { code: string } | null;
  };
  related_human_requests: {
    status: "ready" | "unavailable";
    items: Array<{
      request_ref: string;
      issuer: string;
      kind: string;
      status: string;
      revision: number;
      bindings: Array<{
        source: "target_assertion" | "direct_waiter";
        waiter_ref?: string;
        field: string;
        ref: string;
      }>;
    }>;
    reason: { code: string } | null;
  };
};

export type QuestionTreeProjection =
  | {
      status: "ready";
      items: QuestionTreeItem[];
      reason: null;
    }
  | {
      status: "unavailable";
      reason: { code: string; message?: string };
      items: [];
    }
  | {
      status: "capability_unavailable";
      reason: { code: string; message?: string };
      items: [];
    };

export type ManualQuestionCreationCapability =
  | {
      status: "ready";
      creation_mode: "ManualCreation";
      deepfetch: QuestCapability;
      explicit_waiver: QuestCapability;
    }
  | {
      status: "capability_unavailable";
      creation_mode: "ManualCreation";
      reason: { code: string; message?: string };
      deepfetch?: never;
      explicit_waiver?: never;
    };

export type ManualAcceptedMaterialBinding = {
  asset_ref: string;
  version_ref: string;
  content_hash: string;
  manifest_hash: string;
  receipt: AssetReceipt & { status: "accepted" };
};

export type ManualRawReceiptState =
  | { status: "not_attempted" | "pending"; reason?: { code: string; upstream_step?: string } }
  | ({ status: "accepted" } & Partial<AssetReceipt>)
  | {
      status: "rejected" | "stale" | "unavailable";
      reason: { code: string; upstream_step?: string };
    };

export type ManualQuestionCreationRawView = {
  schema_ref: "meta-research/manual-question-creation/v1";
  context_ref: string;
  creation_mode: "ManualCreation";
  generation: number;
  quest_ref: string;
  quest_initialization_id: string;
  parent_question_ref: string;
  status:
    | "draft"
    | "seed_confirmed"
    | "research_pending"
    | "research_ready"
    | "confirmed"
    | "recovering"
    | "completed"
    | "cancelled";
  seed: null | {
    ref: string;
    hash: string;
    value: {
      intent: string;
      fields: QuestionContent;
      accepted_material_bindings: ManualAcceptedMaterialBinding[];
      deepfetch_preference: "use" | "skip" | "later";
    };
    receipt: AssetReceipt & { status: "accepted" };
  };
  research_path: {
    status: "not_selected" | "pending" | "queued" | "running" | "ready" | "waived" | "failed" | "cancelled";
    basis_hash?: string | null;
    deepfetch: null | {
      request_ref: string;
      status: "pending" | "queued" | "running" | "succeeded" | "failed" | "cancelled";
      run_ref: string | null;
      snapshot_ref: string | null;
      literature_snapshot?: null | {
        snapshot_ref?: string;
        request_ref?: string;
        creation_context_kind?: "manual_question_creation";
        creation_context_ref?: string;
        quest_ref?: string;
        receipt: AssetReceipt & { status: "accepted" };
      };
      failure: null | { code: string };
    };
    waiver: null | {
      status: "accepted";
      ref: string;
      decision_hash: string;
      hash: string;
      receipt: AssetReceipt & { status: "accepted" };
    };
  };
  proposal: null | {
    ref: string;
    revision: number;
    hash: string;
    basis_hash: string;
    content: QuestionContent;
    status: "current" | "confirmed" | "stale";
  };
  confirmation: null | {
    proposal_ref: string;
    proposal_hash: string;
    hash: string;
    receipt: AssetReceipt & { status: "accepted" };
  };
  drafting_session: null | {
    ref: string;
    status: "open" | "closed";
    turns: Array<{
      ref: string;
      ordinal: number;
      basis_hash: string;
      user_content: string;
      assistant_status: "queued" | "running" | "completed" | "unavailable" | "failed";
      assistant_content: string | null;
      reason: null | { code: string };
    }>;
  };
  receipts: {
    seed: ManualRawReceiptState;
    research: ManualRawReceiptState;
    confirmation: ManualRawReceiptState;
    content: ManualRawReceiptState;
    question: ManualRawReceiptState;
  };
  recovery: null | {
    first_missing_step: string | null;
    attempt_count: number;
    reason: null | { code: string };
    next_retry_at: number | null;
  };
  question_anchor: null | {
    question_ref: string;
    quest_ref: string;
    parent_question_ref: string;
    content_ref: string;
    content_hash: string;
    schema_ref: string;
    content_receipt_ref: string;
    question_receipt_ref: string;
  };
  cancellation: null | (AssetReceipt & { status: "accepted" });
  capabilities: {
    manual_creation: QuestCapability;
    deepfetch: QuestCapability;
    explicit_waiver: QuestCapability;
  };
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

export type PlanIdeaSetSummary = {
  ref?: string;
  binding_ref?: string;
  idea_set_ref?: string;
  outcome_ref?: string;
  content_ref?: string;
  content_hash?: string;
  schema_ref?: string;
  candidate_count?: number;
  content_receipt?: IdeaReceipt | null;
  domain_receipt?: IdeaReceipt | null;
  stage_commit_receipt?: IdeaReceipt | null;
  [key: string]: unknown;
};

export type PlanStageProjection = {
  eligibility: {
    status: string;
    cycle_ref?: string;
    question_ref?: string;
    idea_outcome_ref?: string;
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
    accepted_question_binding?: NonNullable<
      IdeaStageProjection["stage_run_request"]
    >["accepted_question_binding"];
    accepted_idea_set_binding?: PlanIdeaSetSummary | null;
    context_pack_ref?: string;
    context_pack_hash?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  run: IdeaStageProjection["run"];
  plan_acceptance: {
    status: string;
    plan_document_ref?: string | null;
    formal_plan_ref?: string | null;
    outcome_ref?: string | null;
    content: IdeaAcceptanceFact;
    domain: IdeaAcceptanceFact;
    bundle_disposition?: "experiments_required" | "no_new_experiment_required" | string;
    answer_contract_hash?: string | null;
    gap_count?: number;
    experiment_brief_count?: number;
    rejection?: null | {
      code?: string;
      message?: string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  stage_commit: IdeaStageProjection["stage_commit"];
  [key: string]: unknown;
};

export type BundleTargetProjection = {
  target_ref: string;
  target_key: string;
  spec_hash: string;
  dependency_refs: string[];
  target_run_ref?: string | null;
  status: string;
  receipt?: IdeaReceipt | null;
  blocker?: null | { code?: string; [key: string]: unknown };
  [key: string]: unknown;
};

export type BundleTargetCommitProjection = {
  status: "realized" | string;
  commit_ref: string;
  target_ref: string;
  target_run_ref: string;
  evaluation_attempt_ref: string;
  target_spec_hash: string;
  closure_hash: string;
  closure: Record<string, unknown>;
  result_disposition: string;
  receipt?: IdeaReceipt | null;
  [key: string]: unknown;
};

export type TargetRootObservation = {
  event_ref: string;
  cursor: string;
  operation_ref: string;
  operation_generation: number;
  sequence: number;
  kind: string;
  stream: string;
  text: string;
  recorded_at: number;
  redacted: boolean;
  truncated: boolean;
  dropped_bytes: number;
  dropped_events: number;
};

export type TargetRootObservationPage = {
  target_ref: string;
  target_run_ref: string;
  attempt_ref: string;
  attempt_generation: number;
  root_session_ref: string;
  native_session_ref: string | null;
  fence_ref: string;
  stream_ref: string;
  status: string;
  items: TargetRootObservation[];
  next_cursor: string | null;
  head_cursor: string | null;
  has_more: boolean;
  observation_only: true;
};

export type TargetRootObservationPointer = {
  target_ref: string;
  target_run_ref: string;
  stream_ref: string;
  head_cursor: string;
};

export type BundleExhaustionProjection = {
  kind: "BundleExhaustion";
  status: string;
  operation_ref: string;
  proposal_identity: string;
  proposal_hash: string;
  proposal_ref: string;
  decision_receipt: IdeaReceipt;
  evidence?: Record<string, unknown> | null;
  basis_kind?: string | null;
  basis_ref?: string | null;
  basis_receipt?: IdeaReceipt | null;
  [key: string]: unknown;
};

export type BundleStageProjection = {
  eligibility: {
    status: string;
    cycle_ref?: string | null;
    question_ref?: string | null;
    formal_plan_ref?: string | null;
    reason?: null | { code?: string; message?: string; [key: string]: unknown };
    next_stage?: string | null;
    [key: string]: unknown;
  };
  stage_run_request: null | {
    status?: string;
    request_ref?: string;
    cycle_ref?: string;
    stage?: string;
    epoch?: number;
    accepted_question_binding?: NonNullable<
      IdeaStageProjection["stage_run_request"]
    >["accepted_question_binding"];
    accepted_formal_plan_binding?: Record<string, unknown> | null;
    context_pack_ref?: string;
    context_pack_hash?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  run: IdeaStageProjection["run"];
  target_graph: {
    status: string;
    graph_ref?: string;
    formal_plan_ref?: string;
    target_plan_hash?: string;
    targets: BundleTargetProjection[];
    frontier: string[];
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  target_commits: BundleTargetCommitProjection[];
  baseline_pool: Array<{
    target_commit_ref: string;
    target_ref: string;
    result_disposition: string;
    metric_result?: Record<string, unknown>;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  }>;
  bundle_exhaustion?: BundleExhaustionProjection;
  disposition: {
    status: string;
    report_disposition?: "exhausted" | string | null;
    operation_ref?: string;
    proposal_ref?: string;
    decision_receipt?: IdeaReceipt | null;
    basis_receipt?: IdeaReceipt | null;
    target_count?: number;
    target_commit_count?: number;
    reason?: { code?: string; [key: string]: unknown };
    [key: string]: unknown;
  };
  stage_commit: null | (NonNullable<IdeaStageProjection["stage_commit"]> & {
    target_commit_refs?: string[];
    disposition?: string;
    basis_kind?: string | null;
    basis_ref?: string | null;
    basis_receipt?: IdeaReceipt | null;
  });
  [key: string]: unknown;
};

export type ReasoningStageProjection = {
  eligibility: {
    status: string;
    cycle_ref?: string | null;
    question_ref?: string | null;
    reason?: null | { code?: string; message?: string; [key: string]: unknown };
    next_stage?: string | null;
    [key: string]: unknown;
  };
  stage_run_request: null | {
    status?: string;
    request_ref?: string;
    cycle_ref?: string;
    stage?: string;
    epoch?: number;
    accepted_question_binding?: NonNullable<
      IdeaStageProjection["stage_run_request"]
    >["accepted_question_binding"];
    context_pack_ref?: string;
    context_pack_hash?: string;
    context_pack?: {
      question_literature_input?: Record<string, unknown>;
      upstream_stage_closure?: Array<Record<string, unknown>>;
      plan_evidence_input?: Record<string, unknown>;
      accepted_target_commit_closures?: Array<Record<string, unknown>>;
      research_context?: Record<string, unknown>;
      [key: string]: unknown;
    };
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  run: IdeaStageProjection["run"];
  reasoning_acceptance: {
    status: string;
    disposition?:
      | "affirmed"
      | "denied"
      | "uncertain"
      | "insufficient_evidence"
      | string;
    outcome_ref?: string | null;
    content: IdeaAcceptanceFact;
    domain: IdeaAcceptanceFact;
    [key: string]: unknown;
  };
  transition: {
    status: string;
    schema_ref?: string;
    kind?: "NextCycleProposal" | "CandidateCompletion" | string;
    ref?: string;
    hash?: string;
    is_authoritative?: boolean;
    [key: string]: unknown;
  };
  stage_commit: null | (NonNullable<IdeaStageProjection["stage_commit"]> & {
    disposition?: string;
    transition_kind?: string;
    transition_ref?: string;
  });
  [key: string]: unknown;
};

export type AutonomousCreationView = {
  context_ref: string;
  generation: number;
  creation_mode: "AutonomousCreation";
  status: string;
  checkpoint: { ref: string; hash: string };
  source: {
    quest_ref?: string;
    cycle_ref?: string;
    reasoning_stage_run_request_ref?: string;
    scientific_outcome_ref?: string;
    question_ref?: string;
    foreground_epoch?: number;
    reasoning_checkpoint_ref?: string;
    reasoning_checkpoint_hash?: string;
    autonomous_scope_content_acceptance_receipt_ref?: string;
    preliminary_scientific_acceptance_receipt_ref?: string;
    [key: string]: unknown;
  };
  scope: {
    creation_mode?: "AutonomousCreation";
    question_blueprint?: Record<string, unknown>;
    [key: string]: unknown;
  };
  proposal: null | {
    creation_mode?: "AutonomousCreation";
    question?: Record<string, unknown>;
    [key: string]: unknown;
  };
  deepfetch: {
    required: true;
    waiver_allowed: false;
    human_authorization_required: false;
    authorization_receipt_ref?: string | null;
    status: string;
    request_ref?: string | null;
    run_ref?: string | null;
    literature_snapshot_ref?: string | null;
    request_receipt?: IdeaReceipt | null;
    literature_snapshot_receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  waiver: null;
  human_confirmation: null;
  human_request?: Record<string, unknown> | null;
  content_acceptance: {
    status: string;
    content_ref?: string;
    content_hash?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  dispatch_eligibility?: Record<string, unknown>;
  question_anchor: Record<string, unknown> | null;
  graph_presence_fact: Record<string, unknown> | null;
  question_research_state_fact: Record<string, unknown> | null;
  literature_revision: Record<string, unknown> | null;
  next_cycle_proposal: null;
  successor_cycle: null;
  [key: string]: unknown;
};

export type AutonomousCreationProjection = {
  status: "ready";
  creation_mode: "AutonomousCreation";
  current: AutonomousCreationView | null;
};

export type QuestCompletionView = {
  context_ref: string;
  status: string;
  quest: { quest_ref: string; status: "active" | "ended" };
  candidate_completion_ref: string;
  candidate_completion_hash: string;
  candidate_completion: {
    completion_milestone_basis_refs?: string[];
    [key: string]: unknown;
  };
  source: {
    quest_ref?: string;
    cycle_ref?: string;
    reasoning_stage_run_request_ref?: string;
    scientific_outcome_ref?: string;
    foreground_epoch?: number;
    reasoning_content_acceptance_receipt_ref?: string;
    reasoning_domain_acceptance_receipt_ref?: string;
    [key: string]: unknown;
  };
  goal_revision: {
    goal_revision_ref?: string;
    goal?: string;
    completion_criteria?: string;
    [key: string]: unknown;
  };
  human_confirmation: {
    status: string;
    preview: null | {
      status: "current" | string;
      ref: string;
      hash: string;
      candidate_completion_ref?: string;
      candidate_completion_hash?: string;
      quest_ref?: string;
      goal_revision_ref?: string;
      completion_milestone_basis_refs?: string[];
      [key: string]: unknown;
    };
    decision: null | {
      decision: "confirmed" | "rejected" | string;
      receipt?: IdeaReceipt | null;
      [key: string]: unknown;
    };
  };
  domain_acceptance: {
    status: string;
    completion_ref?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  ending_transition: null | {
    status?: string;
    transition_ref?: string;
    receipt?: IdeaReceipt | null;
    [key: string]: unknown;
  };
  successor_cycle: null;
  [key: string]: unknown;
};

export type QuestCompletionProjection = {
  status: "ready";
  current: QuestCompletionView | null;
};

export type ExperimentObservation = {
  event_ref: string;
  sequence: number;
  attempt_ref: string;
  fence_ref: string;
  kind: "status" | "stdout" | "telemetry" | string;
  payload: Record<string, unknown>;
  observed_at: number;
};

export type ExperimentStdoutObservation = {
  mode?: string;
  complete?: boolean;
  truncated?: boolean;
  dropped?: number;
  first_sequence?: number | null;
  last_sequence?: number | null;
  observed_at?: number | string | null;
};

export type ExperimentExecutionProjection = {
  status: string;
  managed_status?: string;
  run_ref?: string;
  execution_request_ref?: string;
  attempt_ref?: string;
  attempt_generation?: number;
  root_session_ref?: string;
  fence_ref?: string;
  fence_status?: string;
  runtime_binding_hash?: string;
  events?: ExperimentObservation[];
  stdout_observation?: ExperimentStdoutObservation;
  execution_receipt?: IdeaReceipt | null;
  failure?: null | { code?: string; [key: string]: unknown };
  [key: string]: unknown;
};

export type ExperimentProjection = {
  intent: {
    execution_request_ref?: string;
    quest_ref?: string;
    title?: string;
    hypothesis?: string;
    [key: string]: unknown;
  };
  identities: {
    baseline_ref?: string;
    variant_ref?: string;
    evaluation_protocol_ref?: string;
    protocol_version_ref?: string;
    evaluation_ref?: string;
    variant_run_ref?: string;
    evaluation_attempt_ref?: string;
    [key: string]: unknown;
  };
  execution: ExperimentExecutionProjection;
  execution_request?: Record<string, unknown>;
  frozen_inputs?: Record<string, unknown>;
  assets?: Record<string, unknown>;
  formal_measurement?: Record<string, unknown>;
  [key: string]: unknown;
};

export type PublicExperimentProjection = {
  status: "idle" | "active";
  current: ExperimentProjection | null;
};

export type WritingReceipt = AssetReceipt & { status?: "accepted" };
export type WritingDocumentType = "report" | "paper" | "presentation";

export type WritingProviderCapability = {
  provider_ref: string;
  production_ready: boolean;
  supported_actions: Array<"publish" | "overwrite" | "delete" | "send" | "submit">;
};

export type WritingDeliveryTarget = {
  path: string;
  permissions: 384;
  expected_existing_hash: string | null;
} | {
  target_ref: string;
  permissions: string[];
  expected_existing_hash: string | null;
};

export type WritingDeliveryPayload = {
  schema_ref: string;
  request_nonce: string;
  operation_ref: string;
  action: "publish" | "overwrite" | "delete" | "send" | "submit";
  provider_ref: string;
  target: WritingDeliveryTarget;
  effects: Array<Record<string, unknown>>;
  run_ref: string;
  document_type: WritingDocumentType;
  asset_ref: string;
  version_ref: string;
  content_hash: string;
  manifest_hash: string;
  version_receipt: WritingReceipt;
  citation_decision_ref: string;
  citation_receipt: WritingReceipt;
  renderer_asset_ref: string;
  renderer_version_ref: string;
  renderer_content_hash: string;
  renderer_manifest_hash: string;
  renderer_artifact_sha256: string;
  renderer_format: string;
  renderer_media_type: string;
  renderer_receipt: WritingReceipt;
};

export type WritingDeliveryOperation = {
  operation_ref: string;
  payload: WritingDeliveryPayload;
  payload_hash: string;
  status: "not_attempted" | "partial" | "outcome_unknown" | "completed";
  authority_status: "admitted" | "executing" | "partial" | "outcome_unknown" | "completed";
  attempt_count: number;
  provider_operation_ref: string;
  provider_request_hash: string | null;
  operation_receipt: WritingReceipt;
  execution_receipt: WritingReceipt | null;
  reconciliation_receipt: WritingReceipt | null;
  provider_observations: Array<{
    observation_ref: string;
    provider_ref: string;
    provider_operation_ref: string;
    outcome: string;
    observed_at: number;
    details: Record<string, unknown>;
    observation_hash: string;
  }>;
  failure?: { code: string };
};

export type WritingDeliveryView = {
  intent_id: string;
  status: "not_attempted" | "partial" | "outcome_unknown" | "completed";
  confirmation_status: "draft" | "previewed" | "confirmed";
  draft_revision: number;
  draft_hash: string;
  payload: WritingDeliveryPayload;
  impact_preview?: null | {
    preview_ref: string;
    preview_hash: string;
    draft_revision?: number;
    draft_hash?: string;
    status?: string;
    owner_previews?: Array<{
      source_owner: string;
      target_assertion: Record<string, unknown>;
      will_happen: string[];
      will_not_happen: string[];
      risks: string[];
      stale_conditions: string[];
      digest?: string;
    }>;
    target_assertion?: Record<string, unknown>;
    will_happen?: string[];
    will_not_happen?: string[];
    risks?: string[];
    stale_conditions?: string[];
  };
  confirmation_receipt?: WritingReceipt | null;
  operation?: WritingDeliveryOperation | null;
};

export type WritingVersion = {
  version_ref: string;
  asset_ref: string;
  version_number: number;
  content_hash: string;
  accepted_at: number;
  integrity: string;
  availability: string;
  citation_status: "accepted" | "rejected";
  citations: Array<{
    citation_ref: string;
    source_version_ref: string;
    locator: string;
    claim: string;
    source_quote: string;
  }>;
  citation_feedback: string[];
  deliverable_receipt: WritingReceipt;
  citation_receipt: WritingReceipt;
};

export type WritingReportView = {
  intent_id: string;
  status: "draft" | "previewed" | "confirmed" | "running" | "paused" | "blocked" | "cancelled" | "completed";
  document_type: WritingDocumentType;
  draft_revision: number;
  draft_hash: string;
  intent: {
    schema_ref: string;
    title: string;
    audience: string;
    purpose: string;
    instructions: string;
  };
  snapshot: {
    snapshot_ref: string;
    snapshot_hash: string;
    quest_ref: string;
    accepted_sources: Array<{ version_ref: string; [key: string]: unknown }>;
    [key: string]: unknown;
  };
  impact_preview: null | {
    preview_ref: string;
    preview_hash: string;
    status: string;
    snapshot_hash: string;
    target_assertion?: Record<string, unknown>;
    owner_revisions?: Record<string, number>;
    will_happen: string[];
    will_not_happen: string[];
    risks: string[];
    stale_conditions: string[];
  };
  confirmation_receipt: WritingReceipt | null;
  run: null | {
    run_ref: string;
    document_type: WritingDocumentType;
    status: "active" | "paused" | "blocked" | "cancelled" | "completed";
    attempt_ref: string;
    attempt_generation: number;
    content_revision: number;
    root_session_ref: string;
    native_session_ref: string | null;
    fence_ref: string;
    runtime_binding_hash: string;
  };
  execution: Record<string, unknown> & { status: string; receipt?: WritingReceipt | null };
  deliverable: Record<string, unknown> & {
    status: string;
    version_ref?: string;
    asset_ref?: string;
    version_number?: number;
    acceptance_status?: "accepted";
    integrity?: string;
    availability?: string;
    failure?: null | { code: string };
    receipt?: WritingReceipt;
  };
  citation: Record<string, unknown> & {
    status: string;
    feedback?: string[];
    receipt?: WritingReceipt;
  };
  renderer: {
    status: string;
    reason?: { code: string };
    default_format?: string;
    formats?: string[];
    artifact?: Record<string, unknown> & {
      version_ref?: string;
      content_hash?: string;
      artifact_sha256?: string;
    };
  };
  deliveries?: WritingDeliveryView[];
  versions?: WritingVersion[];
};

export type WritingOverview = {
  status: "ready";
  document_types: WritingDocumentType[];
  delivery_capabilities?: {
    providers: WritingProviderCapability[];
    renderers: Array<{
      document_type: WritingDocumentType;
      default_format: string;
      formats: string[];
    }>;
  };
  runs: WritingReportView[];
};

export type WritingComparison = {
  run_ref: string;
  left_version_ref: string;
  right_version_ref: string;
  content: {
    changed: boolean;
    left_hash: string;
    right_hash: string;
    unified_diff: string;
  };
  evidence: {
    changed: boolean;
    left_source_version_refs: string[];
    right_source_version_refs: string[];
    added_source_version_refs: string[];
    removed_source_version_refs: string[];
  };
  citation: {
    changed: boolean;
    left_status: string;
    right_status: string;
    left_citations: WritingVersion["citations"];
    right_citations: WritingVersion["citations"];
    added_citation_refs: string[];
    removed_citation_refs: string[];
    changed_citations: Array<{
      citation_ref: string;
      left: WritingVersion["citations"][number];
      right: WritingVersion["citations"][number];
    }>;
  };
  snapshot: {
    mode: "frozen";
    snapshot_ref: string;
    snapshot_hash: string;
  };
};

export type WritingRender = {
  version_ref: string;
  render_hash: string;
  content: string;
};

export type WritingVersionContent = {
  version_ref: string;
  content_hash: string;
  citation_status: string;
  formal_renderer: false;
  content: string;
};

export type WritingCancellationPreview = {
  intent_id: string;
  draft_revision: number;
  draft_hash: string;
  impact_preview: null | {
    preview_ref: string;
    preview_hash: string;
    will_happen: string[];
    will_not_happen: string[];
    risks: string[];
    stale_conditions: string[];
  };
};

export type HarnessCapabilityState = {
  status: "available" | "capability_unavailable";
  evidence_refs: string[];
  reason?: { code: string };
};

export type HarnessCapabilityProfile = {
  schema_ref: "meta-research/harness-capability-profile/v1";
  harness_family: "codex" | "claude";
  locked_version: string;
  provider_version: string;
  native_session_ref: string;
  capabilities: Record<string, HarnessCapabilityState>;
  run_ref: string;
  attempt_ref: string;
  root_session_ref: string;
  fence_ref: string;
  provider_operation_ref: string;
  provider_operation_refs: string[];
  provider_transport_receipts: Array<{
    provider_operation_ref: string;
    schema_ref: "meta-research/harness-provider-transport-receipt/v1";
    spool_ref: string;
    transport_invocation_hash: string;
    supervisor_receipt_hash: string;
    termination_reason: string;
    provider_returncode: number;
  }>;
  status: "executed";
};

export type HarnessAdapterStatus = {
  harness_family: "codex" | "claude";
  locked_version: string;
  provider_version?: string;
  status: "ready" | "capability_unavailable";
  installation_status: "ready" | "capability_unavailable";
  reason?: { code: string };
  capability_profile: HarnessCapabilityProfile | null;
  provider_operation: {
    operation_ref: string;
    generation: number;
    status: "running" | "executed" | "failed" | "unknown_outcome";
    outcome_code: string | null;
  } | null;
  missing_reason: { code: string } | null;
};

export type HarnessStatus = {
  status: "ready" | "capability_unavailable";
  reason?: { code: string };
  gateway: null | {
    status: "ready";
    server_instance_ref: string;
    deployment_profile: "local_resident_streamable_http";
    transport: "streamable_http";
    protocol_version: string;
    catalog_revision: number;
    catalog_hash: string;
    operation_ids: string[];
    health_receipt: {
      issuer: "semantic_mcp_gateway";
      kind: "resident_health";
      receipt_ref: string;
      subject_ref: string;
      payload_hash: string;
    };
  };
  adapters: HarnessAdapterStatus[];
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
    current_quest: {
      status: "ready" | "not_bound" | "unavailable";
      quest_ref: string | null;
      goal_revision_ref: string | null;
      draft_revision: number | null;
      draft_hash: string | null;
      goal: string | null;
      completion_criteria: string | null;
      projection_digest: string | null;
      reason: { code: string } | null;
    };
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
  question_tree: QuestionTreeProjection;
  manual_question_creation: ManualQuestionCreationCapability;
  research_assets: ResearchAssetsView;
  idea_stage?: IdeaStageProjection | null;
  human_collaboration?: HumanCollaborationProjection;
  research_control: ResearchControlProjection;
  plan_stage?: PlanStageProjection | null;
  bundle_stage?: BundleStageProjection | null;
  reasoning_stage?: ReasoningStageProjection | null;
  autonomous_creation: AutonomousCreationProjection;
  quest_completion: QuestCompletionProjection;
  experiment: PublicExperimentProjection;
  writing: WritingOverview;
  harnesses: HarnessStatus;
  runtime_observability?: RuntimeObservability;
  unavailable: UnavailableCapability[];
};

export type RuntimeObservability = {
  status: "ready" | "unavailable";
  reason?: { code: string } | null;
  inhibitor?: {
    status: string;
    backend: string;
    scope: string;
    active_count: number;
    reason?: { code: string } | null;
  };
  responsibilities?: Array<{
    owner_scope: string;
    effect_kind: string;
  }>;
  durable_waiting?: Array<{
    effect_kind: string;
    reason: { code: string };
  }>;
  durable_waiting_count?: number;
  durable_waiting_page_truncated?: boolean;
  interruptions?: Array<{
    kind: string;
    reason: { code: string };
    reconciliation_status: string;
  }>;
  interruption_count?: number;
  interruption_page_truncated?: boolean;
  log?: {
    status: "empty" | "fresh" | "stale";
    age_seconds?: number;
  };
  telemetry?: {
    mode: "disabled" | "active" | "revocation_pending" | "revoked";
  };
};

function adaptManualReceipt(receipt: ManualRawReceiptState): ManualCreationReceiptState {
  if (receipt.status === "accepted") {
    if (
      typeof receipt.issuer !== "string" ||
      typeof receipt.kind !== "string" ||
      typeof receipt.receipt_ref !== "string" ||
      typeof receipt.subject_ref !== "string" ||
      typeof receipt.payload_hash !== "string"
    ) {
      return {
        status: "unavailable",
        reason: { code: "accepted_receipt_identity_unavailable" },
      };
    }
    return {
      status: "accepted",
      issuer: receipt.issuer,
      kind: receipt.kind,
      receipt_ref: receipt.receipt_ref,
      subject_ref: receipt.subject_ref,
      payload_hash: receipt.payload_hash,
    };
  }
  if (["rejected", "stale", "unavailable"].includes(receipt.status)) {
    const rejected = receipt as Extract<
      ManualRawReceiptState,
      { status: "rejected" | "stale" | "unavailable" }
    >;
    return { status: rejected.status, reason: { ...rejected.reason } };
  }
  return {
    status: "not_attempted",
    reason: receipt.reason
      ? { ...receipt.reason }
      : receipt.status === "pending"
        ? { code: "owner_receipt_pending" }
        : undefined,
  };
}

function adaptManualContent(content: QuestionContent): ManualQuestionContent {
  return {
    title: content.title,
    unknown_statement: content.unknown_statement,
    answer_shape: content.answer_shape,
    applicability_scope: content.applicability_scope,
    background_context: content.background_context,
    requirements_constraints: content.requirements_constraints,
  };
}

function adaptManualMaterialBinding(
  binding: ManualAcceptedMaterialBinding,
): ManualCreationAcceptedMaterialBindingView {
  return {
    asset_ref: binding.asset_ref,
    version_ref: binding.version_ref,
    content_hash: binding.content_hash,
    manifest_hash: binding.manifest_hash,
    receipt: {
      status: "accepted",
      issuer: binding.receipt.issuer,
      kind: binding.receipt.kind,
      receipt_ref: binding.receipt.receipt_ref,
      subject_ref: binding.receipt.subject_ref,
      payload_hash: binding.receipt.payload_hash,
    },
  };
}

function adaptManualStatus(
  status: ManualQuestionCreationRawView["status"],
): ManualQuestionCreationView["status"] {
  const statuses: Record<
    ManualQuestionCreationRawView["status"],
    ManualQuestionCreationView["status"]
  > = {
    draft: "seed_draft",
    seed_confirmed: "seed_confirmed",
    research_pending: "drafting",
    research_ready: "proposal_ready",
    confirmed: "confirming",
    recovering: "recovering",
    completed: "completed",
    cancelled: "cancelled",
  };
  return statuses[status];
}

function adaptManualTurns(
  session: ManualQuestionCreationRawView["drafting_session"],
): ManualCreationDraftingTurn[] {
  if (!session) return [];
  return session.turns.flatMap((turn) => {
    const rows: ManualCreationDraftingTurn[] = [
      {
        turn_ref: `${turn.ref}:user`,
        role: "user",
        content: turn.user_content,
        status: "completed",
      },
    ];
    if (turn.assistant_content !== null) {
      rows.push({
        turn_ref: `${turn.ref}:assistant`,
        role: "assistant",
        content: turn.assistant_content,
        status: ["queued", "running", "completed"].includes(turn.assistant_status)
          ? turn.assistant_status as "queued" | "running" | "completed"
          : "failed",
      });
    }
    return rows;
  });
}

export function adaptManualQuestionCreation(
  raw: ManualQuestionCreationRawView,
  labels: {
    quest_title?: string | null;
    parent_question_title?: string | null;
    research_receipt?: (AssetReceipt & { status: "accepted" }) | null;
  } = {},
): ManualQuestionCreationView {
  const turns = adaptManualTurns(raw.drafting_session);
  const assistantWaiting = raw.drafting_session?.turns.some((turn) =>
    ["queued", "running"].includes(turn.assistant_status),
  ) ?? false;
  const rawDeepFetch = raw.research_path.deepfetch;
  const deepfetchStatus = rawDeepFetch?.status === "succeeded"
    ? "completed"
    : rawDeepFetch?.status === "pending"
      ? "queued"
      : rawDeepFetch?.status ?? "not_started";
  const deepfetch = rawDeepFetch === null
    ? null
    : {
        status: deepfetchStatus,
        run_ref: rawDeepFetch.run_ref,
        receipt: adaptManualReceipt(
          rawDeepFetch.literature_snapshot?.receipt
            ?? labels.research_receipt
            ?? raw.receipts.research,
        ),
        reason: rawDeepFetch.failure,
      } as ManualQuestionCreationView["research"]["deepfetch"];
  const failure = raw.recovery?.reason ?? (
    raw.capabilities.manual_creation.status === "ready"
      ? null
      : raw.capabilities.manual_creation.reason
  );

  return {
    creation_id: raw.context_ref,
    status: adaptManualStatus(raw.status),
    quest_ref: raw.quest_ref,
    quest_title: labels.quest_title ?? null,
    parent_question_ref: raw.parent_question_ref,
    parent_question_title: labels.parent_question_title ?? null,
    seed: {
      ref: raw.seed?.ref ?? null,
      hash: raw.seed?.hash ?? null,
      value: raw.seed
        ? {
            intent: raw.seed.value.intent,
            fields: adaptManualContent(raw.seed.value.fields),
            accepted_material_bindings: raw.seed.value.accepted_material_bindings.map(
              adaptManualMaterialBinding,
            ),
            deepfetch_preference: raw.seed.value.deepfetch_preference,
          }
        : null,
      receipt: adaptManualReceipt(raw.receipts.seed),
    },
    research: {
      decision: raw.research_path.waiver
        ? "waiver"
        : raw.research_path.deepfetch
          ? "deepfetch"
          : "undecided",
      basis_hash: raw.research_path.basis_hash ?? null,
      deepfetch,
      waiver_receipt: raw.research_path.waiver
        ? adaptManualReceipt(raw.research_path.waiver.receipt)
        : { status: "not_attempted" },
    },
    drafting_session: {
      session_ref: raw.drafting_session?.ref ?? null,
      status: raw.drafting_session === null
        ? "inactive"
        : raw.drafting_session.status === "closed"
          ? "closed"
          : assistantWaiting
            ? "waiting"
            : "ready",
      turns,
    },
    proposal: raw.proposal
      ? {
          ref: raw.proposal.ref,
          hash: raw.proposal.hash,
          content: adaptManualContent(raw.proposal.content),
          status: raw.proposal.status,
        }
      : null,
    receipts: {
      proposal_confirmation: adaptManualReceipt(raw.receipts.confirmation),
      question_content: adaptManualReceipt(raw.receipts.content),
      question_identity: adaptManualReceipt(raw.receipts.question),
    },
    question_anchor: raw.question_anchor
      ? {
          ref: raw.question_anchor.question_ref,
          question_ref: raw.question_anchor.question_ref,
          content_ref: raw.question_anchor.content_ref,
          content_hash: raw.question_anchor.content_hash,
        }
      : null,
    failure: failure ? { code: failure.code } : null,
  };
}
export type CompanionMessage = {
  message_ref?: string;
  scope_ref?: string | null;
  role: "user" | "assistant" | "system";
  content?: string;
  message?: string;
  text?: string;
  status?: "queued" | "processing" | "running" | "completed" | "failed";
  created_at?: number;
  reason?: { code?: string } | null;
};

export type CompanionSoftConstraint = {
  constraint_ref?: string;
  scope_ref?: string | null;
  source_proposal_ref?: string | null;
  revision?: number;
  guidance?: Record<string, unknown>;
  text?: string;
  content?: string;
  status: "active" | "withdrawn" | "expired" | "superseded";
  receipt_ref?: string;
};

export type CompanionAgentProposal = {
  proposal_ref?: string;
  scope_ref?: string | null;
  proposal?: Record<string, unknown>;
  proposal_hash?: string;
  title?: string;
  summary?: string;
  content?: string;
  status?: string;
  kind?: string;
  impact_preview?: HumanRequestImpactPreview | null;
};

export type HumanRequestWaiter = {
  waiter_ref: string;
  generation?: number;
  wait_scope: "local" | "quest";
  status?: "blocked" | "released" | "cancelled" | "consumed";
  other_blockers?: string[];
  target_assertion?: Record<string, unknown>;
  resume_validation?: null | {
    validation_ref: string;
    request_ref: string;
    waiter_ref: string;
    generation: number;
    target_assertion_hash: string;
    authorization_receipt_ref?: string | null;
    other_blockers: string[];
    status: "released" | "blocked";
    reason?: { code?: string } | null;
    started_work: boolean;
    consumption?: null | {
      consumption_ref: string;
      request_ref: string;
      waiter_ref: string;
      generation: number;
      validation_ref: string;
      work_ref: string;
      work_hash: string;
      receipt: AssetReceipt;
      created_at: number;
    };
    created_at: number;
  };
};

export type HumanRequestImpactPreview = {
  preview_ref?: string;
  preview_hash?: string;
  target_assertion?: Record<string, unknown>;
  will_change?: string[];
  will_not_change?: string[];
  risks?: string[];
  stale_conditions?: string[];
};

export type HumanRequestItem = {
  request_ref: string;
  request_id: string;
  revision: number;
  issuer: string;
  quest_ref?: string | null;
  kind:
    | "library_reconnect"
    | "external_material_api_access"
    | "offline_action"
    | "capability_authorization";
  status: "open" | "satisfied" | "declined" | "withdrawn" | "expired" | "superseded";
  obligation: string;
  business_purpose?: string;
  target_assertion?: Record<string, unknown>;
  acceptance_conditions?: string[];
  required_authorization?: Record<string, unknown> | null;
  impact_preview?: HumanRequestImpactPreview | null;
  direct_waiters?: HumanRequestWaiter[];
  responses?: Array<Record<string, unknown>>;
  evaluation?: Record<string, unknown> | null;
  disposition?: Record<string, unknown> | null;
};

export type HumanRequestResponseBody = {
  decision: "provided" | "declined" | "deferred";
  facts: Record<string, unknown>;
  note: string;
};

export type PendingHumanRequestResponse = {
  schema: "meta-research/human-request-response/v1";
  request_ref: string;
  response_path: string;
  sealed_response: PendingHumanRequestAssetResponse["sealed_response"];
  response_idempotency_key: string;
  response_write_slot: string;
};

export type PendingHumanRequestAssetResponse = {
  schema: "meta-research/human-request-asset-response/v1";
  request_ref: string;
  asset_job_ref: string;
  asset_intake_write_slot: string;
  fact_prefix: "material" | "result";
  accepted_asset: AcceptedHumanRequestAssetBinding;
  response_path: string;
  sealed_response: {
    algorithm: "AES-GCM";
    key_ref: string;
    iv_base64: string;
    ciphertext_ref: string;
    body_hash: string;
    binding_hash: string;
  };
  response_idempotency_key: string;
  response_write_slot: string;
};

export type AcceptedHumanRequestAssetBinding = {
  asset_ref: string;
  version_ref: string;
  memory_ref: string;
  content_hash: string;
  manifest_hash: string;
  receipt: AssetReceipt;
};

type PendingAcceptedHumanRequestAsset = {
  schema: "meta-research/human-request-accepted-asset/v1";
  request_ref: string;
  asset_job_ref: string;
  asset_intake_write_slot: string;
  fact_prefix: "material" | "result";
  accepted_asset: AcceptedHumanRequestAssetBinding;
};

type PendingHumanRequestAssetIntakeOperation = {
  schema: "meta-research/human-request-asset-intake/v1";
  request_ref: string;
  intake_path: "/api/v1/research-assets/intakes";
  asset_idempotency_key: string;
  asset_write_slot: string;
  sealed_operation: PendingHumanRequestAssetResponse["sealed_response"];
};

type HumanRequestAssetIntakeOperationBody = {
  intake: AssetIntakeRequest;
  response: HumanRequestResponseBody;
  fact_prefix: "material" | "result";
};

export type HumanCapabilityCommandDraft = {
  command_kind: "capability_authorization";
  payload: {
    capability: string;
    decision: "granted" | "denied" | "revoked";
    scope: Record<string, unknown>;
  };
};

export type ResearchControlAction =
  | "pause"
  | "resume"
  | "normal_switch"
  | "forced_switch"
  | "cancel"
  | "abandon"
  | "prune"
  | "restore";

export type ResearchControlTarget = {
  quest_ref: string;
  cycle_ref: string;
  question_ref: string;
  epoch: number;
  target_scope?: "cycle" | "stage" | "run";
  run_ref?: string;
  target_question_ref?: string;
  prune_record_ref?: string;
};

export type ResearchControlCommandDraft = {
  command_kind: "research_control";
  payload: {
    action: ResearchControlAction;
    target: ResearchControlTarget;
    reason: string;
  };
};

export type HumanCommandDraft =
  | HumanCapabilityCommandDraft
  | ResearchControlCommandDraft;

export type HumanCommandOwnerPreview = {
  source_owner: string;
  target_assertion: Record<string, unknown>;
  will_happen: string[];
  will_not_happen: string[];
  risks: string[];
  stale_conditions: string[];
  digest: string;
};

export type HumanCommand = {
  intent_id: string;
  scope_ref: string;
  source_proposal_ref?: string | null;
  status: "draft" | "previewed" | "confirmed" | "cancelled";
  draft_revision: number;
  draft_hash: string;
  draft: HumanCommandDraft;
  executed: boolean;
  impact_preview: null | {
    preview_ref: string;
    preview_hash: string;
    draft_revision: number;
    draft_hash: string;
    owner_previews: HumanCommandOwnerPreview[];
    owner_revisions: Record<string, number>;
    status: "current" | "stale" | "consumed";
  };
  confirmation_receipt: null | AssetReceipt & { status: "accepted" };
  authorization?: HumanCapabilityAuthorization | null;
  control_execution?: null | {
    execution_ref: string;
    status: "completed";
    owner_receipts: Record<string, unknown>[];
    receipt_ref: string;
    receipt_hash: string;
  };
};

export type ManagedRunProjection = {
  run_ref: string;
  run_kind: string;
  quest_ref: string | null;
  cycle_ref: string | null;
  epoch: number | null;
  status: string;
  attempt_ref: string | null;
  root_session_ref: string | null;
  fence_ref: string | null;
  control_revision: number;
  safe_point_ref: string | null;
  terminal_reason: string | null;
  cleanup_status: "none" | "pending" | "completed";
  updated_at: number;
};

export type ResearchControlProjection = {
  status: "ready" | "idle" | "capability_unavailable";
  quest_ref: string | null;
  foreground: null | {
    quest_ref: string;
    cycle_ref: string;
    question_ref: string;
    stage: string;
    epoch: number;
    status: string;
    grant_ref: string;
    grant_status: string;
    safe_point_ref: string | null;
    pending_operation_ref: string | null;
    owner_revision: number;
  };
  managed_runs: ManagedRunProjection[];
  recovery_records: Array<{
    prune_record_ref: string;
    quest_ref: string;
    root_question_ref: string;
    affected_question_refs: string[];
    affected_question_count: number;
    receipt_ref: string;
    receipt_hash: string;
    created_at: number;
  }>;
  actions: ResearchControlAction[];
};

export type HumanCapabilityAuthorization = {
  authorization_ref: string;
  scope_ref: string;
  authorization_kind: "capability" | "broad_research";
  capability: string | null;
  decision: "granted" | "denied" | "revoked";
  status: string;
  requirement: Record<string, unknown>;
  policy: Record<string, unknown>;
  confirmation_receipt_ref: string;
  quest_ref: string | null;
  receipt_ref: string;
  receipt: AssetReceipt;
  created_at: number;
  is_current?: boolean;
  effective_decision?: "granted" | "denied" | "revoked";
};

export type HumanCollaborationProjection = {
  companion: {
    status: "ready" | "unavailable";
    scope_ref?: string | null;
    messages: CompanionMessage[];
    soft_constraints: CompanionSoftConstraint[];
    agent_proposals: CompanionAgentProposal[];
    reason?: { code?: string } | null;
  };
  human_requests: {
    status: "ready" | "unavailable";
    waiting: {
      scope: "none" | "local" | "quest";
      safe_meaningful_runnable_exists: boolean;
      other_blockers: string[];
    };
    items: HumanRequestItem[];
    reason?: { code?: string } | null;
  };
  commands: {
    status: "ready" | "unavailable";
    items: HumanCommand[];
    authorizations: HumanCapabilityAuthorization[];
    reason?: { code?: string } | null;
  };
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

export function fetchCurrentAutonomousCreation(
  signal?: AbortSignal,
): Promise<AutonomousCreationView | null> {
  return readJson("/api/v1/autonomous-creations/current", signal);
}

export function fetchCurrentQuestCompletion(
  signal?: AbortSignal,
): Promise<QuestCompletionView | null> {
  return readJson("/api/v1/quest-completions/current", signal);
}

export function startQuestCompletion(input: {
  source_outcome_ref: string;
  candidate_completion_ref: string;
}): Promise<QuestCompletionView> {
  return writeJson("/api/v1/quest-completions", "POST", input);
}

export function decideQuestCompletion(
  contextRef: string,
  input: {
    preview_ref: string;
    preview_hash: string;
    decision: "confirmed" | "rejected";
  },
): Promise<QuestCompletionView> {
  return writeJson(
    `/api/v1/quest-completions/${encodeURIComponent(contextRef)}/decision`,
    "POST",
    input,
  );
}

export function fetchTargetRootObservations(
  targetRef: string,
  options: {
    after?: string | null;
    limit?: number;
    signal?: AbortSignal;
  } = {},
): Promise<TargetRootObservationPage> {
  const limit = options.limit ?? 128;
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 256) {
    throw new ProductError("target_root_observation_limit_invalid");
  }
  const parameters = new URLSearchParams({ limit: String(limit) });
  if (options.after) parameters.set("after", options.after);
  return readJson(
    `/api/v1/bundle/targets/${encodeURIComponent(targetRef)}`
      + `/root-observations?${parameters}`,
    options.signal,
  );
}

export function fetchWriting(signal?: AbortSignal): Promise<WritingOverview> {
  return readJson("/api/v1/writing", signal);
}

export function createWritingIntent(input: {
  document_type?: WritingDocumentType;
  quest_ref: string;
  title: string;
  audience: string;
  purpose: string;
  instructions: string;
}): Promise<WritingReportView> {
  return writeJson("/api/v1/writing/intents", "POST", input);
}

export function previewWritingIntent(intentId: string): Promise<WritingReportView> {
  return writeJson(
    `/api/v1/writing/intents/${encodeURIComponent(intentId)}/preview`,
    "POST",
    {},
  );
}

export function confirmWritingIntent(
  report: WritingReportView,
): Promise<WritingReportView> {
  if (!report.impact_preview) throw new ProductError("writing_preview_required");
  return writeJson(
    `/api/v1/writing/intents/${encodeURIComponent(report.intent_id)}/confirmation`,
    "POST",
    {
      draft_revision: report.draft_revision,
      draft_hash: report.draft_hash,
      preview_ref: report.impact_preview.preview_ref,
      preview_hash: report.impact_preview.preview_hash,
    },
  );
}

export function controlWritingRun(
  runRef: string,
  action: "pause" | "resume",
  expectedAttemptRef: string,
  expectedFenceRef: string,
): Promise<WritingReportView> {
  return writeJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/control`,
    "POST",
    {
      action,
      expected_attempt_ref: expectedAttemptRef,
      expected_fence_ref: expectedFenceRef,
    },
  );
}

export function previewWritingCancellation(
  runRef: string,
): Promise<WritingCancellationPreview> {
  return writeJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/cancellation-intents`,
    "POST",
    {},
  );
}

export function confirmWritingCancellation(
  runRef: string,
  cancellation: WritingCancellationPreview,
): Promise<WritingReportView> {
  if (!cancellation.impact_preview) {
    throw new ProductError("writing_cancel_preview_required");
  }
  return writeJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/cancellation-intents/${encodeURIComponent(cancellation.intent_id)}/confirmation`,
    "POST",
    {
      draft_revision: cancellation.draft_revision,
      draft_hash: cancellation.draft_hash,
      preview_ref: cancellation.impact_preview.preview_ref,
      preview_hash: cancellation.impact_preview.preview_hash,
    },
  );
}

export function reviseWritingRun(
  runRef: string,
  feedback: string[],
): Promise<WritingReportView> {
  return writeJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/revisions`,
    "POST",
    { feedback },
  );
}

export function createWritingDeliveryIntent(
  runRef: string,
  input: {
    action: WritingDeliveryPayload["action"];
    provider_ref: string;
    target: WritingDeliveryTarget;
    output_format?: string;
  },
): Promise<WritingDeliveryView> {
  return writeJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/delivery-intents`,
    "POST",
    input,
  );
}

export function previewWritingDeliveryIntent(
  intentId: string,
): Promise<WritingDeliveryView> {
  return writeJson(
    `/api/v1/writing/delivery-intents/${encodeURIComponent(intentId)}/preview`,
    "POST",
    {},
  );
}

export function confirmWritingDeliveryIntent(
  delivery: WritingDeliveryView,
): Promise<WritingDeliveryView> {
  const preview = delivery.impact_preview;
  if (!preview) throw new ProductError("writing_delivery_preview_required");
  return writeJson(
    `/api/v1/writing/delivery-intents/${encodeURIComponent(delivery.intent_id)}/confirmation`,
    "POST",
    {
      draft_revision: delivery.draft_revision,
      draft_hash: delivery.draft_hash,
      preview_ref: preview.preview_ref,
      preview_hash: preview.preview_hash,
    },
  );
}

export function fetchWritingDeliveryOperation(
  operationRef: string,
  signal?: AbortSignal,
): Promise<WritingDeliveryOperation> {
  return readJson(
    `/api/v1/writing/deliveries/${encodeURIComponent(operationRef)}`,
    signal,
  );
}

export function compareWritingVersions(
  runRef: string,
  leftVersionRef: string,
  rightVersionRef: string,
  signal?: AbortSignal,
): Promise<WritingComparison> {
  const parameters = new URLSearchParams({
    left_version_ref: leftVersionRef,
    right_version_ref: rightVersionRef,
  });
  return readJson(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/compare?${parameters}`,
    signal,
  );
}

export function writingRenderUrl(
  runRef: string,
  versionRef?: string,
  outputFormat?: string,
): string {
  const parameters = new URLSearchParams({ format: outputFormat ?? "markdown" });
  if (versionRef) parameters.set("version_ref", versionRef);
  return `/api/v1/writing/runs/${encodeURIComponent(runRef)}/render?${parameters}`;
}

export async function fetchWritingRender(
  runRef: string,
  versionRef?: string,
  outputFormat?: string,
  signal?: AbortSignal,
): Promise<WritingRender> {
  const response = await fetch(writingRenderUrl(runRef, versionRef, outputFormat), {
    credentials: "same-origin",
    headers: { Accept: "text/markdown" },
    signal,
  });
  if (!response.ok) {
    throw new ProductError(`writing_render_unavailable:${response.status}`);
  }
  const renderedVersion = response.headers.get("X-Writing-Version-Ref");
  const renderHash = response.headers.get("X-Writing-Render-Hash");
  if (!renderedVersion || !renderHash) {
    throw new ProductError("writing_render_identity_missing");
  }
  return {
    version_ref: renderedVersion,
    render_hash: renderHash,
    content: await response.text(),
  };
}

export async function fetchWritingVersionContent(
  runRef: string,
  versionRef: string,
  signal?: AbortSignal,
): Promise<WritingVersionContent> {
  const response = await fetch(
    `/api/v1/writing/runs/${encodeURIComponent(runRef)}/versions/`
      + `${encodeURIComponent(versionRef)}/content`,
    {
      credentials: "same-origin",
      headers: { Accept: "text/markdown" },
      signal,
    },
  );
  if (!response.ok) {
    throw new ProductError(`writing_version_content_unavailable:${response.status}`);
  }
  const viewedVersion = response.headers.get("X-Writing-Version-Ref");
  const contentHash = response.headers.get("X-Writing-Content-Hash");
  const citationStatus = response.headers.get("X-Writing-Citation-Status");
  const formalRenderer = response.headers.get("X-Writing-Formal-Renderer");
  if (
    !viewedVersion
    || !contentHash
    || !citationStatus
    || formalRenderer !== "false"
  ) {
    throw new ProductError("writing_version_content_identity_missing");
  }
  return {
    version_ref: viewedVersion,
    content_hash: contentHash,
    citation_status: citationStatus,
    formal_renderer: false,
    content: await response.text(),
  };
}

export function fetchLiteratureSnapshot(
  snapshotRef: string,
  signal?: AbortSignal,
): Promise<LiteratureSnapshot> {
  return readJson(
    `/api/v1/literature-snapshots/${encodeURIComponent(snapshotRef)}`,
    signal,
  );
}

export function openManualQuestionCreation(
  questRef: string,
  parentQuestionRef: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson("/api/v1/manual-question-creations", "POST", {
    quest_ref: questRef,
    parent_question_ref: parentQuestionRef,
  });
}

export function fetchCurrentManualQuestionCreation(
  questRef: string,
  parentQuestionRef: string,
  signal?: AbortSignal,
): Promise<ManualQuestionCreationRawView | null> {
  const parameters = new URLSearchParams({
    quest_ref: questRef,
    parent_question_ref: parentQuestionRef,
  });
  return readJson(`/api/v1/manual-question-creations/current?${parameters}`, signal);
}

export function fetchManualQuestionCreation(
  contextRef: string,
  signal?: AbortSignal,
): Promise<ManualQuestionCreationRawView> {
  return readJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}`,
    signal,
  );
}

export function confirmManualCreationSeed(
  contextRef: string,
  seed: {
    intent: string;
    fields: ManualQuestionContent;
    accepted_material_bindings: ManualAcceptedMaterialBinding[];
    deepfetch_preference: "use" | "skip" | "later";
  },
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/seed-confirmation`,
    "POST",
    { seed },
  );
}

export function startManualCreationDeepFetch(
  contextRef: string,
  expectedSeedRef: string,
  expectedSeedHash: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/deepfetch`,
    "POST",
    {
      expected_seed_ref: expectedSeedRef,
      expected_seed_hash: expectedSeedHash,
    },
  );
}

export function confirmManualDeepFetchWaiver(
  contextRef: string,
  expectedSeedRef: string,
  expectedSeedHash: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/deepfetch-waiver`,
    "POST",
    {
      expected_seed_ref: expectedSeedRef,
      expected_seed_hash: expectedSeedHash,
    },
  );
}

export function sendManualDraftingMessage(
  contextRef: string,
  expectedBasisHash: string,
  message: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/drafting-session/messages`,
    "POST",
    { expected_basis_hash: expectedBasisHash, message },
  );
}

export function saveManualQuestionProposal(
  contextRef: string,
  input: {
    expected_basis_hash: string;
    expected_proposal_ref: string | null;
    expected_proposal_hash: string | null;
    content: ManualQuestionContent;
  },
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/proposal`,
    "PUT",
    input,
  );
}

export function confirmManualQuestionProposal(
  contextRef: string,
  proposalRef: string,
  proposalHash: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/proposal-confirmation`,
    "POST",
    { proposal_ref: proposalRef, proposal_hash: proposalHash },
  );
}

export function cancelManualQuestionCreation(
  contextRef: string,
): Promise<ManualQuestionCreationRawView> {
  return writeJson(
    `/api/v1/manual-question-creations/${encodeURIComponent(contextRef)}/cancel`,
    "POST",
    {},
  );
}

export function sendCompanionMessage(
  message: string,
  scopeRef?: string | null,
): Promise<Record<string, unknown>> {
  return writeJson("/api/v1/companion/messages", "POST", {
    ...(scopeRef ? { scope_ref: scopeRef } : {}),
    message,
  });
}

export async function respondToHumanRequest(
  requestRef: string,
  response: HumanRequestResponseBody,
): Promise<Record<string, unknown>> {
  return runHumanRequestRecoverySingleFlight(
    `response-submit:${requestRef}`,
    requestRef,
    async () => {
      await stageHumanRequestResponse(requestRef, response);
      return deliverPendingHumanRequestResponseOnce(requestRef);
    },
  );
}

async function stageHumanRequestResponse(
  requestRef: string,
  response: HumanRequestResponseBody,
): Promise<PendingHumanRequestResponse> {
  await hydratePendingHumanRequestRecovery();
  const responsePath = humanRequestResponsePath(requestRef);
  const responseBody = JSON.parse(JSON.stringify(response)) as HumanRequestResponseBody;
  const bodyJson = JSON.stringify(responseBody);
  const bodyHash = await sha256Hex(bodyJson);
  const existing = readPendingHumanRequestResponse(requestRef);
  if (existing) {
    if (existing.request_ref !== requestRef
        || existing.response_path !== responsePath
        || existing.sealed_response.body_hash !== bodyHash) {
      throw new ProductError("human_request_response_recovery_conflict");
    }
    return existing;
  }
  if (readPendingHumanRequestAssetResponse(requestRef)
      || readPendingHumanRequestAssetIntakeOperation(requestRef)
      || readPendingAcceptedHumanRequestAsset(requestRef)) {
    throw new ProductError("human_request_response_recovery_conflict");
  }
  const pendingWrite = await reserveIdempotencyKey("POST", responsePath, bodyJson);
  const bindingJson = JSON.stringify({
    schema: "meta-research/human-request-response/v1",
    request_ref: requestRef,
    response_path: responsePath,
    response_idempotency_key: pendingWrite.key,
    response_write_slot: pendingWrite.slot,
  });
  let preparedResponse: PreparedHumanRequestRecoveryPayload;
  try {
    preparedResponse = await sealHumanRequestResponse(bodyJson, bodyHash, bindingJson);
  } catch (error) {
    pendingWrite.clear();
    throw error;
  }
  const delivery: PendingHumanRequestResponse = {
    schema: "meta-research/human-request-response/v1",
    request_ref: requestRef,
    response_path: responsePath,
    sealed_response: preparedResponse.sealed,
    response_idempotency_key: pendingWrite.key,
    response_write_slot: pendingWrite.slot,
  };
  const serialized = JSON.stringify(delivery);
  try {
    await storeHumanRequestRecoveryRecord(
      "response",
      requestRef,
      serialized,
      preparedResponse,
    );
  } catch (error) {
    pendingWrite.clear();
    throw error;
  }
  return delivery;
}

export function pendingHumanRequestResponse(
  requestRef?: string,
): PendingHumanRequestResponse | null {
  return readPendingHumanRequestResponse(requestRef);
}

export function deliverPendingHumanRequestResponse(
  requestRef: string,
): Promise<Record<string, unknown>> {
  return runHumanRequestRecoverySingleFlight(
    `response:${requestRef}`,
    requestRef,
    () => deliverPendingHumanRequestResponseOnce(requestRef),
  );
}

async function deliverPendingHumanRequestResponseOnce(
  requestRef: string,
): Promise<Record<string, unknown>> {
  await hydratePendingHumanRequestRecovery();
  const delivery = readPendingHumanRequestResponse(requestRef);
  if (!delivery || delivery.request_ref !== requestRef
      || delivery.response_path !== humanRequestResponsePath(requestRef)) {
    throw new ProductError("human_request_response_recovery_conflict");
  }
  const response = await unsealHumanRequestResponseBody(
    delivery.sealed_response,
    humanRequestResponseDeliveryBindingJson(delivery),
  );
  const responseWrite: { value: PendingWrite | null } = { value: null };
  try {
    const result = await writeJson<Record<string, unknown>>(
      delivery.response_path,
      "POST",
      response,
      {
        retainPending: () => true,
        onRetained: () => undefined,
        onReserved: (pendingWrite) => {
          if (pendingWrite.key !== delivery.response_idempotency_key
              || pendingWrite.slot !== delivery.response_write_slot) {
            throw new ProductError("human_request_response_idempotency_mismatch");
          }
          responseWrite.value = pendingWrite;
        },
      },
    );
    await deleteHumanRequestRecoveryRecord(
      "response",
      delivery.request_ref,
      JSON.stringify(delivery),
      delivery.sealed_response,
    );
    responseWrite.value?.clear();
    return result;
  } catch (error) {
    if (isCorrectableHumanResponseRejection(error)
        || isPermanentHumanResponseRejection(error)) {
      await discardPendingHumanRequestResponse(delivery);
    }
    throw error;
  }
}

async function stageHumanRequestAssetResponse(
  requestRef: string,
  assetJobRef: string,
  assetIntakeWriteSlot: string,
  factPrefix: "material" | "result",
  acceptedAsset: AcceptedHumanRequestAssetBinding,
  response: HumanRequestResponseBody,
): Promise<PendingHumanRequestAssetResponse> {
  await hydratePendingHumanRequestRecovery();
  const responsePath = humanRequestResponsePath(requestRef);
  const acceptedAssetBinding = {
    asset_ref: acceptedAsset.asset_ref,
    version_ref: acceptedAsset.version_ref,
    memory_ref: acceptedAsset.memory_ref,
    content_hash: acceptedAsset.content_hash,
    manifest_hash: acceptedAsset.manifest_hash,
    receipt: { ...acceptedAsset.receipt },
  };
  const responseBody = JSON.parse(JSON.stringify(response)) as HumanRequestResponseBody;
  const responseBodyJson = JSON.stringify(responseBody);
  const responseBodyHash = await sha256Hex(responseBodyJson);
  const existing = readPendingHumanRequestAssetResponse(requestRef);
  if (existing) {
    if (existing.request_ref !== requestRef
        || existing.asset_job_ref !== assetJobRef
        || existing.asset_intake_write_slot !== assetIntakeWriteSlot
        || existing.fact_prefix !== factPrefix
        || existing.response_path !== responsePath
        || existing.sealed_response.body_hash !== responseBodyHash
        || JSON.stringify(existing.accepted_asset) !== JSON.stringify(acceptedAssetBinding)) {
      throw new ProductError("human_request_asset_response_recovery_conflict");
    }
    return existing;
  }
  const pendingWrite = await reserveIdempotencyKey("POST", responsePath, responseBodyJson);
  const bindingJson = JSON.stringify({
    schema: "meta-research/human-request-asset-response/v1",
    request_ref: requestRef,
    asset_job_ref: assetJobRef,
    asset_intake_write_slot: assetIntakeWriteSlot,
    fact_prefix: factPrefix,
    accepted_asset: acceptedAssetBinding,
    response_path: responsePath,
    response_idempotency_key: pendingWrite.key,
    response_write_slot: pendingWrite.slot,
  });
  let preparedResponse: PreparedHumanRequestRecoveryPayload;
  try {
    preparedResponse = await sealHumanRequestResponse(
      responseBodyJson,
      responseBodyHash,
      bindingJson,
    );
  } catch (error) {
    pendingWrite.clear();
    throw error;
  }
  const delivery: PendingHumanRequestAssetResponse = {
    schema: "meta-research/human-request-asset-response/v1",
    request_ref: requestRef,
    asset_job_ref: assetJobRef,
    asset_intake_write_slot: assetIntakeWriteSlot,
    fact_prefix: factPrefix,
    accepted_asset: acceptedAssetBinding,
    response_path: responsePath,
    sealed_response: preparedResponse.sealed,
    response_idempotency_key: pendingWrite.key,
    response_write_slot: pendingWrite.slot,
  };
  const serialized = JSON.stringify(delivery);
  try {
    await storeHumanRequestRecoveryRecord(
      "asset-response",
      requestRef,
      serialized,
      preparedResponse,
    );
  } catch (error) {
    pendingWrite.clear();
    throw error;
  }
  return delivery;
}

export function pendingHumanRequestAssetResponse(
  requestRef?: string,
): PendingHumanRequestAssetResponse | null {
  return readPendingHumanRequestAssetResponse(requestRef);
}

export function pendingHumanRequestAssetIntakeRequestRef(
  requestRef?: string,
): string | null {
  return readPendingHumanRequestAssetIntakeOperation(requestRef)?.request_ref ?? null;
}

export function pendingAcceptedHumanRequestAssetRequestRef(
  requestRef?: string,
): string | null {
  return readPendingAcceptedHumanRequestAsset(requestRef)?.request_ref ?? null;
}

export async function stagePendingAcceptedHumanRequestAssetResponse(
  requestRef: string,
  response: HumanRequestResponseBody,
): Promise<PendingHumanRequestAssetResponse> {
  return runHumanRequestRecoverySingleFlight(
    `accepted-asset-stage:${requestRef}`,
    requestRef,
    () => stagePendingAcceptedHumanRequestAssetResponseOnce(requestRef, response),
  );
}

async function stagePendingAcceptedHumanRequestAssetResponseOnce(
  requestRef: string,
  response: HumanRequestResponseBody,
): Promise<PendingHumanRequestAssetResponse> {
  await hydratePendingHumanRequestRecovery();
  const accepted = readPendingAcceptedHumanRequestAsset(requestRef);
  if (!accepted || accepted.request_ref !== requestRef) {
    throw new ProductError("human_request_accepted_asset_recovery_conflict");
  }
  const exactResponse: HumanRequestResponseBody = {
    ...response,
    facts: {
      ...response.facts,
      [`${accepted.fact_prefix}_source_ref`]: accepted.accepted_asset.memory_ref,
      [`${accepted.fact_prefix}_version_ref`]: accepted.accepted_asset.version_ref,
      [`${accepted.fact_prefix}_content_hash`]: accepted.accepted_asset.content_hash,
      [`${accepted.fact_prefix}_manifest_hash`]: accepted.accepted_asset.manifest_hash,
      [`${accepted.fact_prefix}_acceptance_receipt_ref`]:
        accepted.accepted_asset.receipt.receipt_ref,
    },
  };
  const delivery = await stageHumanRequestAssetResponse(
    accepted.request_ref,
    accepted.asset_job_ref,
    accepted.asset_intake_write_slot,
    accepted.fact_prefix,
    accepted.accepted_asset,
    exactResponse,
  );
  await deleteHumanRequestRecoveryRecord(
    "accepted-asset",
    accepted.request_ref,
    JSON.stringify(accepted),
  );
  return delivery;
}

export async function resumePendingHumanRequestAssetIntake(
  requestRef: string,
): Promise<AssetIntakeResult> {
  await hydratePendingHumanRequestRecovery();
  const operation = readPendingHumanRequestAssetIntakeOperation(requestRef);
  if (!operation || operation.request_ref !== requestRef) {
    throw new ProductError("human_request_asset_intake_recovery_conflict");
  }
  return executeHumanRequestAssetIntakeOperationOnce(operation);
}

export async function reconcileOrphanedHumanRequestAssetRecovery(
  currentRequestRefs: string[],
): Promise<boolean> {
  await hydratePendingHumanRequestRecovery();
  const current = new Set(currentRequestRefs);
  let changed = false;
  for (const response of readPendingHumanRequestResponses()) {
    try {
      await deliverPendingHumanRequestResponse(response.request_ref);
      changed = true;
    } catch (error) {
      if (!readPendingHumanRequestResponse(response.request_ref)) changed = true;
      else throw error;
    }
  }

  for (const delivery of readPendingHumanRequestAssetResponses()) {
    try {
      await deliverPendingHumanRequestAssetResponse(delivery.request_ref);
      changed = true;
    } catch (error) {
      if (!readPendingHumanRequestAssetResponse(delivery.request_ref)) changed = true;
      else throw error;
    }
  }

  for (const operation of readPendingHumanRequestAssetIntakeOperations()) {
    try {
      const result = await executeHumanRequestAssetIntakeOperation(operation);
      if (result.status === "accepted" && result.asset) {
        await deliverPendingHumanRequestAssetResponse(operation.request_ref);
      }
      changed = true;
    } catch (error) {
      if (!readPendingHumanRequestAssetIntakeOperation(operation.request_ref)
          && !readPendingHumanRequestAssetResponse(operation.request_ref)) changed = true;
      else throw error;
    }
  }

  for (const accepted of readPendingAcceptedHumanRequestAssets()) {
    if (current.has(accepted.request_ref)) continue;
    clearPendingWriteSlot(accepted.asset_intake_write_slot);
    removePendingAssetIntakeMarker(accepted.asset_job_ref);
    await deleteHumanRequestRecoveryRecord(
      "accepted-asset",
      accepted.request_ref,
      JSON.stringify(accepted),
    );
    changed = true;
  }
  return changed;
}

async function stageHumanRequestAssetIntakeOperation(
  requestRef: string,
  pendingWrite: PendingWrite,
  body: HumanRequestAssetIntakeOperationBody,
): Promise<PendingHumanRequestAssetIntakeOperation> {
  const bindingJson = JSON.stringify({
    schema: "meta-research/human-request-asset-intake/v1",
    request_ref: requestRef,
    intake_path: "/api/v1/research-assets/intakes",
    asset_idempotency_key: pendingWrite.key,
    asset_write_slot: pendingWrite.slot,
  });
  const bodyJson = JSON.stringify(body);
  const preparedOperation = await sealHumanRequestResponse(
    bodyJson,
    await sha256Hex(bodyJson),
    bindingJson,
  );
  const operation: PendingHumanRequestAssetIntakeOperation = {
    schema: "meta-research/human-request-asset-intake/v1",
    request_ref: requestRef,
    intake_path: "/api/v1/research-assets/intakes",
    asset_idempotency_key: pendingWrite.key,
    asset_write_slot: pendingWrite.slot,
    sealed_operation: preparedOperation.sealed,
  };
  const serialized = JSON.stringify(operation);
  await storeHumanRequestRecoveryRecord(
    "asset-intake",
    requestRef,
    serialized,
    preparedOperation,
  );
  return operation;
}

async function executeHumanRequestAssetIntakeOperation(
  operation: PendingHumanRequestAssetIntakeOperation,
): Promise<AssetIntakeResult> {
  return runHumanRequestRecoverySingleFlight(
    `asset-intake:${operation.sealed_operation.key_ref}`,
    operation.request_ref,
    () => executeHumanRequestAssetIntakeOperationOnce(operation),
  );
}

async function executeHumanRequestAssetIntakeOperationOnce(
  operation: PendingHumanRequestAssetIntakeOperation,
): Promise<AssetIntakeResult> {
  const body = await unsealHumanRequestAssetIntakeOperation(operation);
  const result = await writeJson<AssetIntakeResult>(
    operation.intake_path,
    "POST",
    body.intake,
    {
      retainPending: () => true,
      onReserved: (pendingWrite) => {
        if (pendingWrite.key !== operation.asset_idempotency_key
            || pendingWrite.slot !== operation.asset_write_slot) {
          throw new ProductError("human_request_asset_intake_idempotency_mismatch");
        }
      },
      onRetained: async (result, pendingWrite) => {
        writeSessionValue(
          pendingAssetIntakeSlot,
          JSON.stringify({ job_ref: result.job_ref, write_slot: pendingWrite.slot }),
        );
        if (result.status !== "accepted" || !result.asset) return;
        const exactResponse: HumanRequestResponseBody = {
          ...body.response,
          facts: {
            ...body.response.facts,
            [`${body.fact_prefix}_source_ref`]: result.asset.memory_ref,
            [`${body.fact_prefix}_version_ref`]: result.asset.version_ref,
            [`${body.fact_prefix}_content_hash`]: result.asset.content_hash,
            [`${body.fact_prefix}_manifest_hash`]: result.asset.manifest_hash,
            [`${body.fact_prefix}_acceptance_receipt_ref`]: result.asset.receipt.receipt_ref,
          },
        };
        await stageHumanRequestAssetResponse(
          operation.request_ref,
          result.job_ref,
          pendingWrite.slot,
          body.fact_prefix,
          result.asset,
          exactResponse,
        );
      },
    },
  );
  if (result.status === "accepted" && result.asset) {
    removePendingAssetIntakeMarker(result.job_ref);
  }
  await clearPendingHumanRequestAssetIntakeOperation(operation);
  return result;
}

export function deliverPendingHumanRequestAssetResponse(
  requestRef: string,
): Promise<Record<string, unknown>> {
  return runHumanRequestRecoverySingleFlight(
    `asset-response:${requestRef}`,
    requestRef,
    () => deliverPendingHumanRequestAssetResponseOnce(requestRef),
  );
}

async function deliverPendingHumanRequestAssetResponseOnce(
  requestRef: string,
): Promise<Record<string, unknown>> {
  await hydratePendingHumanRequestRecovery();
  const delivery = readPendingHumanRequestAssetResponse(requestRef);
  if (!delivery) {
    throw new ProductError("human_request_asset_response_recovery_missing");
  }
  if (delivery.request_ref !== requestRef
      || delivery.response_path !== humanRequestResponsePath(requestRef)) {
    throw new ProductError("human_request_asset_response_recovery_conflict");
  }
  await clearMatchingHumanRequestAssetIntakeOperation(
    delivery.request_ref,
    delivery.asset_intake_write_slot,
  );
  removePendingAssetIntakeMarker(delivery.asset_job_ref);
  await removeMatchingPendingAcceptedHumanRequestAsset(delivery.request_ref);
  const response = await unsealHumanRequestResponse(delivery);
  const responseWrite: { value: PendingWrite | null } = { value: null };
  let result: Record<string, unknown>;
  try {
    result = await writeJson<Record<string, unknown>>(
      delivery.response_path,
      "POST",
      response,
      {
        retainPending: () => true,
        onRetained: () => undefined,
        onReserved: (pendingWrite) => {
          if (pendingWrite.key !== delivery.response_idempotency_key
              || pendingWrite.slot !== delivery.response_write_slot) {
            throw new ProductError("human_request_asset_response_idempotency_mismatch");
          }
          responseWrite.value = pendingWrite;
        },
      },
    );
  } catch (error) {
    if (isCorrectableHumanResponseRejection(error)) {
      try {
        await persistPendingAcceptedHumanRequestAsset(delivery);
      } catch {
        // The rejected response is always destroyed even if the non-sensitive
        // accepted-asset fallback cannot be persisted.
      }
      await discardPendingHumanRequestAssetResponse(delivery);
    } else if (isPermanentHumanResponseRejection(error)) {
      await discardPendingHumanRequestAssetResponse(delivery);
    }
    throw error;
  }

  // Clearing the asset write first is safe because the accepted facts and receipt
  // remain sealed in the delivery record. Removing the delivery record before the
  // response key means a crash can only leave a harmless key, never force a replay
  // under a new identity.
  clearPendingWriteSlot(delivery.asset_intake_write_slot);
  await deleteHumanRequestRecoveryRecord(
    "asset-response",
    delivery.request_ref,
    JSON.stringify(delivery),
    delivery.sealed_response,
  );
  responseWrite.value?.clear();
  return result;
}

function isCorrectableHumanResponseRejection(error: unknown): boolean {
  return error instanceof ProductError && (
    error.code === "human_response_secret_forbidden"
    || error.code === "human_request_secret_forbidden"
    || error.code === "human_collaboration_secret_forbidden"
    || error.code === "human_response_decision_invalid"
    || error.code === "human_response_facts_invalid"
    || error.code === "human_response_facts_too_large"
    || error.code === "human_response_note_invalid"
  );
}

function isPermanentHumanResponseRejection(error: unknown): boolean {
  return error instanceof ProductError && (
    error.code === "human_request_not_current"
    || error.code === "human_request_not_found"
    || error.code === "idempotency_conflict"
  );
}

async function discardPendingHumanRequestAssetResponse(
  delivery: PendingHumanRequestAssetResponse,
): Promise<void> {
  clearPendingWriteSlot(delivery.asset_intake_write_slot);
  clearPendingWriteSlot(delivery.response_write_slot);
  removePendingAssetIntakeMarker(delivery.asset_job_ref);
  await deleteHumanRequestRecoveryRecord(
    "asset-response",
    delivery.request_ref,
    JSON.stringify(delivery),
    delivery.sealed_response,
  );
}

async function discardPendingHumanRequestResponse(
  delivery: PendingHumanRequestResponse,
): Promise<void> {
  clearPendingWriteSlot(delivery.response_write_slot);
  await deleteHumanRequestRecoveryRecord(
    "response",
    delivery.request_ref,
    JSON.stringify(delivery),
    delivery.sealed_response,
  );
}

function humanRequestResponsePath(requestRef: string): string {
  return `/api/v1/human-requests/${encodeURIComponent(requestRef)}/responses`;
}

export function createHumanCommand(
  scopeRef: string,
  command: HumanCommandDraft,
): Promise<HumanCommand> {
  return writeJson("/api/v1/human-collaboration/commands", "POST", {
    scope_ref: scopeRef,
    command,
  });
}

export function convertAgentProposalToCommandDraft(
  proposal: CompanionAgentProposal,
): Promise<{ proposal: CompanionAgentProposal; command_draft: HumanCommand }> {
  if (!proposal.proposal_ref || !proposal.scope_ref || !proposal.proposal_hash) {
    throw new ProductError("agent_proposal_current_basis_required");
  }
  return writeJson(
    `/api/v1/human-collaboration/agent-proposals/${encodeURIComponent(proposal.proposal_ref)}/command-draft`,
    "POST",
    {
      expected_scope_ref: proposal.scope_ref,
      expected_proposal_hash: proposal.proposal_hash,
    },
  );
}

export function reviseHumanCommand(
  command: HumanCommand,
  draft: HumanCommandDraft,
): Promise<HumanCommand> {
  return writeJson(
    `/api/v1/human-collaboration/commands/${encodeURIComponent(command.intent_id)}/revisions`,
    "POST",
    { expected_revision: command.draft_revision, command: draft },
  );
}

export function previewHumanCommand(command: HumanCommand): Promise<HumanCommand> {
  return writeJson(
    `/api/v1/human-collaboration/commands/${encodeURIComponent(command.intent_id)}/previews`,
    "POST",
    {
      draft_revision: command.draft_revision,
      draft_hash: command.draft_hash,
    },
  );
}

export function confirmHumanCommand(command: HumanCommand): Promise<HumanCommand> {
  const preview = command.impact_preview;
  if (!preview || preview.status !== "current") {
    throw new ProductError("command_preview_current_required");
  }
  return writeJson(
    `/api/v1/human-collaboration/commands/${encodeURIComponent(command.intent_id)}/confirmations`,
    "POST",
    {
      draft_revision: command.draft_revision,
      draft_hash: command.draft_hash,
      preview_ref: preview.preview_ref,
      preview_hash: preview.preview_hash,
    },
  );
}

export function executeHumanCommand(command: HumanCommand): Promise<HumanCommand> {
  const confirmation = command.confirmation_receipt;
  if (!confirmation || command.draft.command_kind !== "research_control") {
    throw new ProductError("research_control_confirmation_required");
  }
  return writeJson(
    `/api/v1/human-collaboration/commands/${encodeURIComponent(command.intent_id)}/executions`,
    "POST",
    { confirmation_receipt_ref: confirmation.receipt_ref },
  );
}

export function authorizeHumanCommand(
  command: HumanCommand,
): Promise<HumanCapabilityAuthorization> {
  const confirmation = command.confirmation_receipt;
  if (!confirmation) throw new ProductError("human_confirmation_required");
  if (command.draft.command_kind !== "capability_authorization") {
    throw new ProductError("capability_authorization_command_required");
  }
  const payload = command.draft.payload;
  return writeJson(
    `/api/v1/human-collaboration/commands/${encodeURIComponent(command.intent_id)}/authorizations`,
    "POST",
    {
      capability: payload.capability,
      decision: payload.decision,
      scope: payload.scope,
      confirmation_receipt_ref: confirmation.receipt_ref,
    },
  );
}

export function recordSoftConstraint(
  scopeRef: string,
  guidance: Record<string, unknown>,
): Promise<CompanionSoftConstraint> {
  return writeJson("/api/v1/human-collaboration/soft-constraints", "POST", {
    scope_ref: scopeRef,
    guidance,
  });
}

export function convertAgentProposalToSoftConstraint(
  proposal: CompanionAgentProposal,
): Promise<{
  proposal: CompanionAgentProposal;
  soft_constraint: CompanionSoftConstraint;
}> {
  if (!proposal.proposal_ref || !proposal.scope_ref || !proposal.proposal_hash) {
    throw new ProductError("agent_proposal_current_basis_required");
  }
  return writeJson(
    `/api/v1/human-collaboration/agent-proposals/${encodeURIComponent(proposal.proposal_ref)}/soft-constraint`,
    "POST",
    {
      expected_scope_ref: proposal.scope_ref,
      expected_proposal_hash: proposal.proposal_hash,
    },
  );
}

export function withdrawSoftConstraint(
  constraint: CompanionSoftConstraint,
): Promise<CompanionSoftConstraint> {
  if (!constraint.constraint_ref || constraint.revision === undefined) {
    throw new ProductError("soft_constraint_current_revision_required");
  }
  return writeJson(
    `/api/v1/human-collaboration/soft-constraints/${encodeURIComponent(constraint.constraint_ref)}/withdrawals`,
    "POST",
    { expected_revision: constraint.revision },
  );
}

type ExperimentStartRequestBase = {
  execution_request_ref: string;
  quest_ref: string;
  title: string;
  hypothesis: string;
  variant_parameter: number;
  sample_count: number;
};

export type ExperimentStartRequest = ExperimentStartRequestBase & (
  | {
      request_kind: "retrain";
    }
  | {
      request_kind: "remeasure";
      source_variant_run_ref: string;
      selected_checkpoint_role_refs: string[];
    }
);

export function startExperiment(
  intent: ExperimentStartRequest,
): Promise<ExperimentProjection> {
  return writeJson("/api/v1/experiments", "POST", intent);
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

export async function submitHumanRequestAssetIntake(
  requestRef: string,
  intake: AssetIntakeRequest,
  response: HumanRequestResponseBody,
  factPrefix: "material" | "result",
): Promise<AssetIntakeResult> {
  return runHumanRequestRecoverySingleFlight(
    `asset-intake-submit:${requestRef}`,
    requestRef,
    () => submitHumanRequestAssetIntakeOnce(
      requestRef,
      intake,
      response,
      factPrefix,
    ),
  );
}

async function submitHumanRequestAssetIntakeOnce(
  requestRef: string,
  intake: AssetIntakeRequest,
  response: HumanRequestResponseBody,
  factPrefix: "material" | "result",
): Promise<AssetIntakeResult> {
  await hydratePendingHumanRequestRecovery();
  const existing = readPendingHumanRequestAssetIntakeOperation(requestRef);
  if (existing) {
    throw new ProductError("human_request_asset_intake_recovery_conflict");
  }
  const operationBody: HumanRequestAssetIntakeOperationBody = {
    intake: JSON.parse(JSON.stringify(intake)) as AssetIntakeRequest,
    response: JSON.parse(JSON.stringify(response)) as HumanRequestResponseBody,
    fact_prefix: factPrefix,
  };
  const intakePath = "/api/v1/research-assets/intakes" as const;
  const pendingWrite = await reserveIdempotencyKey(
    "POST",
    intakePath,
    JSON.stringify(operationBody.intake),
  );
  let operation: PendingHumanRequestAssetIntakeOperation;
  try {
    operation = await stageHumanRequestAssetIntakeOperation(
      requestRef,
      pendingWrite,
      operationBody,
    );
  } catch (error) {
    pendingWrite.clear();
    throw error;
  }
  return executeHumanRequestAssetIntakeOperationOnce(operation);
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
    onRetained: (payload: T, pendingWrite: PendingWrite) => void | Promise<void>;
    onReserved?: (pendingWrite: PendingWrite) => void;
  },
): Promise<T> {
  const csrfToken = readCookie("meta_research_csrf");
  if (!csrfToken) throw new ProductError("csrf_token_unavailable");
  const bodyJson = JSON.stringify(body);
  const pendingWrite = await reserveIdempotencyKey(method, path, bodyJson);
  options?.onReserved?.(pendingWrite);
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
  if (options?.retainPending(payload)) await options.onRetained(payload, pendingWrite);
  else pendingWrite.clear();
  return payload;
}

const inMemoryPendingWrites = new Map<string, string>();
const pendingAssetIntakeSlot = "meta_research_pending_asset_intake";
const pendingHumanRequestResponseSlot = "meta_research_pending_human_request_response";
const pendingHumanRequestAssetResponseSlot =
  "meta_research_pending_human_request_asset_response";
const pendingHumanRequestAssetIntakeOperationSlot =
  "meta_research_pending_human_request_asset_intake_operation";
const pendingAcceptedHumanRequestAssetSlot =
  "meta_research_pending_human_request_accepted_asset";
const humanRequestRecoveryManifestSlots = [
  pendingHumanRequestResponseSlot,
  pendingHumanRequestAssetResponseSlot,
  pendingHumanRequestAssetIntakeOperationSlot,
  pendingAcceptedHumanRequestAssetSlot,
] as const;
type HumanRequestRecoveryManifestKind =
  | "response"
  | "asset-response"
  | "asset-intake"
  | "accepted-asset";
const humanRequestRecoveryManifestKinds: Array<{
  kind: HumanRequestRecoveryManifestKind;
  legacySlot: typeof humanRequestRecoveryManifestSlots[number];
}> = [
  { kind: "response", legacySlot: pendingHumanRequestResponseSlot },
  { kind: "asset-response", legacySlot: pendingHumanRequestAssetResponseSlot },
  { kind: "asset-intake", legacySlot: pendingHumanRequestAssetIntakeOperationSlot },
  { kind: "accepted-asset", legacySlot: pendingAcceptedHumanRequestAssetSlot },
];
const humanRequestRecoveryManifestCache = new Map<string, string>();
const humanRequestRecoveryFlights = new Map<string, Promise<unknown>>();
let humanRequestRecoveryHydration: Promise<void> | null = null;

function runHumanRequestRecoverySingleFlight<T>(
  identity: string,
  requestRef: string,
  operation: () => Promise<T>,
): Promise<T> {
  const existing = humanRequestRecoveryFlights.get(identity);
  if (existing) return existing as Promise<T>;
  const execute = (): Promise<T> => {
    if (typeof navigator !== "undefined" && navigator.locks) {
      return navigator.locks.request(
        `meta-research:human-request-recovery:${requestRef}`,
        { mode: "exclusive" },
        () => operation(),
      ) as unknown as Promise<T>;
    }
    return operation();
  };
  const flight = execute().finally(() => {
    if (humanRequestRecoveryFlights.get(identity) === flight) {
      humanRequestRecoveryFlights.delete(identity);
    }
  });
  humanRequestRecoveryFlights.set(identity, flight);
  return flight;
}

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

function removePendingAssetIntakeMarker(jobRef: string): void {
  const record = readPendingAssetIntake();
  if (record?.job_ref === jobRef) removeSessionValue(pendingAssetIntakeSlot);
}

function clearPendingWriteSlot(slot: string): void {
  inMemoryPendingWrites.delete(slot);
  removeSessionValue(slot);
}

function humanRequestRecoveryManifestKey(
  kind: HumanRequestRecoveryManifestKind,
  requestRef: string,
): string {
  return `meta-research/human-request-recovery/v2:${kind}:${encodeURIComponent(requestRef)}`;
}

function recoveryManifestValues(kind: HumanRequestRecoveryManifestKind): string[] {
  const prefix = `meta-research/human-request-recovery/v2:${kind}:`;
  return Array.from(humanRequestRecoveryManifestCache.entries())
    .filter(([key]) => key.startsWith(prefix))
    .map(([, serialized]) => serialized);
}

function recoveryManifestValue(
  kind: HumanRequestRecoveryManifestKind,
  requestRef?: string,
): string | null {
  if (requestRef) {
    return humanRequestRecoveryManifestCache.get(
      humanRequestRecoveryManifestKey(kind, requestRef),
    ) ?? null;
  }
  return recoveryManifestValues(kind)[0] ?? null;
}

function parsePendingHumanRequestResponse(
  value: string | null,
): PendingHumanRequestResponse | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PendingHumanRequestResponse>;
    const sealed = parsed.sealed_response as
      | Partial<PendingHumanRequestResponse["sealed_response"]>
      | undefined;
    const valid = parsed.schema === "meta-research/human-request-response/v1"
      && typeof parsed.request_ref === "string"
      && typeof parsed.response_path === "string"
      && typeof parsed.response_idempotency_key === "string"
      && typeof parsed.response_write_slot === "string"
      && sealed?.algorithm === "AES-GCM"
      && typeof sealed.key_ref === "string"
      && typeof sealed.iv_base64 === "string"
      && typeof sealed.ciphertext_ref === "string"
      && typeof sealed.body_hash === "string"
      && typeof sealed.binding_hash === "string";
    if (!valid) throw new ProductError("human_request_response_recovery_invalid");
    return parsed as PendingHumanRequestResponse;
  } catch (error) {
    if (error instanceof ProductError) throw error;
    throw new ProductError("human_request_response_recovery_invalid");
  }
}

function readPendingHumanRequestResponse(
  requestRef?: string,
): PendingHumanRequestResponse | null {
  return parsePendingHumanRequestResponse(recoveryManifestValue("response", requestRef));
}

function readPendingHumanRequestResponses(): PendingHumanRequestResponse[] {
  return recoveryManifestValues("response").map((serialized) => {
    const response = parsePendingHumanRequestResponse(serialized);
    if (!response) throw new ProductError("human_request_response_recovery_invalid");
    return response;
  });
}

function parsePendingHumanRequestAssetResponse(
  value: string | null,
): PendingHumanRequestAssetResponse | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PendingHumanRequestAssetResponse>;
    const acceptedAsset = parsed.accepted_asset as
      | Partial<PendingHumanRequestAssetResponse["accepted_asset"]>
      | undefined;
    const receipt = acceptedAsset?.receipt as Partial<AssetReceipt> | undefined;
    const sealedResponse = parsed.sealed_response as
      | Partial<PendingHumanRequestAssetResponse["sealed_response"]>
      | undefined;
    const valid = parsed.schema === "meta-research/human-request-asset-response/v1"
      && typeof parsed.request_ref === "string"
      && typeof parsed.asset_job_ref === "string"
      && typeof parsed.asset_intake_write_slot === "string"
      && (parsed.fact_prefix === "material" || parsed.fact_prefix === "result")
      && typeof parsed.response_path === "string"
      && typeof parsed.response_idempotency_key === "string"
      && typeof parsed.response_write_slot === "string"
      && typeof acceptedAsset?.asset_ref === "string"
      && typeof acceptedAsset.version_ref === "string"
      && typeof acceptedAsset.memory_ref === "string"
      && typeof acceptedAsset.content_hash === "string"
      && typeof acceptedAsset.manifest_hash === "string"
      && typeof receipt?.issuer === "string"
      && typeof receipt.kind === "string"
      && typeof receipt.receipt_ref === "string"
      && typeof receipt.subject_ref === "string"
      && typeof receipt.payload_hash === "string"
      && sealedResponse?.algorithm === "AES-GCM"
      && typeof sealedResponse.key_ref === "string"
      && typeof sealedResponse.iv_base64 === "string"
      && typeof sealedResponse.ciphertext_ref === "string"
      && typeof sealedResponse.body_hash === "string"
      && typeof sealedResponse.binding_hash === "string";
    if (!valid) {
      throw new ProductError("human_request_asset_response_recovery_invalid");
    }
    return parsed as PendingHumanRequestAssetResponse;
  } catch (error) {
    if (error instanceof ProductError) throw error;
    throw new ProductError("human_request_asset_response_recovery_invalid");
  }
}

function readPendingHumanRequestAssetResponse(
  requestRef?: string,
): PendingHumanRequestAssetResponse | null {
  return parsePendingHumanRequestAssetResponse(
    recoveryManifestValue("asset-response", requestRef),
  );
}

function readPendingHumanRequestAssetResponses(): PendingHumanRequestAssetResponse[] {
  return recoveryManifestValues("asset-response").map((serialized) => {
    const response = parsePendingHumanRequestAssetResponse(serialized);
    if (!response) {
      throw new ProductError("human_request_asset_response_recovery_invalid");
    }
    return response;
  });
}

function parsePendingHumanRequestAssetIntakeOperation(
  value: string | null,
):
  PendingHumanRequestAssetIntakeOperation | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PendingHumanRequestAssetIntakeOperation>;
    const sealed = parsed.sealed_operation as
      | Partial<PendingHumanRequestAssetResponse["sealed_response"]>
      | undefined;
    const valid = parsed.schema === "meta-research/human-request-asset-intake/v1"
      && typeof parsed.request_ref === "string"
      && parsed.intake_path === "/api/v1/research-assets/intakes"
      && typeof parsed.asset_idempotency_key === "string"
      && typeof parsed.asset_write_slot === "string"
      && sealed?.algorithm === "AES-GCM"
      && typeof sealed.key_ref === "string"
      && typeof sealed.iv_base64 === "string"
      && typeof sealed.ciphertext_ref === "string"
      && typeof sealed.body_hash === "string"
      && typeof sealed.binding_hash === "string";
    if (!valid) {
      throw new ProductError("human_request_asset_intake_recovery_invalid");
    }
    return parsed as PendingHumanRequestAssetIntakeOperation;
  } catch (error) {
    if (error instanceof ProductError) throw error;
    throw new ProductError("human_request_asset_intake_recovery_invalid");
  }
}

function readPendingHumanRequestAssetIntakeOperation(
  requestRef?: string,
): PendingHumanRequestAssetIntakeOperation | null {
  return parsePendingHumanRequestAssetIntakeOperation(
    recoveryManifestValue("asset-intake", requestRef),
  );
}

function readPendingHumanRequestAssetIntakeOperations():
  PendingHumanRequestAssetIntakeOperation[] {
  return recoveryManifestValues("asset-intake").map((serialized) => {
    const operation = parsePendingHumanRequestAssetIntakeOperation(serialized);
    if (!operation) {
      throw new ProductError("human_request_asset_intake_recovery_invalid");
    }
    return operation;
  });
}

function parsePendingAcceptedHumanRequestAsset(
  value: string | null,
): PendingAcceptedHumanRequestAsset | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as Partial<PendingAcceptedHumanRequestAsset>;
    const asset = parsed.accepted_asset as Partial<AcceptedHumanRequestAssetBinding> | undefined;
    const receipt = asset?.receipt as Partial<AssetReceipt> | undefined;
    const valid = parsed.schema === "meta-research/human-request-accepted-asset/v1"
      && typeof parsed.request_ref === "string"
      && typeof parsed.asset_job_ref === "string"
      && typeof parsed.asset_intake_write_slot === "string"
      && (parsed.fact_prefix === "material" || parsed.fact_prefix === "result")
      && typeof asset?.asset_ref === "string"
      && typeof asset.version_ref === "string"
      && typeof asset.memory_ref === "string"
      && typeof asset.content_hash === "string"
      && typeof asset.manifest_hash === "string"
      && typeof receipt?.issuer === "string"
      && typeof receipt.kind === "string"
      && typeof receipt.receipt_ref === "string"
      && typeof receipt.subject_ref === "string"
      && typeof receipt.payload_hash === "string";
    if (!valid) throw new ProductError("human_request_accepted_asset_recovery_invalid");
    return parsed as PendingAcceptedHumanRequestAsset;
  } catch (error) {
    if (error instanceof ProductError) throw error;
    throw new ProductError("human_request_accepted_asset_recovery_invalid");
  }
}

function readPendingAcceptedHumanRequestAsset(
  requestRef?: string,
): PendingAcceptedHumanRequestAsset | null {
  return parsePendingAcceptedHumanRequestAsset(
    recoveryManifestValue("accepted-asset", requestRef),
  );
}

function readPendingAcceptedHumanRequestAssets(): PendingAcceptedHumanRequestAsset[] {
  return recoveryManifestValues("accepted-asset").map((serialized) => {
    const accepted = parsePendingAcceptedHumanRequestAsset(serialized);
    if (!accepted) {
      throw new ProductError("human_request_accepted_asset_recovery_invalid");
    }
    return accepted;
  });
}

async function persistPendingAcceptedHumanRequestAsset(
  delivery: PendingHumanRequestAssetResponse,
): Promise<void> {
  const accepted: PendingAcceptedHumanRequestAsset = {
    schema: "meta-research/human-request-accepted-asset/v1",
    request_ref: delivery.request_ref,
    asset_job_ref: delivery.asset_job_ref,
    asset_intake_write_slot: delivery.asset_intake_write_slot,
    fact_prefix: delivery.fact_prefix,
    accepted_asset: delivery.accepted_asset,
  };
  const serialized = JSON.stringify(accepted);
  await storeHumanRequestRecoveryRecord(
    "accepted-asset",
    accepted.request_ref,
    serialized,
  );
}

async function removeMatchingPendingAcceptedHumanRequestAsset(
  requestRef: string,
): Promise<void> {
  const accepted = readPendingAcceptedHumanRequestAsset(requestRef);
  if (accepted?.request_ref === requestRef) {
    await deleteHumanRequestRecoveryRecord(
      "accepted-asset",
      accepted.request_ref,
      JSON.stringify(accepted),
    );
  }
}

async function clearPendingHumanRequestAssetIntakeOperation(
  operation: PendingHumanRequestAssetIntakeOperation,
): Promise<void> {
  const current = readPendingHumanRequestAssetIntakeOperation(operation.request_ref);
  if (!current || current.sealed_operation.key_ref !== operation.sealed_operation.key_ref) return;
  await deleteHumanRequestRecoveryRecord(
    "asset-intake",
    operation.request_ref,
    JSON.stringify(operation),
    operation.sealed_operation,
  );
}

async function clearMatchingHumanRequestAssetIntakeOperation(
  requestRef: string,
  assetWriteSlot: string,
): Promise<void> {
  const operation = readPendingHumanRequestAssetIntakeOperation(requestRef);
  if (!operation || operation.request_ref !== requestRef
      || operation.asset_write_slot !== assetWriteSlot) return;
  await clearPendingHumanRequestAssetIntakeOperation(operation);
}

const humanRequestRecoveryDatabase = "meta_research_human_request_recovery";
const humanRequestRecoveryKeyStore = "sealed_response_keys";
const humanRequestRecoveryCiphertextStore = "sealed_payloads";
const humanRequestRecoveryManifestStore = "recovery_manifests";

type PreparedHumanRequestRecoveryPayload = {
  sealed: PendingHumanRequestAssetResponse["sealed_response"];
  key: CryptoKey;
  ciphertext: ArrayBuffer;
};

async function sealHumanRequestResponse(
  bodyJson: string,
  bodyHash: string,
  bindingJson: string,
): Promise<PreparedHumanRequestRecoveryPayload> {
  const key = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  const keyRef = crypto.randomUUID();
  const ciphertextRef = crypto.randomUUID();
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    {
      name: "AES-GCM",
      iv,
      additionalData: new TextEncoder().encode(bindingJson),
    },
    key,
    new TextEncoder().encode(bodyJson),
  );
  return {
    sealed: {
      algorithm: "AES-GCM",
      key_ref: keyRef,
      iv_base64: bytesToBase64(iv),
      ciphertext_ref: ciphertextRef,
      body_hash: bodyHash,
      binding_hash: await sha256Hex(bindingJson),
    },
    key,
    ciphertext,
  };
}

async function unsealHumanRequestResponse(
  delivery: PendingHumanRequestAssetResponse,
): Promise<HumanRequestResponseBody> {
  return unsealHumanRequestResponseBody(
    delivery.sealed_response,
    humanRequestResponseBindingJson(delivery),
  );
}

async function unsealHumanRequestResponseBody(
  sealedResponse: PendingHumanRequestAssetResponse["sealed_response"],
  bindingJson: string,
): Promise<HumanRequestResponseBody> {
  const bodyJson = await unsealRecoveryPayload(sealedResponse, bindingJson);
  try {
    const parsed = JSON.parse(bodyJson) as Partial<HumanRequestResponseBody>;
    const valid = (parsed.decision === "provided"
      || parsed.decision === "declined"
      || parsed.decision === "deferred")
      && parsed.facts !== null
      && typeof parsed.facts === "object"
      && !Array.isArray(parsed.facts)
      && typeof parsed.note === "string";
    if (!valid) throw new Error("invalid response shape");
    return parsed as HumanRequestResponseBody;
  } catch {
    throw new ProductError("human_request_asset_response_body_invalid");
  }
}

async function unsealHumanRequestAssetIntakeOperation(
  operation: PendingHumanRequestAssetIntakeOperation,
): Promise<HumanRequestAssetIntakeOperationBody> {
  const bindingJson = JSON.stringify({
    schema: operation.schema,
    request_ref: operation.request_ref,
    intake_path: operation.intake_path,
    asset_idempotency_key: operation.asset_idempotency_key,
    asset_write_slot: operation.asset_write_slot,
  });
  const bodyJson = await unsealRecoveryPayload(operation.sealed_operation, bindingJson);
  try {
    const parsed = JSON.parse(bodyJson) as Partial<HumanRequestAssetIntakeOperationBody>;
    const response = parsed.response as Partial<HumanRequestResponseBody> | undefined;
    const valid = parsed.intake !== null
      && typeof parsed.intake === "object"
      && (parsed.fact_prefix === "material" || parsed.fact_prefix === "result")
      && (response?.decision === "provided"
        || response?.decision === "declined"
        || response?.decision === "deferred")
      && response.facts !== null
      && typeof response.facts === "object"
      && !Array.isArray(response.facts)
      && typeof response.note === "string";
    if (!valid) throw new Error("invalid intake operation shape");
    return parsed as HumanRequestAssetIntakeOperationBody;
  } catch {
    throw new ProductError("human_request_asset_intake_body_invalid");
  }
}

async function unsealRecoveryPayload(
  sealed: PendingHumanRequestAssetResponse["sealed_response"],
  bindingJson: string,
): Promise<string> {
  if (await sha256Hex(bindingJson) !== sealed.binding_hash) {
    throw new ProductError("human_request_recovery_binding_invalid");
  }
  const recoveryPayload = await readHumanRequestRecoveryPayload(
    sealed.key_ref,
    sealed.ciphertext_ref,
  );
  if (!recoveryPayload) {
    throw new ProductError("human_request_recovery_sealing_key_missing");
  }
  let bodyJson: string;
  try {
    const plaintext = await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: base64ToBytes(sealed.iv_base64),
        additionalData: new TextEncoder().encode(bindingJson),
      },
      recoveryPayload.key,
      recoveryPayload.ciphertext,
    );
    bodyJson = new TextDecoder().decode(plaintext);
  } catch {
    throw new ProductError("human_request_recovery_seal_invalid");
  }
  if (await sha256Hex(bodyJson) !== sealed.body_hash) {
    throw new ProductError("human_request_recovery_body_invalid");
  }
  return bodyJson;
}

function humanRequestResponseBindingJson(
  delivery: PendingHumanRequestAssetResponse,
): string {
  return JSON.stringify({
    schema: delivery.schema,
    request_ref: delivery.request_ref,
    asset_job_ref: delivery.asset_job_ref,
    asset_intake_write_slot: delivery.asset_intake_write_slot,
    fact_prefix: delivery.fact_prefix,
    accepted_asset: delivery.accepted_asset,
    response_path: delivery.response_path,
    response_idempotency_key: delivery.response_idempotency_key,
    response_write_slot: delivery.response_write_slot,
  });
}

function humanRequestResponseDeliveryBindingJson(
  delivery: PendingHumanRequestResponse,
): string {
  return JSON.stringify({
    schema: delivery.schema,
    request_ref: delivery.request_ref,
    response_path: delivery.response_path,
    response_idempotency_key: delivery.response_idempotency_key,
    response_write_slot: delivery.response_write_slot,
  });
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function base64ToBytes(value: string): Uint8Array<ArrayBuffer> {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

async function openHumanRequestRecoveryDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    throw new ProductError("human_request_asset_response_key_store_unavailable");
  }
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(humanRequestRecoveryDatabase, 3);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(humanRequestRecoveryKeyStore)) {
        request.result.createObjectStore(humanRequestRecoveryKeyStore);
      }
      if (!request.result.objectStoreNames.contains(humanRequestRecoveryCiphertextStore)) {
        request.result.createObjectStore(humanRequestRecoveryCiphertextStore);
      }
      if (!request.result.objectStoreNames.contains(humanRequestRecoveryManifestStore)) {
        request.result.createObjectStore(humanRequestRecoveryManifestStore);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new ProductError(
      "human_request_asset_response_key_store_unavailable",
    ));
    request.onblocked = () => reject(new ProductError(
      "human_request_asset_response_key_store_unavailable",
    ));
  });
}

async function readHumanRequestRecoveryPayload(
  keyRef: string,
  ciphertextRef: string,
): Promise<{ key: CryptoKey; ciphertext: ArrayBuffer } | null> {
  const database = await openHumanRequestRecoveryDatabase();
  try {
    return await new Promise<{ key: CryptoKey; ciphertext: ArrayBuffer } | null>(
      (resolve, reject) => {
        const transaction = database.transaction(
          [humanRequestRecoveryKeyStore, humanRequestRecoveryCiphertextStore],
          "readonly",
        );
        const keyRequest = transaction.objectStore(humanRequestRecoveryKeyStore).get(keyRef);
        const ciphertextRequest = transaction.objectStore(humanRequestRecoveryCiphertextStore)
          .get(ciphertextRef);
        transaction.oncomplete = () => {
          const key = keyRequest.result as CryptoKey | undefined;
          const ciphertext = ciphertextRequest.result as ArrayBuffer | undefined;
          resolve(key && ciphertext ? { key, ciphertext } : null);
        };
        transaction.onerror = () => reject(new ProductError(
          "human_request_asset_response_key_store_unavailable",
        ));
        transaction.onabort = () => reject(new ProductError(
          "human_request_asset_response_key_store_unavailable",
        ));
      },
    );
  } finally {
    database.close();
  }
}

export function hydratePendingHumanRequestRecovery(): Promise<void> {
  if (!humanRequestRecoveryHydration) {
    const hydration = hydratePendingHumanRequestRecoveryOnce();
    const wrappedHydration = hydration.finally(() => {
      if (humanRequestRecoveryHydration === wrappedHydration) {
        humanRequestRecoveryHydration = null;
      }
    });
    humanRequestRecoveryHydration = wrappedHydration;
  }
  return humanRequestRecoveryHydration;
}

async function hydratePendingHumanRequestRecoveryOnce(): Promise<void> {
  const legacyManifests = await readHumanRequestRecoveryManifests();
  for (const { legacySlot } of humanRequestRecoveryManifestKinds) {
    const serialized = legacyManifests.get(legacySlot) ?? readSessionValue(legacySlot);
    if (!serialized) continue;
    let descriptor: HumanRequestRecoveryManifestDescriptor;
    try {
      descriptor = describeHumanRequestRecoveryManifest(serialized);
    } catch {
      continue;
    }
    await migrateLegacyHumanRequestRecoveryManifest(
      legacySlot,
      humanRequestRecoveryManifestKey(descriptor.kind, descriptor.requestRef),
      serialized,
      legacyManifests.has(legacySlot),
    );
  }

  const manifests = await reconcileHumanRequestRecoveryDatabase();
  humanRequestRecoveryManifestCache.clear();
  manifests.forEach((serialized, key) => {
    humanRequestRecoveryManifestCache.set(key, serialized);
    const descriptor = describeHumanRequestRecoveryManifest(serialized);
    if (descriptor.pendingWrite) {
      seedPendingWrite(descriptor.pendingWrite.slot, descriptor.pendingWrite.key);
    }
  });
  humanRequestRecoveryManifestKinds.forEach(({ kind }) => {
    syncHumanRequestRecoverySessionCache(kind);
  });
}

function seedPendingWrite(slot: string, key: string): void {
  inMemoryPendingWrites.set(slot, key);
  writeSessionValue(slot, key);
}

type HumanRequestRecoveryManifestDescriptor = {
  kind: HumanRequestRecoveryManifestKind;
  requestRef: string;
  sealed?: PendingHumanRequestAssetResponse["sealed_response"];
  pendingWrite?: { slot: string; key: string };
};

function describeHumanRequestRecoveryManifest(
  serialized: string,
): HumanRequestRecoveryManifestDescriptor {
  let schema: unknown;
  try {
    schema = (JSON.parse(serialized) as { schema?: unknown }).schema;
  } catch {
    throw new ProductError("human_request_recovery_manifest_invalid");
  }
  if (schema === "meta-research/human-request-response/v1") {
    const response = parsePendingHumanRequestResponse(serialized);
    if (!response) throw new ProductError("human_request_response_recovery_invalid");
    return {
      kind: "response",
      requestRef: response.request_ref,
      sealed: response.sealed_response,
      pendingWrite: {
        slot: response.response_write_slot,
        key: response.response_idempotency_key,
      },
    };
  }
  if (schema === "meta-research/human-request-asset-response/v1") {
    const response = parsePendingHumanRequestAssetResponse(serialized);
    if (!response) {
      throw new ProductError("human_request_asset_response_recovery_invalid");
    }
    return {
      kind: "asset-response",
      requestRef: response.request_ref,
      sealed: response.sealed_response,
      pendingWrite: {
        slot: response.response_write_slot,
        key: response.response_idempotency_key,
      },
    };
  }
  if (schema === "meta-research/human-request-asset-intake/v1") {
    const operation = parsePendingHumanRequestAssetIntakeOperation(serialized);
    if (!operation) {
      throw new ProductError("human_request_asset_intake_recovery_invalid");
    }
    return {
      kind: "asset-intake",
      requestRef: operation.request_ref,
      sealed: operation.sealed_operation,
      pendingWrite: {
        slot: operation.asset_write_slot,
        key: operation.asset_idempotency_key,
      },
    };
  }
  if (schema === "meta-research/human-request-accepted-asset/v1") {
    const accepted = parsePendingAcceptedHumanRequestAsset(serialized);
    if (!accepted) {
      throw new ProductError("human_request_accepted_asset_recovery_invalid");
    }
    return { kind: "accepted-asset", requestRef: accepted.request_ref };
  }
  throw new ProductError("human_request_recovery_manifest_invalid");
}

function syncHumanRequestRecoverySessionCache(
  kind: HumanRequestRecoveryManifestKind,
): void {
  const legacySlot = humanRequestRecoveryManifestKinds.find(
    (entry) => entry.kind === kind,
  )!.legacySlot;
  const serialized = recoveryManifestValues(kind)[0];
  if (serialized) writeSessionValue(legacySlot, serialized);
  else removeSessionValue(legacySlot);
}

async function storeHumanRequestRecoveryRecord(
  kind: HumanRequestRecoveryManifestKind,
  requestRef: string,
  serialized: string,
  prepared?: PreparedHumanRequestRecoveryPayload,
): Promise<void> {
  const manifestKey = humanRequestRecoveryManifestKey(kind, requestRef);
  const database = await openHumanRequestRecoveryDatabase();
  let conflict = false;
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [
          humanRequestRecoveryKeyStore,
          humanRequestRecoveryCiphertextStore,
          humanRequestRecoveryManifestStore,
        ],
        "readwrite",
      );
      const manifestStore = transaction.objectStore(humanRequestRecoveryManifestStore);
      const existingRequest = manifestStore.get(manifestKey);
      existingRequest.onsuccess = () => {
        const existing = existingRequest.result;
        if (existing !== undefined && existing !== serialized) {
          conflict = true;
          transaction.abort();
          return;
        }
        if (existing === serialized) return;
        if (prepared) {
          transaction.objectStore(humanRequestRecoveryKeyStore)
            .put(prepared.key, prepared.sealed.key_ref);
          transaction.objectStore(humanRequestRecoveryCiphertextStore)
            .put(prepared.ciphertext, prepared.sealed.ciphertext_ref);
        }
        manifestStore.put(serialized, manifestKey);
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
      transaction.onabort = () => reject(new ProductError(conflict
        ? "human_request_recovery_manifest_conflict"
        : "human_request_recovery_manifest_store_unavailable"));
    });
  } finally {
    database.close();
  }
  humanRequestRecoveryManifestCache.set(manifestKey, serialized);
  syncHumanRequestRecoverySessionCache(kind);
}

async function deleteHumanRequestRecoveryRecord(
  kind: HumanRequestRecoveryManifestKind,
  requestRef: string,
  expectedSerialized: string,
  sealed?: PendingHumanRequestAssetResponse["sealed_response"],
): Promise<void> {
  const manifestKey = humanRequestRecoveryManifestKey(kind, requestRef);
  const database = await openHumanRequestRecoveryDatabase();
  let conflict = false;
  let deleted = false;
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [
          humanRequestRecoveryKeyStore,
          humanRequestRecoveryCiphertextStore,
          humanRequestRecoveryManifestStore,
        ],
        "readwrite",
      );
      const manifestStore = transaction.objectStore(humanRequestRecoveryManifestStore);
      const currentRequest = manifestStore.get(manifestKey);
      currentRequest.onsuccess = () => {
        const current = currentRequest.result;
        if (current === undefined) return;
        if (current !== expectedSerialized) {
          conflict = true;
          transaction.abort();
          return;
        }
        manifestStore.delete(manifestKey);
        if (sealed) {
          transaction.objectStore(humanRequestRecoveryKeyStore).delete(sealed.key_ref);
          transaction.objectStore(humanRequestRecoveryCiphertextStore)
            .delete(sealed.ciphertext_ref);
        }
        deleted = true;
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
      transaction.onabort = () => reject(new ProductError(conflict
        ? "human_request_recovery_manifest_conflict"
        : "human_request_recovery_manifest_store_unavailable"));
    });
  } finally {
    database.close();
  }
  if (deleted || humanRequestRecoveryManifestCache.get(manifestKey) === expectedSerialized) {
    humanRequestRecoveryManifestCache.delete(manifestKey);
    syncHumanRequestRecoverySessionCache(kind);
  }
}

async function readHumanRequestRecoveryManifests(): Promise<Map<string, string>> {
  const database = await openHumanRequestRecoveryDatabase();
  try {
    return await new Promise<Map<string, string>>((resolve, reject) => {
      const transaction = database.transaction(
        humanRequestRecoveryManifestStore,
        "readonly",
      );
      const store = transaction.objectStore(humanRequestRecoveryManifestStore);
      const keysRequest = store.getAllKeys();
      const valuesRequest = store.getAll();
      transaction.oncomplete = () => {
        const keys = keysRequest.result;
        const values = valuesRequest.result;
        const entries = keys.flatMap((key, index) => (
          typeof key === "string" && typeof values[index] === "string"
            ? [[key, values[index]] as const]
            : []
        ));
        resolve(new Map(entries));
      };
      transaction.onerror = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
      transaction.onabort = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
    });
  } finally {
    database.close();
  }
}

async function migrateLegacyHumanRequestRecoveryManifest(
  legacyKey: string,
  manifestKey: string,
  serialized: string,
  deleteLegacy: boolean,
): Promise<void> {
  const database = await openHumanRequestRecoveryDatabase();
  let conflict = false;
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        humanRequestRecoveryManifestStore,
        "readwrite",
      );
      const store = transaction.objectStore(humanRequestRecoveryManifestStore);
      const currentRequest = store.get(manifestKey);
      currentRequest.onsuccess = () => {
        if (currentRequest.result !== undefined && currentRequest.result !== serialized) {
          conflict = true;
          transaction.abort();
          return;
        }
        if (currentRequest.result === undefined) store.put(serialized, manifestKey);
        if (deleteLegacy) store.delete(legacyKey);
      };
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
      transaction.onabort = () => reject(new ProductError(conflict
        ? "human_request_recovery_manifest_conflict"
        : "human_request_recovery_manifest_store_unavailable"));
    });
  } finally {
    database.close();
  }
}

async function reconcileHumanRequestRecoveryDatabase(): Promise<Map<string, string>> {
  const database = await openHumanRequestRecoveryDatabase();
  let validManifests = new Map<string, string>();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(
        [
          humanRequestRecoveryKeyStore,
          humanRequestRecoveryCiphertextStore,
          humanRequestRecoveryManifestStore,
        ],
        "readwrite",
      );
      const keyStore = transaction.objectStore(humanRequestRecoveryKeyStore);
      const ciphertextStore = transaction.objectStore(humanRequestRecoveryCiphertextStore);
      const manifestStore = transaction.objectStore(humanRequestRecoveryManifestStore);
      const manifestKeysRequest = manifestStore.getAllKeys();
      const manifestValuesRequest = manifestStore.getAll();
      const keyRefsRequest = keyStore.getAllKeys();
      const ciphertextRefsRequest = ciphertextStore.getAllKeys();
      let completedReads = 0;
      const inspect = () => {
        completedReads += 1;
        if (completedReads !== 4) return;
        const existingKeyRefs = new Set(
          keyRefsRequest.result.filter((key): key is string => typeof key === "string"),
        );
        const existingCiphertextRefs = new Set(
          ciphertextRefsRequest.result.filter(
            (key): key is string => typeof key === "string",
          ),
        );
        const referencedKeyRefs = new Set<string>();
        const referencedCiphertextRefs = new Set<string>();
        const next = new Map<string, string>();
        manifestKeysRequest.result.forEach((key, index) => {
          const serialized = manifestValuesRequest.result[index];
          if (typeof key !== "string" || typeof serialized !== "string") {
            manifestStore.delete(key);
            return;
          }
          try {
            const descriptor = describeHumanRequestRecoveryManifest(serialized);
            const exactKey = humanRequestRecoveryManifestKey(
              descriptor.kind,
              descriptor.requestRef,
            );
            const payloadPresent = !descriptor.sealed
              || (existingKeyRefs.has(descriptor.sealed.key_ref)
                && existingCiphertextRefs.has(descriptor.sealed.ciphertext_ref));
            if (key !== exactKey || !payloadPresent) {
              manifestStore.delete(key);
              return;
            }
            next.set(key, serialized);
            if (descriptor.sealed) {
              referencedKeyRefs.add(descriptor.sealed.key_ref);
              referencedCiphertextRefs.add(descriptor.sealed.ciphertext_ref);
            }
          } catch {
            manifestStore.delete(key);
          }
        });
        existingKeyRefs.forEach((key) => {
          if (!referencedKeyRefs.has(key)) keyStore.delete(key);
        });
        existingCiphertextRefs.forEach((key) => {
          if (!referencedCiphertextRefs.has(key)) ciphertextStore.delete(key);
        });
        validManifests = next;
      };
      manifestKeysRequest.onsuccess = inspect;
      manifestValuesRequest.onsuccess = inspect;
      keyRefsRequest.onsuccess = inspect;
      ciphertextRefsRequest.onsuccess = inspect;
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
      transaction.onabort = () => reject(new ProductError(
        "human_request_recovery_manifest_store_unavailable",
      ));
    });
  } finally {
    database.close();
  }
  return validManifests;
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
  latestSnapshotRevision?: () => number | null,
  onTargetRootObservationsAvailable?: (
    pointer: TargetRootObservationPointer,
  ) => void,
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
    "human_collaboration.manual_question_creation_opened",
    "human_collaboration.manual_creation_seed_confirmed",
    "human_collaboration.manual_deepfetch_requested",
    "human_collaboration.manual_deepfetch_retried",
    "human_collaboration.manual_deepfetch_completed",
    "human_collaboration.manual_deepfetch_failed",
    "human_collaboration.manual_deepfetch_waived",
    "human_collaboration.manual_drafting_turn_completed",
    "human_collaboration.manual_question_proposal_submitted",
    "human_collaboration.manual_question_proposal_confirmed",
    "human_collaboration.manual_question_content_observed",
    "human_collaboration.manual_question_recovery_pending",
    "human_collaboration.manual_question_creation_completed",
    "human_collaboration.manual_question_creation_cancelled",
    "human_collaboration.companion_message_queued",
    "human_collaboration.companion_reply_recorded",
    "human_collaboration.companion_reply_failed",
    "human_collaboration.soft_constraint_recorded",
    "human_collaboration.soft_constraint_withdrawn",
    "human_collaboration.agent_proposal_recorded",
    "human_collaboration.command_draft_created",
    "human_collaboration.command_draft_revised",
    "human_collaboration.command_preview_recorded",
    "human_collaboration.command_confirmed",
    "human_collaboration.capability_authorization_recorded",
    "human_collaboration.human_request_responded",
    "human_collaboration.human_request_response_recorded",
    "research_graph.human_request_opened",
    "research_graph.human_request_revised",
    "research_graph.human_request_evaluated",
    "research_graph.human_request_resume_validated",
    "research_graph.human_request_resume_consumed",
    "research_memory.human_request_opened",
    "research_memory.human_request_revised",
    "research_memory.human_request_evaluated",
    "research_memory.human_request_resume_validated",
    "research_memory.human_request_resume_consumed",
    "agent_runtime.human_request_opened",
    "agent_runtime.human_request_revised",
    "agent_runtime.human_request_evaluated",
    "agent_runtime.human_request_resume_validated",
    "agent_runtime.human_request_resume_consumed",
    "advancement_engine.human_request_opened",
    "advancement_engine.human_request_revised",
    "advancement_engine.human_request_evaluated",
    "advancement_engine.human_request_resume_validated",
    "advancement_engine.human_request_resume_consumed",
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
    "research_graph.experiment_admitted",
    "agent_runtime.experiment_admitted",
    "agent_runtime.experiment_started",
    "agent_runtime.experiment_observed",
    "agent_runtime.experiment_completed",
    "agent_runtime.experiment_failed",
    "agent_runtime.experiment_replaced",
    "agent_runtime.experiment_recovered",
    "research_graph.experiment_assets_accepted",
    "research_graph.formal_measurement_accepted",
    "research_graph.formal_measurement_rejected",
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
    const latestRevision = latestSnapshotRevision?.();
    if (
      latestRevision !== null
      && latestRevision !== undefined
      && Number.isSafeInteger(latestRevision)
      && latestRevision > cursor
    ) {
      cursor = latestRevision;
    }
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
    next.addEventListener(
      "agent_runtime.target_root_observations_available",
      (event) => {
        if (!onTargetRootObservationsAvailable) return;
        try {
          const payload = JSON.parse((event as MessageEvent<string>).data) as
            Partial<TargetRootObservationPointer>;
          if (
            typeof payload.target_ref !== "string"
            || !payload.target_ref
            || typeof payload.target_run_ref !== "string"
            || !payload.target_run_ref
            || typeof payload.stream_ref !== "string"
            || !payload.stream_ref
            || typeof payload.head_cursor !== "string"
            || !payload.head_cursor
          ) return;
          onTargetRootObservationsAvailable(
            payload as TargetRootObservationPointer,
          );
        } catch {
          // A malformed advisory pointer cannot alter the monotonic Projection.
        }
      },
    );
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
