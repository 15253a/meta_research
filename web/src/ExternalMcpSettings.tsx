import { useEffect, useRef, useState } from "react";
import { fetchExternalMcp, ProductError, saveExternalMcp, testExternalMcpConnection,
  type ExternalMcpConfiguration, type ExternalMcpConnection, type ExternalMcpService } from "./api";

type ServiceDraft = Omit<ExternalMcpService, "connection"> & {
  transport: ExternalMcpConnection["transport"]; command: string; arguments: string;
  environment: string; workingDirectory: string; url: string; headers: string;
};

function draft(service: ExternalMcpService): ServiceDraft {
  const connection = service.connection;
  return { service_id: service.service_id, name: service.name,
    allowed_root_kinds: [...service.allowed_root_kinds], research_instructions: service.research_instructions,
    transport: connection.transport, command: connection.transport === "stdio" ? connection.command : "",
    arguments: JSON.stringify(connection.transport === "stdio" ? connection.arguments : [], null, 2),
    environment: JSON.stringify(connection.transport === "stdio" ? connection.environment : {}, null, 2),
    workingDirectory: connection.transport === "stdio" ? connection.working_directory ?? "" : "",
    url: connection.transport === "streamable_http" ? connection.url : "",
    headers: JSON.stringify(connection.transport === "streamable_http" ? connection.headers : {}, null, 2) };
}

function stringMap(text: string): Record<string, string> {
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.values(value).some(item => typeof item !== "string")) {
    throw new Error("环境变量和请求头必须是 JSON 字符串对象。");
  }
  return value as Record<string, string>;
}

function connection(service: ServiceDraft): ExternalMcpConnection {
  if (service.transport === "streamable_http") return { transport: service.transport, url: service.url, headers: stringMap(service.headers) };
  const argumentsValue: unknown = JSON.parse(service.arguments);
  if (!Array.isArray(argumentsValue) || argumentsValue.some(value => typeof value !== "string")) throw new Error("启动参数必须是 JSON 字符串数组。");
  return { transport: service.transport, command: service.command, arguments: argumentsValue,
    environment: stringMap(service.environment), ...(service.workingDirectory ? { working_directory: service.workingDirectory } : {}) };
}

const failureReasons: Record<string, string> = {
  timeout: "连接超时，请检查服务是否正在运行。", authentication_failed: "服务拒绝认证，请检查请求头。",
  process_start_failed: "服务进程无法启动，请检查命令和工作目录。", invalid_protocol: "服务未返回有效的 MCP 协议响应。",
  invalid_catalog: "服务工具目录格式无效。", response_too_large: "服务响应超过允许大小。", unreachable: "无法连接服务，请检查地址或启动配置。",
};

export function ExternalMcpSettings() {
  const alive = useRef(false);
  const [basis, setBasis] = useState<ExternalMcpConfiguration | null>(null);
  const [services, setServices] = useState<ServiceDraft[]>([]);
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [testResult, setTestResult] = useState<{ index: number; text: string } | null>(null);
  const [testing, setTesting] = useState<number | null>(null);
  const disabled = loading || !basis || busy;

  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    let current = true;
    setLoading(true);
    setError(null);
    void fetchExternalMcp(controller.signal).then(value => {
      if (!current) return;
      setBasis(value); setServices(value.services.map(draft)); setConflict(false); setSaved(false);
    }).catch(() => {
      if (current) setError("外部 MCP 配置读取失败，请重试。");
    }).finally(() => { if (current) setLoading(false); });
    return () => { alive.current = false; current = false; controller.abort(); };
  }, [attempt]);

  const edit = (index: number, change: Partial<ServiceDraft>) => {
    setServices(previous => previous.map((service, selected) => selected === index ? { ...service, ...change } : service));
    setSaved(false); setTestResult(null);
  };
  const add = () => {
    if (!basis) return;
    let id = 1;
    while (services.some(service => service.service_id === `service_${id}`)) id += 1;
    setServices(previous => [...previous, draft({ service_id: `service_${id}`, name: "",
      connection: { transport: "stdio", command: "", arguments: [], environment: {} },
      allowed_root_kinds: [...basis.root_kinds], research_instructions: "" })]);
    setSaved(false); setTestResult(null);
  };
  const save = async () => {
    if (!basis || busy || conflict) return;
    setBusy(true); setError(null); setSaved(false);
    try {
      const value = await saveExternalMcp(services.map(service => ({ service_id: service.service_id, name: service.name,
        connection: connection(service), allowed_root_kinds: service.allowed_root_kinds, research_instructions: service.research_instructions })), basis.revision);
      if (!alive.current) return;
      setBasis(value); setServices(value.services.map(draft)); setSaved(true);
    } catch (caught) {
      if (!alive.current) return;
      const stale = caught instanceof ProductError && caught.code === "external_mcp_config_stale";
      setConflict(stale);
      setError(stale ? "外部 MCP 配置已变化。当前编辑已保留，请关闭后重新打开，核对最新内容再保存。"
        : caught instanceof SyntaxError ? "JSON 配置无法解析，当前编辑已保留。"
        : caught instanceof ProductError ? "保存失败，请检查服务标识、名称和连接配置；当前编辑已保留。"
        : caught instanceof Error ? caught.message : "保存失败，当前编辑已保留。");
    } finally { if (alive.current) setBusy(false); }
  };
  const test = async (index: number) => {
    setBusy(true); setTesting(index); setTestResult(null); setError(null);
    try {
      const result = await testExternalMcpConnection(connection(services[index]));
      if (!alive.current) return;
      setTestResult({ index, text: result.status === "ready"
        ? `连接成功：${result.server_name}，发现 ${result.tool_count} 个工具；未执行业务操作。`
        : failureReasons[result.reason_code ?? ""] ?? "连接失败，请检查连接配置。" });
    } catch (caught) {
      if (alive.current) setTestResult({ index, text: caught instanceof SyntaxError ? "JSON 配置无法解析，请检查后重试。"
        : caught instanceof ProductError ? "连接配置无效或服务暂时不可用。" : caught instanceof Error ? caught.message : "连接测试失败。" });
    } finally { if (alive.current) { setBusy(false); setTesting(null); } }
  };

  return <section className="external-mcp-settings" aria-label="外部 MCP 服务">
    <h3>外部 MCP 服务</h3>
    <p>配置适用于本部署的研究。保存后用于后续新操作，正在进行的操作和恢复仍使用原有配置。</p>
    {loading ? <p role="status">正在读取外部 MCP 配置…</p> : null}
    <form onSubmit={event => { event.preventDefault(); void save(); }}>
      {services.map((service, index) => <fieldset key={index} disabled={disabled} className="external-mcp-service">
        <legend>{service.name || `服务 ${index + 1}`}</legend>
        <div className="runtime-conditions-fields">
          <label><span>服务标识</span><input aria-label="服务标识" value={service.service_id} maxLength={32} required pattern={"[a-z][a-z0-9_\\-]{0,31}"}
            onChange={event => edit(index, { service_id: event.currentTarget.value })} /></label>
          <label><span>服务名称</span><input aria-label="服务名称" value={service.name} maxLength={200} required onChange={event => edit(index, { name: event.currentTarget.value })} /></label>
          <label><span>连接方式</span><select aria-label="连接方式" value={service.transport}
            onChange={event => edit(index, { transport: event.currentTarget.value as ServiceDraft["transport"] })}>
            <option value="stdio">本机进程（stdio）</option><option value="streamable_http">Streamable HTTP</option>
          </select></label>
          {service.transport === "stdio" ? <>
            <label><span>启动命令</span><input aria-label="启动命令" value={service.command} required onChange={event => edit(index, { command: event.currentTarget.value })} /></label>
            <label><span>启动参数（JSON 字符串数组）</span><textarea aria-label="启动参数" rows={3} value={service.arguments} onChange={event => edit(index, { arguments: event.currentTarget.value })} /></label>
            <label><span>环境变量（JSON 字符串对象）</span><textarea aria-label="环境变量" rows={3} value={service.environment} onChange={event => edit(index, { environment: event.currentTarget.value })} /></label>
            <label className="runtime-conditions-exclusions"><span>工作目录（可选）</span><input aria-label="工作目录" value={service.workingDirectory} onChange={event => edit(index, { workingDirectory: event.currentTarget.value })} /></label>
          </> : <>
            <label><span>服务地址</span><input aria-label="服务地址" type="url" required value={service.url} onChange={event => edit(index, { url: event.currentTarget.value })} /></label>
            <label className="runtime-conditions-exclusions"><span>请求头（JSON 字符串对象）</span><textarea aria-label="请求头" rows={3} value={service.headers} onChange={event => edit(index, { headers: event.currentTarget.value })} /></label>
          </>}
        </div>
        <p>允许使用此服务的 Root 类型</p>
        <div className="external-mcp-root-actions"><button type="button" onClick={() => edit(index, { allowed_root_kinds: [...(basis?.root_kinds ?? [])] })}>全选</button>
          <button type="button" onClick={() => edit(index, { allowed_root_kinds: [] })}>清空选择</button></div>
        <div className="external-mcp-roots">{basis?.root_kinds.map(kind => <label key={kind}>
          <input type="checkbox" checked={service.allowed_root_kinds.includes(kind)} aria-label={kind}
            onChange={event => edit(index, { allowed_root_kinds: event.currentTarget.checked ? [...service.allowed_root_kinds, kind] : service.allowed_root_kinds.filter(value => value !== kind) })} />
          {kind === "deepfetch" ? "DeepFetch" : kind.charAt(0).toUpperCase() + kind.slice(1)}</label>)}</div>
        {!service.allowed_root_kinds.length ? <p>未选择任何 Root，此服务不会提供给研究操作。</p> : null}
        <label><span>研究使用说明（可选）</span><textarea aria-label="研究使用说明" rows={3} maxLength={24000} value={service.research_instructions}
          placeholder="例如：可用的仪器、数据含义和研究用途；没有可留空"
          onChange={event => edit(index, { research_instructions: event.currentTarget.value })} /></label>
        <p>连接测试只初始化连接并读取工具目录，不调用仪器或业务工具。</p>
        <div className="external-mcp-root-actions"><button type="button" onClick={() => void test(index)}>{testing === index ? "正在测试连接…" : "测试连接"}</button>
          <button type="button" onClick={() => { setServices(previous => previous.filter((_, selected) => selected !== index)); setSaved(false); setTestResult(null); }}>删除服务</button></div>
        {testResult?.index === index ? <p role="status">{testResult.text}</p> : null}
      </fieldset>)}
      {!loading && basis && !services.length ? <p>尚未配置外部 MCP 服务。</p> : null}
      {error ? <p role="alert" className="runtime-conditions-error">{error}</p> : null}
      {saved ? <p role="status" className="runtime-conditions-saved">外部 MCP 配置已保存，将用于后续新操作。</p> : null}
      <footer>{!loading && !basis ? <button type="button" onClick={() => setAttempt(value => value + 1)}>重新读取外部 MCP</button> : null}
        <button type="button" disabled={disabled || services.length >= 32} onClick={add}>添加服务</button>
        <button type="submit" className="runtime-conditions-save" disabled={disabled || conflict}>{busy && testing === null ? "正在保存…" : "保存外部 MCP"}</button>
      </footer>
    </form>
  </section>;
}
