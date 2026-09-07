import { useEffect, useRef, useState } from "react";

type ReplyPreview = { path: string; text: string; status: string };

/** Transient text stays separate from the final, validated conversation history. */
export function useReplyStream(
  path: string | null,
  onSettled?: () => void,
): ReplyPreview | null {
  const [preview, setPreview] = useState<ReplyPreview | null>(null);
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  useEffect(() => {
    if (!path) return;
    const source = new EventSource(path);
    let active = true;
    source.onmessage = (event: MessageEvent<string>) => {
      if (!active) return;
      let value: unknown;
      try { value = JSON.parse(event.data); } catch { return; }
      if (!value || typeof value !== "object") return;
      const update = value as { text?: unknown; status?: unknown };
      if (typeof update.text !== "string" || typeof update.status !== "string") return;
      setPreview({ path, text: update.text, status: update.status });
      if (!["queued", "processing", "running"].includes(update.status)) {
        active = false;
        source.close();
        onSettledRef.current?.();
      }
    };
    // EventSource reconnects with the server's current cumulative text. The
    // existing durable projection/polling remains authoritative if it fails.
    return () => { active = false; source.close(); };
  }, [path]);

  return preview?.path === path ? preview : null;
}
