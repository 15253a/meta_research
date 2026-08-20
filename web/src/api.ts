export type ReadinessCheck = {
  name: string;
  status: "ready" | "stale" | "unavailable";
  revision?: number;
  count?: number;
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

export type QuestDraft = {
  goal: string;
  completion_criteria: string;
  key_configuration: string;
  literature_scope: "comprehensive" | "open_access" | "provided_materials";
  initial_question_direction: string;
  material_receipts: string[];
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

export type QuestCreationView = {
  initialization_id: string;
  creation_context: "quest_initialization";
  route: "direct";
  status: "draft" | "proposal_ready" | "dispatching" | "completed" | "cancelled";
  quest_draft: { revision: number; hash: string; value: QuestDraft };
  proposal: null | {
    ref: string;
    revision: number;
    hash: string;
    basis_revision: number;
    basis_hash: string;
    status: "current" | "stale";
    content: QuestionContent;
  };
  confirmation_preview: null | {
    ref: string;
    hash: string;
    basis_revision: number;
    basis_hash: string;
    proposal_ref: string;
    proposal_hash: string;
    status: "current" | "stale" | "consumed";
    target_assertions: Array<{
      owner: string;
      operation: string;
      may_change: string[];
      will_not_change: string[];
      preconditions: string[];
      risks: string[];
      stale_if: string[];
      target_hash: string;
    }>;
  };
  receipts: Record<
    | "human_confirmation"
    | "quest_goal"
    | "question_content"
    | "question_identity"
    | "cycle_activation",
    ReceiptState
  >;
  canonical_empty_advancement: boolean;
  quest_ref?: string;
  memory_ref?: string;
  question_ref?: string;
  cycle_ref?: string;
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
  };
  owners: Record<string, OwnerSnapshot>;
  quest_creation: {
    status: "ready";
    route: "direct";
    current: QuestCreationView | null;
    accepted_material_basis: UnavailableCapability;
    first_question_deepfetch: UnavailableCapability;
  };
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

export function createQuest(draft: QuestDraft): Promise<QuestCreationView> {
  return writeJson("/api/v1/quest-initializations", "POST", draft);
}

export function reviseQuestDraft(
  creation: QuestCreationView,
  draft: QuestDraft,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/draft`,
    "PUT",
    { ...draft, expected_draft_hash: creation.quest_draft.hash },
  );
}

export function generateQuestionProposal(
  creation: QuestCreationView,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/proposal`,
    "POST",
    { expected_draft_hash: creation.quest_draft.hash },
  );
}

export function saveQuestionProposal(
  creation: QuestCreationView,
  content: QuestionContent,
): Promise<QuestCreationView> {
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/proposal`,
    "PUT",
    { expected_draft_hash: creation.quest_draft.hash, content },
  );
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

export function previewQuestConfirmation(
  creation: QuestCreationView,
): Promise<QuestCreationView> {
  if (!creation.proposal) throw new ProductError("question_proposal_missing");
  return writeJson(
    `/api/v1/quest-initializations/${creation.initialization_id}/confirmation-preview`,
    "POST",
    {
      quest_draft_revision: creation.quest_draft.revision,
      quest_draft_hash: creation.quest_draft.hash,
      proposal_ref: creation.proposal.ref,
      proposal_hash: creation.proposal.hash,
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
  const stream = new EventSource(`/api/v1/events?after=${afterRevision}`);
  stream.onopen = () => onConnection(true);
  stream.onerror = () => onConnection(false);
  const update = (event: Event) => {
    onRevision(Number((event as MessageEvent).lastEventId));
  };
  for (const eventType of [
    "system.ready",
    "human_collaboration.quest_draft_created",
    "human_collaboration.quest_draft_revised",
    "human_collaboration.question_proposal_recorded",
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
  ]) {
    stream.addEventListener(eventType, update);
  }
  stream.addEventListener("snapshot.required", () => onSnapshotRequired());
  return () => stream.close();
}
