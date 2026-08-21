import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  acknowledgeAssetIntake,
  acceptAssetRole,
  assessAssetRelease,
  fetchAssetIntake,
  fetchAssetHoldHistory,
  fetchAssetReleaseHistory,
  fetchAssetRoleHistory,
  fetchResearchAsset,
  fetchResearchAssets,
  handoffAssetToManaged,
  pendingAssetIntakeJobRef,
  placeAssetHold,
  ProductError,
  releaseAssetHold,
  submitAssetIntake,
  type AssetIntakeRequest,
  type AssetReceipt,
  type ResearchAssetItem,
  type ResearchAssetsView,
} from "./api";
import "./research-assets.css";

type IntakeKind = AssetIntakeRequest["source_kind"];
type CommandReceipt = {
  versionRef: string;
  label: string;
  receipt: AssetReceipt;
};
type HistoryCursor = {
  versionRef: string | null;
  roles: string | null;
  holds: string | null;
  assessments: string | null;
  rolesMore: boolean;
  holdsMore: boolean;
  assessmentsMore: boolean;
};
const MAX_ASSET_BYTES = 64 * 1024 * 1024;

const sourceLabels: Record<IntakeKind, string> = {
  text: "文本",
  file: "文件上传",
  directory: "本地目录",
  local_path: "本地路径",
  repository: "代码仓库",
  link: "链接",
  system_artifact: "系统产物",
};

export function ResearchAssetsWorkbench({
  initial,
  intakeWorkerReady,
  verificationWorkerReady,
  onClose,
  onChanged,
}: {
  initial: ResearchAssetsView;
  intakeWorkerReady: boolean;
  verificationWorkerReady: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const intakeControllerRef = useRef<AbortController | null>(null);
  const projectionRevisionRef = useRef(initial.revision);
  const inventoryRevisionRef = useRef(initial.inventory_revision);
  const referenceRevisionRef = useRef(initial.reference_revision);
  const selectedRefRef = useRef<string | null>(
    initial.items[0]?.memory_ref ?? null,
  );
  const [view, setView] = useState(initial);
  const [selectedRef, setSelectedRef] = useState(initial.items[0]?.memory_ref ?? null);
  const [sourceKind, setSourceKind] = useState<IntakeKind>("text");
  const [custodyMode, setCustodyMode] = useState<"managed" | "linked_local">(
    "managed",
  );
  const [displayName, setDisplayName] = useState("research-note.md");
  const [mediaType, setMediaType] = useState("text/markdown; charset=utf-8");
  const [textContent, setTextContent] = useState("");
  const [sourceLocator, setSourceLocator] = useState("");
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [asynchronous, setAsynchronous] = useState(false);
  const [createNextVersion, setCreateNextVersion] = useState(false);
  const [busy, setBusy] = useState<string | null>(() =>
    pendingAssetIntakeJobRef() ? "intake" : null,
  );
  const [pendingJobRef, setPendingJobRef] = useState<string | null>(() =>
    pendingAssetIntakeJobRef(),
  );
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("盘点来自 Research Memory 公开 Query；浏览不会写 Owner。");
  const [questRef, setQuestRef] = useState("");
  const [role, setRole] = useState<"evidence" | "quest_source_material">(
    "quest_source_material",
  );
  const [holdReason, setHoldReason] = useState("研究审计期间保留");
  const [commandReceipt, setCommandReceipt] = useState<CommandReceipt | null>(null);
  const [releaseResult, setReleaseResult] = useState<{
    versionRef: string;
    referenceRevision: number;
    eligible: boolean;
    reasons: string[];
  } | null>(null);
  const [historyCursor, setHistoryCursor] = useState<HistoryCursor>(() =>
    emptyHistoryCursor(initial.items[0]?.memory_ref ?? null),
  );
  const [nextOffset, setNextOffset] = useState(
    initial.offset + initial.items.length,
  );

  const selected = useMemo(
    () => view.items.find((item) => item.memory_ref === selectedRef) ?? null,
    [selectedRef, view.items],
  );
  const selectedRoles = useMemo(
    () =>
      view.roles
        .filter((item) => item.version_ref === selectedRef)
        .sort(
          (left, right) =>
            right.accepted_at - left.accepted_at ||
            right.role_ref.localeCompare(left.role_ref),
        ),
    [selectedRef, view.roles],
  );
  const selectedCustodies = useMemo(
    () => view.custodies.filter((item) => item.version_ref === selectedRef),
    [selectedRef, view.custodies],
  );
  const selectedHolds = useMemo(
    () =>
      view.holds
        .filter((item) => item.version_ref === selectedRef)
        .sort(
          (left, right) =>
            right.placed_at - left.placed_at ||
            right.hold_ref.localeCompare(left.hold_ref),
        ),
    [selectedRef, view.holds],
  );
  const selectedAssessments = useMemo(
    () =>
      view.release_assessments
        .filter((item) => item.version_ref === selectedRef)
        .sort(
          (left, right) =>
            right.assessed_at - left.assessed_at ||
            right.assessment_ref.localeCompare(left.assessment_ref),
        ),
    [selectedRef, view.release_assessments],
  );
  const activeHold = selectedHolds.find((item) => item.active)?.hold_ref ?? null;

  useEffect(() => {
    selectedRefRef.current = selectedRef;
  }, [selectedRef]);

  useEffect(() => {
    if (initial.revision < projectionRevisionRef.current) return;
    projectionRevisionRef.current = initial.revision;
    inventoryRevisionRef.current = initial.inventory_revision;
    referenceRevisionRef.current = initial.reference_revision;
    const inventoryStable =
      view.inventory_revision === initial.inventory_revision;
    const selectedCarry = selectedRef
      ? view.items.find((item) => item.memory_ref === selectedRef) ?? null
      : null;
    const nextView = inventoryStable
      ? mergeResearchAssetPages(view, initial)
      : selectedCarry &&
          !initial.items.some(
            (item) => item.memory_ref === selectedCarry.memory_ref,
          )
        ? {
            ...initial,
            items: [...initial.items, selectedCarry],
            custodies: mergeRows(
              initial.custodies,
              view.custodies.filter(
                (item) => item.version_ref === selectedCarry.memory_ref,
              ),
              (item) => item.custody_ref,
            ),
            roles: mergeRows(
              initial.roles,
              view.roles.filter(
                (item) => item.version_ref === selectedCarry.memory_ref,
              ),
              (item) => item.role_ref,
            ),
            holds: mergeRows(
              initial.holds,
              view.holds.filter(
                (item) => item.version_ref === selectedCarry.memory_ref,
              ),
              (item) => item.hold_ref,
            ),
            release_assessments: mergeRows(
              initial.release_assessments,
              view.release_assessments.filter(
                (item) => item.version_ref === selectedCarry.memory_ref,
              ),
              (item) => item.assessment_ref,
            ),
          }
        : initial;
    setView(nextView);
    setReleaseResult((current) => {
      if (!current || current.referenceRevision !== nextView.reference_revision) {
        return null;
      }
      const item = nextView.items.find(
        (candidate) => candidate.memory_ref === current.versionRef,
      );
      if (!item) return null;
      if (
        current.eligible &&
        (item.integrity !== "verified" ||
          item.availability !== "available" ||
          nextView.holds.some(
            (hold) => hold.version_ref === current.versionRef && hold.active,
          ))
      ) {
        return null;
      }
      return current;
    });
    const nextSelected =
      selectedRef && nextView.items.some((item) => item.memory_ref === selectedRef)
        ? selectedRef
        : nextView.items[0]?.memory_ref ?? null;
    if (
      nextSelected !== selectedRef ||
      view.inventory_revision !== initial.inventory_revision
    ) {
      setHistoryCursor(emptyHistoryCursor(nextSelected));
    }
    if (!inventoryStable) {
      setNextOffset(initial.offset + initial.items.length);
    }
    setSelectedRef(nextSelected);
  }, [initial]);

  useEffect(() => {
    if (
      !selectedRef ||
      initial.items.some((item) => item.memory_ref === selectedRef)
    ) {
      return;
    }
    let active = true;
    void fetchResearchAsset(selectedRef)
      .then((detail) => {
        if (!active) return;
        if (
          detail.revision < projectionRevisionRef.current ||
          detail.inventory_revision !== inventoryRevisionRef.current ||
          detail.reference_revision !== referenceRevisionRef.current
        ) {
          return;
        }
        projectionRevisionRef.current = detail.revision;
        setView((current) => ({
          ...current,
          revision: Math.max(current.revision, detail.revision),
          items: current.items.some(
            (item) => item.memory_ref === detail.memory_ref,
          )
            ? current.items.map((item) =>
                item.memory_ref === detail.memory_ref ? detail : item,
              )
            : [...current.items, detail],
          custodies: mergeRows(
            current.custodies,
            detail.custodies,
            (item) => item.custody_ref,
          ),
          roles: mergeRows(current.roles, detail.roles, (item) => item.role_ref),
          holds: mergeProjectionHolds(
            current.holds,
            detail.holds,
            new Set([detail.memory_ref]),
          ),
          release_assessments: mergeRows(
            current.release_assessments,
            detail.release_assessments,
            (item) => item.assessment_ref,
          ),
          reference_revision: detail.reference_revision,
        }));
      })
      .catch((caught) => {
        if (active) setError(errorCode(caught));
      });
    return () => {
      active = false;
    };
  }, [initial.items, initial.revision, selectedRef]);

  const refresh = useCallback(async (preferredRef?: string) => {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let next = await fetchResearchAssets();
      if (next.revision < projectionRevisionRef.current) continue;
      const contiguousOffset = next.offset + next.items.length;
      if (
        preferredRef &&
        !next.items.some((item) => item.memory_ref === preferredRef)
      ) {
        const detail = await fetchResearchAsset(preferredRef);
        if (
          detail.revision < next.revision ||
          detail.revision < projectionRevisionRef.current ||
          detail.inventory_revision !== next.inventory_revision ||
          detail.reference_revision !== next.reference_revision
        ) {
          continue;
        }
        next = {
          ...next,
          revision: detail.revision,
          items: [...next.items, detail],
          custodies: mergeRows(
            next.custodies,
            detail.custodies,
            (item) => item.custody_ref,
          ),
          roles: mergeRows(next.roles, detail.roles, (item) => item.role_ref),
          holds: mergeProjectionHolds(
            next.holds,
            detail.holds,
            new Set([detail.memory_ref]),
          ),
          release_assessments: mergeRows(
            next.release_assessments,
            detail.release_assessments,
            (item) => item.assessment_ref,
          ),
          reference_revision: detail.reference_revision,
        };
      }
      if (next.revision < projectionRevisionRef.current) continue;
      projectionRevisionRef.current = next.revision;
      inventoryRevisionRef.current = next.inventory_revision;
      referenceRevisionRef.current = next.reference_revision;
      setNextOffset(contiguousOffset);
      setReleaseResult(null);
      setView(next);
      const nextSelected =
        preferredRef && next.items.some((item) => item.memory_ref === preferredRef)
          ? preferredRef
          : selectedRef && next.items.some((item) => item.memory_ref === selectedRef)
          ? selectedRef
          : next.items[0]?.memory_ref ?? null;
      setHistoryCursor(emptyHistoryCursor(nextSelected));
      setSelectedRef(nextSelected);
      return next;
    }
    throw new ProductError("research_asset_projection_stale");
  }, [selectedRef]);

  const loadMore = useCallback(async () => {
    if (!view.has_more || busy !== null) return;
    setBusy("load-more");
    setError(null);
    try {
      const next = await fetchResearchAssets(
        undefined,
        nextOffset,
        view.limit,
      );
      if (
        next.revision < projectionRevisionRef.current ||
        next.inventory_revision !== inventoryRevisionRef.current ||
        next.reference_revision !== referenceRevisionRef.current
      ) {
        setNotice(
          "盘点在翻页期间已更新；已保留新的第一页，请重新加载后续版本。",
        );
        return;
      }
      projectionRevisionRef.current = next.revision;
      setView((current) => mergeResearchAssetPages(current, next));
      setNextOffset(next.offset + next.items.length);
      setNotice(
        `已读取 ${Math.min(view.items.length + next.items.length, next.total_count)} / ${next.total_count} 个精确版本。`,
      );
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  }, [busy, nextOffset, refresh, view]);

  const loadReceiptHistory = useCallback(async () => {
    if (
      !selectedRef ||
      busy !== null ||
      !(
        historyCursor.rolesMore ||
        historyCursor.holdsMore ||
        historyCursor.assessmentsMore
      )
    ) {
      return;
    }
    setBusy("history");
    setError(null);
    const requestedVersionRef = selectedRef;
    const requestedProjectionRevision = projectionRevisionRef.current;
    const requestedInventoryRevision = inventoryRevisionRef.current;
    const requestedReferenceRevision = referenceRevisionRef.current;
    try {
      const [roles, holds, assessments] = await Promise.all([
        historyCursor.rolesMore
          ? fetchAssetRoleHistory(selectedRef, historyCursor.roles)
          : Promise.resolve(null),
        historyCursor.holdsMore
          ? fetchAssetHoldHistory(selectedRef, historyCursor.holds)
          : Promise.resolve(null),
        historyCursor.assessmentsMore
          ? fetchAssetReleaseHistory(selectedRef, historyCursor.assessments)
          : Promise.resolve(null),
      ]);
      if (
        selectedRefRef.current !== requestedVersionRef ||
        projectionRevisionRef.current !== requestedProjectionRevision ||
        inventoryRevisionRef.current !== requestedInventoryRevision ||
        referenceRevisionRef.current !== requestedReferenceRevision
      ) {
        setNotice(
          "Receipt 历史读取期间 Projection 已更新；已丢弃旧页，请按当前状态重试。",
        );
        return;
      }
      setView((current) => ({
        ...current,
        roles: mergeRows(
          current.roles,
          roles?.items ?? [],
          (item) => item.role_ref,
        ),
        holds: mergeHoldHistory(
          current.holds,
          holds?.items ?? [],
        ),
        release_assessments: mergeRows(
          current.release_assessments,
          assessments?.items ?? [],
          (item) => item.assessment_ref,
        ),
      }));
      setHistoryCursor((current) =>
        current.versionRef !== selectedRef
          ? current
          : {
              ...current,
              roles: roles?.next_cursor ?? null,
              holds: holds?.next_cursor ?? null,
              assessments: assessments?.next_cursor ?? null,
              rolesMore: roles?.has_more ?? false,
              holdsMore: holds?.has_more ?? false,
              assessmentsMore: assessments?.has_more ?? false,
            },
      );
      setNotice("已通过分页 public Query 合并更多 durable receipt 历史。");
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  }, [busy, historyCursor, selectedRef]);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (!dialog.open) dialog.showModal();
    let active = true;
    let focusFrame: number | null = null;
    const focusWhenVisible = () => {
      if (!active) return;
      const closeButton = closeRef.current;
      if (
        dialog.dataset.open === "true" &&
        closeButton &&
        closeButton.getClientRects().length > 0 &&
        getComputedStyle(closeButton).visibility !== "hidden"
      ) {
        closeButton.focus({ preventScroll: true });
        return;
      }
      focusFrame = requestAnimationFrame(focusWhenVisible);
    };
    const frame = requestAnimationFrame(() => {
      if (!active) return;
      dialog.dataset.open = "true";
      focusFrame = requestAnimationFrame(focusWhenVisible);
    });
    return () => {
      active = false;
      cancelAnimationFrame(frame);
      if (focusFrame !== null) cancelAnimationFrame(focusFrame);
    };
  }, []);

  useEffect(() => {
    if (["text", "file", "link"].includes(sourceKind)) setCustodyMode("managed");
  }, [sourceKind]);

  useEffect(() => {
    if (!intakeWorkerReady) setAsynchronous(false);
  }, [intakeWorkerReady]);

  useEffect(() => {
    if (!selectedRef) setCreateNextVersion(false);
    setReleaseResult(null);
    setHistoryCursor(emptyHistoryCursor(selectedRef));
  }, [selectedRef]);

  const close = () => {
    intakeControllerRef.current?.abort();
    intakeControllerRef.current = null;
    const dialog = dialogRef.current;
    if (dialog) {
      dialog.dataset.open = "false";
      dialog.close();
    }
    onClose();
  };

  const refreshAfterAcceptedCommand = async (
    acceptedNotice: string,
    versionRef: string,
  ) => {
    try {
      const next = await refresh(versionRef);
      onChanged();
      return next;
    } catch (caught) {
      setNotice(
        `${acceptedNotice} Receipt 已形成；Projection 刷新待恢复 · ${errorCode(caught)}`,
      );
      return null;
    }
  };

  const runCommand = async (
    name: string,
    command: () => Promise<{ receipt?: AssetReceipt }>,
    message: string,
  ) => {
    const commandVersionRef = selectedRef;
    if (commandVersionRef === null) return;
    setBusy(name);
    setError(null);
    setReleaseResult(null);
    try {
      const result = await command();
      if (result.receipt) {
        setCommandReceipt({
          versionRef: commandVersionRef,
          label: message,
          receipt: result.receipt,
        });
      }
      setNotice(message);
      await refreshAfterAcceptedCommand(message, commandVersionRef);
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      setBusy(null);
    }
  };

  const waitForIntake = useCallback(async (jobRef: string, signal?: AbortSignal) => {
    let retryCount = 0;
    for (;;) {
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      try {
        const result = await fetchAssetIntake(jobRef, signal);
        setNotice(`Asset Intake ${result.status} · ${result.job_ref}`);
        if (!["queued", "processing"].includes(result.status)) return result;
      } catch (caught) {
        if (signal?.aborted) throw caught;
        const code = errorCode(caught);
        const clientFailure = /^request_failed:(4\d\d)$/.exec(code);
        if (
          clientFailure &&
          !["401", "403"].includes(clientFailure[1])
        ) {
          acknowledgeAssetIntake(jobRef);
          setPendingJobRef(null);
          throw caught;
        }
        if (/^request_failed:4\d\d$/.test(code)) {
          setNotice(
            `Asset Intake 恢复需要重新授权；durable 指针已保留 · ${jobRef}`,
          );
          throw caught;
        }
        retryCount += 1;
        setNotice(`正在恢复 durable Asset Intake · ${jobRef}`);
      }
      await delay(Math.min(4_000, 250 * 2 ** Math.min(retryCount, 4)));
    }
  }, []);

  const finishIntake = useCallback(async (result: Awaited<ReturnType<typeof fetchAssetIntake>>) => {
    if (result.status === "failed") {
      setError(result.failure?.code ?? "asset_intake_failed");
      setNotice(`Asset Intake failed · ${result.job_ref}`);
      acknowledgeAssetIntake(result.job_ref);
      setPendingJobRef(null);
      return;
    }
    if (result.status !== "accepted" || !result.asset) {
      throw new ProductError("asset_intake_status_invalid");
    }
    setSelectedRef(result.asset.memory_ref);
    setCommandReceipt({
      versionRef: result.asset.memory_ref,
      label: "Asset Accepted",
      receipt: result.asset.receipt,
    });
    try {
      await refresh(result.asset.memory_ref);
    } catch (caught) {
      setNotice(
        `Asset Accepted receipt 已形成；Projection 刷新待恢复 · ${errorCode(caught)}`,
      );
      return;
    }
    setNotice(`已接纳精确版本 ${result.asset.memory_ref}`);
    onChanged();
    acknowledgeAssetIntake(result.job_ref);
    setPendingJobRef(null);
  }, [onChanged, refresh]);

  useEffect(() => {
    const jobRef = pendingAssetIntakeJobRef();
    if (!jobRef) return;
    const controller = new AbortController();
    intakeControllerRef.current?.abort();
    intakeControllerRef.current = controller;
    setBusy("intake");
    setError(null);
    setNotice(`正在恢复 durable Asset Intake · ${jobRef}`);
    void waitForIntake(jobRef, controller.signal)
      .then((result) => finishIntake(result))
      .catch((caught) => {
        if (!controller.signal.aborted) setError(errorCode(caught));
      })
      .finally(() => {
        if (intakeControllerRef.current === controller) {
          intakeControllerRef.current = null;
        }
        if (!controller.signal.aborted) setBusy(null);
      });
    return () => {
      controller.abort();
      if (intakeControllerRef.current === controller) {
        intakeControllerRef.current = null;
      }
    };
  }, [finishIntake, waitForIntake]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const controller = new AbortController();
    intakeControllerRef.current?.abort();
    intakeControllerRef.current = controller;
    setBusy("intake");
    setError(null);
    setReleaseResult(null);
    try {
      const request: AssetIntakeRequest = {
        source_kind: sourceKind,
        custody_mode: custodyMode,
        display_name: displayName,
        media_type: mediaType,
        asynchronous: asynchronous && intakeWorkerReady,
        provenance: { submitted_via: "lumen_research_asset_workbench" },
      };
      if (createNextVersion && selected) request.asset_ref = selected.asset_ref;
      if (sourceKind === "text") request.text = textContent;
      else if (sourceKind === "file") {
        if (fileContent === null) throw new ProductError("asset_file_required");
        request.content_base64 = fileContent;
      } else request.source_locator = sourceLocator;

      let result = await submitAssetIntake(request);
      if (controller.signal.aborted) return;
      setPendingJobRef(result.job_ref);
      setNotice(`Asset Intake ${result.status} · ${result.job_ref}`);
      if (["queued", "processing"].includes(result.status)) {
        result = await waitForIntake(result.job_ref, controller.signal);
      }
      await finishIntake(result);
    } catch (caught) {
      setError(errorCode(caught));
    } finally {
      if (intakeControllerRef.current === controller) {
        intakeControllerRef.current = null;
      }
      setBusy(null);
    }
  };

  const chooseFile = async (file: File | undefined) => {
    if (!file) return;
    if (file.size > MAX_ASSET_BYTES) {
      setFileContent(null);
      setError("asset_content_too_large");
      setNotice("文件超过 Research Memory 的 64 MiB 接纳上限；尚未读取本地字节。");
      return;
    }
    setError(null);
    setDisplayName(file.name);
    setMediaType(file.type || "application/octet-stream");
    setFileContent(arrayBufferToBase64(await file.arrayBuffer()));
  };

  return (
    <dialog
      ref={dialogRef}
      className="asset-dialog"
      aria-labelledby="asset-workbench-title"
      data-testid="research-assets-workbench"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <div className="asset-window">
        <header className="asset-header">
          <span className="asset-symbol" aria-hidden="true">RA</span>
          <div className="asset-title">
            <small>RESEARCH MEMORY · PUBLIC INTERFACE</small>
            <h2 id="asset-workbench-title">Research Asset 工作台</h2>
            <p>Intake → immutable AssetVersion → custody / semantic role / release receipt</p>
          </div>
          <span className="asset-header-chip">
            {view.items.length} / {view.total_count} versions
          </span>
          <button
            ref={closeRef}
            type="button"
            className="asset-close"
            aria-label="关闭 Research Asset 工作台"
            onClick={close}
          >
            ×
          </button>
        </header>

        <div className="asset-body">
          <aside className="asset-intake" aria-labelledby="asset-intake-title">
            <div className="asset-section-heading">
              <div><small>COMMAND · RM</small><b id="asset-intake-title">接纳新资产</b></div>
              <code>
                {busy === "intake"
                  ? "processing"
                  : intakeWorkerReady
                    ? "ready"
                    : "async unavailable"}
              </code>
            </div>
            <form onSubmit={(event) => void submit(event)}>
              <label>
                <span>来源类型</span>
                <select
                  aria-label="Research Asset 来源类型"
                  value={sourceKind}
                  disabled={busy !== null}
                  onChange={(event) => setSourceKind(event.target.value as IntakeKind)}
                >
                  {(Object.keys(sourceLabels) as IntakeKind[]).map((kind) => (
                    <option key={kind} value={kind}>{sourceLabels[kind]}</option>
                  ))}
                </select>
              </label>
              <label>
                <span>显示名称</span>
                <input
                  aria-label="Research Asset 显示名称"
                  value={displayName}
                  disabled={busy !== null}
                  onChange={(event) => setDisplayName(event.target.value)}
                  required
                />
              </label>
              <label>
                <span>媒体类型</span>
                <input
                  aria-label="Research Asset 媒体类型"
                  value={mediaType}
                  disabled={busy !== null}
                  onChange={(event) => setMediaType(event.target.value)}
                  required
                />
              </label>
              {sourceKind === "text" ? (
                <label>
                  <span>原始文本</span>
                  <textarea
                    aria-label="Research Asset 原始文本"
                    rows={6}
                    value={textContent}
                    disabled={busy !== null}
                    onChange={(event) => setTextContent(event.target.value)}
                    required
                  />
                </label>
              ) : sourceKind === "file" ? (
                <label>
                  <span>本地文件</span>
                  <input
                    aria-label="Research Asset 本地文件"
                    type="file"
                    disabled={busy !== null}
                    onChange={(event) => void chooseFile(event.target.files?.[0])}
                    required
                  />
                </label>
              ) : (
                <label>
                  <span>{sourceKind === "link" ? "精确链接" : "本机绝对路径"}</span>
                  <input
                    aria-label="Research Asset 来源位置"
                    type={sourceKind === "link" ? "url" : "text"}
                    value={sourceLocator}
                    disabled={busy !== null}
                    onChange={(event) => setSourceLocator(event.target.value)}
                    required
                  />
                </label>
              )}
              <label>
                <span>保管模式</span>
                <select
                  aria-label="Research Asset 保管模式"
                  value={custodyMode}
                  disabled={busy !== null || ["text", "file", "link"].includes(sourceKind)}
                  onChange={(event) => setCustodyMode(event.target.value as typeof custodyMode)}
                >
                  <option value="managed">managed · 完整校验后接纳</option>
                  <option value="linked_local">linked_local · 冻结 manifest</option>
                </select>
              </label>
              <label className="asset-check">
                <input
                  aria-label="作为所选 AssetRef 的下一版本"
                  type="checkbox"
                  checked={createNextVersion}
                  disabled={busy !== null || !selected}
                  onChange={(event) => setCreateNextVersion(event.target.checked)}
                />
                <span>
                  {selected
                    ? `作为所选 AssetRef 的下一版本 · ${selected.asset_ref}`
                    : "先从盘点中选择一个 AssetRef，或创建全新资产"}
                </span>
              </label>
              <label className="asset-check">
                <input
                  type="checkbox"
                  checked={asynchronous}
                  disabled={busy !== null || !intakeWorkerReady}
                  onChange={(event) => setAsynchronous(event.target.checked)}
                />
                <span>
                  {intakeWorkerReady
                    ? "异步接纳；中断后由 durable worker 恢复"
                    : "异步 worker 暂不可用；同步接纳与只读盘点仍可用"}
                </span>
              </label>
              <button
                className="asset-primary"
                type="submit"
                disabled={busy !== null || pendingJobRef !== null}
              >
                {busy === "intake" ? "正在接纳…" : "提交 Asset Intake"}
              </button>
            </form>
          </aside>

          <main className="asset-inventory" aria-labelledby="asset-inventory-title">
            <div className="asset-section-heading inventory-heading">
              <div><small>QUERY / PROJECTION · NO OWNER WRITE</small><b id="asset-inventory-title">统一盘点</b></div>
              <code>
                {verificationWorkerReady
                  ? "verification ready"
                  : "verification unavailable"}
              </code>
              <button
                type="button"
                onClick={() => void refresh().catch((caught) => setError(errorCode(caught)))}
                disabled={busy !== null}
              >刷新</button>
            </div>
            <div className="asset-inventory-layout">
              <div className="asset-list-column">
                <div className="asset-list" role="list" aria-label="Research Asset 版本清单">
                  {view.items.length === 0 ? (
                    <p className="asset-empty">尚无已接纳版本。左侧 Intake 不会移动或删除原件。</p>
                  ) : view.items.map((item) => (
                    <button
                      key={item.memory_ref}
                      type="button"
                      role="listitem"
                      className={item.memory_ref === selectedRef ? "selected" : ""}
                      onClick={() => setSelectedRef(item.memory_ref)}
                    >
                      <span className="asset-kind">{item.source_kind}</span>
                      <b>{item.display_name}</b>
                      <small>{item.asset_ref} · v{item.version_number}</small>
                      <span className="asset-state-pair">
                        <i data-state={item.integrity}>integrity · {item.integrity}</i>
                        <i data-state={item.availability}>availability · {item.availability}</i>
                      </span>
                    </button>
                  ))}
                </div>
                {view.has_more ? (
                  <button
                    className="asset-load-more"
                    type="button"
                    disabled={busy !== null}
                    onClick={() => void loadMore()}
                  >
                    {busy === "load-more"
                      ? "正在读取下一页…"
                      : `加载更多（已显示 ${view.items.length} / ${view.total_count}）`}
                  </button>
                ) : null}
              </div>
              <AssetDetail
                item={selected}
                custodies={selectedCustodies}
                roles={selectedRoles}
                referenceRevision={view.reference_revision}
                busy={busy}
                questRef={questRef}
                role={role}
                holdReason={holdReason}
                activeHold={activeHold}
                releaseResult={releaseResult}
                onQuestRef={setQuestRef}
                onRole={setRole}
                onHoldReason={setHoldReason}
                onHandoff={() => {
                  if (!selected) return;
                  void runCommand(
                    "handoff",
                    () => handoffAssetToManaged(selected.memory_ref),
                    "managed custody 命令已接纳；返回可验证的 custody receipt，原件未删除。",
                  );
                }}
                onRoleAccept={() => {
                  if (!selected) return;
                  void runCommand(
                    "role",
                    () => acceptAssetRole(selected.memory_ref, role, questRef),
                    `${role} 语义角色已由 Research Graph 接纳。`,
                  );
                }}
                onHold={async () => {
                  if (!selected) return;
                  setBusy("hold");
                  setError(null);
                  setReleaseResult(null);
                  try {
                    const result = await placeAssetHold(selected.memory_ref, holdReason);
                    setCommandReceipt({
                      versionRef: selected.memory_ref,
                      label: "Asset Hold",
                      receipt: result.placement_receipt,
                    });
                    const acceptedNotice =
                      "Hold 已由 Research Memory 接纳；ReleaseEligibility 将 fail closed。";
                    setNotice(acceptedNotice);
                    await refreshAfterAcceptedCommand(
                      acceptedNotice,
                      selected.memory_ref,
                    );
                  } catch (caught) {
                    setError(errorCode(caught));
                  } finally {
                    setBusy(null);
                  }
                }}
                onReleaseHold={async () => {
                  if (!activeHold || !selected) return;
                  setBusy("release-hold");
                  setError(null);
                  setReleaseResult(null);
                  try {
                    const result = await releaseAssetHold(activeHold);
                    if (result.release_receipt) {
                      setCommandReceipt({
                        versionRef: selected.memory_ref,
                        label: "Hold Released",
                        receipt: result.release_receipt,
                      });
                    }
                    const acceptedNotice =
                      "Hold release receipt 已形成；资产字节未删除。";
                    setNotice(acceptedNotice);
                    await refreshAfterAcceptedCommand(
                      acceptedNotice,
                      selected.memory_ref,
                    );
                  } catch (caught) {
                    setError(errorCode(caught));
                  } finally {
                    setBusy(null);
                  }
                }}
                onAssess={async () => {
                  if (!selected) return;
                  setBusy("release");
                  setError(null);
                  setReleaseResult(null);
                  try {
                    const result = await assessAssetRelease(
                      selected.memory_ref,
                      view.reference_revision,
                    );
                    setCommandReceipt({
                      versionRef: selected.memory_ref,
                      label: "ReleaseEligibility",
                      receipt: result.receipt,
                    });
                    const acceptedNotice = result.eligible
                      ? "ReleaseEligibility 为 eligible；这仍不是删除命令。"
                      : `ReleaseEligibility fail closed · ${result.reason_codes.join(" · ")}`;
                    setNotice(acceptedNotice);
                    const next = await refreshAfterAcceptedCommand(
                      acceptedNotice,
                      selected.memory_ref,
                    );
                    if (next === null) return;
                    const refreshedItem = next.items.find(
                      (item) => item.memory_ref === selected.memory_ref,
                    );
                    const refreshedHold = next.holds.some(
                      (item) => item.version_ref === selected.memory_ref && item.active,
                    );
                    if (
                      result.expected_reference_revision === view.reference_revision &&
                      result.observed_reference_revision === next.reference_revision &&
                      (!result.eligible || (
                        refreshedItem?.integrity === "verified" &&
                        refreshedItem.availability === "available" &&
                        !refreshedHold
                      ))
                    ) {
                      setReleaseResult({
                        versionRef: selected.memory_ref,
                        referenceRevision: result.observed_reference_revision,
                        eligible: result.eligible,
                        reasons: result.reason_codes,
                      });
                    } else {
                      setNotice("ReleaseEligibility receipt 已保留，但刷新后状态已变化；请重新检查。");
                    }
                  } catch (caught) {
                    setError(errorCode(caught));
                  } finally {
                    setBusy(null);
                  }
                }}
              />
            </div>
          </main>

          <aside className="asset-receipts" aria-labelledby="asset-receipts-title">
            <div className="asset-section-heading">
              <div><small>AUDIT · EXACT RECEIPTS</small><b id="asset-receipts-title">Receipt Rail</b></div>
            </div>
            <ReceiptCard label="Asset Accepted" receipt={selected?.receipt ?? null} />
            {selectedCustodies.map((item) => (
              <Fragment key={item.custody_ref}>
                <ReceiptCard
                  label={`RM custody · ${item.custody_mode}`}
                  receipt={item.receipt}
                />
                <ReceiptCard
                  label="RM locator correction"
                  receipt={item.locator_receipt}
                />
              </Fragment>
            ))}
            {selectedRoles.map((item) => (
              <ReceiptCard key={item.role_ref} label={`RG · ${item.role}`} receipt={item.receipt} />
            ))}
            {selectedHolds.map((item) => (
              <Fragment key={item.hold_ref}>
                <ReceiptCard
                  label="Hold placed"
                  receipt={item.placement_receipt}
                />
                <ReceiptCard
                  label="Hold released"
                  receipt={item.release_receipt}
                />
              </Fragment>
            ))}
            {selectedAssessments.map((item) => (
              <ReceiptCard
                key={item.assessment_ref}
                label={`ReleaseEligibility · ${item.eligible ? "eligible" : "fail closed"}`}
                receipt={item.receipt}
              />
            ))}
            {selected &&
            historyCursor.versionRef === selected.memory_ref &&
            (historyCursor.rolesMore ||
              historyCursor.holdsMore ||
              historyCursor.assessmentsMore) ? (
              <button
                className="asset-history-more"
                type="button"
                disabled={busy !== null}
                onClick={() => void loadReceiptHistory()}
              >
                {busy === "history" ? "正在读取 receipt 历史…" : "加载更多 receipt 历史"}
              </button>
            ) : null}
            {commandReceipt && commandReceipt.versionRef === selected?.memory_ref ? (
              <ReceiptCard label={commandReceipt.label} receipt={commandReceipt.receipt} />
            ) : null}
            <div className="asset-boundary-note">
              <b>边界</b>
              <p>RM 拥有内容身份与保管；RG 只拥有 Evidence / Quest Source Material 角色。</p>
              <p>ReleaseEligibility 只做检查并签收，不会删除对象或原始来源。</p>
            </div>
          </aside>
        </div>

        <footer className="asset-footer">
          <div aria-live="polite">
            <b>{error ? "操作未完成" : "公开 Interface"}</b>
            <small className={error ? "error" : ""}>{error ?? notice}</small>
          </div>
          <button type="button" onClick={close}>完成</button>
        </footer>
      </div>
    </dialog>
  );
}

function AssetDetail({
  item,
  custodies,
  roles,
  referenceRevision,
  busy,
  questRef,
  role,
  holdReason,
  activeHold,
  releaseResult,
  onQuestRef,
  onRole,
  onHoldReason,
  onHandoff,
  onRoleAccept,
  onHold,
  onReleaseHold,
  onAssess,
}: {
  item: ResearchAssetItem | null;
  custodies: ResearchAssetsView["custodies"];
  roles: ResearchAssetsView["roles"];
  referenceRevision: number;
  busy: string | null;
  questRef: string;
  role: "evidence" | "quest_source_material";
  holdReason: string;
  activeHold: string | null;
  releaseResult: {
    versionRef: string;
    referenceRevision: number;
    eligible: boolean;
    reasons: string[];
  } | null;
  onQuestRef: (value: string) => void;
  onRole: (value: "evidence" | "quest_source_material") => void;
  onHoldReason: (value: string) => void;
  onHandoff: () => void;
  onRoleAccept: () => void;
  onHold: () => void;
  onReleaseHold: () => void;
  onAssess: () => void;
}) {
  if (!item) return <section className="asset-detail empty">选择一个精确版本查看公开事实。</section>;
  return (
    <section className="asset-detail" aria-label="Research Asset 版本详情">
      <header>
        <div><small>MemoryRef · exact, never latest</small><h3>{item.display_name}</h3></div>
        <a href={`/api/v1/research-assets/${item.memory_ref}/content`}>只读下载</a>
      </header>
      <dl>
        <div><dt>MemoryRef</dt><dd>{item.memory_ref}</dd></div>
        <div><dt>AssetRef</dt><dd>{item.asset_ref}</dd></div>
        <div><dt>content hash</dt><dd>{item.content_hash}</dd></div>
        <div><dt>manifest hash</dt><dd>{item.manifest_hash}</dd></div>
        <div><dt>custody</dt><dd>{item.custody_modes.join(" + ")}</dd></div>
        <div><dt>provenance</dt><dd>{JSON.stringify(item.provenance)}</dd></div>
        <div><dt>bytes</dt><dd>{item.byte_count.toLocaleString("zh-CN")}</dd></div>
        <div>
          <dt>verification</dt>
          <dd>
            {item.verification_observed_at === null
              ? "pending"
              : new Date(item.verification_observed_at * 1000).toLocaleString("zh-CN")}
            {item.verification_pending
              ? " · initial verification pending"
              : " · durable state recorded; commands reverify exact bytes"}
          </dd>
        </div>
      </dl>
      {custodies.length ? (
        <details>
          <summary>精确保管记录</summary>
          {custodies.map((custody) => (
            <p key={custody.custody_ref}>
              <code>{custody.custody_ref}</code>
              {` · ${custody.custody_mode} · ${custody.source_locator ?? "managed object store"}`}
              {custody.source_locator && !custody.locator_receipted
                ? " · historical locator not covered by its 0005 receipt"
                : ""}
              {custody.locator_receipt
                ? ` · locator receipt ${custody.locator_receipt.receipt_ref}`
                : ""}
            </p>
          ))}
        </details>
      ) : null}
      <div className="asset-independent-state" aria-label="完整性与可用性状态">
        <span><small>integrity</small><b>{item.integrity}</b></span>
        <i aria-hidden="true">≠</i>
        <span><small>availability</small><b>{item.availability}</b></span>
      </div>
      {!item.custody_modes.includes("managed") ||
      (item.integrity === "failed" && item.availability === "available") ? (
        <button type="button" onClick={onHandoff} disabled={busy !== null}>
          {item.custody_modes.includes("managed")
            ? "从可用原件修复 managed custody"
            : "校验并交接到 managed custody"}
        </button>
      ) : null}
      <details>
        <summary>赋予 Research Graph 语义角色</summary>
        <label><span>QuestRef</span><input value={questRef} onChange={(event) => onQuestRef(event.target.value)} /></label>
        <label>
          <span>角色</span>
          <select value={role} onChange={(event) => onRole(event.target.value as typeof role)}>
            <option value="quest_source_material">Quest Source Material</option>
            <option value="evidence">Evidence</option>
          </select>
        </label>
        <button type="button" onClick={onRoleAccept} disabled={busy !== null || !questRef}>由 RG 接纳角色</button>
        <small>{roles.length ? `已有 ${roles.length} 个精确角色 receipt` : "RM 资产事实不会因赋予角色而改变。"}</small>
      </details>
      <details>
        <summary>Hold 与 ReleaseEligibility</summary>
        <label><span>Hold 原因</span><input value={holdReason} onChange={(event) => onHoldReason(event.target.value)} /></label>
        {activeHold ? (
          <button type="button" onClick={onReleaseHold} disabled={busy !== null}>释放当前 Hold</button>
        ) : (
          <button type="button" onClick={onHold} disabled={busy !== null || !holdReason.trim()}>放置 Hold</button>
        )}
        <button type="button" onClick={onAssess} disabled={busy !== null}>
          检查 ReleaseEligibility · RG r{referenceRevision}
        </button>
        {releaseResult?.versionRef === item.memory_ref &&
        releaseResult.referenceRevision === referenceRevision ? (
          <small data-release-eligible={String(releaseResult.eligible)}>
            {releaseResult.eligible
              ? `assessed eligible · RG r${releaseResult.referenceRevision} · 不是删除授权`
              : `fail closed · ${releaseResult.reasons.join(" · ")}`}
          </small>
        ) : null}
      </details>
    </section>
  );
}

function ReceiptCard({ label, receipt }: { label: string; receipt: AssetReceipt | null }) {
  if (!receipt) return null;
  return (
    <article className="asset-receipt-card">
      <small>{label}</small>
      <b>{receipt.kind}</b>
      <dl>
        <div><dt>issuer</dt><dd>{receipt.issuer}</dd></div>
        <div><dt>receipt</dt><dd>{receipt.receipt_ref}</dd></div>
        <div><dt>subject</dt><dd>{receipt.subject_ref}</dd></div>
        <div><dt>payload</dt><dd>{receipt.payload_hash}</dd></div>
      </dl>
    </article>
  );
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return btoa(binary);
}

function mergeResearchAssetPages(
  current: ResearchAssetsView,
  next: ResearchAssetsView,
): ResearchAssetsView {
  const items = mergeRows(current.items, next.items, (item) => item.memory_ref);
  return {
    ...current,
    revision: Math.max(current.revision, next.revision),
    inventory_revision: next.inventory_revision,
    items,
    custodies: mergeRows(
      current.custodies,
      next.custodies,
      (item) => item.custody_ref,
    ),
    roles: mergeRows(current.roles, next.roles, (item) => item.role_ref),
    holds: mergeProjectionHolds(
      current.holds,
      next.holds,
      new Set(next.items.map((item) => item.memory_ref)),
    ),
    release_assessments: mergeRows(
      current.release_assessments,
      next.release_assessments,
      (item) => item.assessment_ref,
    ),
    reference_revision: next.reference_revision,
    total_count: next.total_count,
    has_more: items.length < next.total_count,
  };
}

function mergeRows<T>(
  left: T[],
  right: T[],
  key: (value: T) => string,
): T[] {
  const rows = new Map(left.map((value) => [key(value), value]));
  for (const value of right) rows.set(key(value), value);
  return [...rows.values()];
}

function mergeProjectionHolds(
  current: ResearchAssetsView["holds"],
  incoming: ResearchAssetsView["holds"],
  coveredVersionRefs: Set<string>,
): ResearchAssetsView["holds"] {
  const incomingRefs = new Set(incoming.map((item) => item.hold_ref));
  return mergeRows(
    current.filter(
      (item) =>
        !(
          item.active &&
          coveredVersionRefs.has(item.version_ref) &&
          !incomingRefs.has(item.hold_ref)
        ),
    ),
    incoming,
    (item) => item.hold_ref,
  );
}

function mergeHoldHistory(
  current: ResearchAssetsView["holds"],
  incoming: ResearchAssetsView["holds"],
): ResearchAssetsView["holds"] {
  const rows = new Map(current.map((item) => [item.hold_ref, item]));
  for (const item of incoming) {
    const existing = rows.get(item.hold_ref);
    if (existing && !existing.active && item.active) continue;
    rows.set(item.hold_ref, item);
  }
  return [...rows.values()];
}

function emptyHistoryCursor(versionRef: string | null): HistoryCursor {
  return {
    versionRef,
    roles: null,
    holds: null,
    assessments: null,
    rolesMore: versionRef !== null,
    holdsMore: versionRef !== null,
    assessmentsMore: versionRef !== null,
  };
}

function errorCode(caught: unknown): string {
  return caught instanceof ProductError ? caught.code : "unknown_error";
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
