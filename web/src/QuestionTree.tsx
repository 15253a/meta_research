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

import {
  fetchQuestionEvidence,
  fetchQuestionHistory,
  type ExperimentProjection,
  type QuestionEvidenceView,
  type QuestionHistoryView,
  type QuestionTreeItem,
} from "./api";
import "./QuestionTree.css";

export type QuestionTreeProps = {
  items: readonly QuestionTreeItem[];
  graphRevision: number | null;
  projectionStatus: "ready" | "unavailable" | "capability_unavailable";
  projectionReason?: string | null;
  initialQuestionRef?: string | null;
  completionLanding?: {
    initializationId: string;
    questionRef: string;
    questionTitle: string;
  } | null;
  manualCreationReady: boolean;
  controlsInert?: boolean;
  openingParentRef?: string | null;
  openError?: string | null;
  currentExperiment?: ExperimentProjection | null;
  initialInspectorMode?: "evidence" | "history" | null;
  onInspectorModeChange?: (mode: "evidence" | "history" | null) => void;
  onClose: () => void;
  onOpenExperiment?: (opener: HTMLButtonElement) => void;
  onSelectionChange?: (selected: QuestionTreeItem | null) => void;
  onDiscussQuestion?: (
    selected: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => void;
  onCreateQuestion: (
    parent: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => void | Promise<void>;
  onControlQuestion: (
    action: "prune",
    question: QuestionTreeItem,
    opener: HTMLButtonElement,
  ) => void | Promise<void>;
};

type CanvasQuestion = QuestionTreeItem & {
  depth: number;
  x: number;
  y: number;
};

type InspectorQuery =
  | {
      mode: "evidence" | "history";
      status: "loading";
      questionRef: string;
      basisKey: string;
    }
  | {
      mode: "evidence";
      status: "ready";
      questionRef: string;
      basisKey: string;
      value: QuestionEvidenceView;
    }
  | {
      mode: "history";
      status: "ready";
      questionRef: string;
      basisKey: string;
      value: QuestionHistoryView;
    }
  | {
      mode: "evidence" | "history";
      status: "error";
      questionRef: string;
      basisKey: string;
      code: string;
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
const OUTLINE_MEDIA_QUERY = "(max-width: 620px)";

function currentExperimentHasFence(
  experiment: ExperimentProjection | null | undefined,
): boolean {
  const execution = experiment?.execution;
  return Boolean(
    execution?.run_ref &&
      typeof execution.attempt_generation === "number" &&
      execution.root_session_ref &&
      execution.fence_ref &&
      Array.isArray(execution.events),
  );
}

function cycleBindingCopy(item: QuestionTreeItem): string {
  const binding = item.cycle_binding;
  if (binding.status !== "bound" || !binding.foreground || !binding.cycle_ref) {
    return `${binding.status} · ${binding.reason?.code ?? "cycle_binding_not_bound"}`;
  }
  return `${binding.cycle_ref} · ${binding.foreground.stage} · epoch ${binding.foreground.epoch} · ${binding.foreground.status}`;
}

function relatedHumanRequestCopy(item: QuestionTreeItem): string {
  const related = item.related_human_requests;
  if (related.status !== "ready") {
    return `${related.status} · ${related.reason?.code ?? "human_request_projection_unavailable"}`;
  }
  if (!related.items.length) return "当前问题没有显式关联 HumanRequest";
  return related.items.map((request) => {
    const bindings = request.bindings.map((binding) => (
      `${binding.source}.${binding.field}=${binding.ref}`
    )).join(", ");
    return `${request.request_ref} · ${request.kind} · ${request.status} · ${bindings}`;
  }).join(" / ");
}

function relatedHumanRequestCount(item: QuestionTreeItem): number {
  return item.related_human_requests.status === "ready"
    ? item.related_human_requests.items.length
    : 0;
}

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

function questionInspectorBasis(item: QuestionTreeItem): string {
  return [
    item.quest_ref,
    item.question_ref,
    item.content_hash,
    item.question_receipt_ref,
    item.lifecycle_revision,
  ].join(":");
}

function validateEvidenceResponse(
  value: QuestionEvidenceView,
  question: QuestionTreeItem,
): QuestionEvidenceView {
  if (
    value.question_ref !== question.question_ref
    || value.quest_ref !== question.quest_ref
    || (value.status === "ready" && (
      value.binding?.question_receipt_ref !== question.question_receipt_ref
      || value.items.some((item) => (
        item.evidence_ref !== item.role.version_ref
        || item.evidence_ref !== item.asset.version_ref
        || item.role.quest_ref !== question.quest_ref
      ))
    ))
  ) {
    throw new Error("question_evidence_response_identity_invalid");
  }
  return value;
}

function validateHistoryResponse(
  value: QuestionHistoryView,
  question: QuestionTreeItem,
): QuestionHistoryView {
  const readyPageInvalid = value.status === "ready" && (
    !value.question
    || !value.lifecycle
    || !Number.isInteger(value.offset)
    || value.offset < 0
    || !Number.isInteger(value.limit)
    || value.limit < 1
    || !Number.isInteger(value.total_count)
    || value.total_count < 1
    || value.total_count !== value.lifecycle.revision
    || value.offset >= value.total_count
    || value.events.length < 1
    || value.events.length > value.limit
    || value.events.length !== Math.min(
      value.limit,
      value.total_count - value.offset,
    )
    || value.has_more !== (
      value.offset + value.events.length < value.total_count
    )
    || value.events.some((event, index) => {
      const revision = value.offset + index + 1;
      return !event.affected_question_refs.includes(question.question_ref)
        || event.lifecycle_revision !== revision
        || (event.action === "accepted" && (
          revision !== 1
          || event.question_ref !== question.question_ref
          || event.record_ref !== question.question_ref
          || event.status !== "active"
          || event.receipt_ref
            !== value.question?.receipts.question_acceptance.receipt_ref
        ))
        || (event.action === "prune" && event.status !== "pruned")
        || (event.action === "restore" && event.status !== "active")
        || (revision !== 1 && event.action === "accepted");
    })
    || (value.offset === 0 && value.events[0]?.action !== "accepted")
    || (!value.has_more
      && value.events.at(-1)?.status !== value.lifecycle.status)
  );
  if (
    value.question_ref !== question.question_ref
    || readyPageInvalid
    || (value.status === "ready" && (
      value.question?.question_ref !== question.question_ref
      || value.question.quest_ref !== question.quest_ref
      || value.question.parent_question_ref !== question.parent_question_ref
      || value.question.content.content_ref !== question.content_ref
      || value.question.content.content_hash !== question.content_hash
      || value.question.receipts.question_acceptance.receipt_ref
        !== question.question_receipt_ref
      || value.lifecycle?.question_ref !== question.question_ref
      || value.lifecycle.quest_ref !== question.quest_ref
      || value.lifecycle.revision < question.lifecycle_revision
    ))
  ) {
    throw new Error("question_history_response_identity_invalid");
  }
  return value;
}

function EvidenceInspector({ value }: { value: QuestionEvidenceView }) {
  if (value.status !== "ready") {
    return (
      <div className="question-query-state">
        <b>{value.status === "absent" ? "没有精确 Question 证据绑定" : "证据查询暂不可用"}</b>
        <code>{value.reason?.code ?? "question_evidence_unavailable"}</code>
        {typeof value.reason?.quest_evidence_role_count === "number" ? (
          <small>Quest 有 {value.reason.quest_evidence_role_count} 个 evidence role；未把它们冒充为当前 Question 的来源。</small>
        ) : null}
      </div>
    );
  }
  return (
    <>
      <div className="question-query-binding">
        <span><small>Question binding / AE</small><b>{value.binding?.request_ref}</b></span>
        <span><small>ContextPack</small><b>{value.binding?.context_pack_ref} · evidence r{value.binding?.evidence_reference_revision}</b></span>
        <span><small>Question receipt / RG</small><b>{value.binding?.question_receipt_ref}</b></span>
      </div>
      <div className="question-evidence-list">
        {value.items.map((item) => (
          <article key={item.evidence_ref}>
            <small>EVIDENCE REF · {item.asset.media_type}</small>
            <h3>{item.asset.display_name}</h3>
            <code>{item.evidence_ref}</code>
            <dl>
              <div><dt>RG role receipt</dt><dd>{item.role.receipt.receipt_ref}</dd></div>
              <div><dt>RM asset receipt</dt><dd>{item.asset.receipt.receipt_ref}</dd></div>
              <div><dt>Integrity / availability</dt><dd>{item.asset.integrity} · {item.asset.availability}</dd></div>
              <div><dt>Content hash</dt><dd>{item.asset.content_hash}</dd></div>
            </dl>
          </article>
        ))}
      </div>
    </>
  );
}

function HistoryInspector({
  value,
  loadingMore,
  onLoadMore,
}: {
  value: QuestionHistoryView;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  if (value.status !== "ready" || !value.question || !value.lifecycle) {
    return (
      <div className="question-query-state">
        <b>{value.status === "absent" ? "未找到已接纳 Question" : "问题历史暂不可用"}</b>
        <code>{value.reason?.code ?? "question_history_unavailable"}</code>
      </div>
    );
  }
  const content = value.question.content.document;
  return (
    <>
      <div className="question-history-content">
        <div>
          <small>ACCEPTED CONTENT / RM</small>
          <h3>{content.title}</h3>
          <p>{content.unknown_statement}</p>
        </div>
        <dl>
          <div><dt>Identity / RG</dt><dd>{value.question.question_ref} · parent {value.question.parent_question_ref ?? "root"}</dd></div>
          <div><dt>Lifecycle / RG</dt><dd>{value.lifecycle.status} · r{value.lifecycle.revision}</dd></div>
          <div><dt>Content receipt</dt><dd>{value.question.receipts.content_acceptance.receipt_ref}</dd></div>
          <div><dt>Question receipt</dt><dd>{value.question.receipts.question_acceptance.receipt_ref}</dd></div>
        </dl>
      </div>
      <ol className="question-history-events">
        {value.events.map((event) => (
          <li key={`${event.action}:${event.record_ref}`}>
            <span aria-hidden="true" />
            <div>
              <small>{event.action.toUpperCase()} · lifecycle r{event.lifecycle_revision}</small>
              <b>{event.record_ref}</b>
              <code>{event.receipt_ref}</code>
              {event.prune_record_ref ? <p>PruneRecord {event.prune_record_ref}</p> : null}
              {event.restore_record_ref ? <p>RestoreRecord {event.restore_record_ref}</p> : null}
            </div>
          </li>
        ))}
      </ol>
      {value.has_more ? (
        <button
          className="question-history-more"
          type="button"
          disabled={loadingMore}
          onClick={onLoadMore}
        >
          {loadingMore
            ? "正在读取下一页…"
            : `继续读取历史 · ${value.events.length}/${value.total_count}`}
        </button>
      ) : null}
    </>
  );
}

export function QuestionTree({
  items,
  graphRevision,
  projectionStatus,
  projectionReason = null,
  initialQuestionRef = null,
  completionLanding = null,
  manualCreationReady,
  controlsInert = false,
  openingParentRef = null,
  openError = null,
  currentExperiment = null,
  initialInspectorMode = null,
  onInspectorModeChange,
  onClose,
  onOpenExperiment,
  onSelectionChange,
  onDiscussQuestion,
  onCreateQuestion,
  onControlQuestion,
}: QuestionTreeProps) {
  const mainRef = useRef<HTMLElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const evidenceButtonRef = useRef<HTMLButtonElement>(null);
  const historyButtonRef = useRef<HTMLButtonElement>(null);
  const reportedSelectionKeyRef = useRef<string | null | undefined>(undefined);
  const appliedInitialQuestionRef = useRef<string | null>(initialQuestionRef);
  const focusedCompletionLandingRef = useRef<string | null>(null);
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
  const [outlineMode, setOutlineMode] = useState(
    () => typeof window !== "undefined" && window.matchMedia(OUTLINE_MEDIA_QUERY).matches,
  );
  const selected = selectedRef ? layout.byRef.get(selectedRef) ?? null : null;
  const inspectorAbortRef = useRef<AbortController | null>(null);
  const [inspectorQuery, setInspectorQuery] = useState<InspectorQuery | null>(null);
  const [historyLoadingMore, setHistoryLoadingMore] = useState(false);
  const selectedInspectorBasis = selected ? questionInspectorBasis(selected) : null;
  const routedInspectorQuestion = initialQuestionRef
    ? layout.byRef.get(initialQuestionRef) ?? null
    : null;
  const inspectorTarget = initialInspectorMode && routedInspectorQuestion
    ? routedInspectorQuestion
    : selected;
  const inspectorTargetBasis = inspectorTarget
    ? questionInspectorBasis(inspectorTarget)
    : null;
  const experimentObservable = currentExperimentHasFence(currentExperiment) &&
    Boolean(onOpenExperiment);

  const loadInspector = useCallback(async (
    mode: "evidence" | "history",
    question: QuestionTreeItem,
  ) => {
    const basisKey = questionInspectorBasis(question);
    inspectorAbortRef.current?.abort();
    const controller = new AbortController();
    inspectorAbortRef.current = controller;
    setInspectorQuery({
      mode,
      status: "loading",
      questionRef: question.question_ref,
      basisKey,
    });
    onInspectorModeChange?.(mode);
    try {
      if (mode === "evidence") {
        const value = validateEvidenceResponse(await fetchQuestionEvidence(
          question.question_ref,
          controller.signal,
        ), question);
        if (!controller.signal.aborted) {
          setInspectorQuery({
            mode,
            status: "ready",
            questionRef: question.question_ref,
            basisKey,
            value,
          });
        }
      } else {
        const value = validateHistoryResponse(await fetchQuestionHistory(
          question.question_ref,
          { signal: controller.signal },
        ), question);
        if (!controller.signal.aborted) {
          setInspectorQuery({
            mode,
            status: "ready",
            questionRef: question.question_ref,
            basisKey,
            value,
          });
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setInspectorQuery({
          mode,
          status: "error",
          questionRef: question.question_ref,
          basisKey,
          code: error instanceof Error ? error.message : "question_query_failed",
        });
      }
    }
  }, [onInspectorModeChange]);

  const loadMoreHistory = useCallback(async () => {
    if (
      !selected
      || inspectorQuery?.status !== "ready"
      || inspectorQuery.mode !== "history"
      || !inspectorQuery.value.has_more
      || historyLoadingMore
    ) return;
    inspectorAbortRef.current?.abort();
    const controller = new AbortController();
    inspectorAbortRef.current = controller;
    setHistoryLoadingMore(true);
    try {
      const current = inspectorQuery.value;
      const next = validateHistoryResponse(await fetchQuestionHistory(
        selected.question_ref,
        {
          offset: current.offset + current.events.length,
          limit: current.limit,
          signal: controller.signal,
        },
      ), selected);
      if (
        next.status !== "ready"
        || next.offset !== current.offset + current.events.length
        || next.total_count !== current.total_count
      ) {
        throw new Error("question_history_page_invalid");
      }
      if (!controller.signal.aborted) {
        setInspectorQuery({
          mode: "history",
          status: "ready",
          questionRef: selected.question_ref,
          basisKey: questionInspectorBasis(selected),
          value: {
            ...next,
            offset: current.offset,
            events: [...current.events, ...next.events],
          },
        });
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setInspectorQuery({
          mode: "history",
          status: "error",
          questionRef: selected.question_ref,
          basisKey: questionInspectorBasis(selected),
          code: error instanceof Error ? error.message : "question_history_failed",
        });
      }
    } finally {
      if (!controller.signal.aborted) setHistoryLoadingMore(false);
    }
  }, [historyLoadingMore, inspectorQuery, selected]);

  useEffect(() => {
    if (!inspectorTarget) {
      inspectorAbortRef.current?.abort();
      setHistoryLoadingMore(false);
      setInspectorQuery(null);
      return;
    }
    if (initialInspectorMode) {
      if (
        inspectorQuery?.mode !== initialInspectorMode
        || inspectorQuery.questionRef !== inspectorTarget.question_ref
        || inspectorQuery.basisKey !== inspectorTargetBasis
      ) {
        void loadInspector(initialInspectorMode, inspectorTarget);
      }
      return;
    }
    if (
      inspectorQuery?.questionRef !== selected?.question_ref
      || inspectorQuery?.basisKey !== selectedInspectorBasis
    ) {
      inspectorAbortRef.current?.abort();
      setHistoryLoadingMore(false);
      setInspectorQuery(null);
    }
  }, [initialInspectorMode, inspectorTargetBasis, loadInspector]);

  useEffect(() => () => inspectorAbortRef.current?.abort(), []);

  useEffect(() => {
    mainRef.current?.focus({ preventScroll: true });
  }, []);

  useEffect(() => {
    const media = window.matchMedia(OUTLINE_MEDIA_QUERY);
    const update = () => setOutlineMode(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const navigationChanged = appliedInitialQuestionRef.current !== initialQuestionRef;
    appliedInitialQuestionRef.current = initialQuestionRef;
    if (
      navigationChanged
      && initialQuestionRef
      && layout.byRef.has(initialQuestionRef)
    ) {
      setSelectedRef(initialQuestionRef);
      return;
    }
    if (!selectedRef || !layout.byRef.has(selectedRef)) {
      setSelectedRef(
        initialQuestionRef && layout.byRef.has(initialQuestionRef)
          ? initialQuestionRef
          : layout.nodes[0]?.question_ref ?? null,
      );
    }
  }, [initialQuestionRef, layout, selectedRef]);

  useEffect(() => {
    const nextKey = selected
      ? `${selected.question_ref}:${selected.content_hash}:${selected.lifecycle_revision}`
      : null;
    if (reportedSelectionKeyRef.current === nextKey) return;
    reportedSelectionKeyRef.current = nextKey;
    onSelectionChange?.(selected);
  }, [onSelectionChange, selected]);

  const fitCanvas = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    setViewport((current) => (
      current.width === width && current.height === height
        ? current
        : { width, height }
    ));
    if (outlineMode) {
      setTransform((current) => (
        current.x === 0 && current.y === 0 && current.scale === 1
          ? current
          : { x: 0, y: 0, scale: 1 }
      ));
      return;
    }
    const scale = clampScale(Math.min(
      (width - 42) / layout.width,
      (height - 34) / layout.height,
    ));
    const next = {
      scale,
      x: (width - layout.width * scale) / 2,
      y: (height - layout.height * scale) / 2,
    };
    setTransform((current) => (
      Math.abs(current.x - next.x) < 0.01 &&
      Math.abs(current.y - next.y) < 0.01 &&
      Math.abs(current.scale - next.scale) < 0.0001
        ? current
        : next
    ));
  }, [layout.height, layout.width, outlineMode]);

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
    if (!outlineMode) {
      const rect = canvas.getBoundingClientRect();
      setTransform((current) => ({
        ...current,
        x: rect.width / 2 - (node.x + NODE_WIDTH / 2) * current.scale,
        y: rect.height / 2 - (node.y + NODE_HEIGHT / 2) * current.scale,
      }));
    }
    requestAnimationFrame(() => {
      const target = canvas.querySelector<HTMLElement>(
        `[data-question-ref="${CSS.escape(questionRef)}"]`,
      );
      target?.focus({ preventScroll: true });
      if (outlineMode) target?.scrollIntoView({ block: "nearest" });
    });
  }, [layout, outlineMode]);

  useEffect(() => {
    if (!completionLanding || !layout.byRef.has(completionLanding.questionRef)) return;
    const landingKey = `${completionLanding.initializationId}:${completionLanding.questionRef}`;
    if (focusedCompletionLandingRef.current === landingKey) return;
    focusedCompletionLandingRef.current = landingKey;
    setSelectedRef(completionLanding.questionRef);
    focusNode(completionLanding.questionRef);
  }, [completionLanding, focusNode, layout]);

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
    if (outlineMode) return;
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
    if (outlineMode) return;
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
            <button
              type="button"
              disabled={controlsInert || !experimentObservable}
              title={experimentObservable
                ? "重新打开当前 Experiment identity 的 stdout"
                : onOpenExperiment
                  ? "当前没有可观测 Experiment identity"
                  : "capability_unavailable · current_experiment_observer"}
              onClick={(event) => {
                if (experimentObservable) onOpenExperiment?.(event.currentTarget);
              }}
            >
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
            <button className="question-tree-canvas-only" type="button" onClick={fitCanvas}>适配画布</button>
            <button type="button" onClick={onClose}>回到总览</button>
          </div>
        </header>

        {openError ? (
          <div className="question-tree-error" role="alert">
            <b>ManualCreation 未打开</b>
            <span>{openError}</span>
          </div>
        ) : null}

        <section
          className="question-tree-card"
          aria-label={outlineMode ? "当前问题树缩进大纲" : "当前问题树画布"}
        >
          {completionLanding ? (
            <div
              className={`question-tree-handoff${
                layout.byRef.has(completionLanding.questionRef) ? " located" : " syncing"
              }`}
              role="status"
              aria-live="polite"
              aria-atomic="true"
              aria-label={`Quest 创建完成。第一个正式 Question：${
                completionLanding.questionTitle
              }。${completionLanding.questionRef} · ${
                layout.byRef.has(completionLanding.questionRef)
                  ? "已定位到问题树当前节点"
                  : "问题树 Projection 正在同步"
              }`}
            >
              <span aria-hidden="true">✓</span>
              <div>
                <small>FIRST FORMAL QUESTION · CURRENT NODE</small>
                <b>第一个正式 Question：{completionLanding.questionTitle}</b>
                <code>
                  {completionLanding.questionRef} · {layout.byRef.has(completionLanding.questionRef)
                    ? "已定位到问题树当前节点"
                    : "问题树 Projection 正在同步"}
                </code>
              </div>
            </div>
          ) : null}
          <div className="question-tree-topline">
            <b>{outlineMode ? "问题树大纲" : "问题树画布"}</b>
            <small>{outlineMode
              ? `缩进层级 · 键盘导航 · ${graphLabel} 只读投影`
              : `拖拽平移 · 滚轮缩放 · ${graphLabel} 只读投影`}</small>
          </div>
          <div
            ref={canvasRef}
            className={`question-tree-canvas${dragging ? " dragging" : ""}`}
            data-layout-mode={outlineMode ? "outline" : "canvas"}
            tabIndex={outlineMode ? -1 : 0}
            aria-label={outlineMode
              ? "Quest 问题树缩进大纲"
              : "可拖拽和缩放的 Quest 问题树画布"}
            onWheel={outlineMode ? undefined : wheelCanvas}
            onPointerDown={outlineMode ? undefined : startDrag}
            onPointerMove={outlineMode ? undefined : moveDrag}
            onPointerUp={outlineMode ? undefined : endDrag}
            onPointerCancel={outlineMode ? undefined : endDrag}
          >
            {layout.nodes.length ? (
              <div
                className="question-tree-canvas-world"
                role="tree"
                aria-label="Quest 问题树"
                style={{
                  width: outlineMode ? "100%" : layout.width,
                  height: outlineMode ? "auto" : layout.height,
                  transform: outlineMode
                    ? "none"
                    : `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
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
                    style={{
                      left: item.x,
                      top: item.y,
                      "--question-outline-indent": `${Math.min(item.depth, 4) * 18}px`,
                    } as CSSProperties}
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
                      title={controlsInert
                        ? "capability_unavailable · question_pruning"
                        : "建立剪裁 Question 的控制草案"}
                      disabled={controlsInert}
                      onClick={(event) => {
                        event.stopPropagation();
                        void onControlQuestion(
                          "prune",
                          item,
                          event.currentTarget,
                        );
                      }}
                    >
                      ×
                    </button>
                    <small>{nodeLabel(item)}</small>
                    <b>{item.title ?? item.unknown_statement ?? item.question_ref}</b>
                    <span>{item.question_ref} · parent {item.parent_question_ref ?? "root"}</span>
                    {!controlsInert && relatedHumanRequestCount(item) ? (
                      <i
                        className="question-node-human-request"
                        aria-label={`关联 ${relatedHumanRequestCount(item)} 个需人工处理事项`}
                        title={`关联 ${relatedHumanRequestCount(item)} 个需人工处理事项`}
                      />
                    ) : null}
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
            {controlsInert ? (
              <span><i className="human" aria-hidden="true" /> 珊瑚点：当前未显示关联的人工处理事项</span>
            ) : (
              <span><i className="human" aria-hidden="true" /> 珊瑚点：关联的人工处理事项</span>
            )}
            <span>{controlsInert
              ? "悬停节点：左侧剪裁（typed disabled）· 右侧新建子问题"
              : "悬停节点：左侧建立剪裁草案 · 右侧新建子问题"}</span>
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
                <button
                  type="button"
                  disabled={controlsInert || !onDiscussQuestion}
                  title={!controlsInert && onDiscussQuestion
                    ? "将当前问题作为 Companion 的只读讨论上下文"
                    : "capability_unavailable · quest_companion"}
                  onClick={(event) => onDiscussQuestion?.(selected, event.currentTarget)}
                >
                  与 Companion 讨论此题
                </button>
                <button
                  ref={evidenceButtonRef}
                  type="button"
                  disabled={controlsInert}
                  aria-expanded={inspectorQuery?.mode === "evidence"}
                  onClick={() => void loadInspector("evidence", selected)}
                >
                  查看证据与来源
                </button>
                <button
                  ref={historyButtonRef}
                  type="button"
                  disabled={controlsInert}
                  aria-expanded={inspectorQuery?.mode === "history"}
                  onClick={() => void loadInspector("history", selected)}
                >
                  问题历史 ↗
                </button>
              </div>
              {inspectorQuery ? (
                <section
                  className="question-tree-query-panel"
                  role="region"
                  aria-label={inspectorQuery.mode === "evidence" ? "问题证据与来源" : "问题历史"}
                  data-query-status={inspectorQuery.status}
                >
                  <header>
                    <div>
                      <small>{inspectorQuery.mode === "evidence" ? "EVIDENCE / OWNER BINDINGS" : "HISTORY / OWNER RECEIPTS"}</small>
                      <b>{inspectorQuery.mode === "evidence" ? "证据与来源" : "问题历史"}</b>
                    </div>
                    <button
                      type="button"
                      aria-label="关闭问题只读下钻"
                      onClick={() => {
                        const returnFocus = inspectorQuery.mode === "evidence"
                          ? evidenceButtonRef.current
                          : historyButtonRef.current;
                        inspectorAbortRef.current?.abort();
                        setInspectorQuery(null);
                        onInspectorModeChange?.(null);
                        requestAnimationFrame(() => returnFocus?.focus({ preventScroll: true }));
                      }}
                    >
                      ×
                    </button>
                  </header>
                  {inspectorQuery.status === "loading" ? (
                    <div className="question-query-state"><b>正在读取 Owner facts…</b></div>
                  ) : inspectorQuery.status === "error" ? (
                    <div className="question-query-state"><b>只读查询失败</b><code>{inspectorQuery.code}</code></div>
                  ) : inspectorQuery.mode === "evidence" ? (
                    <EvidenceInspector value={inspectorQuery.value} />
                  ) : (
                    <HistoryInspector
                      value={inspectorQuery.value}
                      loadingMore={historyLoadingMore}
                      onLoadMore={() => void loadMoreHistory()}
                    />
                  )}
                </section>
              ) : null}
            </div>
            <div className="question-tree-inspector-facts">
              <div><small>Topology / RG</small><b>{selected.parent_question_ref ? `${selected.parent_question_ref} 的直接子题` : "Quest 根问题"} · {graphLabel}</b></div>
              <div><small>Question fact / RG</small><b>{selected.question_receipt_ref}{controlsInert ? "" : ` · ${selected.lifecycle_status} r${selected.lifecycle_revision}`}</b></div>
              <div><small>Content fact / RM</small><b>{selected.content_ref} · {selected.content_hash}</b></div>
              <div><small>Cycle binding / AE</small><b>{controlsInert
                ? "capability_unavailable · Projection 未提供"
                : cycleBindingCopy(selected)}</b></div>
              {!controlsInert ? (
                <div><small>HumanRequest / Owner</small><b>{relatedHumanRequestCopy(selected)}</b></div>
              ) : null}
            </div>
          </section>
        ) : null}
      </div>
    </main>
  );
}

export default QuestionTree;
