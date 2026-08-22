import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";

import "./ManualCreation.css";


export type ManualQuestionContent = {
  title: string;
  unknown_statement: string;
  answer_shape: string;
  applicability_scope: string;
  background_context: string;
  requirements_constraints: string;
};

export type ManualCreationReceiptState =
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
      status: "rejected" | "stale" | "unavailable";
      reason: { code: string; upstream_step?: string };
    };

export type ManualCreationAcceptedMaterialBindingView = {
  asset_ref: string;
  version_ref: string;
  content_hash: string;
  manifest_hash: string;
  receipt: Extract<ManualCreationReceiptState, { status: "accepted" }>;
};

export type ManualCreationConfirmedSeedValue = {
  intent: string;
  fields: ManualQuestionContent;
  accepted_material_bindings: readonly ManualCreationAcceptedMaterialBindingView[];
  deepfetch_preference: "use" | "skip" | "later";
};

export type ManualCreationSeedView = {
  ref: string | null;
  hash: string | null;
  value: ManualCreationConfirmedSeedValue | null;
  receipt: ManualCreationReceiptState;
};

export type ManualCreationDeepFetchView = {
  status:
    | "not_started"
    | "queued"
    | "running"
    | "completed"
    | "failed"
    | "cancelled"
    | "unavailable";
  run_ref: string | null;
  phase?: string | null;
  processed?: number | null;
  total?: number | null;
  elapsed_seconds?: number | null;
  last_progress_at?: string | null;
  receipt: ManualCreationReceiptState;
  reason?: { code: string } | null;
};

export type ManualCreationResearchView = {
  decision: "undecided" | "deepfetch" | "waiver";
  basis_hash: string | null;
  deepfetch: ManualCreationDeepFetchView | null;
  waiver_receipt: ManualCreationReceiptState;
};

export type ManualCreationDraftingTurn = {
  turn_ref: string;
  role: "user" | "assistant";
  content: string;
  status?: "queued" | "running" | "completed" | "failed";
};

export type ManualCreationDraftingSessionView = {
  session_ref: string | null;
  status: "inactive" | "ready" | "waiting" | "unavailable" | "closed";
  turns: readonly ManualCreationDraftingTurn[];
  reason?: { code: string } | null;
};

export type ManualQuestionProposalView = {
  ref: string;
  hash: string;
  content: ManualQuestionContent;
  status: "draft" | "current" | "confirmed" | "stale";
};

export type ManualQuestionCreationView = {
  creation_id: string;
  status:
    | "seed_draft"
    | "seed_confirmed"
    | "drafting"
    | "proposal_ready"
    | "confirming"
    | "partial"
    | "recovering"
    | "completed"
    | "cancelled"
    | "stale"
    | "unavailable";
  quest_ref: string;
  quest_title?: string | null;
  parent_question_ref: string;
  parent_question_title?: string | null;
  seed: ManualCreationSeedView;
  research: ManualCreationResearchView;
  drafting_session: ManualCreationDraftingSessionView;
  proposal: ManualQuestionProposalView | null;
  receipts: {
    proposal_confirmation: ManualCreationReceiptState;
    question_content: ManualCreationReceiptState;
    question_identity: ManualCreationReceiptState;
  };
  question_anchor?: {
    ref: string;
    question_ref: string;
    content_ref: string;
    content_hash: string;
  } | null;
  failure?: { code: string; message?: string } | null;
};

export type ManualCreationMaterialDraft = {
  mode: "unprovided" | "folder" | "files" | "path";
  files: readonly File[];
  local_path: string;
};

export type ManualCreationSeedConfirmationDraft = {
  intent: string;
  fields: ManualQuestionContent;
  deepfetch_preference: "use" | "skip" | "later";
  material_draft: ManualCreationMaterialDraft;
};

export type ManualCreationProposalSaveInput = {
  creation_id: string;
  expected_basis_hash: string;
  expected_proposal_ref: string | null;
  expected_proposal_hash: string | null;
  content: ManualQuestionContent;
};

export type ManualCreationProps = {
  view: ManualQuestionCreationView;
  returnFocusTo?: HTMLElement | null;
  onClose: () => void;
  onCancel: (input: { creation_id: string }) => void | Promise<void>;
  onConfirmSeed: (input: {
    creation_id: string;
    seed: ManualCreationSeedConfirmationDraft;
  }) => void | Promise<void>;
  onStartDeepFetch: (input: {
    creation_id: string;
    seed_ref: string;
    seed_hash: string;
  }) => void | Promise<void>;
  onConfirmWaiver: (input: {
    creation_id: string;
    seed_ref: string;
    seed_hash: string;
  }) => void | Promise<void>;
  onSendDraftMessage: (input: {
    creation_id: string;
    session_ref: string;
    expected_basis_hash: string;
    message: string;
  }) => void | Promise<void>;
  onSaveProposal: (
    input: ManualCreationProposalSaveInput,
  ) => ManualQuestionProposalView | Promise<ManualQuestionProposalView>;
  onConfirmProposal: (input: {
    creation_id: string;
    proposal_ref: string;
    proposal_hash: string;
    content: ManualQuestionContent;
  }) => void | Promise<void>;
  onMaterialDraftChange?: (draft: ManualCreationMaterialDraft) => void;
};

type ResearchPreference = "deepfetch" | "waiver" | "later";
type BusyAction =
  | "seed"
  | "deepfetch"
  | "waiver"
  | "message"
  | "proposal"
  | "confirm"
  | "cancel";

const blankQuestion: ManualQuestionContent = {
  title: "",
  unknown_statement: "",
  answer_shape: "",
  applicability_scope: "",
  background_context: "",
  requirements_constraints: "",
};

const requiredFields: ReadonlyArray<keyof ManualQuestionContent> = [
  "title",
  "unknown_statement",
  "answer_shape",
  "applicability_scope",
];

const questionFieldMaxLengths: Record<keyof ManualQuestionContent, number> = {
  title: 500,
  unknown_statement: 8_000,
  answer_shape: 8_000,
  applicability_scope: 8_000,
  background_context: 12_000,
  requirements_constraints: 12_000,
};
const seedIntentMaxLength = 12_000;
const draftingMessageMaxLength = 12_000;
const maxAcceptedMaterialBindings = 100;
const pseudoQuestionValues = new Set([
  "unknown",
  "not_applicable",
  "not applicable",
  "n/a",
  "na",
]);

const proposalFields: ReadonlyArray<{
  key: keyof ManualQuestionContent;
  label: string;
  seedLabel: string;
  placeholder: string;
  full?: boolean;
  rows: number;
}> = [
  {
    key: "title",
    label: "问题标题",
    seedLabel: "问题标题，可选",
    placeholder: "简短导航标签，例如：低资源条件下的长链一致性边界",
    full: true,
    rows: 1,
  },
  {
    key: "unknown_statement",
    label: "要解决的未知",
    seedLabel: "要解决的未知，可选",
    placeholder: "你真正还不知道、希望研究回答的是什么？",
    rows: 3,
  },
  {
    key: "answer_shape",
    label: "合格答案的形状",
    seedLabel: "合格答案的形状，可选",
    placeholder: "什么类型的答案才算足够？不要写实验协议或指标。",
    rows: 3,
  },
  {
    key: "applicability_scope",
    label: "适用范围与排除项",
    seedLabel: "适用范围与排除项，可选",
    placeholder: "答案适用于哪些对象、条件和范围？明确排除什么？",
    full: true,
    rows: 3,
  },
  {
    key: "background_context",
    label: "背景上下文",
    seedLabel: "背景上下文，可选",
    placeholder: "理解这个问题所需的背景。",
    rows: 3,
  },
  {
    key: "requirements_constraints",
    label: "会改变问题含义的约束",
    seedLabel: "会改变问题含义的约束，可选",
    placeholder: "只写会改变 Question 含义的要求；不要写实现方案。",
    rows: 3,
  },
];

function receiptAccepted(receipt: ManualCreationReceiptState): boolean {
  return receipt.status === "accepted";
}

function contentEquals(
  left: ManualQuestionContent,
  right: ManualQuestionContent,
): boolean {
  return proposalFields.every(({ key }) => left[key] === right[key]);
}

function cloneContent(content: ManualQuestionContent): ManualQuestionContent {
  return { ...content };
}

function normalizeContent(content: ManualQuestionContent): ManualQuestionContent {
  return {
    title: content.title.trim(),
    unknown_statement: content.unknown_statement.trim(),
    answer_shape: content.answer_shape.trim(),
    applicability_scope: content.applicability_scope.trim(),
    background_context: content.background_context.trim(),
    requirements_constraints: content.requirements_constraints.trim(),
  };
}

function questionComplete(content: ManualQuestionContent): boolean {
  return contentIssue(content, true) === null;
}

function contentIssue(
  content: ManualQuestionContent,
  requireComplete: boolean,
): { key: keyof ManualQuestionContent; code: string } | null {
  for (const { key } of proposalFields) {
    const value = content[key].trim();
    if (value.length > questionFieldMaxLengths[key]) {
      return { key, code: `${key}_too_long` };
    }
    if (
      requireComplete &&
      requiredFields.includes(key) &&
      (!value || pseudoQuestionValues.has(value.toLowerCase()))
    ) {
      return { key, code: `${key}_required` };
    }
  }
  return null;
}

function errorDetail(caught: unknown): { code: string; message: string } {
  if (caught && typeof caught === "object") {
    const code = "code" in caught && typeof caught.code === "string"
      ? caught.code
      : "manual_creation_action_failed";
    const message = "message" in caught && typeof caught.message === "string"
      ? caught.message
      : code;
    return { code, message };
  }
  return {
    code: "manual_creation_action_failed",
    message: "操作没有完成；当前 CreationContext 保持不变。",
  };
}

function receiptCopy(receipt: ManualCreationReceiptState): string {
  if (receipt.status === "accepted") return `accepted · ${receipt.receipt_ref}`;
  if (receipt.status === "not_attempted") {
    return receipt.reason?.code ?? "not_attempted";
  }
  return `${receipt.status} · ${receipt.reason.code}`;
}

function deepFetchCopy(deepfetch: ManualCreationDeepFetchView | null): string {
  if (!deepfetch || deepfetch.status === "not_started") return "尚未启动";
  if (deepfetch.status === "queued") return "已排队；等待 durable Projection";
  if (deepfetch.status === "running") {
    const count = deepfetch.processed == null
      ? ""
      : deepfetch.total == null
        ? ` · 已处理 ${deepfetch.processed}`
        : ` · ${deepfetch.processed} / ${deepfetch.total}`;
    return `${deepfetch.phase ?? "运行中"}${count}`;
  }
  if (deepfetch.status === "completed") {
    return receiptAccepted(deepfetch.receipt)
      ? `当前批次已完成 · ${receiptCopy(deepfetch.receipt)}`
      : "执行已完成；等待独立 completion receipt";
  }
  return `${deepfetch.status} · ${deepfetch.reason?.code ?? "可重试或明确 waiver"}`;
}

function actionLabel(action: BusyAction | null): string | null {
  return action === "seed"
    ? "正在确认 Seed…"
    : action === "deepfetch"
      ? "正在启动 DeepFetch…"
      : action === "waiver"
        ? "正在确认 waiver…"
        : action === "message"
          ? "正在发送…"
          : action === "proposal"
            ? "正在保存 Proposal…"
            : action === "confirm"
              ? "正在确认最终问题…"
              : action === "cancel"
                ? "正在取消 CreationContext…"
                : null;
}

export function ManualCreation({
  view,
  returnFocusTo,
  onClose,
  onCancel,
  onConfirmSeed,
  onStartDeepFetch,
  onConfirmWaiver,
  onSendDraftMessage,
  onSaveProposal,
  onConfirmProposal,
  onMaterialDraftChange,
}: ManualCreationProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const seedInputRef = useRef<HTMLTextAreaElement>(null);
  const sessionInputRef = useRef<HTMLTextAreaElement>(null);
  const firstMissingFieldRef = useRef<HTMLInputElement | HTMLTextAreaElement | null>(null);
  const researchActionRef = useRef<HTMLButtonElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const filesInputRef = useRef<HTMLInputElement>(null);
  const closeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);
  const savePromiseRef = useRef<Promise<ManualQuestionProposalView | null> | null>(null);
  const busyActionRef = useRef<BusyAction | null>(null);
  const proposalDraftRef = useRef<ManualQuestionContent>(
    cloneContent(view.proposal?.content ?? view.seed.value?.fields ?? blankQuestion),
  );
  const savedProposalRef = useRef<ManualQuestionProposalView | null>(view.proposal);
  const previousSeedConfirmedRef = useRef(receiptAccepted(view.seed.receipt));

  const [dialogOpen, setDialogOpen] = useState(false);
  const [seedDraft, setSeedDraft] = useState(view.seed.value?.intent ?? "");
  const [proposalDraft, setProposalDraft] = useState<ManualQuestionContent>(
    cloneContent(view.proposal?.content ?? view.seed.value?.fields ?? blankQuestion),
  );
  const [savedProposal, setSavedProposal] = useState<ManualQuestionProposalView | null>(
    view.proposal,
  );
  const [researchPreference, setResearchPreference] = useState<ResearchPreference>(
    view.research.decision === "deepfetch"
      ? "deepfetch"
      : view.research.decision === "waiver"
        ? "waiver"
        : view.seed.value?.deepfetch_preference === "use"
          ? "deepfetch"
          : view.seed.value?.deepfetch_preference === "skip"
            ? "waiver"
            : "later",
  );
  const [messageDraft, setMessageDraft] = useState("");
  const [proposalEditing, setProposalEditing] = useState(false);
  const [busyAction, setBusyAction] = useState<BusyAction | null>(null);
  const [proposalSaving, setProposalSaving] = useState(false);
  const [seedConfirmationDispatched, setSeedConfirmationDispatched] = useState(false);
  const [researchActionDispatched, setResearchActionDispatched] = useState<
    "deepfetch" | "waiver" | null
  >(null);
  const [proposalConfirmationDispatched, setProposalConfirmationDispatched] = useState(false);
  const [localFailure, setLocalFailure] = useState<{
    code: string;
    message: string;
  } | null>(null);
  const [materialDraft, setMaterialDraft] = useState<ManualCreationMaterialDraft>({
    mode: "unprovided",
    files: [],
    local_path: "",
  });

  const seedConfirmed = receiptAccepted(view.seed.receipt);
  const proposalConfirmed = receiptAccepted(view.receipts.proposal_confirmation);
  const contentAccepted = receiptAccepted(view.receipts.question_content);
  const identityAccepted = receiptAccepted(view.receipts.question_identity);
  const deepFetchAccepted = Boolean(
    view.research.deepfetch && receiptAccepted(view.research.deepfetch.receipt),
  );
  const waiverAccepted = receiptAccepted(view.research.waiver_receipt);
  const researchSatisfied = deepFetchAccepted || waiverAccepted;
  const terminal = view.status === "completed" || view.status === "cancelled";
  const writesUnavailable = view.status === "stale" || view.status === "unavailable";
  const seedConfirmationPending = seedConfirmationDispatched && !seedConfirmed;
  const proposalConfirmationPending =
    proposalConfirmationDispatched && !proposalConfirmed;
  const acceptedMaterialBindings = view.seed.value?.accepted_material_bindings ?? [];
  const isBusy = busyAction !== null || proposalSaving;
  const authoritativeProposal = savedProposal ?? view.proposal;
  const proposalDirty = !authoritativeProposal || !contentEquals(
    proposalDraft,
    authoritativeProposal.content,
  );
  const proposalIsCurrent = Boolean(
    authoritativeProposal &&
    ["current", "confirmed"].includes(authoritativeProposal.status) &&
    !proposalDirty,
  );
  const canConfirmProposal =
    seedConfirmed &&
    researchSatisfied &&
    questionComplete(proposalDraft) &&
    !proposalConfirmed &&
    !proposalConfirmationDispatched &&
    !terminal &&
    !writesUnavailable &&
    !isBusy;

  const missingFields = useMemo(
    () => requiredFields.filter((key) => !proposalDraft[key].trim()),
    [proposalDraft],
  );
  const proposalIssue = useMemo(
    () => contentIssue(proposalDraft, true),
    [proposalDraft],
  );

  const visibleFailure = localFailure ?? (view.failure
    ? {
        code: view.failure.code,
        message: view.failure.message ?? view.failure.code,
      }
    : null);

  useEffect(() => {
    mountedRef.current = true;
    const dialog = dialogRef.current;
    if (!returnFocusRef.current) {
      returnFocusRef.current = returnFocusTo ?? (
        document.activeElement instanceof HTMLElement ? document.activeElement : null
      );
    }
    if (dialog && !dialog.open) dialog.showModal();
    folderInputRef.current?.setAttribute("webkitdirectory", "");
    folderInputRef.current?.setAttribute("directory", "");

    const openFrame = requestAnimationFrame(() => {
      setDialogOpen(true);
    });
    return () => {
      mountedRef.current = false;
      cancelAnimationFrame(openFrame);
      if (closeTimerRef.current !== null) clearTimeout(closeTimerRef.current);
    };
  }, [returnFocusTo]);

  useEffect(() => {
    if (!dialogOpen) return;
    let transitionFallback: ReturnType<typeof setTimeout> | null = null;
    const focusInitialControl = () => {
      const dialog = dialogRef.current;
      const closeButton = closeButtonRef.current;
      if (!dialog?.open || !closeButton?.isConnected) return true;
      if (
        document.activeElement instanceof HTMLElement &&
        dialog.contains(document.activeElement)
      ) {
        return true;
      }
      void closeButton.offsetWidth;
      closeButton.focus({ preventScroll: true });
      return document.activeElement === closeButton;
    };
    const postTopLayerTimer = setTimeout(() => {
      if (!focusInitialControl()) {
        transitionFallback = setTimeout(focusInitialControl, 260);
      }
    }, 0);
    return () => {
      clearTimeout(postTopLayerTimer);
      if (transitionFallback !== null) clearTimeout(transitionFallback);
    };
  }, [dialogOpen]);

  useEffect(() => {
    setSeedDraft(view.seed.value?.intent ?? "");
    const nextProposal = cloneContent(
      view.proposal?.content ?? view.seed.value?.fields ?? blankQuestion,
    );
    proposalDraftRef.current = nextProposal;
    savedProposalRef.current = view.proposal;
    setProposalDraft(nextProposal);
    setSavedProposal(view.proposal);
    setResearchPreference(
      view.research.decision === "deepfetch"
        ? "deepfetch"
        : view.research.decision === "waiver"
          ? "waiver"
          : view.seed.value?.deepfetch_preference === "use"
            ? "deepfetch"
            : view.seed.value?.deepfetch_preference === "skip"
              ? "waiver"
              : "later",
    );
    setMessageDraft("");
    setProposalEditing(false);
    setProposalSaving(false);
    setSeedConfirmationDispatched(false);
    setResearchActionDispatched(null);
    setProposalConfirmationDispatched(false);
    setMaterialDraft({ mode: "unprovided", files: [], local_path: "" });
    setLocalFailure(null);
  }, [view.creation_id]);

  useEffect(() => {
    if (!seedConfirmed || view.seed.value == null) return;
    setSeedDraft(view.seed.value.intent);
  }, [seedConfirmed, view.seed.hash, view.seed.value]);

  useEffect(() => {
    const incoming = view.proposal;
    if (!incoming) return;
    const current = savedProposalRef.current;
    if (current?.ref === incoming.ref && current.hash === incoming.hash) return;
    const wasDirty = current == null || !contentEquals(
      proposalDraftRef.current,
      current.content,
    );
    savedProposalRef.current = incoming;
    setSavedProposal(incoming);
    if (!wasDirty || contentEquals(proposalDraftRef.current, incoming.content)) {
      const next = cloneContent(incoming.content);
      proposalDraftRef.current = next;
      setProposalDraft(next);
    }
  }, [view.proposal?.hash, view.proposal?.ref]);

  useEffect(() => {
    if (view.research.decision === "deepfetch") setResearchPreference("deepfetch");
    if (view.research.decision === "waiver") setResearchPreference("waiver");
    if (
      view.research.decision !== "undecided" ||
      (view.research.deepfetch && view.research.deepfetch.status !== "not_started")
    ) {
      setResearchActionDispatched(null);
    }
  }, [view.research.decision, view.research.deepfetch?.status]);

  useEffect(() => {
    if (seedConfirmed) setSeedConfirmationDispatched(false);
    if (
      !seedConfirmed &&
      ["rejected", "stale", "unavailable"].includes(view.seed.receipt.status)
    ) {
      setSeedConfirmationDispatched(false);
    }
  }, [seedConfirmed, view.seed.receipt.status]);

  useEffect(() => {
    if (proposalConfirmed) setProposalConfirmationDispatched(true);
    if (
      !proposalConfirmed &&
      ["rejected", "stale", "unavailable"].includes(
        view.receipts.proposal_confirmation.status,
      )
    ) {
      setProposalConfirmationDispatched(false);
    }
  }, [proposalConfirmed, view.receipts.proposal_confirmation.status]);

  useEffect(() => {
    const previouslyConfirmed = previousSeedConfirmedRef.current;
    previousSeedConfirmedRef.current = seedConfirmed;
    if (!previouslyConfirmed && seedConfirmed) {
      if (!view.proposal && view.seed.value) {
        const confirmedFields = cloneContent(view.seed.value.fields);
        proposalDraftRef.current = confirmedFields;
        setProposalDraft(confirmedFields);
      }
      setProposalEditing(false);
      requestAnimationFrame(() => {
        sessionInputRef.current?.focus({ preventScroll: false });
      });
    }
  }, [seedConfirmed, view.proposal, view.seed.value]);

  useEffect(() => {
    if (!visibleFailure) return;
    requestAnimationFrame(() => errorRef.current?.focus());
  }, [visibleFailure]);

  const finalizeClose = useCallback(() => {
    if (closeTimerRef.current !== null) return;
    const focusTarget = returnFocusRef.current;
    setDialogOpen(false);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    closeTimerRef.current = setTimeout(() => {
      closeTimerRef.current = null;
      if (dialogRef.current?.open) dialogRef.current.close();
      onClose();
      requestAnimationFrame(() => {
        if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
      });
    }, reducedMotion ? 0 : 240);
  }, [onClose]);

  const runAction = useCallback(async (
    action: BusyAction,
    work: () => void | Promise<void>,
  ): Promise<boolean> => {
    if (busyActionRef.current !== null) return false;
    busyActionRef.current = action;
    setBusyAction(action);
    setLocalFailure(null);
    try {
      await work();
      return true;
    } catch (caught) {
      if (mountedRef.current) setLocalFailure(errorDetail(caught));
      return false;
    } finally {
      busyActionRef.current = null;
      if (mountedRef.current) setBusyAction(null);
    }
  }, []);

  const seedIdentity = useCallback(() => {
    if (
      !seedConfirmed ||
      !view.seed.ref ||
      !view.seed.hash
    ) {
      setLocalFailure({
        code: "confirmed_seed_identity_missing",
        message: "Seed receipt 已返回，但精确 Seed identity 不完整；操作保持关闭。",
      });
      return null;
    }
    return { seed_ref: view.seed.ref, seed_hash: view.seed.hash };
  }, [seedConfirmed, view.seed.hash, view.seed.ref]);

  const confirmSeed = async () => {
    const exactSeed = seedDraft;
    if (!exactSeed.trim()) {
      seedInputRef.current?.focus();
      return;
    }
    if (exactSeed.length > seedIntentMaxLength) {
      setLocalFailure({
        code: "manual_creation_seed_intent_too_long",
        message: `CreationSeed 最多 ${seedIntentMaxLength.toLocaleString()} 个字符。`,
      });
      seedInputRef.current?.focus();
      return;
    }
    const seedFieldIssue = contentIssue(proposalDraftRef.current, false);
    if (seedFieldIssue) {
      setLocalFailure({
        code: seedFieldIssue.code,
        message: "结构化 Seed 字段超过 Owner 接纳上限；尚未提交。",
      });
      dialogRef.current?.querySelector<HTMLElement>(
        `[data-manual-proposal-field="${seedFieldIssue.key}"]`,
      )?.focus({ preventScroll: false });
      return;
    }
    if (materialDraft.files.length > maxAcceptedMaterialBindings) {
      setLocalFailure({
        code: "accepted_material_bindings_invalid",
        message: `一次 Seed 最多接纳 ${maxAcceptedMaterialBindings} 个材料版本。`,
      });
      return;
    }
    setSeedConfirmationDispatched(true);
    const dispatched = await runAction("seed", () => onConfirmSeed({
      creation_id: view.creation_id,
      seed: {
        intent: exactSeed,
        fields: normalizeContent(proposalDraftRef.current),
        deepfetch_preference: researchPreference === "deepfetch"
          ? "use"
          : researchPreference === "waiver"
            ? "skip"
            : "later",
        material_draft: {
          ...materialDraft,
          files: [...materialDraft.files],
        },
      },
    }));
    if (!dispatched && mountedRef.current) setSeedConfirmationDispatched(false);
  };

  const startDeepFetch = async () => {
    const identity = seedIdentity();
    if (!identity) return;
    setResearchPreference("deepfetch");
    setResearchActionDispatched("deepfetch");
    const dispatched = await runAction("deepfetch", () => onStartDeepFetch({
      creation_id: view.creation_id,
      ...identity,
    }));
    if (!dispatched && mountedRef.current) setResearchActionDispatched(null);
  };

  const confirmWaiver = async () => {
    const identity = seedIdentity();
    if (!identity) return;
    setResearchPreference("waiver");
    setResearchActionDispatched("waiver");
    const dispatched = await runAction("waiver", () => onConfirmWaiver({
      creation_id: view.creation_id,
      ...identity,
    }));
    if (!dispatched && mountedRef.current) setResearchActionDispatched(null);
  };

  const sendMessage = async () => {
    const expectedBasisHash = view.research.basis_hash ?? view.seed.hash;
    const message = messageDraft.trim();
    const sessionRef = view.drafting_session.session_ref;
    if (!seedConfirmed || !expectedBasisHash || !message || !sessionRef) return;
    if (message.length > draftingMessageMaxLength) {
      setLocalFailure({
        code: "manual_drafting_message_too_long",
        message: `Drafting message 最多 ${draftingMessageMaxLength.toLocaleString()} 个字符。`,
      });
      sessionInputRef.current?.focus();
      return;
    }
    const sent = await runAction("message", () => onSendDraftMessage({
      creation_id: view.creation_id,
      session_ref: sessionRef,
      expected_basis_hash: expectedBasisHash,
      message,
    }));
    if (sent && mountedRef.current) {
      setMessageDraft("");
      requestAnimationFrame(() => sessionInputRef.current?.focus());
    }
  };

  const persistProposal = useCallback(async (): Promise<ManualQuestionProposalView | null> => {
    const expectedBasisHash = view.research.basis_hash;
    if (
      !seedConfirmed ||
      !researchSatisfied ||
      !expectedBasisHash ||
      !questionComplete(proposalDraftRef.current) ||
      terminal ||
      writesUnavailable ||
      proposalConfirmed
    ) {
      return savedProposalRef.current;
    }
    if (savePromiseRef.current) {
      const pending = await savePromiseRef.current;
      if (!pending || contentEquals(proposalDraftRef.current, pending.content)) return pending;
      return persistProposal();
    }

    const basis = savedProposalRef.current;
    const captured = normalizeContent(proposalDraftRef.current);
    if (!contentEquals(captured, proposalDraftRef.current)) {
      proposalDraftRef.current = captured;
      if (mountedRef.current) setProposalDraft(captured);
    }
    if (basis && contentEquals(captured, basis.content)) return basis;

    setProposalSaving(true);
    setLocalFailure(null);
    const promise = Promise.resolve().then(() => onSaveProposal({
      creation_id: view.creation_id,
      expected_basis_hash: expectedBasisHash,
      expected_proposal_ref: basis?.ref ?? null,
      expected_proposal_hash: basis?.hash ?? null,
      content: captured,
    })).then((saved) => {
      if (!contentEquals(saved.content, captured)) {
        throw Object.assign(new Error(
          "公开保存响应与刚才编辑的 Proposal 不一致；没有继续确认。",
        ), { code: "saved_proposal_content_mismatch" });
      }
      savedProposalRef.current = saved;
      if (mountedRef.current) setSavedProposal(saved);
      return saved;
    }).catch((caught) => {
      if (mountedRef.current) setLocalFailure(errorDetail(caught));
      return null;
    });
    savePromiseRef.current = promise;
    const saved = await promise;
    if (savePromiseRef.current === promise) savePromiseRef.current = null;
    if (mountedRef.current) setProposalSaving(false);
    if (saved && !contentEquals(proposalDraftRef.current, saved.content)) {
      return persistProposal();
    }
    return saved;
  }, [
    onSaveProposal,
    proposalConfirmed,
    researchSatisfied,
    seedConfirmed,
    terminal,
    view.creation_id,
    view.research.basis_hash,
    writesUnavailable,
  ]);

  const confirmProposal = async () => {
    const issue = contentIssue(proposalDraft, true);
    if (issue) {
      setProposalEditing(true);
      setLocalFailure({
        code: issue.code,
        message: "请修正 Formal Question 字段后再确认精确 Proposal。",
      });
      requestAnimationFrame(() => {
        dialogRef.current?.querySelector<HTMLElement>(
          `[data-manual-proposal-field="${issue.key}"]`,
        )?.focus({ preventScroll: false });
      });
      return;
    }
    if (!researchSatisfied) {
      researchActionRef.current?.focus();
      return;
    }
    if (busyActionRef.current !== null || proposalConfirmationDispatched) return;
    setProposalConfirmationDispatched(true);
    busyActionRef.current = "confirm";
    setBusyAction("confirm");
    setLocalFailure(null);
    try {
      const exact = await persistProposal();
      if (!exact || !contentEquals(exact.content, proposalDraftRef.current)) {
        if (mountedRef.current) setProposalConfirmationDispatched(false);
        return;
      }
      await onConfirmProposal({
        creation_id: view.creation_id,
        proposal_ref: exact.ref,
        proposal_hash: exact.hash,
        content: cloneContent(exact.content),
      });
    } catch (caught) {
      if (mountedRef.current) {
        setProposalConfirmationDispatched(false);
        setLocalFailure(errorDetail(caught));
      }
    } finally {
      busyActionRef.current = null;
      if (mountedRef.current) setBusyAction(null);
    }
  };

  const cancelCreation = async () => {
    const cancelled = await runAction("cancel", () => onCancel({
      creation_id: view.creation_id,
    }));
    if (cancelled) finalizeClose();
  };

  const updateProposal = (
    key: keyof ManualQuestionContent,
    value: string,
  ) => {
    const next = { ...proposalDraftRef.current, [key]: value };
    proposalDraftRef.current = next;
    setProposalDraft(next);
    setLocalFailure(null);
  };

  const updateMaterials = (next: ManualCreationMaterialDraft) => {
    setMaterialDraft(next);
    onMaterialDraftChange?.(next);
  };

  const selectFiles = (files: FileList | null, mode: "folder" | "files") => {
    const selected = Array.from(files ?? []);
    if (selected.length > maxAcceptedMaterialBindings) {
      setLocalFailure({
        code: "accepted_material_bindings_invalid",
        message: `一次 Seed 最多接纳 ${maxAcceptedMaterialBindings} 个材料版本。`,
      });
    }
    updateMaterials({
      mode: selected.length ? mode : "unprovided",
      files: selected,
      local_path: "",
    });
  };

  const toggleProposalEditor = () => {
    if (!seedConfirmed || proposalConfirmed) return;
    const next = !proposalEditing;
    setProposalEditing(next);
    requestAnimationFrame(() => {
      if (next) {
        const missing = missingFields[0] ?? "title";
        dialogRef.current?.querySelector<HTMLElement>(
          `[data-manual-proposal-field="${missing}"]`,
        )?.focus({ preventScroll: false });
      } else {
        sessionInputRef.current?.focus({ preventScroll: false });
      }
    });
  };

  const trapFocus = (event: KeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), " +
      "select:not([disabled]), summary, a[href], [tabindex]:not([tabindex='-1'])",
    )).filter((element) => {
      const style = getComputedStyle(element);
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

  const researchRunning = Boolean(
    view.research.deepfetch &&
    ["queued", "running"].includes(view.research.deepfetch.status),
  );
  const sessionReady =
    seedConfirmed &&
    view.drafting_session.status === "ready" &&
    Boolean(view.drafting_session.session_ref);
  const stepLabel = !seedConfirmed
    ? "步骤 1 · CreationSeed"
    : !proposalConfirmed
      ? "步骤 2 · Drafting Session"
      : identityAccepted
        ? "QuestionAnchor · ready"
        : "步骤 3 · Owner acceptance";
  const footerNote = proposalConfirmed
    ? identityAccepted && view.question_anchor
      ? `稳定 QuestionAnchor · ${view.question_anchor.ref}`
      : contentAccepted
        ? "精确 Proposal 已确认 · RM 已接纳，等待 RG identity"
        : "精确 Proposal 已确认 · 等待 RM / RG 分别接纳"
    : !seedConfirmed
      ? seedDraft.trim()
        ? "描述已填写 · 其他空字段 = unprovided"
        : "请先写一句自然语言描述 · 其他空字段 = unprovided"
      : proposalIssue
        ? `问题草案需修正：${proposalIssue.code}`
        : !researchSatisfied
          ? "还需完成 DeepFetch 或确认独立 waiver"
          : proposalIsCurrent
            ? "当前精确 Proposal 已保存 · 可以确认最终问题"
            : "当前 Proposal 会先保存精确 identity，再提交确认";
  const pendingActionLabel = proposalSaving
    ? "正在保存 Proposal…"
    : seedConfirmationDispatched && !seedConfirmed
      ? "Seed confirmation 已提交 · 等待独立 HC receipt"
      : researchActionDispatched
        ? `${researchActionDispatched} 已提交 · 等待 durable Projection`
        : proposalConfirmationDispatched && !proposalConfirmed
          ? "精确 Proposal confirmation 已提交 · 等待 HC receipt"
          : actionLabel(busyAction);

  return (
    <dialog
      ref={dialogRef}
      className="manual-dialog"
      data-open={dialogOpen}
      data-seed-confirmed={seedConfirmed}
      data-proposal-editing={proposalEditing}
      data-prototype-source="d7e2c9b7"
      aria-labelledby="manual-creation-title"
      onKeyDown={trapFocus}
      onCancel={(event) => {
        event.preventDefault();
        finalizeClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) finalizeClose();
      }}
    >
      <section className="manual-window">
        <header className="manual-header">
          <span className="manual-symbol" aria-hidden="true">Q</span>
          <div className="manual-title">
            <small>FOLLOW-UP QUESTION · ManualCreation</small>
            <h2 id="manual-creation-title">创建后续研究问题</h2>
            <p>用于问题树中的后续节点 · 与首次 Quest 创建分开</p>
          </div>
          <div className="manual-header-meta">
            <span className="manual-chip">Quest {view.quest_ref}</span>
            <span className="manual-chip current">{stepLabel}</span>
          </div>
          <button
            ref={closeButtonRef}
            className="manual-close"
            type="button"
            aria-label="关闭创建 Question 窗口"
            onClick={finalizeClose}
            autoFocus
          >
            ×
          </button>
        </header>

        <div className="manual-body">
          {visibleFailure ? (
            <div
              ref={errorRef}
              className="manual-error"
              role="alert"
              tabIndex={-1}
            >
              <b>{visibleFailure.code}</b>
              <span>{visibleFailure.message}</span>
            </div>
          ) : null}
          <div className="manual-layout">
            <main className="manual-form" data-testid="manual-seed-and-proposal">
              <div className="manual-seed-banner">
                <i aria-hidden="true" />
                <div>
                  <b>{seedConfirmed ? "CreationSeed 已冻结" : "现在只收集你的 CreationSeed"}</b>
                  <p>
                    {seedConfirmed
                      ? "Drafting Session 与 Proposal 编辑不会回写这段用户原话。"
                      : "先用自然语言说明你想研究什么；确认前 Agent 不会改写或补全。"}
                  </p>
                </div>
              </div>

              <div className="manual-parent-context">
                <b>新建子问题</b>
                <span>
                  将在“{view.parent_question_title ?? view.parent_question_ref}”下起草；父子关系仍需 RG 最终接纳。
                </span>
                <code>parent · {view.parent_question_ref}</code>
              </div>

              <label className="manual-seed-intent" htmlFor="manual-seed-intent">
                <span className="manual-seed-intent-head">
                  <span className="manual-seed-mark" aria-hidden="true">≈</span>
                  <span><small>必填 · 用你自己的话</small><b>你想研究什么？</b></span>
                </span>
                <textarea
                  ref={seedInputRef}
                  id="manual-seed-intent"
                  required
                  maxLength={seedIntentMaxLength}
                  value={seedDraft}
                  disabled={
                    seedConfirmed || terminal || writesUnavailable || isBusy ||
                    seedConfirmationPending
                  }
                  aria-describedby="manual-seed-help"
                  placeholder="例如：我想知道在显存有限时压缩模型的推理轨迹，会不会让它忘记前面关键的信息……"
                  onChange={(event) => setSeedDraft(event.target.value)}
                />
                <span className="manual-seed-intent-foot" id="manual-seed-help">
                  <span><b>这不是第七个 Formal Question 字段。</b> 空白结构化字段仍是 unprovided。</span>
                  <span>{seedDraft.length} / {seedIntentMaxLength.toLocaleString()}</span>
                </span>
              </label>

              <div className="manual-seed-start-grid">
                <section className="manual-material-card" aria-labelledby="manual-material-title">
                  <div className="manual-card-head">
                    <span aria-hidden="true">⌁</span>
                    <div>
                      <b id="manual-material-title">提供你已有的材料</b>
                      <p>文件、文件夹或本地路径只是待筛选输入，不冒充 RM receipt。</p>
                    </div>
                  </div>
                  <div className="manual-material-actions">
                    <button
                      type="button"
                      disabled={
                        seedConfirmed || terminal || writesUnavailable || isBusy ||
                        seedConfirmationPending
                      }
                      onClick={() => folderInputRef.current?.click()}
                    >
                      选择文件夹
                    </button>
                    <button
                      type="button"
                      disabled={
                        seedConfirmed || terminal || writesUnavailable || isBusy ||
                        seedConfirmationPending
                      }
                      onClick={() => filesInputRef.current?.click()}
                    >
                      选择若干文件
                    </button>
                  </div>
                  <input
                    ref={folderInputRef}
                    className="manual-visually-hidden"
                    type="file"
                    multiple
                    tabIndex={-1}
                    aria-hidden="true"
                    onChange={(event) => selectFiles(event.target.files, "folder")}
                  />
                  <input
                    ref={filesInputRef}
                    id="manual-material-files"
                    className="manual-visually-hidden"
                    type="file"
                    multiple
                    tabIndex={-1}
                    aria-hidden="true"
                    onChange={(event) => selectFiles(event.target.files, "files")}
                  />
                  <label className="manual-local-path">
                    <span>或者填写本地文件夹路径 · 可选</span>
                    <input
                      value={materialDraft.local_path}
                      maxLength={16_000}
                      disabled={
                        seedConfirmed || terminal || writesUnavailable || isBusy ||
                        seedConfirmationPending
                      }
                      placeholder="例如 /data/experiments/run-07"
                      onChange={(event) => updateMaterials({
                        mode: event.target.value.trim() ? "path" : "unprovided",
                        files: [],
                        local_path: event.target.value,
                      })}
                    />
                  </label>
                  <div className="manual-material-status">
                    {seedConfirmed
                      ? acceptedMaterialBindings.length
                        ? `已确认 ${acceptedMaterialBindings.length} 个 material binding · ${acceptedMaterialBindings[0].asset_ref}`
                        : "已确认 Seed 未附 accepted material binding · unprovided"
                      : materialDraft.mode === "folder"
                        ? `已选择文件夹 · ${materialDraft.files.length} 个候选文件`
                        : materialDraft.mode === "files"
                          ? `已选择 ${materialDraft.files.length} 个候选文件`
                          : materialDraft.mode === "path"
                            ? `linked_local 草案 · ${materialDraft.local_path}`
                            : "尚未提供材料 · unprovided"}
                  </div>
                </section>

                <fieldset
                  className="manual-fetch-card"
                  disabled={
                    seedConfirmed || terminal || writesUnavailable || isBusy ||
                    seedConfirmationPending
                  }
                >
                  <legend>还需要系统补充检索吗？</legend>
                  <p>这里先记录 browser draft 偏好；Seed 确认后仍需单独执行 DeepFetch 或确认 waiver。</p>
                  <div className="manual-fetch-options">
                    <button
                      type="button"
                      aria-pressed={researchPreference === "deepfetch"}
                      onClick={() => setResearchPreference("deepfetch")}
                    >
                      <b>使用 DeepFetch</b><small>补充文献检索</small>
                    </button>
                    <button
                      type="button"
                      aria-pressed={researchPreference === "waiver"}
                      onClick={() => setResearchPreference("waiver")}
                    >
                      <b>先不使用</b><small>确认 Seed 后独立 waiver</small>
                    </button>
                    <button
                      type="button"
                      aria-pressed={researchPreference === "later"}
                      onClick={() => setResearchPreference("later")}
                    >
                      <b>稍后决定</b><small>最终 Proposal 前选择</small>
                    </button>
                  </div>
                  <div className="manual-fetch-note">
                    当前：{researchPreference === "deepfetch"
                      ? "倾向使用 DeepFetch · 尚未启动"
                      : researchPreference === "waiver"
                        ? "倾向不使用 · 尚未形成 waiver"
                        : "稍后决定 · 无 DeepFetch / waiver receipt"}
                  </div>
                </fieldset>
              </div>

              <div className="manual-seed-divider">
                <span>{seedConfirmed
                  ? "精确 QuestionProposal · 四项必填，两项可选"
                  : "如果你已经想清楚，也可以提前填写 · 以下六项仍然选填"}</span>
              </div>

              <div className="manual-proposal-source">
                <b>{seedConfirmed ? "可编辑 QuestionProposal" : "Seed 阶段的可选结构化草稿"}</b>
                <span>{proposalDirty ? "本地编辑待同步" : "当前精确 Proposal 已同步"}</span>
                <code>
                  {authoritativeProposal && !proposalDirty
                    ? `${authoritativeProposal.ref} · ${authoritativeProposal.hash.slice(0, 12)}`
                    : "proposal identity · pending"}
                </code>
              </div>

              <div className="manual-proposal-fields" aria-label="Formal Question 六字段 QuestionProposal">
                {proposalFields.map((field) => {
                  const required = seedConfirmed && requiredFields.includes(field.key);
                  const missing = proposalIssue?.key === field.key;
                  const common = {
                    "data-manual-proposal-field": field.key,
                    "aria-label": seedConfirmed ? field.label : field.seedLabel,
                    "aria-required": required,
                    "aria-invalid": seedConfirmed && missing,
                    value: proposalDraft[field.key],
                    maxLength: questionFieldMaxLengths[field.key],
                    disabled:
                      proposalConfirmed || terminal || writesUnavailable ||
                      seedConfirmationPending || proposalConfirmationPending ||
                      busyAction === "confirm",
                    placeholder: field.placeholder,
                    onChange: (
                      event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
                    ) => updateProposal(field.key, event.target.value),
                    onBlur: (
                      event: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement>,
                    ) => {
                      if (
                        event.relatedTarget instanceof Element &&
                        event.relatedTarget.closest("[data-manual-confirm-proposal]")
                      ) {
                        return;
                      }
                      if (
                        seedConfirmed &&
                        researchSatisfied &&
                        questionComplete(proposalDraftRef.current)
                      ) {
                        void persistProposal();
                      }
                    },
                  };
                  return (
                    <label
                      className={`manual-proposal-field${field.full ? " full" : ""}${required ? " required" : ""}`}
                      key={field.key}
                    >
                      <span className="manual-proposal-field-head">
                        <code>{field.key}</code>
                        <span>{required ? "Proposal 必填" : seedConfirmed ? "Proposal 可选" : "Seed 可不填"}</span>
                      </span>
                      {field.rows === 1 ? (
                        <input
                          {...common}
                          ref={(element) => {
                            if (missing) firstMissingFieldRef.current = element;
                          }}
                        />
                      ) : (
                        <textarea
                          {...common}
                          rows={field.rows}
                          ref={(element) => {
                            if (missing) firstMissingFieldRef.current = element;
                          }}
                        />
                      )}
                    </label>
                  );
                })}
              </div>
            </main>

            <aside className="manual-side" aria-label="ManualCreation facts 与 Question Drafting Session">
              <div className="manual-preseed">
                <span className="manual-side-eyebrow">Creation facts · HC</span>
                <h3>先冻结你的起点，<br />再开始共同起草。</h3>
                <div className="manual-state-list">
                  <div className="manual-state">
                    <i>01</i><div><small>CREATION SEED</small><b>{seedDraft.trim() ? "可确认 · browser draft only" : "尚未确认 · 需要自然语言描述"}</b></div>
                  </div>
                  <div className="manual-state">
                    <i>02</i><div><small>DEEPFETCH</small><b>{researchPreference === "later" ? "未决定 · 无 waiver" : `${researchPreference} · preference only`}</b></div>
                  </div>
                  <div className="manual-state">
                    <i>03</i><div><small>QUESTION DRAFTING SESSION</small><b>等待 Seed confirmation receipt</b></div>
                  </div>
                </div>
                <div className="manual-rule">
                  <b>确认 Seed ≠ 创建 Question</b><br />
                  Proposal confirmation、RM 内容接纳、RG identity 与 Stage 推进仍是不同事实。
                </div>
              </div>

              <section
                className="manual-session"
                aria-labelledby="manual-session-title"
                aria-hidden={!seedConfirmed}
                data-testid="manual-drafting-session"
              >
                <header className="manual-session-head">
                  <span className="manual-session-orb" aria-hidden="true" />
                  <div>
                    <small>QUESTION DRAFTING SESSION</small>
                    <b id="manual-session-title">一起整理研究问题</b>
                    <span>{view.drafting_session.session_ref ?? "等待独立 Session identity"}</span>
                  </div>
                </header>

                <section className="manual-research-decision" aria-labelledby="manual-research-title">
                  <div className="manual-research-head">
                    <div><small>RESEARCH GATE · HC</small><b id="manual-research-title">DeepFetch 或独立 waiver</b></div>
                    <span>{researchSatisfied ? "satisfied" : "required"}</span>
                  </div>
                  {deepFetchAccepted ? (
                    <p className="manual-research-ready">✓ {deepFetchCopy(view.research.deepfetch)}</p>
                  ) : waiverAccepted ? (
                    <p className="manual-research-ready">✓ explicit waiver · {receiptCopy(view.research.waiver_receipt)}</p>
                  ) : (
                    <>
                      <p>{deepFetchCopy(view.research.deepfetch)}</p>
                      {view.research.deepfetch?.status === "running" && view.research.deepfetch.total ? (
                        <progress
                          max={view.research.deepfetch.total}
                          value={view.research.deepfetch.processed ?? 0}
                          aria-label="DeepFetch 真实进度"
                        />
                      ) : null}
                      <div className="manual-research-actions">
                        <button
                          ref={researchActionRef}
                          type="button"
                          disabled={
                            !seedConfirmed || terminal || writesUnavailable || isBusy ||
                            proposalConfirmationPending || researchRunning ||
                            researchActionDispatched !== null
                          }
                          onClick={() => void startDeepFetch()}
                        >
                          {view.research.deepfetch && ["failed", "cancelled", "unavailable"].includes(view.research.deepfetch.status)
                            ? "重新启动 DeepFetch"
                            : "开始补充检索"}
                        </button>
                        <button
                          type="button"
                          disabled={
                            !seedConfirmed || terminal || writesUnavailable || isBusy ||
                            proposalConfirmationPending || researchRunning ||
                            researchActionDispatched !== null
                          }
                          onClick={() => void confirmWaiver()}
                        >
                          确认本次不运行 DeepFetch
                        </button>
                      </div>
                      <small>失败、取消、默认项和无响应都不会产生 waiver。</small>
                    </>
                  )}
                </section>

                <div className="manual-session-transcript" aria-live="polite">
                  {view.drafting_session.turns.length ? view.drafting_session.turns.map((turn) => (
                    <article className={`manual-message ${turn.role}`} key={turn.turn_ref}>
                      <small>{turn.role === "user" ? "你" : "Draft Agent"} · {turn.status ?? "completed"}</small>
                      {turn.content}
                    </article>
                  )) : (
                    <p className="manual-session-empty">
                      Session 只消费已确认 Seed；讨论不会改写 Seed、确认 Proposal 或创建 Question。
                    </p>
                  )}
                </div>

                <div className="manual-session-compose">
                  <label htmlFor="manual-session-input">继续讨论</label>
                  <div>
                    <textarea
                      ref={sessionInputRef}
                      id="manual-session-input"
                      aria-label="在 Question Drafting Session 中发消息"
                      value={messageDraft}
                      maxLength={draftingMessageMaxLength}
                      disabled={
                        !sessionReady || terminal || writesUnavailable || isBusy ||
                        proposalConfirmationPending
                      }
                      placeholder="讨论未知、答案形态、适用范围，或询问 DeepFetch 状态……"
                      onChange={(event) => setMessageDraft(event.target.value)}
                    />
                    <button
                      type="button"
                      aria-label="发送消息"
                      disabled={
                        !sessionReady || terminal || writesUnavailable || isBusy ||
                        proposalConfirmationPending || !messageDraft.trim() ||
                        messageDraft.trim().length > draftingMessageMaxLength
                      }
                      onClick={() => void sendMessage()}
                    >
                      ↑
                    </button>
                  </div>
                </div>

                <div className="manual-session-status">
                  <span>{view.drafting_session.status}</span>
                  <span>{view.drafting_session.turns.length} turns</span>
                </div>
                <div className="manual-session-boundary">
                  <b>讨论不会改写已确认的 Seed。</b> 最终仍需确认精确 Proposal identity。
                </div>
                {proposalConfirmed ? (
                  <details className="manual-owner-status">
                    <summary>查看确认与 Owner 接纳状态</summary>
                    <dl>
                      <div><dt>HC Proposal</dt><dd>{receiptCopy(view.receipts.proposal_confirmation)}</dd></div>
                      <div><dt>RM content</dt><dd>{receiptCopy(view.receipts.question_content)}</dd></div>
                      <div><dt>RG identity</dt><dd>{receiptCopy(view.receipts.question_identity)}</dd></div>
                    </dl>
                  </details>
                ) : null}
              </section>
            </aside>
          </div>
        </div>

        <footer className="manual-footer" data-testid="manual-confirmation-footer">
          <span className="manual-footer-note" aria-live="polite">
            {pendingActionLabel ?? footerNote}
          </span>
          <button
            className="quiet"
            type="button"
            disabled={terminal || proposalConfirmationPending || isBusy}
            onClick={() => void cancelCreation()}
          >
            取消创建
          </button>
          {seedConfirmed ? (
            <button
              className="manual-edit-proposal quiet"
              type="button"
              disabled={
                proposalConfirmed || terminal || writesUnavailable || isBusy ||
                proposalConfirmationPending
              }
              onClick={toggleProposalEditor}
            >
              {proposalEditing ? "返回讨论" : "编辑问题草案"}
            </button>
          ) : null}
          {!seedConfirmed ? (
            <button
              className="primary violet"
              type="button"
              disabled={
                !seedDraft.trim() || terminal || writesUnavailable || isBusy ||
                seedDraft.trim().length > seedIntentMaxLength ||
                materialDraft.files.length > maxAcceptedMaterialBindings ||
                contentIssue(proposalDraft, false) !== null ||
                seedConfirmationDispatched
              }
              onClick={() => void confirmSeed()}
            >
              确认当前 Seed，开始讨论
            </button>
          ) : (
            <button
              className="primary confirm"
              type="button"
              data-manual-confirm-proposal
              disabled={!canConfirmProposal}
              onClick={() => void confirmProposal()}
            >
              {proposalConfirmed ? "问题已确认" : "确认最终问题"}
            </button>
          )}
        </footer>
      </section>
    </dialog>
  );
}

export default ManualCreation;
