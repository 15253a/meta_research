import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { changeCodexAccount, fetchCodexAccount, type CodexAccountView } from "./codexAccountApi";
import "./codex-account.css";

const plans: Record<string, string> = { plus: "Plus", pro: "Pro", free: "Free", team: "Team", business: "Business", enterprise: "Enterprise", edu: "Edu" };
function verificationLink(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "auth.openai.com" && !url.username && !url.password && (!url.port || url.port === "443") ? url.href : null;
  } catch { return null; }
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
  const close = () => { setOpen(false); setConfirmLogout(false); window.requestAnimationFrame(() => trigger.current?.focus({ preventScroll: true })); };
  useEffect(() => { if (disabled) { setOpen(false); setConfirmLogout(false); } }, [disabled]);
  useEffect(() => {
    if (open && !disabled) panel.current?.showModal();
    else panel.current?.close();
  }, [open, disabled]);
  const label = failed || view?.status === "unavailable" ? "状态未知" : view?.status === "authenticated" ? view.account?.email ?? (view.account?.type === "apiKey" ? "API Key" : "已登录") : view?.status === "unauthenticated" ? "未登录" : "读取中…";
  const url = verificationLink(view?.login?.verification_url);
  return <>
    <button ref={trigger} type="button" className="codex-account-entry" disabled={disabled} aria-haspopup="dialog" aria-label={`Codex · ${label}`}
      title={`Codex · ${label}`} onClick={() => { setOpen(true); void refresh(); }}>
      <span>Codex</span><span className="codex-account-label"> · {label}</span>
    </button>
    {open && !disabled ? createPortal(<dialog ref={panel} className="codex-account-dialog" aria-labelledby="codex-account-title" onCancel={event => { event.preventDefault(); close(); }}>
      <header><h2 id="codex-account-title">Codex 账户</h2><button type="button" aria-label="关闭 Codex 账户" onClick={close}>关闭</button></header>
      <p>此账户用于这台服务的 Codex 调用。</p>
      {view?.account ? <dl><div><dt>账户</dt><dd>{view.account.email ?? "未提供邮箱"}</dd></div><div><dt>登录方式</dt><dd>{view.account.type === "chatgpt" ? "ChatGPT" : "API Key"}</dd></div><div><dt>套餐</dt><dd>{plans[view.account.plan_type ?? ""] ?? view.account.plan_type ?? "未提供"}</dd></div></dl> : <p>{label}</p>}
      {failed || view?.status === "unavailable" ? <p role="alert">暂时无法确认账户状态。{view?.account ? "上方保留上次读取的账户。" : "请刷新后重试。"}</p> : null}
      <div className="codex-account-actions">
        <button type="button" disabled={busy || loading} onClick={() => void refresh()}>{loading ? "刷新中…" : "刷新账户"}</button>
        <button type="button" disabled={busy || pending} onClick={() => void act("login")}>{view?.status === "authenticated" ? "切换账户" : "登录 Codex"}</button>
        {view?.account ? <button type="button" disabled={busy || pending} onClick={() => setConfirmLogout(true)}>退出账户</button> : null}
      </div>
      {confirmLogout ? <section className="codex-logout-confirm" aria-label="确认退出 Codex">
        <p>退出此账户后，新的 Codex 调用需要重新登录。确认退出？</p>
        <div className="codex-account-actions"><button type="button" disabled={busy} onClick={() => void act("logout")}>确认退出</button><button type="button" onClick={() => setConfirmLogout(false)}>保留当前账户</button></div>
      </section> : null}
      {pending ? <section className="codex-device-login" aria-label="Codex 登录验证" aria-live="polite">
        <h3>完成 Codex 登录</h3>
        <p>{view?.account ? "完成新账户登录前，当前账户继续保留。" : "打开官方登录页面，输入下面的验证码。"}</p>
        {view?.login?.user_code ? <div className="codex-device-code"><code>{view.login.user_code}</code><button type="button" onClick={() => { if (!navigator.clipboard) { setError("请手动复制验证码。"); return; } void navigator.clipboard.writeText(view.login!.user_code!).then(() => setCopied(true)).catch(() => setError("复制失败，请手动复制验证码。")); }}>{copied ? "已复制" : "复制验证码"}</button></div> : <p>正在取得验证码…</p>}
        {url ? <a href={url} target="_blank" rel="noopener noreferrer">打开登录页面</a> : view?.login?.verification_url ? <p role="alert">未取得可信的官方登录地址，请取消后重试。</p> : null}
        <p>等待登录完成。关闭此面板后仍可继续登录。</p>
        <p>若提示设备码登录不可用，请在 ChatGPT 安全设置中启用后重试。</p>
        <button type="button" disabled={busy} onClick={() => void act("login/cancel", { login_id: view!.login!.id })}>取消登录</button>
      </section> : null}
      {view?.login?.status === "completed" ? <p role="status">Codex 账户已更新。</p> : view?.login?.status === "cancelled" ? <p role="status">已取消本次登录。</p> : view?.login?.status === "failed" ? <p role="alert">登录未完成或已过期，请重新登录。{view.account ? "原账户仍保留。" : ""}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </dialog>, document.body) : null}
  </>;
}
