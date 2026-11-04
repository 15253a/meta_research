import { useEffect, useRef, useState } from "react";
import { fetchQuestGoalHistory, type PublicSnapshot, type QuestGoalHistory } from "./api";
import "./quest-goal-evolution.css";

export type QuestGoalDisplay = Pick<PublicSnapshot["research_space"]["current_quest"],
  "status" | "quest_ref" | "goal_revision_ref" | "goal" | "sequence" | "origin" | "judgment" | "history">;

type GoalRow = { ref: string; version: number | null; text: string; judgment?: string | null };

function historyRows(page: QuestGoalHistory): GoalRow[] {
  return page.items.map((item, index) => ({ ref: item.goal_revision_ref,
    // The initial accepted goal precedes evolution sequence 1. A draft revision
    // or research Cycle is never used to invent a formal goal version.
    version: Number.isInteger(item.sequence) && item.sequence! >= 0 ? item.sequence! + 1
      : page.offset === 0 && index === 0 ? 1 : null,
    text: item.goal.goal, judgment: item.judgment }));
}

function GoalRows({ rows, current }: { rows: GoalRow[]; current: string }) {
  return <ol className="quest-goal-lines">{rows.map(row => <li key={row.ref} data-goal-revision={row.ref} data-current={row.ref === current}>
    <b>{row.version === null ? "版本待确认" : `v${row.version}`}</b>
    <details className="quest-goal-copy">
      <summary aria-label={row.version === null ? "查看目标全文" : `查看 v${row.version} 目标全文`}>
        <span className="quest-goal-preview" title={row.text}>{row.text}</span><span className="quest-goal-chevron" aria-hidden="true">⌄</span>
      </summary>
      <div className="quest-goal-full">
        <p>{row.text}</p>
        {row.judgment ? <div><b>调整依据</b><p>{row.judgment}</p></div> : null}
      </div>
    </details>
    {row.ref === current ? <small>当前</small> : null}
  </li>)}</ol>;
}

/** Read-only accepted goal versions, scoped to the same data instance as the workspace. */
export function QuestGoalEvolution({ goal, stale = false, scopeKey }: {
  goal?: QuestGoalDisplay | null; stale?: boolean; scopeKey?: string;
}) {
  const scope = JSON.stringify([scopeKey, goal?.quest_ref, goal?.goal_revision_ref]);
  const latestScope = useRef(scope);
  latestScope.current = scope;
  const [state, setState] = useState<{ scope: string; pages: QuestGoalHistory[]; loading: boolean; failed: boolean }>(
    () => ({ scope, pages: [], loading: false, failed: false }));
  const currentState = state.scope === scope ? state : { scope, pages: [], loading: false, failed: false };
  const requestToken = useRef(0);
  useEffect(() => () => { requestToken.current += 1; }, [scope]);
  if (goal?.status !== "ready" || !goal.quest_ref || !goal.goal_revision_ref || !goal.goal) {
    return <div className="quest-goal-evolution" aria-label="研究目标版本"><header><b>研究目标</b><span>暂未读取到正式目标</span></header></div>;
  }
  const history = [goal.history, ...currentState.pages].filter((page): page is QuestGoalHistory => Boolean(page));
  const byRef = new Map(history.flatMap(historyRows).map(row => [row.ref, row]));
  const current: GoalRow = { ref: goal.goal_revision_ref, text: goal.goal, judgment: goal.judgment,
    version: Number.isInteger(goal.sequence) && goal.sequence! >= 0 ? goal.sequence! + 1
      : byRef.get(goal.goal_revision_ref)?.version ?? (goal.origin === "initialization" ? 1 : null) };
  byRef.set(current.ref, current);
  const rows = [...byRef.values()].sort((left, right) => (left.version ?? Infinity) - (right.version ?? Infinity));
  const previous = rows.filter(row => row.ref !== current.ref);
  const nextOffset = currentState.pages.length ? currentState.pages.at(-1)!.next_offset : goal.history ? goal.history.next_offset : 0;
  const folded = rows.length > 3 || nextOffset !== null;
  const loadMore = async () => {
    if (currentState.loading || nextOffset === null) return;
    const token = ++requestToken.current;
    setState({ ...currentState, loading: true, failed: false });
    try {
      const page = await fetchQuestGoalHistory(goal.quest_ref!, nextOffset);
      if (page.offset !== nextOffset || !Array.isArray(page.items)
        || page.next_offset !== null && (!Number.isInteger(page.next_offset) || page.next_offset <= nextOffset)) {
        throw new Error("goal_history_page_invalid");
      }
      if (token === requestToken.current && latestScope.current === scope) {
        setState({ scope, pages: [...currentState.pages, page], loading: false, failed: false });
      }
    } catch {
      if (token === requestToken.current && latestScope.current === scope) setState({ ...currentState, loading: false, failed: true });
    }
  };
  return <section className="quest-goal-evolution" aria-label="研究目标版本" data-quest-ref={goal.quest_ref}>
    <header><b>研究目标</b>{stale ? <span role="status">上次读取，更新暂不可用</span> : null}</header>
    <GoalRows key={`shown:${scope}`} rows={folded ? [current] : rows} current={current.ref} />
    {folded ? <details><summary>查看目标版本</summary>
      <GoalRows key={`history:${scope}`} rows={previous} current={current.ref} />
      {currentState.failed ? <p role="status">目标历史暂不可用，已读取的版本仍保留。</p> : null}
      {nextOffset !== null ? <button type="button" disabled={currentState.loading} onClick={() => void loadMore()}>{currentState.loading ? "正在读取…" : currentState.failed ? "重新读取目标历史" : "读取更多目标版本"}</button> : null}
    </details> : null}
  </section>;
}
