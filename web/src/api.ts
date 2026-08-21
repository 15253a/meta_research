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
  };
  proposal: null | {
    ref: string;
    revision: number;
    hash: string;
    basis_revision: number;
    basis_hash: string;
    status: "current" | "incomplete" | "stale";
    content: QuestionContent;
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
  >;
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
    route: "direct";
    current: QuestCreationView | null;
    accepted_material_basis: Omit<UnavailableCapability, "capability">;
    first_question_deepfetch: Omit<UnavailableCapability, "capability">;
  };
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

async function writeJson<T>(
  path: string,
  method: "POST" | "PUT",
  body: object,
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
  pendingWrite.clear();
  return payload;
}

const inMemoryPendingWrites = new Map<string, string>();

async function reserveIdempotencyKey(
  method: string,
  path: string,
  bodyJson: string,
): Promise<{ key: string; clear: () => void }> {
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
    clear: () => {
      inMemoryPendingWrites.delete(slot);
      removeSessionValue(slot);
    },
  };
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
