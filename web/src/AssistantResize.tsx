import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";

const storageKey = "meta-research.assistant-width";
const minimum = 320;
const maximum = 720;
const clamp = (value: number, upper = maximum) => Math.min(upper, Math.max(minimum, value));
function readPreference() {
  try {
    const value = Number(localStorage.getItem(storageKey));
    return Number.isFinite(value) && value >= minimum && value <= maximum ? value : 420;
  } catch { return 420; }
}

export function useAssistantWidth() {
  const shellRef = useRef<HTMLDivElement>(null);
  const [preferred, setPreferred] = useState(readPreference);
  const [available, setAvailable] = useState(maximum);
  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const measure = () => {
      const style = getComputedStyle(shell);
      const rail = shell.querySelector<HTMLElement>(".lumen-rail");
      const occupied = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
        + (rail?.getBoundingClientRect().width ?? 76) + parseFloat(style.columnGap) * 3 + 8;
      setAvailable(clamp(shell.clientWidth - occupied - 680));
    };
    const observer = new ResizeObserver(measure);
    observer.observe(shell);
    measure();
    return () => observer.disconnect();
  }, []);
  const resize = useCallback((value: number) => setPreferred(clamp(value)), []);
  const commit = useCallback((value: number) => {
    const next = clamp(value);
    setPreferred(next);
    try { localStorage.setItem(storageKey, String(next)); } catch { }
  }, []);
  return { shellRef, width: Math.min(preferred, available), maximum: available, resize, commit,
    style: { "--assistant-width": `${Math.min(preferred, available)}px` } as CSSProperties };
}

export function AssistantResize({ width, maximum: upper, onResize, onCommit }: {
  width: number; maximum: number; onResize: (width: number) => void; onCommit: (width: number) => void;
}) {
  const drag = useRef<{ pointer: number; x: number; width: number; value: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current.value = clamp(drag.current.width + drag.current.x - event.clientX, upper);
    onResize(drag.current.value);
  };
  const finish = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    onCommit(drag.current.value);
    drag.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const keyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 40 : 16;
    const next = event.key === "ArrowLeft" ? width + step : event.key === "ArrowRight" ? width - step
      : event.key === "Home" ? minimum : event.key === "End" ? upper : null;
    if (next === null) return;
    event.preventDefault();
    onCommit(clamp(next, upper));
  };
  return <div className="assistant-resize" role="separator" aria-label="调整研究助手宽度" aria-orientation="vertical"
    aria-controls="research-companion" aria-valuemin={minimum} aria-valuemax={upper} aria-valuenow={Math.round(width)}
    tabIndex={0} data-dragging={dragging} onKeyDown={keyboard}
    onPointerDown={event => {
      if (event.button !== 0) return;
      event.preventDefault();
      drag.current = { pointer: event.pointerId, x: event.clientX, width, value: width };
      event.currentTarget.setPointerCapture(event.pointerId);
      setDragging(true);
    }} onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
    onLostPointerCapture={() => { drag.current = null; setDragging(false); }} />;
}
