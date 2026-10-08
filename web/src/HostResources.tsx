import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import "./host-resources.css";

type Metric = { status: "ready" | "unavailable"; source: string; reason_code: string | null };
type Device = { uuid: string; name: string; utilization_percent: number | null; memory_used_mib: number | null; memory_total_mib: number | null; reason_code?: string | null };
export type HostResourceSample = {
  schema_ref: "meta-research/runtime-resources/v1";
  scope: "execution_host";
  host: { hostname: string };
  observed_at: number;
  sampled_from: number;
  sampled_to: number;
  refresh_interval_seconds: number;
  cpu: Metric & { utilization_percent: number | null; sampled_from: number; sampled_to: number };
  memory: Metric & { used_bytes: number | null; total_bytes: number | null; observed_at: number };
  gpu: { status: "ready" | "no_devices" | "unavailable"; source: string; reason_code: string | null; devices: Device[]; observed_at: number };
};

function percent(value: number | null): string {
  return value === null ? "缺测" : `${Number(value.toFixed(1))}%`;
}
function gib(value: number): string { return Number((value / 1024 ** 3).toFixed(1)).toString(); }
function time(value: number): string { return new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3 }); }

function ResourceReadout() {
  const [sample, setSample] = useState<HostResourceSample | null>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const refresh = useRef<() => void>(() => undefined);
  useEffect(() => {
    let stopped = false, running = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;
    const read = async () => {
      if (stopped || running || document.hidden) return;
      running = true;
      clearTimeout(timer);
      setLoading(true);
      controller = new AbortController();
      const deadline = window.setTimeout(() => controller?.abort(), 4_000);
      try {
        const response = await fetch("/api/v1/runtime/resources", { credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal });
        if (!response.ok) throw new Error("resources_unavailable");
        const next = await response.json() as HostResourceSample;
        if (next.schema_ref !== "meta-research/runtime-resources/v1" || next.scope !== "execution_host"
          || !Number.isFinite(next.observed_at) || !Number.isFinite(next.sampled_from) || !Number.isFinite(next.sampled_to)
          || !next.host?.hostname || !next.cpu || !next.memory || !Array.isArray(next.gpu?.devices)) throw new Error("resources_invalid");
        if (!stopped) { setSample(next); setFailed(false); }
      } catch {
        if (!stopped) setFailed(true);
      } finally {
        clearTimeout(deadline);
        running = false;
        if (!stopped) {
          setLoading(false);
          if (!document.hidden) timer = window.setTimeout(() => void read(), 5_000);
        }
      }
    };
    refresh.current = () => void read();
    const visible = () => { if (document.hidden) clearTimeout(timer); else void read(); };
    document.addEventListener("visibilitychange", visible);
    void read();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); document.removeEventListener("visibilitychange", visible); };
  }, []);
  return <>
    <p className="host-resource-scope">执行机器整机口径，包含该机器上的全部工作。</p>
    <div className="host-resource-refresh"><span>{failed ? "资源读取失败" : loading ? "读取采样…" : "采样已更新"}</span><button type="button" disabled={loading} onClick={() => refresh.current()}>刷新资源</button></div>
    {failed ? <p className="host-resource-warning" role="status">{sample ? "保留上次成功采样，当前状态未知。" : "尚未取得采样，当前状态未知。"}</p> : null}
    {sample ? <>
      <p className="host-resource-host">{sample.host.hostname}{failed ? " · 上次采样" : ""}</p>
      <p className="host-resource-time">采样时间 <time dateTime={new Date(sample.observed_at * 1000).toISOString()}>{new Date(sample.observed_at * 1000).toLocaleString("zh-CN", { hour12: false })}</time><br />CPU 采样区间 {time(sample.cpu.sampled_from)} 至 {time(sample.cpu.sampled_to)} · {Math.round((sample.cpu.sampled_to - sample.cpu.sampled_from) * 1000)} ms<br />内存采样时间 {time(sample.memory.observed_at)} · GPU 采样时间 {time(sample.gpu.observed_at)}</p>
      <div className="host-resource-metrics">
        <article data-testid="host-cpu"><h3>CPU</h3><b>{sample.cpu.status === "ready" ? percent(sample.cpu.utilization_percent) : "缺测"}</b><small>整机利用率 · {sample.cpu.source}</small>{sample.cpu.reason_code ? <span>{sample.cpu.reason_code}</span> : null}</article>
        <article data-testid="host-memory"><h3>内存</h3><b>{sample.memory.status === "ready" && sample.memory.used_bytes !== null && sample.memory.total_bytes !== null ? `${gib(sample.memory.used_bytes)} / ${gib(sample.memory.total_bytes)} GiB` : "缺测"}</b><small>已用 / 总量 · {sample.memory.source}</small>{sample.memory.reason_code ? <span>{sample.memory.reason_code}</span> : null}</article>
      </div>
      <h3 className="host-gpu-heading">GPU <small>{sample.gpu.source}</small></h3>
      {sample.gpu.status === "no_devices" ? <p>未检测到 GPU 设备</p> : sample.gpu.status === "unavailable" ? <p className="host-resource-warning">GPU 采集不可用 · {sample.gpu.reason_code ?? "缺测"}</p> : <ul className="host-gpu-list">{sample.gpu.devices.map(device => <li key={device.uuid} data-gpu-uuid={device.uuid}>
        <div><b>{device.name}</b><code>{device.uuid}</code></div>
        <dl><div><dt>利用率</dt><dd>{percent(device.utilization_percent)}</dd></div><div><dt>显存 · 已用 / 总量</dt><dd>{device.memory_used_mib === null || device.memory_total_mib === null ? "缺测" : `${device.memory_used_mib} / ${device.memory_total_mib} MiB`}</dd></div></dl>
        {device.reason_code ? <small>{device.reason_code}</small> : null}
      </li>)}</ul>}
    </> : !failed ? <p>尚未取得采样</p> : null}
  </>;
}

export function HostResources({ disabled = false }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const close = () => { setOpen(false); trigger.current?.focus(); };
  useEffect(() => {
    if (disabled) { setOpen(false); return; }
    if (!open) return;
    panel.current?.focus();
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.stopPropagation(); setOpen(false); trigger.current?.focus(); }
    };
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, [open, disabled]);
  return <div className="host-resources">
    <button ref={trigger} type="button" disabled={disabled} className="host-resource-trigger" aria-expanded={open} aria-controls="host-resource-panel" onClick={() => setOpen(value => !value)}>执行机器资源</button>
    {open && !disabled ? createPortal(<section id="host-resource-panel" ref={panel} tabIndex={-1} className="host-resource-panel" aria-label="执行机器整机资源">
      <header><h2>执行机器整机资源</h2><button type="button" aria-label="关闭资源面板" onClick={close}>关闭</button></header>
      <ResourceReadout />
    </section>, document.body) : null}
  </div>;
}
