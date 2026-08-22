import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type WheelEvent as ReactWheelEvent,
} from "react";

import type { QuestionTreeItem } from "./api";
import "./QuestionTree.css";

export type QuestionTreeProps = {
  items: readonly QuestionTreeItem[];
  graphRevision: number | null;
  projectionStatus: "ready" | "unavailable" | "capability_unavailable";
  projectionReason?: string | null;
  initialQuestionRef?: string | null;
  manualCreationReady: boolean;
  openingParentRef?: string | null;
  openError?: string | null;
  onClose: () => void;
  onCreateQuestion: (
    parent: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => void | Promise<void>;
};

type CanvasQuestion = QuestionTreeItem & {
  depth: number;
  x: number;
  y: number;
};

type CanvasEdge = {
  parentRef: string;
  childRef: string;
  path: string;
};

type CanvasLayout = {
  nodes: CanvasQuestion[];
  byRef: Map<string, CanvasQuestion>;
  edges: CanvasEdge[];
  width: number;
  height: number;
};

type CanvasTransform = { x: number; y: number; scale: number };

const NODE_WIDTH = 250;
const NODE_HEIGHT = 104;
const COLUMN_GAP = 120;
const ROW_GAP = 56;
const WORLD_MIN_WIDTH = 1_280;
const WORLD_MIN_HEIGHT = 660;
const MIN_SCALE = 0.42;
const MAX_SCALE = 1.8;

function questionLayout(items: readonly QuestionTreeItem[]): CanvasLayout {
  const byQuestionRef = new Map(items.map((item) => [item.question_ref, item]));
  const children = new Map<string, QuestionTreeItem[]>();
  const roots: QuestionTreeItem[] = [];
  for (const item of items) {
    if (
      item.parent_question_ref &&
      item.parent_question_ref !== item.question_ref &&
      byQuestionRef.has(item.parent_question_ref)
    ) {
      children.set(item.parent_question_ref, [
        ...(children.get(item.parent_question_ref) ?? []),
        item,
      ]);
    } else {
      roots.push(item);
    }
  }

  const positions = new Map<string, CanvasQuestion>();
  const visiting = new Set<string>();
  let nextLeafCenter = 120;
  const position = (item: QuestionTreeItem, depth: number): number => {
    const existing = positions.get(item.question_ref);
    if (existing) return existing.y + NODE_HEIGHT / 2;
    visiting.add(item.question_ref);
    const childCenters = (children.get(item.question_ref) ?? [])
      .filter((child) => !visiting.has(child.question_ref))
      .map((child) => position(child, depth + 1));
    const center = childCenters.length
      ? childCenters.reduce((total, value) => total + value, 0) / childCenters.length
      : nextLeafCenter;
    if (!childCenters.length) nextLeafCenter += NODE_HEIGHT + ROW_GAP;
    visiting.delete(item.question_ref);
    positions.set(item.question_ref, {
      ...item,
      depth,
      x: 72 + depth * (NODE_WIDTH + COLUMN_GAP),
      y: center - NODE_HEIGHT / 2,
    });
    return center;
  };

  for (const root of roots) position(root, 0);
  for (const item of items) {
    if (!positions.has(item.question_ref)) position(item, 0);
  }

  const ordered: CanvasQuestion[] = [];
  const orderedRefs = new Set<string>();
  const append = (item: QuestionTreeItem) => {
    if (orderedRefs.has(item.question_ref)) return;
    orderedRefs.add(item.question_ref);
    const positioned = positions.get(item.question_ref);
    if (positioned) ordered.push(positioned);
    for (const child of children.get(item.question_ref) ?? []) append(child);
  };
  for (const root of roots) append(root);
  for (const item of items) append(item);

  const edges: CanvasEdge[] = [];
  for (const child of ordered) {
    if (!child.parent_question_ref) continue;
    const parent = positions.get(child.parent_question_ref);
    if (!parent) continue;
    const startX = parent.x + NODE_WIDTH;
    const startY = parent.y + NODE_HEIGHT / 2;
    const endX = child.x;
    const endY = child.y + NODE_HEIGHT / 2;
    const control = startX + Math.max(48, (endX - startX) / 2);
    edges.push({
      parentRef: parent.question_ref,
      childRef: child.question_ref,
      path: `M ${startX} ${startY} C ${control} ${startY}, ${control} ${endY}, ${endX} ${endY}`,
    });
  }

  const maxX = ordered.reduce(
    (value, item) => Math.max(value, item.x + NODE_WIDTH + 72),
    WORLD_MIN_WIDTH,
  );
  const maxY = ordered.reduce(
    (value, item) => Math.max(value, item.y + NODE_HEIGHT + 68),
    WORLD_MIN_HEIGHT,
  );
  return {
    nodes: ordered,
    byRef: new Map(ordered.map((item) => [item.question_ref, item])),
    edges,
    width: maxX,
    height: maxY,
  };
}

function clampScale(value: number): number {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, value));
}

function nodeClass(item: CanvasQuestion): string {
  if (item.depth === 0) return "root";
  if (item.depth === 1) return "formal";
  return "branch";
}

function nodeLabel(item: CanvasQuestion): string {
  if (item.depth === 0) return "Root Question · RG accepted";
  if (item.depth === 1) return "Formal Question · RG accepted";
  return "Research branch · RG topology";
}

export function QuestionTree({
  items,
  graphRevision,
  projectionStatus,
  projectionReason = null,
  initialQuestionRef = null,
  manualCreationReady,
  openingParentRef = null,
  openError = null,
  onClose,
  onCreateQuestion,
}: QuestionTreeProps) {
  const mainRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<null | {
    pointerId: number;
    clientX: number;
    clientY: number;
    startX: number;
    startY: number;
  }>(null);
  const layout = useMemo(() => questionLayout(items), [items]);
  const [selectedRef, setSelectedRef] = useState<string | null>(
    initialQuestionRef && layout.byRef.has(initialQuestionRef)
      ? initialQuestionRef
      : layout.nodes[0]?.question_ref ?? null,
  );
  const [hoveredRef, setHoveredRef] = useState<string | null>(null);
  const [transform, setTransform] = useState<CanvasTransform>({
    x: 0,
    y: 0,
    scale: 1,
  });
  const [viewport, setViewport] = useState({ width: 0, height: 0 });
  const [dragging, setDragging] = useState(false);
  const selected = selectedRef ? layout.byRef.get(selectedRef) ?? null : null;

  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    if (initialQuestionRef && layout.byRef.has(initialQuestionRef)) {
      setSelectedRef(initialQuestionRef);
      return;
    }
    if (selectedRef && layout.byRef.has(selectedRef)) return;
    setSelectedRef(layout.nodes[0]?.question_ref ?? null);
  }, [initialQuestionRef, layout, selectedRef]);

  const fitCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    const scale = clampScale(Math.min(
      (width - 42) / layout.width,
      (height - 34) / layout.height,
    ));
    setViewport({ width, height });
    setTransform({
      scale,
      x: (width - layout.width * scale) / 2,
      y: (height - layout.height * scale) / 2,
    });
  }, [layout.height, layout.width]);

  useEffect(() => {
    const frame = requestAnimationFrame(fitCanvas);
    const canvas = canvasRef.current;
    if (!canvas || typeof ResizeObserver === "undefined") {
      return () => cancelAnimationFrame(frame);
    }
    const observer = new ResizeObserver(() => fitCanvas());
    observer.observe(canvas);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [fitCanvas]);

  const focusNode = useCallback((questionRef: string) => {
    const node = layout.byRef.get(questionRef);
    const canvas = canvasRef.current;
    if (!node || !canvas) return;
    const rect = canvas.getBoundingClientRect();
    setTransform((current) => ({
      ...current,
      x: rect.width / 2 - (node.x + NODE_WIDTH / 2) * current.scale,
      y: rect.height / 2 - (node.y + NODE_HEIGHT / 2) * current.scale,
    }));
    requestAnimationFrame(() => {
      canvas.querySelector<HTMLElement>(
        `[data-question-ref="${CSS.escape(questionRef)}"]`,
      )?.focus({ preventScroll: true });
    });
  }, [layout]);

  const zoomCanvas = (multiplier: number) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const centerX = rect.width / 2;
    const centerY = rect.height / 2;
    setTransform((current) => {
      const scale = clampScale(current.scale * multiplier);
      const ratio = scale / current.scale;
      return {
        scale,
        x: centerX - (centerX - current.x) * ratio,
        y: centerY - (centerY - current.y) * ratio,
      };
    });
  };

  const wheelCanvas = (event: ReactWheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    setTransform((current) => {
      const scale = clampScale(current.scale * Math.exp(-event.deltaY * 0.0012));
      const ratio = scale / current.scale;
      return {
        scale,
        x: pointerX - (pointerX - current.x) * ratio,
        y: pointerY - (pointerY - current.y) * ratio,
      };
    });
  };

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if ((event.target as Element).closest("button,.question-canvas-node")) return;
    dragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      clientY: event.clientY,
      startX: transform.x,
      startY: transform.y,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setDragging(true);
    setTransform((current) => ({
      ...current,
      x: drag.startX + event.clientX - drag.clientX,
      y: drag.startY + event.clientY - drag.clientY,
    }));
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    dragRef.current = null;
    setDragging(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const selectFromKeyboard = (
    event: KeyboardEvent<HTMLElement>,
    questionRef: string,
  ) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      setSelectedRef(questionRef);
      return;
    }
    const currentIndex = layout.nodes.findIndex(
      (item) => item.question_ref === questionRef,
    );
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? layout.nodes.length - 1
        : event.key === "ArrowRight" || event.key === "ArrowDown"
          ? Math.min(layout.nodes.length - 1, currentIndex + 1)
          : event.key === "ArrowLeft" || event.key === "ArrowUp"
            ? Math.max(0, currentIndex - 1)
            : currentIndex;
    if (nextIndex === currentIndex || nextIndex < 0) return;
    event.preventDefault();
    const next = layout.nodes[nextIndex];
    setSelectedRef(next.question_ref);
    focusNode(next.question_ref);
  };

  const miniScaleX = 160 / layout.width;
  const miniScaleY = 96 / layout.height;
  const miniViewport = {
    x: Math.max(0, -transform.x / transform.scale * miniScaleX),
    y: Math.max(0, -transform.y / transform.scale * miniScaleY),
    width: Math.max(
      8,
      Math.min(160, viewport.width / transform.scale * miniScaleX),
    ),
    height: Math.max(
      8,
      Math.min(96, viewport.height / transform.scale * miniScaleY),
    ),
  };
  const graphLabel = graphRevision === null ? "Graph revision unavailable" : `Graph r${graphRevision}`;
  const projectionLabel = projectionStatus === "ready"
    ? `只读投影 · ${graphLabel}`
    : `${projectionStatus} · ${projectionReason ?? "question_tree_unavailable"}`;

  return (
    <main
      ref={mainRef}
      id="main-content"
      className="question-tree-main"
      data-shell-region="main"
      data-testid="question-tree"
      tabIndex={-1}
      aria-labelledby="question-tree-title"
    >
      <div className="question-tree-world">
        <header className="question-tree-header">
          <div>
            <small>QUESTION WORLD · {graphLabel}</small>
            <h1 id="question-tree-title">问题树</h1>
            <p>从已接纳父题沿父题—子题关系查看研究支线。树只表达 RG 问题拓扑；Stage 进度仍在总览中单独解释。</p>
          </div>
          <div className="question-tree-tools">
            <span className="projection">{projectionLabel}</span>
            <button type="button" disabled title="capability_unavailable · stdout">
              当前实验 · stdout
            </button>
            <button
              type="button"
              disabled={!selected || !manualCreationReady || openingParentRef !== null}
              onClick={(event) => {
                if (selected) void onCreateQuestion(selected, event.currentTarget);
              }}
            >
              ＋ 创建 Question
            </button>
            <button
              type="button"
              disabled={!selected}
              onClick={() => selected && focusNode(selected.question_ref)}
            >
              聚焦当前支线
            </button>
            <button type="button" onClick={fitCanvas}>适配画布</button>
            <button type="button" onClick={onClose}>回到总览</button>
          </div>
        </header>

        {openError ? (
          <div className="question-tree-error" role="alert">
            <b>ManualCreation 未打开</b>
            <span>{openError}</span>
          </div>
        ) : null}

        <section className="question-tree-card" aria-label="当前问题树画布">
          <div className="question-tree-topline">
            <b>问题树画布</b>
            <small>拖拽平移 · 滚轮缩放 · {graphLabel} 只读投影</small>
          </div>
          <div
            ref={canvasRef}
            className={`question-tree-canvas${dragging ? " dragging" : ""}`}
            tabIndex={0}
            aria-label="可拖拽和缩放的 Quest 问题树画布"
            onWheel={wheelCanvas}
            onPointerDown={startDrag}
            onPointerMove={moveDrag}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
          >
            {layout.nodes.length ? (
              <div
                className="question-tree-canvas-world"
                role="tree"
                aria-label="Quest 问题树"
                style={{
                  width: layout.width,
                  height: layout.height,
                  transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
                }}
              >
                <svg
                  className="question-tree-edges"
                  viewBox={`0 0 ${layout.width} ${layout.height}`}
                  width={layout.width}
                  height={layout.height}
                  aria-hidden="true"
                >
                  {layout.edges.map((edge) => (
                    <path
                      className={
                        hoveredRef &&
                        [edge.parentRef, edge.childRef].includes(hoveredRef)
                          ? "highlight"
                          : undefined
                      }
                      d={edge.path}
                      key={`${edge.parentRef}:${edge.childRef}`}
                    />
                  ))}
                </svg>
                {layout.nodes.map((item) => (
                  <article
                    className={`question-canvas-node ${nodeClass(item)}`}
                    style={{ left: item.x, top: item.y } as CSSProperties}
                    role="treeitem"
                    aria-level={item.depth + 1}
                    aria-selected={item.question_ref === selected?.question_ref}
                    tabIndex={item.question_ref === selected?.question_ref ? 0 : -1}
                    key={item.question_ref}
                    data-question-ref={item.question_ref}
                    onClick={() => setSelectedRef(item.question_ref)}
                    onKeyDown={(event) => selectFromKeyboard(event, item.question_ref)}
                    onMouseEnter={() => setHoveredRef(item.question_ref)}
                    onMouseLeave={() => setHoveredRef(null)}
                  >
                    <button
                      className="question-node-action prune"
                      type="button"
                      aria-label={`剪裁 ${item.question_ref}`}
                      title="capability_unavailable · question_pruning"
                      disabled
                    >
                      ×
                    </button>
                    <small>{nodeLabel(item)}</small>
                    <b>{item.title ?? item.unknown_statement ?? item.question_ref}</b>
                    <span>{item.question_ref} · parent {item.parent_question_ref ?? "root"}</span>
                    <button
                      className="question-node-action add"
                      type="button"
                      aria-label={`在 ${item.question_ref} 下创建子问题`}
                      title={manualCreationReady
                        ? "创建子问题"
                        : "ManualCreation capability_unavailable"}
                      disabled={!manualCreationReady || openingParentRef !== null}
                      aria-busy={openingParentRef === item.question_ref}
                      data-create-parent-ref={item.question_ref}
                      onClick={(event) => {
                        event.stopPropagation();
                        void onCreateQuestion(item, event.currentTarget);
                      }}
                    >
                      {openingParentRef === item.question_ref ? "…" : "＋"}
                    </button>
                  </article>
                ))}
              </div>
            ) : (
              <div className="question-tree-empty">
                <span aria-hidden="true">◇</span>
                <b>{projectionStatus === "ready" ? "还没有已接纳 Question" : "问题树 Projection 不可用"}</b>
                <p>{projectionStatus === "ready"
                  ? "先完成 Quest 与首问题接纳；这里不会用临时 Proposal 填充问题树。"
                  : projectionReason ?? "question_tree_unavailable"}</p>
              </div>
            )}
            <div className="question-tree-canvas-toolbar" aria-label="问题树画布工具">
              <button type="button" title="适配全图" aria-label="适配全图" onClick={fitCanvas}>⤢</button>
              <button type="button" title="放大" aria-label="放大问题树" onClick={() => zoomCanvas(1.18)}>＋</button>
              <button type="button" title="缩小" aria-label="缩小问题树" onClick={() => zoomCanvas(0.84)}>−</button>
            </div>
            <div className="question-tree-canvas-hint">空白处拖拽 · 滚轮缩放</div>
            {layout.nodes.length ? (
              <svg
                className="question-tree-minimap"
                viewBox="0 0 160 96"
                aria-label="问题树小地图"
              >
                {layout.edges.map((edge) => {
                  const parent = layout.byRef.get(edge.parentRef)!;
                  const child = layout.byRef.get(edge.childRef)!;
                  return (
                    <path
                      d={`M ${(parent.x + NODE_WIDTH) * miniScaleX} ${(parent.y + NODE_HEIGHT / 2) * miniScaleY} L ${child.x * miniScaleX} ${(child.y + NODE_HEIGHT / 2) * miniScaleY}`}
                      key={`${edge.parentRef}:${edge.childRef}`}
                    />
                  );
                })}
                {layout.nodes.map((item) => (
                  <rect
                    className={item.question_ref === selected?.question_ref ? "current" : undefined}
                    x={item.x * miniScaleX}
                    y={item.y * miniScaleY}
                    width={Math.max(8, NODE_WIDTH * miniScaleX)}
                    height={Math.max(5, NODE_HEIGHT * miniScaleY)}
                    rx="3"
                    key={item.question_ref}
                  />
                ))}
                <rect
                  className="viewport"
                  x={miniViewport.x}
                  y={miniViewport.y}
                  width={miniViewport.width}
                  height={miniViewport.height}
                  rx="5"
                />
              </svg>
            ) : null}
          </div>
          <div className="question-tree-caption">
            <span><i aria-hidden="true" /> 实线：RG 父子拓扑</span>
            <span><i className="selected" aria-hidden="true" /> 紫色光晕：本地选中</span>
            <span><i className="human" aria-hidden="true" /> 珊瑚点：Projection 未提供关联 HumanRequest</span>
            <span>悬停节点：左侧剪裁（typed disabled）· 右侧新建子问题</span>
          </div>
        </section>

        {selected ? (
          <section className="question-tree-inspector" aria-live="polite" aria-label="选中问题详情">
            <div className="question-tree-inspector-main">
              <div className="question-tree-inspector-kicker">
                <span>{nodeLabel(selected)}</span>
                <small>{selected.question_ref}</small>
              </div>
              <h2>{selected.title ?? "已接纳 Question"}</h2>
              <p>{selected.unknown_statement ?? "公开 Projection 没有附带 unknown statement。"}</p>
              <div className="question-tree-inspector-actions">
                <button type="button" disabled title="capability_unavailable · quest_companion">与 Companion 讨论此题</button>
                <button type="button" disabled title="capability_unavailable · question_evidence">查看证据与来源</button>
                <button type="button" disabled title="capability_unavailable · question_history">问题历史 ↗</button>
              </div>
            </div>
            <div className="question-tree-inspector-facts">
              <div><small>Topology / RG</small><b>{selected.parent_question_ref ? `${selected.parent_question_ref} 的直接子题` : "Quest 根问题"} · {graphLabel}</b></div>
              <div><small>Question fact / RG</small><b>{selected.question_receipt_ref}</b></div>
              <div><small>Content fact / RM</small><b>{selected.content_ref} · {selected.content_hash}</b></div>
              <div><small>Cycle binding / AE</small><b>capability_unavailable · Projection 未提供</b></div>
            </div>
          </section>
        ) : null}
      </div>
    </main>
  );
}

export default QuestionTree;
