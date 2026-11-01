import { useEffect, useRef, useState, type FormEvent } from "react";
import { fetchResearchAssets, ProductError, sendCompanionMessage,
  type CompanionWritingMaterial, type CompanionWritingRequest,
  type ResearchAssetItem, type ResearchAssetsView } from "./api";
import { usePageDraft } from "./pageDraft";
import "./writing-request.css";

type SelectedMaterial = CompanionWritingMaterial & { name: string; version_number: number };
type Draft = Omit<CompanionWritingRequest, "materials"> & { materials: SelectedMaterial[] };
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
  const [inventory, setInventory] = useState(assets);
  const [materialRef, setMaterialRef] = useState("");
  const [materialRole, setMaterialRole] = useState<CompanionWritingMaterial["role"]>("source");
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const sendingRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const change = (patch: Partial<Draft>) => setDraft(current => ({ ...current, ...patch }));

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    return () => dialog.close();
  }, []);

  // Inventory refreshes never replace a material version already selected.
  useEffect(() => { setInventory(assets); }, [assets]);

  const addMaterial = () => {
    const material = inventory.items.find(item => item.version_ref === materialRef);
    if (!material) return;
    if (draft.materials.some(item => item.version_ref === material.version_ref && item.role === materialRole)) return;
    change({ materials: [...draft.materials, {
      version_ref: material.version_ref, content_hash: material.content_hash, role: materialRole,
      name: material.display_name, version_number: material.version_number,
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
      await sendCompanionMessage(draft.requirements, scopeRef, null, request);
      setDraft(initialDraft);
      onSent();
    } catch (caught) { setError(errorText(caught)); }
    finally { sendingRef.current = false; setSending(false); }
  };

  return <dialog ref={dialogRef} className="writing-request" aria-label="交给研究助手写作" onCancel={event => { event.preventDefault(); if (!sending) onClose(); }}>
    <header><div><p>研究助手</p><h1>写作与演示</h1></div><button type="button" onClick={onClose} disabled={sending} aria-label="关闭写作">关闭</button></header>
    <p className="writing-request-intro">说明你要交付的内容、读者和要求。助手会在本研究的持久目录中制作文件，你可以在同一对话中继续修改。</p>
    <form onSubmit={event => void submit(event)}>
      <fieldset disabled={sending}>
        <div className="writing-request-row">
          <label>文稿类型<select value={draft.document_type} onChange={event => change({ document_type: event.target.value as Draft["document_type"], ...(event.target.value === "presentation" ? { include_presentation: false } : {}) })}><option value="report">研究报告</option><option value="paper">学术文章</option><option value="presentation">演示稿</option></select></label>
          <label>工作范围<select value={draft.task_scope} onChange={event => change({ task_scope: event.target.value as Draft["task_scope"] })}><option value="full_document">完整文件</option><option value="outline">提纲</option><option value="revision">局部修改</option></select></label>
        </div>
        {draft.document_type !== "presentation" && <label className="writing-request-check"><input type="checkbox" checked={draft.include_presentation} onChange={event => change({ include_presentation: event.target.checked })} />同时制作配套 PPT</label>}
        <label>写作要求<textarea required rows={5} value={draft.requirements} onChange={event => change({ requirements: event.target.value })} placeholder="需要表达什么、给谁阅读、篇幅与格式、保留哪些结论或限制……" /></label>
        <section aria-label="本次写作材料">
          <h2>材料与模板</h2>
          <p>选择本次使用的准确版本；已选前稿会保持该版本。模板需要的图表、字体和书目可作为依赖一起选择。</p>
          <div className="writing-request-row">
            <label>材料用途<select value={materialRole} onChange={event => setMaterialRole(event.target.value as CompanionWritingMaterial["role"])}>{Object.entries(materialRoles).map(([role, label]) => <option key={role} value={role}>{label}</option>)}</select></label>
            <label>选择材料版本<select value={materialRef} onChange={event => setMaterialRef(event.target.value)}><option value="">请选择</option>{inventory.items.map(item => <option key={item.version_ref} value={item.version_ref}>{item.display_name} · 版本 {item.version_number}</option>)}</select></label>
            <button type="button" disabled={!materialRef} onClick={addMaterial}>加入本次写作</button>
          </div>
          {inventory.has_more && <button type="button" disabled={loading} onClick={() => void moreMaterials()}>{loading ? "正在读取…" : "更多材料"}</button>}
          {draft.materials.length ? <ul className="writing-request-materials">{draft.materials.map(item => <li key={`${item.role}:${item.version_ref}`}><span>{materialRoles[item.role]} · {item.name} · 版本 {item.version_number}</span><button type="button" aria-label={`移除 ${item.name} ${materialRoles[item.role]}`} onClick={() => change({ materials: draft.materials.filter(candidate => candidate !== item) })}>移除</button></li>)}</ul> : <p>尚未指定材料。也可在要求中说明需要助手查找的已有资料。</p>}
        </section>
        <div className="writing-request-row">
          <label>内容模型<select value={draft.model.kind} onChange={event => change({ model: event.target.value === "current" ? { kind: "current" } : { kind: "configured_api", config_ref: "" } })}><option value="current">当前 Codex 模型</option><option value="configured_api">已配置的 API 模型</option></select></label>
          {draft.model.kind === "configured_api" && <label>已配置模型引用<input required pattern="[A-Za-z0-9_-]{1,64}" autoComplete="off" value={draft.model.config_ref} onChange={event => change({ model: { kind: "configured_api", config_ref: event.target.value } })} placeholder="配置名称，不填写密钥" /></label>}
        </div>
        <label>输出子目录<input value={draft.output_directory ?? ""} onChange={event => change({ output_directory: event.target.value })} placeholder="留空使用助手目录" /></label>
        {workspacePath && <p className="writing-request-location">助手目录：{workspacePath}</p>}
      </fieldset>
      <footer>{error && <p role="alert">{error}</p>}<button type="submit" disabled={sending || !scopeRef || !draft.requirements.trim()}>{sending ? "正在交给助手…" : "开始写作"}</button></footer>
    </form>
  </dialog>;
}

function errorText(error: unknown) {
  return error instanceof ProductError ? error.code : "提交暂未完成，请保留要求后重试。";
}
