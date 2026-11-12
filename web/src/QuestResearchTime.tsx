import { useEffect, useState } from "react";
import { fetchQuestResearchTime, type QuestResearchTime as ResearchTime } from "./api";

export function durationLabel(seconds: number): string {
  return `${Number((seconds / 3600).toFixed(2))} 小时`;
}

export function QuestResearchTime({ questRef, refreshKey = "", onObserved }: {
  questRef: string | null; refreshKey?: string; onObserved?: (snapshot: ResearchTime) => void;
}) {
  const [snapshot, setSnapshot] = useState<ResearchTime | null>(null);
  const [error, setError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    if (!questRef || questRef.startsWith("initialization:")) return;
    let alive = true;
    let reading = false;
    const controller = new AbortController();
    setSnapshot(null);
    const read = async () => {
      if (reading) return;
      reading = true;
      try {
        const next = await fetchQuestResearchTime(questRef, controller.signal);
        if (next.quest_ref !== questRef || !Number.isFinite(next.observed_at)) throw new Error("research_time_invalid");
        if (alive) { setSnapshot(next); setError(false); onObserved?.(next); }
      } catch { if (alive) setError(true); }
      finally { reading = false; }
    };
    void read();
    const timer = window.setInterval(() => { void read(); }, 15_000);
    return () => { alive = false; controller.abort(); window.clearInterval(timer); };
  }, [questRef, refreshKey, refresh, onObserved]);
  if (!questRef || questRef.startsWith("initialization:")) return null;
  return <section className="quest-research-time" aria-label="研究时间">
    <h3>研究时间</h3>
    {snapshot ? <>
      <p><b>{snapshot.budget_kind === "unlimited" ? "未设建议总时长" : snapshot.accuracy === "unknown" ? "预算余量暂不能准确确定"
        : `预算余量 ${durationLabel(snapshot.remaining_seconds ?? 0)}`}</b>
        {snapshot.overrun_seconds != null && snapshot.overrun_seconds > 0 ? ` · 超出建议 ${durationLabel(snapshot.overrun_seconds)}` : ""}</p>
      <p>{snapshot.approved_seconds == null ? "规划预算：未设建议总时长" : `建议总时长 ${durationLabel(snapshot.approved_seconds)}`}；
        {snapshot.accuracy === "exact" ? `已用 ${durationLabel(snapshot.used_seconds ?? 0)}` : `已确认用量 ${durationLabel(snapshot.confirmed_used_seconds)}`}
      </p>
      <p>时间预算用于规划。超出或暂不能准确核定时，按研究价值调整安排，不自动停止或要求延期。</p>
      <p>{snapshot.status === "paused" ? "人工暂停中，暂停期间不计时。" : snapshot.status === "stopped" ? "服务已停止，停机期间不计时。"
        : snapshot.status === "not_started" ? "正式研究尚未开始。" : snapshot.status === "completed" ? "本项研究已结束计时。" : "等待回应期间继续计时；人工暂停和服务停机不计时。"}</p>
      {snapshot.accuracy === "unknown" && <p>有缺少计时依据的区间，已确认用量仍保留；需明确校正依据后才能给出准确余额。</p>}
      {snapshot.measurement_basis === "human_confirmation" && <p>当前余额包含人类明确确认的校正；原已确认用量和缺证记录保留在计时依据中。</p>}
      <details><summary>计时依据与观察时间</summary>
        <p>同一 Quest 共用一条时间轴，并行工作只计一次，开始下一轮不会重置。</p>
        <p>观察于 {new Date(snapshot.observed_at * 1000).toLocaleString()}，显示服务器快照。</p>
        <code>{snapshot.budget_revision}</code>
        {snapshot.unknown_intervals.length > 0 && <pre>{JSON.stringify(snapshot.unknown_intervals, null, 2)}</pre>}
        {!!snapshot.accounting_corrections?.length && <pre>{JSON.stringify(snapshot.accounting_corrections, null, 2)}</pre>}
      </details>
    </> : !error && <p>正在读取当前研究时间…</p>}
    {error && <p role="status">当前时间读取失败{snapshot ? "，以上为上次观测" : ""}。<button type="button" onClick={() => setRefresh(value => value + 1)}>重新读取时间</button></p>}
  </section>;
}
