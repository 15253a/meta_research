import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  compareWritingVersions,
  confirmWritingCancellation,
  confirmWritingIntent,
  controlWritingRun,
  createWritingIntent,
  fetchWritingVersionContent,
  previewWritingIntent,
  previewWritingCancellation,
  ProductError,
  reviseWritingRun,
  writingRenderUrl,
  type WritingComparison,
  type WritingCancellationPreview,
  type WritingOverview,
  type WritingVersionContent,
  type WritingReportView,
} from "./api";
import "./writing-report.css";

export function WritingReportWorkbench({
  initial,
  questRef,
  onClose,
  onChanged,
}: {
  initial: WritingOverview;
  questRef: string | null;
  onClose: () => void;
  onChanged: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const [overview, setOverview] = useState(initial);
  const [selectedRef, setSelectedRef] = useState(
    initial.runs[0]?.run?.run_ref ?? initial.runs[0]?.intent_id ?? null,
  );
  const [pending, setPending] = useState<WritingReportView | null>(
    initial.runs.find((item) => !item.run) ?? null,
  );
  const [cancellation, setCancellation] =
    useState<WritingCancellationPreview | null>(null);
  const [title, setTitle] = useState("阶段性研究报告");
  const [audience, setAudience] = useState("研究负责人");
  const [purpose, setPurpose] = useState("复核当前证据、结论与未知边界");
  const [instructions, setInstructions] = useState("突出可证伪结论与局限。");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [comparison, setComparison] = useState<WritingComparison | null>(null);
  const [leftVersion, setLeftVersion] = useState("");
  const [rightVersion, setRightVersion] = useState("");
  const [rendered, setRendered] = useState<
    (WritingVersionContent & { run_ref: string }) | null
  >(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    dialog.showModal();
    requestAnimationFrame(() => closeRef.current?.focus());
    return () => dialog.close();
  }, []);

  useEffect(() => {
    setOverview(initial);
    setSelectedRef((current) => {
      if (current && initial.runs.some(
        (item) => (item.run?.run_ref ?? item.intent_id) === current,
      )) {
        return current;
      }
      return initial.runs[0]?.run?.run_ref ?? initial.runs[0]?.intent_id ?? current;
    });
    setPending((current) => {
      if (current) {
        return initial.runs.find((item) => item.intent_id === current.intent_id) ?? current;
      }
      return initial.runs.find((item) => !item.run) ?? null;
    });
  }, [initial]);

  const selected = useMemo(
    () => overview.runs.find(
      (item) => (item.run?.run_ref ?? item.intent_id) === selectedRef,
    ) ?? null,
    [overview.runs, selectedRef],
  );
  const versions = selected?.versions ?? [];

  useEffect(() => {
    if (versions.length < 2) {
      setLeftVersion(versions[0]?.version_ref ?? "");
      setRightVersion(versions[0]?.version_ref ?? "");
      setComparison(null);
      return;
    }
    if (!versions.some((item) => item.version_ref === leftVersion)) {
      setLeftVersion(versions[0].version_ref);
    }
    if (!versions.some((item) => item.version_ref === rightVersion)) {
      setRightVersion(versions.at(-1)?.version_ref ?? versions[0].version_ref);
    }
  }, [leftVersion, rightVersion, versions]);

  useEffect(() => {
    setComparison(null);
  }, [selectedRef, leftVersion, rightVersion]);

  const activeComparison = (
    comparison
    && selected?.run?.run_ref === comparison.run_ref
    && comparison.left_version_ref === leftVersion
    && comparison.right_version_ref === rightVersion
  ) ? comparison : null;

  const replaceReport = (next: WritingReportView) => {
    if (!next.run) {
      setPending(next);
      setSelectedRef(next.intent_id);
      setOverview((current) => ({
        ...current,
        runs: [
          next,
          ...current.runs.filter((item) => item.intent_id !== next.intent_id),
        ],
      }));
      return;
    }
    setOverview((current) => ({
      ...current,
      runs: [
        next,
        ...current.runs.filter(
          (item) => item.intent_id !== next.intent_id
            && item.run?.run_ref !== next.run?.run_ref,
        ),
      ],
    }));
    setPending(null);
    setSelectedRef(next.run.run_ref);
  };

  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (!questRef || busy) return;
    setBusy("create");
    setError(null);
    try {
      const drafted = await createWritingIntent({
        quest_ref: questRef,
        title,
        audience,
        purpose,
        instructions,
      });
      replaceReport(drafted);
      onChanged();
      replaceReport(await previewWritingIntent(drafted.intent_id));
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const retryPreview = async () => {
    if (!pending || busy) return;
    setBusy("preview");
    setError(null);
    try {
      replaceReport(await previewWritingIntent(pending.intent_id));
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const confirm = async () => {
    if (!pending || busy) return;
    setBusy("confirm");
    setError(null);
    try {
      const next = await confirmWritingIntent(pending);
      replaceReport(next);
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const control = async (action: "pause" | "resume") => {
    if (!selected?.run || busy) return;
    setBusy(action);
    setError(null);
    try {
      replaceReport(
        await controlWritingRun(
          selected.run.run_ref,
          action,
          selected.run.attempt_ref,
          selected.run.fence_ref,
        ),
      );
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const previewCancel = async () => {
    if (!selected?.run || busy) return;
    setBusy("cancel-preview");
    setError(null);
    try {
      setCancellation(await previewWritingCancellation(selected.run.run_ref));
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const confirmCancel = async () => {
    if (!selected?.run || !cancellation || busy) return;
    setBusy("cancel-confirm");
    setError(null);
    try {
      replaceReport(
        await confirmWritingCancellation(selected.run.run_ref, cancellation),
      );
      setCancellation(null);
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const revise = async () => {
    if (!selected?.run || !feedback.trim() || busy) return;
    setBusy("revise");
    setError(null);
    try {
      replaceReport(
        await reviseWritingRun(selected.run.run_ref, [feedback.trim()]),
      );
      setFeedback("");
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const compare = async () => {
    if (!selected?.run || !leftVersion || !rightVersion || busy) return;
    setBusy("compare");
    setError(null);
    try {
      setComparison(
        await compareWritingVersions(
          selected.run.run_ref,
          leftVersion,
          rightVersion,
        ),
      );
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const viewVersion = async (versionRef: string) => {
    if (!selected?.run || busy) return;
    setBusy("render");
    setError(null);
    try {
      setRendered({
        ...(await fetchWritingVersionContent(selected.run.run_ref, versionRef)),
        run_ref: selected.run.run_ref,
      });
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      className="writing-dialog"
      data-testid="writing-workbench"
      aria-labelledby="writing-report-title"
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === dialogRef.current) onClose();
      }}
    >
      <div className="writing-window">
        <header className="writing-header">
          <span className="writing-symbol" aria-hidden="true">WR</span>
          <div>
            <small>WRITING / REPORT</small>
            <h2 id="writing-report-title">Writing report 核心闭环</h2>
            <p>独立 Session · RM 交付物 · RG 引用 · 确定性渲染</p>
          </div>
          <span className="writing-head-status">{overview.status}</span>
          <button
            ref={closeRef}
            className="writing-close"
            type="button"
            aria-label="关闭 Writing"
            onClick={onClose}
          >
            ×
          </button>
        </header>

        <div className="writing-body">
          <aside className="writing-compose" aria-label="创建 report Writing Run">
            <small>01 · INTENT</small>
            <h3>冻结一次精确请求</h3>
            {questRef ? (
              <form onSubmit={create}>
                <fieldset disabled={busy !== null}>
                <label>标题<input value={title} onChange={(event) => setTitle(event.target.value)} required /></label>
                <label>受众<input value={audience} onChange={(event) => setAudience(event.target.value)} required /></label>
                <label>目的<textarea value={purpose} onChange={(event) => setPurpose(event.target.value)} required /></label>
                <label>写作说明<textarea value={instructions} onChange={(event) => setInstructions(event.target.value)} /></label>
                <button className="writing-primary" type="submit" disabled={busy !== null}>
                  {busy === "create"
                    ? "正在冻结…"
                    : pending
                      ? "以当前状态重新冻结新 Intent"
                      : "生成影响预览"}
                </button>
                </fieldset>
              </form>
            ) : (
              <p className="writing-empty">先确认一个 Quest，Writing 接口随后可在任意 Stage 使用。</p>
            )}
            {pending ? (
              <section className="writing-preview" data-testid="writing-intent-preview">
                <b>{pending.impact_preview ? "确认前预览" : "已冻结，等待影响预览"}</b>
                <dl className="writing-frozen-manifest">
                  <div><dt>Intent</dt><dd>{pending.intent.title} · {pending.intent.audience}</dd></div>
                  <div><dt>Purpose</dt><dd>{pending.intent.purpose}</dd></div>
                  <div><dt>Quest</dt><dd>{pending.snapshot.quest_ref}</dd></div>
                  <div><dt>Snapshot</dt><dd>{pending.snapshot.snapshot_ref} · {pending.snapshot.snapshot_hash}</dd></div>
                  <div><dt>Questions</dt><dd>{Array.isArray(pending.snapshot.questions) ? pending.snapshot.questions.length : 0}</dd></div>
                  <div><dt>Accepted sources</dt><dd>{pending.snapshot.accepted_sources.map((item) => item.version_ref).join(" · ") || "none"}</dd></div>
                  <div><dt>Owner cut</dt><dd>{JSON.stringify(pending.snapshot.owner_revisions ?? {})}</dd></div>
                  {pending.impact_preview?.target_assertion ? <div><dt>TargetAssertion</dt><dd>{JSON.stringify(pending.impact_preview.target_assertion)}</dd></div> : null}
                </dl>
                {pending.impact_preview ? <>
                <ul>{pending.impact_preview.will_happen.map((item) => <li key={item}>{item}</li>)}</ul>
                <ul className="will-not">{pending.impact_preview.will_not_happen.map((item) => <li key={item}>{item}</li>)}</ul>
                <button className="writing-primary" type="button" disabled={busy !== null} onClick={() => void confirm()}>
                  {busy === "confirm" ? "正在建立 Session…" : "确认 Intent 与 Snapshot"}
                </button>
                </> : <button className="writing-primary" type="button" disabled={busy !== null} onClick={() => void retryPreview()}>
                  {busy === "preview" ? "正在生成预览…" : "重新生成影响预览"}
                </button>}
                <p className="writing-recovery-note">
                  若此冻结 Snapshot 已 stale，可在上方重新冻结；旧 Intent 会保留在历史中。
                </p>
              </section>
            ) : null}
          </aside>

          <main className="writing-main">
            <div className="writing-run-tabs" aria-label="Writing Runs">
              {overview.runs.length ? overview.runs.map((item) => (
                <button
                  type="button"
                  className={(item.run?.run_ref ?? item.intent_id) === selectedRef ? "active" : ""}
                  aria-pressed={(item.run?.run_ref ?? item.intent_id) === selectedRef}
                  key={item.run?.run_ref ?? item.intent_id}
                  onClick={() => {
                    setSelectedRef(item.run?.run_ref ?? item.intent_id);
                    setPending(item.run ? null : item);
                    setCancellation(null);
                    setRendered(null);
                    setComparison(null);
                  }}
                >
                  <b>{item.intent.title}</b>
                  <small>{item.status} · r{item.run?.content_revision ?? 0}</small>
                </button>
              )) : <p className="writing-empty">尚无 Writing Run。左侧确认后，daemon 会独立运行。</p>}
            </div>

            {selected?.run ? (
              <article className="writing-run" data-testid="writing-run-detail">
                <div className="writing-run-title">
                  <div><small>RUN / {shortRef(selected.run.run_ref)}</small><h3>{selected.intent.title}</h3></div>
                  <div className="writing-controls">
                    {selected.status === "paused" ? (
                      <button type="button" onClick={() => void control("resume")} disabled={busy !== null}>继续</button>
                    ) : selected.status === "running" ? (
                      <button type="button" onClick={() => void control("pause")} disabled={busy !== null}>暂停</button>
                    ) : null}
                    {!['cancelled', 'completed'].includes(selected.status) ? (
                      <button type="button" onClick={() => void previewCancel()} disabled={busy !== null}>预览取消</button>
                    ) : null}
                  </div>
                </div>
                {cancellation?.impact_preview ? (
                  <section className="writing-cancel-preview" data-testid="writing-cancel-preview">
                    <b>终态操作确认</b>
                    <ul>{cancellation.impact_preview.will_happen.map((item) => <li key={item}>{item}</li>)}</ul>
                    <ul>{cancellation.impact_preview.risks.map((item) => <li key={item}>{item}</li>)}</ul>
                    <button type="button" disabled={busy !== null} onClick={() => void confirmCancel()}>
                      {busy === "cancel-confirm" ? "正在终止…" : "确认终止 Writing Run"}
                    </button>
                  </section>
                ) : null}
                <div className="writing-layers" aria-label="Writing 四层状态">
                  <Layer number="01" label="Execution" status={selected.execution.status} receipt={receiptRef(selected.execution.receipt)} />
                  <Layer number="02" label="Deliverable / RM" status={selected.deliverable.status} receipt={receiptRef(selected.deliverable.receipt)} />
                  <Layer number="03" label="Citation / RG" status={selected.citation.status} receipt={receiptRef(selected.citation.receipt)} />
                  <Layer number="04" label="Renderer" status={selected.renderer.status} receipt={selected.renderer.status === "ready" ? "deterministic" : null} />
                </div>
                <dl className="writing-identities">
                  <div><dt>root Session</dt><dd>{selected.run.root_session_ref}</dd></div>
                  <div><dt>native Session</dt><dd>{selected.run.native_session_ref ?? "pending"}</dd></div>
                  <div><dt>Attempt</dt><dd>a{selected.run.attempt_generation} · {selected.run.attempt_ref}</dd></div>
                  <div><dt>Fence</dt><dd>{selected.run.fence_ref}</dd></div>
                  <div><dt>Snapshot</dt><dd>{selected.snapshot.snapshot_hash}</dd></div>
                </dl>
                {selected.citation.status === "accepted" ? (
                  <div className="writing-feedback">
                    <label>反馈形成 successor revision<textarea value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder="写下必须修改的内容；历史版本不会被覆盖。" /></label>
                    <button type="button" disabled={!feedback.trim() || busy !== null} onClick={() => void revise()}>提交修订</button>
                    <a href={writingRenderUrl(selected.run.run_ref)} download>下载确定性 Markdown</a>
                  </div>
                ) : null}
                {selected.citation.status === "rejected" ? (
                  <p className="writing-rejection">RG feedback：{(selected.citation.feedback ?? []).join(" · ")}</p>
                ) : null}
                {rendered?.run_ref === selected.run.run_ref ? (
                  <section className="writing-viewer" aria-label="Writing 报告正文" data-testid="writing-report-viewer">
                    <header>
                      <div>
                        <b>RM deliverable · citation {rendered.citation_status}</b>
                        <code>{shortRef(rendered.version_ref)} · {shortRef(rendered.content_hash)}</code>
                      </div>
                      <button type="button" onClick={() => setRendered(null)}>关闭正文</button>
                    </header>
                    <pre>{rendered.content}</pre>
                  </section>
                ) : null}
              </article>
            ) : null}
          </main>

          <aside className="writing-history" aria-label="Writing 版本与比较">
            <small>03 · VERSIONS</small>
            <h3>不可变历史</h3>
            {versions.map((version) => (
              <section className="writing-history-version" key={version.version_ref}>
                <button
                  type="button"
                  aria-pressed={rightVersion === version.version_ref}
                  onClick={() => setRightVersion(version.version_ref)}
                >
                  <b>v{version.version_number}</b>
                  <span>{version.citation_status} · {version.availability}</span>
                  <code>{shortRef(version.content_hash)}</code>
                </button>
                {version.citation_feedback.length ? (
                  <p>RG feedback：{version.citation_feedback.join(" · ")}</p>
                ) : null}
                {selected?.run ? (
                  <div className="writing-history-actions">
                    <button type="button" disabled={busy !== null} onClick={() => void viewVersion(version.version_ref)}>
                      查看 v{version.version_number} 正文
                    </button>
                    {version.citation_status === "accepted" ? (
                      <a href={writingRenderUrl(selected.run.run_ref, version.version_ref)} download>
                        下载 Markdown
                      </a>
                    ) : null}
                  </div>
                ) : null}
              </section>
            ))}
            {versions.length >= 2 && selected?.run ? (
              <section className="writing-compare">
                <label>左版本<select value={leftVersion} onChange={(event) => setLeftVersion(event.target.value)}>{versions.map((version) => <option key={version.version_ref} value={version.version_ref}>v{version.version_number}</option>)}</select></label>
                <label>右版本<select value={rightVersion} onChange={(event) => setRightVersion(event.target.value)}>{versions.map((version) => <option key={version.version_ref} value={version.version_ref}>v{version.version_number}</option>)}</select></label>
                <button type="button" disabled={busy !== null} onClick={() => void compare()}>比较三轴</button>
              </section>
            ) : null}
            {activeComparison ? (
              <section className="writing-comparison" data-testid="writing-comparison">
                <dl>
                  <div><dt>内容</dt><dd>{activeComparison.content.changed ? "changed" : "same"}</dd></div>
                  <div><dt>证据</dt><dd>{activeComparison.evidence.changed ? "changed" : "same"}</dd></div>
                  <div><dt>引用</dt><dd>{activeComparison.citation.changed ? "changed" : "same"} · {activeComparison.citation.left_status} → {activeComparison.citation.right_status}</dd></div>
                  <div><dt>Snapshot</dt><dd>{activeComparison.stale ? "stale" : "current"}</dd></div>
                </dl>
                <div className="writing-comparison-axis">
                  <b>内容 diff</b>
                  <pre>{activeComparison.content.unified_diff || "无内容变化"}</pre>
                </div>
                <div className="writing-comparison-axis">
                  <b>证据变化</b>
                  <p>+ {activeComparison.evidence.added_source_version_refs.join(" · ") || "none"}</p>
                  <p>− {activeComparison.evidence.removed_source_version_refs.join(" · ") || "none"}</p>
                </div>
                <div className="writing-comparison-axis">
                  <b>Citation 变化</b>
                  <p>+ {activeComparison.citation.added_citation_refs.join(" · ") || "none"}</p>
                  <p>− {activeComparison.citation.removed_citation_refs.join(" · ") || "none"}</p>
                  {activeComparison.citation.changed_citations.map((item) => (
                    <details key={item.citation_ref}>
                      <summary>{item.citation_ref}</summary>
                      <code>{item.left.locator} → {item.right.locator}</code>
                      <p>{item.left.claim} → {item.right.claim}</p>
                      <p>source quote: {item.left.source_quote} → {item.right.source_quote}</p>
                    </details>
                  ))}
                </div>
              </section>
            ) : null}
          </aside>
        </div>
        <footer className="writing-footer" aria-live="polite">
          <span>execution ≠ deliverable acceptance ≠ citation acceptance ≠ render</span>
          {error ? <b>{error}</b> : <small>关闭此窗口不会停止 daemon Writing Run。</small>}
        </footer>
      </div>
    </dialog>
  );
}

function Layer({ number, label, status, receipt }: { number: string; label: string; status: string; receipt: string | null }) {
  return <section data-status={status}><small>{number}</small><b>{label}</b><span>{status}</span><code>{receipt ? shortRef(receipt) : "—"}</code></section>;
}

function receiptRef(value: unknown): string | null {
  if (!value || typeof value !== "object") return null;
  const receipt = value as { receipt_ref?: unknown };
  return typeof receipt.receipt_ref === "string" ? receipt.receipt_ref : null;
}

function shortRef(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-7)}` : value;
}

function errorCode(error: unknown): string {
  return error instanceof ProductError ? error.code : "writing_request_failed";
}
