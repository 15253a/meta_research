export type ServerMaterialKind = "file" | "directory";

export type MaterialServer = {
  server_ref: string;
  hostname: string;
  platform: string;
  permission_context: string;
};

export type MaterialObservation = {
  device: string;
  inode: string;
  kind: ServerMaterialKind | "unsupported";
  size: string;
  modified_ns: string;
  changed_ns: string;
  observation_ref: string;
};

export type ServerMaterialSelection = {
  server: MaterialServer;
  absolute_path: string;
  kind: ServerMaterialKind;
  description: string;
  observation: MaterialObservation;
  availability: "available";
};

export type ServerMaterialEntry = {
  name: string;
  absolute_path: string;
  kind: ServerMaterialKind | "unsupported" | "unknown";
  availability: string;
  observation?: MaterialObservation;
};

export type ServerMaterialPage = {
  server: MaterialServer;
  absolute_path: string;
  entries: ServerMaterialEntry[];
  next_cursor: string | null;
  unexpanded: true;
};

export type MaterialReceiver = { kind: "creation" | "manual" | "current" | "request" } & Record<string, unknown>;
export type WorkMaterialSubmission = { receiver: MaterialReceiver; selections: ServerMaterialSelection[]; description: string };
export type WorkMaterialReference = {
  reference_ref: string; submission_ref: string; receiver: MaterialReceiver;
  source: ServerMaterialSelection; description: string; availability: string;
  read_state: "read" | "not_read"; read_ranges: Array<{ path: string; offset: number; bytes: number }>;
  failures: Array<{ error: string; path: string }>; unexpanded: boolean;
};
export type WorkMaterialReceipt = { submission_ref: string; receiver: MaterialReceiver; references: WorkMaterialReference[] };
export type PendingMaterialCommand = { namespace: string; scope: string; path: string; key: string; body: Record<string, unknown> };

const pendingPrefix = "meta_research_pending_work_material:v1:";

function csrfToken(): string {
  const prefix = "meta_research_csrf=";
  const cookie = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(prefix));
  if (!cookie) throw new WorkMaterialError("csrf_token_unavailable");
  return decodeURIComponent(cookie.slice(prefix.length));
}

async function sessionNamespace(): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(csrfToken()));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
}

export async function pendingMaterialCommand(scope: string): Promise<PendingMaterialCommand | null> {
  const namespace = await sessionNamespace();
  let raw: string | null;
  try { raw = localStorage.getItem(`${pendingPrefix}${namespace}:${scope}`); }
  catch { throw new WorkMaterialError("material_pending_storage_unavailable"); }
  if (!raw) return null;
  let value: unknown;
  try { value = JSON.parse(raw); }
  catch { throw new WorkMaterialError("material_pending_invalid"); }
  if (!record(value) || value.namespace !== namespace || value.scope !== scope || typeof value.path !== "string" || !value.path.startsWith("/api/v1/")
    || typeof value.key !== "string" || !record(value.body)) throw new WorkMaterialError("material_pending_invalid");
  return value as PendingMaterialCommand;
}

export async function stageMaterialCommand(scope: string, path: string, body: Record<string, unknown>): Promise<PendingMaterialCommand> {
  if (await pendingMaterialCommand(scope)) throw new WorkMaterialError("material_pending_exists");
  const command = { namespace: await sessionNamespace(), scope, path, key: crypto.randomUUID(), body: JSON.parse(JSON.stringify(body)) as Record<string, unknown> };
  try { localStorage.setItem(`${pendingPrefix}${command.namespace}:${scope}`, JSON.stringify(command)); }
  catch { throw new WorkMaterialError("material_pending_storage_unavailable"); }
  return command;
}

export async function discardMaterialCommand(scope: string): Promise<void> {
  const namespace = await sessionNamespace();
  try { localStorage.removeItem(`${pendingPrefix}${namespace}:${scope}`); }
  catch { throw new WorkMaterialError("material_pending_storage_unavailable"); }
}

export async function deliverMaterialCommand(command: PendingMaterialCommand): Promise<Record<string, unknown>> {
  if (command.namespace !== await sessionNamespace()) throw new WorkMaterialError("material_session_changed");
  const value = await request(command.path, {
    method: "POST", body: JSON.stringify(command.body),
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken(), "Idempotency-Key": command.key },
  });
  if (!record(value)) throw new WorkMaterialError("material_response_invalid");
  await discardMaterialCommand(command.scope);
  return value;
}

export async function fetchMaterialReceiver(path: string): Promise<MaterialReceiver> {
  const value = await request(path);
  if (!record(value) || !["creation", "manual", "current", "request"].includes(String(value.kind))) throw new WorkMaterialError("material_response_invalid");
  return value as MaterialReceiver;
}

export async function fetchWorkMaterialReference(reference: string): Promise<WorkMaterialReference> {
  return await request(`/api/v1/work-materials/${encodeURIComponent(reference)}`) as WorkMaterialReference;
}

export async function fetchResearchInputMaterialReceipts(inputRef: string, questRef: string): Promise<WorkMaterialReceipt[]> {
  const value = await request(`/api/v1/research-inputs/${encodeURIComponent(inputRef)}?${new URLSearchParams({ quest_ref: questRef })}`);
  if (!record(value) || !Array.isArray(value.work_materials)) throw new WorkMaterialError("material_response_invalid");
  return value.work_materials as WorkMaterialReceipt[];
}

export function currentMaterialReceiverPath(questRef: string, questionRef?: string | null): string {
  const query = new URLSearchParams({ quest_ref: questRef });
  if (questionRef) query.set("question_ref", questionRef);
  return `/api/v1/work-materials/receiver?${query}`;
}

export class WorkMaterialError extends Error {
  constructor(readonly code: string, readonly httpStatus?: number) {
    super(code);
    this.name = "WorkMaterialError";
  }
}

const materialErrors: Record<string, string> = {
  material_missing: "服务器上已找不到此路径。",
  material_not_readable: "服务器当前运行账户没有读取此路径的权限。",
  material_unsafe: "此路径含符号链接或不是普通文件、目录，无法安全选择。",
  unsafe: "符号链接或特殊文件无法安全选择。",
  material_path_invalid: "请输入服务器上的完整绝对路径。",
  material_directory_required: "此路径不是目录。可以用“选择此路径”选择文件。",
  material_cursor_expired: "目录续页已过期，请重新打开目录。",
  material_cursor_capacity: "服务器目录浏览会话已满，请稍后重试。",
  material_namespace_changed: "目录在浏览期间发生变化，请重新打开目录。",
  material_source_changed: "来源已经变化，请重新检查并选择。",
  material_safe_reader_unavailable: "此服务器目前无法提供安全的原始文件访问。",
  material_kind_mismatch: "此处需要的文件或目录类型与所选路径不一致。",
  material_response_invalid: "服务器返回的材料元数据无法校验，请重新检查。",
  material_unavailable: "服务器暂时无法访问此路径。",
  material_request_timeout: "服务器材料请求超时，请重试。",
  material_receiver_stale: "接收工作已经变化，此封存提交未被接受。请放弃此命令并重新选择接收位置。",
  material_receiver_unavailable: "当前没有可接收材料的实际工作。",
  material_receiver_closed: "原创建上下文已经关闭，无法接收新材料。",
  material_pending_exists: "已有未完成的封存提交，请先重试或明确放弃。",
  material_pending_invalid: "本地封存提交无法校验。请保留现场并刷新重试。",
  material_pending_storage_unavailable: "浏览器无法保存封存提交。请恢复浏览器存储后重试。",
  material_session_changed: "登录会话已经变化。原封存提交仍保留在原会话中。",
  csrf_token_unavailable: "会话校验信息缺失，请刷新并重新登录。",
  csrf_invalid: "会话校验已失效，请刷新并重新登录。",
};

export function workMaterialErrorMessage(error: unknown): string {
  const code = error instanceof WorkMaterialError ? error.code : "material_unavailable";
  if (code === "request_failed:401") return "登录已失效，请重新登录。";
  return materialErrors[code] ?? `材料请求失败（${code}）。`;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function server(value: unknown): value is MaterialServer {
  return record(value) && ["server_ref", "hostname", "platform", "permission_context"]
    .every(key => typeof value[key] === "string" && value[key] !== "");
}

function observation(value: unknown): value is MaterialObservation {
  return record(value) && ["device", "inode", "size", "modified_ns", "changed_ns"]
    .every(key => typeof value[key] === "string" && /^-?\d+$/.test(value[key] as string))
    && ["file", "directory", "unsupported"].includes(String(value.kind))
    && typeof value.observation_ref === "string" && value.observation_ref !== "";
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  const controller = new AbortController();
  const deadline = window.setTimeout(() => controller.abort(), 12_000);
  try {
    const response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      headers: { Accept: "application/json", ...init?.headers },
      signal: controller.signal,
    });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = record(payload) && record(payload.detail) ? payload.detail : null;
      throw new WorkMaterialError(typeof detail?.code === "string" ? detail.code : `request_failed:${response.status}`, response.status);
    }
    return payload;
  } catch (error) {
    if (controller.signal.aborted) throw new WorkMaterialError("material_request_timeout");
    throw error;
  } finally {
    window.clearTimeout(deadline);
  }
}

export async function browseServerMaterials(path: string, cursor?: string): Promise<ServerMaterialPage> {
  const query = new URLSearchParams({ path, limit: "50" });
  if (cursor) query.set("cursor", cursor);
  const value = await request(`/api/v1/server-materials/browse?${query}`);
  if (!record(value) || !server(value.server) || value.absolute_path !== path
    || value.unexpanded !== true || !(value.next_cursor === null || typeof value.next_cursor === "string")
    || !Array.isArray(value.entries) || !value.entries.every(entry => record(entry)
      && typeof entry.name === "string" && typeof entry.absolute_path === "string"
      && ["file", "directory", "unsupported", "unknown"].includes(String(entry.kind))
      && typeof entry.availability === "string"
      && (entry.observation === undefined || observation(entry.observation)))) {
    throw new WorkMaterialError("material_response_invalid");
  }
  return value as ServerMaterialPage;
}

export async function inspectServerMaterial(path: string, description: string): Promise<ServerMaterialSelection> {
  const query = new URLSearchParams({ path, description });
  const value = await request(`/api/v1/server-materials/inspect?${query}`);
  if (!record(value) || !server(value.server) || value.absolute_path !== path
    || !["file", "directory"].includes(String(value.kind)) || value.description !== description
    || value.availability !== "available" || !observation(value.observation)
    || value.observation.kind !== value.kind) throw new WorkMaterialError("material_response_invalid");
  return value as ServerMaterialSelection;
}

export async function cancelServerMaterialCursor(cursor: string): Promise<void> {
  const prefix = "meta_research_csrf=";
  const cookie = document.cookie.split(";").map(value => value.trim()).find(value => value.startsWith(prefix));
  if (!cookie) throw new WorkMaterialError("csrf_token_unavailable");
  const csrf = decodeURIComponent(cookie.slice(prefix.length));
  await request(`/api/v1/server-materials/cursors/${encodeURIComponent(cursor)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: "{}",
    keepalive: true,
  });
}
