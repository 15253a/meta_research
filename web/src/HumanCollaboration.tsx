import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import {
  acknowledgeAssetIntake,
  authorizeHumanCommand,
  confirmHumanCommand,
  convertAgentProposalToCommandDraft,
  convertAgentProposalToSoftConstraint,
  deliverPendingHumanRequestResponse,
  createHumanCommand,
  deliverPendingHumanRequestAssetResponse,
  executeHumanCommand,
  hydratePendingHumanRequestRecovery,
  pendingAcceptedHumanRequestAssetRequestRef,
  pendingAssetIntakeJobRef,
  pendingHumanRequestAssetIntakeRequestRef,
  pendingHumanRequestAssetResponse,
  pendingHumanRequestResponse,
  previewHumanCommand,
  ProductError,
  reconcileOrphanedHumanRequestAssetRecovery,
  respondToHumanRequest,
  reviseHumanCommand,
  sendCompanionMessage,
  stagePendingAcceptedHumanRequestAssetResponse,
  submitHumanRequestAssetIntake,
  resumePendingHumanRequestAssetIntake,
  withdrawSoftConstraint,
  type CompanionAgentProposal,
  type CompanionMessage,
  type CompanionSoftConstraint,
  type HumanCollaborationProjection,
  type HumanCapabilityAuthorization,
  type HumanCommand,
  type HumanCommandDraft,
  type HumanCapabilityCommandDraft,
  type HumanRequestImpactPreview,
  type HumanRequestItem,
  type HumanRequestResponseBody,
  type QuestionTreeItem,
  type ResearchControlAction,
  type ResearchControlProjection,
} from "./api";

export type CompanionShellState =
  | "loading"
  | "first-error"
  | "readiness-unavailable"
  | "ready-empty"
  | "ready-active";

const requestCopy: Record<HumanRequestItem["kind"], {
  list: string;
  title: string;
  eyebrow: string;
  draftLabel: string;
  draftPlaceholder: string;
}> = {
  library_reconnect: {
    list: "图书馆访问",
    title: "处理图书馆访问阻塞",
    eyebrow: "需要你恢复访问",
    draftLabel: "就图书馆恢复事项发消息",
    draftPlaceholder: "询问当前状态，或说明你希望怎样处理……",
  },
  external_material_api_access: {
    list: "外部材料或 API",
    title: "完成外部材料或 API 申请",
    eyebrow: "需要你完成外部申请",
    draftLabel: "就外部材料事项发消息",
    draftPlaceholder: "询问为什么需要材料，或讨论替代路线……",
  },
  offline_action: {
    list: "线下操作",
    title: "完成线下操作",
    eyebrow: "需要你完成线下操作",
    draftLabel: "就线下操作事项发消息",
    draftPlaceholder: "询问步骤、安全边界，或说明现场限制……",
  },
  capability_authorization: {
    list: "低频权限",
    title: "决定低频能力授权",
    eyebrow: "需要你决定是否授权",
    draftLabel: "就权限事项发消息",
    draftPlaceholder: "询问风险，或讨论更窄的授权范围……",
  },
};

const fallbackCompanionCopy: Record<CompanionShellState, {
  label: string;
  message: string;
}> = {
  loading: {
    label: "正在建立上下文",
    message: "研究状态返回后，我会在同一个窗口解释正在发生什么。",
  },
  "first-error": {
    label: "本地连接不可用",
    message: "研究状态尚未返回；页面会保持在原处，服务恢复后可以重新读取。",
  },
  "readiness-unavailable": {
    label: "底座尚未就绪",
    message: "研究服务当前不可用。页面保持只读，不会猜测或补写研究状态。",
  },
  "ready-empty": {
    label: "研究空间已就绪",
    message: "这里还没有 Quest。使用左侧 ＋ 后，我会继续留在这个位置。",
  },
  "ready-active": {
    label: "跟随当前研究",
    message: "我会在这里解释研究状态；普通聊天不会直接写入领域事实。",
  },
};

function messageText(message: CompanionMessage): string {
  return message.content ?? message.message ?? message.text ?? "";
}

function documentText(
  value: Record<string, unknown> | undefined,
  ...keys: string[]
): string | undefined {
  for (const key of keys) {
    const candidate = value?.[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate;
  }
  return undefined;
}

function reasonCode(error: unknown): string {
  return error instanceof Error ? error.message : "request_failed";
}

function submitTextareaOnEnter(
  event: ReactKeyboardEvent<HTMLTextAreaElement>,
): void {
  if (
    event.key !== "Enter"
    || event.shiftKey
    || event.nativeEvent.isComposing
  ) return;
  event.preventDefault();
  event.currentTarget.form?.requestSubmit();
}

const COMPANION_MESSAGE_PAGE_SIZE = 50;

type OptimisticCompanionMessage = {
  localRef: number;
  scopeRef: string;
  content: string;
  interactionRef: string | null;
  matchingProjectedMessagesAtSend: number;
  status: "sending" | "queued";
};

function optimisticMessageIsProjected(
  optimistic: OptimisticCompanionMessage,
  messages: readonly CompanionMessage[],
): boolean {
  if (optimistic.interactionRef && messages.some((message) =>
    message.message_ref === `${optimistic.interactionRef}:user`
  )) return true;
  return messages.filter((message) =>
    message.role === "user"
    && message.scope_ref === optimistic.scopeRef
    && messageText(message) === optimistic.content
  ).length > optimistic.matchingProjectedMessagesAtSend;
}

function openRequests(projection?: HumanCollaborationProjection): HumanRequestItem[] {
  return projection?.human_requests.items.filter((item) => item.status === "open") ?? [];
}

function questIdentity(scopeRef: string): string {
  return scopeRef.startsWith("quest:") ? scopeRef.slice("quest:".length) : scopeRef;
}

function scopeLabel(request: HumanRequestItem): string {
  return request.direct_waiters?.some((waiter) => waiter.wait_scope === "quest")
    ? "影响整个研究"
    : "只影响相关任务";
}

function requestStatusLabel(status: HumanRequestItem["status"]): string {
  return {
    open: "待处理",
    satisfied: "已满足",
    unsatisfied: "已回应，未满足",
    declined: "已拒绝",
    withdrawn: "已撤回",
    expired: "已过期",
    superseded: "已有后续事项",
  }[status];
}

export function QuestCompanion({
  state,
  collaboration,
  researchControl,
  questions = [],
  questionContext = null,
  onChanged,
  onOpenRequest,
}: {
  state: CompanionShellState;
  collaboration?: HumanCollaborationProjection;
  researchControl?: ResearchControlProjection;
  questions?: readonly QuestionTreeItem[];
  questionContext?: QuestionTreeItem | null;
  onChanged: () => void;
  onOpenRequest: (requestRef: string) => void;
}) {
  const companion = collaboration?.companion;
  const ready = companion?.status === "ready";
  const canSend = ready && Boolean(companion.scope_ref);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [optimisticMessages, setOptimisticMessages] = useState<
    OptimisticCompanionMessage[]
  >([]);
  const optimisticSequence = useRef(0);
  const [visibleMessageCount, setVisibleMessageCount] = useState(
    COMPANION_MESSAGE_PAGE_SIZE,
  );
  const chatRef = useRef<HTMLDivElement>(null);
  const scopeRef = companion?.scope_ref ?? null;
  const requests = scopeRef
    ? openRequests(collaboration).filter((request) => Boolean(request.quest_ref)
      && questIdentity(request.quest_ref!) === questIdentity(scopeRef))
    : [];
  const attention = requests.find((request) =>
    request.direct_waiters?.some((waiter) => waiter.wait_scope === "quest"),
  ) ?? requests[0];
  const messages = useMemo(() => scopeRef
    ? companion?.messages.filter((item) =>
      item.scope_ref === scopeRef
      && item.view_context?.kind !== "human_request",
    ) ?? []
    : [], [companion?.messages, scopeRef]);
  const hiddenMessageCount = Math.max(0, messages.length - visibleMessageCount);
  const visibleMessages = hiddenMessageCount
    ? messages.slice(-visibleMessageCount)
    : messages;
  const visibleOptimisticMessages = optimisticMessages.filter((message) =>
    message.scopeRef === scopeRef
    && !optimisticMessageIsProjected(message, messages)
  );
  const softConstraints = scopeRef
    ? companion?.soft_constraints.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
  const agentProposals = scopeRef
    ? companion?.agent_proposals.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
  const commands = collaboration?.commands.items.filter((item) =>
    (item.scope_ref === scopeRef || item.scope_ref === "runtime:telemetry")
    && ["capability_authorization", "research_control"].includes(
      item.draft.command_kind,
    )
  ) ?? [];
  const broadAuthorizationHistory = scopeRef
    ? collaboration?.commands.authorizations.filter((item) =>
      item.scope_ref === scopeRef
      && item.authorization_kind === "broad_research"
      && item.is_current !== false,
    ) ?? []
    : [];
  const currentBroadAuthorization = broadAuthorizationHistory.reduce<
    HumanCapabilityAuthorization | null
  >((latest, item) => !latest || item.created_at >= latest.created_at ? item : latest, null);
  const broadResearchAuthorizations = currentBroadAuthorization?.decision === "granted"
    && currentBroadAuthorization.effective_decision !== "revoked"
    ? [currentBroadAuthorization]
    : [];

  useEffect(() => {
    setVisibleMessageCount(COMPANION_MESSAGE_PAGE_SIZE);
  }, [scopeRef]);

  const newestMessageRef = visibleOptimisticMessages.at(-1)?.localRef
    ?? messages.at(-1)?.message_ref
    ?? messages.at(-1)?.created_at
    ?? messages.length;
  useEffect(() => {
    const chat = chatRef.current;
    if (!chat) return;
    chat.scrollTop = chat.scrollHeight;
  }, [newestMessageRef, scopeRef]);
  const optimisticProjectionKey = optimisticMessages.map((message) =>
    `${message.localRef}:${message.interactionRef ?? "pending"}`
  ).join("|");
  useEffect(() => {
    setOptimisticMessages((current) => {
      const remaining = current.filter((message) =>
        !optimisticMessageIsProjected(message, messages)
      );
      return remaining.length === current.length ? current : remaining;
    });
  }, [messages, optimisticProjectionKey]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!canSend || !scopeRef || !message || sending) return;
    const localRef = optimisticSequence.current + 1;
    optimisticSequence.current = localRef;
    const pending: OptimisticCompanionMessage = {
      localRef,
      scopeRef,
      content: message,
      interactionRef: null,
      matchingProjectedMessagesAtSend: messages.filter((item) =>
        item.role === "user"
        && item.scope_ref === scopeRef
        && messageText(item) === message
      ).length,
      status: "sending",
    };
    setDraft("");
    setOptimisticMessages((current) => [...current, pending]);
    setSending(true);
    setError(null);
    try {
      const queued = await sendCompanionMessage(
        message,
        scopeRef,
        questionContext && questionContext.lifecycle_revision !== null ? {
          kind: "question",
          quest_ref: questionContext.quest_ref,
          question_ref: questionContext.question_ref,
          content_ref: questionContext.content_ref,
          content_hash: questionContext.content_hash,
          lifecycle_revision: questionContext.lifecycle_revision,
        } : null,
      );
      setOptimisticMessages((current) => current.map((item) =>
        item.localRef === localRef
          ? {
              ...item,
              interactionRef: typeof queued.interaction_ref === "string"
                ? queued.interaction_ref
                : null,
              status: "queued",
            }
          : item
      ));
      onChanged();
    } catch (caught) {
      setOptimisticMessages((current) => current.filter(
        (item) => item.localRef !== localRef,
      ));
      setDraft((current) => current || message);
      setError(reasonCode(caught));
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      className="lumen-companion"
      aria-label="研究助手"
      data-shell-region="companion"
      tabIndex={0}
    >
      <header className="lumen-companion-head">
        <span className="lumen-orb" aria-hidden="true" />
        <div>
          <b>研究助手</b>
          <small>{questionContext
            ? `正在跟随 ${questionContext.question_ref}`
            : "随时询问正在发生的研究"}</small>
        </div>
        <code>{ready ? "可交流" : "暂不可用"}</code>
      </header>
      <div ref={chatRef} className="lumen-chat" aria-live="polite">
        {questionContext ? (
          <article
            className="lumen-message lumen-question-context"
            data-testid="companion-question-context"
          >
            <small>选中问题 · 只读上下文</small>
            <b>{questionContext.title ?? questionContext.question_ref}</b>
            <p>{questionContext.unknown_statement ?? "这个问题暂时没有更多说明。"}</p>
          </article>
        ) : null}
        {attention ? (
          <article className="lumen-human-attention">
            <small>需要你 · {scopeLabel(attention)}</small>
            <b>{requestCopy[attention.kind].list}需要你处理</b>
            <p>{attention.obligation}</p>
            <button type="button" onClick={() => onOpenRequest(attention.request_ref)}>
              查看请求
            </button>
          </article>
        ) : null}
        {hiddenMessageCount ? (
          <button
            className="lumen-chat-history"
            type="button"
            onClick={() => setVisibleMessageCount((current) => (
              current + COMPANION_MESSAGE_PAGE_SIZE
            ))}
          >
            显示更早的 {Math.min(hiddenMessageCount, COMPANION_MESSAGE_PAGE_SIZE)} 条消息
          </button>
        ) : null}
        {ready && visibleMessages.length ? visibleMessages.map((message, index) => {
          const pending = message.status === "queued"
            || message.status === "processing"
            || message.status === "running";
          return (
            <article
              key={message.message_ref ?? `${message.role}-${index}`}
              className={`lumen-message ${message.role === "user" ? "me" : ""}`}
              data-message-status={message.status}
            >
              <small>
                {message.role === "user"
                  ? "YOU · CONVERSATION"
                  : message.role === "system"
                    ? "SYSTEM · STATUS"
                    : "COMPANION · READ-ONLY EXPLANATION"}
              </small>
              {messageText(message)}
              {pending ? (
                <span className="lumen-message-state" role="status">
                  {message.role === "user" ? "消息正在发送…" : "Codex 正在思考…"}
                </span>
              ) : null}
            </article>
          );
        }) : (
          <article className="lumen-message">
            <small>{fallbackCompanionCopy[state].label}</small>
            {fallbackCompanionCopy[state].message}
          </article>
        )}
        {visibleOptimisticMessages.map((message) => (
          <article
            key={`optimistic-${message.localRef}`}
            className="lumen-message me"
            data-message-status={message.status}
          >
            <small>YOU · CONVERSATION</small>
            {message.content}
            <span className="lumen-message-state">
              {message.status === "sending" ? "消息正在发送…" : "消息已发送"}
            </span>
          </article>
        ))}
        {visibleOptimisticMessages.length ? (
          <article
            className="lumen-message lumen-companion-thinking"
            data-message-status="processing"
          >
            <small>COMPANION · THINKING</small>
            <span className="lumen-message-state" role="status">Codex 正在思考…</span>
          </article>
        ) : null}
        {ready ? softConstraints.map((constraint, index) => (
          <SoftConstraintCard
            key={constraint.constraint_ref ?? `constraint-${index}`}
            constraint={constraint}
            onChanged={onChanged}
          />
        )) : null}
        {ready ? agentProposals.map((proposal, index) => (
          <AgentProposalCard
            key={proposal.proposal_ref ?? `proposal-${index}`}
            proposal={proposal}
            onChanged={onChanged}
          />
        )) : null}
        {ready ? broadResearchAuthorizations.map((authorization) => (
          <BroadResearchAuthorizationCard
            key={authorization.authorization_ref}
            authorization={authorization}
            onChanged={onChanged}
          />
        )) : null}
        {ready ? commands.map((command) => (
          <HumanCommandCard
            key={command.intent_id}
            command={command}
            authorization={collaboration?.commands.authorizations.find((item) =>
              item.confirmation_receipt_ref
                === command.confirmation_receipt?.receipt_ref,
            ) ?? command.authorization}
            onChanged={onChanged}
          />
        )) : null}
        {ready && researchControl?.status === "ready" && researchControl.foreground ? (
          <ResearchControlComposer
            control={researchControl}
            questions={questions}
            scopeRef={scopeRef}
            onChanged={onChanged}
          />
        ) : null}
        {!ready ? (
          <article className="lumen-proposal">
            <small>当前边界 · 无写入</small>
            <b>对话能力尚未启用</b>
            <p>这个固定位置不会被 capability list、Owner revision 或 receipt rail 取代。</p>
          </article>
        ) : null}
      </div>
      <form className="lumen-compose" onSubmit={(event) => void submit(event)}>
        <div>
          <textarea
            aria-label="给研究助手发消息"
            disabled={!canSend || sending}
            rows={1}
            placeholder={canSend
              ? "问为什么，或提出一个软约束……"
              : ready
                ? "创建 Quest 后开始对话"
                : "研究助手尚未启用"}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={submitTextareaOnEnter}
          />
          <button
            type="submit"
            disabled={!canSend || sending || !draft.trim()}
            aria-label="发送消息"
          >
            ↑
          </button>
        </div>
        <small>{error ? `发送失败 · ${error}` : "Enter 发送 · Shift+Enter 换行"}</small>
      </form>
    </aside>
  );
}

export function TelemetryAuthorizationCard({
  collaboration,
  onChanged,
}: {
  collaboration?: HumanCollaborationProjection;
  onChanged: () => void;
}) {
  const authorization = collaboration?.commands.authorizations
    .filter((item) =>
      item.scope_ref === "runtime:telemetry"
      && item.capability === "opentelemetry_export"
      && item.is_current !== false,
    )
    .reduce<HumanCapabilityAuthorization | null>(
      (latest, item) => !latest || item.created_at >= latest.created_at ? item : latest,
      null,
    ) ?? null;
  const requirementScope = isRecord(authorization?.requirement.scope)
    ? authorization.requirement.scope
    : null;
  const authorizedEndpoint = typeof requirementScope?.endpoint === "string"
    ? requirementScope.endpoint
    : "";
  const active = authorization?.decision === "granted"
    && authorization.effective_decision !== "revoked";
  const [endpoint, setEndpoint] = useState(authorizedEndpoint);
  const [pending, setPending] = useState(false);
  const [created, setCreated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setEndpoint(authorizedEndpoint);
    setCreated(false);
  }, [authorization?.receipt_ref, authorizedEndpoint]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const exactEndpoint = (active ? authorizedEndpoint : endpoint).trim();
    if (pending || !exactEndpoint) return;
    setPending(true);
    setError(null);
    try {
      await createHumanCommand("runtime:telemetry", {
        command_kind: "capability_authorization",
        payload: {
          capability: "opentelemetry_export",
          decision: active ? "revoked" : "granted",
          scope: {
            schema_ref: "meta-research/opentelemetry-export-scope/v1",
            provider: "otlp_http",
            endpoint: exactEndpoint,
            credential_ref: null,
          },
        },
      });
      setCreated(true);
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };

  return (
    <article className="lumen-command lumen-telemetry-authorization">
      <small>LOCAL OBSERVABILITY · EXPLICIT OPT-IN</small>
      <b>OpenTelemetry export · {active ? "authorized" : "local-only"}</b>
      <p>本地 durable facts 始终保留；远端只接收脱敏 allow-list event。</p>
      <form onSubmit={(event) => void submit(event)}>
        <label>
          OTLP/HTTP endpoint
          <input
            aria-label="OpenTelemetry OTLP HTTP endpoint"
            type="url"
            required
            disabled={active || pending || created}
            placeholder="http://127.0.0.1:4318/v1/logs"
            value={active ? authorizedEndpoint : endpoint}
            onChange={(event) => setEndpoint(event.target.value)}
          />
        </label>
        <button
          type="submit"
          disabled={pending || created || !(active ? authorizedEndpoint : endpoint).trim()}
        >
          {created
            ? "Command Draft 已建立"
            : active
              ? "建立 revoke Command Draft"
              : "建立 opt-in Command Draft"}
        </button>
      </form>
      <span>草案仍须经过 current Impact Preview、human confirmation 与独立 authorization。</span>
      {error ? <small role="alert">{error}</small> : null}
    </article>
  );
}

const researchControlLabels: Record<ResearchControlAction, string> = {
  pause: "暂停当前 Quest",
  resume: "恢复当前 Quest",
  normal_switch: "安全切换",
  forced_switch: "强制切换",
  cancel: "取消当前 Cycle",
  abandon: "放弃当前 Cycle",
  prune: "剪裁 Question",
  restore: "恢复 Question",
};

type ForegroundResearchControlShortcutProps = {
  control?: ResearchControlProjection;
  commands?: readonly HumanCommand[];
  disabled?: boolean;
  onChanged: () => void;
};

const researchControlRepreviewErrors = new Set([
  "research_control_repreview_required",
  "foreground_control_repreview_required",
  "runtime_control_repreview_required",
  "runtime_control_reservation_stale",
  "command_preview_stale",
  "command_draft_stale",
  "command_preview_current_required",
]);

function foregroundControlIdentity(
  foreground: NonNullable<ResearchControlProjection["foreground"]> | null,
): string | null {
  return foreground
    ? [
        foreground.quest_ref,
        foreground.cycle_ref,
        foreground.question_ref,
        foreground.epoch,
      ].join(":")
    : null;
}

function commandControlAction(
  command: HumanCommand | null,
): Extract<ResearchControlAction, "pause" | "resume"> | null {
  if (command?.draft.command_kind !== "research_control") return null;
  const action = command.draft.payload.action;
  return action === "pause" || action === "resume" ? action : null;
}

function commandMatchesForeground(
  command: HumanCommand,
  foreground: NonNullable<ResearchControlProjection["foreground"]>,
  action: Extract<ResearchControlAction, "pause" | "resume">,
): boolean {
  if (
    command.executed
    || !command.confirmation_receipt
    || command.draft.command_kind !== "research_control"
    || command.draft.payload.action !== action
  ) return false;
  const target = command.draft.payload.target;
  return target.target_scope === "cycle"
    && target.quest_ref === foreground.quest_ref
    && target.cycle_ref === foreground.cycle_ref
    && target.question_ref === foreground.question_ref
    && target.epoch === foreground.epoch;
}

export function ForegroundResearchControlShortcut({
  control,
  commands = [],
  disabled = false,
  onChanged,
}: ForegroundResearchControlShortcutProps) {
  const foreground = control?.status === "ready" ? control.foreground : null;
  const action: Extract<ResearchControlAction, "pause" | "resume"> | null =
    foreground?.status === "active"
      ? "pause"
      : foreground?.status === "suspended"
        ? "resume"
        : null;
  const [command, setCommand] = useState<HumanCommand | null>(null);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState<"preview" | "execute" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [previewInvalidated, setPreviewInvalidated] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dismissedCommandRefs = useRef(new Set<string>());
  const foregroundIdentity = foregroundControlIdentity(foreground);
  const foregroundIdentityRef = useRef(foregroundIdentity);
  foregroundIdentityRef.current = foregroundIdentity;

  const commandAction = commandControlAction(command);
  const exactCommandTarget = Boolean(
    command
    && commandAction
    && foreground
    && command.draft.command_kind === "research_control"
    && command.draft.payload.target.target_scope === "cycle"
    && command.draft.payload.target.quest_ref === foreground.quest_ref
    && command.draft.payload.target.cycle_ref === foreground.cycle_ref
    && command.draft.payload.target.question_ref === foreground.question_ref
    && command.draft.payload.target.epoch === foreground.epoch,
  );
  const confirmedExecutionPending = Boolean(
    commandAction && command?.confirmation_receipt && !command.executed && exactCommandTarget,
  );
  const commandApplied = Boolean(
    commandAction
    && exactCommandTarget
    && foreground
    && (commandAction === "pause"
      ? foreground.status === "suspended"
      : foreground.status === "active"),
  );
  const executionAwaitingProjection = Boolean(
    commandAction && command?.executed && exactCommandTarget && !commandApplied,
  );
  const projectedConfirmedCommand = foreground && action
    ? commands.find((item) =>
        !dismissedCommandRefs.current.has(item.intent_id)
        && commandMatchesForeground(item, foreground, action)
      ) ?? null
    : null;

  useEffect(() => {
    if (!projectedConfirmedCommand) return;
    if (command && (
      command.intent_id !== projectedConfirmedCommand.intent_id
      || command.confirmation_receipt
    )) return;
    setCommand(projectedConfirmedCommand);
    setError(null);
    setPreviewInvalidated(false);
  }, [command, projectedConfirmedCommand]);

  useEffect(() => {
    if (!commandAction || !foreground) return;
    if (exactCommandTarget && !commandApplied) return;
    setCommand(null);
    setOpen(false);
    setError(null);
    setPreviewInvalidated(false);
  }, [commandAction, commandApplied, exactCommandTarget, foregroundIdentity]);

  if (!foreground || !action || !control?.actions.includes(action)) return null;

  const assertCurrentForeground = (expectedIdentity: string | null) => {
    if (!expectedIdentity || foregroundIdentityRef.current !== expectedIdentity) {
      throw new ProductError("foreground_control_changed");
    }
  };

  const close = () => {
    if (pending) return;
    setOpen(false);
    setError(null);
    if (!command?.confirmation_receipt) {
      setCommand(null);
      setPreviewInvalidated(false);
    }
    requestAnimationFrame(() => buttonRef.current?.focus({ preventScroll: true }));
  };

  const preview = async () => {
    if (pending || executionAwaitingProjection) return;
    if (confirmedExecutionPending) {
      setOpen(true);
      return;
    }
    const expectedIdentity = foregroundIdentity;
    setPending("preview");
    setError(null);
    let current: HumanCommand | null = null;
    try {
      current = await createHumanCommand(`quest:${foreground.quest_ref}`, {
        command_kind: "research_control",
        payload: {
          action,
          target: {
            quest_ref: foreground.quest_ref,
            cycle_ref: foreground.cycle_ref,
            question_ref: foreground.question_ref,
            epoch: foreground.epoch,
            target_scope: "cycle",
          },
          reason: "operator_requested",
        },
      });
      assertCurrentForeground(expectedIdentity);
      setCommand(current);
      setPreviewInvalidated(false);
      setOpen(true);
      current = await previewHumanCommand(current);
      assertCurrentForeground(expectedIdentity);
      setCommand(current);
      setPreviewInvalidated(false);
      onChanged();
    } catch (caught) {
      const code = reasonCode(caught);
      if (code === "foreground_control_changed") {
        setCommand(null);
        setOpen(false);
      } else {
        setCommand(current);
        setOpen(Boolean(current));
      }
      setError(code);
      if (current) onChanged();
    } finally {
      setPending(null);
    }
  };

  const retryPreview = async () => {
    if (!command || pending || command.confirmation_receipt) return;
    const expectedIdentity = foregroundIdentity;
    setPending("preview");
    setError(null);
    try {
      const previewed = await previewHumanCommand(command);
      assertCurrentForeground(expectedIdentity);
      setCommand(previewed);
      setPreviewInvalidated(false);
      onChanged();
    } catch (caught) {
      const code = reasonCode(caught);
      if (code === "foreground_control_changed") {
        setCommand(null);
        setOpen(false);
      }
      setError(code);
      onChanged();
    } finally {
      setPending(null);
    }
  };

  const confirmAndExecute = async () => {
    if (!command || pending) return;
    const expectedIdentity = foregroundIdentity;
    setPending("execute");
    setError(null);
    let executable = command;
    try {
      assertCurrentForeground(expectedIdentity);
      if (!executable.confirmation_receipt) {
        if (
          previewInvalidated
          || executable.impact_preview?.status !== "current"
          || executable.impact_preview.draft_revision !== executable.draft_revision
          || executable.impact_preview.draft_hash !== executable.draft_hash
        ) {
          throw new ProductError("command_preview_current_required");
        }
        executable = await confirmHumanCommand(executable);
        assertCurrentForeground(expectedIdentity);
        setCommand(executable);
        onChanged();
      }
      const executed = await executeHumanCommand(executable);
      setCommand(executed);
      setOpen(false);
      setError(null);
      onChanged();
      requestAnimationFrame(() => buttonRef.current?.focus({ preventScroll: true }));
    } catch (caught) {
      const code = reasonCode(caught);
      // A successful confirmation is durable even when execution times out.
      // Retain that exact command so the user retries execution without
      // rebuilding a draft or issuing a second confirmation.
      setCommand(executable);
      setError(code);
      if (researchControlRepreviewErrors.has(code)) {
        setPreviewInvalidated(true);
      }
      onChanged();
    } finally {
      setPending(null);
    }
  };

  const resetForFreshPreview = () => {
    if (pending) return;
    if (command) dismissedCommandRefs.current.add(command.intent_id);
    setCommand(null);
    setOpen(false);
    setError(null);
    setPreviewInvalidated(false);
    onChanged();
    requestAnimationFrame(() => buttonRef.current?.focus({ preventScroll: true }));
  };

  const effectiveAction = confirmedExecutionPending || executionAwaitingProjection
    ? commandAction
    : action;
  const actionLabel = effectiveAction === "resume" ? "继续研究" : "暂停研究";
  const busyLabel = pending === "preview"
    ? "正在核对…"
    : pending === "execute"
      ? effectiveAction === "pause" ? "正在暂停…" : "正在继续…"
      : executionAwaitingProjection
        ? "正在同步状态…"
      : confirmedExecutionPending
        ? effectiveAction === "pause" ? "完成暂停" : "完成继续"
        : actionLabel;
  const unavailable = disabled
    || pending !== null
    || executionAwaitingProjection
    || Boolean(foreground.pending_operation_ref && !confirmedExecutionPending);

  return (
    <div className="lumen-research-power">
      <button
        ref={buttonRef}
        type="button"
        data-action={effectiveAction}
        disabled={unavailable}
        aria-label={busyLabel}
        title={`${actionLabel}当前 Cycle；当前界面与 Web 服务保持在线`}
        onClick={() => void preview()}
      >
        <span aria-hidden="true">{effectiveAction === "resume" ? "▶" : "Ⅱ"}</span>
        <b>{busyLabel}</b>
      </button>
      {error && !open ? (
        <small className="lumen-research-power-error" role="alert">
          控制未完成 · {error}
        </small>
      ) : null}
      {open && command && commandAction ? (
        <ForegroundResearchControlDialog
          command={command}
          action={commandAction}
          pending={pending}
          error={error}
          previewInvalidated={previewInvalidated}
          onClose={close}
          onRetryPreview={() => void retryPreview()}
          onConfirm={() => void confirmAndExecute()}
          onReset={resetForFreshPreview}
        />
      ) : null}
    </div>
  );
}

function ForegroundResearchControlDialog({
  command,
  action,
  pending,
  error,
  previewInvalidated,
  onClose,
  onRetryPreview,
  onConfirm,
  onReset,
}: {
  command: HumanCommand;
  action: Extract<ResearchControlAction, "pause" | "resume">;
  pending: "preview" | "execute" | null;
  error: string | null;
  previewInvalidated: boolean;
  onClose: () => void;
  onRetryPreview: () => void;
  onConfirm: () => void;
  onReset: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const preview = command.impact_preview;
  const currentPreview = preview?.status === "current"
    && preview.draft_revision === command.draft_revision
    && preview.draft_hash === command.draft_hash
    && !previewInvalidated
    ? preview
    : null;
  const confirmed = Boolean(command.confirmation_receipt);
  const visiblePreview = confirmed ? preview : currentPreview;
  const executionRequiresFreshPreview = Boolean(
    confirmed && (previewInvalidated
      || Boolean(error && researchControlRepreviewErrors.has(error))),
  );
  const verb = action === "pause" ? "暂停" : "继续";

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    requestAnimationFrame(() => closeRef.current?.focus({ preventScroll: true }));
    return () => dialog.close();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className="lumen-research-power-dialog"
      aria-labelledby="research-power-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section>
        <header>
          <div>
            <small>OWNER IMPACT PREVIEW</small>
            <h2 id="research-power-title">{verb}当前研究？</h2>
            <p>
              {action === "pause"
                ? "系统会等待当前 Provider 到达安全边界；当前界面与 Web 服务保持在线。"
                : "系统会从已记录的安全边界继续当前 Cycle。"}
            </p>
          </div>
          <button
            ref={closeRef}
            type="button"
            aria-label="关闭研究控制确认"
            disabled={pending !== null}
            onClick={onClose}
          >
            ×
          </button>
        </header>
        <div className="lumen-research-power-preview">
          <p className="lumen-research-power-scope">
            Quest {command.draft.command_kind === "research_control"
              ? command.draft.payload.target.quest_ref
              : "unknown"}
            {command.draft.command_kind === "research_control"
              ? ` · Cycle ${command.draft.payload.target.cycle_ref} · Epoch ${command.draft.payload.target.epoch}`
              : ""}
          </p>
          {visiblePreview ? visiblePreview.owner_previews.map((owner) => (
            <article key={owner.digest}>
              <small>{owner.source_owner}</small>
              <code>{JSON.stringify(owner.target_assertion)}</code>
              <ResearchControlPreviewList label="会发生" items={owner.will_happen} />
              <ResearchControlPreviewList label="不会发生" items={owner.will_not_happen} />
              <ResearchControlPreviewList
                label="风险 / 陈旧条件"
                items={[...owner.risks, ...owner.stale_conditions]}
              />
            </article>
          )) : (
            <p className="lumen-research-power-loading" role="status">
              {pending === "preview" ? "正在生成精确影响预览…" : "影响预览尚未生成。"}
            </p>
          )}
          {error ? (
            <p className="lumen-research-power-dialog-error" role="alert">
              {confirmed
                ? executionRequiresFreshPreview
                  ? `当前控制绑定已变化，原确认不会继续执行 · ${error}`
                  : `Human Confirmation 已记录，执行尚未完成 · ${error}`
                : `控制尚未确认，也不会执行 · ${error}`}
            </p>
          ) : null}
        </div>
        <footer>
          <button type="button" disabled={pending !== null} onClick={onClose}>暂不操作</button>
          {executionRequiresFreshPreview ? (
            <button type="button" disabled={pending !== null} onClick={onReset}>
              重新读取当前状态
            </button>
          ) : !currentPreview && !confirmed ? (
            <button type="button" disabled={pending !== null} onClick={onRetryPreview}>
              {pending === "preview" ? "正在生成预览…" : "重新生成预览"}
            </button>
          ) : (
            <button
              type="button"
              disabled={pending !== null || (!confirmed && !currentPreview)}
              onClick={onConfirm}
            >
              {pending === "execute"
                ? action === "pause" ? "正在等待安全边界…" : "正在继续…"
                : confirmed
                  ? `重试执行${verb}`
                  : `确认并${verb}研究`}
            </button>
          )}
        </footer>
      </section>
    </dialog>
  );
}

function ResearchControlPreviewList({
  label,
  items,
}: {
  label: string;
  items: readonly string[];
}) {
  return (
    <div>
      <b>{label}</b>
      {items.length ? <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul> : <p>无</p>}
    </div>
  );
}

const questionTargetActions = new Set<ResearchControlAction>([
  "normal_switch",
  "forced_switch",
  "prune",
  "restore",
]);

type ResearchControlTargetScope = "cycle" | "stage" | "run";

const scopedControlActions = new Set<ResearchControlAction>([
  "pause",
  "resume",
  "cancel",
]);

function ResearchControlComposer({
  control,
  questions,
  scopeRef,
  onChanged,
}: {
  control: ResearchControlProjection;
  questions: readonly QuestionTreeItem[];
  scopeRef: string | null;
  onChanged: () => void;
}) {
  const foreground = control.foreground!;
  const initialAction: ResearchControlAction = foreground.status === "suspended"
    ? "resume"
    : "pause";
  const [action, setAction] = useState<ResearchControlAction>(initialAction);
  const [targetScope, setTargetScope] = useState<ResearchControlTargetScope>("cycle");
  const [targetQuestionRef, setTargetQuestionRef] = useState(
    questions.find((item) => item.question_ref !== foreground.question_ref
      && item.lifecycle_status === "active")?.question_ref
      ?? foreground.question_ref,
  );
  const [runRef, setRunRef] = useState(control.managed_runs.find((item) =>
    !["completed", "terminated"].includes(item.status))?.run_ref ?? "");
  const [pruneRecordRef, setPruneRecordRef] = useState(
    control.recovery_records[0]?.prune_record_ref ?? "",
  );
  const [reason, setReason] = useState("operator_requested");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const availableActions = control.actions.length
    ? control.actions
    : (Object.keys(researchControlLabels) as ResearchControlAction[]);
  const activeQuestions = questions.filter((item) => item.lifecycle_status === "active");
  const selectableQuestions = action === "normal_switch" || action === "forced_switch"
    ? activeQuestions.filter((item) => item.question_ref !== foreground.question_ref)
    : activeQuestions;
  const managedRuns = control.managed_runs.filter((item) =>
    !["completed", "terminated"].includes(item.status));
  const selectedPruneRecord = control.recovery_records.find((item) =>
    item.prune_record_ref === pruneRecordRef) ?? null;
  const effectiveScope = scopedControlActions.has(action) ? targetScope : "cycle";
  const missingTarget = effectiveScope === "run" && !runRef
    || action === "restore" && !selectedPruneRecord
    || questionTargetActions.has(action)
      && action !== "restore"
      && !selectableQuestions.some((item) => item.question_ref === targetQuestionRef);

  useEffect(() => {
    if (!scopedControlActions.has(action)) setTargetScope("cycle");
    if (action === "restore" && !selectedPruneRecord) {
      setPruneRecordRef(control.recovery_records[0]?.prune_record_ref ?? "");
    }
    if (questionTargetActions.has(action)
      && action !== "restore"
      && !selectableQuestions.some((item) => item.question_ref === targetQuestionRef)) {
      setTargetQuestionRef(selectableQuestions[0]?.question_ref ?? "");
    }
    if (effectiveScope === "run" && !managedRuns.some((item) => item.run_ref === runRef)) {
      setRunRef(managedRuns[0]?.run_ref ?? "");
    }
  }, [action, control.recovery_records, effectiveScope, managedRuns,
    runRef, selectableQuestions, selectedPruneRecord, targetQuestionRef]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (pending || !scopeRef || !reason.trim() || missingTarget) return;
    setPending(true);
    setError(null);
    try {
      const target = {
        quest_ref: foreground.quest_ref,
        cycle_ref: foreground.cycle_ref,
        question_ref: foreground.question_ref,
        epoch: foreground.epoch,
        target_scope: effectiveScope,
        ...(effectiveScope === "run" ? { run_ref: runRef } : {}),
        ...(action === "restore" && selectedPruneRecord
          ? {
            target_question_ref: selectedPruneRecord.root_question_ref,
            prune_record_ref: selectedPruneRecord.prune_record_ref,
          }
          : questionTargetActions.has(action)
          ? { target_question_ref: targetQuestionRef }
          : {}),
      };
      await createHumanCommand(`quest:${foreground.quest_ref}`, {
        command_kind: "research_control",
        payload: { action, target, reason: reason.trim() },
      });
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  return (
    <section className="lumen-research-control" aria-label="研究控制">
      <details>
        <summary>
          <span>研究控制</span>
          <small>按需建立精确 Command Draft</small>
        </summary>
        <div className="lumen-research-control-body">
          <small>FOREGROUND CONTROL · PREVIEW FIRST</small>
          <b>{researchControlLabels[action]}</b>
          <p>
            Epoch {foreground.epoch} · {foreground.stage} · {foreground.status}
            {control.managed_runs.length
              ? ` · ${control.managed_runs.length} managed Run`
              : " · 当前无在途 Run"}
          </p>
          <form onSubmit={(event) => void submit(event)}>
            <label>
              控制动作
              <select
                aria-label="控制动作"
                value={action}
                onChange={(event) => setAction(event.target.value as ResearchControlAction)}
              >
                {availableActions.map((item) => (
                  <option value={item} key={item}>{researchControlLabels[item]}</option>
                ))}
              </select>
            </label>
            {scopedControlActions.has(action) ? (
              <label>
                控制范围
                <select
                  aria-label="控制范围"
                  value={targetScope}
                  onChange={(event) => setTargetScope(
                    event.target.value as ResearchControlTargetScope,
                  )}
                >
                  <option value="cycle">当前 Cycle</option>
                  <option value="stage">当前 Stage</option>
                  <option value="run">一个 managed Run</option>
                </select>
              </label>
            ) : null}
            {effectiveScope === "run" ? (
              <label>
                Managed Run
                <select
                  aria-label="Managed Run"
                  value={runRef}
                  onChange={(event) => setRunRef(event.target.value)}
                >
                  {managedRuns.map((item) => (
                    <option value={item.run_ref} key={item.run_ref}>
                      {item.run_kind} · {item.status} · {item.run_ref}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {questionTargetActions.has(action) && action !== "restore" ? (
              <label>
                目标 Question
                <select
                  aria-label="目标 Question"
                  value={targetQuestionRef}
                  onChange={(event) => setTargetQuestionRef(event.target.value)}
                >
                  {selectableQuestions.map((item) => (
                    <option value={item.question_ref} key={item.question_ref}>
                      {item.title ?? item.question_ref}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            {action === "restore" ? (
              <label>
                恢复记录
                <select
                  aria-label="恢复记录"
                  value={pruneRecordRef}
                  onChange={(event) => setPruneRecordRef(event.target.value)}
                >
                  {control.recovery_records.map((item) => (
                    <option value={item.prune_record_ref} key={item.prune_record_ref}>
                      {item.root_question_ref} · {item.affected_question_count} Questions
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <label>
              操作理由
              <input
                aria-label="操作理由"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </label>
            <button type="submit" disabled={pending || !reason.trim() || missingTarget}>
              查看操作草案
            </button>
          </form>
          <span>不会直接写 Owner；先生成精确草案，再检查 Impact Preview。</span>
          {action === "restore" && !selectedPruneRecord ? (
            <em role="status">没有可恢复的 PruneRecord。</em>
          ) : null}
          {error ? <em role="status">草案失败 · {error}</em> : null}
        </div>
      </details>
    </section>
  );
}

function BroadResearchAuthorizationCard({
  authorization,
  onChanged,
}: {
  authorization: HumanCapabilityAuthorization;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [created, setCreated] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const questRef = authorization.quest_ref
    ?? questIdentity(authorization.scope_ref);
  const createRevokeDraft = async () => {
    if (pending || created) return;
    setPending(true);
    setError(null);
    try {
      await createHumanCommand(authorization.scope_ref, {
        command_kind: "capability_authorization",
        payload: {
          capability: "broad_research",
          decision: "revoked",
          scope: {
            quest_ref: questRef,
          },
        },
      });
      setCreated(true);
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  return (
    <article className="lumen-command lumen-broad-authorization">
      <small>BROAD RESEARCH AUTHORIZATION · CURRENT GRANT</small>
      <b>ordinary reversible local research · granted</b>
      <p>Quest · {questRef}</p>
      <code>receipt · {authorization.receipt_ref}</code>
      <p>撤销必须先建立精确 Command Draft，再经过 Owner Impact Preview、human confirmation 与独立 authorization。</p>
      <button type="button" disabled={pending || created} onClick={() => void createRevokeDraft()}>
        {created ? "revoke Command Draft 已建立" : "建立 revoke Command Draft"}
      </button>
      {error ? <small role="alert">{error}</small> : null}
    </article>
  );
}

function HumanCommandCard({
  command,
  authorization: projectedAuthorization,
  onChanged,
}: {
  command: HumanCommand;
  authorization?: HumanCapabilityAuthorization | null;
  onChanged: () => void;
}) {
  const capabilityCommand = command.draft.command_kind === "capability_authorization"
    ? command.draft
    : null;
  const controlCommand = command.draft.command_kind === "research_control"
    ? command.draft
    : null;
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewedPreviewRef, setReviewedPreviewRef] = useState<string | null>(null);
  const previewDetailsRef = useRef<HTMLDetailsElement | null>(null);
  const [recordedAuthorization, setRecordedAuthorization] = useState<HumanCapabilityAuthorization | null>(null);
  const [capabilityDraft, setCapabilityDraft] = useState(
    capabilityCommand?.payload.capability ?? "",
  );
  const [decisionDraft, setDecisionDraft] = useState<
    HumanCapabilityCommandDraft["payload"]["decision"]
  >(capabilityCommand?.payload.decision ?? "denied");
  const [scopeDraft, setScopeDraft] = useState(() => JSON.stringify(
    capabilityCommand?.payload.scope ?? {},
    null,
    2,
  ));

  useEffect(() => {
    if (command.draft.command_kind === "capability_authorization") {
      setCapabilityDraft(command.draft.payload.capability);
      setDecisionDraft(command.draft.payload.decision);
      setScopeDraft(JSON.stringify(command.draft.payload.scope, null, 2));
    }
    setReviewedPreviewRef(null);
  }, [command.draft, command.draft_hash, command.draft_revision]);

  const act = async (operation: () => Promise<HumanCommand>) => {
    setPending(true);
    setError(null);
    try {
      await operation();
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  const preview = command.impact_preview;
  const currentPreview = preview?.status === "current"
    && preview.draft_revision === command.draft_revision
    && preview.draft_hash === command.draft_hash;
  useEffect(() => {
    if (currentPreview && preview && previewDetailsRef.current?.open) {
      setReviewedPreviewRef(preview.preview_ref);
    }
  }, [currentPreview, preview]);
  const authorization = capabilityCommand
    ? projectedAuthorization ?? recordedAuthorization
    : null;
  const authorize = async () => {
    setPending(true);
    setError(null);
    try {
      const recorded = await authorizeHumanCommand(command);
      setRecordedAuthorization(recorded);
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  const revise = async (event: FormEvent) => {
    event.preventDefault();
    if (!capabilityCommand) {
      setError("capability_authorization_command_required");
      return;
    }
    let scope: unknown;
    try {
      scope = JSON.parse(scopeDraft);
    } catch {
      setError("command_scope_json_object_required");
      return;
    }
    if (!isRecord(scope) || !capabilityDraft.trim()) {
      setError("command_scope_json_object_required");
      return;
    }
    await act(() => reviseHumanCommand(command, {
      command_kind: "capability_authorization",
      payload: {
        capability: capabilityDraft.trim(),
        decision: decisionDraft,
        scope,
      },
    }));
  };
  return (
    <article className="lumen-command" data-command-status={command.status}>
      <small>COMMAND DRAFT · NO AUTHORITY</small>
      {controlCommand ? (
        <>
          <b>{researchControlLabels[controlCommand.payload.action]}</b>
          <p>
            Quest {controlCommand.payload.target.quest_ref} · Cycle {controlCommand.payload.target.cycle_ref}
            {` · Epoch ${controlCommand.payload.target.epoch}`}
          </p>
          <code>{stringify(controlCommand.payload)}</code>
        </>
      ) : capabilityCommand ? (
        <>
          <b>{capabilityCommand.payload.decision} · {capabilityCommand.payload.capability}</b>
          <p>{stringify(capabilityCommand.payload.scope)}</p>
        </>
      ) : null}
      {command.source_proposal_ref ? (
        <code className="lumen-source-proposal">source · {command.source_proposal_ref}</code>
      ) : null}
      <details ref={previewDetailsRef} onToggle={(event) => {
        if (event.currentTarget.open && currentPreview && preview) {
          setReviewedPreviewRef(preview.preview_ref);
        }
      }}>
        <summary>查看精确草案与 Owner Impact Preview</summary>
        <dl>
          <Detail label="Intent" value={command.intent_id} />
          <Detail label="Current draft" value={`r${command.draft_revision} · ${command.draft_hash}`} />
          <Detail label="Executed" value={String(command.executed)} />
        </dl>
        {preview ? (
          <div className="lumen-owner-previews">
            {preview.owner_previews.map((owner) => (
              <section key={owner.digest}>
                <small>{owner.source_owner} · TARGET ASSERTION</small>
                <code>{stringify(owner.target_assertion)}</code>
                <b>会发生</b><p>{owner.will_happen.join("；")}</p>
                <b>不会发生</b><p>{owner.will_not_happen.join("；")}</p>
                <b>风险 / 陈旧条件</b><p>{[...owner.risks, ...owner.stale_conditions].join("；")}</p>
              </section>
            ))}
          </div>
        ) : null}
      </details>
      {!command.confirmation_receipt && capabilityCommand ? (
        <details className="lumen-command-revision">
          <summary>修订精确 Command Draft</summary>
          <form onSubmit={(event) => void revise(event)}>
            <label>
              Capability
              <input
                aria-label={`Command ${command.intent_id} capability`}
                value={capabilityDraft}
                onChange={(event) => setCapabilityDraft(event.target.value)}
              />
            </label>
            <label>
              Decision
              <select
                aria-label={`Command ${command.intent_id} decision`}
                value={decisionDraft}
                onChange={(event) => setDecisionDraft(
                  event.target.value as HumanCapabilityCommandDraft["payload"]["decision"],
                )}
              >
                <option value="granted">granted</option>
                <option value="denied">denied</option>
                <option value="revoked">revoked</option>
              </select>
            </label>
            <label>
              Exact scope · JSON object
              <textarea
                aria-label={`Command ${command.intent_id} exact scope`}
                value={scopeDraft}
                onChange={(event) => setScopeDraft(event.target.value)}
              />
            </label>
            <button type="submit" disabled={pending || !capabilityDraft.trim()}>
              保存修订并使旧 Preview 失效
            </button>
          </form>
        </details>
      ) : null}
      {!command.confirmation_receipt && !currentPreview ? (
        <button type="button" disabled={pending} onClick={() => void act(() => previewHumanCommand(command))}>
          生成 Owner Impact Preview
        </button>
      ) : null}
      {!command.confirmation_receipt && currentPreview ? (
        <button
          type="button"
          disabled={pending || reviewedPreviewRef !== preview?.preview_ref}
          onClick={() => void act(() => confirmHumanCommand(command))}
        >
          {reviewedPreviewRef === preview?.preview_ref
            ? "确认当前草案与预览"
            : "先展开并检查 Owner Impact Preview"}
        </button>
      ) : null}
      {command.confirmation_receipt ? (
        <div className="lumen-confirmed">
          <b>Human Confirmation 已记录</b>
          <span>{command.confirmation_receipt.receipt_ref}</span>
          <p>{controlCommand
            ? command.executed
              ? "确认与 Owner execution receipts 均已持久化。"
              : "确认已记录，控制尚未执行。"
            : "确认没有执行命令，也没有签发 Capability Authorization。"}</p>
        </div>
      ) : null}
      {command.confirmation_receipt && controlCommand && !command.executed ? (
        <button
          type="button"
          disabled={pending}
          onClick={() => void act(() => executeHumanCommand(command))}
        >
          执行已确认控制
        </button>
      ) : null}
      {command.control_execution ? (
        <div className="lumen-authorized lumen-control-executed">
          <b>CONTROL EXECUTION · {command.control_execution.status.toUpperCase()}</b>
          <span>{command.control_execution.receipt_ref}</span>
        </div>
      ) : null}
      {command.confirmation_receipt && capabilityCommand && !authorization ? (
        <button type="button" disabled={pending} onClick={() => void authorize()}>
          签发独立 Capability Authorization
        </button>
      ) : null}
      {authorization ? (
        <div className="lumen-authorized">
          <b>Capability Authorization · {authorization.decision}</b>
          <span>{authorization.receipt_ref}</span>
        </div>
      ) : null}
      {error ? <em role="status">控制失败 · {error}</em> : null}
    </article>
  );
}

function SoftConstraintCard({
  constraint,
  onChanged,
}: {
  constraint: CompanionSoftConstraint;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const withdraw = async () => {
    setPending(true);
    setError(null);
    try {
      await withdrawSoftConstraint(constraint);
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  return (
    <article className={`lumen-constraint ${constraint.status}`}>
      <small>SOFT CONSTRAINT · {constraint.status.toUpperCase()}</small>
      <p>{constraint.text ?? constraint.content ?? documentText(constraint.guidance, "text")}</p>
      <span>可撤回的指导，不是执行授权</span>
      {constraint.source_proposal_ref ? (
        <code className="lumen-source-proposal">source · {constraint.source_proposal_ref}</code>
      ) : null}
      {constraint.status === "active" && constraint.constraint_ref && constraint.revision !== undefined ? (
        <button type="button" disabled={pending} onClick={() => void withdraw()}>撤回软约束</button>
      ) : null}
      {error ? <em role="status">撤回失败 · {error}</em> : null}
    </article>
  );
}

function AgentProposalCard({
  proposal,
  onChanged,
}: {
  proposal: CompanionAgentProposal;
  onChanged: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [convertedTo, setConvertedTo] = useState<"soft_constraint" | "command_draft" | null>(null);
  const proposalKind = proposal.kind
    ?? documentText(proposal.proposal, "proposal_kind", "kind");
  const proposedGuidance = documentText(proposal.proposal, "text");
  const proposedCommand = commandDraftFromProposal(proposal.proposal);
  const acceptGuidance = async () => {
    if (!proposal.scope_ref || !proposal.proposal_ref || !proposal.proposal_hash || !proposedGuidance) return;
    setPending(true);
    setError(null);
    try {
      await convertAgentProposalToSoftConstraint(proposal);
      setConvertedTo("soft_constraint");
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  const createCommandDraft = async () => {
    if (!proposal.scope_ref || !proposal.proposal_ref || !proposal.proposal_hash || !proposedCommand) return;
    setPending(true);
    setError(null);
    try {
      await convertAgentProposalToCommandDraft(proposal);
      setConvertedTo("command_draft");
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };
  return (
    <article className="lumen-proposal">
      <small>{proposalKind === "command_draft" ? "COMMAND DRAFT · NO AUTHORITY" : "AGENT PROPOSAL"}</small>
      <b>{proposal.title ?? documentText(proposal.proposal, "title") ?? "Agent 建议"}</b>
      <p>{proposal.summary ?? proposal.content ?? documentText(proposal.proposal, "summary", "description", "text")}</p>
      {proposal.impact_preview ? <ImpactPreview preview={proposal.impact_preview} /> : null}
      {convertedTo ? (
        <span role="status">
          已原子转换为 {convertedTo === "command_draft" ? "Command Draft" : "Soft Constraint"}；原 Proposal 不可重复转换。
        </span>
      ) : null}
      {!convertedTo && proposal.status === "proposed" && proposalKind === "command_draft" && proposal.scope_ref && proposal.proposal_ref && proposal.proposal_hash && proposedCommand ? (
        <button type="button" disabled={pending} onClick={() => void createCommandDraft()}>
          建立精确 Command Draft
        </button>
      ) : null}
      {!convertedTo && proposal.status === "proposed" && proposalKind !== "command_draft" && proposal.scope_ref && proposal.proposal_ref && proposal.proposal_hash && proposedGuidance ? (
        <button type="button" disabled={pending} onClick={() => void acceptGuidance()}>
          明确接受为软约束
        </button>
      ) : null}
      {proposal.status === "proposed" && !proposal.proposal_hash ? (
        <em role="status">当前 Proposal 缺少 exact hash，已停止转换。</em>
      ) : null}
      {error ? <em role="status">记录失败 · {error}</em> : null}
    </article>
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function commandDraftFromProposal(
  proposal?: Record<string, unknown>,
): HumanCommandDraft | null {
  const command = proposal?.command;
  if (!isRecord(command) || command.command_kind !== "capability_authorization") {
    return null;
  }
  const payload = command.payload;
  if (!isRecord(payload)
    || typeof payload.capability !== "string"
    || !["granted", "denied", "revoked"].includes(String(payload.decision))
    || !isRecord(payload.scope)) return null;
  return {
    command_kind: command.command_kind,
    payload: {
      capability: payload.capability,
      decision: payload.decision as HumanCapabilityCommandDraft["payload"]["decision"],
      scope: payload.scope,
    },
  };
}

function ImpactPreview({ preview }: { preview: HumanRequestImpactPreview }) {
  const row = (label: string, values?: string[]) => values?.length ? (
    <div><dt>{label}</dt><dd>{values.join("；")}</dd></div>
  ) : null;
  return (
    <details className="hc-impact-preview">
      <summary>查看影响说明</summary>
      <dl>
        {row("会改变", preview.will_change)}
        {row("不会改变", preview.will_not_change)}
        {row("风险", preview.risks)}
        {row("陈旧条件", preview.stale_conditions)}
      </dl>
    </details>
  );
}

export function HumanRequestSurface({
  open,
  blocking,
  selectedRef,
  collaboration,
  onSelect,
  onBeforeOpen,
  onClose,
  onChanged,
}: {
  open: boolean;
  blocking: boolean;
  selectedRef: string | null;
  collaboration?: HumanCollaborationProjection;
  onSelect: (requestRef: string | null) => void;
  onBeforeOpen: () => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const items = collaboration?.human_requests.items ?? [];
  const currentItems = items.filter((item) => item.status === "open");
  const requestedSelected = items.find(
    (item) => item.request_ref === selectedRef,
  ) ?? null;
  const selected = blocking
    ? currentItems.find((item) => item.request_ref === selectedRef)
      ?? currentItems[0]
      ?? null
    : requestedSelected;
  const queuePosition = selected
    ? currentItems.findIndex((item) => item.request_ref === selected.request_ref) + 1
    : 0;
  const previousRequest = queuePosition > 1
    ? currentItems[queuePosition - 2] ?? null
    : null;
  const nextRequest = queuePosition > 0 && queuePosition < currentItems.length
    ? currentItems[queuePosition] ?? null
    : null;
  const dialogRef = useRef<HTMLDialogElement>(null);
  const onCloseRef = useRef(onClose);
  const [orphanRecoveryAttempt, setOrphanRecoveryAttempt] = useState(0);
  const currentRequestRefs = currentItems.map((item) => item.request_ref);
  const currentRequestRefsKey = currentRequestRefs.join("\n");

  useEffect(() => {
    if (collaboration?.human_requests.status !== "ready") return;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    void reconcileOrphanedHumanRequestAssetRecovery(currentRequestRefs)
      .then((changed) => {
        if (changed) onChanged();
      })
      .catch(() => {
        if (orphanRecoveryAttempt >= 4) return;
        retryTimer = setTimeout(() => {
          setOrphanRecoveryAttempt((attempt) => attempt + 1);
        }, Math.min(250 * 2 ** orphanRecoveryAttempt, 2_000));
      });
    return () => {
      if (retryTimer !== null) clearTimeout(retryTimer);
    };
  }, [collaboration?.human_requests.status, currentRequestRefsKey, orphanRecoveryAttempt]);

  useEffect(() => {
    setOrphanRecoveryAttempt(0);
  }, [currentRequestRefsKey]);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    onBeforeOpen();
    if (!dialog.open) dialog.showModal();
    return () => {
      if (dialog.open) dialog.close();
    };
  }, [onBeforeOpen, open]);

  useEffect(() => {
    if (!open) return;
    const backgrounds = Array.from(
      document.querySelectorAll<HTMLElement>(
        "[data-hc-background]:not([data-hc-inert-owner])",
      ),
    ).map((element) => ({ element, inert: element.inert }));
    const previousOverflow = document.body.style.overflow;
    backgrounds.forEach(({ element }) => {
      element.inert = true;
    });
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      if (blocking) {
        event.stopImmediatePropagation();
        return;
      }
      onCloseRef.current();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      backgrounds.forEach(({ element, inert }) => {
        element.inert = inert;
      });
      document.body.style.overflow = previousOverflow;
    };
  }, [blocking, open]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      const dialog = dialogRef.current;
      const target = blocking
        ? dialog?.querySelector<HTMLElement>(
          "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), "
          + "select:not([disabled]), summary, a[href]",
        )
        : dialog?.querySelector<HTMLElement>(".hc-close");
      (target ?? dialog)?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [blocking, open, selectedRef]);

  const trapDialogFocus = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), textarea:not([disabled]), "
      + "select:not([disabled]), summary, a[href], [tabindex]:not([tabindex='-1'])",
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
    if (event.shiftKey && (active === dialog || active === first || !dialog.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  if (!open) return null;

  return (
    <dialog
      ref={dialogRef}
      className="hc-backdrop"
      data-open="true"
      data-blocking={blocking ? "true" : "false"}
      aria-label="需要你处理的事项"
      tabIndex={-1}
      onCancel={(event) => {
        event.preventDefault();
        if (!blocking) onClose();
      }}
      onKeyDown={trapDialogFocus}
      onMouseDown={(event) => {
        if (!blocking && event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className={`hc-dialog ${selected?.kind === "library_reconnect"
          ? "hc-dialog-library"
          : ""}`}
        data-blocking={blocking ? "true" : "false"}
      >
        {selected ? (
          <HumanRequestView
            key={selected.request_ref}
            request={selected}
            collaboration={collaboration}
            onBack={() => onSelect(null)}
            onClose={onClose}
            onChanged={onChanged}
            blocking={blocking}
            queuePosition={queuePosition}
            queueSize={currentItems.length}
            onPrevious={() => {
              if (previousRequest) onSelect(previousRequest.request_ref);
            }}
            onNext={() => {
              if (nextRequest) onSelect(nextRequest.request_ref);
            }}
            hasPrevious={previousRequest !== null}
            hasNext={nextRequest !== null}
          />
        ) : (
          <HumanRequestList
            items={items}
            onSelect={onSelect}
            onClose={onClose}
            blocking={blocking}
          />
        )}
      </section>
    </dialog>
  );
}

function HumanRequestList({
  items,
  onSelect,
  onClose,
  blocking,
}: {
  items: HumanRequestItem[];
  onSelect: (requestRef: string) => void;
  onClose: () => void;
  blocking: boolean;
}) {
  const orderedKinds = Object.keys(requestCopy) as HumanRequestItem["kind"][];
  return (
    <>
      <header className="hc-head">
        <span className="hc-symbol" aria-hidden="true">!</span>
        <div>
          <small>{blocking ? "当前需要你处理" : "处理记录"}</small>
          <h2>需要你处理的事项</h2>
          <p>这里列出需要你决定或完成的事情；只有条件满足且其他阻碍已解除，相关任务才会继续。</p>
        </div>
        {!blocking ? (
          <button type="button" className="hc-close" onClick={onClose} aria-label="关闭需要你处理的事项">×</button>
        ) : null}
      </header>
      <main className="hc-list">
        {items.length ? orderedKinds.map((kind) => {
          const sameKind = items.filter((item) => item.kind === kind);
          if (!sameKind.length) return null;
          return (
            <section className="hc-kind-group" key={kind}>
              <header><b>{requestCopy[kind].list}</b><small>{sameKind.length} 项</small></header>
              {sameKind.map((item) => (
                <button
                  type="button"
                  className="hc-request-card"
                  key={item.request_ref}
                  onClick={() => onSelect(item.request_ref)}
                  aria-label={`${requestCopy[kind].list} · ${item.obligation}`}
                >
                  <span><b>{item.obligation}</b><small>{scopeLabel(item)}</small></span>
                  <i className={item.status === "open" ? "current" : ""}>{requestStatusLabel(item.status)}</i>
                </button>
              ))}
            </section>
          );
        }) : (
          <section className="hc-list-empty">
            <b>当前没有需要你处理的事项</b>
            <p>研究过程中的等待或错误不会自动变成你的待办。</p>
          </section>
        )}
      </main>
    </>
  );
}

function HumanRequestView({
  request,
  collaboration,
  onBack,
  onClose,
  onChanged,
  blocking,
  queuePosition,
  queueSize,
  onPrevious,
  onNext,
  hasPrevious,
  hasNext,
}: {
  request: HumanRequestItem;
  collaboration?: HumanCollaborationProjection;
  onBack: () => void;
  onClose: () => void;
  onChanged: () => void;
  blocking: boolean;
  queuePosition: number;
  queueSize: number;
  onPrevious: () => void;
  onNext: () => void;
  hasPrevious: boolean;
  hasNext: boolean;
}) {
  const copy = requestCopy[request.kind];
  const waiter = request.direct_waiters?.find((item) => item.status === "blocked")
    ?? request.direct_waiters?.[0];
  const waiting = collaboration?.human_requests.waiting;
  const safeWork = waiter?.wait_scope === "quest"
    ? waiting?.safe_meaningful_runnable_exists ?? false
    : true;

  return (
    <>
      <header className="hc-head">
        <span className="hc-symbol" aria-hidden="true">↻</span>
        <div>
          <small>
            {blocking
              ? `当前待办 · ${queuePosition} / ${queueSize} · ${copy.eyebrow}`
              : `${copy.eyebrow} · ${scopeLabel(request)}`}
          </small>
          <h2>{copy.title}</h2>
          <p>{request.obligation}</p>
        </div>
        {blocking ? (
          <nav className="hc-queue-nav" aria-label="当前待办队列">
            <button
              type="button"
              disabled={!hasPrevious}
              onClick={onPrevious}
              aria-label="查看上一个待办"
            >
              ← 上一个
            </button>
            <span><b>{queuePosition}</b> / {queueSize}</span>
            <button
              type="button"
              disabled={!hasNext}
              onClick={onNext}
              aria-label="查看下一个待办"
            >
              下一个 →
            </button>
          </nav>
        ) : (
          <div className="hc-head-actions">
            <button type="button" onClick={onBack}>返回请求列表</button>
            <button type="button" className="hc-close" onClick={onClose} aria-label="关闭需要你处理的事项">×</button>
          </div>
        )}
      </header>
      <div className="hc-request-workspace">
        <main className="hc-request-core">
          {request.kind !== "library_reconnect" ? (
            <section className={`hc-waiting ${safeWork ? "local" : "quest"}`}>
              <small>对研究的影响</small>
              <b>{safeWork ? "只等待直接依赖，其他工作继续" : "当前没有安全且有意义的工作可继续"}</b>
              <p>
                {safeWork
                  ? "这件事只暂停与它直接相关的任务，其他研究仍可继续。"
                  : "提交回应后，系统会核对当前任务和所需材料；只有核对通过且没有其他阻碍，相关工作才会继续。"}
              </p>
            </section>
          ) : null}
          <RequestForm
            request={request}
            commands={collaboration?.commands.items ?? []}
            authorizations={collaboration?.commands.authorizations ?? []}
            onChanged={onChanged}
          />
          <RequestDetails request={request} otherBlockers={waiting?.other_blockers ?? []} />
        </main>
        <IntentDraftingSession
          request={request}
          scopeRef={collaboration?.companion.scope_ref ?? null}
          messages={collaboration?.companion.messages ?? []}
          onChanged={onChanged}
        />
      </div>
    </>
  );
}

function RequestDetails({
  request,
  otherBlockers,
}: {
  request: HumanRequestItem;
  otherBlockers: string[];
}) {
  const evaluation = request.evaluation?.decision;
  const disposition = request.disposition?.decision;
  const openEffect = request.open_effect ?? undefined;
  const operation = openEffect?.operation_binding;
  const taskYield = openEffect?.task_yield ?? openEffect?.yield ?? undefined;
  const openWaiter = openEffect?.waiter_ref
    ?? documentText(taskYield, "waiter_ref");
  const taskYieldStatus = documentText(taskYield, "status", "state");
  const taskYieldTask = documentText(taskYield, "task_ref", "run_ref");
  const taskYieldSession = documentText(
    taskYield,
    "session_ref",
    "root_session_ref",
  );
  const responseReceipts = request.responses?.map((response) => {
    const receipt = isRecord(response.receipt) ? response.receipt : undefined;
    return firstDefined(response, "receipt_ref", "response_receipt_ref")
      ?? firstDefined(receipt ?? {}, "receipt_ref");
  }).filter((value): value is string => typeof value === "string") ?? [];
  const rejectionReceipts = request.response_rejections?.map((rejection) => {
    const receipt = isRecord(rejection.receipt) ? rejection.receipt : undefined;
    return firstDefined(rejection, "receipt_ref")
      ?? firstDefined(receipt ?? {}, "receipt_ref");
  }).filter((value): value is string => typeof value === "string") ?? [];
  const evaluationReceipt = isRecord(request.evaluation?.receipt)
    ? firstDefined(request.evaluation.receipt, "receipt_ref")
    : firstDefined(request.evaluation ?? {}, "receipt_ref", "evaluation_receipt_ref");
  const dispositionReceipt = isRecord(request.disposition?.receipt)
    ? firstDefined(request.disposition.receipt, "receipt_ref")
    : firstDefined(request.disposition ?? {}, "receipt_ref", "disposition_receipt_ref");
  return (
    <details className="hc-request-details">
      <summary>查看请求身份、验收与恢复票据</summary>
      <dl>
        <Detail label="Request identity" value={`${request.request_ref} · current revision ${request.revision}`} />
        <Detail
          label="Issuer / request owner"
          value={`${request.issuer} · ${operation?.request_owner ?? request.issuer}`}
        />
        <Detail label="Open effect" value={openEffect?.effect_id} />
        <Detail
          label="Operation"
          value={operation
            ? `${operation.operation_id} · ${operation.task_ref}`
            : undefined}
        />
        <Detail
          label="Attempt / generation"
          value={operation
            ? `${operation.attempt_ref} · generation ${operation.generation}`
            : undefined}
        />
        <Detail
          label="Quest / Root Session"
          value={operation
            ? `${operation.quest_ref ?? "none"} · ${operation.root_session_ref}`
            : undefined}
        />
        <Detail label="Open waiter" value={openWaiter} />
        <Detail label="Open receipt" value={openEffect?.receipt.receipt_ref} />
        <Detail
          label="Task yield"
          value={taskYieldStatus || taskYieldTask || taskYieldSession
            ? [taskYieldStatus, taskYieldTask, taskYieldSession]
              .filter((item): item is string => Boolean(item))
              .join(" · ")
            : undefined}
        />
        <Detail label="Predecessor request" value={request.predecessor_request_ref ?? undefined} />
        <Detail label="Successor request" value={request.successor_request_ref ?? undefined} />
        <Detail label="Business purpose" value={request.business_purpose} />
        <Detail label="TargetAssertion" value={stringify(request.target_assertion)} />
        <Detail label="Acceptance" value={request.acceptance_conditions?.join("；")} />
        <Detail label="Required authorization" value={stringify(request.required_authorization)} />
        <Detail label="Human responses" value={String(request.responses?.length ?? 0)} />
        <Detail label="Response receipts" value={responseReceipts.join("；") || "none"} />
        <Detail label="Rejected response receipts" value={rejectionReceipts.join("；") || "none"} />
        <Detail label="Owner Evaluation" value={typeof evaluation === "string" ? evaluation : "pending"} />
        <Detail label="Evaluation receipt" value={typeof evaluationReceipt === "string" ? evaluationReceipt : undefined} />
        <Detail label="Disposition" value={typeof disposition === "string" ? disposition : "pending"} />
        <Detail label="Disposition receipt" value={typeof dispositionReceipt === "string" ? dispositionReceipt : undefined} />
        <Detail label="Other blockers" value={otherBlockers.join("；") || "none"} />
      </dl>
      {request.direct_waiters?.map((waiter) => {
        const validation = waiter.resume_validation;
        const consumption = validation?.consumption;
        return (
          <section className="hc-waiter-receipts" key={waiter.waiter_ref}>
            <b>Waiter · {waiter.waiter_ref}</b>
            <dl>
              <Detail label="Waiter state" value={`generation ${waiter.generation ?? "?"} · ${waiter.status ?? "unknown"} · ${waiter.wait_scope}`} />
              <Detail label="Persisted blockers" value={waiter.other_blockers?.join("；") || "none"} />
              <Detail label="Resume Validation" value={validation
                ? `${validation.validation_ref} · ${validation.status}`
                : "not recorded"} />
              <Detail label="Validation target" value={validation?.target_assertion_hash} />
              <Detail label="Authorization receipt" value={validation?.authorization_receipt_ref ?? undefined} />
              <Detail label="Validation reason" value={validation?.reason?.code ?? (validation ? "none" : undefined)} />
              <Detail label="Work consumption" value={consumption
                ? `${consumption.consumption_ref} · ${consumption.work_ref}`
                : validation?.started_work ? "receipt unavailable" : "not consumed"} />
              <Detail label="Consumption binding" value={consumption
                ? `${consumption.validation_ref} · ${consumption.work_hash}`
                : undefined} />
              <Detail label="Consumption receipt" value={consumption?.receipt.receipt_ref} />
            </dl>
          </section>
        );
      })}
      {request.impact_preview ? <ImpactPreview preview={request.impact_preview} /> : null}
      <p>HumanRequestResponse、Owner Evaluation、Disposition 与 waiter Resume Validation 是四个不同事实。</p>
    </details>
  );
}

function Detail({ label, value }: { label: string; value?: string }) {
  return <div><dt>{label}</dt><dd>{value || "unprovided"}</dd></div>;
}

function stringify(value: unknown): string {
  if (value === undefined || value === null) return "unprovided";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function firstDefined(
  value: Record<string, unknown>,
  ...keys: string[]
): unknown {
  for (const key of keys) {
    if (value[key] !== undefined && value[key] !== null) return value[key];
  }
  return undefined;
}

function IntentDraftingSession({
  request,
  scopeRef,
  messages,
  onChanged,
}: {
  request: HumanRequestItem;
  scopeRef: string | null;
  messages: CompanionMessage[];
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const questRef = typeof request.quest_ref === "string" && request.quest_ref
    ? request.quest_ref
    : null;
  const scoped = messages.filter((message) =>
    message.scope_ref === scopeRef
    && message.view_context?.kind === "human_request"
    && message.view_context.quest_ref === questRef
    && message.view_context.request_ref === request.request_ref
    && message.view_context.revision === request.revision,
  );

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending || !scopeRef || !questRef) return;
    setSending(true);
    setError(null);
    try {
      await sendCompanionMessage(message, scopeRef, {
        kind: "human_request",
        quest_ref: questRef,
        request_ref: request.request_ref,
        revision: request.revision,
      });
      setDraft("");
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setSending(false);
    }
  };

  return (
    <aside className="hc-request-draft" aria-label={`${requestCopy[request.kind].list}相关交流`}>
      <header>
        <span className="hc-draft-orb" aria-hidden="true" />
        <div><small>询问与协商</small><b>和研究助手聊一聊</b><span>只讨论当前事项</span></div>
      </header>
      <div className="hc-draft-transcript" aria-live="polite">
        {!scoped.length ? (
          <article>
            <small>当前事项</small>
            <p>{request.obligation} 你可以询问状态、验收边界或讨论替代路线。</p>
          </article>
        ) : scoped.map((message, index) => (
          <article className={message.role === "user" ? "me" : ""} key={message.message_ref ?? index}>
            <small>{message.role === "user" ? "你" : "研究助手"}</small>
            <p>{messageText(message)}</p>
          </article>
        ))}
      </div>
      <form className="hc-draft-compose" onSubmit={(event) => void submit(event)}>
        <label>
          继续交流
          <span>
            <textarea
              aria-label={requestCopy[request.kind].draftLabel}
              placeholder={requestCopy[request.kind].draftPlaceholder}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={submitTextareaOnEnter}
              disabled={sending || !scopeRef || !questRef}
            />
            <button
              type="submit"
              disabled={sending || !scopeRef || !questRef || !draft.trim()}
              aria-label="发送消息"
            >↑</button>
          </span>
        </label>
        <small>{error ? `发送失败 · ${error}` : `${draft.length} 字 · Enter 发送 · Shift+Enter 换行`}</small>
      </form>
      <div className="hc-draft-status">这里的交流不会提交回应，也不会改变当前事项的处理状态。</div>
      <div className="hc-draft-boundary">聊天可以帮助理解情况；只有左侧明确提交，才会记录你的回应。</div>
    </aside>
  );
}

type SubmitResponse = (
  facts?: Record<string, unknown>,
  decision?: "provided" | "declined" | "deferred",
  material?: HumanRequestMaterial,
) => Promise<void>;

type HumanRequestMaterial = {
  file: File | null;
  localPath: string;
  factPrefix: "material" | "result";
  evidenceKind: "library_fulltext" | "external_approval" | "offline_result";
};

const MAX_HUMAN_REQUEST_ASSET_BYTES = 64 * 1024 * 1024;

async function acceptHumanRequestMaterial(
  requestRef: string,
  material: HumanRequestMaterial,
  response: HumanRequestResponseBody,
): Promise<boolean> {
  const localPath = material.localPath.trim();
  if (!material.file && !localPath) return false;
  if (pendingAssetIntakeJobRef()) {
    throw new ProductError("asset_intake_recovery_required");
  }
  if (material.file && material.file.size > MAX_HUMAN_REQUEST_ASSET_BYTES) {
    throw new ProductError("asset_content_too_large");
  }
  const displayName = material.file?.name
    ?? localPath.split(/[\\/]/).filter(Boolean).at(-1)
    ?? `${material.evidenceKind}-material`;
  const result = await submitHumanRequestAssetIntake(
    requestRef,
    {
      source_kind: material.file ? "file" : "local_path",
      custody_mode: material.file ? "managed" : "linked_local",
      display_name: displayName,
      media_type: material.file?.type || "application/octet-stream",
      ...(material.file
        ? { content_base64: arrayBufferToBase64(await material.file.arrayBuffer()) }
        : { source_locator: localPath }),
      provenance: {
        submitted_via: "human_request_response",
        human_request_ref: requestRef,
        evidence_kind: material.evidenceKind,
      },
      asynchronous: false,
    },
    response,
    material.factPrefix,
  );
  if (result.status === "failed") {
    acknowledgeAssetIntake(result.job_ref);
    throw new ProductError(result.failure?.code ?? "asset_intake_failed");
  }
  if (result.status !== "accepted" || !result.asset) {
    throw new ProductError("asset_intake_not_terminal");
  }
  return true;
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function RequestForm({ request, commands, authorizations, onChanged }: {
  request: HumanRequestItem;
  commands: HumanCommand[];
  authorizations: HumanCapabilityAuthorization[];
  onChanged: () => void;
}) {
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [recorded, setRecorded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentRequestRef = useRef(request.request_ref);
  const successRef = useRef<HTMLElement>(null);
  const evaluationDecision = typeof request.evaluation?.decision === "string"
    ? request.evaluation.decision
    : null;

  useEffect(() => {
    setNote("");
    setPending(false);
    setRecorded(false);
    setError(null);
  }, [request.request_ref, evaluationDecision]);

  currentRequestRef.current = request.request_ref;

  const markResponseRecorded = () => {
    if (currentRequestRef.current !== request.request_ref) return;
    setRecorded(true);
    onChanged();
  };

  useEffect(() => {
    if (!recorded) return;
    const frame = window.requestAnimationFrame(() => {
      successRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [recorded]);

  const submit: SubmitResponse = async (
    facts = {},
    decision = "provided",
    material,
  ) => {
    if (pending || recorded) return;
    setPending(true);
    setError(null);
    try {
      await hydratePendingHumanRequestRecovery();
      const response: HumanRequestResponseBody = {
        decision,
        facts,
        note,
      };
      const pendingResponse = pendingHumanRequestResponse(request.request_ref);
      if (pendingResponse) {
        if (pendingResponse.request_ref !== request.request_ref) {
          throw new ProductError("human_request_response_recovery_conflict");
        }
        await deliverPendingHumanRequestResponse(request.request_ref);
        markResponseRecorded();
        return;
      }
      const pendingDelivery = pendingHumanRequestAssetResponse(request.request_ref);
      if (pendingDelivery) {
        if (pendingDelivery.request_ref !== request.request_ref) {
          throw new ProductError("human_request_asset_response_recovery_conflict");
        }
        await deliverPendingHumanRequestAssetResponse(request.request_ref);
        markResponseRecorded();
        return;
      }
      const pendingIntakeRequestRef = pendingHumanRequestAssetIntakeRequestRef(
        request.request_ref,
      );
      if (pendingIntakeRequestRef) {
        if (pendingIntakeRequestRef !== request.request_ref) {
          throw new ProductError("human_request_asset_intake_recovery_conflict");
        }
        await resumePendingHumanRequestAssetIntake(request.request_ref);
        await deliverPendingHumanRequestAssetResponse(request.request_ref);
        markResponseRecorded();
        return;
      }
      const pendingAcceptedRequestRef = pendingAcceptedHumanRequestAssetRequestRef(
        request.request_ref,
      );
      if (pendingAcceptedRequestRef) {
        if (pendingAcceptedRequestRef !== request.request_ref) {
          throw new ProductError("human_request_accepted_asset_recovery_conflict");
        }
        await stagePendingAcceptedHumanRequestAssetResponse(request.request_ref, response);
        await deliverPendingHumanRequestAssetResponse(request.request_ref);
        markResponseRecorded();
        return;
      }
      const acceptedMaterial = material
        ? await acceptHumanRequestMaterial(request.request_ref, material, response)
        : false;
      if (acceptedMaterial) {
        await deliverPendingHumanRequestAssetResponse(request.request_ref);
      } else {
        await respondToHumanRequest(request.request_ref, response);
      }
      markResponseRecorded();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setPending(false);
    }
  };

  if (request.status !== "open") {
    return (
      <div className="hc-response-boundary" role="status">
        <b>这件事已处理 · {requestStatusLabel(request.status)}</b><br />
        处理记录可在详情中查看；如果出现后续待办，可在那里补充。
      </div>
    );
  }

  const awaitingOwner = recorded
    || ((request.responses?.length ?? 0) > 0 && evaluationDecision !== "needs_input");
  if (awaitingOwner) {
    return (
      <section
        ref={successRef}
        className="hc-submission-success"
        role="status"
        tabIndex={-1}
      >
        <span aria-hidden="true">✓</span>
        <div>
          <h3>回应已提交</h3>
          <p>系统正在核对回应；表单已关闭。只有条件满足且其他阻碍已解除，相关任务才会继续。</p>
        </div>
      </section>
    );
  }

  return (
    <>
      {request.kind === "library_reconnect" ? (
        <LibraryForm request={request} note={note} setNote={setNote} submit={submit} disabled={pending} />
      ) : null}
      {request.kind === "external_material_api_access" ? (
        <ExternalForm note={note} setNote={setNote} submit={submit} disabled={pending} />
      ) : null}
      {request.kind === "offline_action" ? (
        <OfflineForm request={request} note={note} setNote={setNote} submit={submit} disabled={pending} />
      ) : null}
      {request.kind === "capability_authorization" ? (
        <PermissionForm
          request={request}
          commands={commands}
          authorizations={authorizations}
          note={note}
          setNote={setNote}
          submit={submit}
          disabled={pending}
          onChanged={onChanged}
        />
      ) : null}
      {error || request.kind !== "library_reconnect" ? (
      <div className="hc-response-boundary" role="status">
        {error ? (
          <><b>回应没有记录</b><br />{error}</>
        ) : (
          <><b>提交回应不会立即代表条件已经满足</b><br />系统会核对当前任务、材料和授权；只有核对通过且没有其他阻碍，相关工作才会继续。</>
        )}
      </div>
      ) : null}
      {request.kind !== "library_reconnect" ? <button
        type="button"
        className="hc-defer"
        disabled={pending}
        onClick={() => void submit({}, "deferred")}
      >
        稍后处理
      </button> : null}
    </>
  );
}

function RequestIntro({ children }: { children: ReactNode }) {
  return <section className="hc-request-intro"><small>你现在需要</small>{children}</section>;
}

function OptionalNote({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}) {
  return (
    <label className="hc-optional-note">
      <span><b>有自己的处理想法？只填这里也可以</b><em>备注 · 可选</em></span>
      <p>不需要先完成其他字段；核对时会判断是否还需补充。</p>
      <textarea value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} />
    </label>
  );
}

function ShortSteps({ steps }: { steps: Array<{ title: string; detail: string }> }) {
  return (
    <section className="hc-request-section">
      <header><b>怎么做</b><small>三步即可</small></header>
      <ol className="hc-short-steps">
        {steps.map((step, index) => (
          <li key={step.title}><i>{index + 1}</i><span><b>{step.title}</b><small>{step.detail}</small></span></li>
        ))}
      </ol>
    </section>
  );
}

function LibraryForm({
  request,
  note,
  setNote,
  submit,
  disabled,
}: {
  request: HumanRequestItem;
  note: string;
  setNote: (value: string) => void;
  submit: SubmitResponse;
  disabled: boolean;
}) {
  const [mode, setMode] = useState<"none" | "oa" | "material">("none");
  const [localPath, setLocalPath] = useState("");
  const [materialFile, setMaterialFile] = useState<File | null>(null);
  const acquisitionPaperId = typeof request.target_assertion?.acquisition_paper_id === "string"
    ? request.target_assertion.acquisition_paper_id
    : null;
  return (
    <>
      <RequestIntro>
        <h3>图书馆连接已失效</h3>
        <p>请恢复连接，或选择仅使用 OA、手动上传文献。登录仍在受控浏览器内完成。</p>
      </RequestIntro>
      <section className="hc-library-options" aria-label="图书馆访问恢复选项">
        <button type="button" disabled={disabled} onClick={() => void submit({ route: "institutional_browser_reconnected" })}>
          <i>推荐 · 重新检查</i><b>我已重连</b><small>提交后会重新检查当前访问路线。</small>
        </button>
        <button type="button" disabled={disabled} onClick={() => setMode("oa")}>
          <i>OA 替代</i><b>跳过，之后只用 OA</b><small>选择更窄的获取路线；无需申请额外权限。</small>
        </button>
        <button type="button" disabled={disabled} onClick={() => setMode("material")}>
          <i>提供全文</i><b>手动上传该文献</b><small>提交文件或本地路径，等待系统核对。</small>
        </button>
      </section>
      <div className="hc-secret-note" role="note">
        <span aria-hidden="true">!</span>
        <p><b>不要在这里提交密码、Cookie、验证码或 token。</b> 登录只在受控浏览器中完成。</p>
      </div>
      {mode === "oa" ? (
        <section className="hc-choice-panel">
          <h4>提交 OA-only 路线回应？</h4>
          <p>提交后系统会核对这条更窄的 OA 路线，不会建立新的授权。</p>
          {request.impact_preview ? <ImpactPreview preview={request.impact_preview} /> : (
            <p className="hc-preview-missing">影响说明尚未提供，因此这里只记录你的选择，不会自动授权。</p>
          )}
          <button type="button" disabled={disabled} onClick={() => void submit({ route: "oa_only" })}>提交 OA 路线回应</button>
        </section>
      ) : null}
      {mode === "material" ? (
        <section className="hc-choice-panel">
          <label className="hc-file-picker">
            合法 PDF · 可选
            <input
              type="file"
              accept="application/pdf,.pdf"
              aria-label="合法全文 PDF"
              disabled={disabled}
              onChange={(event) => setMaterialFile(event.target.files?.[0] ?? null)}
            />
            <small>{materialFile ? `待接纳 · ${materialFile.name}` : "尚未选择 PDF"}</small>
          </label>
          <label>
            本地文件或文件夹路径 · 可选
            <input
              value={localPath}
              disabled={disabled}
              onChange={(event) => setLocalPath(event.target.value)}
              placeholder="例如 /data/papers/source.pdf"
            />
          </label>
          <p>文件或本地路径会先安全保存并核对；核对通过后才会提交这个回应。</p>
          {!acquisitionPaperId ? (
            <p className="hc-preview-missing">
              当前事项没有绑定到具体文献，暂时无法提交这份全文。
            </p>
          ) : null}
          <button type="button" disabled={disabled || !acquisitionPaperId} onClick={() => void submit(
            { acquisition_paper_id: acquisitionPaperId! },
            "provided",
            {
              file: materialFile,
              localPath,
              factPrefix: "material",
              evidenceKind: "library_fulltext",
            },
          )}>提交全文来源回应</button>
        </section>
      ) : null}
      <section className="hc-optional-note hc-library-note">
        <label>
          <span><b>有自己的处理想法？只填这里也可以</b><em>备注 · 可选</em></span>
          <p>不用选择上面的恢复路线。写下希望暂停、改用其他来源或之后再处理，然后直接提交。</p>
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="例如：先暂停这篇；或者改为向作者索取全文。"
          />
        </label>
        <footer>
          <small>这会提交回应，不会直接判定请求已满足。</small>
          <button type="button" disabled={disabled} onClick={() => void submit({}, "deferred")}>提交这条想法</button>
        </footer>
      </section>
    </>
  );
}

function ExternalForm({
  note,
  setNote,
  submit,
  disabled,
}: {
  note: string;
  setNote: (value: string) => void;
  submit: SubmitResponse;
  disabled: boolean;
}) {
  const [applicationRef, setApplicationRef] = useState("");
  const [scope, setScope] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [proofFile, setProofFile] = useState<File | null>(null);
  const facts = useMemo(() => ({
    ...(applicationRef ? { application_ref: applicationRef } : {}),
    ...(scope ? { approved_scope: scope } : {}),
  }), [applicationRef, scope]);
  return (
    <>
      <RequestIntro>
        <h3>本人完成外部申请，并把结果交回来。</h3>
        <p>协议、身份验证和 secret 只在官方页面完成；不要把密码、Cookie、OTP 或 API secret 填在这里。</p>
      </RequestIntro>
      <ShortSteps steps={[
        { title: "打开请求指定的官方页面", detail: "核对目标材料或 API 及真实申请主体。" },
        { title: "完成协议并等待结果", detail: "培训、DUA、PI 或伦理声明留在外部系统。" },
        { title: "把批准结果交回", detail: "可提供申请编号、获准范围和材料来源。" },
      ]} />
      <section className="hc-request-section">
        <header><b>完成后交回</b><small>所有内容均可选</small></header>
        <div className="hc-return-fields">
          <label>申请编号 · 可选<input value={applicationRef} disabled={disabled} onChange={(event) => setApplicationRef(event.target.value)} /></label>
          <label>获准版本与范围 · 可选<input value={scope} disabled={disabled} onChange={(event) => setScope(event.target.value)} /></label>
          <label className="hc-file-picker full">
            批准凭证 · 可选文件
            <input
              type="file"
              accept="application/pdf,image/*"
              aria-label="批准凭证文件"
              disabled={disabled}
              onChange={(event) => setProofFile(event.target.files?.[0] ?? null)}
            />
            <small>{proofFile ? `待接纳 · ${proofFile.name}` : "尚未选择文件"}</small>
          </label>
          <label className="full">
            批准凭证本地路径 · 可选
            <input
              value={localPath}
              disabled={disabled}
              onChange={(event) => setLocalPath(event.target.value)}
              placeholder="例如 /data/approvals/physionet-1842"
            />
          </label>
        </div>
        <p className="hc-asset-boundary">文件或路径会先安全保存并核对；核对通过后才会随回应交回。</p>
      </section>
      <OptionalNote value={note} onChange={setNote} placeholder="例如：审批周期太长，建议改用公开替代数据。" />
      <button className="hc-submit" type="button" disabled={disabled} onClick={() => void submit(
        facts,
        "provided",
        {
          file: proofFile,
          localPath,
          factPrefix: "material",
          evidenceKind: "external_approval",
        },
      )}>提交回应</button>
    </>
  );
}

function OfflineForm({
  request,
  note,
  setNote,
  submit,
  disabled,
}: {
  request: HumanRequestItem;
  note: string;
  setNote: (value: string) => void;
  submit: SubmitResponse;
  disabled: boolean;
}) {
  const [steps, setSteps] = useState([false, false, false]);
  const [deviceRef, setDeviceRef] = useState("");
  const [temperature, setTemperature] = useState("");
  const [resultPath, setResultPath] = useState("");
  const [resultFile, setResultFile] = useState<File | null>(null);
  const [deviation, setDeviation] = useState("");
  const protocolMaterialRef = documentText(
    request.target_assertion,
    "protocol_material_ref",
    "protocol_asset_ref",
  );
  const toggle = (index: number) => setSteps((current) => current.map((value, item) => item === index ? !value : value));
  return (
    <>
      <RequestIntro>
        <h3>按已接纳协议完成线下操作，并交回原始结果。</h3>
        <p>设备、环境或安全边界不匹配时应中止并记录原因；浏览器不会把勾选框当成领域验收。</p>
      </RequestIntro>
      <section className="hc-request-section">
        <header>
          <b>怎么做</b>
          <small>先核对完整协议</small>
          {protocolMaterialRef ? (
            <a
              className="hc-protocol-download"
              href={`/api/v1/research-assets/${encodeURIComponent(protocolMaterialRef)}/content`}
              download
            >
              下载实验说明.md ↓
            </a>
          ) : (
            <small className="hc-protocol-unavailable">暂无可下载协议</small>
          )}
        </header>
        <ol className="hc-short-steps">
          {[
            ["阅读协议并核对设备", "确认设备身份、安全边界和现场条件。"],
            ["按协议执行线下操作", "保留原始观察，不替系统声称成功。"],
            ["保留原始结果", "不要平滑、覆盖或删除异常；把完整记录交回。"],
          ].map(([title, detail], index) => (
            <li key={title}><i>{index + 1}</i><span><b>{title}</b><small>{detail}</small></span><label><input type="checkbox" checked={steps[index]} disabled={disabled} onChange={() => toggle(index)} />已完成</label></li>
          ))}
        </ol>
      </section>
      <section className="hc-request-section">
        <header><b>完成后交回</b><small>所有内容均可选</small></header>
        <div className="hc-return-fields">
          <label>设备或现场标识 · 可选<input value={deviceRef} disabled={disabled} onChange={(event) => setDeviceRef(event.target.value)} /></label>
          <label>现场温度 · 可选<input value={temperature} disabled={disabled} onChange={(event) => setTemperature(event.target.value)} placeholder="例如 24.2°C" /></label>
          <label className="hc-file-picker full">
            原始结果 · 可选文件
            <input
              type="file"
              accept=".zip,.csv,application/zip,text/csv,image/*"
              aria-label="原始结果文件"
              disabled={disabled}
              onChange={(event) => setResultFile(event.target.files?.[0] ?? null)}
            />
            <small>{resultFile ? `待接纳 · ${resultFile.name}` : "尚未选择结果文件"}</small>
          </label>
          <label className="full">
            原始结果本地路径 · 可选
            <input
              value={resultPath}
              disabled={disabled}
              onChange={(event) => setResultPath(event.target.value)}
              placeholder="例如 /data/experiments/fsr-04/run-07"
            />
          </label>
          <label className="full">偏差、异常或中止原因 · 可选<textarea value={deviation} disabled={disabled} onChange={(event) => setDeviation(event.target.value)} /></label>
        </div>
        <p className="hc-asset-boundary">结果文件或路径会先由 Research Memory 接纳；页面不会把文件选择冒充为领域验收。</p>
      </section>
      <OptionalNote value={note} onChange={setNote} placeholder="例如：现场设备型号不符，建议先修改实验方案。" />
      <button className="hc-submit" type="button" disabled={disabled} onClick={() => void submit({
        completed_steps: steps.flatMap((done, index) => done ? [index + 1] : []),
        ...(deviceRef ? { device_ref: deviceRef } : {}),
        ...(temperature ? { temperature } : {}),
        ...(deviation ? { deviation } : {}),
      }, "provided", {
        file: resultFile,
        localPath: resultPath,
        factPrefix: "result",
        evidenceKind: "offline_result",
      })}>提交回应</button>
    </>
  );
}

function PermissionForm({
  request,
  commands,
  authorizations,
  note,
  setNote,
  submit,
  disabled,
  onChanged,
}: {
  request: HumanRequestItem;
  commands: HumanCommand[];
  authorizations: HumanCapabilityAuthorization[];
  note: string;
  setNote: (value: string) => void;
  submit: SubmitResponse;
  disabled: boolean;
  onChanged: () => void;
}) {
  const [decision, setDecision] = useState<"denied" | "allow_once" | null>(null);
  const [recordedCommand, setRecordedCommand] = useState<HumanCommand | null>(null);
  const [recordedAuthorization, setRecordedAuthorization] = useState<HumanCapabilityAuthorization | null>(null);
  const [authorizationPending, setAuthorizationPending] = useState(false);
  const [authorizationError, setAuthorizationError] = useState<string | null>(null);
  const [reviewedPreviewRef, setReviewedPreviewRef] = useState<string | null>(null);
  const authorization = request.required_authorization ?? {};
  const authorizationScope = isRecord(authorization.scope)
    ? authorization.scope
    : {};
  const capability = typeof authorization.capability === "string"
    ? authorization.capability
    : "";
  const commandScopeRef = `human_request:${request.request_ref}`;
  const projectedCommand = [...commands].reverse().find((item) =>
    item.scope_ref === commandScopeRef
      && item.draft.command_kind === "capability_authorization"
      && item.draft.payload.capability === capability
      && item.draft.payload.decision === "granted"
      && stringify(item.draft.payload.scope) === stringify(authorizationScope),
  ) ?? null;
  const command = projectedCommand ?? recordedCommand;
  const projectedAuthorization = authorizations.find((item) =>
    item.confirmation_receipt_ref === command?.confirmation_receipt?.receipt_ref
      && item.decision === "granted"
      && item.is_current !== false
      && stringify(item.requirement) === stringify(authorization),
  ) ?? null;
  const authorizationReceipt = projectedAuthorization ?? recordedAuthorization;
  const preview = command?.impact_preview;
  const currentPreview = preview?.status === "current"
    && preview.draft_revision === command?.draft_revision
    && preview.draft_hash === command?.draft_hash;

  useEffect(() => {
    setDecision(null);
    setRecordedCommand(null);
    setRecordedAuthorization(null);
    setAuthorizationPending(false);
    setAuthorizationError(null);
    setReviewedPreviewRef(null);
  }, [request.request_ref]);

  const runAuthorizationStep = async (
    operation: () => Promise<HumanCommand | HumanCapabilityAuthorization>,
  ) => {
    if (authorizationPending || disabled) return;
    setAuthorizationPending(true);
    setAuthorizationError(null);
    try {
      const result = await operation();
      if ("intent_id" in result) {
        if (
          result.scope_ref !== commandScopeRef
          || result.draft.command_kind !== "capability_authorization"
          || result.draft.payload.capability !== capability
          || result.draft.payload.decision !== "granted"
          || stringify(result.draft.payload.scope)
            !== stringify(authorizationScope)
        ) {
          throw new Error("capability_authorization_command_invalid");
        }
        setRecordedCommand(result);
      } else {
        if (
          result.decision !== "granted"
          || result.is_current === false
          || stringify(result.requirement) !== stringify(authorization)
          || result.confirmation_receipt_ref
            !== command?.confirmation_receipt?.receipt_ref
        ) {
          throw new Error("capability_authorization_invalid");
        }
        setRecordedAuthorization(result);
      }
      onChanged();
    } catch (caught) {
      setAuthorizationError(reasonCode(caught));
    } finally {
      setAuthorizationPending(false);
    }
  };

  const summary = [
    ["允许什么", firstDefined(authorization, "capability", "method", "action")],
    ["访问哪里", firstDefined(authorizationScope, "destination", "target")],
    ["持续多久", firstDefined(authorizationScope, "duration", "expires_at", "valid_for")],
    ["明确不允许", firstDefined(authorizationScope, "exclusions", "forbidden", "not_allowed")],
  ] as const;
  return (
    <>
      <RequestIntro>
        <h3>决定是否允许精确、低频的能力扩张。</h3>
        <p>日常、可撤销的本地研究已在现有范围内；这里仅处理超出范围的动作。</p>
      </RequestIntro>
      <section className="hc-permission-brief">
        {summary.map(([label, value]) => (
          <div key={label}><small>{label}</small><b>{stringify(value)}</b></div>
        ))}
      </section>
      {request.impact_preview ? <ImpactPreview preview={request.impact_preview} /> : (
        <p className="hc-preview-missing">影响说明尚未提供；提交只记录回应，不代表动作已经执行。</p>
      )}
      <div className="hc-permission-actions">
        <button type="button" aria-pressed={decision === "denied"} onClick={() => setDecision("denied")}>拒绝这次访问</button>
        <button type="button" className="allow" aria-pressed={decision === "allow_once"} onClick={() => setDecision("allow_once")}>仅允许本次任务</button>
      </div>
      <OptionalNote value={note} onChange={setNote} placeholder="例如：仅允许 /metadata，并限制最多下载 10 MB。" />
      {decision === "allow_once" ? (
        <section className="lumen-command" data-command-status={command?.status ?? "required"}>
          <small>本次精确授权</small>
          {!command ? (
            <button
              type="button"
              disabled={disabled || authorizationPending || !capability}
              onClick={() => void runAuthorizationStep(() => createHumanCommand(
                commandScopeRef,
                {
                  command_kind: "capability_authorization",
                  payload: {
                    capability,
                    decision: "granted",
                    scope: authorizationScope,
                  },
                },
              ))}
            >建立授权草案</button>
          ) : null}
          {command && !currentPreview && !command.confirmation_receipt ? (
            <button
              type="button"
              disabled={disabled || authorizationPending}
              onClick={() => void runAuthorizationStep(() => previewHumanCommand(command))}
            >生成并核对影响说明</button>
          ) : null}
          {command && currentPreview && preview && !command.confirmation_receipt ? (
            <>
              <details onToggle={(event) => {
                if (event.currentTarget.open) {
                  setReviewedPreviewRef(preview.preview_ref);
                }
              }}>
                <summary>查看本次授权会发生什么</summary>
                <div className="lumen-owner-previews">
                  {preview.owner_previews.map((owner) => (
                    <section key={owner.digest}>
                      <b>会发生</b><p>{owner.will_happen.join("；")}</p>
                      <b>不会发生</b><p>{owner.will_not_happen.join("；")}</p>
                      <b>风险与失效条件</b><p>{[...owner.risks, ...owner.stale_conditions].join("；")}</p>
                    </section>
                  ))}
                </div>
              </details>
              <button
                type="button"
                disabled={disabled || authorizationPending || reviewedPreviewRef !== preview.preview_ref}
                onClick={() => void runAuthorizationStep(() => confirmHumanCommand(command))}
              >确认当前草案与影响说明</button>
            </>
          ) : null}
          {command?.confirmation_receipt && !authorizationReceipt ? (
            <button
              type="button"
              disabled={disabled || authorizationPending}
              onClick={() => void runAuthorizationStep(() => authorizeHumanCommand(command))}
            >签发仅限本次任务的授权</button>
          ) : null}
          {authorizationReceipt ? (
            <p role="status">本次授权已记录；提交回应后，系统还会独立核对并决定是否继续任务。</p>
          ) : null}
          {authorizationError ? <em role="alert">授权步骤失败 · {authorizationError}</em> : null}
        </section>
      ) : null}
      {decision === "denied" ? (
        <button
          className="hc-submit"
          type="button"
          disabled={disabled}
          onClick={() => void submit({}, "declined")}
        >提交拒绝</button>
      ) : null}
      {decision === "allow_once" && authorizationReceipt ? (
        <button
          className="hc-submit"
          type="button"
          disabled={disabled || authorizationPending}
          onClick={() => void submit(
            { authorization_receipt_ref: authorizationReceipt.receipt_ref },
            "provided",
          )}
        >提交授权回应</button>
      ) : null}
    </>
  );
}
