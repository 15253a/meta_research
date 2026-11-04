import { useCallback, useEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import "./observation-resize.css";

const storageKey = "meta-research.observation-split";
const defaultRatio = 1 / 3;
const handleWidth = 8;
const clamp = (value: number, minimum: number, maximum: number) => Math.min(maximum, Math.max(minimum, value));

function readPreference() {
  try {
    const value = Number(localStorage.getItem(storageKey));
    return Number.isFinite(value) && value > 0 && value < 1 ? value : defaultRatio;
  } catch { return defaultRatio; }
}

export function useObservationSplit() {
  const gridRef = useRef<HTMLDivElement>(null);
  const [preferred, setPreferred] = useState(readPreference);
  const [available, setAvailable] = useState(0);
  useEffect(() => {
    const grid = gridRef.current;
    if (!grid) return;
    const measure = () => {
      if (grid.clientWidth > handleWidth) setAvailable(grid.clientWidth - handleWidth);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(grid);
    measure();
    return () => observer.disconnect();
  }, []);
  // Preserve both readable panes in narrow desktop layouts, without replacing
  // the user's preferred proportion when the viewport temporarily shrinks.
  const minimum = Math.min(220, available * 0.4);
  const maximum = available - Math.min(280, available * 0.4);
  const width = clamp(available * preferred, minimum, maximum);
  const save = useCallback((ratio: number) => {
    setPreferred(ratio);
    try { localStorage.setItem(storageKey, String(ratio)); } catch { }
  }, []);
  const resize = (value: number) => {
    if (available) setPreferred(clamp(value, minimum, maximum) / available);
  };
  const commit = (value: number) => {
    if (available) save(clamp(value, minimum, maximum) / available);
  };
  return { gridRef, width, minimum, maximum, resize, commit, reset: () => save(defaultRatio),
    style: available ? { "--timeline-width": `${width}px` } as CSSProperties : undefined };
}

export function ObservationResize({ split }: { split: ReturnType<typeof useObservationSplit> }) {
  const drag = useRef<{ pointer: number; x: number; width: number; value: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const move = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current.value = clamp(drag.current.width + event.clientX - drag.current.x, split.minimum, split.maximum);
    split.resize(drag.current.value);
  };
  const finish = (event: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== event.pointerId) return;
    split.commit(drag.current.value);
    drag.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  };
  const keyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 40 : 16;
    const next = event.key === "ArrowLeft" ? split.width - step : event.key === "ArrowRight" ? split.width + step
      : event.key === "Home" ? split.minimum : event.key === "End" ? split.maximum : null;
    if (next === null) return;
    event.preventDefault();
    split.commit(next);
  };
  return <div className="research-observation-resize" role="separator" aria-label="调整时间线与输出宽度" aria-orientation="vertical"
    aria-controls="research-timeline-panel research-output-panel" aria-valuemin={Math.round(split.minimum)}
    aria-valuemax={Math.round(split.maximum)} aria-valuenow={Math.round(split.width)}
    aria-valuetext={`时间线宽度 ${Math.round(split.width)} 像素`}
    title="左右拖动调整宽度，双击恢复默认" tabIndex={0} data-dragging={dragging}
    onDoubleClick={split.reset} onKeyDown={keyboard}
    onPointerDown={event => {
      if (event.button !== 0) return;
      event.preventDefault();
      event.currentTarget.focus({ preventScroll: true });
      drag.current = { pointer: event.pointerId, x: event.clientX, width: split.width, value: split.width };
      event.currentTarget.setPointerCapture(event.pointerId);
      setDragging(true);
    }} onPointerMove={move} onPointerUp={finish} onPointerCancel={finish}
    onLostPointerCapture={() => { drag.current = null; setDragging(false); }} />;
}
