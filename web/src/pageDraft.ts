import { useCallback, useEffect, useState, type RefObject, type SetStateAction } from "react";

// Unsaved edits live only in this page. Saved and sealed submissions use their
// existing server / IndexedDB contracts instead.
const drafts = new Map<string, unknown>();

export function usePageDraft<T>(scope: string, initial: T) {
  const read = () => drafts.has(scope) ? drafts.get(scope) as T : initial;
  const [entry, setEntry] = useState(() => ({ scope, value: read() }));
  const value = entry.scope === scope ? entry.value : read();
  const setValue = useCallback((next: SetStateAction<T>) => {
    const previous = drafts.has(scope) ? drafts.get(scope) as T : initial;
    const value = typeof next === "function" ? (next as (previous: T) => T)(previous) : next;
    drafts.set(scope, value);
    setEntry({ scope, value });
  }, [scope]);
  return [value, setValue] as const;
}

type ReadingPosition = { scroll: Record<string, number>; expanded: Record<string, boolean> };
const readingPositions = new Map<string, ReadingPosition>();

export function usePageReadingPosition(scope: string, ref: RefObject<HTMLElement | null>) {
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    const saved = readingPositions.get(scope) ?? { scroll: {}, expanded: {} };
    let interacted = false;
    const scrollables = () => [root, ...root.querySelectorAll<HTMLElement>(".hc-request-core, .hc-draft-transcript")];
    const key = (element: HTMLElement) => element === root ? "workspace" : element.className;
    const restore = () => {
      if (interacted) return;
      scrollables().forEach(element => { element.scrollTop = saved.scroll[key(element)] ?? 0; });
      root.querySelectorAll("details").forEach(element => {
        const title = element.querySelector("summary")?.textContent ?? "";
        if (title in saved.expanded) element.open = saved.expanded[title];
      });
    };
    const remember = () => {
      // Closing a native dialog collapses its scroll boxes before React's
      // passive cleanup. Keep the last visible reading position in that case.
      if (!interacted || !root.isConnected || root.closest("dialog")?.open === false) return;
      readingPositions.set(scope, {
        scroll: Object.fromEntries(scrollables().map(element => [key(element), element.scrollTop])),
        expanded: Object.fromEntries(Array.from(root.querySelectorAll("details")).map(element => [element.querySelector("summary")?.textContent ?? "", element.open])),
      });
    };
    const interact = () => { interacted = true; };
    const observer = new ResizeObserver(restore);
    observer.observe(root);
    root.querySelectorAll(".hc-request-core, .hc-draft-transcript").forEach(element => observer.observe(element));
    root.addEventListener("scroll", remember, true);
    root.addEventListener("toggle", remember, true);
    ["wheel", "pointerdown", "keydown"].forEach(event => root.addEventListener(event, interact, true));
    restore();
    return () => {
      remember(); observer.disconnect();
      root.removeEventListener("scroll", remember, true); root.removeEventListener("toggle", remember, true);
      ["wheel", "pointerdown", "keydown"].forEach(event => root.removeEventListener(event, interact, true));
    };
  }, [scope]);
}
