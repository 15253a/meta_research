import { useEffect, useRef, useState } from "react";
import {
  fetchSearchSources, fetchSearchSourceSelection, ProductError, saveSearchSource, saveSearchSourceSelection, testSearchSource,
  type ExternalMcpConnection, type SearchSourceApiContract, type SearchSourceForm, type SearchSourceSafeForm,
  type SearchSourceScope, type SearchSourceSelection, type SearchSourceTestReport, type SearchSourceView,
} from "./api";
import "./search-sources.css";

type Props = {
  scope: SearchSourceScope | { kind: "shared-only" };
  onEnsureInitialization?: () => Promise<string>;
  onSelectionSaved?: () => void;
};
type EditorDraft =
  | { kind: "website"; name: string; instructions: string; url: string }
  | { kind: "api"; name: string; instructions: string; template: "crossref" | "custom"; contract: SearchSourceApiContract;
      fixedParameters: string; credentialMode: "keep" | "replace" | "clear"; credentialValue: string }
  | { kind: "mcp"; name: string; instructions: string; connectionMode: "keep" | "replace"; connectionJson: string };
type Editor = { draft: EditorDraft; source?: SearchSourceView; report: SearchSourceTestReport | null };
type Operation = "idle" | "testing" | "saving-source" | "saving-selection";
const sourceDrafts = new Map<string, { selection: SearchSourceSelection | null; allowed: string[]; editor: Editor | null; probeQuery: string }>();
const capabilityNames: Record<string, string> = {
  connection: "连接", authentication: "认证", search: "搜索", abstract: "摘要", page_read: "网页读取", fulltext: "论文全文",
};
const statusNames = { verified: "已验证", unverified: "未验证", limited: "受限", failed: "失败" };
const reasonNames: Record<string, string> = {
  not_probed: "本次测试未验证此能力", not_tested: "当前配置尚未测试", http_response: "服务器已返回响应",
  not_required: "此接口不需要认证", not_required_or_unknown: "未验证认证要求", catalog_discovered: "已完成初始化和工具目录发现",
  catalog_only: "只发现目录，未验证业务能力", credential_request_accepted_without_authentication_proof: "带凭据的请求被接受，尚不能独立证明认证有效",
  parsed_results: "已解析查询结果", empty_results: "查询返回空结果", mapped_abstract: "已解析结果中的摘要",
  page_read_only: "仅验证网页读取，未验证站内搜索或论文全文", login_or_challenge: "页面要求登录或验证码", no_readable_content: "未取得可读正文",
  authentication_failed: "服务拒绝认证，请检查凭据", credential_missing: "认证方式需要配置 API Key", rate_limited: "服务限流，请稍后重试",
  parse_failed: "响应与结果数组或字段映射不匹配", connection_failed: "连接失败，请检查地址和服务状态", timeout: "请求超时",
  page_format_unsupported: "响应不是可读取的网页格式", response_too_large: "响应超过允许大小", cross_origin_redirect: "认证请求发生跨站重定向，已停止",
  redirect_invalid: "服务返回无效重定向", mcp_probe_failed: "MCP 初始化或工具发现失败", mcp_runtime_unavailable: "MCP 运行环境暂不可用",
};
const emptyContract = (): SearchSourceApiContract => ({
  endpoint: "", method: "GET", query_parameter: "q", limit_parameter: "limit", fixed_parameters: {}, auth: { kind: "none" },
  items_path: ["items"], fields: { title: ["title"], url: ["url"] }, result_kind: "paper_metadata",
});
function newDraft(kind: EditorDraft["kind"]): EditorDraft {
  const common = { name: "", instructions: "" };
  if (kind === "website") return { ...common, kind, url: "" };
  if (kind === "mcp") return { ...common, kind, connectionMode: "replace", connectionJson: "" };
  return { ...common, kind, template: "crossref", contract: emptyContract(), fixedParameters: "{}", credentialMode: "clear", credentialValue: "" };
}
function editSource(source: SearchSourceView): Editor {
  const form = source.form;
  let draft: EditorDraft;
  if (form.kind === "website") draft = { ...form };
  else if (form.kind === "mcp") draft = { kind: "mcp", name: form.name, instructions: form.instructions,
    connectionMode: source.connection_present ? "keep" : "replace", connectionJson: "" };
  else draft = { kind: "api", name: form.name, instructions: form.instructions, template: form.template,
    contract: form.template === "custom" ? form.contract : emptyContract(),
    fixedParameters: JSON.stringify(form.template === "custom" ? form.contract.fixed_parameters ?? {} : {}, null, 2),
    credentialMode: source.credential_present ? "keep" : "clear", credentialValue: "" };
  return { source, draft, report: source.needs_retest ? null : source.test };
}
function stringMap(value: unknown, label: string): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label}必须是 JSON 字符串对象。`);
  const result: Record<string, string> = {};
  for (const [key, item] of Object.entries(value)) {
    if (typeof item !== "string") throw new Error(`${label}必须是 JSON 字符串对象。`);
    result[key] = item;
  }
  return result;
}
function parseConnection(text: string): ExternalMcpConnection {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value) || !("transport" in value)) throw new Error("请填写完整的 MCP 连接 JSON。");
  if (value.transport === "streamable_http" && "url" in value && typeof value.url === "string") {
    return { transport: "streamable_http", url: value.url, headers: stringMap("headers" in value ? value.headers : {}, "请求头") };
  }
  if (value.transport === "stdio" && "command" in value && typeof value.command === "string") {
    const argumentsValue = "arguments" in value ? value.arguments : [];
    if (!Array.isArray(argumentsValue) || !argumentsValue.every((item): item is string => typeof item === "string")) throw new Error("MCP arguments 必须是字符串数组。");
    const directory = "working_directory" in value ? value.working_directory : undefined;
    if (directory !== undefined && typeof directory !== "string") throw new Error("MCP working_directory 必须是字符串。");
    return { transport: "stdio", command: value.command, arguments: argumentsValue,
      environment: stringMap("environment" in value ? value.environment : {}, "环境变量"),
      ...(directory ? { working_directory: directory } : {}) };
  }
  throw new Error("MCP transport 必须是 stdio 或 streamable_http，并填写对应命令或地址。");
}
function sourceForm(draft: EditorDraft): SearchSourceForm {
  const common = { name: draft.name, instructions: draft.instructions };
  if (draft.kind === "website") return { ...common, kind: "website", url: draft.url };
  if (draft.kind === "mcp") return { ...common, kind: "mcp", connection: draft.connectionMode === "keep"
    ? { mode: "keep" } : { mode: "replace", value: parseConnection(draft.connectionJson) } };
  const credential = draft.credentialMode === "replace"
    ? { mode: "replace" as const, value: draft.credentialValue } : { mode: draft.credentialMode };
  if (draft.template === "crossref") return { ...common, kind: "api", template: "crossref", credential: { mode: "clear" } };
  return { ...common, kind: "api", template: "custom", credential,
    contract: { ...draft.contract, fixed_parameters: stringMap(JSON.parse(draft.fixedParameters), "固定参数") } };
}
function isConflict(caught: unknown) {
  return caught instanceof ProductError && (/stale|conflict/.test(caught.code) || caught.code === "request_failed:409");
}
function failure(caught: unknown, action: string) {
  if (caught instanceof SyntaxError) return "JSON 配置无法解析，当前编辑已保留。";
  if (caught instanceof ProductError) return `${action}失败，请检查配置或稍后重试。当前编辑已保留。错误代码 ${caught.code}。`;
  return caught instanceof Error ? caught.message : `${action}失败，当前编辑已保留。`;
}
function scopeKey(scope: Props["scope"]) {
  return scope.kind === "initialization" ? `initialization:${scope.initializationId}` : scope.kind === "quest" ? `quest:${scope.questRef}` : "shared-only";
}
export function SearchSourcesSettings(props: Props) {
  return <SearchSourcesEditor key={scopeKey(props.scope)} {...props} />;
}
function SearchSourcesEditor({ scope, onEnsureInitialization, onSelectionSaved }: Props) {
  const alive = useRef(true);
  const identity = scopeKey(scope);
  const restored = sourceDrafts.get(identity);
  const [sources, setSources] = useState<SearchSourceView[]>([]);
  const [selection, setSelection] = useState<SearchSourceSelection | null>(null);
  const [allowed, setAllowed] = useState<string[]>(restored?.allowed ?? []);
  const [editor, setEditor] = useState<Editor | null>(restored?.editor ?? null);
  const [probeQuery, setProbeQuery] = useState(restored?.probeQuery ?? "deep learning");
  const [operation, setOperation] = useState<Operation>("idle");
  const [loading, setLoading] = useState(true);
  const [readAttempt, setReadAttempt] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selectionError, setSelectionError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<"source" | "selection" | null>(null);
  const [sharedReceipt, setSharedReceipt] = useState<{ name: string; version: number } | null>(null);
  const [selectionReceipt, setSelectionReceipt] = useState<number | null>(null);
  const busy = operation !== "idle";
  const selectionTarget = scope.kind !== "shared-only" || Boolean(onEnsureInitialization);
  const selectionDirty = selection === null ? allowed.length > 0 : JSON.stringify([...allowed].sort()) !== JSON.stringify([...selection.allowed_source_ids].sort());

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    let current = true;
    setLoading(true); setError(null); setSelectionError(null);
    void Promise.all([
      fetchSearchSources(controller.signal),
      scope.kind === "shared-only" ? Promise.resolve(null) : fetchSearchSourceSelection(scope, controller.signal),
    ]).then(([shared, scoped]) => {
      if (!current) return;
      const cached = sourceDrafts.get(identity);
      const latestSelection = scoped?.selection ?? null;
      const dirtySelection = cached && JSON.stringify([...cached.allowed].sort()) !== JSON.stringify([...(cached.selection?.allowed_source_ids ?? [])].sort());
      const staleSelection = dirtySelection && cached.selection?.revision !== latestSelection?.revision;
      const staleSource = cached?.editor?.source && shared.sources.find(source => source.source_id === cached.editor?.source?.source_id)?.version !== cached.editor.source.version;
      setSources(shared.sources); setSelection(dirtySelection ? cached.selection : latestSelection);
      setAllowed(dirtySelection ? cached.allowed : latestSelection?.allowed_source_ids ?? []);
      setEditor(cached?.editor ?? null); setProbeQuery(cached?.probeQuery ?? "deep learning");
      setConflict(staleSelection ? "selection" : staleSource ? "source" : null); setSharedReceipt(null); setSelectionReceipt(null);
      if (staleSelection) setSelectionError("当前研究的允许来源已变化。勾选草稿已保留，请显式读取最新选择后核对。");
      if (staleSource) setError("共享来源已被其他编辑更新。当前草稿已保留，请显式读取最新来源再核对。");
    }).catch(() => {
      if (current) setError("搜索源读取失败，请重试。当前编辑不会自动提交。");
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; alive.current = false; controller.abort(); };
  }, [readAttempt, identity]);

  useEffect(() => {
    if (!loading) sourceDrafts.set(identity, { selection, allowed, editor, probeQuery });
  }, [identity, selection, allowed, editor, probeQuery, loading]);

  const edit = (draft: EditorDraft) => {
    setEditor(previous => previous ? { ...previous, draft, report: null } : null);
    setError(null);
  };
  const test = async () => {
    if (!editor || busy) return;
    setOperation("testing"); setError(null); setEditor({ ...editor, report: null });
    try {
      const report = await testSearchSource(sourceForm(editor.draft), probeQuery, editor.source);
      if (alive.current) setEditor({ ...editor, report });
    } catch (caught) {
      if (alive.current) { setError(isConflict(caught) ? "共享来源已被其他编辑更新。当前草稿已保留，请显式读取最新来源再核对。" : failure(caught, "测试")); if (isConflict(caught)) setConflict("source"); }
    } finally { if (alive.current) setOperation("idle"); }
  };
  const saveShared = async () => {
    if (!editor || busy || conflict === "source") return;
    setOperation("saving-source"); setError(null);
    try {
      const result = await saveSearchSource(sourceForm(editor.draft), editor.source, editor.report?.test_ref);
      if (!alive.current) return;
      setSources(previous => [...previous.filter(source => source.source_id !== result.source.source_id), result.source]);
      setEditor(editSource(result.source)); setSharedReceipt({ name: result.source.form.name, version: result.receipt.version });
    } catch (caught) {
      if (alive.current) { setError(isConflict(caught) ? "共享来源已被其他编辑更新。当前草稿已保留，请显式读取最新来源再核对。" : failure(caught, "保存共享来源")); if (isConflict(caught)) setConflict("source"); }
    } finally { if (alive.current) setOperation("idle"); }
  };
  const saveSelection = async () => {
    if (busy || !selectionTarget || conflict === "selection") return;
    setOperation("saving-selection"); setSelectionError(null);
    try {
      let target: SearchSourceScope;
      let basis = selection;
      if (scope.kind === "shared-only") {
        if (!onEnsureInitialization) return;
        target = { kind: "initialization", initializationId: await onEnsureInitialization() };
        basis = (await fetchSearchSourceSelection(target)).selection;
      } else target = scope;
      if (!basis) throw new Error("当前选择尚未读取，请重新读取后保存。");
      const result = await saveSearchSourceSelection(target, allowed, basis.revision);
      if (!alive.current) return;
      setSelection(result.selection); setAllowed(result.selection.allowed_source_ids); setSelectionReceipt(result.receipt.revision);
      onSelectionSaved?.();
    } catch (caught) {
      if (alive.current) { setSelectionError(isConflict(caught) ? "当前研究的允许来源已变化。勾选草稿已保留，请显式读取最新选择后核对。" : failure(caught, "保存允许来源")); if (isConflict(caught)) setConflict("selection"); }
    } finally { if (alive.current) setOperation("idle"); }
  };
  const reloadConflict = async () => {
    setOperation("saving-source");
    try {
      if (conflict === "selection" && scope.kind !== "shared-only") {
        const latest = await fetchSearchSourceSelection(scope);
        if (!alive.current) return;
        setSelection(latest.selection); setAllowed(latest.selection.allowed_source_ids); setSelectionError(null); setSelectionReceipt(null);
      } else {
        const latest = await fetchSearchSources();
        if (!alive.current) return;
        setSources(latest.sources);
        const latestSource = latest.sources.find(source => source.source_id === editor?.source?.source_id);
        setEditor(latestSource ? editSource(latestSource) : null); setError(null); setSharedReceipt(null);
      }
      setConflict(null);
    } catch { if (alive.current) setError("读取最新搜索源失败，当前草稿已保留。请稍后重试。"); }
    finally { if (alive.current) setOperation("idle"); }
  };

  return <section className="search-sources-settings" aria-label="DeepFetch 搜索源">
    <h3>DeepFetch 搜索源</h3>
    <p className="search-sources-defaults">默认使用 OpenAlex 和原生 Web。补充来源按研究需要使用，勾选不要求每次全部调用。</p>
    <section className="search-sources-selection" aria-label="当前研究允许来源">
      <h4>{scope.kind === "quest" ? "当前 Quest 允许来源" : scope.kind === "initialization" || onEnsureInitialization ? "创建草稿允许来源" : "尚无当前研究"}</h4>
      {!selectionTarget ? <p>目前可管理共享来源。打开创建草稿或 Quest 后再保存其允许来源。</p> : <>
        <p>只使用这里已保存的选择。未保存的勾选不用于 DeepFetch。图书馆用于获取全文，不属于搜索源。</p>
        <fieldset disabled={loading || busy} className="search-sources-choices"><legend>补充来源</legend>
          {sources.map(source => <label key={source.source_id}>
            <input type="checkbox" aria-label={`允许 ${source.form.name}`} checked={allowed.includes(source.source_id)}
              onChange={event => { const checked = event.currentTarget.checked; setAllowed(previous => checked ? [...previous, source.source_id] : previous.filter(id => id !== source.source_id)); setSelectionReceipt(null); }} />
            <span>{source.form.name}<small>{sourceKindName(source.form)} · {source.needs_retest ? "需要重测" : source.test ? "已有配置测试" : "尚未测试"}</small></span>
          </label>)}
          {!sources.length && !loading ? <p>尚无共享补充来源，可在下方添加。默认来源始终保留。</p> : null}
        </fieldset>
        {selectionDirty ? <p role="status">允许来源有未保存的修改，不会用于研究。</p> : null}
        {selectionError ? <p role="alert" className="runtime-conditions-error">{selectionError}</p> : null}
        {selectionReceipt !== null ? <p role="status" className="runtime-conditions-saved">已保存{scope.kind === "quest" ? "当前 Quest" : "创建草稿"}允许来源，修订 {selectionReceipt}。后续新运行使用此选择。</p> : null}
        <button type="button" disabled={loading || busy || conflict === "selection" || (selection !== null && !selectionDirty)} onClick={() => void saveSelection()}>
          {operation === "saving-selection" ? "正在保存允许来源…" : scope.kind === "quest" ? "保存当前 Quest 允许来源" : "保存创建草稿允许来源"}</button>
      </>}
    </section>
    <section className="search-sources-shared" aria-label="共享搜索源管理">
      <h4>共享搜索源管理</h4>
      <p>共享保存只更新该来源，其他 Quest 的选择独立保存。已运行研究保留原来源版本。测试不会保存、启用来源或启动研究。</p>
      {loading ? <p role="status">正在读取搜索源…</p> : null}
      <div className="search-sources-actions">
        {sources.map(source => <button type="button" key={source.source_id} disabled={loading || busy} aria-pressed={editor?.source?.source_id === source.source_id}
          onClick={() => { setEditor(editSource(source)); setError(null); setConflict(previous => previous === "source" ? null : previous); }}>{source.form.name}</button>)}
        <button type="button" disabled={loading || busy} onClick={() => { setEditor({ draft: newDraft("website"), report: null }); setError(null); setConflict(previous => previous === "source" ? null : previous); }}>添加搜索源</button>
      </div>
      {editor ? <div className="search-source-editor">
        <fieldset disabled={loading || busy}><legend>{editor.source ? "编辑共享来源" : "新增共享来源"}</legend>
          {editor.source ? <p>已保存版本 {editor.source.version}。凭据{editor.source.credential_present ? "已配置" : "未配置"}，MCP 连接{editor.source.connection_present ? "已配置" : "未配置"}。</p> : null}
          <div className="search-sources-fields">
            <label><span>来源类型</span><select aria-label="来源类型" value={editor.draft.kind} disabled={Boolean(editor.source)}
              onChange={event => { const kind = event.currentTarget.value; if (kind === "website" || kind === "api" || kind === "mcp") edit(newDraft(kind)); }}>
              <option value="website">网站</option><option value="api">API</option><option value="mcp">MCP</option>
            </select></label>
            <label><span>来源名称</span><input aria-label="来源名称" maxLength={200} value={editor.draft.name} onChange={event => edit({ ...editor.draft, name: event.currentTarget.value })} /></label>
            <SourceFields draft={editor.draft} saved={Boolean(editor.source)} edit={edit} />
            <label className="search-sources-wide"><span>来源使用说明</span><textarea aria-label="来源使用说明" rows={3} maxLength={24000}
              value={editor.draft.instructions} onChange={event => edit({ ...editor.draft, instructions: event.currentTarget.value })} /></label>
            <label><span>测试查询</span><input aria-label="测试查询" maxLength={500} value={probeQuery} onChange={event => { setProbeQuery(event.currentTarget.value); setEditor({ ...editor, report: null }); }} /></label>
          </div>
          {editor.draft.kind === "mcp" ? <p>MCP 测试只初始化连接并发现工具目录，搜索、摘要和全文能力仍需实际研究调用验证。</p> : editor.draft.kind === "website" ? <p>网站测试验证有界网页读取。主页可达不证明站内搜索或论文全文可用。</p> : <p>API 测试发起有界 GET 查询，并报告实际解析结果。空结果、认证失败、限流和格式不匹配分别展示。</p>}
          <div className="search-sources-actions"><button type="button" disabled={!editor.draft.name.trim() || conflict === "source"} onClick={() => void test()}>
            {editor.source ? "重新测试当前配置" : "测试当前配置"}</button>
            <button type="button" disabled={!editor.draft.name.trim() || conflict === "source"} onClick={() => void saveShared()}>保存共享来源</button></div>
        </fieldset>
        {operation === "testing" ? <p role="status">正在测试当前配置…</p> : operation === "saving-source" ? <p role="status">正在保存共享来源…</p> : null}
        {editor.report ? <TestReport report={editor.report} /> : <p>当前表单没有匹配的测试结果，可保存后再测试。测试结果不代表研究已使用此来源。</p>}
      </div> : null}
      {error ? <p role="alert" className="runtime-conditions-error">{error}</p> : null}
      {sharedReceipt ? <p role="status" className="runtime-conditions-saved">共享来源「{sharedReceipt.name}」已保存，版本 {sharedReceipt.version}。当前研究的允许来源未自动改变。</p> : null}
      {conflict ? <button type="button" disabled={busy} onClick={() => void reloadConflict()}>读取最新{conflict === "source" ? "来源并替换当前编辑" : "选择并替换当前勾选"}</button> : !loading && error && !sources.length ? <button type="button" disabled={busy} onClick={() => setReadAttempt(value => value + 1)}>重新读取搜索源</button> : null}
    </section>
  </section>;
}
function sourceKindName(form: SearchSourceSafeForm) { return form.kind === "website" ? "网站" : form.kind === "api" ? "API" : "MCP"; }
function SourceFields({ draft, saved, edit }: { draft: EditorDraft; saved: boolean; edit: (draft: EditorDraft) => void }) {
  if (draft.kind === "website") return <label className="search-sources-wide"><span>网站地址</span><input aria-label="网站地址" type="url" value={draft.url} onChange={event => edit({ ...draft, url: event.currentTarget.value })} /></label>;
  if (draft.kind === "mcp") return <>
    <label><span>MCP 连接操作</span><select aria-label="MCP 连接操作" value={draft.connectionMode} onChange={event => edit({ ...draft, connectionMode: event.currentTarget.value === "keep" ? "keep" : "replace", connectionJson: "" })}>
      {saved ? <option value="keep">保留当前私密连接</option> : null}<option value="replace">替换完整连接</option>
    </select></label>
    {draft.connectionMode === "replace" ? <label className="search-sources-wide"><span>MCP 连接配置（私密 JSON）</span>
      <input aria-label="MCP 连接配置" type="password" autoComplete="new-password" spellCheck={false} value={draft.connectionJson} onChange={event => edit({ ...draft, connectionJson: event.currentTarget.value })} />
      <small>填写 stdio 的 command、arguments、environment，或 streamable_http 的 url、headers。保存后不回显。</small>
      <small>{'示例 {"transport":"streamable_http","url":"https://example.org/mcp","headers":{}}'}</small></label> : <p>服务器保留完整连接，认证、环境变量和启动参数不会回显。</p>}
  </>;
  return <>
    <label><span>API 模板</span><select aria-label="API 模板" value={draft.template} onChange={event => edit({ ...draft, template: event.currentTarget.value === "custom" ? "custom" : "crossref" })}>
      <option value="crossref">Crossref 论文标题搜索</option><option value="custom">自定义 GET JSON</option>
    </select></label>
    {draft.template === "crossref" ? <p>使用 Crossref works 的 query.title 和 rows，解析 message.items。无需 API Key。</p> : <ApiContractFields draft={draft} edit={edit} />}
    {draft.template === "custom" ? <><label><span>API Key 操作</span><select aria-label="API Key 操作" value={draft.credentialMode} onChange={event => {
      const mode = event.currentTarget.value;
      if (mode === "keep" || mode === "replace" || mode === "clear") edit({ ...draft, credentialMode: mode, credentialValue: "" });
    }}>{saved ? <option value="keep">保留当前 Key</option> : null}<option value="replace">替换 Key</option><option value="clear">清除／不使用 Key</option></select></label>
    {draft.credentialMode === "replace" ? <label><span>新 API Key（私密）</span><input aria-label="新 API Key" type="password" autoComplete="new-password" value={draft.credentialValue} onChange={event => edit({ ...draft, credentialValue: event.currentTarget.value })} /></label> : null}</> : null}
  </>;
}
function ApiContractFields({ draft, edit }: { draft: Extract<EditorDraft, { kind: "api" }>; edit: (draft: EditorDraft) => void }) {
  const contract = draft.contract;
  const change = (next: SearchSourceApiContract) => edit({ ...draft, contract: next });
  const path = (value: string) => value.split(".").map(part => part.trim()).filter(Boolean);
  const fields: Array<[keyof SearchSourceApiContract["fields"], string]> = [["title", "标题"], ["url", "结果 URL"], ["doi", "DOI"], ["arxiv", "arXiv"], ["version", "版本"], ["abstract", "摘要"]];
  return <>
    <label className="search-sources-wide"><span>GET 端点</span><input aria-label="GET 端点" type="url" value={contract.endpoint} onChange={event => change({ ...contract, endpoint: event.currentTarget.value })} /></label>
    <label><span>查询参数名</span><input aria-label="查询参数名" value={contract.query_parameter} onChange={event => change({ ...contract, query_parameter: event.currentTarget.value })} /></label>
    <label><span>数量参数名（可选）</span><input aria-label="数量参数名" value={contract.limit_parameter ?? ""} onChange={event => change({ ...contract, limit_parameter: event.currentTarget.value || undefined })} /></label>
    <label className="search-sources-wide"><span>固定参数（JSON 字符串对象）</span><textarea aria-label="固定参数" rows={2} value={draft.fixedParameters} onChange={event => edit({ ...draft, fixedParameters: event.currentTarget.value })} /></label>
    <label><span>认证方式</span><select aria-label="认证方式" value={contract.auth.kind} onChange={event => {
      const kind = event.currentTarget.value;
      if (kind === "none" || kind === "bearer") change({ ...contract, auth: { kind } });
      else if (kind === "header" || kind === "query") change({ ...contract, auth: { kind, name: "" } });
    }}><option value="none">无认证</option><option value="bearer">Bearer Key</option><option value="header">指定请求头</option><option value="query">指定查询参数</option></select></label>
    {contract.auth.kind === "header" || contract.auth.kind === "query" ? <label><span>认证字段名</span><input aria-label="认证字段名" value={contract.auth.name} onChange={event => {
      const kind = contract.auth.kind;
      if (kind === "header" || kind === "query") change({ ...contract, auth: { kind, name: event.currentTarget.value } });
    }} /></label> : null}
    <label><span>结果数组路径</span><input aria-label="结果数组路径" value={contract.items_path.join(".")} onChange={event => change({ ...contract, items_path: path(event.currentTarget.value) })} /><small>用点分隔字段，留空表示根数组。</small></label>
    <label><span>结果性质</span><select aria-label="结果性质" value={contract.result_kind} onChange={event => {
      const result_kind = event.currentTarget.value;
      if (result_kind === "paper_metadata" || result_kind === "abstract" || result_kind === "web_lead") change({ ...contract, result_kind });
    }}><option value="paper_metadata">论文元数据</option><option value="abstract">摘要</option><option value="web_lead">网页线索</option></select></label>
    {fields.map(([key, label]) => <label key={key}><span>{label}字段路径{key === "title" || key === "url" ? "" : "（可选）"}</span><input aria-label={`${label}字段路径`} value={contract.fields[key]?.join(".") ?? ""} onChange={event => {
      const next = { ...contract.fields }; const value = path(event.currentTarget.value);
      if (key === "title" || key === "url" || value.length) next[key] = value; else delete next[key];
      change({ ...contract, fields: next });
    }} /></label>)}
  </>;
}
function TestReport({ report }: { report: SearchSourceTestReport }) {
  return <section className="search-source-report" aria-label="当前配置测试结果">
    <h5>当前配置测试结果</h5><p>测试时间 {report.tested_at}。{report.result_count === null ? "未验证结果数量。" : `返回 ${report.result_count} 条结果。`}测试没有保存或启用来源，也不代表研究已使用。</p>
    <dl>{Object.entries(report.capabilities).map(([name, result]) => <div key={name}>
      <dt>{capabilityNames[name] ?? name}</dt><dd><strong data-status={result.status}>{statusNames[result.status]}</strong> {reasonNames[result.reason] ?? result.reason}</dd>
    </div>)}</dl>
    {report.tools.length ? <details><summary>发现的工具（{report.tools.length}）</summary><ul>{report.tools.map(tool => <li key={tool.name}><strong>{tool.name}</strong>{tool.description ? ` · ${tool.description}` : ""}</li>)}</ul></details> : <p>未发现 MCP 工具。</p>}
  </section>;
}
