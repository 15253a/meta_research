import { useState, type ReactNode } from "react";

/** Collapsed histories do not construct their bodies. A page replaces the last page. */
export function BoundedDetails({ summary, children, className, defaultOpen = false }: { summary: ReactNode; children: () => ReactNode; className?: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return <details className={className} open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary>{summary}</summary>{open ? children() : null}</details>;
}

export function PageWindow<T>({ items, size = 12, label, render }: { items: readonly T[]; size?: number; label: string; render: (item: T, index: number) => ReactNode }) {
  const [page, setPage] = useState(0);
  const count = Math.max(1, Math.ceil(items.length / size));
  const bounded = Math.min(page, count - 1);
  return <>{items.slice(bounded * size, (bounded + 1) * size).map((item, index) => render(item, bounded * size + index))}
    {count > 1 ? <nav className="research-history-pages" aria-label={label}><button type="button" disabled={bounded === 0} onClick={() => setPage(bounded - 1)}>上一页</button><span>{bounded + 1} / {count} · 共 {items.length} 项</span><button type="button" disabled={bounded === count - 1} onClick={() => setPage(bounded + 1)}>下一页</button></nav> : null}</>;
}
