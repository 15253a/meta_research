import { useEffect, useState, type FormEvent } from "react";
import "./system-mcp.css";

type Connection = { command?: string; args?: string[]; cwd?: string; env?: Record<string, string>; env_vars?: string[];
  url?: string; bearer_token_env_var?: string; http_headers?: Record<string, string>; env_http_headers?: Record<string, string> };
type Server = { server_id: string; display_name: string; description: string; enabled: boolean;
  scope: { mode: "all" | "root_kinds"; root_kinds?: string[] }; transport: "stdio" | "streamable_http";
  connection: Connection; revision: number; startup_timeout_sec: number; tool_timeout_sec: number;
  connection_status: { status: string; revision: number; checked_at: string | null; error_code?: string; tool_count?: number } };
type Catalog = { revision: number; servers: Server[]; root_kinds: string[]; connection_check: string;
  operations?: { operation_id: string; root_kind: string; registry_revision: number; server_ids: string[]; recorded_at: string }[] };
const labels: Record<string, string> = { idea: "研究思路", plan: "研究计划", bundle: "研究实施", reasoning: "综合推理",
  writing: "写作", companion: "研究助手与草拟", acquisition: "材料获取", target: "Target 执行", deepfetch: "DeepFetch" };
const newServer = (): Server => ({ server_id: "", display_name: "", description: "", enabled: true,
  scope: { mode: "all" }, transport: "streamable_http", connection: { url: "" }, revision: 0,
  startup_timeout_sec: 10, tool_timeout_sec: 60, connection_status: { status: "unknown", revision: 0, checked_at: null } });
const errorMessage = (code: string) => code === "system_mcp_revision_conflict" ? "配置已被其他操作更新。请重新读取列表，再核对并保存你的编辑。"
  : code === "system_mcp_registry_unavailable" ? "暂时无法读取或保存系统 MCP 配置，请稍后重试。"
  : code === "system_mcp_config_invalid" ? "配置不符合要求，请检查服务标识、连接参数及适用范围。" : code;

async function request<T>(path = "", method = "GET", body?: object): Promise<T> {
  const csrf = document.cookie.split("; ").find(value => value.startsWith("meta_research_csrf="))?.split("=")[1];
  const response = await fetch(`/api/v1/system/mcp${path}`, { method, credentials: "same-origin",
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "X-CSRF-Token": decodeURIComponent(csrf ?? "") } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw new Error(errorMessage(data.detail?.code ?? `请求失败 (${response.status})`));
  return data as T;
}

export function SystemMcpSettings() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [draft, setDraft] = useState<Server | null>(null);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [args, setArgs] = useState("[]");
  const [ordinary, setOrdinary] = useState("{}");
  const [secretRefs, setSecretRefs] = useState("");
  const [removeId, setRemoveId] = useState<string | null>(null);
  const reload = async () => { try { setCatalog(await request<Catalog>()); setError(""); } catch (cause) { setError(String((cause as Error).message)); } };
  useEffect(() => { void reload(); }, []);
  const open = (server?: Server) => {
    const value = server ? structuredClone(server) : newServer();
    setDraft(value); setEditing(Boolean(server)); setArgs(JSON.stringify(value.connection.args ?? []));
    setOrdinary(JSON.stringify(value.transport === "stdio" ? value.connection.env ?? {} : value.connection.http_headers ?? {}, null, 2));
    setSecretRefs(value.transport === "stdio" ? (value.connection.env_vars ?? []).join("\n") : JSON.stringify(value.connection.env_http_headers ?? {}, null, 2));
    setError(""); setNotice("");
  };
  const mutate = async (path: string, method: string, body: object) => {
    setBusy(true); setError(""); setNotice("");
    try { setCatalog(await request<Catalog>(path, method, body)); setNotice("已保存。后续新的会话操作会自动读取此配置。"); return true; }
    catch (cause) { setError((cause as Error).message); return false; }
    finally { setBusy(false); }
  };
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (!draft || !catalog) return;
    try {
      const connection: Connection = draft.transport === "stdio" ? {
        command: draft.connection.command, args: JSON.parse(args), ...(draft.connection.cwd ? { cwd: draft.connection.cwd } : {}),
        env: JSON.parse(ordinary), env_vars: secretRefs.split(/\r?\n/).map(value => value.trim()).filter(Boolean),
      } : { url: draft.connection.url, http_headers: JSON.parse(ordinary), env_http_headers: JSON.parse(secretRefs || "{}"),
        ...(draft.connection.bearer_token_env_var ? { bearer_token_env_var: draft.connection.bearer_token_env_var } : {}) };
      const config = { server_id: draft.server_id, display_name: draft.display_name, description: draft.description,
        enabled: draft.enabled, scope: draft.scope, transport: draft.transport, connection,
        startup_timeout_sec: draft.startup_timeout_sec, tool_timeout_sec: draft.tool_timeout_sec };
      if (await mutate(editing ? `/${encodeURIComponent(draft.server_id)}` : "", editing ? "PUT" : "POST",
        { expected_revision: catalog.revision, config })) setDraft(null);
    } catch { setError("参数数组、普通参数和 HTTP 认证引用需要有效的 JSON 格式。"); }
  };
  const toggle = async (server: Server) => {
    if (!catalog) return;
    const { revision: _revision, connection_status: _connectionStatus, ...config } = server;
    await mutate(`/${encodeURIComponent(server.server_id)}`, "PUT", { expected_revision: catalog.revision, config: { ...config, enabled: !server.enabled } });
  };
  return <main className="system-mcp">
    <header><a href="/">← 返回工作台</a><span>系统设置</span></header>
    <div className="system-mcp-title"><div><p className="system-mcp-eyebrow">运行能力</p><h1>系统 MCP</h1></div>
      <button type="button" disabled={!catalog || busy} onClick={() => open()}>添加 MCP 服务</button></div>
    <p>为根会话接入外部工具。保存后，新会话及同一会话的下一次新操作自动生效；正在执行或恢复的操作继续使用原配置。</p>
    <p className="system-mcp-note">命令、路径和 localhost 均指 8769 执行服务器。当前支持 Codex 后端。认证填写服务器环境变量名称。</p>
    {error && <div className="system-mcp-error" role="alert">{error} <button type="button" onClick={() => void reload()}>重新读取列表</button></div>}
    {notice && <p role="status" className="system-mcp-notice">{notice}</p>}
    {!catalog && !error && <p role="status">正在读取配置…</p>}
    {catalog && <><div className="system-mcp-toolbar"><span>注册表修订 {catalog.revision} · {catalog.servers.length} 项外部服务</span><button type="button" disabled={busy} onClick={() => void reload()}>刷新</button></div>
      <section aria-label="已注册 MCP 服务" className="system-mcp-list">
        {catalog.servers.map(server => <article key={server.server_id}>
          <div className="system-mcp-service-title"><h2>{server.display_name}</h2><span>{server.enabled ? "已启用" : "已停用"}</span></div>
          <p>{server.description || "暂无说明"}</p><code>{server.server_id}</code>
          <dl><div><dt>连接方式</dt><dd>{server.transport === "stdio" ? "本机进程 · stdio" : "Streamable HTTP"}</dd></div>
            <div><dt>适用范围</dt><dd>{server.scope.mode === "all" ? "全部根会话" : server.scope.root_kinds?.map(kind => labels[kind] ?? kind).join("、")}</dd></div>
            <div><dt>配置修订</dt><dd>{server.revision}</dd></div>
            <div><dt>连接状态</dt><dd>{server.connection_status.status === "connected" ? "检查连接成功" : server.connection_status.status === "failed" ? "检查失败" : "未知 · 尚无连接证据"}
              {server.connection_status.checked_at && <> · {new Date(server.connection_status.checked_at).toLocaleString()} · 修订 {server.connection_status.revision}{server.connection_status.revision !== server.revision ? "（旧修订）" : ""}</>}
              {server.connection_status.error_code && <small>{server.connection_status.error_code === "system_mcp_check_timeout" ? "连接检查超时" : server.connection_status.error_code === "system_mcp_connection_failed" ? "服务无法连接或认证失败" : "尚未取得可验证的连接信息"}</small>}</dd></div></dl>
          <div className="system-mcp-actions"><button disabled={busy} onClick={() => open(server)}>编辑</button><button disabled={busy} onClick={() => void toggle(server)}>{server.enabled ? "停用" : "启用"}</button>
            {catalog.connection_check === "supported" && <button disabled={busy} onClick={async () => { setBusy(true); try { await request(`/${encodeURIComponent(server.server_id)}/check`, "POST", { expected_revision: catalog.revision }); await reload(); } catch (cause) { setError((cause as Error).message); } finally { setBusy(false); } }}>检查连接</button>}
            <button disabled={busy} onClick={() => setRemoveId(server.server_id)}>移除</button></div>
          {removeId === server.server_id && <div className="system-mcp-remove"><p>移除 {server.display_name}？未决操作仍可使用原快照恢复。</p><button disabled={busy} onClick={async () => { if (await mutate(`/${encodeURIComponent(server.server_id)}`, "DELETE", { expected_revision: catalog.revision })) setRemoveId(null); }}>确认移除</button><button onClick={() => setRemoveId(null)}>取消</button></div>}
        </article>)}
        {!catalog.servers.length && <p className="system-mcp-empty">还没有注册外部 MCP 服务。添加后可供所选根会话使用。</p>}
      </section>
      {catalog.connection_check !== "supported" && <p className="system-mcp-note">独立连接检查未支持；连接状态以原生运行器的握手与工具发现证据为准。</p>}
      <section className="system-mcp-internal"><h2>meta_research <small>内置 · 只读</small></h2><p>研究工具通道由系统管理，随会话权限装载。</p></section>
      <section><h2>操作装载记录</h2><p className="system-mcp-note">记录各次操作选中的配置修订；连接与工具调用结果另行判断。</p>
        {catalog.operations?.length ? <ul>{catalog.operations.slice(0, 20).map(operation => <li key={operation.operation_id}>{labels[operation.root_kind] ?? operation.root_kind} · 修订 {operation.registry_revision} · {operation.server_ids.join("、") || "无外部服务"} <code>{operation.operation_id}</code></li>)}</ul> : <p>暂无操作装载记录。</p>}</section>
    </>}
    {draft && <section className="system-mcp-editor" aria-label={editing ? "编辑 MCP 服务" : "新建 MCP 服务"}>
      <h2>{editing ? "编辑 MCP 服务" : "新建 MCP 服务"}</h2>
      <form onSubmit={event => void save(event)}>
        <div className="system-mcp-fields"><label>服务标识<input required disabled={editing} value={draft.server_id} pattern="[a-z][a-z0-9_\-]*" maxLength={48} onChange={event => setDraft({ ...draft, server_id: event.target.value })} /><small>以小写字母开头；保存后不可修改。</small></label>
          <label>显示名称<input required value={draft.display_name} onChange={event => setDraft({ ...draft, display_name: event.target.value })} /></label></div>
        <label>说明<textarea value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} /></label>
        <label>连接方式<select aria-label="连接方式" value={draft.transport} onChange={event => { const transport = event.target.value as Server["transport"]; setDraft({ ...draft, transport, connection: transport === "stdio" ? { command: "" } : { url: "" } }); setOrdinary("{}"); setSecretRefs(transport === "stdio" ? "" : "{}"); }}><option value="streamable_http">Streamable HTTP</option><option value="stdio">stdio 本机进程</option></select></label>
        {draft.transport === "stdio" ? <>
          <label>命令<input required value={draft.connection.command ?? ""} placeholder="/usr/bin/python3" onChange={event => setDraft({ ...draft, connection: { ...draft.connection, command: event.target.value } })} /></label>
          <label>参数数组（JSON）<textarea value={args} onChange={event => setArgs(event.target.value)} placeholder={'["/srv/mcp/server.py"]'} /></label>
          <label>工作目录（绝对路径，可选）<input value={draft.connection.cwd ?? ""} onChange={event => setDraft({ ...draft, connection: { ...draft.connection, cwd: event.target.value } })} /></label>
        </> : <>
          <label>服务 URL<input type="url" required value={draft.connection.url ?? ""} placeholder="https://example.com/mcp" onChange={event => setDraft({ ...draft, connection: { ...draft.connection, url: event.target.value } })} /></label>
          <label>Bearer 认证环境变量（可选）<input value={draft.connection.bearer_token_env_var ?? ""} placeholder="CAMERA_MCP_TOKEN" onChange={event => setDraft({ ...draft, connection: { ...draft.connection, bearer_token_env_var: event.target.value } })} /></label>
        </>}
        <details><summary>环境与超时设置</summary>
          <label>{draft.transport === "stdio" ? "普通环境变量（JSON）" : "普通请求头（JSON）"}<textarea value={ordinary} onChange={event => setOrdinary(event.target.value)} /></label>
          <label>{draft.transport === "stdio" ? "敏感环境变量引用（每行一个名称）" : "认证请求头引用（JSON，值为环境变量名称）"}<textarea value={secretRefs} onChange={event => setSecretRefs(event.target.value)} /></label>
          <div className="system-mcp-fields"><label>连接超时（秒）<input type="number" min={1} max={60} required value={draft.startup_timeout_sec} onChange={event => setDraft({ ...draft, startup_timeout_sec: Number(event.target.value) })} /></label>
            <label>工具超时（秒）<input type="number" min={1} max={600} required value={draft.tool_timeout_sec} onChange={event => setDraft({ ...draft, tool_timeout_sec: Number(event.target.value) })} /></label></div>
        </details>
        <fieldset><legend>适用范围</legend><label className="system-mcp-choice"><input type="radio" checked={draft.scope.mode === "all"} onChange={() => setDraft({ ...draft, scope: { mode: "all" } })} />全部根会话（含未来接入的根类型）</label>
          <label className="system-mcp-choice"><input type="radio" checked={draft.scope.mode === "root_kinds"} onChange={() => setDraft({ ...draft, scope: { mode: "root_kinds", root_kinds: [] } })} />选择根类型</label>
          {draft.scope.mode === "root_kinds" && <div className="system-mcp-kinds">{catalog?.root_kinds.map(kind => <label className="system-mcp-choice" key={kind}><input type="checkbox" checked={draft.scope.root_kinds?.includes(kind) ?? false} onChange={event => setDraft({ ...draft, scope: { mode: "root_kinds", root_kinds: event.target.checked ? [...(draft.scope.root_kinds ?? []), kind] : draft.scope.root_kinds?.filter(value => value !== kind) } })} />{labels[kind] ?? kind}</label>)}</div>}
        </fieldset>
        <label className="system-mcp-choice"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用此服务</label>
        <div className="system-mcp-actions"><button type="submit" disabled={busy || (draft.scope.mode === "root_kinds" && !draft.scope.root_kinds?.length)}>{busy ? "正在保存…" : "保存配置"}</button><button type="button" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
      </form>
    </section>}
  </main>;
}
