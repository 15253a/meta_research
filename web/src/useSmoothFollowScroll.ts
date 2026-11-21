import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";

type Options = {
  logRef: RefObject<HTMLDivElement | null>;
  streamKey: string;
  itemCount: number;
  lastKey: string | number | null;
  enabled?: boolean;
};

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function useSmoothFollowScroll({
  logRef,
  streamKey,
  itemCount,
  lastKey,
  enabled = true,
}: Options) {
  const pinnedRef = useRef(true);
  const anchoredKeyRef = useRef<string | null>(null);
  const lastSeenRef = useRef<{ streamKey: string; lastKey: string | number | null }>({
    streamKey,
    lastKey,
  });
  const rafRef = useRef<number | null>(null);
  const animatingRef = useRef(false);
  const expectedTopRef = useRef<number | null>(null);
  const [hasUnseen, setHasUnseen] = useState(false);

  const cancelAnimation = useCallback(() => {
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    animatingRef.current = false;
    expectedTopRef.current = null;
  }, []);

  const snapToBottom = useCallback((log: HTMLDivElement) => {
    cancelAnimation();
    const before = log.scrollTop;
    const target = Math.max(0, log.scrollHeight - log.clientHeight);
    log.scrollTop = target;
    expectedTopRef.current = Math.abs(log.scrollTop - before) > 0.5 ? log.scrollTop : null;
    pinnedRef.current = true;
    setHasUnseen(false);
  }, [cancelAnimation]);

  const smoothScrollToBottom = useCallback((log: HTMLDivElement) => {
    const target = Math.max(0, log.scrollHeight - log.clientHeight);
    const start = log.scrollTop;
    const distance = target - start;
    if (distance <= 1 || distance > 280 || prefersReducedMotion()) {
      snapToBottom(log);
      return;
    }
    cancelAnimation();
    animatingRef.current = true;
    pinnedRef.current = true;
    setHasUnseen(false);
    const duration = 190;
    const startedAt = performance.now();
    const step = (now: number) => {
      const node = logRef.current;
      if (!node || !animatingRef.current) return;
      const currentTarget = Math.max(0, node.scrollHeight - node.clientHeight);
      const t = Math.min(1, (now - startedAt) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      node.scrollTop = start + (currentTarget - start) * eased;
      expectedTopRef.current = node.scrollTop;
      if (t < 1) {
        rafRef.current = requestAnimationFrame(step);
      } else {
        node.scrollTop = currentTarget;
        expectedTopRef.current = node.scrollTop;
        animatingRef.current = false;
        rafRef.current = null;
      }
    };
    rafRef.current = requestAnimationFrame(step);
  }, [cancelAnimation, logRef, snapToBottom]);

  useEffect(() => cancelAnimation, [cancelAnimation]);

  useLayoutEffect(() => {
    const log = logRef.current;
    const prev = lastSeenRef.current;
    const streamChanged = prev.streamKey !== streamKey;
    const itemAppended = !streamChanged && lastKey !== null && lastKey !== prev.lastKey;
    lastSeenRef.current = { streamKey, lastKey };

    if (streamChanged) {
      cancelAnimation();
      pinnedRef.current = true;
      anchoredKeyRef.current = null;
      setHasUnseen(false);
    }

    if (!log || !enabled || itemCount === 0 || lastKey === null) return;
    if (log.clientHeight === 0) return;

    if (anchoredKeyRef.current !== streamKey) {
      anchoredKeyRef.current = streamKey;
      snapToBottom(log);
      return;
    }

    if (pinnedRef.current) {
      if (itemAppended) smoothScrollToBottom(log);
      else snapToBottom(log);
    } else if (itemAppended) {
      setHasUnseen(true);
    }
  }, [cancelAnimation, enabled, itemCount, lastKey, logRef, smoothScrollToBottom, snapToBottom, streamKey]);

  const handleScroll = useCallback(() => {
    const log = logRef.current;
    if (!log) return;
    if (
      expectedTopRef.current !== null &&
      Math.abs(log.scrollTop - expectedTopRef.current) <= 2
    ) {
      if (!animatingRef.current) expectedTopRef.current = null;
      return;
    }
    cancelAnimation();
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    pinnedRef.current = atBottom;
    if (atBottom) setHasUnseen(false);
  }, [cancelAnimation, logRef]);

  const jumpToLatest = useCallback(() => {
    const log = logRef.current;
    if (!log) return;
    pinnedRef.current = true;
    setHasUnseen(false);
    smoothScrollToBottom(log);
  }, [logRef, smoothScrollToBottom]);

  return { handleScroll, hasUnseen, jumpToLatest };
}
