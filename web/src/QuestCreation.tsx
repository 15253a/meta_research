import { useEffect, useMemo, useRef, useState } from "react";
import {
  cancelQuest,
  confirmQuest,
  createQuest,
  generateQuestionProposal,
  previewQuestConfirmation,
  ProductError,
  reviseQuestDraft,
  saveQuestionProposal,
  type QuestionContent,
  type QuestCreationView,
  type QuestDraft,
} from "./api";

const emptyDraft: QuestDraft = {
  goal: "",
  completion_criteria: "",
  key_configuration: "",
  literature_scope: "open_access",
  initial_question_direction: "",
  material_receipts: [],
};

const receiptLabels: Array<
  [keyof QuestCreationView["receipts"], string, string]
> = [
  ["human_confirmation", "最终确认", "Human Collaboration"],
  ["quest_goal", "Quest / Goal", "Research Graph"],
  ["question_content", "问题内容", "Research Memory"],
  ["question_identity", "问题身份", "Research Graph"],
  ["cycle_activation", "Cycle 激活", "Advancement Engine"],
];

const proposalFields: Array<{
  key: keyof QuestionContent;
  label: string;
  required?: boolean;
  rows: number;
}> = [
  { key: "title", label: "标题", required: true, rows: 1 },
  { key: "unknown_statement", label: "未知陈述", required: true, rows: 3 },
  { key: "answer_shape", label: "答案形状", required: true, rows: 3 },
  { key: "applicability_scope", label: "适用范围", required: true, rows: 3 },
  { key: "background_context", label: "背景上下文", rows: 3 },
  { key: "requirements_constraints", label: "要求与约束", rows: 3 },
];

export function QuestCreationWorkbench({
  current,
  onClose,
  onChanged,
}: {
  current: QuestCreationView | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [creation, setCreation] = useState<QuestCreationView | null>(current);
  const [draft, setDraft] = useState<QuestDraft>(
    current?.quest_draft.value ?? emptyDraft,
  );
  const [proposal, setProposal] = useState<QuestionContent | null>(
    current?.proposal?.content ?? null,
  );
  const [proposalDirty, setProposalDirty] = useState(false);
  const [editingBasis, setEditingBasis] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => {
      if (dialog?.open) dialog.close();
    };
  }, []);

  useEffect(() => setCreation(current), [current]);

  useEffect(() => {
    setDraft(current?.quest_draft.value ?? emptyDraft);
    setProposal(current?.proposal?.content ?? null);
    setProposalDirty(false);
    setEditingBasis(false);
  }, [
    current?.initialization_id,
    current?.quest_draft.hash,
    current?.proposal?.ref,
  ]);

  const draftComplete = [
    draft.goal,
    draft.completion_criteria,
    draft.key_configuration,
    draft.initial_question_direction,
  ].every((value) => value.trim().length > 0);
  const proposalComplete = useMemo(
    () =>
      proposal !== null &&
      [
        proposal.title,
        proposal.unknown_statement,
        proposal.answer_shape,
        proposal.applicability_scope,
      ].every((value) => value.trim().length > 0),
    [proposal],
  );
  const terminal = creation?.status === "completed" || creation?.status === "cancelled";

  const run = async (operation: () => Promise<QuestCreationView>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await operation();
      setCreation(next);
      if (next.proposal) setProposal(next.proposal.content);
      setProposalDirty(false);
      onChanged();
      return next;
    } catch (caught) {
      setError(messageFor(caught));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const persistDraft = async (): Promise<QuestCreationView | null> => {
    if (!creation) return run(() => createQuest(draft));
    if (sameDraft(creation.quest_draft.value, draft)) return creation;
    return run(() => reviseQuestDraft(creation, draft));
  };

  const generate = async () => {
    const basis = await persistDraft();
    if (!basis) return;
    const next = await run(() => generateQuestionProposal(basis));
    if (next) setEditingBasis(false);
  };

  const rebindReviewedProposal = async () => {
    if (!proposal) return;
    const basis = await persistDraft();
    if (!basis) return;
    const next = await run(() => saveQuestionProposal(basis, proposal));
    if (next) setEditingBasis(false);
  };

  const saveEdits = async (): Promise<QuestCreationView | null> => {
    if (!creation || !proposal) return null;
    if (!proposalDirty) return creation;
    return run(() => saveQuestionProposal(creation, proposal));
  };

  const confirm = async () => {
    if (!creation || proposalDirty) return;
    await run(() => confirmQuest(creation));
  };

  const preview = async () => {
    const exact = await saveEdits();
    if (!exact) return;
    await run(() => previewQuestConfirmation(exact));
  };

  const close = () => {
    if (dialogRef.current?.open) dialogRef.current.close();
    onClose();
  };

  return (
    <dialog
      ref={dialogRef}
      className="creation-backdrop"
      aria-labelledby="creation-title"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <section
        className="creation-workbench"
      >
        <header className="creation-header">
          <div>
            <p className="eyebrow">Direct · Quest initialization</p>
            <h2 id="creation-title">定义 Quest 与首问题</h2>
            <p>一个连续窗口，一次精确确认；Owner 接纳仍保持分层。</p>
          </div>
          <button className="icon-button" type="button" onClick={close} aria-label="关闭" autoFocus>
            ×
          </button>
        </header>

        <div className="creation-layout">
          <div className="creation-editor">
            {!proposal || editingBasis ? (
              <section className="draft-form" aria-labelledby="draft-title">
                <div className="form-heading">
                  <span className="step-index">01</span>
                  <div>
                    <h3 id="draft-title">先固定研究基底</h3>
                    <p>这些内容共同决定首问题 Proposal 的 currentness。</p>
                  </div>
                </div>

                <label>
                  <span>Quest Goal</span>
                  <textarea
                    rows={3}
                    value={draft.goal}
                    disabled={busy || terminal}
                    onChange={(event) =>
                      setDraft({ ...draft, goal: event.target.value })
                    }
                    placeholder="这项长期研究最终要回答或改变什么？"
                  />
                </label>
                <label>
                  <span>完成标准</span>
                  <textarea
                    rows={3}
                    value={draft.completion_criteria}
                    disabled={busy || terminal}
                    onChange={(event) =>
                      setDraft({ ...draft, completion_criteria: event.target.value })
                    }
                    placeholder="什么证据足以让你接受阶段结论？"
                  />
                </label>
                <label>
                  <span>关键配置</span>
                  <textarea
                    rows={2}
                    value={draft.key_configuration}
                    disabled={busy || terminal}
                    onChange={(event) =>
                      setDraft({ ...draft, key_configuration: event.target.value })
                    }
                    placeholder="资源、期限与不可越过的运行约束"
                  />
                </label>
                <fieldset disabled={busy || terminal}>
                  <legend>文献范围</legend>
                  <label className="scope-choice">
                    <input
                      type="radio"
                      name="literature-scope"
                      value="comprehensive"
                      checked={draft.literature_scope === "comprehensive"}
                      onChange={() =>
                        setDraft({ ...draft, literature_scope: "comprehensive" })
                      }
                    />
                    <span>全面搜索，包括当前已授权的图书馆范围</span>
                  </label>
                  <label className="scope-choice">
                    <input
                      type="radio"
                      name="literature-scope"
                      value="open_access"
                      checked={draft.literature_scope === "open_access"}
                      onChange={() =>
                        setDraft({ ...draft, literature_scope: "open_access" })
                      }
                    />
                    <span>只搜索开放获取资源</span>
                  </label>
                  <label className="scope-choice unavailable-choice">
                    <input
                      type="radio"
                      name="literature-scope"
                      value="provided_materials"
                      disabled
                    />
                    <span>只使用我提供的材料</span>
                    <code>capability_unavailable</code>
                  </label>
                </fieldset>
                <label>
                  <span>首问题方向</span>
                  <textarea
                    rows={3}
                    value={draft.initial_question_direction}
                    disabled={busy || terminal}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        initial_question_direction: event.target.value,
                      })
                    }
                    placeholder="系统应先把哪一项未知起草成正式问题？"
                  />
                </label>

                <div className="typed-unavailable">
                  <strong>材料 basis 尚未启用</strong>
                  <p>当前 direct 路线不依赖材料。Research Memory 资产接纳交付前，不会把本地文件或路径伪装成已接纳材料。</p>
                </div>

                <div className="form-actions">
                  {creation && !terminal ? (
                    <button
                      className="secondary-button"
                      type="button"
                      disabled={busy}
                      onClick={() => void run(() => cancelQuest(creation))}
                    >
                      取消创建
                    </button>
                  ) : <span />}
                  <button
                    className="primary-button"
                    type="button"
                    disabled={!draftComplete || busy || terminal}
                    onClick={() => void generate()}
                  >
                    {busy
                      ? "正在起草…"
                      : proposal
                        ? "按新基底重新生成"
                        : "生成六字段问题"}
                  </button>
                  {proposal ? (
                    <button
                      className="quiet-button"
                      type="button"
                      disabled={!draftComplete || busy || terminal}
                      onClick={() => void rebindReviewedProposal()}
                    >
                      按新基底明确复核原问题
                    </button>
                  ) : null}
                </div>
              </section>
            ) : (
              <section className="proposal-form" aria-labelledby="proposal-title">
                <div className="form-heading">
                  <span className="step-index">02</span>
                  <div>
                    <h3 id="proposal-title">审阅完整 QuestionProposal</h3>
                    <p>可修改、重新生成，也可以一字不改直接确认。</p>
                  </div>
                </div>

                {proposalFields.map((field) => (
                  <label key={field.key}>
                    <span>
                      {field.label}
                      {field.required ? <sup>必填</sup> : <small>可选</small>}
                    </span>
                    <textarea
                      rows={field.rows}
                      value={proposal[field.key]}
                      disabled={busy || creation?.status === "dispatching" || terminal}
                      onChange={(event) => {
                        setProposal({ ...proposal, [field.key]: event.target.value });
                        setProposalDirty(true);
                      }}
                    />
                  </label>
                ))}

                <div className="proposal-basis">
                  <span>绑定 Quest draft</span>
                  <code>{creation?.quest_draft.hash.slice(0, 12)}</code>
                  <span>Proposal</span>
                  <code>{creation?.proposal?.ref.slice(-12)}</code>
                </div>

                <section className="impact-preview" aria-labelledby="impact-preview-title">
                  <div>
                    <span className="step-index">03</span>
                    <div>
                      <h3 id="impact-preview-title">确定性 Impact Preview</h3>
                      <p>每个受影响 Owner 独立声明可能变化、明确不变与接纳前提。</p>
                    </div>
                  </div>
                  {creation?.confirmation_preview ? (
                    <>
                      <p className={`preview-status ${creation.confirmation_preview.status}`}>
                        {creation.confirmation_preview.status}
                        <code>{creation.confirmation_preview.hash.slice(0, 12)}</code>
                      </p>
                      <ul>
                        {creation.confirmation_preview.target_assertions.map((assertion) => (
                          <li key={`${assertion.owner}:${assertion.operation}`}>
                            <strong>{assertion.owner}</strong>
                            <span>{assertion.operation}</span>
                            <small>
                              可能变化：{assertion.may_change.join(" · ")}
                              <br />明确不变：{assertion.will_not_change.join(" · ")}
                              <br />接纳前提：{assertion.preconditions.join(" · ")}
                              <br />风险：{assertion.risks.join(" · ")}
                              <br />失效条件：{assertion.stale_if.join(" · ")}
                            </small>
                          </li>
                        ))}
                      </ul>
                    </>
                  ) : (
                    <p className="preview-empty">尚未生成预览；在预览 current 前不能确认。</p>
                  )}
                </section>

                <div className="form-actions proposal-actions">
                  <button
                    className="quiet-button"
                    type="button"
                    disabled={busy || creation?.status === "dispatching" || terminal}
                    onClick={() => setEditingBasis(true)}
                  >
                    修改 Quest 基底
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={busy || creation?.status === "dispatching" || terminal}
                    onClick={() => void generate()}
                  >
                    重新生成
                  </button>
                  <button
                    className="quiet-button"
                    type="button"
                    disabled={!proposalDirty || !proposalComplete || busy || terminal}
                    onClick={() => void saveEdits()}
                  >
                    保存修改
                  </button>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={!proposalComplete || busy || terminal}
                    onClick={() => void preview()}
                  >
                    {creation?.confirmation_preview?.status === "stale"
                      ? "重新生成影响预览"
                      : "生成影响预览"}
                  </button>
                  <button
                    className="primary-button"
                    type="button"
                    disabled={
                      !proposalComplete ||
                      proposalDirty ||
                      busy ||
                      creation?.confirmation_preview?.status !== "current" ||
                      creation?.status === "dispatching" ||
                      terminal
                    }
                    onClick={() => void confirm()}
                  >
                    确认 Quest 与首问题
                  </button>
                </div>
              </section>
            )}

            {error ? <p className="creation-error" role="alert">{error}</p> : null}
          </div>

          <aside className="receipt-rail" aria-labelledby="receipt-title">
            <div>
              <p className="eyebrow">Exact acceptance chain</p>
              <h3 id="receipt-title">光谱绑定轨</h3>
              <p>确认、内容接纳、身份接纳与推进从不合并。</p>
            </div>
            <ol>
              {receiptLabels.map(([key, label, owner]) => {
                const receipt = creation?.receipts[key] ?? { status: "not_attempted" };
                return (
                  <li className={`receipt-step ${receipt.status}`} key={key}>
                    <span className="receipt-node" aria-hidden="true" />
                    <div>
                      <strong>{label}</strong>
                      <small>{owner}</small>
                      <code>{receipt.status}</code>
                    </div>
                  </li>
                );
              })}
            </ol>
            {creation?.canonical_empty_advancement ? (
              <div className="empty-advancement-note">
                <strong>Canonical Empty Advancement</strong>
                <p>Quest 已接纳；首问题或 Cycle 尚待从缺失 receipt 恢复。</p>
              </div>
            ) : null}
            {creation?.status === "completed" ? (
              <div className="completion-note" role="status">
                <strong>首个研究 Cycle 已建立</strong>
                <p>Quest、问题内容、问题身份与推进 receipt 均已独立接纳。</p>
              </div>
            ) : null}
          </aside>
        </div>
      </section>
    </dialog>
  );
}

function sameDraft(left: QuestDraft, right: QuestDraft): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function messageFor(caught: unknown): string {
  const code = caught instanceof ProductError ? caught.code : "unknown_error";
  const messages: Record<string, string> = {
    quest_draft_stale: "Quest 基底已变化。已停止确认，请重新生成问题。",
    question_proposal_stale: "当前问题已陈旧，请重新生成或复核后再确认。",
    research_memory_asset_intake_not_delivered:
      "材料接纳能力尚未交付；请先使用不依赖材料的 direct 路线。",
    csrf_token_unavailable: "会话写入凭据不可用，请重新打开认证页面。",
    idempotency_conflict: "这次操作与已提交内容不一致，请刷新后重试。",
  };
  return messages[code] ?? `操作未完成（${code}）。当前已接纳事实不会回滚。`;
}
