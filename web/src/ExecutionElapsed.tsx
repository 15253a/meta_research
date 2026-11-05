import { useEffect, useState } from "react";

export type ExecutionClockSample = {
  sourceUpdatedAt: number;
  observedAt: number;
  receivedAt: number;
};

/** Server timestamps establish the age; the local monotonic clock only ticks it. */
export function ExecutionElapsed({ sample, ended = false, state = "live" }: {
  sample: ExecutionClockSample | null;
  ended?: boolean;
  state?: "live" | "executing" | "waiting" | "paused" | "failed" | "pending" | "completed" | "unknown";
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
  const label = ended || state === "completed" ? "输出已结束" : state === "paused" ? "已暂停"
    : state === "failed" ? "执行受阻" : state === "unknown" ? "状态待确认" : state === "pending" ? "等待启动" : "等待继续";
  if (stopped) return <span className="research-execution-clock" aria-live="off" data-state={ended ? "completed" : state}>
    <span aria-hidden="true">◷</span> {label} · {sample
      ? <>最后记录于 <time dateTime={new Date(sample.sourceUpdatedAt * 1_000).toISOString()}>{new Date(sample.sourceUpdatedAt * 1_000).toLocaleString()}</time></>
      : "最后记录时间不可用"}
  </span>;
  const seconds = sample ? Math.floor(Math.max(0,
    sample.observedAt - sample.sourceUpdatedAt + Math.max(0, now - sample.receivedAt) / 1_000,
  )) : null;
  return <span className="research-execution-clock" aria-live="off"
    title={sample
      ? `最近执行记录：${new Date(sample.sourceUpdatedAt * 1_000).toLocaleString()}。仅表示输出间隔，不代表进程卡住；状态心跳和页面刷新不会归零。`
      : "取得真实执行记录的写入时间后开始计时。"}>
    <span aria-hidden="true">◷</span> {state === "executing" ? "执行中 · " : ""}{seconds === null ? "尚无执行时间记录" : <>距上次执行记录 <b>{seconds}</b> 秒</>}
  </span>;
}
