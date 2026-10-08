import { useEffect, useRef, useState } from "react";
import { ServerMaterialPicker } from "./ServerMaterialPicker";
import {
  deliverMaterialCommand, discardMaterialCommand, fetchMaterialReceiver, pendingMaterialCommand,
  stageMaterialCommand, workMaterialErrorMessage, WorkMaterialError, fetchWorkMaterialReference, fetchResearchInputMaterialReceipts,
  type MaterialReceiver, type PendingMaterialCommand, type ServerMaterialSelection,
  type WorkMaterialReceipt, type WorkMaterialSubmission,
} from "./workMaterialApi";

export function useWorkMaterialDraft(scope: string, receiverPath: string) {
  const [selection, setSelection] = useState<ServerMaterialSelection | null>(null);
  const [receiver, setReceiver] = useState<MaterialReceiver | null>(null);
  const [pending, setPending] = useState<PendingMaterialCommand | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [receipts, setReceipts] = useState<WorkMaterialReceipt[]>([]);
  const generation = useRef(0);
  const inFlight = useRef(false);
  const activeScope = useRef(scope);
  activeScope.current = scope;

  useEffect(() => {
    let active = true;
    generation.current += 1;
    setSelection(null); setReceiver(null); setPending(null);
    setError(null); setErrorCode(null); setErrorStatus(null); setReceipts([]); setBusy(true);
    void pendingMaterialCommand(scope).then(value => { if (active) setPending(value); }).catch(caught => {
      if (active) { setError(workMaterialErrorMessage(caught)); setErrorCode(caught instanceof WorkMaterialError ? caught.code : "material_pending_invalid"); }
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [scope]);

  async function select(value: ServerMaterialSelection | null) {
    const requestGeneration = ++generation.current;
    setSelection(value); setReceiver(null); setError(null); setErrorCode(null); setErrorStatus(null);
    if (!value) return;
    setBusy(true);
    try {
      const exact = await fetchMaterialReceiver(receiverPath);
      if (requestGeneration === generation.current) setReceiver(exact);
    } catch (caught) {
      if (requestGeneration === generation.current) setError(workMaterialErrorMessage(caught));
    } finally {
      if (requestGeneration === generation.current) setBusy(false);
    }
  }

  function materialCommand(): WorkMaterialSubmission | null {
    if (!selection) return null;
    if (!receiver) throw new WorkMaterialError("material_receiver_unavailable");
    return { receiver, selections: [selection], description: selection.description };
  }

  async function deliver(command: PendingMaterialCommand) {
    if (inFlight.current) return null;
    inFlight.current = true; setBusy(true); setError(null); setErrorCode(null); setErrorStatus(null);
    try {
      const result = await deliverMaterialCommand(command);
      if (activeScope.current === command.scope) {
        setPending(null); setSelection(null); setReceiver(null);
        const saved = Array.isArray(result.work_materials) ? result.work_materials as WorkMaterialReceipt[]
          : Array.isArray(result.references) ? [result as unknown as WorkMaterialReceipt] : [];
        setReceipts(saved);
      }
      return result;
    } catch (caught) {
      if (activeScope.current === command.scope) {
        setError(workMaterialErrorMessage(caught));
        setErrorCode(caught instanceof WorkMaterialError ? caught.code : null);
        setErrorStatus(caught instanceof WorkMaterialError ? caught.httpStatus ?? null : null);
      }
      return null;
    } finally { inFlight.current = false; if (activeScope.current === command.scope) setBusy(false); }
  }

  async function submit(path: string, body: Record<string, unknown>) {
    try {
      const command = await stageMaterialCommand(scope, path, body);
      setPending(command);
      return await deliver(command);
    } catch (caught) { setError(workMaterialErrorMessage(caught)); return null; }
  }

  async function retry() { return pending ? deliver(pending) : null; }

  async function discard() {
    try {
      await discardMaterialCommand(scope); setPending(null); setReceiver(null); setSelection(null); setError(null); setErrorCode(null); setErrorStatus(null);
    } catch (caught) { setError(workMaterialErrorMessage(caught)); }
  }

  return { selection, receiver, pending, busy, error, errorCode, errorStatus, receipts, select, materialCommand, submit, retry, discard };
}

export type WorkMaterialDraft = ReturnType<typeof useWorkMaterialDraft>;

export function ReceivingIdentity({ receiver }: { receiver: MaterialReceiver }) {
  return <details className="work-material-receiver"><summary>材料接收位置 · {receiver.kind === "creation" ? "Quest 创建草稿" : receiver.kind === "manual" ? "手动创建上下文" : receiver.kind === "request" ? "求助原始工作" : "当前研究工作"}</summary><pre>{JSON.stringify(receiver, null, 2)}</pre></details>;
}

export function WorkMaterialReferences({ receipts }: { receipts: readonly WorkMaterialReceipt[] }) {
  return <div className="work-material-references">{receipts.flatMap(receipt => receipt.references).map(reference => <article className="server-material-picker__candidate" key={reference.reference_ref}>
    <b>{reference.read_state === "read" ? "已保存，存在成功读取的字节范围" : "已保存，尚未读取"}</b>
    <code>{reference.source.absolute_path}</code>
    <span>{reference.source.server.hostname} · {reference.source.server.platform} · 运行账户 {reference.source.server.permission_context}</span>
    {reference.source.description && <p>{reference.source.description}</p>}
    <ReceivingIdentity receiver={reference.receiver} />
    <small>{reference.unexpanded ? "目录未整体展开。" : ""}读取记录不代表理解或研究采用。</small>
    {reference.availability !== "available" && <p role="status">{workMaterialErrorMessage(new WorkMaterialError(reference.availability))}</p>}
    {reference.read_ranges.length > 0 && <details><summary>成功读取范围</summary><ul>{reference.read_ranges.map((range, index) => <li key={index}>{range.path || reference.source.absolute_path} · 从字节 {range.offset} 起读取 {range.bytes} 字节</li>)}</ul></details>}
    {reference.failures.length > 0 && <details><summary>来源访问失败记录</summary><ul>{reference.failures.map((failure, index) => <li key={index}>{failure.path} · {workMaterialErrorMessage(new WorkMaterialError(failure.error))}</li>)}</ul></details>}
  </article>)}</div>;
}

export function ResearchInputMaterialReferences({ inputRef, questRef }: { inputRef: string; questRef: string }) {
  const [receipts, setReceipts] = useState<WorkMaterialReceipt[]>([]);
  const [error, setError] = useState<string | null>(null);
  return <details onToggle={event => {
    if (!event.currentTarget.open) return;
    void fetchResearchInputMaterialReceipts(inputRef, questRef).then(setReceipts).catch(caught => setError(workMaterialErrorMessage(caught)));
  }}><summary>已保存的工作材料引用</summary><WorkMaterialReferences receipts={receipts} />{error && <p role="alert">{error}</p>}</details>;
}

export function HumanRequestMaterialReferences({ responses }: { responses: readonly Record<string, unknown>[] }) {
  const [receipts, setReceipts] = useState<WorkMaterialReceipt[]>([]);
  const [error, setError] = useState<string | null>(null);
  const refs = responses.flatMap(response => {
    const delivery = response.delivery as { work_materials?: Array<{ reference_ref: string }> } | undefined;
    return delivery?.work_materials?.map(item => item.reference_ref) ?? [];
  });
  const identity = refs.join("|");
  useEffect(() => {
    let active = true;
    setError(null);
    void Promise.all(refs.map(fetchWorkMaterialReference)).then(references => {
      if (active) setReceipts(references.map(reference => ({ submission_ref: reference.submission_ref, receiver: reference.receiver, references: [reference] })));
    }).catch(caught => { if (active) setError(workMaterialErrorMessage(caught)); });
    return () => { active = false; };
  }, [identity]);
  return <><WorkMaterialReferences receipts={receipts} />{error && <p role="alert">{error}</p>}</>;
}

export function WorkMaterialFields({ draft, disabled = false, onRetried }: { draft: WorkMaterialDraft; disabled?: boolean; onRetried?: (result: Record<string, unknown>) => void }) {
  const command = draft.pending?.body.work_materials as WorkMaterialSubmission | undefined;
  const explicit = draft.pending?.body.receiver as MaterialReceiver | undefined;
  return <div className="work-material-fields">
    <ServerMaterialPicker value={draft.selection} onSelect={value => void draft.select(value)} disabled={disabled || draft.busy || !!draft.pending || draft.errorCode === "material_pending_invalid"} />
    {draft.receiver && <ReceivingIdentity receiver={draft.receiver} />}
    {draft.pending && <div role="status" className="server-material-picker__candidate">
      <b>待重试的封存提交</b><p>重试保留原始正文、材料及接收位置。</p>
      {(command?.receiver ?? explicit) && <ReceivingIdentity receiver={(command?.receiver ?? explicit)!} />}
      <details><summary>封存提交原文</summary><pre>{JSON.stringify(draft.pending.body, null, 2)}</pre></details>
      <button type="button" disabled={disabled || draft.busy} onClick={() => void draft.retry().then(result => { if (result) onRetried?.(result); })}>重试封存提交</button>
      {draft.errorStatus === 409 && draft.errorCode && ["material_receiver_stale", "material_receiver_closed", "material_source_changed", "material_missing", "material_not_readable", "material_path_invalid", "material_safe_reader_unavailable", "material_selection_invalid", "material_unsafe"].includes(draft.errorCode) && <button type="button" disabled={draft.busy} onClick={draft.discard}>放弃未接受的命令，重新选择</button>}
    </div>}
    {draft.error && <p role="alert" className="server-material-picker__error">{draft.error}</p>}
    {draft.errorCode === "material_pending_invalid" && <button type="button" disabled={draft.busy} onClick={() => void draft.discard()}>明确放弃损坏的本地封存记录</button>}
    <WorkMaterialReferences receipts={draft.receipts} />
  </div>;
}
