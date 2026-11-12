import { useEffect, useRef, useState, type FormEvent } from "react";
import { fetchResearchAssets, ProductError, provisionCompanionContentCredential, sendCompanionMessage,
  type CompanionWritingMaterial, type CompanionWritingRequest,
  type ResearchAssetItem, type ResearchAssetsView } from "./api";
import { usePageDraft } from "./pageDraft";
import { writingMaterialLabel } from "./writingMaterialLabel";
import "./writing-request.css";

type SelectedMaterial = CompanionWritingMaterial & { name: string; version_number: number };
type Draft = Omit<CompanionWritingRequest, "materials"> & { materials: SelectedMaterial[] };
type TaskApiModel = Extract<CompanionWritingRequest["model"], { kind: "task_api" }>;
const initialDraft: Draft = {
  document_type: "report", include_presentation: false, task_scope: "full_document",
  requirements: "", materials: [], model: { kind: "current" }, output_directory: null,
};
const materialRoles: Record<CompanionWritingMaterial["role"], string> = {
  source: "研究材料", draft: "前稿", feedback: "修改反馈", document_template: "主文模板",
  presentation_template: "PPT 模板", dependency: "模板依赖、图表或书目",
};

export function WritingRequestForm({ assets, scopeRef, workspacePath, onClose, onSent }: {
  assets: ResearchAssetsView;
  scopeRef: string | null;
  workspacePath: string | null;
  onClose: () => void;
  onSent: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = usePageDraft<Draft>(`companion-writing:${scopeRef}`, initialDraft);
  const [credentialMode, setCredentialMode] = useState<"key" | "reference">(
    () => draft.model.kind === "task_api" && draft.model.api_key_ref ? "reference" : "key");
  // Kept outside the resumable draft: closing this dialog discards an unsubmitted key.
  const [apiKey, setApiKey] = useState("");
  const [inventory, setInventory] = useState(assets);
  const [materialRef, setMaterialRef] = useState("");
  const [materialRole, setMaterialRole] = useState<CompanionWritingMaterial["role"]>("source");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [savingCredential, setSavingCredential] = useState(false);
  const [credentialNotice, setCredentialNotice] = useState<string | null>(null);
  const busy = sending || savingCredential;
  const sendingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const change = (patch: Partial<Draft>) => setDraft(current => ({ ...current, ...patch }));
  const changeTaskApi = (patch: Partial<TaskApiModel>) => setDraft(current => current.model.kind === "task_api"
    ? { ...current, model: { ...current.model, ...patch } } : current);

  const saveCredential = async (model: TaskApiModel): Promise<TaskApiModel> => {
    if (!scopeRef) throw new ProductError("writing_credential_not_saved");
    let credential: { api_key_ref: string };
    try {
      credential = await provisionCompanionContentCredential({ scope_ref: scopeRef,
        endpoint: model.endpoint, model: model.model, api_key: apiKey,
        ...(/^writing-[a-f0-9]{32}$/.test(model.api_key_ref) ? { api_key_ref: model.api_key_ref } : {}) });
    } finally { setApiKey(""); }
    const selected = { ...model, api_key_ref: credential.api_key_ref };
    change({ model: selected });
    setCredentialMode("reference");
    return selected;
  };

  const saveCredentialOnly = async () => {
    if (sendingRef.current || draft.model.kind !== "task_api" || !apiKey) return;
    sendingRef.current = true; setSavingCredential(true); setError(null); setCredentialNotice(null);
    try {
      await saveCredential(draft.model);
      setCredentialNotice("密钥已保存。可关闭窗口，在原对话中继续本次工作。");
    } catch (caught) { setError(errorText(caught)); }
    finally { sendingRef.current = false; setSavingCredential(false); }
  };

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, []);

  // Inventory refreshes never replace a material version already selected.
  useEffect(() => { setInventory(assets); }, [assets]);
  useEffect(() => { setApiKey(""); }, [scopeRef]);

  const addMaterial = () => {
    const material = inventory.items.find(item => item.version_ref === materialRef);
    if (!material) return;
    if (draft.materials.some(item => item.version_ref === material.version_ref && item.role === materialRole)) return;
    change({ materials: [...draft.materials, {
      version_ref: material.version_ref, content_hash: material.content_hash, role: materialRole,
      name: writingMaterialLabel(material), version_number: material.version_number,
    }] });
  };

  const moreMaterials = async () => {
    if (loading) return;
    setLoading(true); setError(null);
    try {
      const next = await fetchResearchAssets(undefined, inventory.offset + inventory.items.length, inventory.limit);
      const items = new Map<string, ResearchAssetItem>(inventory.items.map(item => [item.version_ref, item]));
      next.items.forEach(item => items.set(item.version_ref, item));
      setInventory({ ...next, offset: inventory.offset, items: [...items.values()] });
    } catch (caught) { setError(errorText(caught)); }
    finally { setLoading(false); }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (sendingRef.current || !scopeRef || !draft.requirements.trim()) return;
    sendingRef.current = true; setSending(true); setError(null);
    const request: CompanionWritingRequest = {
      ...draft,
      materials: draft.materials.map(({ version_ref, content_hash, role }) => ({ version_ref, content_hash, role })),
      output_directory: draft.output_directory?.trim() || null,
    };
    try {
      if (request.model.kind === "task_api" && credentialMode === "key") {
        request.model = await saveCredential(request.model);
      }
      await sendCompanionMessage(draft.requirements, scopeRef, null, request);
      setDraft(initialDraft);
      onSent();
    } catch (caught) { setError(errorText(caught)); }
    finally { sendingRef.current = false; setSending(false); }
  };

  return <dialog ref={dialogRef} className="writing-request" aria-label="交给研究助手写作" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <header><div><p>研究助手</p><h1>写作与演示</h1></div><button type="button" onClick={onClose} disabled={busy} aria-label="关闭写作">关闭</button></header>
    <p className="writing-request-intro">说明你要交付的内容、读者和要求。助手会从你有权访问的全部研究材料中按需取材，尽量依据开始写作时的研究状态完成文件，并保留文件供后续修改。</p>
    <form onSubmit={event => void submit(event)}>
      <fieldset disabled={busy}>
        <div className="writing-request-row">
          <label>文稿类型<select value={draft.document_type} onChange={event => change({ document_type: event.target.value as Draft["document_type"], ...(event.target.value === "presentation" ? { include_presentation: false } : {}) })}><option value="report">研究报告</option><option value="paper">学术文章</option><option value="presentation">演示稿</option></select></label>
          <label>工作范围<select value={draft.task_scope} onChange={event => change({ task_scope: event.target.value as Draft["task_scope"] })}><option value="full_document">完整文件</option><option value="outline">提纲</option><option value="revision">局部修改</option></select></label>
        </div>
        {draft.document_type !== "presentation" && <label className="writing-request-check"><input type="checkbox" checked={draft.include_presentation} onChange={event => change({ include_presentation: event.target.checked })} />同时制作配套 PPT</label>}
        <label>写作要求<textarea required rows={5} value={draft.requirements} onChange={event => change({ requirements: event.target.value })} placeholder="需要表达什么、给谁阅读、篇幅与格式、保留哪些结论或限制……" /></label>
        <section aria-label="本次写作材料">
          <h2>材料与模板（可选）</h2>
          <p>需要指定前稿、模板或重点材料时，可在这里选入具体版本。已选版本会保留；模板需要的图表、字体和书目可一起加入。</p>
          <div className="writing-request-row">
            <label>材料用途<select value={materialRole} onChange={event => setMaterialRole(event.target.value as CompanionWritingMaterial["role"])}>{Object.entries(materialRoles).map(([role, label]) => <option key={role} value={role}>{label}</option>)}</select></label>
            <label>选择材料版本<select value={materialRef} onChange={event => setMaterialRef(event.target.value)}><option value="">请选择</option>{inventory.items.map(item => <option key={item.version_ref} value={item.version_ref}>{writingMaterialLabel(item)} · 版本 {item.version_number}</option>)}</select></label>
            <button type="button" disabled={!materialRef} onClick={addMaterial}>加入本次写作</button>
          </div>
          {inventory.has_more && <button type="button" disabled={loading} onClick={() => void moreMaterials()}>{loading ? "正在读取…" : "更多材料"}</button>}
          {draft.materials.length ? <ul className="writing-request-materials">{draft.materials.map(item => <li key={`${item.role}:${item.version_ref}`}><span>{materialRoles[item.role]} · {item.name} · 版本 {item.version_number}</span><button type="button" aria-label={`移除 ${item.name} ${materialRoles[item.role]}`} onClick={() => change({ materials: draft.materials.filter(candidate => candidate !== item) })}>移除</button></li>)}</ul> : <p>未手动指定材料，助手将按写作要求查找和选用相关研究资料。</p>}
        </section>
        <div className="writing-request-row">
          <label>内容模型<select value={draft.model.kind} onChange={event => { setApiKey(""); setCredentialMode("key"); change({ model: event.target.value === "current" ? { kind: "current" } : event.target.value === "task_api"
            ? { kind: "task_api", endpoint: "", model: "", protocol: "openai_chat_completions", api_key_ref: "" }
            : { kind: "configured_api", config_ref: "" } }); }}><option value="current">当前 Codex 模型</option><option value="task_api">本次指定 API 模型</option><option value="configured_api">已配置的 API 模型</option></select></label>
          {draft.model.kind === "configured_api" && <label>已配置模型引用<input required pattern="[A-Za-z0-9_\-]{1,64}" autoComplete="off" value={draft.model.config_ref} onChange={event => change({ model: { kind: "configured_api", config_ref: event.target.value } })} placeholder="配置名称，不填写密钥" /></label>}
        </div>
        {draft.model.kind === "task_api" && <section aria-label="本次 API 模型">
          <p>选择你要用于本次写作的服务与模型。调用失败时会保留任务，并说明原因。</p>
          <label>API 地址<input type="url" required maxLength={2048} autoComplete="off" value={draft.model.endpoint} onChange={event => changeTaskApi({ endpoint: event.target.value })} placeholder="完整接口地址，例如 https://api.example/v1/chat/completions" /></label>
          <div className="writing-request-row">
            <label>模型名称<input required maxLength={256} autoComplete="off" value={draft.model.model} onChange={event => changeTaskApi({ model: event.target.value })} placeholder="服务提供的准确模型名称" /></label>
            <label>接口协议<select value={draft.model.protocol} onChange={event => changeTaskApi({ protocol: event.target.value as TaskApiModel["protocol"] })}>
              <option value="openai_chat_completions">OpenAI Chat Completions（文本）</option>
              <option value="openai_responses">OpenAI Responses（文本）</option>
              <option value="anthropic_messages">Anthropic Messages（文本）</option>
            </select></label>
          </div>
          <label>凭据来源<select value={credentialMode} onChange={event => { setApiKey(""); setCredentialNotice(null); setCredentialMode(event.target.value as "key" | "reference"); }}><option value="key">输入 API 密钥</option><option value="reference">使用已有凭据引用</option></select></label>
          {credentialMode === "key"
            ? <label>API 密钥<input type="password" required maxLength={8192} autoComplete="new-password" value={apiKey} onChange={event => setApiKey(event.target.value)} aria-describedby="writing-credential-help" /></label>
            : <label>凭据引用<input required pattern="[A-Za-z0-9_\-]{1,64}" maxLength={64} autoComplete="off" value={draft.model.api_key_ref} onChange={event => changeTaskApi({ api_key_ref: event.target.value })} placeholder="例如 writing-key，不填写密钥" aria-describedby="writing-credential-help" /></label>}
          {credentialMode === "key" && <>
            {/^writing-[a-f0-9]{32}$/.test(draft.model.api_key_ref) && <p>替换此引用的密钥：{draft.model.api_key_ref}。地址与模型须保持原绑定。</p>}
            <button type="button" disabled={!apiKey || !draft.model.endpoint.trim() || !draft.model.model.trim()} onClick={() => void saveCredentialOnly()}>{savingCredential ? "正在保存…" : "保存密钥"}</button>
          </>}
          {credentialNotice && <p role="status">{credentialNotice}</p>}
          <p id="writing-credential-help">密钥单独保存，仅授权本项目使用上述地址与模型；对话和写作任务只保留引用。关闭窗口会清除尚未提交的密钥。请勿将密钥填入地址、说明或写作要求。</p>
          <label>调用说明（可选）<textarea rows={2} maxLength={12000} value={draft.model.instructions ?? ""} onChange={event => changeTaskApi({ instructions: event.target.value })} placeholder="本次模型需要遵循的内容要求；不填写密钥" /></label>
        </section>}
        <label>输出子目录<input value={draft.output_directory ?? ""} onChange={event => change({ output_directory: event.target.value })} placeholder="留空使用助手目录" /></label>
        {workspacePath && <p className="writing-request-location">助手目录：{workspacePath}</p>}
      </fieldset>
      <footer>{error && <p role="alert">{error}</p>}<button type="submit" disabled={busy || !scopeRef || !draft.requirements.trim()}>{sending ? "正在交给助手…" : "开始写作"}</button></footer>
    </form>
  </dialog>;
}

function errorText(error: unknown) {
  if (error instanceof ProductError && error.code === "writing_credential_not_saved") {
    return "凭据未确认保存。请核对接口地址与登录状态后重新输入密钥；写作要求已保留。";
  }
  if (error instanceof ProductError && error.code === "writing_model_unavailable") {
    return "所选模型暂时不可用，写作要求已保留。请核对模型设置后继续。";
  }
  return error instanceof ProductError ? error.code : "提交暂未完成，请保留要求后重试。";
}
