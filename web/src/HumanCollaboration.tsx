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
  type HumanRequestImpactPreview,
  type HumanRequestItem,
  type HumanRequestResponseBody,
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
    eyebrow: "HumanRequest · access recovery",
    draftLabel: "在图书馆恢复 Draft Session 中发消息",
    draftPlaceholder: "询问当前状态，或说明你希望怎样处理……",
  },
  external_material_api_access: {
    list: "外部材料或 API",
    title: "完成外部材料或 API 申请",
    eyebrow: "HumanRequest · external handoff",
    draftLabel: "在外部材料 Draft Session 中发消息",
    draftPlaceholder: "询问为什么需要材料，或讨论替代路线……",
  },
  offline_action: {
    list: "线下操作",
    title: "完成线下操作",
    eyebrow: "HumanRequest · offline action",
    draftLabel: "在线下操作 Draft Session 中发消息",
    draftPlaceholder: "询问步骤、安全边界，或说明现场限制……",
  },
  capability_authorization: {
    list: "低频权限",
    title: "决定低频能力授权",
    eyebrow: "HumanRequest · capability authorization",
    draftLabel: "在权限请求 Draft Session 中发消息",
    draftPlaceholder: "询问风险，或讨论更窄的授权范围……",
  },
};

const fallbackCompanionCopy: Record<CompanionShellState, {
  label: string;
  message: string;
}> = {
  loading: {
    label: "正在建立上下文",
    message: "我会在首个 Snapshot 返回后，用同一个窗口解释研究空间。",
  },
  "first-error": {
    label: "本地连接不可用",
    message: "首个 Snapshot 尚未返回；Shell 保持在位，修复 daemon 后可以重新读取。",
  },
  "readiness-unavailable": {
    label: "底座尚未就绪",
    message: "readiness 当前不可用。研究浏览保持只读，不会猜测或补写 Owner 状态。",
  },
  "ready-empty": {
    label: "研究空间已就绪",
    message: "这里还没有 Quest。使用左侧 ＋ 后，我会继续留在这个位置。",
  },
  "ready-active": {
    label: "跟随当前 Projection",
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

function openRequests(projection?: HumanCollaborationProjection): HumanRequestItem[] {
  return projection?.human_requests.items.filter((item) => item.status === "open") ?? [];
}

function questIdentity(scopeRef: string): string {
  return scopeRef.startsWith("quest:") ? scopeRef.slice("quest:".length) : scopeRef;
}

function scopeLabel(request: HumanRequestItem): string {
  return request.direct_waiters?.some((waiter) => waiter.wait_scope === "quest")
    ? "QUEST WAIT"
    : "LOCAL WAIT";
}

export function QuestCompanion({
  state,
  collaboration,
  onChanged,
  onOpenRequest,
}: {
  state: CompanionShellState;
  collaboration?: HumanCollaborationProjection;
  onChanged: () => void;
  onOpenRequest: (requestRef: string) => void;
}) {
  const companion = collaboration?.companion;
  const ready = companion?.status === "ready";
  const canSend = ready && Boolean(companion.scope_ref);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scopeRef = companion?.scope_ref ?? null;
  const requests = scopeRef
    ? openRequests(collaboration).filter((request) => Boolean(request.quest_ref)
      && questIdentity(request.quest_ref!) === questIdentity(scopeRef))
    : [];
  const attention = requests.find((request) =>
    request.direct_waiters?.some((waiter) => waiter.wait_scope === "quest"),
  ) ?? requests[0];
  const messages = scopeRef
    ? companion?.messages.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
  const softConstraints = scopeRef
    ? companion?.soft_constraints.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
  const agentProposals = scopeRef
    ? companion?.agent_proposals.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
  const commands = scopeRef
    ? collaboration?.commands.items.filter((item) => item.scope_ref === scopeRef) ?? []
    : [];
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

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!canSend || !message || sending) return;
    setSending(true);
    setError(null);
    try {
      await sendCompanionMessage(message, companion.scope_ref);
      setDraft("");
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setSending(false);
    }
  };

  return (
    <aside
      className="lumen-companion"
      aria-label="Quest Companion"
      data-shell-region="companion"
      tabIndex={0}
    >
      <header className="lumen-companion-head">
        <span className="lumen-orb" aria-hidden="true" />
        <div>
          <b>Quest Companion</b>
          <small>贯穿研究空间的高频入口</small>
        </div>
        <code>{ready ? "conversation · ready" : "capability_unavailable"}</code>
      </header>
      <div className="lumen-chat" aria-live="polite">
        {attention ? (
          <article className="lumen-human-attention">
            <small>NEEDS YOU · {scopeLabel(attention)}</small>
            <b>{requestCopy[attention.kind].list}需要你处理</b>
            <p>{attention.obligation}</p>
            <button type="button" onClick={() => onOpenRequest(attention.request_ref)}>
              查看请求
            </button>
          </article>
        ) : null}
        {ready && messages.length ? messages.map((message, index) => (
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
            {message.status === "queued" || message.status === "processing" || message.status === "running" ? (
              <span className="lumen-message-state">正在形成回复…</span>
            ) : null}
          </article>
        )) : (
          <article className="lumen-message">
            <small>{fallbackCompanionCopy[state].label}</small>
            {fallbackCompanionCopy[state].message}
          </article>
        )}
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
          <input
            aria-label="给 Quest Companion 发消息"
            disabled={!canSend || sending}
            placeholder={canSend
              ? "问为什么，或提出一个软约束……"
              : ready
                ? "创建 Quest 后开始对话"
                : "Quest Companion 尚未启用"}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button
            type="submit"
            disabled={!canSend || sending || !draft.trim()}
            aria-label="发送消息"
          >
            ↑
          </button>
        </div>
        <small>{error ? `发送失败 · ${error}` : "普通聊天不会被猜成硬命令"}</small>
      </form>
    </aside>
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
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewedPreviewRef, setReviewedPreviewRef] = useState<string | null>(null);
  const [recordedAuthorization, setRecordedAuthorization] = useState<HumanCapabilityAuthorization | null>(null);
  const [capabilityDraft, setCapabilityDraft] = useState(command.draft.payload.capability);
  const [decisionDraft, setDecisionDraft] = useState(command.draft.payload.decision);
  const [scopeDraft, setScopeDraft] = useState(() => JSON.stringify(command.draft.payload.scope, null, 2));

  useEffect(() => {
    setCapabilityDraft(command.draft.payload.capability);
    setDecisionDraft(command.draft.payload.decision);
    setScopeDraft(JSON.stringify(command.draft.payload.scope, null, 2));
    setReviewedPreviewRef(null);
  }, [command.draft_hash, command.draft_revision]);

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
  const payload = command.draft.payload;
  const authorization = projectedAuthorization ?? recordedAuthorization;
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
      command_kind: command.draft.command_kind,
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
      <b>{payload.decision} · {payload.capability}</b>
      <p>{stringify(payload.scope)}</p>
      {command.source_proposal_ref ? (
        <code className="lumen-source-proposal">source · {command.source_proposal_ref}</code>
      ) : null}
      <details onToggle={(event) => {
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
      {!command.confirmation_receipt ? (
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
                onChange={(event) => setDecisionDraft(event.target.value as HumanCommandDraft["payload"]["decision"])}
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
          <p>确认没有执行命令，也没有签发 Capability Authorization。</p>
        </div>
      ) : null}
      {command.confirmation_receipt && !authorization ? (
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
  if (!isRecord(command) || typeof command.command_kind !== "string") return null;
  const payload = command.payload;
  if (!isRecord(payload)
    || typeof payload.capability !== "string"
    || !["granted", "denied", "revoked"].includes(String(payload.decision))
    || !isRecord(payload.scope)) return null;
  return {
    command_kind: command.command_kind,
    payload: {
      capability: payload.capability,
      decision: payload.decision as HumanCommandDraft["payload"]["decision"],
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
      <summary>查看 Owner Impact Preview</summary>
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
  selectedRef,
  collaboration,
  onSelect,
  onClose,
  onChanged,
}: {
  open: boolean;
  selectedRef: string | null;
  collaboration?: HumanCollaborationProjection;
  onSelect: (requestRef: string | null) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const items = collaboration?.human_requests.items ?? [];
  const selected = items.find((item) => item.request_ref === selectedRef) ?? null;
  const dialogRef = useRef<HTMLElement>(null);
  const [orphanRecoveryAttempt, setOrphanRecoveryAttempt] = useState(0);
  const currentRequestRefs = items
    .filter((item) => item.status === "open")
    .map((item) => item.request_ref);
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
    if (!open) return;
    const backgrounds = Array.from(
      document.querySelectorAll<HTMLElement>("[data-hc-background]"),
    ).map((element) => ({ element, inert: element.inert }));
    const previousOverflow = document.body.style.overflow;
    backgrounds.forEach(({ element }) => {
      element.inert = true;
    });
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      onClose();
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      window.removeEventListener("keydown", closeOnEscape);
      backgrounds.forEach(({ element, inert }) => {
        element.inert = inert;
      });
      document.body.style.overflow = previousOverflow;
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const frame = window.requestAnimationFrame(() => {
      dialogRef.current
        ?.querySelector<HTMLButtonElement>(".hc-close")
        ?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [open, selectedRef]);

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
    <div
      className="hc-backdrop"
      data-open="true"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        ref={dialogRef}
        className={`hc-dialog ${selected?.kind === "library_reconnect"
          ? "hc-dialog-library"
          : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label="HumanRequest"
        tabIndex={-1}
        onKeyDown={trapDialogFocus}
      >
        {selected ? (
          <HumanRequestView
            key={selected.request_ref}
            request={selected}
            collaboration={collaboration}
            onBack={() => onSelect(null)}
            onClose={onClose}
            onChanged={onChanged}
          />
        ) : (
          <HumanRequestList items={items} onSelect={onSelect} onClose={onClose} />
        )}
      </section>
    </div>
  );
}

function HumanRequestList({
  items,
  onSelect,
  onClose,
}: {
  items: HumanRequestItem[];
  onSelect: (requestRef: string) => void;
  onClose: () => void;
}) {
  const orderedKinds = Object.keys(requestCopy) as HumanRequestItem["kind"][];
  return (
    <>
      <header className="hc-head">
        <span className="hc-symbol" aria-hidden="true">!</span>
        <div>
          <small>HUMAN WAITING PROJECTION</small>
          <h2>HumanRequest</h2>
          <p>每个请求只阻塞直接 waiter；人的回应仍需 Owner 验收。</p>
        </div>
        <button type="button" className="hc-close" onClick={onClose} aria-label="关闭 HumanRequest">×</button>
      </header>
      <main className="hc-list">
        {items.length ? orderedKinds.map((kind) => {
          const sameKind = items.filter((item) => item.kind === kind);
          if (!sameKind.length) return null;
          return (
            <section className="hc-kind-group" key={kind}>
              <header><b>{requestCopy[kind].list}</b><small>{sameKind.length} current / history</small></header>
              {sameKind.map((item) => (
                <button
                  type="button"
                  className="hc-request-card"
                  key={item.request_ref}
                  onClick={() => onSelect(item.request_ref)}
                  aria-label={`${requestCopy[kind].list} · ${item.obligation}`}
                >
                  <span><b>{item.obligation}</b><small>{item.business_purpose}</small></span>
                  <i className={item.status === "open" ? "current" : ""}>{item.status}</i>
                </button>
              ))}
            </section>
          );
        }) : (
          <section className="hc-list-empty">
            <b>当前没有 HumanRequest</b>
            <p>Conversation、ReadQuery 或技术不可用不会伪装成人的待办。</p>
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
}: {
  request: HumanRequestItem;
  collaboration?: HumanCollaborationProjection;
  onBack: () => void;
  onClose: () => void;
  onChanged: () => void;
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
          <small>{copy.eyebrow} · {scopeLabel(request)}</small>
          <h2>{copy.title}</h2>
          <p>{request.obligation}</p>
        </div>
        <div className="hc-head-actions">
          <button type="button" onClick={onBack}>返回请求列表</button>
          <button type="button" className="hc-close" onClick={onClose} aria-label="关闭 HumanRequest">×</button>
        </div>
      </header>
      <div className="hc-request-workspace">
        <main className="hc-request-core">
          {request.kind !== "library_reconnect" ? (
            <section className={`hc-waiting ${safeWork ? "local" : "quest"}`}>
              <small>Human Waiting Projection</small>
              <b>{safeWork ? "只等待直接依赖，其他工作继续" : "当前没有安全且有意义的工作可继续"}</b>
              <p>
                {safeWork
                  ? `${waiter?.waiter_ref ?? "当前 waiter"} 保持局部等待；这份 Projection 没有恢复权。`
                  : "请求窗口只呈现人的待办；Owner 仍需逐 waiter 验证 currentness、授权、receipt 与其他 blocker。"}
              </p>
            </section>
          ) : null}
          <RequestForm request={request} onChanged={onChanged} />
          <RequestDetails request={request} otherBlockers={waiting?.other_blockers ?? []} />
        </main>
        <IntentDraftingSession
          request={request}
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
  const responseReceipts = request.responses?.map((response) => {
    const receipt = isRecord(response.receipt) ? response.receipt : undefined;
    return firstDefined(response, "receipt_ref", "response_receipt_ref")
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
        <Detail label="Owner" value={request.issuer} />
        <Detail label="Business purpose" value={request.business_purpose} />
        <Detail label="TargetAssertion" value={stringify(request.target_assertion)} />
        <Detail label="Acceptance" value={request.acceptance_conditions?.join("；")} />
        <Detail label="Required authorization" value={stringify(request.required_authorization)} />
        <Detail label="Human responses" value={String(request.responses?.length ?? 0)} />
        <Detail label="Response receipts" value={responseReceipts.join("；") || "none"} />
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
  messages,
  onChanged,
}: {
  request: HumanRequestItem;
  messages: CompanionMessage[];
  onChanged: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scoped = messages.filter((message) => message.scope_ref === request.request_ref);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!message || sending) return;
    setSending(true);
    setError(null);
    try {
      await sendCompanionMessage(message, request.request_ref);
      setDraft("");
      onChanged();
    } catch (caught) {
      setError(reasonCode(caught));
    } finally {
      setSending(false);
    }
  };

  return (
    <aside className="hc-request-draft" aria-label={`${requestCopy[request.kind].list} Drafting Session`}>
      <header>
        <span className="hc-draft-orb" aria-hidden="true" />
        <div><small>INTENT DRAFTING SESSION</small><b>询问与协商</b><span>request context</span></div>
      </header>
      <div className="hc-draft-transcript" aria-live="polite">
        {!scoped.length ? (
          <article>
            <small>COMPANION · REQUEST CONTEXT</small>
            <p>{request.obligation} 你可以询问状态、验收边界或讨论替代路线。</p>
          </article>
        ) : scoped.map((message, index) => (
          <article className={message.role === "user" ? "me" : ""} key={message.message_ref ?? index}>
            <small>{message.role === "user" ? "YOU · CONVERSATION" : "COMPANION · EXPLANATION"}</small>
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
              disabled={sending}
            />
            <button
              type="submit"
              disabled={sending || !draft.trim()}
              aria-label="发送 Draft Session 消息"
            >↑</button>
          </span>
        </label>
        <small>{error ? `发送失败 · ${error}` : `${draft.length} 字 · session draft`}</small>
      </form>
      <div className="hc-draft-status">Drafting Session 不会提交、满足或拒绝左侧 HumanRequest。</div>
      <div className="hc-draft-boundary">聊天帮助解释状态和形成草案；只有左侧明确提交才形成 HumanRequestResponse。</div>
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

function RequestForm({ request, onChanged }: {
  request: HumanRequestItem;
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
        <b>这个 revision 已终结 · {request.status}</b><br />
        历史回应与 receipt 可在详情中查看；终态请求不能重新提交或恢复。
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
          <p>Owner Evaluation 尚未完成；表单已退出且不能重复提交，当前 waiter 也不会自动恢复。</p>
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
        <PermissionForm request={request} note={note} setNote={setNote} submit={submit} disabled={pending} />
      ) : null}
      {error || request.kind !== "library_reconnect" ? (
      <div className="hc-response-boundary" role="status">
        {error ? (
          <><b>回应没有记录</b><br />{error}</>
        ) : (
          <><b>回应 ≠ 已满足 ≠ 已恢复</b><br />Owner 会验证精确 revision、证据、授权与 currentness。</>
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
      <p>不需要先完成其他字段；Owner 会依据实际内容判断是否还需补充。</p>
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
          <i>推荐 · RETEST</i><b>我已重连</b><small>只提交重连声明；Runtime 会重新测试当前路线。</small>
        </button>
        <button type="button" disabled={disabled} onClick={() => setMode("oa")}>
          <i>OA 替代 · RESPONSE</i><b>跳过，之后只用 OA</b><small>选择更窄的获取路线；无需额外 Capability Authorization。</small>
        </button>
        <button type="button" disabled={disabled} onClick={() => setMode("material")}>
          <i>提供全文 · MATERIAL</i><b>手动上传该文献</b><small>提交文件或本地路径，等待 Research Memory 接纳。</small>
        </button>
      </section>
      <div className="hc-secret-note" role="note">
        <span aria-hidden="true">!</span>
        <p><b>不要在这里提交密码、Cookie、验证码或 token。</b> 登录只在受控浏览器中完成。</p>
      </div>
      {mode === "oa" ? (
        <section className="hc-choice-panel">
          <h4>提交 OA-only 路线回应？</h4>
          <p>这仍是 HumanRequestResponse；Acquisition Runtime 会验证更窄的 OA 路线，无需建立新的授权。</p>
          {request.impact_preview ? <ImpactPreview preview={request.impact_preview} /> : (
            <p className="hc-preview-missing">Owner Impact Preview 尚未提供，因此这里不会伪造确认或授权。</p>
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
          <p>文件或本地路径会先交给 Research Memory；只有 Asset Accepted receipt 形成后，才会提交这个回应。</p>
          {!acquisitionPaperId ? (
            <p className="hc-preview-missing">
              当前请求缺少 Owner 签发的 acquisition_paper_id，不能提交未绑定到精确获取项的全文。
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
          <button type="button" disabled={disabled} onClick={() => void submit({})}>提交这条想法</button>
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
        <p className="hc-asset-boundary">文件或路径会先由 Research Memory 接纳；只有真实 Asset Accepted receipt 会随回应交回。</p>
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
            <small className="hc-protocol-unavailable">Owner 未提供可下载协议</small>
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
  const [decision, setDecision] = useState<"denied" | "allow_once" | null>(null);
  const authorization = request.required_authorization ?? {};
  const summary = [
    ["允许什么", firstDefined(authorization, "capability", "method", "action")],
    ["访问哪里", firstDefined(authorization, "destination", "scope", "target")],
    ["持续多久", firstDefined(authorization, "duration", "expires_at", "valid_for")],
    ["明确不允许", firstDefined(authorization, "exclusions", "forbidden", "not_allowed")],
  ] as const;
  return (
    <>
      <RequestIntro>
        <h3>决定是否允许精确、低频的能力扩张。</h3>
        <p>普通可逆本地研究已经由 Quest broad authorization 覆盖；这里仅处理超出既有范围的动作。</p>
      </RequestIntro>
      <section className="hc-permission-brief">
        {summary.map(([label, value]) => (
          <div key={label}><small>{label}</small><b>{stringify(value)}</b></div>
        ))}
      </section>
      {request.impact_preview ? <ImpactPreview preview={request.impact_preview} /> : (
        <p className="hc-preview-missing">Owner Impact Preview 尚未提供；提交只记录回应，不会被前端当成已执行。</p>
      )}
      <div className="hc-permission-actions">
        <button type="button" aria-pressed={decision === "denied"} onClick={() => setDecision("denied")}>拒绝这次访问</button>
        <button type="button" className="allow" aria-pressed={decision === "allow_once"} onClick={() => setDecision("allow_once")}>仅允许本次 Run</button>
      </div>
      <OptionalNote value={note} onChange={setNote} placeholder="例如：仅允许 /metadata，并限制最多下载 10 MB。" />
      <button className="hc-submit" type="button" disabled={disabled} onClick={() => void submit(
        decision ? { authorization_decision: decision } : {},
        decision === "denied" ? "declined" : "provided",
      )}>提交回应</button>
    </>
  );
}
