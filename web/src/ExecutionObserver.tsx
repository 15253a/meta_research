import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from "react";

import type {
  ExperimentExecutionProjection,
  ExperimentObservation,
  ExperimentProjection,
} from "./api";
import "./execution-observer.css";

const AUTO_PRESENTED_STORAGE_KEY =
  "meta-research:execution-observer:auto-presented-fence";
const DISMISSED_STORAGE_KEY =
  "meta-research:execution-observer:dismissed-fence";
const LOCALLY_STARTED_STORAGE_KEY =
  "meta-research:execution-observer:locally-started-fence";

type Freshness = "connecting" | "live" | "stale" | "historical";

type TelemetryMetric = {
  key: string;
  label: string;
  value: number;
  displayValue: string;
  unit: string;
  denominator: string;
  percent: number | null;
};

export type ExecutionObserverController = {
  current: ExperimentProjection | null;
  displayed: ExperimentProjection | null;
  isOpen: boolean;
  noticeVisible: boolean;
  open: (trigger?: HTMLElement | null) => void;
  close: () => void;
  deferForPrioritySurface: () => void;
  dismissNotice: () => void;
  recordStarted: (experiment: ExperimentProjection) => void;
};

function sessionValue(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function rememberSessionValue(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    // Session-scoped UI preferences are optional. Domain state never depends on them.
  }
}

function forgetSessionValue(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Session-scoped UI preferences are optional. Domain state never depends on them.
  }
}

function firstVisibleObserverEntry(): HTMLElement | null {
  return Array.from(document.querySelectorAll<HTMLElement>(
    "[data-execution-observer-entry]",
  )).find((entry) => (
    entry.isConnected &&
    entry.offsetParent !== null &&
    !(entry instanceof HTMLButtonElement && entry.disabled)
  )) ?? null;
}

function focusReturnTarget(preferred: HTMLElement | null): void {
  const validPreferred =
    preferred &&
    preferred !== document.body &&
    preferred.isConnected &&
    preferred.offsetParent !== null &&
    !preferred.inert &&
    !(preferred instanceof HTMLButtonElement && preferred.disabled);
  const target = validPreferred ? preferred : firstVisibleObserverEntry();
  target?.focus({ preventScroll: true });
}

export function executionFenceKey(
  experiment: ExperimentProjection | null,
): string | null {
  const execution = experiment?.execution;
  if (
    !execution?.run_ref ||
    execution.attempt_generation === undefined ||
    !execution.root_session_ref
  ) {
    return null;
  }
  return `${execution.run_ref}:g${execution.attempt_generation}:${execution.root_session_ref}`;
}

function currentFenceIsObservable(
  experiment: ExperimentProjection | null,
  locallyStartedFence: string | null,
): boolean {
  const execution = experiment?.execution;
  const fenceKey = executionFenceKey(experiment);
  return Boolean(
    execution &&
      execution.fence_status === "current" &&
      Array.isArray(execution.events) &&
      fenceKey &&
      (
        ["admitted", "running"].includes(execution.status) ||
        (
          locallyStartedFence === fenceKey &&
          ["executed", "failed"].includes(execution.status)
        )
      ),
  );
}

function focusShouldBePreserved(active: Element | null): boolean {
  if (!(active instanceof HTMLElement)) return false;
  if (
    active.matches("input, textarea, select, [contenteditable='true']") ||
    active.closest("dialog[open], [aria-modal='true']:not([aria-hidden='true']), [data-command-draft-open='true']")
  ) {
    return true;
  }
  return false;
}

export function useExecutionObserver(
  current: ExperimentProjection | null,
  autoPresentationBlocked: boolean,
): ExecutionObserverController {
  const [displayed, setDisplayed] = useState<ExperimentProjection | null>(null);
  const [isOpen, setIsOpen] = useState(false);
  const [noticeFence, setNoticeFence] = useState<string | null>(null);
  const [locallyStartedFence, setLocallyStartedFence] = useState<string | null>(
    () => sessionValue(LOCALLY_STARTED_STORAGE_KEY),
  );
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const protectedFocusRef = useRef(false);
  const currentKey = executionFenceKey(current);

  useEffect(() => {
    let releaseTimer: number | null = null;
    const focused = (event: FocusEvent) => {
      if (releaseTimer !== null) window.clearTimeout(releaseTimer);
      protectedFocusRef.current = focusShouldBePreserved(event.target as Element | null);
    };
    const blurred = (event: FocusEvent) => {
      if (event.relatedTarget instanceof Element) {
        protectedFocusRef.current = focusShouldBePreserved(event.relatedTarget);
        return;
      }
      // React may remove the focused launcher/workflow in the same commit that
      // introduces a new Fence. Retain that focus intent through the effect.
      releaseTimer = window.setTimeout(() => {
        protectedFocusRef.current = false;
      }, 1_000);
    };
    protectedFocusRef.current = focusShouldBePreserved(document.activeElement);
    document.addEventListener("focusin", focused, true);
    document.addEventListener("focusout", blurred, true);
    return () => {
      if (releaseTimer !== null) window.clearTimeout(releaseTimer);
      document.removeEventListener("focusin", focused, true);
      document.removeEventListener("focusout", blurred, true);
    };
  }, []);

  useEffect(() => {
    if (!isOpen || !currentKey || executionFenceKey(displayed) !== currentKey) return;
    setDisplayed(current);
  }, [current, currentKey, displayed, isOpen]);

  useEffect(() => {
    if (!currentKey || !currentFenceIsObservable(current, locallyStartedFence)) return;
    if (
      sessionValue(AUTO_PRESENTED_STORAGE_KEY) === currentKey ||
      sessionValue(DISMISSED_STORAGE_KEY) === currentKey
    ) {
      return;
    }

    rememberSessionValue(AUTO_PRESENTED_STORAGE_KEY, currentKey);
    if (locallyStartedFence === currentKey) {
      forgetSessionValue(LOCALLY_STARTED_STORAGE_KEY);
      setLocallyStartedFence(null);
    }
    const active = document.activeElement;
    const blockingSurface = document.querySelector(
      "dialog[open], [aria-modal='true']:not([aria-hidden='true']), [data-command-draft-open='true']",
    );
    if (
      autoPresentationBlocked ||
      document.visibilityState !== "visible" ||
      blockingSurface ||
      protectedFocusRef.current ||
      focusShouldBePreserved(active)
    ) {
      setNoticeFence(currentKey);
      return;
    }

    returnFocusRef.current = active instanceof HTMLElement ? active : null;
    setDisplayed(current);
    setNoticeFence(null);
    setIsOpen(true);
  }, [autoPresentationBlocked, current, currentKey, locallyStartedFence]);

  const recordStarted = useCallback((experiment: ExperimentProjection) => {
    const key = executionFenceKey(experiment);
    if (!key) return;
    rememberSessionValue(LOCALLY_STARTED_STORAGE_KEY, key);
    setLocallyStartedFence(key);
  }, []);

  const open = useCallback((trigger?: HTMLElement | null) => {
    if (!current || !executionFenceKey(current)) return;
    returnFocusRef.current = trigger ?? (
      document.activeElement instanceof HTMLElement ? document.activeElement : null
    );
    setDisplayed(current);
    setNoticeFence(null);
    setIsOpen(true);
  }, [current]);

  const close = useCallback(() => {
    const key = executionFenceKey(displayed);
    if (key) rememberSessionValue(DISMISSED_STORAGE_KEY, key);
    setIsOpen(false);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => focusReturnTarget(returnFocusRef.current));
    });
  }, [displayed]);

  const deferForPrioritySurface = useCallback(() => {
    if (!isOpen) return;
    const key = executionFenceKey(displayed) ?? currentKey;
    setIsOpen(false);
    if (key) setNoticeFence(key);
  }, [currentKey, displayed, isOpen]);

  const dismissNotice = useCallback(() => {
    if (noticeFence) rememberSessionValue(DISMISSED_STORAGE_KEY, noticeFence);
    setNoticeFence(null);
  }, [noticeFence]);

  return {
    current,
    displayed,
    isOpen,
    noticeVisible: Boolean(noticeFence && noticeFence === currentKey && !isOpen),
    open,
    close,
    deferForPrioritySurface,
    dismissNotice,
    recordStarted,
  };
}

function currentAttemptEvents(experiment: ExperimentProjection): ExperimentObservation[] {
  const execution = experiment.execution;
  if (!Array.isArray(execution.events)) return [];
  return execution.events.filter(
    (event) =>
      event.attempt_ref === execution.attempt_ref &&
      event.fence_ref === execution.fence_ref,
  );
}

function epochMilliseconds(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 10_000_000_000 ? value * 1_000 : value;
  }
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function formattedTime(value: unknown): string {
  const milliseconds = epochMilliseconds(value);
  if (milliseconds === null) return "not observed";
  return `${new Date(milliseconds).toISOString().slice(11, 19)} UTC`;
}

function telemetryEvent(
  experiment: ExperimentProjection,
): ExperimentObservation | null {
  return currentAttemptEvents(experiment)
    .filter((event) => event.kind === "telemetry")
    .at(-1) ?? null;
}

function telemetrySampleTime(payload: Record<string, unknown>): unknown {
  return payload.sample_time ?? payload.sampleTime;
}

function telemetryStaleAfter(payload: Record<string, unknown>): unknown {
  return payload.stale_after_seconds ?? payload.staleAfter;
}

function telemetryCadence(payload: Record<string, unknown>): unknown {
  return payload.cadence_seconds ?? payload.cadence;
}

function freshnessAt(
  experiment: ExperimentProjection,
  currentFenceKey: string | null,
  now: number,
): Freshness {
  const execution = experiment.execution;
  const current =
    executionFenceKey(experiment) === currentFenceKey &&
    execution.fence_status === "current" &&
    ["admitted", "running"].includes(execution.status);
  if (!current) return "historical";
  const latest = telemetryEvent(experiment);
  if (!latest) return "connecting";
  const sampledAt = epochMilliseconds(telemetrySampleTime(latest.payload) ?? latest.observed_at);
  const staleAfter = telemetryStaleAfter(latest.payload);
  if (sampledAt === null || typeof staleAfter !== "number" || staleAfter <= 0) {
    return "stale";
  }
  return now - sampledAt > staleAfter * 1_000 ? "stale" : "live";
}

function useFreshness(
  experiment: ExperimentProjection,
  currentFenceKey: string | null,
): Freshness {
  const [clock, setClock] = useState(() => Date.now());
  const displayedFenceKey = executionFenceKey(experiment);
  const latest = telemetryEvent(experiment);
  const sample = epochMilliseconds(
    latest ? telemetrySampleTime(latest.payload) ?? latest.observed_at : null,
  );
  const staleAfter = latest ? telemetryStaleAfter(latest.payload) : null;

  useEffect(() => {
    setClock(Date.now());
    if (
      sample === null ||
      typeof staleAfter !== "number" ||
      staleAfter <= 0 ||
      experiment.execution.status !== "running" ||
      displayedFenceKey !== currentFenceKey
    ) {
      return;
    }
    const delay = Math.max(50, sample + staleAfter * 1_000 - Date.now() + 25);
    const timer = window.setTimeout(() => setClock(Date.now()), delay);
    return () => window.clearTimeout(timer);
  }, [currentFenceKey, displayedFenceKey, experiment.execution.status, sample, staleAfter]);

  return freshnessAt(experiment, currentFenceKey, clock);
}

function humanMetricLabel(key: string): string {
  return key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function metricPercent(value: number, unit: string, denominator: string): number | null {
  const normalized = unit.trim().toLowerCase();
  if (["%", "percent", "percentage"].includes(normalized)) {
    return Math.max(0, Math.min(100, value));
  }
  if (normalized === "ratio") return Math.max(0, Math.min(100, value * 100));
  const denominatorNumber = Number.parseFloat(denominator.replace(/[^0-9.+-]/g, ""));
  if (Number.isFinite(denominatorNumber) && denominatorNumber > 0) {
    return Math.max(0, Math.min(100, (value / denominatorNumber) * 100));
  }
  return null;
}

function telemetryMetrics(payload: Record<string, unknown>): TelemetryMetric[] {
  const metrics: TelemetryMetric[] = [];
  const nested = payload.measurements;
  const candidates = nested && typeof nested === "object" && !Array.isArray(nested)
    ? nested as Record<string, unknown>
    : payload;
  for (const [key, candidate] of Object.entries(candidates)) {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) continue;
    const value = (candidate as Record<string, unknown>).value;
    const unit = (candidate as Record<string, unknown>).unit;
    const denominator = (candidate as Record<string, unknown>).denominator;
    if (
      typeof value !== "number" ||
      !Number.isFinite(value) ||
      typeof unit !== "string" ||
      typeof denominator !== "string"
    ) {
      continue;
    }
    metrics.push({
      key,
      label: humanMetricLabel(key),
      value,
      displayValue: `${value.toLocaleString("en-US", { maximumFractionDigits: 2 })} ${unit}`,
      unit,
      denominator,
      percent: metricPercent(value, unit, denominator),
    });
  }

  const total = payload.memory_total_kib;
  const available = payload.memory_available_kib;
  if (
    typeof total === "number" &&
    Number.isFinite(total) &&
    total > 0 &&
    typeof available === "number" &&
    Number.isFinite(available)
  ) {
    const used = Math.max(0, total - available);
    metrics.push({
      key: "host_memory_used",
      label: "Host Memory Used",
      value: used,
      displayValue: `${(used / 1_048_576).toFixed(1)} GiB`,
      unit: "KiB",
      denominator: `${total} KiB host total memory`,
      percent: Math.max(0, Math.min(100, (used / total) * 100)),
    });
  }
  if (typeof payload.load_1m === "number" && Number.isFinite(payload.load_1m)) {
    metrics.push({
      key: "host_load_1m",
      label: "Host Load 1m",
      value: payload.load_1m,
      displayValue: payload.load_1m.toFixed(2),
      unit: "load average",
      denominator: "host scheduler runnable queue",
      percent: null,
    });
  }
  return metrics;
}

function observationCopy(
  execution: ExperimentExecutionProjection,
  stdout: ExperimentObservation[],
): { mode: string; completeness: string; truncation: string; dropped: string; time: string; sequence: string } {
  const observation = execution.stdout_observation;
  const sequences = stdout.map((event) => event.sequence);
  const first = observation?.first_sequence ?? (sequences.length ? Math.min(...sequences) : null);
  const last = observation?.last_sequence ?? (sequences.length ? Math.max(...sequences) : null);
  const complete = observation?.complete ?? (
    ["executed", "failed", "retired"].includes(execution.status) ? true : false
  );
  return {
    mode: observation?.mode ?? "projection snapshot",
    completeness: complete ? "complete" : "incomplete",
    truncation: observation?.truncated === undefined
      ? "truncation unknown"
      : observation.truncated ? "truncated" : "not truncated",
    dropped: observation?.dropped === undefined
      ? "dropped unknown"
      : `dropped ${observation.dropped}`,
    time: formattedTime(observation?.observed_at ?? stdout.at(-1)?.observed_at),
    sequence: first === null || last === null ? "seq —" : `seq ${first}–${last}`,
  };
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(
    "button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex='0']",
  )).filter((element) => element.offsetParent !== null);
}

function CurrentExperimentIdentity({ experiment }: { experiment: ExperimentProjection }) {
  const execution = experiment.execution;
  return (
    <>
      Run {execution.run_ref ?? "unavailable"} · G{execution.attempt_generation ?? "?"} · root Session {execution.root_session_ref ?? "unavailable"}
    </>
  );
}

function experimentLayerStatus(layer: Record<string, unknown> | undefined): string {
  return typeof layer?.status === "string" ? layer.status : "not_reported";
}

export function CurrentExperimentSummary({
  experiment,
  onOpen,
}: {
  experiment: ExperimentProjection;
  onOpen: (trigger: HTMLElement) => void;
}) {
  const execution = experiment.execution;
  return (
    <div className="lumen-current-experiment" data-testid="current-experiment-card">
      <div className="lumen-current-experiment-label">
        <i aria-hidden="true" />当前实验 · {execution.status}
      </div>
      <b>{experiment.intent.title ?? "当前 Formal Measurement 实验"}</b>
      <small>
        EvaluationAttempt {experiment.identities.evaluation_attempt_ref ?? "unavailable"} · <CurrentExperimentIdentity experiment={experiment} />
      </small>
      <div
        className="lumen-experiment-layer-status"
        data-testid="experiment-layer-status"
        aria-label="当前实验的执行、资产与 Formal Measurement 分层状态"
      >
        <span>execution <b>{execution.status}</b></span>
        <span>asset <b>{experimentLayerStatus(experiment.assets)}</b></span>
        <span>Formal Measurement <b>{experimentLayerStatus(experiment.formal_measurement)}</b></span>
      </div>
      <button
        type="button"
        data-execution-observer-entry="overview"
        onClick={(event) => onOpen(event.currentTarget)}
      >
        打开 stdout 与硬件利用 ↗
      </button>
    </div>
  );
}

export function ExperimentToolbarEntry({
  experiment,
  onOpen,
}: {
  experiment: ExperimentProjection;
  onOpen: (trigger: HTMLElement) => void;
}) {
  return (
    <button
      className="lumen-experiment-toolbar-entry"
      type="button"
      data-execution-observer-entry="question-toolbar"
      data-execution-status={experiment.execution.status}
      onClick={(event) => onOpen(event.currentTarget)}
    >
      当前实验 · stdout
    </button>
  );
}

export function ExecutionObserver({
  controller,
}: {
  controller: ExecutionObserverController;
}) {
  const { displayed: experiment, isOpen } = controller;
  const dialogRef = useRef<HTMLElement>(null);
  const terminalRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [followLatest, setFollowLatest] = useState(true);

  useEffect(() => {
    const shell = document.querySelector<HTMLElement>(".lumen-shell");
    if (!isOpen) return;
    document.body.classList.add("execution-observer-open");
    if (shell) shell.inert = true;
    let focusPending = true;
    let secondFrame: number | null = null;
    const ensureDialogFocus = () => {
      if (
        focusPending &&
        !dialogRef.current?.contains(document.activeElement)
      ) {
        closeRef.current?.focus({ preventScroll: true });
      }
    };
    ensureDialogFocus();
    const zeroTimer = window.setTimeout(ensureDialogFocus, 0);
    const firstFrame = window.requestAnimationFrame(() => {
      ensureDialogFocus();
      secondFrame = window.requestAnimationFrame(ensureDialogFocus);
    });
    const settleTimer = window.setTimeout(ensureDialogFocus, 320);
    return () => {
      focusPending = false;
      window.clearTimeout(zeroTimer);
      window.clearTimeout(settleTimer);
      window.cancelAnimationFrame(firstFrame);
      if (secondFrame !== null) window.cancelAnimationFrame(secondFrame);
      document.body.classList.remove("execution-observer-open");
      if (shell) shell.inert = false;
    };
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      controller.close();
    };
    window.addEventListener("keydown", closeOnEscape, { capture: true });
    return () => window.removeEventListener("keydown", closeOnEscape, { capture: true });
  }, [controller.close, isOpen]);

  const attemptEvents = useMemo(
    () => experiment ? currentAttemptEvents(experiment) : [],
    [experiment],
  );
  const stdout = useMemo(
    () => attemptEvents.filter((event) => event.kind === "stdout"),
    [attemptEvents],
  );
  const latestTelemetry = useMemo(
    () => attemptEvents.filter((event) => event.kind === "telemetry").at(-1) ?? null,
    [attemptEvents],
  );
  const metrics = useMemo(
    () => telemetryMetrics(latestTelemetry?.payload ?? {}),
    [latestTelemetry],
  );
  const freshness = useFreshness(
    experiment ?? {
      intent: {},
      identities: {},
      execution: { status: "not_attempted" },
    },
    executionFenceKey(controller.current),
  );
  const stdoutMeta = experiment
    ? observationCopy(experiment.execution, stdout)
    : null;
  const hasGpuTelemetry = Boolean(
    latestTelemetry && (
      /(?:gpu|cuda|nvidia)/i.test(String(latestTelemetry.payload.device ?? "")) ||
      metrics.some((metric) => /(?:gpu|vram|power)/i.test(metric.key))
    ),
  );

  useEffect(() => {
    if (!isOpen || !followLatest) return;
    const terminal = terminalRef.current;
    if (!terminal) return;
    window.requestAnimationFrame(() => {
      terminal.scrollTop = terminal.scrollHeight;
    });
  }, [followLatest, isOpen, stdout.length, stdout.at(-1)?.sequence]);

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLElement>) => {
    if (event.key === "Escape") {
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = focusableElements(dialogRef.current);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable.at(-1)!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <>
      {controller.noticeVisible && controller.current ? (
        <aside
          className="execution-start-notice"
          aria-label="新实验运行提示"
          data-testid="execution-start-notice"
        >
          <span role="status" aria-live="polite">
            <i aria-hidden="true" />
            <b>新 Execution Attempt 已开始</b>
            <small>{executionFenceKey(controller.current)}</small>
          </span>
          <button type="button" onClick={(event) => controller.open(event.currentTarget)}>
            打开 stdout
          </button>
          <button
            className="execution-notice-close"
            type="button"
            aria-label="关闭提示"
            onClick={controller.dismissNotice}
          >
            ×
          </button>
        </aside>
      ) : null}

      <div
        className="execution-observer-backdrop"
        data-open={isOpen}
        aria-hidden="true"
        onClick={controller.close}
      />
      <section
        ref={dialogRef as RefObject<HTMLElement>}
        className="execution-observer"
        data-open={isOpen}
        data-freshness={freshness}
        data-testid="execution-observer"
        role="dialog"
        aria-modal="true"
        aria-hidden={!isOpen}
        aria-labelledby="execution-observer-title"
        aria-describedby="execution-observer-boundary execution-observer-identity"
        onKeyDown={handleKeyDown}
      >
        {isOpen && experiment && stdoutMeta ? (
          <>
            <header className="execution-observer-head">
              <span className="execution-live-mark" data-status={freshness} aria-hidden="true" />
              <div className="execution-observer-head-copy">
                <small>{freshnessCopy(freshness)}</small>
                <h2 id="execution-observer-title">
                  {experiment.intent.title ?? "当前 Formal Measurement 实验"}
                </h2>
                <p>
                  EvaluationAttempt {experiment.identities.evaluation_attempt_ref ?? "unavailable"} ｜ <CurrentExperimentIdentity experiment={experiment} />
                </p>
              </div>
              <div className="execution-observer-actions">
                <button
                  type="button"
                  aria-pressed={followLatest}
                  onClick={() => setFollowLatest((current) => !current)}
                >
                  跟随最新
                </button>
                <button
                  ref={closeRef}
                  className="execution-observer-close"
                  type="button"
                  autoFocus
                  aria-label="关闭当前实验观测窗"
                  onClick={controller.close}
                >
                  ×
                </button>
              </div>
            </header>

            <div className="execution-observer-body">
              <div className="execution-stdout-pane">
                <div className="execution-stdout-toolbar">
                  <span>RAW STDOUT · RUNTIME PROJECTION</span>
                  <b>{executionFenceKey(experiment)}</b>
                  <span data-testid="stdout-observation-mode">
                    {stdoutMeta.mode} · {stdoutMeta.completeness} · {stdoutMeta.truncation} · {stdoutMeta.dropped} · {stdoutMeta.time}
                  </span>
                  <span className="execution-stdout-seq">{stdoutMeta.sequence}</span>
                </div>
                <div
                  ref={terminalRef}
                  className="execution-stdout-terminal"
                  tabIndex={0}
                  role="log"
                  aria-live="off"
                  aria-label="当前实验原始标准输出，可用方向键或 Page Up、Page Down 滚动"
                >
                  <pre>
                    {stdout.length
                      ? stdout.map((event) => stdoutLine(event)).join("\n")
                      : "[connecting] current Attempt 尚未投影 stdout。"}
                  </pre>
                  {freshness === "live" ? <span className="execution-stdout-caret" aria-hidden="true" /> : null}
                </div>
                <div className="execution-stdout-note" id="execution-observer-boundary">
                  <b>执行观察，不是研究结论。</b> stdout 与硬件遥测不代表 Formal Measurement 已接纳、Question 已回答或 Stage 已推进；token、cookie、password 等秘密禁止进入源日志。
                  <span data-testid="experiment-observer-layer-status">
                    当前 Projection：execution {experiment.execution.status} / asset {experimentLayerStatus(experiment.assets)} / Formal Measurement {experimentLayerStatus(experiment.formal_measurement)}
                  </span>
                </div>
              </div>

              <aside className="execution-telemetry-pane" aria-label="实时硬件利用">
                <div className="execution-telemetry-head">
                  <b>硬件利用</b>
                  <span data-freshness={freshness}>{freshnessBadge(freshness, latestTelemetry)}</span>
                </div>
                {latestTelemetry ? (
                  <>
                    <div className="execution-device-card">
                      <small>Observed device / host</small>
                      <b>{String(latestTelemetry.payload.device ?? "device not reported")}</b>
                    </div>
                    {!hasGpuTelemetry ? (
                      <div className="execution-no-gpu" data-testid="gpu-telemetry-unavailable">
                        GPU telemetry not provided · 不显示推测的 GPU / VRAM / power
                      </div>
                    ) : null}
                    <div className="execution-metric-grid">
                      {metrics.length ? metrics.map((metric) => (
                        <article className="execution-metric" key={metric.key}>
                          <div>
                            <small>{metric.label}</small>
                            <b>{metric.displayValue}</b>
                          </div>
                          {metric.percent === null ? null : (
                            <span className="execution-metric-track" aria-hidden="true">
                              <i style={{ "--execution-metric": `${metric.percent}%` } as CSSProperties} />
                            </span>
                          )}
                          <p>unit: {metric.unit}<br />denominator: {metric.denominator}</p>
                        </article>
                      )) : (
                        <p className="execution-no-metrics">Collector 尚未投影带 unit / denominator 的数值指标。</p>
                      )}
                    </div>
                    <dl className="execution-telemetry-source">
                      <TelemetryDetail label="collector" value={latestTelemetry.payload.collector} />
                      <TelemetryDetail label="device" value={latestTelemetry.payload.device} />
                      <TelemetryDetail label="scope" value={latestTelemetry.payload.scope} />
                      <TelemetryDetail label="correlation" value={latestTelemetry.payload.correlation} />
                      <TelemetryDetail label="cadence" value={secondsCopy(telemetryCadence(latestTelemetry.payload))} />
                      <TelemetryDetail label="stale after" value={secondsCopy(telemetryStaleAfter(latestTelemetry.payload))} />
                      <TelemetryDetail label="sampled at" value={formattedTime(telemetrySampleTime(latestTelemetry.payload) ?? latestTelemetry.observed_at)} />
                    </dl>
                  </>
                ) : (
                  <div className="execution-telemetry-connecting" role="status">
                    当前 Execution Fence 尚未投影遥测样本；不会补造设备利用率。
                  </div>
                )}
                <div className="execution-telemetry-boundary" id="execution-observer-identity">
                  <b>身份保持分离</b><br />
                  实验：EvaluationAttempt {experiment.identities.evaluation_attempt_ref ?? "unavailable"} · RG<br />
                  执行：<CurrentExperimentIdentity experiment={experiment} /> · AR<br />
                  live stream 不是 RM AssetVersion / RG LogAsset
                </div>
              </aside>
            </div>
          </>
        ) : null}
      </section>
    </>
  );
}

function TelemetryDetail({ label, value }: { label: string; value: unknown }) {
  const visible = value === null || value === undefined || value === "" ? "not reported" : String(value);
  return <div><dt>{label}</dt><dd>{visible}</dd></div>;
}

function secondsCopy(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${value}s`
    : "not fixed / not reported";
}

function stdoutLine(event: ExperimentObservation): string {
  const line = typeof event.payload.line === "string"
    ? event.payload.line
    : JSON.stringify(event.payload);
  const stream = typeof event.payload.stream === "string" ? event.payload.stream : "stdout";
  return `[seq ${event.sequence} · ${formattedTime(event.observed_at)} · ${stream}] ${line}`;
}

function freshnessCopy(freshness: Freshness): string {
  return {
    connecting: "current execution fence · connecting",
    live: "current execution fence · live",
    stale: "current execution fence · telemetry stale",
    historical: "historical output · execution fence no longer current",
  }[freshness];
}

function freshnessBadge(
  freshness: Freshness,
  latest: ExperimentObservation | null,
): string {
  if (freshness === "live") {
    return `LIVE · ${formattedTime(latest ? telemetrySampleTime(latest.payload) ?? latest.observed_at : null)}`;
  }
  if (freshness === "stale") {
    return `STALE · ${formattedTime(latest ? telemetrySampleTime(latest.payload) ?? latest.observed_at : null)}`;
  }
  return freshness.toUpperCase();
}
