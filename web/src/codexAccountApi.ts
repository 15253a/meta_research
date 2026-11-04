import { ProductError, writeJson } from "./api";

export type CodexAccountView = {
  status: "authenticated" | "unauthenticated" | "unavailable";
  account: null | { type: "chatgpt" | "apiKey"; email: string | null; plan_type: string | null };
  login: null | { id: string; status: "pending" | "completed" | "failed" | "cancelled"; verification_url: string | null; user_code: string | null; error_code: string | null; expires_at: number | null };
  error_code: string | null;
};
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ProductError("codex_account_invalid");
  return value as Record<string, unknown>;
};
function text(value: unknown, limit = 320): string | null {
  if (value === null) return null;
  if (typeof value !== "string" || value.length > limit) throw new ProductError("codex_account_invalid");
  return value;
}
function parse(value: unknown): CodexAccountView {
  const data = record(value);
  if (data.status !== "authenticated" && data.status !== "unauthenticated" && data.status !== "unavailable") throw new ProductError("codex_account_invalid");
  let account: CodexAccountView["account"] = null;
  if (data.account !== null) {
    const source = record(data.account);
    if (source.type !== "chatgpt" && source.type !== "apiKey") throw new ProductError("codex_account_invalid");
    account = { type: source.type, email: text(source.email), plan_type: text(source.plan_type, 64) };
  }
  let login: CodexAccountView["login"] = null;
  if (data.login !== null) {
    const source = record(data.login), id = text(source.id, 160);
    if (!id || !["pending", "completed", "failed", "cancelled"].includes(String(source.status))) throw new ProductError("codex_account_invalid");
    if (source.expires_at !== null && (typeof source.expires_at !== "number" || !Number.isFinite(source.expires_at))) throw new ProductError("codex_account_invalid");
    login = { id, status: source.status as NonNullable<CodexAccountView["login"]>["status"], verification_url: text(source.verification_url, 2048), user_code: text(source.user_code, 80), error_code: text(source.error_code, 160), expires_at: source.expires_at as number | null };
  }
  return { status: data.status, account, login, error_code: text(data.error_code, 160) };
}
export async function fetchCodexAccount(signal?: AbortSignal): Promise<CodexAccountView> {
  const response = await fetch("/api/v1/codex/account", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new ProductError("codex_account_unavailable");
  return parse(await response.json());
}
export async function changeCodexAccount(action: "login" | "login/cancel" | "logout", body: object, signal?: AbortSignal): Promise<CodexAccountView> {
  return parse(await writeJson(`/api/v1/codex/account/${action}`, "POST", body, { signal, retainPending: () => false, onRetained: () => undefined }));
}
