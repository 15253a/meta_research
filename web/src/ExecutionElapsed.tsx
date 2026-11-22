import { useEffect, useState } from "react";

export type ExecutionClockSample = {
  sourceUpdatedAt: number;
  observedAt: number;
  receivedAt: number;
};

function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  if (s < 60) return `${s} 秒`;
  const hours = Math.floor(s / 3_600);
  const minutes = Math.floor((s % 3_600) / 60);
  const remainder = s % 60;
  const pad = (value: number) => value < 10 ? `0${value}` : `${value}`;
  if (hours > 0) return `${hours} 小时 ${pad(minutes)} 分 ${pad(remainder)} 秒`;
  return `${minutes} 分 ${pad(remainder)} 秒`;
}

/** Server timestamps establish the age; the local monotonic clock only ticks it. */
export function ExecutionElapsed({ sample, ended = false, state = "live", startedAt = null, endedAt = null }: {
  sample: ExecutionClockSample | null;
  ended?: boolean;
  state?: "live" | "executing" | "waiting" | "paused" | "failed" | "pending" | "completed" | "unknown";
  startedAt?: number | null;
  endedAt?: number | null;
}) {
  const stopped = ended || !["live", "executing"].includes(state);
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (stopped) return;
    const tick = () => setNow(performance.now());
    const timer = window.setInterval(tick, 1_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [stopped]);
  const validStart = typeof startedAt === "number" && Number.isFinite(startedAt) && startedAt > 0 ? startedAt : null;
  const label = ended || state === "completed" ? "输出已结束" : state === "paused" ? "已暂停"
    : state === "failed" ? "执行受阻" : state === "unknown" ? "状态待确认" : state === "pending" ? "等待启动" : "等待继续";
  if (stopped) {
    const validEnd = validStart !== null
      ? typeof endedAt === "number" && Number.isFinite(endedAt) && endedAt > validStart ? endedAt
      : sample && Number.isFinite(sample.sourceUpdatedAt) && sample.sourceUpdatedAt > validStart ? sample.sourceUpdatedAt
      : null
      : null;
    const runDuration = validStart !== null && validEnd !== null ? validEnd - validStart : null;
    const durationLabel = runDuration === null ? null : runDuration < 1 ? "不足 1 秒" : formatDuration(runDuration);
    return <span className="research-execution-clock" aria-live="off" data-state={ended ? "completed" : state}>
      <span aria-hidden="true">◷</span> {label}
      {durationLabel ? <> · <span className="research-execution-duration">上次运行 {durationLabel}</span></> : null}
      {" · "}{sample
        ? <>最后记录于 <time dateTime={new Date(sample.sourceUpdatedAt * 1_000).toISOString()}>{new Date(sample.sourceUpdatedAt * 1_000).toLocaleString()}</time></>
        : "最后记录时间不可用"}
    </span>;
  }
  const seconds = sample ? Math.floor(Math.max(0,
    sample.observedAt - sample.sourceUpdatedAt + Math.max(0, now - sample.receivedAt) / 1_000,
  )) : null;
  const elapsedRun = sample && validStart !== null && sample.observedAt >= validStart
    ? Math.floor(Math.max(0, sample.observedAt - validStart + Math.max(0, now - sample.receivedAt) / 1_000))
    : null;
  return <span className="research-execution-clock" aria-live="off" data-state={state}
    title={sample
      ? `最近执行记录：${new Date(sample.sourceUpdatedAt * 1_000).toLocaleString()}。仅表示输出间隔，不代表进程卡住；状态心跳和页面刷新不会归零。`
      : "取得真实执行记录的写入时间后开始计时。"}>
    <span aria-hidden="true">◷</span> {state === "executing" ? "执行中 · " : ""}
    {elapsedRun !== null ? <><span className="research-execution-duration">已运行 {formatDuration(elapsedRun)}</span> · </> : null}
    {seconds === null ? "尚无执行时间记录" : <>距上次执行记录 <b>{seconds}</b> 秒</>}
  </span>;
}
