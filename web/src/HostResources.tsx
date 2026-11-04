import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
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
  return value === null || !Number.isFinite(value) ? "缺测" : `${Number(value.toFixed(1))}%`;
}
function gib(value: number): string { return Number((value / 1024 ** 3).toFixed(1)).toString(); }
function time(value: number): string { return new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3 }); }
function ratio(used: number | null, total: number | null): number | null {
  return used !== null && total !== null && Number.isFinite(used) && Number.isFinite(total) && total > 0 ? used / total * 100 : null;
}
function ResourceIcon({ close = false }: { close?: boolean }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{close ? <path d="m6 6 12 12M18 6 6 18" /> : <><rect x="3" y="4" width="18" height="14" rx="3" /><path d="M8 21h8m-4-3v3M6 12h3l2-4 3 7 2-3h2" /></>}</svg>;
}
function Meter({ value }: { value: number | null }) {
  const available = value !== null && Number.isFinite(value);
  return <div className={`host-resource-meter${available ? "" : " is-unavailable"}`} aria-hidden="true"><span style={{ width: available ? `${Math.max(0, Math.min(100, value))}%` : "0%" }} /></div>;
}

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
  const cpuUsage = sample?.cpu.status === "ready" ? sample.cpu.utilization_percent : null;
  const memoryUsage = sample?.memory.status === "ready" ? ratio(sample.memory.used_bytes, sample.memory.total_bytes) : null;
  return <>
    <div className="host-resource-refresh"><div><span className={`host-resource-status${failed ? " is-failed" : ""}`} role="status"><i aria-hidden="true" />{failed ? "资源读取失败" : loading ? "读取采样…" : "采样已更新"}</span><p className="host-resource-scope">整机口径 · 包含这台机器上的全部工作</p></div><button type="button" disabled={loading} onClick={() => refresh.current()}>刷新资源</button></div>
    {failed ? <p className="host-resource-warning" role="status">{sample ? "保留上次成功采样，当前状态未知。" : "尚未取得采样，当前状态未知。"}</p> : null}
    {sample ? <>
      <div className="host-resource-metrics">
        <article data-testid="host-cpu"><h3>CPU <span>处理器</span></h3><b>{percent(cpuUsage)}</b><Meter value={cpuUsage} /><small>整机利用率</small></article>
        <article data-testid="host-memory"><h3>内存 <span>RAM</span></h3><b>{percent(memoryUsage)}</b><Meter value={memoryUsage} /><small>{sample.memory.status === "ready" && sample.memory.used_bytes !== null && sample.memory.total_bytes !== null ? `${gib(sample.memory.used_bytes)} / ${gib(sample.memory.total_bytes)} GiB · 已用 / 总量` : "内存采样缺测"}</small></article>
      </div>
      <div className="host-gpu-heading"><h3>GPU{sample.gpu.status === "ready" ? <span>{sample.gpu.devices.length} 块</span> : null}</h3><small>逐卡显示</small></div>
      {sample.gpu.status === "no_devices" ? <p className="host-resource-empty">未检测到 GPU 设备</p> : sample.gpu.status === "unavailable" ? <p className="host-resource-warning">GPU 采集不可用 · 缺测</p> : <ul className="host-gpu-list">{sample.gpu.devices.map((device, index) => <li key={device.uuid} data-gpu-uuid={device.uuid}>
        <div className="host-gpu-identity"><span>GPU {index + 1}</span><b>{device.name}</b></div>
        <dl><div className="host-gpu-utilization"><dt>利用率</dt><dd>{percent(device.utilization_percent)}</dd></div></dl>
        <Meter value={device.utilization_percent} />
        <dl><div className="host-gpu-memory"><dt>显存 <span>已用 / 总量</span></dt><dd>{device.memory_used_mib === null || device.memory_total_mib === null ? "缺测" : `${device.memory_used_mib} / ${device.memory_total_mib} MiB`}</dd></div></dl>
        <Meter value={ratio(device.memory_used_mib, device.memory_total_mib)} />
      </li>)}</ul>}
      <details className="host-resource-details"><summary>采样详情</summary><div>
        <p className="host-resource-host">{sample.host.hostname}{failed ? " · 上次采样" : ""}</p>
        <p className="host-resource-time">采样时间 <time dateTime={new Date(sample.observed_at * 1000).toISOString()}>{new Date(sample.observed_at * 1000).toLocaleString("zh-CN", { hour12: false })}</time><br />CPU 采样区间 {time(sample.cpu.sampled_from)} 至 {time(sample.cpu.sampled_to)} · {Math.round((sample.cpu.sampled_to - sample.cpu.sampled_from) * 1000)} ms<br />内存采样时间 {time(sample.memory.observed_at)} · GPU 采样时间 {time(sample.gpu.observed_at)}</p>
        <dl><div><dt>CPU 来源</dt><dd>{sample.cpu.source}{sample.cpu.reason_code ? ` · ${sample.cpu.reason_code}` : ""}</dd></div><div><dt>内存来源</dt><dd>{sample.memory.source}{sample.memory.reason_code ? ` · ${sample.memory.reason_code}` : ""}</dd></div><div><dt>GPU 来源</dt><dd>{sample.gpu.source}{sample.gpu.reason_code ? ` · ${sample.gpu.reason_code}` : ""}</dd></div>{sample.gpu.devices.map((device, index) => <div key={device.uuid}><dt>GPU {index + 1} 标识</dt><dd>{device.uuid}{device.reason_code ? ` · ${device.reason_code}` : ""}</dd></div>)}</dl>
      </div></details>
    </> : !failed ? <p className="host-resource-empty">尚未取得采样</p> : null}
  </>;
}

type PanelPosition = { left: number; top: number };
function constrainedPosition(position: PanelPosition, element: HTMLElement): PanelPosition {
  const margin = 12;
  return {
    left: Math.max(margin, Math.min(position.left, document.documentElement.clientWidth - element.offsetWidth - margin)),
    top: Math.max(margin, Math.min(position.top, window.innerHeight - element.offsetHeight - margin)),
  };
}
export function HostResources({ disabled = false }: { disabled?: boolean }) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<PanelPosition | null>(null);
  const [dragging, setDragging] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const drag = useRef<{ pointerId: number; x: number; y: number; left: number; top: number } | null>(null);
  const close = () => { setOpen(false); setDragging(false); drag.current = null; trigger.current?.focus(); };
  useLayoutEffect(() => {
    if (!open || disabled || !panel.current) return;
    const element = panel.current;
    const adjust = () => setPosition(previous => {
      const next = constrainedPosition(previous ?? { left: document.documentElement.clientWidth - element.offsetWidth - 24, top: 88 }, element);
      return previous?.left === next.left && previous?.top === next.top ? previous : next;
    });
    adjust();
    const observer = new ResizeObserver(adjust);
    observer.observe(element);
    window.addEventListener("resize", adjust);
    return () => { observer.disconnect(); window.removeEventListener("resize", adjust); };
  }, [open, disabled]);
  useEffect(() => {
    if (disabled) { setOpen(false); setDragging(false); drag.current = null; return; }
    if (!open) return;
    panel.current?.focus();
    const escape = (event: globalThis.KeyboardEvent) => {
      if (document.querySelector("dialog[open]")) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setOpen(false); setDragging(false); drag.current = null; trigger.current?.focus(); }
    };
    document.addEventListener("keydown", escape, true);
    return () => document.removeEventListener("keydown", escape, true);
  }, [open, disabled]);
  const startDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || !panel.current) return;
    const bounds = panel.current.getBoundingClientRect();
    drag.current = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, left: bounds.left, top: bounds.top };
    event.currentTarget.setPointerCapture(event.pointerId);
    event.currentTarget.focus();
    setDragging(true);
    event.preventDefault();
  };
  const moveDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag.current || drag.current.pointerId !== event.pointerId || !panel.current) return;
    setPosition(constrainedPosition({ left: drag.current.left + event.clientX - drag.current.x, top: drag.current.top + event.clientY - drag.current.y }, panel.current));
  };
  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointerId !== event.pointerId) return;
    drag.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const moveWithKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!panel.current || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home"].includes(event.key)) return;
    event.preventDefault();
    const bounds = panel.current.getBoundingClientRect();
    const step = event.shiftKey ? 40 : 16;
    const next = event.key === "Home" ? { left: document.documentElement.clientWidth - bounds.width - 24, top: 88 } : {
      left: bounds.left + (event.key === "ArrowRight" ? step : event.key === "ArrowLeft" ? -step : 0),
      top: bounds.top + (event.key === "ArrowDown" ? step : event.key === "ArrowUp" ? -step : 0),
    };
    setPosition(constrainedPosition(next, panel.current));
  };
  return <div className="host-resources">
    <button ref={trigger} type="button" disabled={disabled} className="host-resource-trigger" aria-expanded={open} aria-controls="host-resource-panel" onClick={() => open ? close() : setOpen(true)}><ResourceIcon /><span>资源监控</span></button>
    {open && !disabled ? createPortal(<section id="host-resource-panel" ref={panel} tabIndex={-1} className={`host-resource-panel${dragging ? " is-dragging" : ""}`} aria-label="资源监控" style={position ? { left: position.left, top: position.top } : undefined}>
      <header className="host-resource-panel-header"><div className="host-resource-drag-handle" role="button" tabIndex={0} aria-label="移动资源监控面板" aria-describedby="host-resource-move-help" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag} onLostPointerCapture={endDrag} onKeyDown={moveWithKeyboard}><ResourceIcon /><h2>资源监控</h2><span aria-hidden="true">拖动移动</span></div><button className="host-resource-close" type="button" aria-label="关闭资源面板" onClick={close}><ResourceIcon close /></button></header>
      <p id="host-resource-move-help" className="host-resource-sr-only">拖动标题栏或使用方向键移动面板，按住 Shift 加快移动，Home 返回初始位置，Esc 关闭。</p>
      <div className="host-resource-body"><ResourceReadout /></div>
    </section>, document.body) : null}
  </div>;
}
