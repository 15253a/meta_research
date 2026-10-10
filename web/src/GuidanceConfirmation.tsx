import { useEffect, useRef, useState } from "react";
import { ProductError, type CompanionAgentProposal, type GuidanceStrength } from "./api";
import { confirmGuidanceProposal, reviseGuidanceProposal, reviewedGuidance, validGuidanceScope, type ReviewedGuidance } from "./guidanceApi";
import { ServerMaterialPicker } from "./ServerMaterialPicker";
import { ReceivingIdentity } from "./WorkMaterialForm";
import { currentMaterialReceiverPath, fetchMaterialReceiver, type ServerMaterialSelection } from "./workMaterialApi";
import "./guidance-confirmation.css";

type ReviewDraft = { proposal: CompanionAgentProposal; guidance: ReviewedGuidance; scopeJson: string; cancelled: boolean };
const drafts = new Map<string, ReviewDraft>();
const lines = (value: string) => value.split("\n").map(line => line.trim()).filter(Boolean);

export function GuidanceConfirmation({ proposal, onChanged }: { proposal: CompanionAgentProposal; onChanged: () => void }) {
  const initial = reviewedGuidance(proposal.proposal);
  const identity = `${proposal.scope_ref}:${proposal.proposal_ref}:${proposal.proposal_hash}`;
  const [review, setReview] = useState<ReviewDraft | null>(() => drafts.get(identity) ?? (initial ? {
    proposal, guidance: initial, scopeJson: JSON.stringify(initial.semantic_scope, null, 2), cancelled: false,
  } : null));
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const materialGeneration = useRef(0);
  const active = useRef(true);
  useEffect(() => { active.current = true; return () => { active.current = false; materialGeneration.current += 1; }; }, []);
  useEffect(() => { if (review) drafts.set(identity, review); }, [identity, review]);
  const current = reviewedGuidance(review?.proposal.proposal);
  let scope = review?.guidance.semantic_scope ?? null;
  try { const parsed: unknown = JSON.parse(review?.scopeJson ?? "null"); scope = validGuidanceScope(parsed) ? parsed : null; }
  catch { scope = null; }
  const content = review && scope ? { ...review.guidance, semantic_scope: scope } : null;
  const dirty = !content || JSON.stringify(content) !== JSON.stringify(current);
  const materialScopeMatches = !content?.work_materials || (content.work_materials.receiver.quest_ref === scope?.quest_ref
    && (!scope?.question_ref || content.work_materials.receiver.question_ref === scope.question_ref));
  const valid = Boolean(content?.text.trim() && content.assistant_understanding.trim() && content.applies_to.length && materialScopeMatches);
  const proposed = review?.proposal.proposal_ref !== proposal.proposal_ref
    ? review?.proposal.status === "proposed" : proposal.status === "proposed";
  const change = (patch: Partial<ReviewedGuidance>) => { setError(null); setReview(value => value ? { ...value, guidance: { ...value.guidance, ...patch } } : value); };
  const save = async () => {
    if (pendingRef.current || !content || !valid || !review) return;
    pendingRef.current = true; setPending(true); setError(null);
    try {
      const revised = await reviseGuidanceProposal(review.proposal, content);
      const guidance = reviewedGuidance(revised.proposal);
      if (!guidance) throw new ProductError("guidance_confirmation_required");
      if (active.current) setReview({ proposal: revised, guidance, scopeJson: JSON.stringify(guidance.semantic_scope, null, 2), cancelled: false });
      onChanged();
    } catch (caught) { if (active.current) setError(caught instanceof ProductError ? caught.code : "guidance_revision_failed"); }
    finally { pendingRef.current = false; if (active.current) setPending(false); }
  };
  const confirm = async () => {
    if (pendingRef.current || dirty || !valid || !review) return;
    pendingRef.current = true; setPending(true); setError(null);
    try {
      await confirmGuidanceProposal(review.proposal, review.guidance.strength);
      if (active.current) setConfirmed(true);
      drafts.delete(identity); onChanged();
    } catch (caught) { if (active.current) setError(caught instanceof ProductError ? caught.code : "guidance_confirmation_failed"); }
    finally { pendingRef.current = false; if (active.current) setPending(false); }
  };
  const selectMaterial = async (selection: ServerMaterialSelection | null) => {
    const generation = ++materialGeneration.current;
    if (!selection) { change({ work_materials: null }); return; }
    if (!scope) { setError("请先核对有效的作用范围，再选择材料。"); return; }
    pendingRef.current = true; setPending(true); setError(null);
    try {
      const receiver = await fetchMaterialReceiver(currentMaterialReceiverPath(scope.quest_ref, scope.question_ref));
      if (active.current && generation === materialGeneration.current) change({ work_materials: { receiver, selections: [selection], description: selection.description } });
    } catch (caught) { if (active.current) setError(caught instanceof Error ? caught.message : "material_receiver_unavailable"); }
    finally { pendingRef.current = false; if (active.current) setPending(false); }
  };
  if (!review) return <article className="lumen-proposal guidance-confirmation"><b>指导需要补充理解与作用范围</b><p>{String(proposal.proposal?.text ?? proposal.content ?? "")}</p><p>这份旧建议缺少完整确认依据。请向研究助手澄清，再审阅新的建议。</p></article>;
  if (review.cancelled) return <article className="lumen-proposal guidance-confirmation"><p>已取消本次审阅；没有交给研究。</p><button type="button" onClick={() => setReview({ ...review, cancelled: false })}>继续审阅指导</button></article>;
  return <article className="lumen-proposal guidance-confirmation" aria-label="确认研究指导">
    <small>理解预览 · {confirmed ? "已确认" : "尚未交给研究"}</small>
    <b>{proposal.title ?? "确认研究指导"}</b>
    <label>指导原文<textarea aria-label="指导原文" value={review.guidance.text} rows={3} disabled={pending || confirmed || !proposed} onChange={event => change({ text: event.target.value })} /></label>
    <label>助手理解<textarea aria-label="助手理解" value={review.guidance.assistant_understanding} rows={3} disabled={pending || confirmed || !proposed} onChange={event => change({ assistant_understanding: event.target.value })} /></label>
    <label>拟作用范围<textarea aria-label="拟作用范围" value={review.guidance.applies_to.join("\n")} rows={2} disabled={pending || confirmed || !proposed} onChange={event => change({ applies_to: lines(event.target.value) })} /></label>
    <dl><dt>接收研究范围</dt><dd>{scope ? `${scope.kind} · ${[scope.quest_ref, scope.question_ref, scope.cycle_ref, scope.target_ref].filter(Boolean).join(" · ")}` : "范围身份无效，请核对。"}</dd></dl>
    <details><summary>核对或修改精确范围身份</summary><textarea aria-label="指导范围身份" value={review.scopeJson} rows={6} disabled={pending || confirmed || !proposed} onChange={event => setReview({ ...review, scopeJson: event.target.value })} /></details>
    <label>确认指导力度<select aria-label="确认指导力度" value={review.guidance.strength} disabled={pending || confirmed || !proposed} onChange={event => change({ strength: Number(event.target.value) as GuidanceStrength })}>{["供参考", "有所倾向", "优先考虑", "强烈要求", "按我设定"].map((label, index) => <option key={index} value={index + 1}>{index + 1} · {label}</option>)}</select></label>
    <label>保留限制<textarea aria-label="保留限制" value={review.guidance.preserve_conditions.join("\n")} rows={2} disabled={pending || confirmed || !proposed} onChange={event => change({ preserve_conditions: lines(event.target.value) })} /></label>
    <ServerMaterialPicker value={review.guidance.work_materials?.selections[0] ?? null} onSelect={selection => void selectMaterial(selection)} disabled={pending || confirmed || !proposed} />
    {review.guidance.work_materials ? <ReceivingIdentity receiver={review.guidance.work_materials.receiver} /> : null}
    {!materialScopeMatches ? <p role="alert">作用范围已变化，请重新选择材料以核对其精确接收位置。</p> : null}
    <p>力度只约束确认范围；材料引用不表示已读、采用或研究完成。</p>
    <details><summary>当前确认版本</summary><code>{review.proposal.proposal_ref} · {review.proposal.proposal_hash}</code></details>
    {confirmed ? <p role="status">已确认保存。等待适用研究工作接续；送达、读取和实际影响以研究回执为准。</p> : proposed ? <div className="guidance-confirmation-actions">
      {dirty ? <button type="button" disabled={pending || !valid} onClick={() => void save()}>保存修改并重新审阅</button> : null}
      <button type="button" disabled={pending || dirty || !valid} onClick={() => void confirm()}>明确确认并交给研究</button>
      <button type="button" disabled={pending} onClick={() => setReview({ ...review, cancelled: true })}>取消审阅</button>
    </div> : <p role="status">这份建议已失效或已转换，请查看当前指导及回执。</p>}
    {dirty && !confirmed ? <p role="status">内容已修改，旧确认依据已失效。保存后请重新审阅。</p> : null}
    {error ? <p role="alert">{error} · 保留当前内容供核对；重试仍使用同一请求身份。</p> : null}
  </article>;
}
