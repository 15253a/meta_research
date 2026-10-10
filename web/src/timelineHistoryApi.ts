import { useCallback, useEffect, useState } from "react";
import { fetchResearchLibrary, type ResearchLibraryItem } from "./api";
import type { ResearchOverviewData } from "./ResearchOverview";

export type TimelineQuest = { quest_ref: string; goal: string };

async function read<T>(path: string, signal: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = window.setTimeout(abort, 15_000);
  try {
    const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal });
    if (!response.ok) throw new Error(`timeline_history_unavailable:${response.status}`);
    return await response.json() as T;
  } finally {
    window.clearTimeout(timer); signal.removeEventListener("abort", abort);
  }
}

/** Enumerate existing public pages; incomplete reads remain visibly incomplete. */
export function useTimelineHistory(questRef: string | null, liveQuestRef: string | null, questionVersion: string) {
  const [quests, setQuests] = useState<TimelineQuest[]>([]);
  const [questError, setQuestError] = useState(false);
  const [questLoading, setQuestLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [questions, setQuestions] = useState<{ questRef: string | null; items: ResearchLibraryItem[]; error: boolean; loading: boolean }>({ questRef: null, items: [], error: false, loading: true });
  const [overview, setOverview] = useState<{ questRef: string | null; data: ResearchOverviewData | null; error: boolean; loading: boolean }>({ questRef: null, data: null, error: false, loading: false });
  const retry = useCallback(() => setAttempt(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setQuestLoading(true); setQuestError(false);
    void (async () => {
      try {
        let offset = 0;
        const items: TimelineQuest[] = [];
        while (true) {
          const page = await read<{ schema_ref: string; items: TimelineQuest[]; next_offset: number | null }>(`/api/v1/research-timeline/quests?${new URLSearchParams({ offset: String(offset), limit: "100" })}`, controller.signal);
          if (page.schema_ref !== "meta-research/timeline-quests/v1" || !Array.isArray(page.items)
            || page.items.some(item => typeof item.quest_ref !== "string" || typeof item.goal !== "string")) throw new Error("timeline_quests_invalid");
          items.push(...page.items);
          if (!controller.signal.aborted) setQuests([...new Map(items.map(item => [item.quest_ref, item])).values()]);
          if (page.next_offset == null) break;
          if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset) throw new Error("timeline_quests_cursor_invalid");
          offset = page.next_offset;
        }
      } catch { if (!controller.signal.aborted) setQuestError(true); }
      finally { if (!controller.signal.aborted) setQuestLoading(false); }
    })();
    return () => controller.abort();
  }, [liveQuestRef, attempt]);

  useEffect(() => {
    if (!questRef) return;
    const controller = new AbortController();
    setQuestions(previous => ({ questRef, items: previous.questRef === questRef ? previous.items : [], error: false, loading: true }));
    void (async () => {
      const items: ResearchLibraryItem[] = [];
      try {
        let offset = 0;
        while (true) {
          const page = await fetchResearchLibrary("questions", questRef, "", offset, controller.signal, { limit: "100" });
          if (!Array.isArray(page.items) || page.items.some(item => !item.question_ref || (item.quest_ref && item.quest_ref !== questRef))) throw new Error("timeline_questions_invalid");
          items.push(...page.items);
          if (!controller.signal.aborted) setQuestions({ questRef, items: [...items], error: false, loading: page.next_offset != null });
          if (page.next_offset == null) break;
          if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset) throw new Error("timeline_questions_cursor_invalid");
          offset = page.next_offset;
        }
      } catch { if (!controller.signal.aborted) setQuestions(previous => ({ ...previous, error: true, loading: false })); }
    })();
    return () => controller.abort();
  }, [questRef, questionVersion, attempt]);

  useEffect(() => {
    if (!questRef || questRef === liveQuestRef) return;
    const controller = new AbortController();
    setOverview(previous => ({ questRef, data: previous.questRef === questRef ? previous.data : null, error: false, loading: true }));
    void (async () => {
      try {
        const data = await read<ResearchOverviewData>(`/api/v1/research-overview?${new URLSearchParams({ quest_ref: questRef })}`, controller.signal);
        if (data.schema_ref !== "meta-research/research-overview/v1" || data.quest_ref !== questRef || !Array.isArray(data.cycles) || !data.findings) throw new Error("timeline_overview_scope_invalid");
        if (!controller.signal.aborted) setOverview({ questRef, data, error: false, loading: false });
      } catch { if (!controller.signal.aborted) setOverview(previous => ({ ...previous, error: true, loading: false })); }
    })();
    return () => controller.abort();
  }, [questRef, liveQuestRef, attempt]);

  return { quests, questError, questLoading, retry,
    questions: questions.questRef === questRef ? questions.items : [],
    questionsError: questions.questRef === questRef && questions.error,
    questionsLoading: questions.questRef !== questRef || questions.loading,
    overview: overview.questRef === questRef ? overview.data : null,
    overviewError: overview.questRef === questRef && overview.error,
    overviewLoading: overview.questRef !== questRef || overview.loading };
}
