import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  acknowledgeScopedAssetIntake,
  cancelQuest,
  confirmQuest,
  createQuest,
  fetchAssetIntake,
  fetchQuestCreation,
  fetchResearchAssets,
  generateQuestionProposal,
  hydrateScopedAssetIntakeRecovery,
  observeHostCompute,
  prepareAcquisitionSession,
  ProductError,
  reviseQuestDraft,
  saveQuestionProposal,
  sendIntentMessage,
  submitScopedAssetIntake,
  type IntentSessionTurn,
  type LegacyQuestDraft,
  type QuestionContent,
  type QuestReceiptState,
  type AssetIntakeResult,
  type ResearchAssetItem,
  type ScopedAssetIntakeContext,
  type QuestCreationView,
  type QuestDraft,
} from "./api";
import "./quest-creation.css";

const QUEST_MATERIAL_LIMIT = 100;
const QUEST_MATERIAL_MAX_BYTES = 64 * 1024 * 1024;
const QUEST_MATERIAL_POLL_TIMEOUT_MS = 30 * 60 * 1_000;
const BOUNDARY_COMPLETION_MAX_LENGTH = 4_000;
const BOUNDARY_EXCLUSIONS_MAX_LENGTH = 8_000;
const BOUNDARY_SEGMENT_SEPARATOR = "\n\n范围与排除：";
const BOUNDARY_RAW_INPUT_MAX_LENGTH =
  BOUNDARY_COMPLETION_MAX_LENGTH + BOUNDARY_EXCLUSIONS_MAX_LENGTH;
const BOUNDARY_CANONICAL_INPUT_MAX_LENGTH =
  BOUNDARY_RAW_INPUT_MAX_LENGTH +
  BOUNDARY_SEGMENT_SEPARATOR.length;
const questMaterialIntakeFlights = new Map<string, Promise<AssetIntakeResult>>();
const questMaterialBindingFlights = new Map<string, Promise<QuestCreationView>>();
const questMaterialCommitFlights = new Map<string, Promise<void>>();

const blankDraft: QuestDraft = {
  goal: "",
  completion_criteria: "",
  time_budget: "30d",
  route: "direct",
  resource_envelope_ref: null,
  resource_envelope_hash: null,
  literature: {
    mode: "oa_then_institution",
    library_entry_url: "",
    scope_exclusions: "",
    accepted_material_bindings: [],
  },
  background_and_initial_direction: "",
};

const proposalFields: Array<{
  key: keyof QuestionContent;
  label: string;
  code: string;
  required?: boolean;
  rows: number;
  full?: boolean;
  placeholder: string;
}> = [
  {
    key: "title",
    label: "首问题标题",
    code: "title",
    required: true,
    rows: 1,
    full: true,
    placeholder: "简短、稳定的导航标题",
  },
  {
    key: "unknown_statement",
    label: "首问题要解决的未知",
    code: "unknown_statement",
    required: true,
    rows: 3,
    placeholder: "真正还不知道、需要研究回答的是什么？",
  },
  {
    key: "answer_shape",
    label: "首问题的答案形态",
    code: "answer_shape",
    required: true,
    rows: 3,
    placeholder: "什么类型的答案才足够？",
  },
  {
    key: "applicability_scope",
    label: "首问题的适用范围",
    code: "applicability_scope",
    required: true,
    rows: 3,
    full: true,
    placeholder: "答案适用于哪些对象、条件和范围？明确排除什么？",
  },
  {
    key: "background_context",
    label: "首问题背景上下文",
    code: "background_context",
    rows: 3,
    placeholder: "理解这个问题所需的背景。",
  },
  {
    key: "requirements_constraints",
    label: "首问题含义约束",
    code: "requirements_constraints",
    rows: 3,
    placeholder: "只写会改变问题含义的要求。",
  },
];

type SaveState = "opening" | "restored" | "unsaved" | "saving" | "saved" | "error";
type Operation =
  | "compute"
  | "acquisition"
  | "generating"
  | "reviewing"
  | "intent"
  | "confirming"
  | "cancelling"
  | "closing";
type InFlightOperations = Record<Operation, boolean>;
type ProductFailure = { code: string; message: string };
type WriteConflict = "draft" | "proposal";
type MaterialUploadStatus = "reading" | "accepting" | "binding" | "failed";
type MaterialUpload = {
  key: string;
  name: string;
  status: MaterialUploadStatus;
  reason?: string;
};
type QuestAcceptedMaterialBinding =
  QuestDraft["literature"]["accepted_material_bindings"][number];

type ApplyViewOptions = {
  syncDraft?: boolean;
  syncProposal?: boolean;
  adoptDirtyDraftBasis?: boolean;
  acceptedMaterialAdditions?: QuestAcceptedMaterialBinding[];
  adoptDirtyProposalBasis?: boolean;
  notify?: boolean;
};

type DraftWriteBasis = {
  revision: number;
  hash: string;
  value: QuestDraft;
};

type ProposalWriteBasis = {
  draftRevision: number;
  draftHash: string;
  proposalRef: string | null;
  proposalHash: string | null;
};

type NormalizedQuestCreationView = Omit<QuestCreationView, "quest_draft"> & {
  quest_draft: Omit<QuestCreationView["quest_draft"], "value"> & {
    value: QuestDraft;
  };
};

const idleOperations: InFlightOperations = {
  compute: false,
  acquisition: false,
  generating: false,
  reviewing: false,
  intent: false,
  confirming: false,
  cancelling: false,
  closing: false,
};

export function QuestCreationWorkbench({
  current,
  researchAssets,
  researchAssetInventoryRevision,
  researchAssetTotal,
  onClose,
  onChanged,
}: {
  current: QuestCreationView | null;
  researchAssets: ResearchAssetItem[];
  researchAssetInventoryRevision: number;
  researchAssetTotal: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const normalizedCurrent = current ? normalizeQuestCreationView(current) : null;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const errorFocusRef = useRef<
    "draft" | "compute" | "acquisition" | "proposal" | "conflict" | "alert"
  >("alert");
  const computeButtonRef = useRef<HTMLButtonElement>(null);
  const acquisitionButtonRef = useRef<HTMLButtonElement>(null);
  const proposalActionRef = useRef<HTMLButtonElement>(null);
  const conflictRecoveryRef = useRef<HTMLButtonElement>(null);
  const firstRequiredRef = useRef<HTMLTextAreaElement>(null);
  const materialFilesInputRef = useRef<HTMLInputElement>(null);
  const materialFolderInputRef = useRef<HTMLInputElement>(null);
  const materialUploadQueueRef = useRef<Promise<void>>(Promise.resolve());
  const materialUploadsRef = useRef<MaterialUpload[]>([]);
  const recoveredMaterialJobsRef = useRef(new Set<string>());
  const mountedRef = useRef(true);
  const openingPromiseRef = useRef<Promise<QuestCreationView> | null>(null);
  const draftTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const proposalTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeAnimationTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draftSavePromiseRef = useRef<Promise<QuestCreationView | null> | null>(null);
  const proposalSavePromiseRef = useRef<Promise<QuestCreationView | null> | null>(null);
  const closingRef = useRef(false);
  const operationSequenceRef = useRef(0);
  const operationTokensRef = useRef<Record<Operation, number>>({
    compute: 0,
    acquisition: 0,
    generating: 0,
    reviewing: 0,
    intent: 0,
    confirming: 0,
    cancelling: 0,
    closing: 0,
  });
  const recoveryPollRef = useRef<{
    initializationId: string;
    promise: Promise<QuestCreationView>;
  } | null>(null);

  const [creation, setCreation] = useState<NormalizedQuestCreationView | null>(
    normalizedCurrent,
  );
  const creationRef = useRef<NormalizedQuestCreationView | null>(normalizedCurrent);
  const [draft, setDraft] = useState<QuestDraft>(
    normalizedCurrent?.quest_draft.value ?? cloneDraft(blankDraft),
  );
  const draftRef = useRef(draft);
  const draftDirtyRef = useRef(false);
  const draftWriteBasisRef = useRef<DraftWriteBasis | null>(null);
  const [proposal, setProposal] = useState<QuestionContent | null>(
    current?.proposal?.content ?? null,
  );
  const proposalRef = useRef<QuestionContent | null>(proposal);
  const proposalDirtyRef = useRef(false);
  const proposalWriteBasisRef = useRef<ProposalWriteBasis | null>(null);
  const writeConflictRef = useRef<WriteConflict | null>(null);
  const [writeConflict, setWriteConflict] = useState<WriteConflict | null>(null);
  const [proposalDirty, setProposalDirty] = useState(false);
  const [draftSaveState, setDraftSaveState] = useState<SaveState>(
    current ? "restored" : "opening",
  );
  const [proposalSaveState, setProposalSaveState] = useState<SaveState>(
    current?.proposal ? "restored" : "opening",
  );
  const [hasEditedDraft, setHasEditedDraft] = useState(false);
  const [inFlight, setInFlight] = useState<InFlightOperations>(idleOperations);
  const [error, setError] = useState<ProductFailure | null>(null);
  const [intentText, setIntentText] = useState("");
  const [dialogOpen, setDialogOpen] = useState(false);
  const [materialPickerOpen, setMaterialPickerOpen] = useState(false);
  const [materialAssets, setMaterialAssets] = useState(researchAssets);
  const [materialInventoryRevision, setMaterialInventoryRevision] = useState(
    researchAssetInventoryRevision,
  );
  const materialInventoryRevisionRef = useRef(
    researchAssetInventoryRevision,
  );
  const [materialTotal, setMaterialTotal] = useState(researchAssetTotal);
  const [materialsLoading, setMaterialsLoading] = useState(false);
  const [materialUploads, setMaterialUploads] = useState<MaterialUpload[]>([]);

  const replaceMaterialUploads = (
    update: (current: MaterialUpload[]) => MaterialUpload[],
  ) => {
    if (!mountedRef.current) return;
    const next = update(materialUploadsRef.current);
    materialUploadsRef.current = next;
    setMaterialUploads(next);
  };

  useEffect(() => {
    materialFolderInputRef.current?.setAttribute("webkitdirectory", "");
  }, []);

  useEffect(() => {
    materialInventoryRevisionRef.current = researchAssetInventoryRevision;
    setMaterialAssets((current) => {
      if (materialInventoryRevision !== researchAssetInventoryRevision) {
        return researchAssets;
      }
      const byRef = new Map(
        current.map((item) => [item.memory_ref, item]),
      );
      for (const item of researchAssets) byRef.set(item.memory_ref, item);
      return [...byRef.values()];
    });
    setMaterialInventoryRevision(researchAssetInventoryRevision);
    setMaterialTotal(researchAssetTotal);
  }, [researchAssetInventoryRevision, researchAssetTotal, researchAssets]);

  const loadMoreMaterials = useCallback(async () => {
    if (materialsLoading || materialAssets.length >= materialTotal) return;
    setMaterialsLoading(true);
    try {
      const next = await fetchResearchAssets(
        undefined,
        materialAssets.length,
        50,
      );
      if (
        next.inventory_revision !== materialInventoryRevisionRef.current
      ) {
        const currentPage = await fetchResearchAssets();
        setMaterialAssets(currentPage.items);
        materialInventoryRevisionRef.current = currentPage.inventory_revision;
        setMaterialInventoryRevision(currentPage.inventory_revision);
        setMaterialTotal(currentPage.total_count);
        return;
      }
      setMaterialAssets((current) => {
        const byRef = new Map(current.map((item) => [item.memory_ref, item]));
        for (const item of next.items) byRef.set(item.memory_ref, item);
        return [...byRef.values()];
      });
      setMaterialTotal(next.total_count);
    } catch (caught) {
      const code = caught instanceof ProductError ? caught.code : "unknown_error";
      errorFocusRef.current = "alert";
      setError({ code, message: messageFor(code) });
    } finally {
      setMaterialsLoading(false);
    }
  }, [
    materialAssets.length,
    materialInventoryRevision,
    materialTotal,
    materialsLoading,
  ]);

  const applyView = useCallback((
    received: QuestCreationView,
    options: ApplyViewOptions = {},
  ): boolean => {
    const next = normalizeQuestCreationView(received);
    const previous = creationRef.current;
    if (previous && previous.initialization_id !== next.initialization_id) return false;
    if (previous && viewIsOlder(previous, next)) return false;
    creationRef.current = next;
    setCreation(next);
    if (options.adoptDirtyDraftBasis && draftDirtyRef.current) {
      const nextDraft = cloneDraft(next.quest_draft.value);
      const mergedDraft = mergeServerManagedDraft(
        draftRef.current,
        nextDraft,
        options.acceptedMaterialAdditions ?? [],
      );
      const stillDirty = !sameDraft(mergedDraft, nextDraft);
      draftRef.current = mergedDraft;
      draftDirtyRef.current = stillDirty;
      draftWriteBasisRef.current = stillDirty
        ? draftBasisOf(next)
        : null;
      if (proposalWriteBasisRef.current) {
        proposalWriteBasisRef.current = {
          ...proposalWriteBasisRef.current,
          draftRevision: next.quest_draft.revision,
          draftHash: next.quest_draft.hash,
        };
      }
      setDraft(mergedDraft);
      if (!stillDirty) setDraftSaveState("saved");
    } else if (options.syncDraft) {
      const nextDraft = cloneDraft(next.quest_draft.value);
      if (!draftDirtyRef.current || sameDraft(draftRef.current, nextDraft)) {
        const wasDirty = draftDirtyRef.current;
        draftRef.current = nextDraft;
        draftDirtyRef.current = false;
        draftWriteBasisRef.current = null;
        setDraft(nextDraft);
        if (wasDirty) setDraftSaveState("saved");
      }
    }
    if (options.adoptDirtyProposalBasis && proposalDirtyRef.current) {
      const nextProposal = next.proposal ? { ...next.proposal.content } : null;
      const stillDirty = !sameQuestion(proposalRef.current, nextProposal);
      proposalDirtyRef.current = stillDirty;
      proposalWriteBasisRef.current = stillDirty
        ? proposalBasisOf(next)
        : null;
      if (!stillDirty) {
        proposalRef.current = nextProposal;
        setProposal(nextProposal);
        setProposalDirty(false);
        setProposalSaveState(nextProposal ? "saved" : "opening");
      }
    } else if (options.syncProposal) {
      const nextProposal = next.proposal ? { ...next.proposal.content } : null;
      if (!proposalDirtyRef.current || sameQuestion(proposalRef.current, nextProposal)) {
        proposalRef.current = nextProposal;
        proposalDirtyRef.current = false;
        proposalWriteBasisRef.current = null;
        setProposal(nextProposal);
        setProposalDirty(false);
        setProposalSaveState(nextProposal ? "saved" : "opening");
      }
    }
    if (options.notify !== false) onChanged();
    return true;
  }, [onChanged]);

  const beginOperation = useCallback((kind: Operation): number => {
    const token = operationSequenceRef.current + 1;
    operationSequenceRef.current = token;
    operationTokensRef.current[kind] = token;
    setInFlight((active) => ({ ...active, [kind]: true }));
    return token;
  }, []);

  const operationIsCurrent = useCallback((kind: Operation, token: number): boolean => (
    mountedRef.current && operationTokensRef.current[kind] === token
  ), []);

  const finishOperation = useCallback((kind: Operation, token: number) => {
    if (!mountedRef.current || operationTokensRef.current[kind] !== token) return;
    setInFlight((active) => ({ ...active, [kind]: false }));
  }, []);

  const invalidateConcurrentOperations = useCallback((except: Operation) => {
    const nextTokens = { ...operationTokensRef.current };
    for (const kind of Object.keys(nextTokens) as Operation[]) {
      if (kind !== except) nextTokens[kind] = operationSequenceRef.current + 1;
      operationSequenceRef.current = Math.max(operationSequenceRef.current, nextTokens[kind]);
    }
    operationTokensRef.current = nextTokens;
    setInFlight((active) => Object.fromEntries(
      (Object.keys(active) as Operation[]).map((kind) => [kind, kind === except && active[kind]]),
    ) as InFlightOperations);
  }, []);

  const showError = useCallback((caught: unknown) => {
    const code = caught instanceof ProductError ? caught.code : "unknown_error";
    errorFocusRef.current = writeConflictRef.current || [
      "quest_draft_stale",
      "question_proposal_stale",
      "quest_reload_stale",
    ].includes(code)
      ? "conflict"
      : ["quest_basis_incomplete"].includes(code)
      ? "draft"
      : ["resource_envelope_required", "compute_device_selection_stale"].includes(code)
      ? "compute"
      : [
          "acquisition_session_required",
          "acquisition_session_stale",
          "acquisition_session_not_ready",
          "deepfetch_acquisition_not_ready",
          "institutional_entry_required",
          "institutional_login_required",
        ].includes(code)
        ? "acquisition"
      : [
            "confirmation_preview_required",
            "confirmation_preview_stale",
          ].includes(code)
          ? "proposal"
          : "alert";
    setError({ code, message: messageFor(code) });
  }, []);

  useEffect(() => {
    if (!error && !writeConflict) return;
    const target = {
      draft: firstRequiredRef.current,
      compute: computeButtonRef.current,
      acquisition: acquisitionButtonRef.current,
      proposal: proposalActionRef.current,
      conflict: conflictRecoveryRef.current,
      alert: errorRef.current,
    }[errorFocusRef.current];
    target?.focus();
  }, [error, writeConflict]);

  useEffect(() => {
    mountedRef.current = true;
    let active = true;
    const dialog = dialogRef.current;
    if (!returnFocusRef.current && document.activeElement instanceof HTMLElement) {
      returnFocusRef.current = document.activeElement;
    }
    if (dialog && !dialog.open) dialog.showModal();
    let focusFrame: number | null = null;
    const focusWhenVisible = () => {
      if (!active) return;
      const closeButton = closeButtonRef.current;
      if (
        dialog?.dataset.open === "true" &&
        closeButton &&
        closeButton.getClientRects().length > 0 &&
        getComputedStyle(closeButton).visibility !== "hidden"
      ) {
        closeButton.focus({ preventScroll: true });
        return;
      }
      focusFrame = requestAnimationFrame(focusWhenVisible);
    };
    const openFrame = requestAnimationFrame(() => {
      if (!active) return;
      setDialogOpen(true);
      focusFrame = requestAnimationFrame(focusWhenVisible);
    });

    if (!openingPromiseRef.current) {
      openingPromiseRef.current = current && !["completed", "cancelled"].includes(current.status)
        ? fetchQuestCreation(current.initialization_id)
        : createQuest();
    }
    void openingPromiseRef.current
      .then((received) => {
        if (!active) return;
        const next = normalizeQuestCreationView(received);
        const isBrandNew = !current || ["completed", "cancelled"].includes(current.status);
        const nextDraft = isBrandNew && next.quest_draft.value.time_budget === "open"
          ? { ...next.quest_draft.value, time_budget: "30d" as const }
          : next.quest_draft.value;
        applyView(next, { syncDraft: false, syncProposal: true });
        const openingDraft = cloneDraft(nextDraft);
        draftRef.current = openingDraft;
        draftDirtyRef.current = !sameDraft(next.quest_draft.value, openingDraft);
        draftWriteBasisRef.current = draftDirtyRef.current ? draftBasisOf(next) : null;
        setDraft(openingDraft);
        setDraftSaveState(draftDirtyRef.current ? "unsaved" : "restored");
      })
      .catch((caught) => {
        if (!active) return;
        setDraftSaveState("error");
        showError(caught);
      });

    return () => {
      active = false;
      mountedRef.current = false;
      cancelAnimationFrame(openFrame);
      if (focusFrame !== null) cancelAnimationFrame(focusFrame);
      if (draftTimerRef.current !== null) clearTimeout(draftTimerRef.current);
      if (proposalTimerRef.current !== null) clearTimeout(proposalTimerRef.current);
      if (closeAnimationTimerRef.current !== null) clearTimeout(closeAnimationTimerRef.current);
    };
    // Opening is intentionally tied to this dialog lifecycle. Unsafe retries reuse
    // the request fingerprint's durable idempotency key in api.ts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const pollCreation = useCallback(async (
    initializationId: string,
    complete: (view: QuestCreationView) => boolean,
    timeoutMs = 190_000,
  ): Promise<QuestCreationView> => {
    const deadline = Date.now() + timeoutMs;
    while (mountedRef.current && Date.now() < deadline) {
      const latest = normalizeQuestCreationView(
        await fetchQuestCreation(initializationId),
      );
      if (!mountedRef.current) throw new ProductError("quest_initialization_poll_cancelled");
      applyView(latest, { notify: false });
      if (complete(latest)) return latest;
      await delay(300);
    }
    throw new ProductError("quest_initialization_poll_timeout");
  }, [applyView]);

  const resumeExecution = useCallback((basis: QuestCreationView) => {
    if (!executionIsPending(basis)) return;
    if (recoveryPollRef.current?.initializationId === basis.initialization_id) return;
    const promise = pollCreation(
      basis.initialization_id,
      (view) => ["completed", "cancelled"].includes(view.status),
    ).then((settled) => {
      if (mountedRef.current) {
        applyView(settled, {
          syncDraft: true,
          syncProposal: Boolean(settled.proposal),
        });
      }
      return settled;
    }).catch((caught) => {
      if (mountedRef.current) showError(caught);
      return creationRef.current ?? basis;
    });
    recoveryPollRef.current = {
      initializationId: basis.initialization_id,
      promise,
    };
    void promise.finally(() => {
      if (recoveryPollRef.current?.promise === promise) recoveryPollRef.current = null;
    });
  }, [applyView, pollCreation, showError]);

  useEffect(() => {
    const local = creationRef.current;
    if (!current || !local || current.initialization_id !== local.initialization_id) return;
    applyView(current, {
      syncDraft: true,
      syncProposal: true,
      notify: false,
    });
  }, [applyView, current]);

  useEffect(() => {
    if (creation) resumeExecution(creation);
  }, [creation, resumeExecution]);

  useEffect(() => {
    const basis = creation;
    if (!basis || basis.status !== "completed") return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const verifyCustody = async () => {
      try {
        const latest = await fetchQuestCreation(basis.initialization_id);
        if (!active || !mountedRef.current) return;
        applyView(latest, {
          syncDraft: true,
          syncProposal: Boolean(latest.proposal),
          notify: false,
        });
      } catch {
        // Snapshot/SSE owns connection health. Keep the last verified view and
        // retry this exact completed initialization while its dialog is open.
      } finally {
        if (active && creationRef.current?.status === "completed") {
          timer = setTimeout(() => void verifyCustody(), 1_500);
        }
      }
    };
    timer = setTimeout(() => void verifyCustody(), 1_500);
    return () => {
      active = false;
      if (timer !== null) clearTimeout(timer);
    };
  }, [applyView, creation?.initialization_id, creation?.status]);

  const settleCurrentPreview = useCallback(async (
    recorded: QuestCreationView,
  ): Promise<QuestCreationView> => {
    const targetProposalHash = recorded.proposal?.hash;
    if (!targetProposalHash || recorded.proposal?.status !== "current" || confirmationIsCurrent(recorded)) {
      return recorded;
    }
    try {
      const settled = await pollCreation(recorded.initialization_id, (view) => (
        view.proposal?.hash !== targetProposalHash || confirmationIsCurrent(view)
      ));
      if (mountedRef.current) applyView(settled);
      return settled;
    } catch (caught) {
      if (mountedRef.current) showError(caught);
      return creationRef.current ?? recorded;
    }
  }, [applyView, pollCreation, showError]);

  const persistDraft = useCallback(async function persist(): Promise<QuestCreationView | null> {
    if (Boolean(writeConflictRef.current)) return null;
    if (draftSavePromiseRef.current) {
      await draftSavePromiseRef.current;
      if (Boolean(writeConflictRef.current)) return null;
      const latest = creationRef.current;
      if (latest && !sameDraft(latest.quest_draft.value, draftRef.current)) return persist();
      return latest;
    }
    const basis = creationRef.current;
    if (!basis || creationIsLocked(basis)) return basis;
    const captured = cloneDraft(draftRef.current);
    if (sameDraft(basis.quest_draft.value, captured)) {
      draftDirtyRef.current = false;
      draftWriteBasisRef.current = null;
      setDraftSaveState(hasEditedDraft ? "saved" : "restored");
      return basis;
    }

    const expectedBasis = draftWriteBasisRef.current ?? draftBasisOf(basis);
    setDraftSaveState("saving");
    setError(null);
    const task = (async () => {
      try {
        const saved = await reviseQuestDraft(basis, captured, expectedBasis);
        if (!mountedRef.current) return saved;
        let next = saved;
        const acquisitionChanged = literatureAcquisitionConfigChanged(
          basis.quest_draft.value,
          captured,
        );
        if (
          acquisitionChanged ||
          !saved.acquisition_session ||
          saved.acquisition_session.freshness !== "current"
        ) {
          try {
            next = await prepareAcquisitionSession(saved);
          } catch (caught) {
            // The DraftRevision is already durable. Keep that truth visible
            // even if the independent environment probe could not return.
            applyView(saved, {
              syncDraft: sameDraft(draftRef.current, captured),
              syncProposal: !proposalDirtyRef.current,
              adoptDirtyDraftBasis: true,
            });
            showError(caught);
          }
        }
        if (!mountedRef.current) return next;
        const unchanged = sameDraft(draftRef.current, captured);
        applyView(next, {
          syncDraft: unchanged,
          syncProposal: !proposalDirtyRef.current,
          adoptDirtyDraftBasis: true,
        });
        draftDirtyRef.current = !unchanged;
        setDraftSaveState(unchanged ? "saved" : "unsaved");
        return next;
      } catch (caught) {
        if (mountedRef.current) {
          if (caught instanceof ProductError && caught.code === "quest_draft_stale") {
            try {
              const latest = normalizeQuestCreationView(
                await fetchQuestCreation(basis.initialization_id),
              );
              const additions = acceptedMaterialAdditions(
                expectedBasis.value,
                latest.quest_draft.value,
              );
              if (additions !== null) {
                applyView(latest, {
                  syncDraft: true,
                  syncProposal: !proposalDirtyRef.current,
                  adoptDirtyDraftBasis: true,
                  acceptedMaterialAdditions: additions,
                  notify: false,
                });
                setDraftSaveState("unsaved");
                return latest;
              }
            } catch {
              // The original stale write remains the authoritative error.
            }
            writeConflictRef.current = "draft";
            setWriteConflict("draft");
          }
          setDraftSaveState("error");
          showError(caught);
        }
        return null;
      }
    })();
    draftSavePromiseRef.current = task;
    const result = await task;
    if (draftSavePromiseRef.current === task) draftSavePromiseRef.current = null;
    if (
      result &&
      mountedRef.current &&
      !sameDraft(creationRef.current?.quest_draft.value ?? captured, draftRef.current)
    ) {
      return persist();
    }
    return result;
  }, [applyView, hasEditedDraft, showError]);

  const persistProposal = useCallback(async function persistQuestion(): Promise<QuestCreationView | null> {
    if (Boolean(writeConflictRef.current)) return null;
    if (proposalSavePromiseRef.current) {
      await proposalSavePromiseRef.current;
      if (Boolean(writeConflictRef.current)) return null;
      return proposalDirtyRef.current ? persistQuestion() : creationRef.current;
    }
    const savedBasis = await persistDraft();
    const content = proposalRef.current;
    if (!savedBasis || !savedBasis.proposal || !content || !proposalDirtyRef.current) {
      return savedBasis;
    }
    const captured = { ...content };
    const expectedBasis = proposalWriteBasisRef.current ?? proposalBasisOf(savedBasis);
    setProposalSaveState("saving");
    setError(null);
    const task = (async () => {
      try {
        const next = await saveQuestionProposal(savedBasis, captured, false, expectedBasis);
        if (!mountedRef.current) return next;
        const unchanged = sameQuestion(proposalRef.current, captured);
        applyView(next, {
          syncProposal: unchanged,
          adoptDirtyProposalBasis: true,
        });
        if (unchanged) {
          proposalDirtyRef.current = false;
          proposalWriteBasisRef.current = null;
          setProposalDirty(false);
          setProposalSaveState("saved");
        } else {
          setProposalSaveState("unsaved");
        }
        void settleCurrentPreview(next);
        return next;
      } catch (caught) {
        if (mountedRef.current) {
          if (caught instanceof ProductError && caught.code === "question_proposal_stale") {
            writeConflictRef.current = "proposal";
            setWriteConflict("proposal");
          }
          setProposalSaveState("error");
          showError(caught);
        }
        return null;
      }
    })();
    proposalSavePromiseRef.current = task;
    const result = await task;
    if (proposalSavePromiseRef.current === task) proposalSavePromiseRef.current = null;
    if (result && mountedRef.current && proposalDirtyRef.current) return persistQuestion();
    return result;
  }, [applyView, persistDraft, settleCurrentPreview, showError]);

  useEffect(() => {
    const basis = creation;
    if (!basis || creationIsLocked(basis) || sameDraft(basis.quest_draft.value, draft)) return;
    if (draftTimerRef.current !== null) clearTimeout(draftTimerRef.current);
    draftTimerRef.current = setTimeout(() => {
      draftTimerRef.current = null;
      void persistDraft();
    }, 450);
    return () => {
      if (draftTimerRef.current !== null) clearTimeout(draftTimerRef.current);
      draftTimerRef.current = null;
    };
  }, [creation, draft, persistDraft]);

  useEffect(() => {
    if (!proposalDirty || !creation?.proposal || creationIsLocked(creation)) return;
    if (proposalTimerRef.current !== null) clearTimeout(proposalTimerRef.current);
    proposalTimerRef.current = setTimeout(() => {
      proposalTimerRef.current = null;
      void persistProposal();
    }, 500);
    return () => {
      if (proposalTimerRef.current !== null) clearTimeout(proposalTimerRef.current);
      proposalTimerRef.current = null;
    };
  }, [creation, persistProposal, proposal, proposalDirty]);

  const updateDraft = (next: QuestDraft) => {
    if (!draftDirtyRef.current && creationRef.current) {
      draftWriteBasisRef.current = draftBasisOf(creationRef.current);
    }
    draftRef.current = next;
    draftDirtyRef.current = true;
    setDraft(next);
    setHasEditedDraft(true);
    setDraftSaveState("unsaved");
    if (writeConflictRef.current === null) setError(null);
  };

  const updateBoundary = (value: string) => {
    if (
      !value.includes(BOUNDARY_SEGMENT_SEPARATOR) &&
      value.length > BOUNDARY_RAW_INPUT_MAX_LENGTH
    ) {
      errorFocusRef.current = "alert";
      setError({
        code: "quest_boundary_too_long",
        message: messageFor("quest_boundary_too_long"),
      });
      return;
    }
    updateDraft(boundaryDraft(draftRef.current, value));
  };

  const toggleAcceptedMaterial = (asset: ResearchAssetItem) => {
    const bindings = draftRef.current.literature.accepted_material_bindings;
    const selected = bindings.some(
      (binding) => binding.version_ref === asset.version_ref,
    );
    if (!selected) {
      const remaining = remainingQuestMaterialSlots(
        draftRef.current,
        materialUploadsRef.current,
      );
      if (remaining < 1) {
        errorFocusRef.current = "alert";
        setError(questMaterialLimitFailure(remaining));
        return;
      }
    }
    const nextBindings = selected
      ? bindings.filter((binding) => binding.version_ref !== asset.version_ref)
      : [...bindings, researchAssetBinding(asset)];
    updateDraft({
      ...draftRef.current,
      literature: {
        ...draftRef.current.literature,
        accepted_material_bindings: nextBindings,
      },
    });
  };

  const updateMaterialUpload = (
    key: string,
    patch: Partial<Omit<MaterialUpload, "key">>,
  ) => {
    if (!mountedRef.current) return;
    replaceMaterialUploads((currentUploads) => currentUploads.map((upload) =>
      upload.key === key ? { ...upload, ...patch } : upload
    ));
  };

  const settleMaterialUpload = async (
    key: string,
    initializationId: string,
    initial: Awaited<ReturnType<typeof fetchAssetIntake>>,
  ) => {
    const context = questMaterialIntakeContext(initializationId);
    const jobRef = initial.job_ref;
    let terminalStatus: "accepted" | "failed" | null = null;
    try {
      const result = await awaitQuestMaterialIntake(initial);
      terminalStatus = result.status === "accepted" || result.status === "failed"
        ? result.status
        : null;
      if (result.status === "failed") {
        throw new ProductError(result.failure?.code ?? "asset_intake_failed");
      }
      if (result.status !== "accepted" || !result.asset) {
        throw new ProductError("asset_intake_status_invalid");
      }

      const asset = result.asset;
      updateMaterialUpload(key, { status: "binding" });
      if (mountedRef.current) {
        setMaterialAssets((currentAssets) => {
          const byRef = new Map(currentAssets.map((item) => [item.memory_ref, item]));
          byRef.set(asset.memory_ref, asset);
          return [...byRef.values()];
        });
        setMaterialTotal((currentTotal) => Math.max(currentTotal, materialAssets.length + 1));
        // Save the user's current text before the independent material CAS.
        // Later edits never wait for this optional bind: whichever CAS loses
        // retries from the newer basis, and Draft stale recovery only merges
        // an exact accepted-material addition into the user's local fields.
        await persistDraft();
      }
      await enqueueQuestMaterialCommit(initializationId, async () => {
        let bound = normalizeQuestCreationView(
          await bindAcceptedQuestMaterial(initializationId, asset),
        );
        if (mountedRef.current) {
          let merged = false;
          for (let attempt = 0; attempt < 5; attempt += 1) {
            const acceptedBinding = bound.quest_draft.value.literature
              .accepted_material_bindings.find(
                (candidate) => candidate.version_ref === asset.version_ref,
              );
            if (acceptedBinding) {
              const applied = applyView(bound, {
                syncDraft: true,
                syncProposal: !proposalDirtyRef.current,
                adoptDirtyDraftBasis: true,
                acceptedMaterialAdditions: [acceptedBinding],
                notify: false,
              });
              merged = (applied || creationRef.current?.initialization_id === initializationId) &&
                draftRef.current.literature.accepted_material_bindings.some(
                  (candidate) => candidate.version_ref === asset.version_ref,
                );
              if (merged) break;
            }
            bound = normalizeQuestCreationView(
              await fetchQuestCreation(initializationId),
            );
          }
          if (!merged) throw new ProductError("quest_material_binding_pending");
        }
        // Only a verified Owner bind that has also reached the current local
        // merge may retire its durable intake pointer. Keeping this inside the
        // per-initialization commit queue prevents a later AssetVersion from
        // acknowledging ahead of an earlier held CAS response.
        acknowledgeScopedAssetIntake(context, result.job_ref);
      });
      if (mountedRef.current) {
        replaceMaterialUploads((currentUploads) => currentUploads.filter(
          (upload) => upload.key !== key,
        ));
        onChanged();
      }
    } catch (caught) {
      const code = caught instanceof ProductError ? caught.code : "unknown_error";
      // Keep an accepted intake pointer until the exact AssetVersion has been
      // durably bound. A reload can then resume the independent Quest write.
      // Failed intakes and already-closed Quests have no remaining bind work.
      if (terminalStatus === "failed" || code === "quest_material_binding_closed") {
        acknowledgeScopedAssetIntake(context, jobRef);
      }
      updateMaterialUpload(key, {
        status: "failed",
        reason: materialUploadFailureCopy(code),
      });
    }
  };

  const runMaterialUpload = async (
    key: string,
    file: File,
    selectionMode: "files" | "folder",
    initializationId: string,
  ) => {
    try {
      if (file.size > QUEST_MATERIAL_MAX_BYTES) {
        throw new ProductError("asset_content_too_large");
      }
      const contentBase64 = arrayBufferToBase64(await file.arrayBuffer());
      updateMaterialUpload(key, { status: "accepting" });
      const result = await submitScopedAssetIntake(
        questMaterialIntakeContext(initializationId),
        {
          source_kind: "file",
          custody_mode: "managed",
          display_name: file.name,
          media_type: file.type || "application/octet-stream",
          content_base64: contentBase64,
          asynchronous: true,
          provenance: {
            submitted_via: "quest_creation",
            initialization_id: initializationId,
            selection_mode: selectionMode,
            relative_path: (file as File & { webkitRelativePath?: string })
              .webkitRelativePath || null,
          },
        },
      );
      // The selection queue ends at the durable POST ACK. This settlement
      // owns its own failure handling and may poll independently, so one slow
      // accepted-material job cannot prevent the next file from being read
      // and assigned its own durable operation/job pointer.
      void settleMaterialUpload(key, initializationId, result);
    } catch (caught) {
      const code = caught instanceof ProductError ? caught.code : "unknown_error";
      updateMaterialUpload(key, {
        status: "failed",
        reason: materialUploadFailureCopy(code),
      });
    }
  };

  const selectMaterialFiles = (
    selected: FileList | null,
    selectionMode: "files" | "folder",
  ) => {
    const initializationId = creationRef.current?.initialization_id;
    if (!selected || !selected.length || !initializationId) return;
    const files = [...selected];
    const remaining = remainingQuestMaterialSlots(
      draftRef.current,
      materialUploadsRef.current,
    );
    if (files.length > remaining) {
      errorFocusRef.current = "alert";
      setError(questMaterialLimitFailure(remaining));
      return;
    }
    const uploads = files.map((file, index) => ({
      key: `${Date.now()}-${index}-${crypto.randomUUID()}`,
      name: file.name,
      status: "reading" as const,
    }));
    replaceMaterialUploads((currentUploads) => [...currentUploads, ...uploads]);
    files.forEach((file, index) => {
      const upload = uploads[index];
      materialUploadQueueRef.current = materialUploadQueueRef.current.then(
        () => runMaterialUpload(upload.key, file, selectionMode, initializationId),
      );
    });
  };

  useEffect(() => {
    const initializationId = creation?.initialization_id;
    if (!initializationId) return;
    let active = true;
    void hydrateScopedAssetIntakeRecovery(
      questMaterialIntakeContext(initializationId),
    ).then((pointers) => {
      if (!active || !mountedRef.current) return;
      for (const pointer of pointers) {
        if (recoveredMaterialJobsRef.current.has(pointer.job_ref)) continue;
        recoveredMaterialJobsRef.current.add(pointer.job_ref);
        const key = `recovery:${pointer.job_ref}`;
        replaceMaterialUploads((currentUploads) => [
          ...currentUploads.filter((upload) => upload.key !== key),
          {
            key,
            name: pointer.display_name,
            status: "accepting",
          },
        ]);
        materialUploadQueueRef.current = materialUploadQueueRef.current.then(
          async () => {
            try {
              const result = await fetchAssetIntake(pointer.job_ref);
              // A synthetic recovery ref may replay the exact POST here, so
              // the queue waits through that durable ACK. Polling and binding
              // then settle independently and cannot serialize later jobs.
              void settleMaterialUpload(key, initializationId, result);
            } catch (caught) {
              const code = caught instanceof ProductError
                ? caught.code
                : "unknown_error";
              updateMaterialUpload(key, {
                status: "failed",
                reason: materialUploadFailureCopy(code),
              });
            }
          },
        );
      }
    }).catch((caught) => {
      if (active && mountedRef.current) showError(caught);
    });
    return () => {
      active = false;
    };
    // The exact initialization identity owns this recovery scan. Any queued
    // settlement continues after unmount; a scan still hydrating is resumed by
    // the next instance instead of retaining a stale component closure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [creation?.initialization_id]);

  const updateProposal = (key: keyof QuestionContent, value: string) => {
    const currentProposal = proposalRef.current;
    if (!currentProposal) return;
    const next = { ...currentProposal, [key]: value };
    if (!proposalDirtyRef.current && creationRef.current) {
      proposalWriteBasisRef.current = proposalBasisOf(creationRef.current);
    }
    proposalRef.current = next;
    proposalDirtyRef.current = true;
    setProposal(next);
    setProposalDirty(true);
    setProposalSaveState("unsaved");
    if (writeConflictRef.current === null) setError(null);
  };

  const reloadLatestDurableVersion = async () => {
    const basis = creationRef.current;
    if (!basis) return;
    const token = beginOperation("reviewing");
    try {
      const received = await fetchQuestCreation(basis.initialization_id);
      if (!operationIsCurrent("reviewing", token)) return;
      const accepted = applyView(received, { notify: false });
      if (!accepted) throw new ProductError("quest_reload_stale");
      const next = creationRef.current;
      if (!next) throw new ProductError("quest_reload_stale");

      if (draftTimerRef.current !== null) clearTimeout(draftTimerRef.current);
      if (proposalTimerRef.current !== null) clearTimeout(proposalTimerRef.current);
      draftTimerRef.current = null;
      proposalTimerRef.current = null;

      const nextDraft = cloneDraft(next.quest_draft.value);
      draftRef.current = nextDraft;
      draftDirtyRef.current = false;
      draftWriteBasisRef.current = null;
      setDraft(nextDraft);
      setHasEditedDraft(false);
      setDraftSaveState("restored");

      const nextProposal = next.proposal ? { ...next.proposal.content } : null;
      proposalRef.current = nextProposal;
      proposalDirtyRef.current = false;
      proposalWriteBasisRef.current = null;
      writeConflictRef.current = null;
      setWriteConflict(null);
      setProposal(nextProposal);
      setProposalDirty(false);
      setProposalSaveState(nextProposal ? "restored" : "opening");
      setError(null);
      onChanged();
    } catch (caught) {
      if (operationIsCurrent("reviewing", token)) showError(caught);
    } finally {
      finishOperation("reviewing", token);
    }
  };

  const detectCompute = async (selectedDeviceUuids: string[] = []) => {
    const token = beginOperation("compute");
    setError(null);
    try {
      const basis = await persistDraft();
      if (!basis || !operationIsCurrent("compute", token)) return;
      const next = await observeHostCompute(basis, selectedDeviceUuids);
      if (!operationIsCurrent("compute", token)) return;
      applyView(next, {
        syncDraft: true,
        syncProposal: !proposalDirtyRef.current,
        adoptDirtyDraftBasis: true,
      });
      setDraftSaveState(draftDirtyRef.current ? "unsaved" : "saved");
    } catch (caught) {
      if (operationIsCurrent("compute", token)) showError(caught);
    } finally {
      finishOperation("compute", token);
    }
  };

  const detectAcquisition = async () => {
    const token = beginOperation("acquisition");
    setError(null);
    try {
      const basis = await persistDraft();
      if (!basis || !operationIsCurrent("acquisition", token)) return;
      const next = await prepareAcquisitionSession(basis);
      if (!operationIsCurrent("acquisition", token)) return;
      applyView(next, {
        syncDraft: true,
        syncProposal: !proposalDirtyRef.current,
        adoptDirtyDraftBasis: true,
      });
      const session = next.acquisition_session;
      if (session && session.status !== "ready") {
        showError(new ProductError(
          session.reason?.code ?? "acquisition_session_not_ready",
        ));
      }
    } catch (caught) {
      if (operationIsCurrent("acquisition", token)) showError(caught);
    } finally {
      finishOperation("acquisition", token);
    }
  };

  const generateProposal = async (returnFocus?: HTMLButtonElement) => {
    const token = beginOperation("generating");
    let restoreTriggerFocus = false;
    setError(null);
    try {
      let basis = await persistProposal();
      if (!basis || !operationIsCurrent("generating", token)) return;
      if (!draftIsComplete(draftRef.current)) {
        showError(new ProductError("quest_basis_incomplete"));
        requestAnimationFrame(() => firstRequiredRef.current?.focus());
        return;
      }
      if (basis.resource_envelope?.status !== "current") {
        showError(new ProductError("resource_envelope_required"));
        requestAnimationFrame(() => computeButtonRef.current?.focus());
        return;
      }
      if (
        draftRef.current.route === "deepfetch" &&
        (
          !basis.acquisition_session ||
          basis.acquisition_session.freshness !== "current" ||
          basis.acquisition_session.status !== "ready"
        )
      ) {
        basis = await prepareAcquisitionSession(basis);
        if (!operationIsCurrent("generating", token)) return;
        applyView(basis, {
          syncDraft: true,
          syncProposal: !proposalDirtyRef.current,
        });
        if (basis.acquisition_session?.status !== "ready") {
          showError(new ProductError(
            basis.acquisition_session?.reason?.code ??
              "acquisition_session_not_ready",
          ));
          requestAnimationFrame(() => acquisitionButtonRef.current?.focus());
          return;
        }
      }
      const queued = await generateQuestionProposal(basis);
      if (!operationIsCurrent("generating", token)) return;
      applyView(queued);
      const next = await pollCreation(
        queued.initialization_id,
        proposalGenerationSettled,
        draftRef.current.route === "deepfetch" ? 1_810_000 : 190_000,
      );
      if (!operationIsCurrent("generating", token)) return;
      applyView(next, { syncProposal: Boolean(next.proposal) });
      if (
        next.proposal_generation?.status === "capability_unavailable" ||
        next.proposal_generation?.status === "failed" ||
        next.deepfetch?.status === "failed" ||
        next.deepfetch?.status === "cancelled"
      ) {
        showError(new ProductError(
          next.deepfetch?.failure?.code ??
          next.proposal_generation?.failure?.code ??
          "proposal_drafter_unavailable",
        ));
      } else {
        restoreTriggerFocus = true;
      }
    } catch (caught) {
      if (operationIsCurrent("generating", token)) showError(caught);
    } finally {
      if (operationIsCurrent("generating", token)) {
        if (restoreTriggerFocus) {
          requestAnimationFrame(() => {
            if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
          });
        }
      }
      finishOperation("generating", token);
    }
  };

  const submitIntent = async () => {
    const message = intentText.trim();
    if (!message) return;
    const token = beginOperation("intent");
    setError(null);
    try {
      const basis = await persistDraft();
      if (!basis || !operationIsCurrent("intent", token)) return;
      const queued = await sendIntentMessage(basis, message);
      if (!operationIsCurrent("intent", token)) return;
      setIntentText("");
      applyView(queued);
      const turnRef = queued.intent_session?.turns.at(-1)?.ref;
      const next = await pollCreation(
        queued.initialization_id,
        (view) => {
          const turn = view.intent_session?.turns.find((item) => item.ref === turnRef);
          if (!turn || ["queued", "running"].includes(turn.assistant_status)) return false;
          return view.proposal?.status === "current"
            ? confirmationIsCurrent(view)
            : true;
        },
      );
      if (!operationIsCurrent("intent", token)) return;
      applyView(next);
    } catch (caught) {
      if (operationIsCurrent("intent", token)) showError(caught);
    } finally {
      finishOperation("intent", token);
    }
  };

  const submitConfirmation = async () => {
    const token = beginOperation("confirming");
    setError(null);
    try {
      const savedDraft = await persistDraft();
      if (!savedDraft || !operationIsCurrent("confirming", token)) return;
      const exact = await persistProposal();
      if (!operationIsCurrent("confirming", token)) return;
      if (!exact || !confirmationIsCurrent(exact)) {
        showError(new ProductError("confirmation_preview_required"));
        return;
      }
      const dispatching = await confirmQuest(exact);
      if (!operationIsCurrent("confirming", token)) return;
      applyView(dispatching);
    } catch (caught) {
      if (operationIsCurrent("confirming", token)) showError(caught);
    } finally {
      finishOperation("confirming", token);
    }
  };

  const finalizeClose = useCallback(() => {
    if (closeAnimationTimerRef.current !== null) return;
    const target = returnFocusRef.current;
    setDialogOpen(false);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    closeAnimationTimerRef.current = setTimeout(() => {
      closeAnimationTimerRef.current = null;
      if (dialogRef.current?.open) dialogRef.current.close();
      onClose();
      requestAnimationFrame(() => target?.focus());
    }, reducedMotion ? 0 : 240);
  }, [onClose]);

  const closePreservingDraft = async () => {
    if (closingRef.current) return;
    if (!creationRef.current) {
      finalizeClose();
      return;
    }
    if (writeConflictRef.current) {
      // Closing a conflicted recovery view is a local navigation action. The
      // stale browser draft must neither be persisted nor trap the user in the
      // dialog; reopening hydrates the latest durable version.
      finalizeClose();
      return;
    }
    closingRef.current = true;
    const token = beginOperation("closing");
    setError(null);
    const saved = await persistProposal();
    if (!saved) {
      closingRef.current = false;
      finishOperation("closing", token);
      return;
    }
    finalizeClose();
  };

  const explicitlyReviewProposal = async () => {
    if (!proposalRef.current) return;
    const token = beginOperation("reviewing");
    setError(null);
    try {
      const basis = await persistProposal();
      if (!basis?.proposal || !operationIsCurrent("reviewing", token)) return;
      const content = proposalRef.current;
      if (!content) return;
      let next = await saveQuestionProposal(basis, content, true);
      if (!operationIsCurrent("reviewing", token)) return;
      applyView(next, { syncProposal: true });
      setProposalSaveState("saved");
      next = await settleCurrentPreview(next);
    } catch (caught) {
      if (operationIsCurrent("reviewing", token)) showError(caught);
    } finally {
      finishOperation("reviewing", token);
    }
  };

  const explicitCancel = async () => {
    const basis = creationRef.current;
    if (!basis || creationIsLocked(basis)) return;
    const token = beginOperation("cancelling");
    invalidateConcurrentOperations("cancelling");
    setError(null);
    try {
      const next = await cancelQuest(basis);
      if (!operationIsCurrent("cancelling", token)) return;
      applyView(next);
      finalizeClose();
    } catch (caught) {
      if (operationIsCurrent("cancelling", token)) showError(caught);
    } finally {
      finishOperation("cancelling", token);
    }
  };

  const terminal = creation ? creationIsLocked(creation) : false;
  const proposalGenerationActive = Boolean(
    creation?.status === "proposal_generating" ||
    creation?.proposal_generation && ["queued", "running"].includes(
      creation.proposal_generation.status,
    ),
  );
  const acquisitionSession = creation?.acquisition_session ?? null;
  const deepfetch = creation?.deepfetch ?? null;
  const durableIntentActive = Boolean(
    creation?.intent_session?.turns.some((turn) =>
      ["queued", "running"].includes(turn.assistant_status)),
  );
  const terminalMutationActive = inFlight.confirming || inFlight.cancelling || inFlight.closing;
  const draftInteractionLocked = terminal || inFlight.acquisition || inFlight.reviewing || terminalMutationActive;
  const sessionInteractionLocked = terminal || durableIntentActive || inFlight.intent || inFlight.acquisition ||
    inFlight.reviewing || terminalMutationActive;
  const proposalInteractionLocked = terminal || proposalGenerationActive || inFlight.generating || inFlight.acquisition ||
    inFlight.reviewing || terminalMutationActive;
  const computeActionLocked = terminal || inFlight.compute || inFlight.acquisition || inFlight.generating ||
    proposalGenerationActive || inFlight.reviewing || terminalMutationActive;
  const acquisitionActionLocked = terminal || inFlight.acquisition || inFlight.generating ||
    proposalGenerationActive || inFlight.reviewing || terminalMutationActive;
  const proposalActionLocked = terminal || inFlight.compute || inFlight.acquisition || inFlight.generating ||
    proposalGenerationActive || inFlight.reviewing || terminalMutationActive;
  const anyOperationActive = Object.values(inFlight).some(Boolean);
  const draftComplete = draftIsComplete(draft);
  const proposalComplete = questionIsComplete(proposal);
  const selectedDevices = new Set(
    creation?.resource_envelope?.selected_device_uuids ?? [],
  );
  const resourceEnvelopeCurrent = creation?.resource_envelope?.status === "current";
  const canConfirm = Boolean(
    creation &&
    draftComplete &&
    resourceEnvelopeCurrent &&
    proposalComplete &&
    !proposalDirty &&
    draftSaveState !== "saving" &&
    draftSaveState !== "unsaved" &&
    proposalSaveState !== "saving" &&
    proposalSaveState !== "unsaved" &&
    confirmationIsCurrent(creation) &&
    !anyOperationActive &&
    !proposalGenerationActive &&
    !terminal,
  );
  const statusLabel = creation ? creationStatusLabel(creation.status) : "正在建立";
  const footerNote = footerCopy(creation, draft, proposal, proposalDirty, inFlight);
  const visibleError = error ?? (writeConflict ? {
    code: writeConflict === "draft" ? "quest_draft_stale" : "question_proposal_stale",
    message: messageFor(
      writeConflict === "draft" ? "quest_draft_stale" : "question_proposal_stale",
    ),
  } : null);

  const trapDialogFocus = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), " +
      "select:not([disabled]), summary, a[href], [tabindex]:not([tabindex='-1'])",
    )).filter((element) => {
      const style = window.getComputedStyle(element);
      return element.getClientRects().length > 0 && style.visibility !== "hidden";
    });
    if (!focusable.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable.at(-1)!;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="quest-dialog"
      data-open={dialogOpen}
      data-prototype-source="d7e2c9b7"
      aria-labelledby="quest-creation-title"
      onKeyDown={trapDialogFocus}
      onCancel={(event) => {
        event.preventDefault();
        void closePreservingDraft();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) void closePreservingDraft();
      }}
    >
      <section className="quest-window">
        <header className="quest-modal-header">
          <span className="quest-modal-symbol" aria-hidden="true">QST</span>
          <div className="quest-modal-title">
            <small>CREATE QUEST · FIRST QUESTION BUNDLE</small>
            <h2 id="quest-creation-title">创建 Quest，并决定第一个研究问题</h2>
            <p>同一个 durable 草案 · 同一次确认 · 首问题由生产 Drafter 起草</p>
          </div>
          <div className="quest-modal-meta">
            <span className="quest-modal-chip">首次创建专用</span>
            <span className="quest-modal-chip current">{statusLabel}</span>
          </div>
          <button
            ref={closeButtonRef}
            className="quest-close"
            type="button"
            aria-label="关闭创建 Quest 窗口"
            onClick={() => void closePreservingDraft()}
            autoFocus
          >
            ×
          </button>
        </header>

        <div className="quest-modal-body">
          <div className="quest-modal-layout">
            <main className="quest-continuous-form" data-testid="quest-continuous-form">
              <div className="quest-intro">
                <span aria-hidden="true">◎</span>
                <div>
                  <small>ONE FORM · ONE CONFIRMATION</small>
                  <h3>先设定研究边界，<br />再共同确定第一问。</h3>
                  <p>Quest 配置、真实资源范围与六字段 QuestionProposal 始终留在这一条连续旅程中。</p>
                </div>
              </div>

              <section className="quest-journey-section" data-journey-section="basis" aria-label="Quest 背景、目标与边界">
                <div className="quest-section-heading">
                  <b>研究简报</b>
                  <small>BACKGROUND · GOAL · BOUNDARY</small>
                </div>
                <div className="quest-basis-card">
                  <label className="quest-field">
                    <span>背景（可选）</span>
                    <textarea
                      aria-label="背景（可选）"
                      rows={4}
                      maxLength={12_000}
                      value={draft.background_and_initial_direction}
                      disabled={!creation || draftInteractionLocked}
                      placeholder="已有事实、研究动机，以及希望第一问优先关注的方向。"
                      onChange={(event) => updateDraft({
                        ...draft,
                        background_and_initial_direction: event.target.value,
                      })}
                      onBlur={() => void persistDraft()}
                    />
                  </label>
                  <label className="quest-field">
                    <span>目标 <em>必填</em></span>
                    <textarea
                      ref={firstRequiredRef}
                      aria-label="目标"
                      rows={4}
                      maxLength={4000}
                      value={draft.goal}
                      disabled={!creation || draftInteractionLocked}
                      placeholder="这项研究最终要回答什么、交付什么？"
                      onChange={(event) => updateDraft({ ...draft, goal: event.target.value })}
                      onBlur={() => void persistDraft()}
                    />
                  </label>
                  <label className="quest-field">
                    <span>边界 <em>必填</em></span>
                    <textarea
                      aria-label="边界"
                      rows={4}
                      maxLength={boundaryInputMaxLength(draft)}
                      value={boundaryText(draft)}
                      disabled={!creation || draftInteractionLocked}
                      placeholder="怎样算完成？哪些范围、约束或排除项必须遵守？"
                      onChange={(event) => updateBoundary(event.target.value)}
                      onBlur={() => void persistDraft()}
                    />
                  </label>
                </div>
              </section>

              <section className="quest-journey-section" data-journey-section="configuration" aria-labelledby="quest-config-title">
                <div className="quest-section-heading">
                  <b id="quest-config-title">关键配置</b>
                  <small>Provider availability → Quest Resource Envelope</small>
                </div>
                <div className="quest-config-card">
                  <div className="quest-config-grid">
                    <label className="quest-field">
                      <span>时间预算</span>
                      <select
                        aria-label="时间预算"
                        value={draft.time_budget}
                        disabled={!creation || draftInteractionLocked}
                        onChange={(event) => updateDraft({
                          ...draft,
                          time_budget: event.target.value as QuestDraft["time_budget"],
                        })}
                        onBlur={() => void persistDraft()}
                      >
                        <option value="7d">7 天</option>
                        <option value="30d">30 天</option>
                        <option value="90d">90 天</option>
                        <option value="open">不设硬截止</option>
                      </select>
                    </label>
                    <section className="quest-compute" aria-labelledby="quest-compute-title">
                      <div className="quest-compute-head">
                        <div>
                          <b id="quest-compute-title">本机计算卡</b>
                          <small>真实 host snapshot</small>
                        </div>
                        <button
                          ref={computeButtonRef}
                          type="button"
                          disabled={!creation || computeActionLocked || draftSaveState === "saving"}
                          onClick={() => void detectCompute()}
                        >
                          {inFlight.compute ? "正在检测…" : creation?.compute ? "重新检测" : "检测本机计算卡"}
                        </button>
                      </div>
                      <div
                        className={`quest-compute-status ${creation?.compute?.status ?? ""}`}
                        role="status"
                      >
                        {computeCopy(creation)}
                      </div>
                      {creation?.compute?.status === "ready" && creation.compute.devices.length ? (
                        <div className="quest-device-list" aria-label="实际检测到的计算卡">
                          {creation.compute.devices.map((device) => {
                            const selected = selectedDevices.has(device.uuid);
                            return (
                              <button
                                className="quest-device"
                                type="button"
                                key={device.uuid}
                                aria-pressed={selected}
                                disabled={draftInteractionLocked || computeActionLocked}
                                onClick={() => {
                                  const next = new Set(selectedDevices);
                                  if (selected) next.delete(device.uuid);
                                  else next.add(device.uuid);
                                  void detectCompute([...next]);
                                }}
                              >
                                <b>{device.name}</b>
                                <span>{formatMemory(device.memory_total_mib)} · {device.uuid}</span>
                              </button>
                            );
                          })}
                        </div>
                      ) : null}
                      <small className="quest-compute-boundary">
                        <b>Quest Resource Envelope：</b>{" "}
                        {creation?.resource_envelope
                          ? resourceEnvelopeCopy(creation)
                          : "检测并选择设备后形成；尚未创建任何 Run ResourceBinding。"}
                      </small>
                    </section>
                  </div>
                </div>
              </section>

              <section className="quest-journey-section" data-journey-section="literature" aria-labelledby="quest-literature-title">
                <div className="quest-section-heading">
                  <b id="quest-literature-title">文献范围与图书馆准备</b>
                  <small>三种范围保留在原位</small>
                </div>
                <div className="quest-literature-card">
                  <label className="quest-field">
                    <span>文献搜索范围</span>
                    <select
                      aria-label="文献搜索范围"
                      value={draft.literature.mode}
                      disabled={!creation || draftInteractionLocked}
                      onChange={(event) => updateDraft({
                        ...draft,
                        literature: {
                          ...draft.literature,
                          mode: event.target.value as QuestDraft["literature"]["mode"],
                        },
                      })}
                      onBlur={() => void persistDraft()}
                    >
                      <option value="oa_then_institution">全面搜索（包括图书馆）</option>
                      <option value="oa_only">只搜索开放获取资源</option>
                      <option value="provided_only" disabled={materialTotal === 0}>
                        只使用我提供的材料
                      </option>
                    </select>
                  </label>
                  <div className="quest-library-prep">
                    <i aria-hidden="true">↗</i>
                    <div>
                      <b>全面搜索前准备 Google Chrome</b>
                      <small>检测真实 provider、浏览器控制与获准资源；凭据和 provider manifest 始终留在私有会话。</small>
                    </div>
                    <button
                      ref={acquisitionButtonRef}
                      className="quest-library-test"
                      type="button"
                      disabled={
                        !creation ||
                        acquisitionActionLocked ||
                        draftSaveState === "saving"
                      }
                      onClick={() => void detectAcquisition()}
                    >
                      {inFlight.acquisition
                        ? "正在检测…"
                        : acquisitionSession?.status === "waiting_user"
                          ? "重新检测登录"
                          : acquisitionSession
                            ? "重新检测"
                            : "检测搜索环境"}
                    </button>
                    <span
                      className={`quest-unavailable-tag ${acquisitionSession?.status ?? "not-checked"}`}
                      data-testid="acquisition-session-status"
                      aria-live="polite"
                    >
                      {inFlight.acquisition
                        ? "probing · current"
                        : acquisitionSessionCopy(acquisitionSession, draft)}
                    </span>
                  </div>
                  <label className="quest-field">
                    <span>图书馆／数据库入口链接 <em>可选</em></span>
                    <input
                      type="url"
                      inputMode="url"
                      autoComplete="url"
                      value={draft.literature.library_entry_url}
                      disabled={!creation || draftInteractionLocked}
                      placeholder="例如 https://library.example.edu/resource"
                      onChange={(event) => updateDraft({
                        ...draft,
                        literature: { ...draft.literature, library_entry_url: event.target.value },
                      })}
                      onBlur={() => void persistDraft()}
                    />
                  </label>
                  <small className="quest-library-boundary">
                    不收集密码、Cookie、token、OTP 或浏览器 profile；入口 URL 只是 Quest 配置。
                  </small>
                  {draft.literature.mode === "provided_only" ? (
                    <small className="quest-library-boundary">
                      {draft.literature.accepted_material_bindings.length
                        ? `已绑定 ${draft.literature.accepted_material_bindings.length} 个 RM Accepted AssetVersion。`
                        : "请在下方选择至少一个已由 Research Memory 接纳的精确版本。"}
                    </small>
                  ) : null}
                </div>
              </section>

              <section className="quest-journey-section" data-journey-section="materials" aria-labelledby="quest-materials-title">
                <div className="quest-section-heading">
                  <b id="quest-materials-title">研究材料 <em>可选</em></b>
                  <small>选取后后台接纳，成功即同步到当前草案</small>
                </div>
                <div className="quest-material-card">
                  <div className="quest-material-actions">
                    <button
                      className="quest-material-button"
                      type="button"
                      disabled={!creation || draftInteractionLocked}
                      onClick={() => materialFolderInputRef.current?.click()}
                    >
                      选择文件夹
                    </button>
                    <button
                      className="quest-material-button"
                      type="button"
                      disabled={!creation || draftInteractionLocked}
                      onClick={() => materialFilesInputRef.current?.click()}
                    >
                      选择文件
                    </button>
                    <span>无需先去材料工作台；名称、类型与保管方式会自动处理。</span>
                  </div>
                  <input
                    ref={materialFolderInputRef}
                    className="quest-visually-hidden"
                    type="file"
                    multiple
                    tabIndex={-1}
                    aria-label="上传研究材料文件夹"
                    onChange={(event) => {
                      selectMaterialFiles(event.target.files, "folder");
                      event.target.value = "";
                    }}
                  />
                  <input
                    ref={materialFilesInputRef}
                    className="quest-visually-hidden"
                    type="file"
                    multiple
                    tabIndex={-1}
                    aria-label="上传研究材料文件"
                    onChange={(event) => {
                      selectMaterialFiles(event.target.files, "files");
                      event.target.value = "";
                    }}
                  />
                  {materialUploads.length || draft.literature.accepted_material_bindings.length ? (
                    <div className="quest-material-statuses" aria-live="polite">
                      {draft.literature.accepted_material_bindings.map((binding) => {
                        const versionRef = String(binding.version_ref ?? "");
                        const asset = materialAssets.find((item) => item.version_ref === versionRef);
                        return (
                          <div className="quest-material-status accepted" key={versionRef}>
                            <span aria-hidden="true">✓</span>
                            <b>{asset?.display_name ?? versionRef}</b>
                            <small>已加入本次 Quest</small>
                            {asset && !draftInteractionLocked ? (
                              <button type="button" onClick={() => toggleAcceptedMaterial(asset)}>
                                移除
                              </button>
                            ) : null}
                          </div>
                        );
                      })}
                      {materialUploads.map((upload) => (
                        <div className={`quest-material-status ${upload.status}`} key={upload.key}>
                          <span aria-hidden="true">{upload.status === "failed" ? "!" : "…"}</span>
                          <b>{upload.name}</b>
                          <small>{upload.reason ?? materialUploadStatusCopy(upload.status)}</small>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="quest-material-empty">尚未选择材料。这里为空不会阻塞 Direct 创建。</p>
                  )}
                  <details
                    className="quest-material-library"
                    open={materialPickerOpen}
                    onToggle={(event) => setMaterialPickerOpen(event.currentTarget.open)}
                  >
                    <summary>从已有材料中选择</summary>
                    <div className="quest-material-picker" role="group" aria-label="选择已接纳 Research Asset">
                      {materialAssets.length ? materialAssets.map((asset) => {
                        const selected = draft.literature.accepted_material_bindings.some(
                          (binding) => binding.version_ref === asset.version_ref,
                        );
                        return (
                          <button
                            key={asset.version_ref}
                            type="button"
                            aria-pressed={selected}
                            disabled={draftInteractionLocked}
                            onClick={() => toggleAcceptedMaterial(asset)}
                          >
                            <span>{selected ? "✓" : "+"}</span>
                            <b>{asset.display_name}</b>
                            <small>{asset.version_ref} · integrity {asset.integrity} · availability {asset.availability}</small>
                          </button>
                        );
                      }) : (
                        <small>还没有已接纳材料；可直接使用上方按钮添加。</small>
                      )}
                      {materialAssets.length < materialTotal ? (
                        <button
                          type="button"
                          disabled={materialsLoading}
                          onClick={() => void loadMoreMaterials()}
                        >
                          <span>+</span>
                          <b>{materialsLoading ? "正在读取…" : "加载更多已接纳版本"}</b>
                          <small>已显示 {materialAssets.length} / {materialTotal}</small>
                        </button>
                      ) : null}
                    </div>
                  </details>
                  <p className="quest-material-footnote">
                    上传失败只影响该材料；若选择“只使用我提供的材料”，至少一个材料正式加入后才能继续。
                  </p>
                </div>
              </section>

              <section className="quest-journey-section" data-journey-section="route" aria-labelledby="quest-route-title">
                <div className="quest-section-heading">
                  <b id="quest-route-title">生成第一问之前，要不要补充检索？</b>
                  <small>direct 与 DeepFetch 均在当前 initialization 原位收敛</small>
                </div>
                <div className="quest-route-card">
                  <div className="quest-route-options">
                    <button
                      className="quest-route-choice"
                      type="button"
                      aria-label="先运行 DeepFetch"
                      aria-pressed={draft.route === "deepfetch"}
                      disabled={!creation || draftInteractionLocked}
                      onClick={() => updateDraft({ ...draft, route: "deepfetch" })}
                    >
                      <b>先运行 DeepFetch</b>
                      <small>绑定当前 DraftRevision 做真实 Web Research，再原位起草六字段。</small>
                    </button>
                    <button
                      className="quest-route-choice"
                      type="button"
                      aria-label="直接根据目标生成"
                      aria-pressed={draft.route === "direct"}
                      disabled={!creation || draftInteractionLocked}
                      onClick={() => updateDraft({ ...draft, route: "direct" })}
                    >
                      <b>直接根据目标生成</b>
                      <small>不等待检索，使用当前精确 Quest basis 起草。</small>
                    </button>
                  </div>
                  <p className="quest-route-note">
                    <b>当前边界：</b>
                    {draft.route === "deepfetch"
                      ? " DeepFetch 只形成 RM 接纳的精确 LiteratureSnapshot；不会创建 Quest、Question 或 Cycle，也不会替你完成最终确认。"
                      : " direct 使用当前精确 Quest basis 起草；没有检索运行，也不会伪造 LiteratureSnapshot。"}
                  </p>
                  {draft.route === "deepfetch" ? (
                    <div
                      className={`quest-deepfetch-runway ${deepfetch?.status ?? "not-started"}`}
                      data-testid="deepfetch-runway"
                      data-status={deepfetch?.status ?? "not-started"}
                      role="status"
                      aria-live="polite"
                    >
                      <div className="quest-deepfetch-head">
                        <div>
                          <small>FIRST-QUESTION PREPARATION</small>
                          <b>DeepFetch Web Research</b>
                        </div>
                        <span>{deepfetch ? deepfetchStatusLabel(deepfetch) : "等待启动"}</span>
                      </div>
                      {deepfetch ? (
                        <>
                          <div className="quest-deepfetch-progress">
                            <progress
                              aria-label="DeepFetch 真实进度"
                              value={deepfetch.progress.completed}
                              max={deepfetch.progress.total}
                            />
                            <code>
                              {deepfetch.progress.completed}/{deepfetch.progress.total}
                            </code>
                          </div>
                          <div className="quest-deepfetch-stages" aria-label="DeepFetch 活动阶段">
                            {["授权", "排队", "Web Research", "RM 接纳", "Proposal"].map((label, index) => (
                              <span
                                key={label}
                                className={index < deepfetch.progress.completed ? "done" : undefined}
                              >
                                {label}
                              </span>
                            ))}
                          </div>
                          <div className="quest-deepfetch-facts">
                            <span>
                              <small>ACTIVITY</small>
                              <b>{deepfetchActivityLabel(deepfetch.activity)}</b>
                            </span>
                            <span>
                              <small>FRESHNESS</small>
                              <b>{deepfetch.freshness}</b>
                            </span>
                            <span>
                              <small>ATTEMPT</small>
                              <b>{deepfetch.run?.attempt_generation ?? 0}</b>
                            </span>
                          </div>
                          {deepfetch.literature_snapshot ? (
                            <div className="quest-deepfetch-snapshot">
                              <div>
                                <b>
                                  LiteratureSnapshot · {deepfetch.literature_snapshot.completion}
                                </b>
                                <small>
                                  {deepfetch.literature_snapshot.paper_count} papers · {deepfetch.literature_snapshot.fulltext_count} fulltexts · RM accepted
                                </small>
                              </div>
                              {deepfetch.literature_snapshot.limitations.length ? (
                                <ul>
                                  {deepfetch.literature_snapshot.limitations.map((limitation) => (
                                    <li key={limitation}>{limitation}</li>
                                  ))}
                                </ul>
                              ) : null}
                            </div>
                          ) : null}
                          {deepfetch.failure ? (
                            <p className="quest-deepfetch-failure">
                              {deepfetchFailureCopy(deepfetch)}
                            </p>
                          ) : null}
                        </>
                      ) : (
                        <p className="quest-deepfetch-awaiting">
                          保存当前 DraftRevision 和 Resource Envelope 后，点击下方唯一生成按钮启动；进度只读取 durable Projection。
                        </p>
                      )}
                    </div>
                  ) : null}
                </div>
              </section>

              <section className="quest-journey-section" data-journey-section="question-proposal" aria-labelledby="quest-proposal-title">
                <div className="quest-section-heading">
                  <b id="quest-proposal-title">系统起草的第一个研究问题</b>
                  <small>六字段持续可编辑</small>
                </div>
                <div className="quest-proposal">
                  <div className="quest-question-sourcebar">
                    <b>可编辑 QuestionProposal</b>
                    <span>系统已填入六字段；确认前都可修改</span>
                    <small>{proposalSourceCopy(creation)}</small>
                  </div>

                  {creation?.status === "proposal_generating" ? (
                    <div className="quest-proposal-state generating" role="status">
                      <span>
                        {draft.route === "deepfetch"
                          ? "正在准备精确 LiteratureSnapshot 并原位生成六字段；右侧 Drafting Session 始终保留。"
                          : "正在依据精确 DraftRevision 生成六字段；Quest 配置和右侧 Session 仍可使用。"}
                      </span>
                      <code>proposal_generating</code>
                      <small>{creation.deepfetch?.activity ?? creation.proposal_generation?.status ?? "queued"}</small>
                    </div>
                  ) : null}
                  {creation?.status === "proposal_stale" || creation?.proposal?.status === "stale" ? (
                    <div className="quest-proposal-state stale" role="status">
                      <span>Quest basis 已变化；旧 Proposal 保留，但不能直接确认。</span>
                      <code>proposal_stale</code>
                    </div>
                  ) : null}
                  {creation?.proposal?.status === "incomplete" ? (
                    <div className="quest-proposal-state incomplete" role="status">
                      <span>Proposal 当前 basis 仍有效，但四个必填字段尚未完整；补齐后才能确认。</span>
                      <code>proposal_incomplete</code>
                    </div>
                  ) : null}

                  {!proposal ? (
                    <div className="quest-proposal-state">
                      <span>
                        {creation?.proposal_generation?.status === "capability_unavailable"
                          ? "生产 Proposal Drafter 当前不可用；不会用字符串模板伪造首问题。"
                          : "填写必填基底并绑定 Quest Resource Envelope 后，可以原位生成完整六字段。"}
                      </span>
                      <button
                        ref={proposalActionRef}
                        className="quest-proposal-action"
                        type="button"
                        disabled={!creation || !draftComplete || !resourceEnvelopeCurrent || proposalActionLocked}
                        onClick={(event) => void generateProposal(event.currentTarget)}
                      >
                        {inFlight.generating || proposalGenerationActive ? "正在生成…" : "生成第一个问题"}
                      </button>
                    </div>
                  ) : (
                    <>
                      <div className="quest-proposal-grid quest-question-fields" aria-label="首问题 Formal Question 六字段草案">
                        {proposalFields.map((field) => (
                          <label
                            className={[
                              "quest-field",
                              "quest-question-field",
                              "seed-field",
                              field.full ? "full" : "",
                              field.required ? "core" : "",
                            ].filter(Boolean).join(" ")}
                            key={field.key}
                          >
                            <span className="seed-field-head">
                              <code>{field.code}</code>
                              <em className={field.required ? "required" : undefined}>
                                {field.required ? "必填" : "可选"}
                              </em>
                            </span>
                            {field.rows === 1 ? (
                              <input
                                aria-label={field.label}
                                value={proposal[field.key]}
                                disabled={proposalInteractionLocked}
                                placeholder={field.placeholder}
                                onChange={(event) => updateProposal(field.key, event.target.value)}
                                onBlur={() => void persistProposal()}
                              />
                            ) : (
                              <textarea
                                aria-label={field.label}
                                rows={field.rows}
                                value={proposal[field.key]}
                                disabled={proposalInteractionLocked}
                                placeholder={field.placeholder}
                                onChange={(event) => updateProposal(field.key, event.target.value)}
                                onBlur={() => void persistProposal()}
                              />
                            )}
                          </label>
                        ))}
                      </div>
                      <div className="quest-proposal-actions">
                        <span className="quest-proposal-validation">
                          {proposalComplete
                            ? proposalSaveState === "saving"
                              ? "六字段完整 · 正在自动保存"
                              : proposalSaveState === "saved"
                                ? "六字段完整 · 首问题已自动保存"
                                : "六字段完整 · 确认前仍可修改"
                            : "仍需补齐四个必填字段"}
                        </span>
                        <div className="quest-proposal-action-group">
                          {creation?.proposal?.status === "stale" ? (
                            <button
                              className="quest-inline-button"
                              type="button"
                              disabled={proposalActionLocked || !draftComplete || !resourceEnvelopeCurrent}
                              onClick={() => void explicitlyReviewProposal()}
                            >
                              {inFlight.reviewing ? "正在复核…" : "按当前依据明确复核"}
                            </button>
                          ) : null}
                          <button
                            ref={proposalActionRef}
                            className="quest-proposal-action"
                            type="button"
                            aria-label="重新生成第一个问题"
                            disabled={proposalActionLocked || !draftComplete || !resourceEnvelopeCurrent}
                            onClick={(event) => void generateProposal(event.currentTarget)}
                          >
                            重新生成并覆盖
                          </button>
                        </div>
                      </div>
                    </>
                  )}

                  <ImpactSummary creation={creation} />
                  <TechnicalDetails creation={creation} />
                </div>
              </section>

              {creation?.status === "completed" ? (
                <p className="quest-impact-summary" role="status">
                  <b>Quest 与第一个问题已就绪。</b>
                  已接纳事实保持分层；可以关闭窗口并进入当前 Quest。
                </p>
              ) : null}
              {creation && ["partial", "recovering"].includes(creation.status) ? (
                <p className="quest-impact-summary waiting" role="status">
                  <b>{creation.status === "recovering" ? "正在从首个缺失 receipt 恢复" : "Quest 已创建，第一个问题尚未完成"}</b>
                  已接纳事实不会回滚；完整分层状态可在上方技术详情中查看。
                </p>
              ) : null}
              {creation?.status === "unavailable" ? (
                <p className="quest-impact-summary waiting" role="alert">
                  <b>已完成事实的持久对象暂时无法验证。</b>
                  系统不会继续宣称 completed；将按原 receipt 绑定自动对账并恢复。
                </p>
              ) : null}
              {visibleError ? (
                <div className="quest-error" role="alert" tabIndex={-1} ref={errorRef}>
                  <b>{visibleError.message}</b><br />
                  <code>{visibleError.code}</code>
                  {writeConflict ? (
                    <button
                      ref={conflictRecoveryRef}
                      className="quest-inline-button"
                      type="button"
                      disabled={inFlight.reviewing}
                      onClick={() => void reloadLatestDurableVersion()}
                    >
                      {inFlight.reviewing
                        ? "正在载入最新 durable 版本…"
                        : "载入最新 durable 版本并放弃本地未保存修改"}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </main>

            <IntentDraftingSession
              creation={creation}
              value={intentText}
              disabled={!creation || sessionInteractionLocked}
              onChange={setIntentText}
              onSubmit={() => void submitIntent()}
            />
          </div>
        </div>

        <footer className="quest-footer" data-testid="quest-confirmation-footer">
          <div className="quest-footer-note" aria-live="polite">
            <span>{footerNote}</span>
            <strong className="quest-autosave-state">
              {draftSaveLabel(draftSaveState, hasEditedDraft)}
            </strong>
          </div>
          <button
            type="button"
            disabled={!creation || terminal || inFlight.confirming || inFlight.cancelling || inFlight.closing}
            onClick={() => void explicitCancel()}
          >
            取消
          </button>
          <button
            className="confirm"
            type="button"
            disabled={!canConfirm}
            onClick={() => void submitConfirmation()}
          >
            确认创建 Quest 与第一个问题
          </button>
        </footer>
      </section>
    </dialog>
  );
}

function IntentDraftingSession({
  creation,
  value,
  disabled,
  onChange,
  onSubmit,
}: {
  creation: QuestCreationView | null;
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
}) {
  const transcriptRef = useRef<HTMLDivElement>(null);
  const turns = creation?.intent_session?.turns ?? [];

  useEffect(() => {
    if (transcriptRef.current) transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
  }, [turns]);

  return (
    <aside
      className="quest-intent-session"
      aria-label="讨论 Quest 与第一问"
      data-testid="quest-intent-session"
    >
      <header className="quest-intent-header">
        <span className="quest-intent-orb" aria-hidden="true" />
        <div>
          <small>INTENT DRAFTING SESSION</small>
          <b>讨论 Quest 与第一问</b>
          <span>{creation?.intent_session?.ref ?? "正在建立 pre-Quest session"}</span>
        </div>
      </header>
      <div className="quest-intent-transcript" ref={transcriptRef} aria-live="polite">
        {!turns.length ? (
          <p className="quest-intent-empty">
            我会在这里解释配置、讨论是否需要 DeepFetch，并帮助缩小第一问；左侧字段仍只由你编辑。
          </p>
        ) : turns.map((turn) => (
          <div key={turn.ref}>
            <article className="quest-intent-message user">
              <small>你 · draft r{turn.basis_revision}</small>
              {turn.user_content}
            </article>
            <article className={`quest-intent-message${turn.basis_hash !== creation?.quest_draft.hash ? " stale" : ""}`}>
              <small>Drafting Session · {turn.assistant_status}</small>
              {turn.assistant_content ?? (
                turn.reason
                  ? `capability_unavailable · ${turn.reason.code}`
                  : "正在准备回复…"
              )}
            </article>
          </div>
        ))}
      </div>
      <div className="quest-session-compose">
        <label htmlFor="quest-intent-message">继续讨论</label>
        <div className="quest-session-compose-row">
          <textarea
            id="quest-intent-message"
            aria-label="在 Quest Drafting Session 中发消息"
            rows={3}
            value={value}
            disabled={disabled}
            placeholder="询问配置含义、讨论是否需要 DeepFetch，或要求解释第一问……"
            onChange={(event) => onChange(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && value.trim()) {
                event.preventDefault();
                onSubmit();
              }
            }}
          />
          <button
            className="quest-session-send"
            type="button"
            aria-label="发送消息"
            disabled={disabled || !value.trim()}
            onClick={onSubmit}
          >
            ↑
          </button>
        </div>
      </div>
      <div className="quest-intent-status">
        <span>{creation?.intent_session?.status ?? "opening"}</span>
        <span>{creation ? `绑定 draft r${creation.quest_draft.revision}` : "等待 initialization"}</span>
      </div>
      <div className="quest-intent-boundary">
        <b>聊天不会直接创建 Quest。</b> 回复不会改写左侧字段、确认草案或签发 Owner receipt。
      </div>
    </aside>
  );
}

function ImpactSummary({ creation }: { creation: QuestCreationView | null }) {
  const preview = creation?.confirmation_preview;
  const current = Boolean(
    preview?.status === "current" && creation?.proposal?.status === "current",
  );
  if (!creation?.proposal) return null;
  return (
    <section className={`quest-impact-summary${current ? "" : " waiting"}`} aria-label="确认影响摘要">
      <b>{current ? "当前 Impact Preview 已绑定，可以确认" : "Impact Preview 正在等待 current basis"}</b>
      <div className="quest-impact-columns">
        <div>
          <small>将发生</small>
          <p>{preview?.will_happen?.join("；") || "自动 Preview current 后显示。"}</p>
        </div>
        <div>
          <small>不会发生</small>
          <p>{preview?.will_not_happen?.join("；") || "不会把一次确认冒充全部 Owner 已接纳。"}</p>
        </div>
      </div>
    </section>
  );
}

function TechnicalDetails({ creation }: { creation: QuestCreationView | null }) {
  if (!creation) return null;
  return (
    <details className="quest-technical-details">
      <summary>查看 Preview、Owner 与 receipt 技术详情</summary>
      <div className="quest-technical-grid">
        <span>initialization · {creation.initialization_id}</span>
        <span>draft · r{creation.quest_draft.revision} · {creation.quest_draft.hash}</span>
        <span>
          resource envelope · {creation.resource_envelope?.ref ?? "not_bound"} · {creation.resource_envelope?.hash ?? "no_hash"}
        </span>
        <span>
          proposal · {creation.proposal?.ref ?? "not_generated"} · {creation.proposal?.hash ?? "no_hash"}
        </span>
        <span>
          preview · {creation.confirmation_preview?.ref ?? "not_current"} · {creation.confirmation_preview?.hash ?? "no_hash"} · feed r{creation.confirmation_preview?.feed_revision ?? "none"}
        </span>
        {creation.recovery ? (
          <article className="quest-technical-record recovery">
            <b>recovery · {creation.recovery.state}</b>
            <span>first missing · {creation.recovery.first_missing_step ?? "none"}</span>
            <span>attempt · {creation.recovery.attempt_count}</span>
            <span>reason · {creation.recovery.reason?.code ?? "none"}</span>
            <span>next retry · {creation.recovery.next_retry_at ?? "none"}</span>
          </article>
        ) : null}
        {creation.confirmation_preview?.target_assertions.map((assertion) => (
          <article
            className="quest-technical-record assertion"
            key={`${assertion.owner}:${assertion.operation}`}
          >
            <b>{assertion.owner} · {assertion.operation}</b>
            <span>target hash · {assertion.target_hash}</span>
            <span>may change · {technicalList(assertion.may_change)}</span>
            <span>will not change · {technicalList(assertion.will_not_change)}</span>
            <span>preconditions · {technicalList(assertion.preconditions)}</span>
            <span>risks · {technicalList(assertion.risks)}</span>
            <span>stale if · {technicalList(assertion.stale_if)}</span>
            <span>bindings · {JSON.stringify(assertion.bindings)}</span>
          </article>
        ))}
        {Object.entries(creation.receipts).map(([name, receipt]) => (
          <ReceiptTechnicalDetails key={name} name={name} receipt={receipt} />
        ))}
      </div>
    </details>
  );
}

function ReceiptTechnicalDetails({
  name,
  receipt,
}: {
  name: string;
  receipt: QuestReceiptState;
}) {
  return (
    <article className={`quest-technical-record receipt ${receipt.status}`}>
      <b>{name} · {receipt.status}</b>
      {receipt.status === "accepted" ? (
        <>
          <span>issuer · {receipt.issuer}</span>
          <span>kind · {receipt.kind}</span>
          {"role_refs" in receipt ? (
            <>
              <span>role refs · {technicalList(receipt.role_refs)}</span>
              <span>receipts · {receipt.receipts.length}</span>
            </>
          ) : (
            <>
              <span>receipt · {receipt.receipt_ref}</span>
              <span>subject · {receipt.subject_ref}</span>
              <span>payload hash · {receipt.payload_hash}</span>
            </>
          )}
        </>
      ) : receipt.reason ? (
        <>
          <span>reason · {receipt.reason.code}</span>
          {"upstream_step" in receipt.reason && receipt.reason.upstream_step ? (
            <span>upstream step · {receipt.reason.upstream_step}</span>
          ) : null}
        </>
      ) : null}
    </article>
  );
}

function technicalList(values: string[]): string {
  return values.length ? values.join(" · ") : "none";
}

function cloneDraft(value: QuestDraft): QuestDraft {
  return {
    ...value,
    literature: {
      ...value.literature,
      accepted_material_bindings: value.literature.accepted_material_bindings.map(
        (binding) => ({ ...binding }),
      ),
    },
  };
}

function normalizeQuestCreationView(
  view: QuestCreationView,
): NormalizedQuestCreationView {
  const value = view.quest_draft.value;
  if (isQuestDraftV2(value)) {
    return view as NormalizedQuestCreationView;
  }
  return {
    ...view,
    route: "direct",
    quest_draft: {
      ...view.quest_draft,
      value: legacyDraftForWorkbench(value),
    },
  };
}

function isQuestDraftV2(
  value: QuestCreationView["quest_draft"]["value"],
): value is QuestDraft {
  return "literature" in value && typeof value.literature === "object" &&
    value.literature !== null && Array.isArray(value.literature.accepted_material_bindings);
}

function legacyDraftForWorkbench(value: LegacyQuestDraft): QuestDraft {
  const literatureMode: Record<LegacyQuestDraft["literature_scope"], QuestDraft["literature"]["mode"]> = {
    comprehensive: "oa_then_institution",
    open_access: "oa_only",
    provided_materials: "provided_only",
  };
  const background = [
    value.key_configuration
      ? `Legacy key configuration：${value.key_configuration}`
      : "",
    value.initial_question_direction
      ? `Legacy initial question direction：${value.initial_question_direction}`
      : "",
  ].filter(Boolean).join("\n\n");
  return {
    goal: value.goal,
    completion_criteria: value.completion_criteria,
    time_budget: "open",
    route: "direct",
    resource_envelope_ref: null,
    resource_envelope_hash: null,
    literature: {
      mode: literatureMode[value.literature_scope] ?? "oa_only",
      library_entry_url: "",
      scope_exclusions: "",
      accepted_material_bindings: [],
    },
    background_and_initial_direction: background,
  };
}

function mergeServerManagedDraft(
  local: QuestDraft,
  server: QuestDraft,
  acceptedMaterialAdditions: QuestAcceptedMaterialBinding[] = [],
): QuestDraft {
  const acceptedByVersion = new Map(
    local.literature.accepted_material_bindings.map(
      (binding) => [binding.version_ref, binding],
    ),
  );
  for (const binding of acceptedMaterialAdditions) {
    acceptedByVersion.set(binding.version_ref, binding);
  }
  return {
    ...cloneDraft(local),
    resource_envelope_ref: server.resource_envelope_ref,
    resource_envelope_hash: server.resource_envelope_hash,
    literature: {
      ...local.literature,
      accepted_material_bindings: [...acceptedByVersion.values()],
    },
  };
}

function acceptedMaterialAdditions(
  previous: QuestDraft,
  next: QuestDraft,
): QuestAcceptedMaterialBinding[] | null {
  const previousWithoutBindings = cloneDraft(previous);
  const nextWithoutBindings = cloneDraft(next);
  previousWithoutBindings.literature.accepted_material_bindings = [];
  nextWithoutBindings.literature.accepted_material_bindings = [];
  if (!sameDraft(previousWithoutBindings, nextWithoutBindings)) return null;
  const previousByVersion = new Map(
    previous.literature.accepted_material_bindings.map(
      (binding) => [binding.version_ref, binding],
    ),
  );
  const nextByVersion = new Map(
    next.literature.accepted_material_bindings.map(
      (binding) => [binding.version_ref, binding],
    ),
  );
  for (const [versionRef, prior] of previousByVersion) {
    const current = nextByVersion.get(versionRef);
    if (!current || JSON.stringify(prior) !== JSON.stringify(current)) return null;
  }
  for (const binding of next.literature.accepted_material_bindings) {
    const prior = previousByVersion.get(binding.version_ref);
    if (prior && JSON.stringify(prior) !== JSON.stringify(binding)) return null;
  }
  return next.literature.accepted_material_bindings.filter(
    (binding) => !previousByVersion.has(binding.version_ref),
  );
}

function draftBasisOf(creation: QuestCreationView): DraftWriteBasis {
  return {
    revision: creation.quest_draft.revision,
    hash: creation.quest_draft.hash,
    value: cloneDraft(normalizeQuestCreationView(creation).quest_draft.value),
  };
}

function proposalBasisOf(creation: QuestCreationView): ProposalWriteBasis {
  return {
    draftRevision: creation.quest_draft.revision,
    draftHash: creation.quest_draft.hash,
    proposalRef: creation.proposal?.ref ?? null,
    proposalHash: creation.proposal?.hash ?? null,
  };
}

function sameDraft(left: QuestDraft, right: QuestDraft): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function literatureAcquisitionConfigChanged(
  left: QuestDraft,
  right: QuestDraft,
): boolean {
  return left.literature.mode !== right.literature.mode ||
    left.literature.library_entry_url.trim() !==
      right.literature.library_entry_url.trim();
}

function acquisitionSessionCopy(
  session: QuestCreationView["acquisition_session"],
  draft: QuestDraft,
): string {
  if (!session) return "not_checked";
  const freshness = session.mode === draft.literature.mode
    ? session.freshness
    : "stale";
  if (session.status === "ready") {
    const browser = draft.literature.mode === "oa_then_institution"
      ? `browser ${session.browser_context}`
      : "browser not_required";
    return `ready · ${freshness} · ${browser}`;
  }
  const reason = session.reason?.code ?? "no_reason";
  return `${session.status} · ${freshness} · ${reason}`;
}

function sameQuestion(left: QuestionContent | null, right: QuestionContent | null): boolean {
  if (left === null || right === null) return left === right;
  return JSON.stringify(left) === JSON.stringify(right);
}

function meaningful(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return Boolean(normalized) && !["unknown", "not_applicable", "not applicable", "n/a", "na"].includes(normalized);
}

function draftIsComplete(value: QuestDraft): boolean {
  return meaningful(value.goal) && meaningful(value.completion_criteria) &&
    ["direct", "deepfetch"].includes(value.route);
}

function questionIsComplete(value: QuestionContent | null): boolean {
  return Boolean(value && [
    value.title,
    value.unknown_statement,
    value.answer_shape,
    value.applicability_scope,
  ].every(meaningful));
}

function confirmationIsCurrent(creation: QuestCreationView): boolean {
  const proposal = creation.proposal;
  const preview = creation.confirmation_preview;
  return Boolean(
    proposal &&
    proposal.status === "current" &&
    proposal.basis_revision === creation.quest_draft.revision &&
    proposal.basis_hash === creation.quest_draft.hash &&
    preview &&
    preview.status === "current" &&
    preview.basis_revision === creation.quest_draft.revision &&
    preview.basis_hash === creation.quest_draft.hash &&
    preview.proposal_ref === proposal.ref &&
    preview.proposal_hash === proposal.hash,
  );
}

function viewIsOlder(
  current: QuestCreationView,
  next: QuestCreationView,
): boolean {
  const currentTerminal = current.status === "cancelled";
  const nextTerminal = next.status === "cancelled";
  if (nextTerminal && !currentTerminal) return false;
  if (currentTerminal) return next.status !== current.status;
  if (next.quest_draft.revision < current.quest_draft.revision) return true;
  if (
    next.quest_draft.revision === current.quest_draft.revision &&
    current.compute &&
    (!next.compute || next.compute.observed_at < current.compute.observed_at)
  ) return true;
  if (
    next.quest_draft.revision === current.quest_draft.revision &&
    current.proposal &&
    (!next.proposal || next.proposal.revision < current.proposal.revision)
  ) return true;
  const currentPreview = current.confirmation_preview;
  const nextPreview = next.confirmation_preview;
  if (
    currentPreview &&
    nextPreview?.ref === currentPreview.ref &&
    previewStatusRank(nextPreview.status) < previewStatusRank(currentPreview.status)
  ) return true;

  const currentTurns = current.intent_session?.turns ?? [];
  const nextTurns = next.intent_session?.turns ?? [];
  if (nextTurns.length < currentTurns.length) return true;
  const currentLastTurn = currentTurns.at(-1);
  const nextLastTurn = nextTurns.at(-1);
  if (
    currentLastTurn &&
    nextLastTurn &&
    nextLastTurn.ordinal < currentLastTurn.ordinal
  ) return true;
  if (
    currentLastTurn &&
    nextLastTurn?.ref === currentLastTurn.ref &&
    intentStatusRank(nextLastTurn.assistant_status) <
      intentStatusRank(currentLastTurn.assistant_status)
  ) return true;

  const currentExecution = executionStatusRank(current.status);
  const nextExecution = executionStatusRank(next.status);
  if (currentExecution !== null && (nextExecution === null || nextExecution < currentExecution)) {
    return true;
  }

  const currentGeneration = current.proposal_generation;
  const nextGeneration = next.proposal_generation;
  if (
    currentGeneration &&
    ["queued", "running"].includes(currentGeneration.status) &&
    next.quest_draft.revision === current.quest_draft.revision &&
    nextGeneration?.ref !== currentGeneration.ref
  ) return true;
  if (
    currentGeneration &&
    nextGeneration?.ref === currentGeneration.ref &&
    proposalGenerationStatusRank(nextGeneration.status) <
      proposalGenerationStatusRank(currentGeneration.status)
  ) return true;
  const currentDeepFetch = current.deepfetch;
  const nextDeepFetch = next.deepfetch;
  if (
    currentDeepFetch &&
    ["queued", "running", "accepting"].includes(currentDeepFetch.status) &&
    next.quest_draft.revision === current.quest_draft.revision &&
    nextDeepFetch?.request_ref !== currentDeepFetch.request_ref
  ) return true;
  if (
    currentDeepFetch &&
    nextDeepFetch?.request_ref === currentDeepFetch.request_ref &&
    ["queued", "running", "accepting"].includes(currentDeepFetch.status) &&
    deepfetchStatusRank(nextDeepFetch.status) < deepfetchStatusRank(currentDeepFetch.status)
  ) return true;
  return false;
}

function executionStatusRank(status: QuestCreationView["status"]): number | null {
  const ranks: Partial<Record<QuestCreationView["status"], number>> = {
    dispatching: 0,
    partial: 1,
    recovering: 1,
    unavailable: 2,
    completed: 2,
    cancelled: 2,
  };
  return ranks[status] ?? null;
}

function previewStatusRank(
  status: NonNullable<QuestCreationView["confirmation_preview"]>["status"],
): number {
  return ["current", "stale", "consumed"].indexOf(status);
}

function intentStatusRank(status: IntentSessionTurn["assistant_status"]): number {
  return ["queued", "running", "completed", "unavailable", "failed"].indexOf(status);
}

function proposalGenerationStatusRank(
  status: NonNullable<QuestCreationView["proposal_generation"]>["status"],
): number {
  return ["queued", "running", "succeeded", "capability_unavailable", "failed"].indexOf(status);
}

function proposalGenerationSettled(creation: QuestCreationView): boolean {
  const generation = creation.proposal_generation;
  const deepfetch = creation.deepfetch;
  if (deepfetch && ["failed", "cancelled"].includes(deepfetch.status)) return true;
  if (
    deepfetch &&
    deepfetch.freshness === "stale" &&
    deepfetch.status === "succeeded"
  ) return true;
  if (!generation) return false;
  if (["capability_unavailable", "failed"].includes(generation.status)) return true;
  if (generation.status !== "succeeded") return false;
  // A basis edit is intentionally allowed while the provider works. Its late
  // result remains visible as stale history, so it is terminal even though no
  // current Preview can (or should) be produced for it.
  if (creation.proposal?.status === "stale") return true;
  return confirmationIsCurrent(creation);
}

function deepfetchStatusRank(
  status: NonNullable<QuestCreationView["deepfetch"]>["status"],
): number {
  return ["queued", "running", "accepting", "succeeded", "failed", "cancelled"].indexOf(status);
}

function deepfetchRequiresNewInitialization(
  deepfetch: NonNullable<QuestCreationView["deepfetch"]>,
): boolean {
  return deepfetch.failure?.code === "deepfetch_run_identity_conflict" ||
    deepfetch.run?.provider_operation_retry_permitted === false;
}

function deepfetchFailureCopy(
  deepfetch: NonNullable<QuestCreationView["deepfetch"]>,
): string {
  const failure = deepfetch.failure;
  if (!failure) return "";
  if (deepfetchRequiresNewInitialization(deepfetch)) {
    const hardFailure = failure.code === "deepfetch_run_identity_conflict"
      ? messageFor(failure.code)
      : `DeepFetch 运行未完成（${failure.code}）；当前 durable 草案与已接纳事实不会回滚。`;
    return `${hardFailure} 需要新建 initialization 或交由人工检查；当前记录不会自动重试。`;
  }
  return `${messageFor(failure.code)} ` +
    "重新点击“生成第一个问题”会沿同一 correlation 创建新 Attempt。";
}

function deepfetchStatusLabel(
  deepfetch: NonNullable<QuestCreationView["deepfetch"]>,
): string {
  return {
    queued: "已授权 · 排队中",
    running: "真实检索中",
    accepting: "RM 接纳中",
    succeeded: "快照已接纳",
    failed: deepfetchRequiresNewInitialization(deepfetch) ? "需人工检查" : "需要重试",
    cancelled: "已取消",
  }[deepfetch.status];
}

function deepfetchActivityLabel(
  activity: NonNullable<QuestCreationView["deepfetch"]>["activity"],
): string {
  return {
    waiting_for_runtime: "等待 Agent Runtime",
    web_research: "Web Search / Fetch",
    accepting_assets: "接纳 summary / papers / fulltexts",
    proposal_drafting: "使用精确快照起草",
    complete: "首问题已原位展开",
    needs_retry: "运行未完成",
    cancelled: "运行已取消",
  }[activity];
}

function creationIsLocked(creation: QuestCreationView): boolean {
  return ["dispatching", "partial", "recovering", "unavailable", "completed", "cancelled"].includes(creation.status);
}

function executionIsPending(creation: QuestCreationView): boolean {
  return ["dispatching", "partial", "recovering", "unavailable"].includes(creation.status);
}

function creationStatusLabel(status: QuestCreationView["status"]): string {
  return {
    draft: "草案中",
    proposal_generating: "首问题生成中",
    proposal_ready: "待最终确认",
    proposal_stale: "依据已变化",
    dispatching: "正在创建",
    partial: "部分完成",
    recovering: "正在恢复",
    unavailable: "持久对象不可验证",
    completed: "已完成",
    cancelled: "已取消",
  }[status];
}

function draftSaveLabel(state: SaveState, edited: boolean): string {
  if (state === "opening") return "正在建立 durable 草案";
  if (state === "unsaved") return "修改等待自动保存";
  if (state === "saving") return "正在自动保存…";
  if (state === "saved") return edited ? "草案已自动保存" : "durable 草案已同步";
  if (state === "error") return "自动保存失败 · 草案仍留在当前窗口";
  return "已恢复 durable 草案";
}

function footerCopy(
  creation: QuestCreationView | null,
  draft: QuestDraft,
  proposal: QuestionContent | null,
  proposalDirty: boolean,
  inFlight: InFlightOperations,
): string {
  if (!creation) return "正在建立首次创建专用 initialization";
  if (inFlight.confirming || creation.status === "dispatching") return "正在创建；重复确认已禁用，可安全关闭后恢复";
  if (creation.status === "recovering") return "正在从首个缺失 receipt 恢复；已接纳事实不会回滚";
  if (creation.status === "partial") return "Quest 已创建，第一个问题尚未完成；系统会继续对账";
  if (creation.status === "unavailable") return "已完成事实暂不可验证；系统会按原 receipt 自动恢复";
  if (creation.status === "completed") return "Quest 与第一个问题已就绪";
  if (creation.status === "cancelled") return "已明确取消；未分配 provisional QuestRef 或 QuestionRef";
  if (!meaningful(draft.goal)) return "请先填写 Quest 目标";
  if (!meaningful(draft.completion_criteria)) return "还需填写边界";
  if (!creation.resource_envelope) return "请检测本机计算卡，并为 Quest 形成 Resource Envelope";
  if (!proposal) {
    if (creation.deepfetch?.status === "failed") {
      return deepfetchRequiresNewInitialization(creation.deepfetch)
        ? "DeepFetch 未完成；需要新建 initialization 或交由人工检查，当前记录不会自动重试"
        : "DeepFetch 未完成；可沿同一 correlation 重试";
    }
    if (creation.status === "proposal_generating") {
      return creation.route === "deepfetch"
        ? "DeepFetch 正在原位准备 LiteratureSnapshot 与首问题"
        : "首问题正在原位生成";
    }
    return "请生成第一个问题";
  }
  if (proposalDirty) return "首问题修改正在自动保存";
  if (!questionIsComplete(proposal)) return "首问题仍需补齐四个必填字段";
  if (!confirmationIsCurrent(creation)) return "依据已变化；等待 current Impact Preview";
  return "Quest 与第一问已就绪 · 可以用唯一按钮一起确认";
}

function proposalSourceCopy(creation: QuestCreationView | null): string {
  if (!creation) return "等待 initialization";
  if (creation.status === "proposal_generating") {
    return creation.route === "deepfetch"
      ? `DeepFetch · ${creation.deepfetch?.activity ?? "queued"}`
      : "production drafter · generating";
  }
  if (creation.proposal?.status === "stale") return `旧 Proposal · basis r${creation.proposal.basis_revision}`;
  if (creation.proposal?.status === "incomplete") {
    return `${creation.route} · incomplete draft r${creation.proposal.basis_revision}`;
  }
  if (creation.proposal) {
    return `${creation.route} · current draft r${creation.proposal.basis_revision}`;
  }
  if (creation.proposal_generation?.status === "capability_unavailable") return "capability_unavailable";
  return "等待生成";
}

function computeCopy(creation: QuestCreationView | null): string {
  const compute = creation?.compute;
  if (!compute) return "尚未检测 · 不会显示 synthetic A100";
  if (compute.status === "unavailable") {
    return `capability_unavailable · ${compute.reason?.code ?? "host_compute_unavailable"}`;
  }
  if (!compute.devices.length) return "真实 Provider 已返回；当前没有可选择的计算卡";
  return `检测到 ${compute.devices.length} 张计算卡 · 请选择允许本 Quest 使用的设备`;
}

function resourceEnvelopeCopy(creation: QuestCreationView): string {
  const envelope = creation.resource_envelope;
  if (!envelope) return "尚未形成；不会冒充 Run ResourceBinding。";
  const devicesByUuid = new Map(
    (creation.compute?.devices ?? []).map((device) => [device.uuid, device.name]),
  );
  const devices = envelope.selected_device_uuids
    .map((uuid) => devicesByUuid.get(uuid) ?? uuid)
    .join("、");
  const ceiling = envelope.hard_ceiling.seconds === null
    ? "开放式 hard ceiling"
    : `${envelope.hard_ceiling.seconds.toLocaleString("zh-CN")} 秒 hard ceiling`;
  return `已绑定 ${envelope.selected_device_uuids.length} 张实际检测设备。` +
    `时间预算 ${timeBudgetLabel(envelope.time_budget)} · ${ceiling} · ${devices || "未选设备"}；` +
    `状态 ${envelope.status}，不是 Run ResourceBinding。`;
}

function timeBudgetLabel(value: QuestDraft["time_budget"]): string {
  return { "7d": "7 天", "30d": "30 天", "90d": "90 天", open: "不设硬截止" }[value];
}

function formatMemory(memoryTotalMib: number): string {
  const gib = memoryTotalMib / 1024;
  return `${Number.isInteger(gib) ? gib.toFixed(0) : gib.toFixed(1)} GiB`;
}

function researchAssetBinding(asset: ResearchAssetItem): Record<string, unknown> {
  return {
    asset_ref: asset.asset_ref,
    version_ref: asset.version_ref,
    content_hash: asset.content_hash,
    manifest_hash: asset.manifest_hash,
    receipt: { ...asset.receipt },
  };
}

function questMaterialIntakeContext(
  initializationId: string,
): ScopedAssetIntakeContext {
  return { kind: "quest_creation", ref: initializationId };
}

function boundaryText(draft: QuestDraft): string {
  const completion = draft.completion_criteria;
  const exclusions = draft.literature.scope_exclusions;
  if (!exclusions.trim()) return completion;
  return `${completion}${BOUNDARY_SEGMENT_SEPARATOR}${exclusions}`;
}

function boundaryInputMaxLength(draft: QuestDraft): number {
  return draft.literature.scope_exclusions.trim()
    ? BOUNDARY_CANONICAL_INPUT_MAX_LENGTH
    : BOUNDARY_RAW_INPUT_MAX_LENGTH;
}

function boundaryDraft(draft: QuestDraft, boundary: string): QuestDraft {
  const separatorIndex = boundary.indexOf(BOUNDARY_SEGMENT_SEPARATOR);
  const rawCompletion = separatorIndex >= 0
    ? boundary.slice(0, separatorIndex)
    : boundary.slice(0, BOUNDARY_COMPLETION_MAX_LENGTH);
  const rawExclusions = separatorIndex >= 0
    ? boundary.slice(separatorIndex + BOUNDARY_SEGMENT_SEPARATOR.length)
    : boundary.slice(BOUNDARY_COMPLETION_MAX_LENGTH);
  const completion = rawCompletion.slice(0, BOUNDARY_COMPLETION_MAX_LENGTH);
  const completionOverflow = rawCompletion.slice(BOUNDARY_COMPLETION_MAX_LENGTH);
  const exclusions = `${completionOverflow}${rawExclusions}`.slice(
    0,
    BOUNDARY_EXCLUSIONS_MAX_LENGTH,
  );
  return {
    ...draft,
    completion_criteria: completion,
    literature: {
      ...draft.literature,
      // One visible Boundary remains reversible across the two durable fields;
      // neither half can overflow its public write-contract limit.
      scope_exclusions: exclusions,
    },
  };
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function awaitQuestMaterialIntake(
  initial: AssetIntakeResult,
): Promise<AssetIntakeResult> {
  const existing = questMaterialIntakeFlights.get(initial.job_ref);
  if (existing) return existing;
  const aliases = new Set([initial.job_ref]);
  let flight!: Promise<AssetIntakeResult>;
  flight = (async () => {
    const deadline = Date.now() + QUEST_MATERIAL_POLL_TIMEOUT_MS;
    let result = initial;
    let retryCount = 0;
    while (["queued", "processing"].includes(result.status)) {
      if (Date.now() >= deadline) {
        throw new ProductError("quest_material_intake_poll_timeout");
      }
      await delay(Math.min(4_000, 250 * 2 ** Math.min(retryCount, 4)));
      try {
        result = await fetchAssetIntake(result.job_ref);
        retryCount = 0;
        if (!aliases.has(result.job_ref)) {
          aliases.add(result.job_ref);
          questMaterialIntakeFlights.set(result.job_ref, flight);
        }
      } catch (caught) {
        retryCount += 1;
        if (
          retryCount >= 5 ||
          caught instanceof ProductError &&
            /^request_failed:(401|403|4\d\d)$/.test(caught.code)
        ) throw caught;
      }
    }
    return result;
  })().finally(() => {
    for (const alias of aliases) {
      if (questMaterialIntakeFlights.get(alias) === flight) {
        questMaterialIntakeFlights.delete(alias);
      }
    }
  });
  questMaterialIntakeFlights.set(initial.job_ref, flight);
  return flight;
}

async function bindAcceptedQuestMaterial(
  initializationId: string,
  asset: ResearchAssetItem,
): Promise<QuestCreationView> {
  const bindingKey = `${initializationId}:${asset.version_ref}`;
  const existing = questMaterialBindingFlights.get(bindingKey);
  if (existing) return existing;
  let flight!: Promise<QuestCreationView>;
  flight = bindAcceptedQuestMaterialOnce(initializationId, asset)
    .finally(() => {
      if (questMaterialBindingFlights.get(bindingKey) === flight) {
        questMaterialBindingFlights.delete(bindingKey);
      }
    });
  questMaterialBindingFlights.set(bindingKey, flight);
  return flight;
}

function enqueueQuestMaterialCommit(
  initializationId: string,
  operation: () => Promise<void>,
): Promise<void> {
  const prior = questMaterialCommitFlights.get(initializationId) ?? Promise.resolve();
  const current = prior.catch(() => undefined).then(operation);
  questMaterialCommitFlights.set(initializationId, current);
  void current.then(
    () => {
      if (questMaterialCommitFlights.get(initializationId) === current) {
        questMaterialCommitFlights.delete(initializationId);
      }
    },
    () => {
      if (questMaterialCommitFlights.get(initializationId) === current) {
        questMaterialCommitFlights.delete(initializationId);
      }
    },
  );
  return current;
}

async function bindAcceptedQuestMaterialOnce(
  initializationId: string,
  asset: ResearchAssetItem,
): Promise<QuestCreationView> {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const latest = normalizeQuestCreationView(
      await fetchQuestCreation(initializationId),
    );
    if (latest.quest_draft.value.literature.accepted_material_bindings.some(
      (binding) => binding.version_ref === asset.version_ref,
    )) return latest;
    if (creationIsLocked(latest)) {
      throw new ProductError("quest_material_binding_closed");
    }
    const nextDraft = cloneDraft(latest.quest_draft.value);
    nextDraft.literature.accepted_material_bindings.push(
      researchAssetBinding(asset),
    );
    try {
      return await reviseQuestDraft(latest, nextDraft, draftBasisOf(latest));
    } catch (caught) {
      if (!(caught instanceof ProductError) || caught.code !== "quest_draft_stale") {
        throw caught;
      }
    }
  }
  throw new ProductError("quest_material_binding_pending");
}

function materialUploadStatusCopy(status: MaterialUploadStatus): string {
  return {
    reading: "正在读取",
    accepting: "后台处理中；你可以继续填写",
    binding: "已接纳，正在加入当前草案",
    failed: "未加入",
  }[status];
}

function remainingQuestMaterialSlots(
  draft: QuestDraft,
  uploads: MaterialUpload[],
): number {
  const bound = draft.literature.accepted_material_bindings.length;
  const pending = uploads.filter((upload) => upload.status !== "failed").length;
  return Math.max(0, QUEST_MATERIAL_LIMIT - bound - pending);
}

function questMaterialLimitFailure(remaining: number): ProductFailure {
  return {
    code: "quest_material_limit_exceeded",
    message: `每个 Quest 最多可加入 ${QUEST_MATERIAL_LIMIT} 个材料；` +
      `当前剩余 ${remaining} 个。本次选择未读取、未上传。`,
  };
}

function materialUploadFailureCopy(code: string): string {
  return {
    asset_content_too_large: "文件超过 64 MiB；未上传",
    quest_material_binding_closed: "材料已接纳，但 Quest 已确认，未加入本次依据",
    quest_material_binding_pending: "材料已接纳；自动加入待重试",
  }[code] ?? `材料处理未完成（${code}）；其他步骤不受影响`;
}

function messageFor(code: string): string {
  const messages: Record<string, string> = {
    quest_draft_stale: "Quest basis 已变化；已停止当前写入，请重新检查字段。",
    question_proposal_stale: "首问题依据已变化；旧 Proposal 保留，请重新生成或编辑后复核。",
    quest_reload_stale: "载入期间 durable 版本再次推进；本地未保存修改仍保留，请重新载入。",
    confirmation_preview_required: "Impact Preview 尚未绑定当前 Quest 与首问题，不能确认。",
    confirmation_preview_stale: "Impact Preview 已陈旧；等待系统自动刷新后再确认。",
    quest_basis_incomplete: "请先补齐 Quest 目标与边界。",
    quest_boundary_too_long: "边界正文最多 12,000 字；“范围与排除”分隔文字不计入正文。请缩短后再保存。",
    resource_envelope_required: "请先检测真实本机计算卡并形成 Quest Resource Envelope。",
    acquisition_session_required: "请先检测真实文献获取环境并形成当前 AcquisitionSession。",
    acquisition_session_stale: "AcquisitionSession 与当前文献配置不一致；请重新检测。",
    acquisition_session_not_ready: "当前 AcquisitionSession 尚未 ready；请按状态提示处理后重新检测。",
    deepfetch_acquisition_not_ready: "DeepFetch 正等待当前 AcquisitionSession 恢复 ready。",
    institutional_entry_required: "全面搜索需要填写获准的图书馆或数据库入口链接。",
    library_entry_url_required: "全面搜索需要填写获准的图书馆或数据库入口链接，或切换为只搜索开放获取资源。",
    institutional_login_required: "请在已打开的 Google Chrome 资源页完成机构登录，然后重新检测。",
    institutional_preflight_unavailable: "机构资源预检未通过；请确认本机浏览器控制与入口可用。",
    institutional_resource_unverified: "浏览器已连接，但尚未验证到获准的数据库资源页。",
    nature_downloader_unavailable: "固定 Nature Downloader provider 当前不可执行。",
    deepfetch_acquisition_waiting_user: "DeepFetch 已保存精确批次，正在等待你完成浏览器登录；恢复后不会换 request_id。",
    literature_snapshot_required: "当前 DeepFetch basis 尚无已接纳 LiteratureSnapshot；请先运行或重试检索。",
    literature_snapshot_stale: "LiteratureSnapshot 与当前 DraftRevision 不一致；旧快照已保留但不会静默套用。",
    web_search_temporarily_unavailable: "真实 Web Search 暂不可用；可沿同一 correlation 重试，不会转成 direct waiver。",
    deepfetch_web_evidence_invalid: "DeepFetch 未形成可验证的 Web Search / Fetch 证据；这不是成功结果。请在运行环境恢复后沿同一记录重试。",
    codex_deepfetch_timeout: "DeepFetch 本次 Attempt 超时；当前草案未丢失，可从同一运行检查点重试。",
    deepfetch_provider_stopped: "DeepFetch Provider 已停止；系统会按原 correlation 恢复，不会接纳晚到结果。",
    deepfetch_run_identity_conflict: "DeepFetch 的 durable Run 身份与当前执行记录冲突。",
    initialization_cancelled: "初始化已取消；DeepFetch 结果不会转成 Proposal 或任何 domain object。",
    research_memory_asset_intake_not_delivered: "材料接纳尚未交付；raw 文件或路径不能进入 basis。",
    codex_cli_unavailable: "生产 Proposal Drafter 当前不可用；系统不会用静态模板代替。",
    codex_cli_failed: "生产 Proposal Drafter 未完成请求；当前草案与旧 Proposal 已保留。",
    proposal_drafter_unavailable: "生产 Proposal Drafter 当前不可用；当前草案仍可继续编辑。",
    csrf_token_unavailable: "会话写入凭据不可用，请重新打开认证页面。",
    idempotency_conflict: "该操作的重试内容与原请求不一致，已安全停止。",
    compute_device_selection_stale: "本机设备快照已变化，请重新检测并选择。",
    host_compute_unavailable: "本机计算 Provider 当前不可用。",
    quest_initialization_poll_timeout: "后台操作仍未收敛；可以关闭窗口，稍后从同一入口恢复。",
  };
  return messages[code] ?? `操作未完成（${code}）。当前 durable 草案与已接纳事实不会回滚。`;
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
