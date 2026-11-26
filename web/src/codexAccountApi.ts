import { ProductError, writeJson } from "./api";

export type CodexQuotaView = {
  ordinary_usage_allowed: boolean | null;
  used_percent: number | null;
  remaining_percent: number | null;
  window_duration_mins: number | null;
  resets_at: number | null;
  secondary_used_percent: number | null;
  secondary_resets_at: number | null;
  spend_control_reached: boolean | null;
  rate_limit_reached_type: string | null;
  has_credits: boolean | null;
  unlimited: boolean | null;
  balance: string | null;
  exhausted: boolean;
};

export type CodexAccountView = {
  status: "authenticated" | "unauthenticated" | "unavailable";
  account: null | { type: "chatgpt" | "apiKey"; email: string | null; plan_type: string | null };
  quota: CodexQuotaView | null;
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

function optionalText(value: unknown, limit = 64): string | null {
  if (value === undefined || value === null) return null;
  return text(value, limit);
}

function optionalFlag(value: unknown): boolean | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "boolean") throw new ProductError("codex_account_invalid");
  return value;
}

function optionalFiniteNumber(value: unknown): number | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value)) throw new ProductError("codex_account_invalid");
  return value;
}

function optionalPercent(value: unknown): number | null {
  const numeric = optionalFiniteNumber(value);
  if (numeric === null) return null;
  if (numeric < 0 || numeric > 100) throw new ProductError("codex_account_invalid");
  return numeric;
}

function optionalPositiveInt(value: unknown): number | null {
  const numeric = optionalFiniteNumber(value);
  if (numeric === null) return null;
  if (!Number.isInteger(numeric) || numeric <= 0) throw new ProductError("codex_account_invalid");
  return numeric;
}

function optionalPositiveNumber(value: unknown): number | null {
  const numeric = optionalFiniteNumber(value);
  if (numeric === null) return null;
  if (numeric <= 0) throw new ProductError("codex_account_invalid");
  return numeric;
}

function parseQuota(value: unknown): CodexQuotaView | null {
  if (value === undefined || value === null) return null;
  const source = record(value);
  const exhausted = source.exhausted === undefined ? false : source.exhausted;
  if (typeof exhausted !== "boolean") throw new ProductError("codex_account_invalid");
  return {
    ordinary_usage_allowed: optionalFlag(source.ordinary_usage_allowed),
    used_percent: optionalPercent(source.used_percent),
    remaining_percent: optionalPercent(source.remaining_percent),
    window_duration_mins: optionalPositiveInt(source.window_duration_mins),
    resets_at: optionalPositiveNumber(source.resets_at),
    secondary_used_percent: optionalPercent(source.secondary_used_percent),
    secondary_resets_at: optionalPositiveNumber(source.secondary_resets_at),
    spend_control_reached: optionalFlag(source.spend_control_reached),
    rate_limit_reached_type: optionalText(source.rate_limit_reached_type, 64),
    has_credits: optionalFlag(source.has_credits),
    unlimited: optionalFlag(source.unlimited),
    balance: optionalText(source.balance, 64),
    exhausted,
  };
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
  const quota = parseQuota(data.quota);
  let login: CodexAccountView["login"] = null;
  if (data.login !== null) {
    const source = record(data.login), id = text(source.id, 160);
    if (!id || !["pending", "completed", "failed", "cancelled"].includes(String(source.status))) throw new ProductError("codex_account_invalid");
    if (source.expires_at !== null && (typeof source.expires_at !== "number" || !Number.isFinite(source.expires_at))) throw new ProductError("codex_account_invalid");
    login = { id, status: source.status as NonNullable<CodexAccountView["login"]>["status"], verification_url: text(source.verification_url, 2048), user_code: text(source.user_code, 80), error_code: text(source.error_code, 160), expires_at: source.expires_at as number | null };
  }
  return { status: data.status, account, quota, login, error_code: text(data.error_code, 160) };
}

export async function fetchCodexAccount(signal?: AbortSignal): Promise<CodexAccountView> {
  const response = await fetch("/api/v1/codex/account", { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" }, signal });
  if (!response.ok) throw new ProductError("codex_account_unavailable");
  return parse(await response.json());
}

export async function changeCodexAccount(action: "login" | "login/cancel" | "logout", body: object, signal?: AbortSignal): Promise<CodexAccountView> {
  return parse(await writeJson(`/api/v1/codex/account/${action}`, "POST", body, { signal, retainPending: () => false, onRetained: () => undefined }));
}
