import { useEffect, useRef, useState } from "react";
import { fetchResearchLibrary, type ResearchLibraryItem } from "./api";

/** Question identities are immutable; fetch missing labels, including retired questions, once per Quest. */
export function useTimelineQuestions(questRef: string | null, questionRefs: string[]) {
  const key = JSON.stringify([...new Set(questionRefs)].sort());
  const cache = useRef<{ questRef: string | null; items: Record<string, ResearchLibraryItem> }>({ questRef: null, items: {} });
  const [result, setResult] = useState({ questRef, items: {} as Record<string, ResearchLibraryItem>, error: false });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!questRef) return;
    if (cache.current.questRef !== questRef) cache.current = { questRef, items: {} };
    const wanted = new Set((JSON.parse(key) as string[]).filter(ref => !cache.current.items[ref]));
    if (!wanted.size) return;
    const controller = new AbortController();
    setResult({ questRef, items: { ...cache.current.items }, error: false });
    void (async () => {
      try {
        let offset = 0;
        while (wanted.size) {
          const page = await fetchResearchLibrary("questions", questRef, "", offset, controller.signal, { limit: "100" });
          if (controller.signal.aborted) return;
          if (!Array.isArray(page.items)) throw new Error("timeline_questions_invalid");
          for (const item of page.items) {
            const ref = typeof item.question_ref === "string" ? item.question_ref : item.ref;
            if (ref && wanted.has(ref) && (!item.quest_ref || item.quest_ref === questRef)) {
              cache.current.items[ref] = item;
              wanted.delete(ref);
            }
          }
          setResult({ questRef, items: { ...cache.current.items }, error: false });
          if (page.next_offset == null) {
            if (wanted.size) setResult({ questRef, items: { ...cache.current.items }, error: true });
            break;
          }
          if (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset) throw new Error("timeline_questions_cursor_invalid");
          offset = page.next_offset;
        }
      } catch {
        if (!controller.signal.aborted) setResult({ questRef, items: { ...cache.current.items }, error: true });
      }
    })();
    return () => controller.abort();
  }, [questRef, key, attempt]);
  return { items: result.questRef === questRef ? result.items : {}, error: result.questRef === questRef && result.error,
    retry: () => setAttempt(value => value + 1) };
}
