import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { changeCodexAccount, fetchCodexAccount, type CodexAccountView, type CodexQuotaView } from "./codexAccountApi";
import { MetaTrace } from "./MetaTrace";
import "./codex-account.css";

const plans: Record<string, string> = { plus: "Plus", pro: "Pro", free: "Free", team: "Team", business: "Business", enterprise: "Enterprise", edu: "Edu" };

function verificationLink(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "auth.openai.com" && !url.username && !url.password && (!url.port || url.port === "443") ? url.href : null;
  } catch { return null; }
}

function formatPercent(value: number): string {
  return Number.isInteger(value) ? `${value}%` : `${value.toFixed(1).replace(/\.0$/, "")}%`;
}

function formatWindow(minutes: number | null): string | null {
  if (minutes === null) return null;
  if (minutes % 1440 === 0) return `${minutes / 1440} 天周期`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时周期`;
  return `${minutes} 分钟周期`;
}

function formatResetTime(timestamp: number | null): string | null {
  if (timestamp === null) return null;
  try {
    const ms = timestamp > 1e12 ? timestamp : timestamp * 1000;
    const date = new Date(ms);
    if (Number.isNaN(date.getTime())) return null;
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return null;
  }
}

function quotaHealth(quota: CodexQuotaView): "healthy" | "low" | "exhausted" {
  if (quota.exhausted) return "exhausted";
  if (quota.remaining_percent !== null && quota.remaining_percent <= 20) return "low";
  return "healthy";
}

export function CodexAccount({ disabled = false }: { disabled?: boolean }) {
  const [view, setView] = useState<CodexAccountView | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmLogout, setConfirmLogout] = useState(false);
  const alive = useRef(false);
  const generation = useRef(0);
  const mutating = useRef(false);
  const reading = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const autoPromptedKey = useRef<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDialogElement>(null);

  const refresh = useCallback(async () => {
    if (mutating.current) return;
    reading.current?.abort();
    const controller = new AbortController(), request = ++generation.current;
    reading.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 15_000);
    setLoading(true);
    try {
      const next = await fetchCodexAccount(controller.signal);
      if (alive.current && request === generation.current) { setView(next); setFailed(false); }
    } catch {
      if (alive.current && request === generation.current) setFailed(true);
    } finally {
      clearTimeout(timeout);
      if (reading.current === controller) reading.current = null;
      if (alive.current && request === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const visible = () => { if (!document.hidden) void refresh(); };
    document.addEventListener("visibilitychange", visible);
    return () => { alive.current = false; ++generation.current; reading.current?.abort(); mutation.current?.abort(); document.removeEventListener("visibilitychange", visible); };
  }, [refresh]);

  const pending = view?.login?.status === "pending";

  useEffect(() => {
    const timer = window.setInterval(() => { if (!document.hidden && !reading.current) void refresh(); }, pending ? 2_000 : 30_000);
    return () => clearInterval(timer);
  }, [pending, refresh]);

  useEffect(() => {
    if (disabled || loading || failed || !view) return;
    const gateKey =
      view.status === "unauthenticated"
        ? "unauthenticated"
        : view.status === "authenticated" && view.quota?.exhausted
          ? `exhausted:${view.account?.email ?? view.account?.type ?? "account"}`
          : null;
    if (gateKey === null) {
      autoPromptedKey.current = null;
    } else if (autoPromptedKey.current !== gateKey) {
      autoPromptedKey.current = gateKey;
      setOpen(true);
    }
  }, [view, failed, loading, disabled]);

  const act = async (action: "login" | "login/cancel" | "logout", body: object = {}) => {
    if (mutating.current) return;
    mutating.current = true;
    reading.current?.abort(); reading.current = null;
    const request = ++generation.current, controller = new AbortController();
    mutation.current = controller;
    setBusy(true); setLoading(false); setError(null); setCopied(false); setConfirmLogout(false);
    const timeout = window.setTimeout(() => controller.abort(), 20_000);
    try {
      const next = await changeCodexAccount(action, body, controller.signal);
      if (alive.current && request === generation.current) {
        setView(next); setFailed(false);
        if (next.error_code) setError("操作未完成，请刷新账户状态后重试。");
      }
    } catch {
      if (alive.current && request === generation.current) setError("操作未完成，请刷新账户状态后重试。");
    } finally {
      clearTimeout(timeout); mutating.current = false;
      if (alive.current && request === generation.current) setBusy(false);
    }
  };

  const close = () => {
    setOpen(false);
    setConfirmLogout(false);
    window.requestAnimationFrame(() => trigger.current?.focus({ preventScroll: true }));
  };

  useEffect(() => { if (disabled) { setOpen(false); setConfirmLogout(false); } }, [disabled]);

  useEffect(() => {
    if (open && !disabled) {
      if (panel.current && !panel.current.open) panel.current.showModal();
    } else if (panel.current?.open) {
      panel.current.close();
    }
  }, [open, disabled]);

  const label = failed || view?.status === "unavailable"
    ? "状态未知"
    : view?.status === "authenticated"
      ? view.account?.email ?? (view.account?.type === "apiKey" ? "API Key" : "已登录")
      : view?.status === "unauthenticated"
        ? "未登录"
        : "读取中…";

  const visualState = failed || view?.status === "unavailable"
    ? "unavailable"
    : view?.status === "authenticated"
      ? view.quota?.exhausted
        ? "exhausted"
        : "authenticated"
      : view?.status === "unauthenticated"
        ? "unauthenticated"
        : "loading";

  const statusBadgeText = visualState === "authenticated"
    ? "已连接"
    : visualState === "exhausted"
      ? "额度受限"
      : visualState === "unauthenticated"
        ? "未登录"
        : visualState === "unavailable"
          ? "状态未知"
          : "读取中…";

  const url = verificationLink(view?.login?.verification_url);
  const quota = view?.quota ?? null;
  const qHealth = quota ? quotaHealth(quota) : null;
  const windowLabel = quota ? formatWindow(quota.window_duration_mins) : null;
  const resetLabel = quota ? formatResetTime(quota.resets_at) : null;
  const meterPercent = quota
    ? quota.used_percent !== null
      ? Math.max(0, Math.min(100, quota.used_percent))
      : quota.exhausted
        ? 100
        : null
    : null;

  const quotaHeadline = quota
    ? quota.exhausted
      ? "额度已耗尽"
      : quota.unlimited
        ? "无限制"
        : quota.remaining_percent !== null
          ? `剩余 ${formatPercent(quota.remaining_percent)}`
          : quota.ordinary_usage_allowed === true
            ? "额度可用"
            : null
    : null;

  return <>
    <button
      ref={trigger}
      type="button"
      className="codex-account-entry"
      data-state={visualState}
      disabled={disabled}
      aria-haspopup="dialog"
      aria-label={`Codex · ${label}`}
      title={`Codex · ${label}`}
      onClick={() => { setOpen(true); void refresh(); }}
    >
      <span>Codex</span><span className="codex-account-label"> · {label}</span>
    </button>
    {open && !disabled ? createPortal(
      <dialog
        ref={panel}
        className="codex-account-dialog"
        data-state={visualState}
        aria-labelledby="codex-account-title"
        onCancel={event => { event.preventDefault(); close(); }}
      >
        <div className="codex-dialog-accent" aria-hidden="true" />
        <div className="codex-dialog-body">
          <header className="codex-dialog-header">
            <div className="codex-dialog-title-group">
              <span className="codex-brand-badge" aria-hidden="true">
                <MetaTrace variant="brief" />
              </span>
              <div className="codex-dialog-heading">
                <div className="codex-dialog-title-row">
                  <h2 id="codex-account-title">Codex 账户</h2>
                  <span className="codex-status-pill" data-state={visualState}>{statusBadgeText}</span>
                </div>
                <p className="codex-dialog-subtitle">此账户用于这台服务的 Codex 调用。</p>
              </div>
            </div>
            <button type="button" className="codex-dialog-close" aria-label="关闭 Codex 账户" onClick={close}>关闭</button>
          </header>

          {view?.account ? (
            <dl className="codex-identity-card">
              <div>
                <dt>账户</dt>
                <dd className="codex-identity-email">{view.account.email ?? "未提供邮箱"}</dd>
              </div>
              <div>
                <dt>登录方式</dt>
                <dd>{view.account.type === "chatgpt" ? "ChatGPT" : "API Key"}</dd>
              </div>
              <div>
                <dt>套餐</dt>
                <dd>
                  <span className="codex-plan-pill">{plans[view.account.plan_type ?? ""] ?? view.account.plan_type ?? "未提供"}</span>
                </dd>
              </div>
            </dl>
          ) : visualState === "unauthenticated" ? (
            <section className="codex-unauth-hero" aria-label="Codex 登录提示">
              <div className="codex-hero-icon" aria-hidden="true">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="3" y="11" width="18" height="10" rx="2.5" />
                  <path d="M7 11V7a5 5 0 0 1 10 0v4" />
                  <circle cx="12" cy="16" r="1.2" fill="currentColor" stroke="none" />
                </svg>
              </div>
              <div className="codex-hero-copy">
                <h3>连接您的 Codex 账户</h3>
                <p>请先登录您的 Codex 账户以启动研究任务，登录后将自动检测可用额度。</p>
              </div>
            </section>
          ) : (
            <p className="codex-empty-status">{label}</p>
          )}

          {quota ? (
            <section className="codex-quota-card" data-health={qHealth ?? "healthy"} aria-label="Codex 额度状态">
              <div className="codex-quota-header">
                <div className="codex-quota-label-group">
                  <span className="codex-quota-title">当前额度</span>
                  {windowLabel ? <span className="codex-quota-window">{windowLabel}</span> : null}
                </div>
                {quotaHeadline ? <strong className="codex-quota-headline">{quotaHeadline}</strong> : null}
              </div>

              {meterPercent !== null ? (
                <div
                  className="codex-quota-track"
                  role="progressbar"
                  aria-label="Codex 额度使用率"
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-valuenow={Math.round(meterPercent)}
                >
                  <div className="codex-quota-fill" style={{ width: `${meterPercent}%` }} />
                </div>
              ) : null}

              <div className="codex-quota-meta">
                {quota.used_percent !== null ? <span>{`已使用 ${formatPercent(quota.used_percent)}`}</span> : null}
                {resetLabel ? <span>{`重置时间：${resetLabel}`}</span> : null}
                {quota.secondary_used_percent !== null ? <span>{`次级窗口已用 ${formatPercent(quota.secondary_used_percent)}`}</span> : null}
                {quota.unlimited ? (
                  <span>无限积分</span>
                ) : quota.has_credits && quota.balance ? (
                  <span>{`积分余额：${quota.balance}`}</span>
                ) : quota.has_credits ? (
                  <span>包含可用积分</span>
                ) : null}
              </div>

              {quota.exhausted ? (
                <div className="codex-quota-warning" role="status">
                  当前账户可用额度已耗尽或受限，建议点击下方「切换账户」登录其他 Codex 账户，或等待额度重置后再试。
                </div>
              ) : null}
            </section>
          ) : null}

          {failed || view?.status === "unavailable" ? (
            <p role="alert" className="codex-alert">
              暂时无法确认账户状态。{view?.account ? "上方保留上次读取的账户。" : "请刷新后重试。"}
            </p>
          ) : null}

          <div className="codex-account-actions">
            <button
              type="button"
              className="codex-btn-primary"
              disabled={busy || pending}
              onClick={() => void act("login")}
            >
              {view?.status === "authenticated" ? "切换账户" : "登录 Codex"}
            </button>
            <button
              type="button"
              className="codex-btn-secondary"
              disabled={busy || loading}
              onClick={() => void refresh()}
            >
              {loading ? "刷新中…" : "刷新账户"}
            </button>
            {view?.account ? (
              <button
                type="button"
                className="codex-btn-danger-ghost"
                disabled={busy || pending}
                onClick={() => setConfirmLogout(true)}
              >
                退出账户
              </button>
            ) : null}
          </div>

          {confirmLogout ? (
            <section className="codex-logout-confirm" aria-label="确认退出 Codex">
              <p>退出此账户后，新的 Codex 调用需要重新登录。确认退出？</p>
              <div className="codex-account-actions">
                <button type="button" className="codex-btn-danger" disabled={busy} onClick={() => void act("logout")}>确认退出</button>
                <button type="button" className="codex-btn-secondary" onClick={() => setConfirmLogout(false)}>保留当前账户</button>
              </div>
            </section>
          ) : null}

          {pending ? (
            <section className="codex-device-login" aria-label="Codex 登录验证" aria-live="polite">
              <div className="codex-device-header">
                <h3>完成 Codex 登录</h3>
                <span className="codex-live-badge" aria-hidden="true">
                  <span className="codex-live-dot" />
                  等待验证
                </span>
              </div>
              <p className="codex-device-intro">
                {view?.account ? "完成新账户登录前，当前账户继续保留。" : "打开官方登录页面，输入下面的验证码。"}
              </p>

              <div className="codex-device-steps">
                <div className="codex-device-step">
                  <span className="codex-step-num" aria-hidden="true">1</span>
                  <div className="codex-step-body">
                    <span className="codex-step-label">复制下方设备验证码</span>
                    {view?.login?.user_code ? (
                      <div className="codex-device-code">
                        <code>{view.login.user_code}</code>
                        <button
                          type="button"
                          className={`codex-btn-copy${copied ? " is-copied" : ""}`}
                          onClick={() => {
                            if (!navigator.clipboard) { setError("请手动复制验证码。"); return; }
                            void navigator.clipboard.writeText(view.login!.user_code!).then(() => setCopied(true)).catch(() => setError("复制失败，请手动复制验证码。"));
                          }}
                        >
                          {copied ? "已复制" : "复制验证码"}
                        </button>
                      </div>
                    ) : (
                      <p className="codex-step-loading">正在取得验证码…</p>
                    )}
                  </div>
                </div>

                <div className="codex-device-step">
                  <span className="codex-step-num" aria-hidden="true">2</span>
                  <div className="codex-step-body">
                    <span className="codex-step-label">前往官方授权页面粘贴验证码</span>
                    {url ? (
                      <a className="codex-device-link" href={url} target="_blank" rel="noopener noreferrer">
                        <span>打开登录页面</span>
                        <svg aria-hidden="true" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M6.5 3.5H12.5V9.5" />
                          <path d="M12.5 3.5L5.5 10.5" />
                          <path d="M11 12.5H3.5V5" />
                        </svg>
                      </a>
                    ) : view?.login?.verification_url ? (
                      <p role="alert" className="codex-alert">未取得可信的官方登录地址，请取消后重试。</p>
                    ) : null}
                  </div>
                </div>
              </div>

              <div className="codex-device-footer">
                <p className="codex-device-wait">等待登录完成。关闭此面板后仍可继续登录。</p>
                <p className="codex-device-tip">若提示设备码登录不可用，请在 ChatGPT 安全设置中启用后重试。</p>
                <button
                  type="button"
                  className="codex-btn-ghost"
                  disabled={busy}
                  onClick={() => void act("login/cancel", { login_id: view!.login!.id })}
                >
                  取消登录
                </button>
              </div>
            </section>
          ) : null}

          {view?.login?.status === "completed" ? (
            <p role="status" className="codex-status codex-status-success">Codex 账户已更新。</p>
          ) : view?.login?.status === "cancelled" ? (
            <p role="status" className="codex-status">已取消本次登录。</p>
          ) : view?.login?.status === "failed" ? (
            <p role="alert" className="codex-alert">
              登录未完成或已过期，请重新登录。{view.account ? "原账户仍保留。" : ""}
            </p>
          ) : null}

          {error ? <p role="alert" className="codex-alert">{error}</p> : null}
        </div>
      </dialog>,
      document.body
    ) : null}
  </>;
}
