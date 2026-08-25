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
  confirmWritingDeliveryIntent,
  confirmWritingIntent,
  controlWritingRun,
  createWritingDeliveryIntent,
  createWritingIntent,
  fetchWritingVersionContent,
  previewWritingDeliveryIntent,
  previewWritingIntent,
  previewWritingCancellation,
  ProductError,
  reviseWritingRun,
  writingRenderUrl,
  type WritingComparison,
  type WritingCancellationPreview,
  type WritingDeliveryPayload,
  type WritingDeliveryView,
  type WritingDocumentType,
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
  const [documentType, setDocumentType] = useState<WritingDocumentType>("report");
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
  const [deliveryDraft, setDeliveryDraft] = useState<WritingDeliveryView | null>(null);
  const [selectedDeliveryId, setSelectedDeliveryId] = useState<string | null>(null);
  const [deliveryCreatingNew, setDeliveryCreatingNew] = useState(false);
  const [deliveryAction, setDeliveryAction] =
    useState<WritingDeliveryPayload["action"]>("publish");
  const [deliveryProvider, setDeliveryProvider] = useState("local-filesystem");
  const [deliveryPath, setDeliveryPath] = useState("");
  const [deliveryExpectedHash, setDeliveryExpectedHash] = useState("");
  const [deliveryFormat, setDeliveryFormat] = useState("");

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
  const providerCapabilities = overview.delivery_capabilities?.providers ?? [];
  const selectedRendererCapability = overview.delivery_capabilities?.renderers.find(
    (item) => item.document_type === selected?.document_type,
  );
  const availableDeliveryFormats = useMemo(
    () => selected?.renderer.formats ?? selectedRendererCapability?.formats ?? [],
    [selected?.renderer.formats, selectedRendererCapability?.formats],
  );
  const defaultDeliveryFormat = selected?.renderer.default_format
    ?? selectedRendererCapability?.default_format
    ?? availableDeliveryFormats[0]
    ?? "";
  const selectedProviderCapability = providerCapabilities.find(
    (item) => item.provider_ref === deliveryProvider,
  );
  const deliveryReady = selected?.citation.status === "accepted"
    && selected.renderer.status === "ready";
  const selectedDelivery = (() => {
    if (deliveryCreatingNew) return null;
    const desiredId = selectedDeliveryId ?? deliveryDraft?.intent_id;
    const persisted = desiredId
      ? selected?.deliveries?.find((item) => item.intent_id === desiredId)
      : selected?.deliveries?.[0];
    if (persisted) return persisted;
    const payload = deliveryDraft ? deliveryPayload(deliveryDraft) : null;
    return payload?.run_ref === selected?.run?.run_ref ? deliveryDraft : null;
  })();
  const currentDeliveryPayload = selectedDelivery
    ? deliveryPayload(selectedDelivery)
    : null;
  const currentDeliveryPreview = selectedDelivery
    ? deliveryOwnerPreview(selectedDelivery)
    : null;
  const currentDeliveryOperation = selectedDelivery?.operation ?? null;

  useEffect(() => {
    const supportedActions = selectedProviderCapability?.supported_actions ?? [];
    setDeliveryAction((current) => (
      supportedActions.includes(current) ? current : supportedActions[0] ?? "publish"
    ));
  }, [selectedProviderCapability]);

  useEffect(() => {
    if (!selected) return;
    const supportedFormats = availableDeliveryFormats;
    const fallback = defaultDeliveryFormat;
    setDeliveryFormat((current) => (
      current && supportedFormats.includes(current) ? current : fallback
    ));
    setDeliveryDraft((current) => {
      const desiredId = selectedDeliveryId ?? current?.intent_id;
      const persisted = desiredId
        ? selected.deliveries?.find((item) => item.intent_id === desiredId)
        : selected.deliveries?.[0];
      if (persisted) return persisted;
      const payload = current ? deliveryPayload(current) : null;
      return payload?.run_ref === selected.run?.run_ref ? current : null;
    });
    setSelectedDeliveryId((current) => {
      if (current && selected.deliveries?.some((item) => item.intent_id === current)) {
        return current;
      }
      const localPayload = deliveryDraft ? deliveryPayload(deliveryDraft) : null;
      if (
        current
        && deliveryDraft?.intent_id === current
        && localPayload?.run_ref === selected.run?.run_ref
      ) {
        return current;
      }
      return selected.deliveries?.[0]?.intent_id ?? null;
    });
  }, [
    availableDeliveryFormats,
    defaultDeliveryFormat,
    deliveryDraft,
    selected,
    selectedDeliveryId,
  ]);

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
        document_type: documentType,
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
      const cleanupDeadline = Date.now() + 5_000;
      while (true) {
        try {
          replaceReport(
            await controlWritingRun(
              selected.run.run_ref,
              action,
              selected.run.attempt_ref,
              selected.run.fence_ref,
            ),
          );
          break;
        } catch (caught) {
          const code = errorCode(caught);
          if (
            action === "resume"
            && code === "runtime_quiescence_pending"
            && Date.now() < cleanupDeadline
          ) {
            // Pause retires the logical Fence before an in-flight provider is
            // physically quiescent. Preserve this one user resume intent and
            // replay its idempotent command after the worker records the safe
            // boundary instead of requiring a timing-dependent second click.
            await new Promise((resolveDelay) => window.setTimeout(resolveDelay, 100));
            continue;
          }
          throw caught;
        }
      }
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

  const createDelivery = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected?.run || busy || deliveryProvider !== "local-filesystem") return;
    setBusy("delivery-create");
    setError(null);
    try {
      const drafted = await createWritingDeliveryIntent(selected.run.run_ref, {
        action: deliveryAction,
        provider_ref: deliveryProvider,
        target: {
          path: deliveryPath,
          permissions: 384,
          expected_existing_hash: deliveryAction === "publish"
            ? null
            : deliveryExpectedHash,
        },
        output_format: deliveryFormat || defaultDeliveryFormat,
      });
      setDeliveryDraft(drafted);
      setSelectedDeliveryId(drafted.intent_id);
      setDeliveryCreatingNew(false);
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const previewDelivery = async () => {
    if (!selectedDelivery || busy) return;
    setBusy("delivery-preview");
    setError(null);
    try {
      const previewed = await previewWritingDeliveryIntent(selectedDelivery.intent_id);
      setDeliveryDraft(previewed);
      setSelectedDeliveryId(previewed.intent_id);
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const confirmDelivery = async () => {
    if (!selectedDelivery?.impact_preview || busy) return;
    setBusy("delivery-confirm");
    setError(null);
    try {
      const confirmed = await confirmWritingDeliveryIntent(selectedDelivery);
      setDeliveryDraft(confirmed);
      setSelectedDeliveryId(confirmed.intent_id);
      onChanged();
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const resetDelivery = () => {
    setDeliveryDraft(null);
    setDeliveryCreatingNew(true);
    setDeliveryPath("");
    setDeliveryExpectedHash("");
    setError(null);
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
          <aside className="writing-compose" aria-label="创建 Writing Run">
            <small>01 · INTENT</small>
            <h3>冻结一次精确请求</h3>
            {questRef ? (
              <form onSubmit={create}>
                <fieldset disabled={busy !== null}>
                <label>交付类型<select value={documentType} onChange={(event) => setDocumentType(event.target.value as WritingDocumentType)}>
                  <option value="report">Report · 阶段报告</option>
                  <option value="paper">Paper · 论文</option>
                  <option value="presentation">PPT · 演示文稿</option>
                </select></label>
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
                    setDeliveryDraft(null);
                    setSelectedDeliveryId(null);
                    setDeliveryCreatingNew(false);
                  }}
                >
                  <b>{item.intent.title}</b>
                  <small>{documentTypeLabel(item.document_type)} · {item.status} · r{item.run?.content_revision ?? 0}</small>
                </button>
              )) : <p className="writing-empty">尚无 Writing Run。左侧确认后，daemon 会独立运行。</p>}
            </div>

            {selected?.run ? (
              <article className="writing-run" data-testid="writing-run-detail">
                <div className="writing-run-title">
                  <div><small>{documentTypeLabel(selected.document_type)} / RUN / {shortRef(selected.run.run_ref)}</small><h3>{selected.intent.title}</h3></div>
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
                    <a href={writingRenderUrl(selected.run.run_ref, undefined, selected.renderer.default_format)} download>
                      下载确定性 {renderFormatLabel(selected.renderer.default_format)}
                    </a>
                  </div>
                ) : null}
                {deliveryReady || Boolean(selected.deliveries?.length) ? (
                  <details
                    className="writing-delivery"
                    data-testid="writing-external-delivery"
                  >
                    <summary>
                      <span>外部交付</span>
                      <b>{selectedDelivery ? deliveryOutcomeStatus(selectedDelivery) : "按需启用"}</b>
                    </summary>
                    <section className="writing-delivery-capabilities" aria-label="Writing Provider capabilities">
                      <small>PROVIDER CAPABILITIES</small>
                      {providerCapabilities.length ? (
                        <ul>
                          {providerCapabilities.map((capability) => (
                            <li key={capability.provider_ref}>
                              <code>{capability.provider_ref}</code>
                              <span>{capability.production_ready ? "production" : "non-production"}</span>
                              <span>{capability.supported_actions.join(" · ")}</span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p>当前没有可用的 production Provider；不会尝试外部动作。</p>
                      )}
                    </section>
                    {selected.deliveries?.length ? (
                      <nav
                        className="writing-delivery-history"
                        aria-label="Writing delivery intent 历史"
                      >
                        <small>DELIVERY INTENTS</small>
                        <ul>
                          {selected.deliveries.map((item) => (
                            <li key={item.intent_id}>
                              <button
                                type="button"
                                aria-pressed={!deliveryCreatingNew
                                  && selectedDelivery?.intent_id === item.intent_id}
                                onClick={() => {
                                  setDeliveryDraft(item);
                                  setSelectedDeliveryId(item.intent_id);
                                  setDeliveryCreatingNew(false);
                                  setError(null);
                                }}
                              >
                                <span>{deliveryTargetLabel(item)}</span>
                                <small>{item.confirmation_status} · {item.status}</small>
                              </button>
                            </li>
                          ))}
                        </ul>
                      </nav>
                    ) : null}
                    {!deliveryReady ? (
                      <p
                        className="writing-delivery-blocker"
                        data-testid="writing-delivery-current-blocker"
                        role="status"
                      >
                        asset custody / Citation / Renderer 当前不可用于新外部动作；
                        durable 历史 receipts 与 observations 保持可读。
                      </p>
                    ) : null}
                    {!selectedDelivery ? (
                      deliveryReady ? (
                      <form className="writing-delivery-form" onSubmit={createDelivery}>
                        <label>Provider<select
                          aria-label="Provider"
                          value={deliveryProvider}
                          onChange={(event) => setDeliveryProvider(event.target.value)}
                          disabled={busy !== null}
                        >
                          {providerCapabilities.map((capability) => (
                            <option key={capability.provider_ref} value={capability.provider_ref}>
                              {capability.provider_ref}{capability.production_ready ? "" : " · unavailable"}
                            </option>
                          ))}
                        </select></label>
                        {deliveryProvider === "local-filesystem" ? <>
                          <label>Action<select
                            aria-label="Action"
                            value={deliveryAction}
                            onChange={(event) => {
                              const action = event.target.value as WritingDeliveryPayload["action"];
                              setDeliveryAction(action);
                              if (action === "publish") setDeliveryExpectedHash("");
                            }}
                            disabled={busy !== null}
                          >
                            {(selectedProviderCapability?.supported_actions ?? []).map((action) => (
                              <option key={action} value={action}>{deliveryActionLabel(action)}</option>
                            ))}
                          </select></label>
                          <label>绝对 path<input
                            aria-label="绝对 path"
                            value={deliveryPath}
                            onChange={(event) => setDeliveryPath(event.target.value)}
                            placeholder="/absolute/path/output.docx"
                            required
                            disabled={busy !== null}
                          /></label>
                          <label>Permissions<input aria-label="Permissions" value="0600" readOnly /></label>
                          {deliveryAction === "publish" ? null : (
                            <label>Expected existing SHA-256<input
                              aria-label="Expected existing SHA-256"
                              value={deliveryExpectedHash}
                              onChange={(event) => setDeliveryExpectedHash(event.target.value)}
                              minLength={64}
                              maxLength={64}
                              pattern="[0-9a-f]{64}"
                              required
                              disabled={busy !== null}
                            /></label>
                          )}
                          <label>Renderer format<select
                            aria-label="Renderer format"
                            value={deliveryFormat}
                            onChange={(event) => setDeliveryFormat(event.target.value)}
                            disabled={busy !== null}
                          >
                            {availableDeliveryFormats.map((value) => (
                              <option key={value} value={value}>{renderFormatLabel(value)}</option>
                            ))}
                          </select></label>
                        </> : (
                          <p className="writing-delivery-adapter-note">
                            此 Provider 的 target editor 未安装；capability 列表不会被固化为本地 Provider 矩阵。
                          </p>
                        )}
                        <button
                          type="submit"
                          disabled={
                            busy !== null
                            || !selectedProviderCapability?.production_ready
                            || !selectedProviderCapability.supported_actions.includes(deliveryAction)
                            || deliveryProvider !== "local-filesystem"
                            || !isAbsoluteDeliveryPath(deliveryPath)
                            || (deliveryAction !== "publish" && !isSha256(deliveryExpectedHash))
                            || !deliveryFormat
                          }
                        >
                          {busy === "delivery-create" ? "正在冻结…" : "创建交付 Draft"}
                        </button>
                      </form>
                      ) : (
                        <p className="writing-delivery-adapter-note">
                          当前状态 fail-closed：选择既有 intent 可查看事实，恢复 ready 后才能创建新 Draft。
                        </p>
                      )
                    ) : (
                      <section className="writing-delivery-intent">
                        <header>
                          <div>
                            <small>HC COMMAND · {selectedDelivery.confirmation_status}</small>
                            <b>{currentDeliveryPayload?.provider_ref} / {currentDeliveryPayload?.action}</b>
                          </div>
                          <span data-status={deliveryOutcomeStatus(selectedDelivery)}>
                            {deliveryOutcomeStatus(selectedDelivery)}
                          </span>
                        </header>
                        {currentDeliveryPayload ? (
                          <dl className="writing-delivery-bindings">
                            <div><dt>Stable operation</dt><dd>{currentDeliveryPayload.operation_ref}</dd></div>
                            <div><dt>Exact target</dt><dd>{JSON.stringify(currentDeliveryPayload.target)}</dd></div>
                            <div><dt>Exact effects</dt><dd>{JSON.stringify(currentDeliveryPayload.effects)}</dd></div>
                            <div><dt>Writing version</dt><dd>{currentDeliveryPayload.version_ref} · {currentDeliveryPayload.content_hash}</dd></div>
                            <div><dt>RG citation</dt><dd>{currentDeliveryPayload.citation_decision_ref} · {receiptRef(currentDeliveryPayload.citation_receipt)}</dd></div>
                            <div><dt>Renderer artifact</dt><dd>{currentDeliveryPayload.renderer_version_ref} · {currentDeliveryPayload.renderer_artifact_sha256} · {currentDeliveryPayload.renderer_format}</dd></div>
                          </dl>
                        ) : null}
                        {currentDeliveryPreview ? (
                          <section className="writing-delivery-preview" data-testid="writing-delivery-preview">
                            <b>精确影响预览</b>
                            <code>{JSON.stringify(currentDeliveryPreview.target_assertion)}</code>
                            <ul>{currentDeliveryPreview.will_happen.map((item) => <li key={item}>{item}</li>)}</ul>
                            <ul className="will-not">{currentDeliveryPreview.will_not_happen.map((item) => <li key={item}>{item}</li>)}</ul>
                            <ul className="risks">{currentDeliveryPreview.risks.map((item) => <li key={item}>{item}</li>)}</ul>
                          </section>
                        ) : null}
                        <div className="writing-delivery-actions">
                          {!selectedDelivery.impact_preview ? (
                            <button type="button" disabled={busy !== null || !deliveryReady} onClick={() => void previewDelivery()}>
                              {busy === "delivery-preview" ? "正在校验…" : "生成精确影响预览"}
                            </button>
                          ) : !selectedDelivery.confirmation_receipt ? (
                            <button type="button" disabled={busy !== null || !deliveryReady} onClick={() => void confirmDelivery()}>
                              {busy === "delivery-confirm" ? "正在接纳…" : "确认本次外部交付"}
                            </button>
                          ) : (
                            <button type="button" disabled={busy !== null || !deliveryReady} onClick={resetDelivery}>
                              为新的外部副作用创建新 Draft
                            </button>
                          )}
                        </div>
                        <dl className="writing-delivery-custody" aria-label="Writing 交付 receipts 与 observations">
                          <div><dt>HC confirmation</dt><dd>{receiptRef(selectedDelivery.confirmation_receipt) ?? "not_confirmed"}</dd></div>
                          <div><dt>AR admission</dt><dd>{receiptRef(currentDeliveryOperation?.operation_receipt) ?? "not_admitted"}</dd></div>
                          <div><dt>AR execution</dt><dd>{receiptRef(currentDeliveryOperation?.execution_receipt) ?? "not_attempted"}</dd></div>
                          <div><dt>AR reconciliation</dt><dd>{receiptRef(currentDeliveryOperation?.reconciliation_receipt) ?? "none"}</dd></div>
                        </dl>
                        <section className="writing-delivery-observations">
                          <b>Provider observations</b>
                          <p>Provider ACK 不是 Owner receipt；这里只展示外部系统观测。</p>
                          {currentDeliveryOperation?.provider_observations.length ? (
                            <ul>{currentDeliveryOperation.provider_observations.map((observation) => (
                              <li key={observation.observation_ref}>
                                <code>{observation.provider_operation_ref}</code>
                                <span>{observation.outcome}</span>
                                <small>{JSON.stringify(observation.details)}</small>
                              </li>
                            ))}</ul>
                          ) : <p>none</p>}
                        </section>
                      </section>
                    )}
                  </details>
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
                      <a href={writingRenderUrl(selected.run.run_ref, version.version_ref, selected.renderer.default_format)} download>
                        下载 {renderFormatLabel(selected.renderer.default_format)}
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
                  <div><dt>Snapshot</dt><dd>frozen · {activeComparison.snapshot.snapshot_hash.slice(0, 12)}</dd></div>
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

function documentTypeLabel(value: WritingDocumentType): string {
  return value === "presentation" ? "PPT" : value.toUpperCase();
}

function renderFormatLabel(value: string | undefined): string {
  return (value ?? "markdown").toUpperCase();
}

function deliveryPayload(delivery: WritingDeliveryView): WritingDeliveryPayload | null {
  return delivery.payload ?? delivery.operation?.payload ?? null;
}

function deliveryOwnerPreview(delivery: WritingDeliveryView): {
  target_assertion: Record<string, unknown>;
  will_happen: string[];
  will_not_happen: string[];
  risks: string[];
  stale_conditions: string[];
} | null {
  const preview = delivery.impact_preview;
  if (!preview) return null;
  const owner = preview.owner_previews?.[0];
  if (owner) return owner;
  if (!preview.target_assertion) return null;
  return {
    target_assertion: preview.target_assertion,
    will_happen: preview.will_happen ?? [],
    will_not_happen: preview.will_not_happen ?? [],
    risks: preview.risks ?? [],
    stale_conditions: preview.stale_conditions ?? [],
  };
}

function deliveryOutcomeStatus(
  delivery: WritingDeliveryView,
): "not_attempted" | "partial" | "outcome_unknown" | "completed" {
  return delivery.status;
}

function deliveryTargetLabel(delivery: WritingDeliveryView): string {
  const payload = deliveryPayload(delivery);
  if (!payload) return shortRef(delivery.intent_id);
  return "path" in payload.target ? payload.target.path : payload.target.target_ref;
}

function deliveryActionLabel(action: WritingDeliveryPayload["action"]): string {
  if (action === "publish") return "publish · 新建文件";
  if (action === "overwrite") return "overwrite · 精确覆盖";
  if (action === "delete") return "delete · 精确删除";
  if (action === "send") return "send · 发送";
  return "submit · 提交";
}

function isAbsoluteDeliveryPath(value: string): boolean {
  if (!value.startsWith("/") || value === "/" || value.includes("\0")) return false;
  return value.split("/").slice(1).every(
    (part) => part.length > 0 && part !== "." && part !== "..",
  );
}

function isSha256(value: string): boolean {
  return /^[0-9a-f]{64}$/.test(value);
}

function shortRef(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-7)}` : value;
}

function errorCode(error: unknown): string {
  return error instanceof ProductError ? error.code : "writing_request_failed";
}
