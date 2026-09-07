import { useEffect, useRef, useState } from "react";
import { ResearchIcon, SpectrumStages, spectrumStage } from "./Spectrum";
import "./status-home.css";

type Health = { status: string; checks: { name: string; status: string; reason?: { code: string } }[] };
type RuntimeStatus = {
  schema_ref: "meta-research/runtime-status/v1";
  revision: number;
  observed_at: string;
  updated_at: string;
  state: string;
  current_task: { kind: string; title: string; run_ref: string | null; target_ref: string | null; status: string } | null;
  waiting_reason: string | null;
  pending_requests: number;
  foreground: { stage: string; question_ref: string; cycle_ref: string } | null;
  health: Health;
};

// Shared across StrictMode remounts. A discarded view does not start a duplicate GET.
let pendingStatus: Promise<RuntimeStatus> | null = null;
function readStatus(): Promise<RuntimeStatus> {
  if (pendingStatus) return pendingStatus;
  const controller = new AbortController();
  const deadline = window.setTimeout(() => controller.abort(), 4_000);
  const request = (async () => {
    const response = await fetch("/api/v1/status", {
      credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal,
    });
    if (!response.ok) throw new Error(`status_unavailable:${response.status}`);
    const value = await response.json() as RuntimeStatus;
    if (value.schema_ref !== "meta-research/runtime-status/v1" || !Number.isInteger(value.revision)
      || !value.observed_at || !value.updated_at || !value.health?.checks) throw new Error("status_response_invalid");
    return value;
  })().finally(() => {
    window.clearTimeout(deadline);
    if (pendingStatus === request) pendingStatus = null;
  });
  pendingStatus = request;
  return request;
}

const states: Record<string, string> = {
  running: "执行中", advancing: "推进中", waiting: "等待继续条件", paused: "已暂停",
  completed: "已完成", pending: "等待接续", idle: "尚未开始", failed: "执行异常",
};
const stages: Record<string, string> = { idea: "研究思路", plan: "验证计划", bundle: "实验与证据", reasoning: "研究判断" };
function timeLabel(value: string): string {
  const time = new Date(value);
  return Number.isNaN(time.getTime()) ? value : time.toLocaleString("zh-CN", { hour12: false });
}

export function StatusHome() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const refresh = useRef<() => void>(() => undefined);
  useEffect(() => {
    let stopped = false;
    let running = false;
    let failures = 0;
    let timer: number | undefined;
    const reload = async () => {
      if (stopped || running) return;
      running = true;
      window.clearTimeout(timer);
      setLoading(true);
      try {
        const next = await readStatus();
        if (stopped) return;
        setStatus(current => current && current.revision > next.revision ? current : next);
        setError(false);
        failures = 0;
      } catch {
        if (!stopped) { setError(true); failures += 1; }
      } finally {
        running = false;
        if (!stopped) {
          setLoading(false);
          // Schedule from completion; slow or failed requests never accumulate.
          timer = window.setTimeout(() => void reload(), failures ? Math.min(5_000 * 2 ** (failures - 1), 30_000) : 5_000);
        }
      }
    };
    refresh.current = () => void reload();
    void reload();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, []);
  const unhealthy = status?.health.checks.filter(check => check.status !== "ready") ?? [];
  return <div className="status-home">
    <header className="status-home-header">
      <a href="/" aria-label="Meta Research 首页"><span className="lumen-logo" aria-hidden="true">MR</span><span><b>Meta Research</b><small>LUMEN WORKSPACE</small></span></a>
      <nav aria-label="工作台导航"><a className="is-active" href="/" aria-current="page">工作台</a><a href="/?workspace=1">研究现场</a><a href="/?workspace=1&companion=1">研究助手 <span>↗</span></a></nav>
    </header>
    <main id="main-content">
      <div className="status-home-intro"><div><p className="home-eyebrow">从一个问题，到新的发现</p><h2>研究工作台<span> / Workspace</span></h2></div><p className="home-intro-note">思路、验证、证据与判断。<br />在同一条研究脉络里，继续探索。</p></div>
      <section className="status-home-current" aria-labelledby="current-task-title" data-testid="runtime-status">
        <SpectrumStages current={spectrumStage(status?.foreground?.stage)} stale={error} />
        <div className="home-task-panel">
        <div className="home-task-copy">
        <div className="status-home-topline">
          <p className="status-home-caption">{status?.current_task?.kind === "target" ? "当前实验任务" : "当前任务"}</p>
          <span className="status-state" data-state={error ? "stale" : status?.state ?? "pending"}>
            <i />{error ? "刷新失败" : status ? states[status.state] ?? "状态待确认" : "读取状态…"}
          </span>
        </div>
        <h1 id="current-task-title">{status?.current_task?.title ?? (status ? "还没有运行中的研究任务" : error ? "暂时无法读取运行状态" : "正在读取当前任务")}</h1>
        {error ? <p className="status-home-error" role="alert">刷新失败。{status ? "下面保留上次成功读取的状态，不代表此刻的最新进度。" : "请稍后重试；尚未取得有效状态。"}</p> : null}
        <div className="status-home-primary">
          <a className="status-home-primary-link" href={status?.state === "idle" ? "/?panel=create-quest" : "/?workspace=1"}>{status?.state === "idle" ? "开始研究" : "当前任务与实验日志"} <span>↗</span></a>
          {status && status.pending_requests > 0 ? <a className="status-home-response" href="/?panel=human-requests">需要你回应 · {status.pending_requests} 项 ↗</a> : null}
        </div>
        </div>
        <div className="home-task-details">
        <div className="home-detail-title"><span>运行摘要{status?.foreground ? ` / ${stages[status.foreground.stage] ?? status.foreground.stage}` : ""}</span><button type="button" disabled={loading} onClick={() => refresh.current()}>{loading ? "刷新中…" : "刷新状态"}</button></div>
        <dl className="status-home-facts">
          <div><dt>任务状态{error && status ? " · 上次记录" : ""}</dt><dd>{status ? states[status.state] ?? status.state : "待确认"}</dd></div>
          <div><dt>等待原因</dt><dd>{status ? status.waiting_reason ?? "未记录等待原因" : "待确认"}</dd></div>
          <div><dt>数据更新时间</dt><dd><time dateTime={status?.updated_at}>{status ? timeLabel(status.updated_at) : "—"}</time></dd></div>
        </dl>
        </div>
        </div>
      </section>
      <nav className="status-home-links" aria-label="按需查看研究详情">
        <a href="/?panel=question-tree&inspector=history"><ResearchIcon glyph="↺" /><div><b>研究历史 <span>↗</span></b><p>回看问题、轮次与研究发现</p></div></a>
        <a href="/?panel=research-assets"><ResearchIcon glyph="▤" /><div><b>研究资料 <span>↗</span></b><p>整理数据、文献与证据</p></div></a>
        <a href="/?panel=writing"><ResearchIcon glyph="✎" /><div><b>报告与写作 <span>↗</span></b><p>查看交付物与写作进展</p></div></a>
      </nav>
      <footer className="status-home-footer">
        <div><b>{status ? error ? "工作器状态 · 上次记录" : status.health.status === "ready" ? "工作器在线" : "部分工作器暂不可用" : "工作器状态待确认"}</b>
          <p>上次成功查询：<time dateTime={status?.observed_at}>{status ? timeLabel(status.observed_at) : "尚无"}</time>{status ? ` · 状态版本 ${status.revision}` : ""}</p></div>
        {unhealthy.length ? <details><summary>查看工作器状态 · {unhealthy.length} 项</summary><ul>{unhealthy.map(check => <li key={check.name}>{check.name}：{check.reason?.code ?? check.status}</li>)}</ul></details> : null}
      </footer>
    </main>
  </div>;
}
