import { useState } from "react";

import {
  ProductError,
  startExperiment,
  type ExperimentProjection,
  type ExperimentStartRequest,
} from "./api";

type ExperimentDraft = Pick<
  ExperimentStartRequest,
  "title" | "hypothesis" | "variant_parameter" | "sample_count"
  | "wall_time_budget_seconds"
>;

const INITIAL_DRAFT: ExperimentDraft = {
  title: "",
  hypothesis: "",
  variant_parameter: -0.25,
  sample_count: 16,
  wall_time_budget_seconds: 300,
};

type RequestKind = ExperimentStartRequest["request_kind"];

type SourceCheckpoint = {
  roleRef: string;
  label: string;
};

function projectedString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function projectedFiniteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function sourceVariantRunRef(
  experiment: ExperimentProjection | null,
): string | null {
  return projectedString(experiment?.identities.variant_run_ref);
}

function sourceCheckpoints(
  experiment: ExperimentProjection | null,
  variantRunRef: string | null,
): SourceCheckpoint[] {
  const projected = experiment?.assets?.checkpoint_artifacts;
  if (!Array.isArray(projected) || !variantRunRef) return [];
  const seen = new Set<string>();
  return projected.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const role = item as Record<string, unknown>;
    const roleRef = projectedString(role.role_ref);
    const subjectRef = projectedString(role.subject_ref);
    if (
      !roleRef ||
      seen.has(roleRef) ||
      (subjectRef !== null && subjectRef !== variantRunRef)
    ) {
      return [];
    }
    seen.add(roleRef);
    return [{
      roleRef,
      label: projectedString(role.display_name) ?? roleRef,
    }];
  });
}

export function ExperimentLauncher({
  questRef,
  sourceExperiment = null,
  onStarted,
}: {
  questRef: string;
  sourceExperiment?: ExperimentProjection | null;
  onStarted: (experiment: ExperimentProjection) => void;
}) {
  const [draft, setDraft] = useState<ExperimentDraft>(INITIAL_DRAFT);
  const [requestKind, setRequestKind] = useState<RequestKind>("retrain");
  const [selectedCheckpointRoleRefs, setSelectedCheckpointRoleRefs] = useState<string[]>([]);
  const [executionRequestRef, setExecutionRequestRef] = useState(
    () => crypto.randomUUID(),
  );
  const [submitting, setSubmitting] = useState(false);
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sourceVariantRef = sourceVariantRunRef(sourceExperiment);
  const sourceVariantParameter = projectedFiniteNumber(
    sourceExperiment?.intent.variant_parameter,
  );
  const projectedSourceSampleCount = projectedFiniteNumber(
    sourceExperiment?.intent.sample_count,
  );
  const sourceSampleCount = projectedSourceSampleCount !== null &&
    Number.isInteger(projectedSourceSampleCount) &&
    projectedSourceSampleCount >= 4 &&
    projectedSourceSampleCount <= 4_096
    ? projectedSourceSampleCount
    : null;
  const checkpoints = sourceCheckpoints(sourceExperiment, sourceVariantRef);
  const remeasureAvailable =
    sourceVariantRef !== null &&
    sourceVariantParameter !== null &&
    sourceSampleCount !== null;
  const isNextExperiment = sourceExperiment !== null;
  const executionIsActive = Boolean(
    sourceExperiment &&
    ["admitted", "running"].includes(sourceExperiment.execution.status),
  );

  const reviseIntentIdentity = () => {
    setExecutionRequestRef(crypto.randomUUID());
    setError(null);
  };

  const revise = <Key extends keyof ExperimentDraft>(
    key: Key,
    value: ExperimentDraft[Key],
  ) => {
    setDraft((current) => ({ ...current, [key]: value }));
    reviseIntentIdentity();
  };

  const chooseRequestKind = (next: RequestKind) => {
    setRequestKind(next);
    if (next === "retrain") {
      setSelectedCheckpointRoleRefs([]);
    } else if (sourceVariantParameter !== null && sourceSampleCount !== null) {
      setDraft((current) => ({
        ...current,
        variant_parameter: sourceVariantParameter,
        sample_count: sourceSampleCount,
      }));
    }
    reviseIntentIdentity();
  };

  const addCheckpoint = (roleRef: string) => {
    setSelectedCheckpointRoleRefs((current) => (
      current.includes(roleRef) ? current : [...current, roleRef]
    ));
    reviseIntentIdentity();
  };

  const removeCheckpoint = (roleRef: string) => {
    setSelectedCheckpointRoleRefs((current) => (
      current.filter((currentRef) => currentRef !== roleRef)
    ));
    reviseIntentIdentity();
  };

  const moveCheckpoint = (index: number, direction: -1 | 1) => {
    setSelectedCheckpointRoleRefs((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.length) return current;
      const next = [...current];
      [next[index], next[target]] = [next[target], next[index]];
      return next;
    });
    reviseIntentIdentity();
  };

  const valid =
    draft.title.trim().length > 0 &&
    draft.hypothesis.trim().length > 0 &&
    Number.isFinite(draft.variant_parameter) &&
    Number.isInteger(draft.sample_count) &&
    draft.sample_count >= 4 &&
    draft.sample_count <= 4_096 &&
    Number.isFinite(draft.wall_time_budget_seconds) &&
    draft.wall_time_budget_seconds >= 1 &&
    draft.wall_time_budget_seconds <= 86_400 &&
    (requestKind === "retrain" || remeasureAvailable);

  if (executionIsActive) {
    return (
      <section
        className="lumen-experiment-launcher"
        aria-labelledby="experiment-launcher-title"
        data-testid="experiment-launcher"
        data-experiment-admission="blocked-active-execution"
      >
        <header>
          <div>
            <small>NEXT EXPERIMENT INTENT · HELD</small>
            <b id="experiment-launcher-title">当前 Execution Attempt 尚未结束</b>
          </div>
          <code>Quest {questRef}</code>
        </header>
        <div className="lumen-experiment-active-blocker" role="status">
          <b>下一次实验 intent 暂不可提交</b>
          <span>
            execution {sourceExperiment!.execution.status} · 保留当前 Run、Attempt、Session 与 Fence 的可见性；terminal Projection 返回后再开放 retrain／remeasure。
          </span>
        </div>
      </section>
    );
  }

  const submit = async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const baseIntent = {
        execution_request_ref: executionRequestRef,
        quest_ref: questRef,
        title: draft.title.trim(),
        hypothesis: draft.hypothesis.trim(),
        variant_parameter: draft.variant_parameter,
        sample_count: draft.sample_count,
        wall_time_budget_seconds: draft.wall_time_budget_seconds,
      };
      const experiment = await startExperiment(requestKind === "retrain"
        ? { ...baseIntent, request_kind: "retrain" }
        : {
            ...baseIntent,
            request_kind: "remeasure",
            source_variant_run_ref: sourceVariantRef!,
            selected_checkpoint_role_refs: selectedCheckpointRoleRefs,
          });
      setSubmitted(true);
      onStarted(experiment);
    } catch (caught) {
      setError(
        caught instanceof ProductError
          ? caught.code
          : "experiment_start_failed",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section
      className="lumen-experiment-launcher"
      aria-labelledby="experiment-launcher-title"
      data-testid="experiment-launcher"
      data-experiment-admission="available"
    >
      <header>
        <div>
          <small>{isNextExperiment ? "NEXT EXPERIMENT INTENT · WEB" : "EXPERIMENT INTENT · WEB"}</small>
          <b id="experiment-launcher-title">
            {isNextExperiment ? "启动下一次实验" : "启动微型真实实验"}
          </b>
        </div>
        <code>Quest {questRef}</code>
      </header>
      <p>
        {isNextExperiment ? "下一次实验 intent 与当前实验观察彼此独立。" : null}
        浏览器只提交 title、hypothesis、variant 与 sample intent，并显式标记 retrain／remeasure；定义资产、执行身份、结果资产和 Formal Measurement 仍由 RM／RG／AR 分层接纳。
      </p>
      <details>
        <summary>填写实验意图</summary>
        <div className="lumen-experiment-fields">
          <label className="wide">
            <span>实验类型</span>
            <select
              value={requestKind}
              disabled={submitting || submitted}
              onChange={(event) => chooseRequestKind(event.target.value as RequestKind)}
            >
              <option value="retrain">retrain · 形成新的 VariantRun</option>
              <option value="remeasure" disabled={!remeasureAvailable}>
                remeasure · 测量已投影的 VariantRun
              </option>
            </select>
          </label>
          <label>
            <span>标题</span>
            <input
              required
              value={draft.title}
              disabled={submitting || submitted}
              placeholder="例如：固定样本偏移微实验"
              onChange={(event) => revise("title", event.target.value)}
            />
          </label>
          <label className="wide">
            <span>假设</span>
            <textarea
              required
              rows={3}
              value={draft.hypothesis}
              disabled={submitting || submitted}
              placeholder="描述可被这个微实验测量的预期；结果符号不决定是否可接纳。"
              onChange={(event) => revise("hypothesis", event.target.value)}
            />
          </label>
          <label>
            <span>Variant parameter</span>
            <input
              required
              type="number"
              step="0.01"
              value={draft.variant_parameter}
              disabled={submitting || submitted || requestKind === "remeasure"}
              onChange={(event) => revise("variant_parameter", event.target.valueAsNumber)}
            />
          </label>
          <label>
            <span>Sample count</span>
            <input
              required
              type="number"
              min={4}
              max={4_096}
              step={1}
              value={draft.sample_count}
              disabled={submitting || submitted || requestKind === "remeasure"}
              onChange={(event) => revise("sample_count", event.target.valueAsNumber)}
            />
          </label>
          <label>
            <span>科学运行预算（秒）</span>
            <input
              required
              type="number"
              min={1}
              max={86_400}
              step={1}
              value={draft.wall_time_budget_seconds}
              disabled={submitting || submitted}
              onChange={(event) => revise(
                "wall_time_budget_seconds",
                event.target.valueAsNumber,
              )}
            />
            <small>这是本次实验的显式测量预算，不是 Agent Session 上限。</small>
          </label>
        </div>
        {requestKind === "remeasure" && sourceVariantRef ? (
          <section className="lumen-remeasure-source" aria-label="remeasure source selection">
            <header>
              <small>SOURCE VARIANT RUN · PUBLIC PROJECTION</small>
              <code data-testid="remeasure-source-variant">{sourceVariantRef}</code>
            </header>
            <p>
              variant {sourceVariantParameter} / sample {sourceSampleCount} 从 source recipe 冻结；checkpoint 只来自这个 source VariantRun 的公开 checkpoint_artifacts。选择 0 个有效，已选数组按下列顺序提交。
            </p>
            <div className="lumen-remeasure-checkpoints">
              <div>
                <small>可用 checkpoint_artifacts</small>
                {checkpoints.length ? (
                  <ul>
                    {checkpoints.map((checkpoint) => {
                      const selected = selectedCheckpointRoleRefs.includes(checkpoint.roleRef);
                      return (
                        <li key={checkpoint.roleRef}>
                          <span><b>{checkpoint.label}</b><code>{checkpoint.roleRef}</code></span>
                          <button
                            type="button"
                            disabled={selected || submitting || submitted}
                            aria-label={`加入 checkpoint ${checkpoint.roleRef}`}
                            onClick={() => addCheckpoint(checkpoint.roleRef)}
                          >
                            {selected ? "已加入" : "加入"}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p>这个 source VariantRun 没有投影 checkpoint_artifacts；将提交有序空数组。</p>
                )}
              </div>
              <div>
                <small>提交顺序 · {selectedCheckpointRoleRefs.length}</small>
                {selectedCheckpointRoleRefs.length ? (
                  <ol data-testid="selected-checkpoint-order">
                    {selectedCheckpointRoleRefs.map((roleRef, index) => (
                      <li key={roleRef}>
                        <code>{roleRef}</code>
                        <span>
                          <button
                            type="button"
                            disabled={index === 0 || submitting || submitted}
                            aria-label={`上移 checkpoint ${roleRef}`}
                            onClick={() => moveCheckpoint(index, -1)}
                          >↑</button>
                          <button
                            type="button"
                            disabled={index === selectedCheckpointRoleRefs.length - 1 || submitting || submitted}
                            aria-label={`下移 checkpoint ${roleRef}`}
                            onClick={() => moveCheckpoint(index, 1)}
                          >↓</button>
                          <button
                            type="button"
                            disabled={submitting || submitted}
                            aria-label={`移除 checkpoint ${roleRef}`}
                            onClick={() => removeCheckpoint(roleRef)}
                          >×</button>
                        </span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <p data-testid="selected-checkpoint-order">0 个 checkpoint · ordered []</p>
                )}
              </div>
            </div>
          </section>
        ) : null}
        <div className="lumen-experiment-submit-row">
          <small>execution_request_ref · {executionRequestRef}</small>
          <button
            type="button"
            disabled={!valid || submitting || submitted}
            onClick={() => void submit()}
          >
            {submitted ? "已提交，等待 Projection…" : submitting ? "正在提交 intent…" : "启动实验"}
          </button>
        </div>
        {error ? (
          <div className="lumen-experiment-error" role="alert">
            未能确认实验已启动 · <code>{error}</code>。原 intent 与 request ref 已保留，可安全重试。
          </div>
        ) : null}
      </details>
    </section>
  );
}
