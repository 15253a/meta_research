import { ServerMaterialPicker } from "./ServerMaterialPicker";
import type { ServerMaterialSelection } from "./workMaterialApi";
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
  retireResearchAsset,
  releaseAssetHold,
  submitAssetIntake,
  type AssetIntakeRequest,
  type AssetChangeImpact,
  type AssetChangeKind,
  type AssetChangeRequest,
  type AssetLifecycle,
  type AssetReceipt,
  type ResearchAssetItem,
  type ResearchAssetsView,
} from "./api";
import "./research-assets.css";
import { ResearchLibrary } from "./ResearchLibrary";
import { OutputLanguageControl, useOutputLanguage } from "./OutputLanguage";

type IntakeKind = Exclude<AssetIntakeRequest["source_kind"], "file">;
type ChangeDraft = {
  kind: "supplement" | "substantive_change" | "correction";
  explanation: string;
  error: string;
  scope: string;
  evidenceRefs: string[];
  impact: AssetChangeImpact[];
  noAffectedWorkExplanation: string;
};
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

const sourceLabels: Record<IntakeKind, string> = {
  text: "文本",
  directory: "服务器目录",
  local_path: "服务器文件路径",
  repository: "代码仓库",
  link: "链接",
  system_artifact: "系统产物",
};

const changeLabels: Record<AssetChangeKind, string> = {
  initial: "首次保留",
  supplement: "补充",
  substantive_change: "实质变更",
  correction: "更正",
  retirement: "退役",
};

const lifecycleLabels = {
  current: "当前可复用版本",
  superseded: "已有后继版本",
  retired: "已退役",
  unselected: "尚未选为当前版本",
};

function emptyChangeDraft(versionRef: string | null): ChangeDraft {
  return {
    kind: "supplement",
    explanation: "",
    error: "",
    scope: "",
    evidenceRefs: versionRef ? [versionRef] : [],
    impact: [],
    noAffectedWorkExplanation: "",
  };
}

export function ResearchAssetsWorkbench({
  initial,
  currentQuestRef = null,
  currentQuestionRef = null,
  intakeWorkerReady,
  verificationWorkerReady,
  onClose,
  onChanged,
}: {
  initial: ResearchAssetsView;
  currentQuestRef?: string | null;
  currentQuestionRef?: string | null;
  intakeWorkerReady: boolean;
  verificationWorkerReady: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const { language } = useOutputLanguage();
  const t = (zh: string, en: string) => language === "zh" ? zh : en;
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
  const [serverSelection, setServerSelection] = useState<ServerMaterialSelection | null>(null);
  const [custodyMode, setCustodyMode] = useState<"managed" | "linked_local">(
    "managed",
  );
  const [displayName, setDisplayName] = useState("research-note.md");
  const [mediaType, setMediaType] = useState("text/markdown; charset=utf-8");
  const [textContent, setTextContent] = useState("");
  const [sourceLocator, setSourceLocator] = useState("");
  const [asynchronous, setAsynchronous] = useState(false);
  const [createNextVersion, setCreateNextVersion] = useState(false);
  const [changeDraft, setChangeDraft] = useState<ChangeDraft>(() => emptyChangeDraft(selectedRef));
  const [lifecycleDetail, setLifecycleDetail] = useState<{
    versionRef: string;
    lifecycle: AssetLifecycle;
    referenceRevision: number;
  } | null>(null);
  const [lifecycleQueryTick, setLifecycleQueryTick] = useState(0);
  const [retirementFailure, setRetirementFailure] = useState<ProductError | null>(null);
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
  const lifecycle = lifecycleDetail?.versionRef === selectedRef ? lifecycleDetail.lifecycle : null;
  const selectedLifecycle = lifecycle?.versions.find((item) => item.version_ref === selectedRef);
  const canChangeSelected = Boolean(
    selected && lifecycle && selectedLifecycle
    && selectedLifecycle.state !== "retired"
    && (lifecycle.current_version_ref === null || lifecycle.current_version_ref === selectedRef),
  );
  const changeBasisHint = !selected
    ? null
    : !lifecycle
      ? "正在读取所选版本的状态。"
      : selectedLifecycle?.state === "retired"
        ? "所选版本已退役。请打开当前版本，或保存为全新资产。"
        : lifecycle.current_version_ref !== null && lifecycle.current_version_ref !== selectedRef
          ? "当前复用入口已指向其他版本。请在版本详情中打开当前版本。"
          : lifecycle.current_version_ref === null
            ? selectedLifecycle?.state === "unselected"
              ? "这份历史版本尚未选为当前。提交变更将明确选择其后继版本作为当前版本。"
              : "当前复用入口为空。提交变更将选择所选历史版本的后继作为当前版本。"
            : null;

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
    if (!selectedRef) return;
    const controller = new AbortController();
    setLifecycleDetail(null);
    void fetchResearchAsset(selectedRef, controller.signal)
      .then((detail) => {
        if (controller.signal.aborted) return;
        if (
          detail.revision < projectionRevisionRef.current ||
          detail.inventory_revision !== inventoryRevisionRef.current ||
          detail.reference_revision !== referenceRevisionRef.current
        ) {
          return;
        }
        setLifecycleDetail({
          versionRef: selectedRef,
          lifecycle: detail.lifecycle,
          referenceRevision: detail.reference_revision,
        });
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
        if (!controller.signal.aborted) setError(errorCode(caught));
      });
    return () => {
      controller.abort();
    };
  }, [initial.items, initial.revision, lifecycleQueryTick, selectedRef]);

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
      setLifecycleQueryTick((current) => current + 1);
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
    if (["text", "link"].includes(sourceKind)) setCustodyMode("managed");
  }, [sourceKind]);

  useEffect(() => {
    if (!intakeWorkerReady) setAsynchronous(false);
  }, [intakeWorkerReady]);

  useEffect(() => {
    setCreateNextVersion(false);
    setChangeDraft(emptyChangeDraft(selectedRef));
    setRetirementFailure(null);
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
    if (busy || pendingJobRef) return;
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
      if (serverSelection && sourceLocator === serverSelection.absolute_path
          && sourceKind === (serverSelection.kind === "directory" ? "directory" : "local_path")) {
        request.provenance!.server_material_selection = serverSelection;
      }
      if (createNextVersion && selected) {
        if (!lifecycle || !canChangeSelected) {
          throw new ProductError("asset_current_version_required");
        }
        const basis = {
          predecessor_version_ref: selected.memory_ref,
          expected_revision: lifecycle.revision,
          explanation: changeDraft.explanation.trim(),
        };
        let change: AssetChangeRequest;
        if (changeDraft.kind === "correction") {
          if (!changeDraft.impact.length && !changeDraft.noAffectedWorkExplanation.trim()) {
            throw new ProductError("asset_change_no_affected_work_explanation_required");
          }
          const evidence = changeDraft.evidenceRefs.map((ref) => view.items.find((item) => item.memory_ref === ref));
          if (!changeDraft.error.trim() || !changeDraft.scope.trim()
            || !evidence.length || evidence.some((item) => !item)
            || changeDraft.impact.some((item) => !item.work_ref.trim() || !item.explanation.trim())) {
            throw new ProductError("asset_correction_basis_required");
          }
          change = {
            ...basis,
            kind: "correction",
            error: changeDraft.error.trim(),
            scope: changeDraft.scope.trim(),
            evidence_bindings: evidence.flatMap((item) => item ? [{
              asset_ref: item.asset_ref,
              version_ref: item.memory_ref,
              content_hash: item.content_hash,
              manifest_hash: item.manifest_hash,
              receipt: item.receipt,
            }] : []),
            impact: changeDraft.impact.map((item) => ({
              ...item,
              work_ref: item.work_ref.trim(),
              explanation: item.explanation.trim(),
            })),
            ...(!changeDraft.impact.length ? {
              no_affected_work_explanation: changeDraft.noAffectedWorkExplanation.trim(),
            } : {}),
          };
        } else change = { ...basis, kind: changeDraft.kind };
        request.asset_ref = selected.asset_ref;
        request.change = change;
      }
      if (sourceKind === "text") request.text = textContent;
      else request.source_locator = sourceLocator;

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

  const intakeBlocker = busy !== null
      ? "正在处理当前操作，请稍候。"
      : pendingJobRef !== null
        ? "上次提交仍在恢复中，请等待结果。"
        : createNextVersion && !canChangeSelected
            ? changeBasisHint ?? "所选版本目前不能作为变更基础。"
            : createNextVersion && !changeDraft.explanation.trim()
              ? "请说明这次变更及其影响。"
              : createNextVersion && changeDraft.kind === "correction"
                && (!changeDraft.error.trim() || !changeDraft.scope.trim() || !changeDraft.evidenceRefs.length
                  || changeDraft.impact.some((item) => !item.work_ref.trim() || !item.explanation.trim()))
                ? "请补全错误、更正范围、精确证据和已添加的影响判断。"
                : createNextVersion && changeDraft.kind === "correction"
                  && !changeDraft.impact.length && !changeDraft.noAffectedWorkExplanation.trim()
                  ? "请说明已核查的工作范围，以及没有受影响工作的理由。"
                : null;

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
            <small>{t("研究项目资料", "PROJECT RESEARCH LIBRARY")}</small>
            <h2 id="asset-workbench-title">{t("研究资料", "Research library")}</h2>
            <p>{t("找到、阅读并保存可供后续研究使用的材料", "Find, read, and preserve material for future research")}</p>
          </div>
          <OutputLanguageControl />
          <span className="asset-header-chip">
            {t("已保存内容版本：", "Saved content versions: ")}{view.total_count}
          </span>
          <button
            ref={closeRef}
            type="button"
            className="asset-close"
            aria-label={t("关闭研究资料", "Close research library")}
            onClick={close}
          >
            ×
          </button>
        </header>

        <ResearchLibrary questRef={currentQuestRef} questionRef={currentQuestionRef} />
        <details className="asset-storage-details">
          <summary>{t("保存文件与保管设置", "Save files and manage storage")}</summary>
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
              <ServerMaterialPicker value={serverSelection} disabled={busy !== null} onSelect={selection => {
                setServerSelection(selection);
                if (selection) {
                  setSourceKind(selection.kind === "directory" ? "directory" : "local_path");
                  setCustodyMode("linked_local"); setSourceLocator(selection.absolute_path);
                  setDisplayName(selection.absolute_path.split("/").at(-1) || selection.absolute_path);
                  setMediaType(selection.kind === "directory" ? "application/x-directory" : "application/octet-stream");
                }
              }} />
              <p>服务器候选仅提供来源位置。点击“提交 Asset Intake”才会正式接纳资产。</p>
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
              ) : (
                <label>
                  <span>{sourceKind === "link" ? "精确链接" : "服务器绝对路径"}</span>
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
                  disabled={busy !== null || ["text", "link"].includes(sourceKind)}
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
                  disabled={busy !== null || !canChangeSelected}
                  onChange={(event) => setCreateNextVersion(event.target.checked)}
                />
                <span>
                  {selected
                    ? `作为所选 AssetRef 的下一版本 · ${selected.asset_ref}`
                    : "先从盘点中选择一个 AssetRef，或创建全新资产"}
                </span>
              </label>
              {changeBasisHint ? <small>{changeBasisHint}</small> : null}
              {createNextVersion ? (
                <ChangeFields
                  draft={changeDraft}
                  items={view.items}
                  busy={busy !== null}
                  onChange={setChangeDraft}
                />
              ) : null}
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
                disabled={intakeBlocker !== null}
                aria-describedby="asset-intake-help"
              >
                {busy === "intake" ? "正在接纳…" : "提交 Asset Intake"}
              </button>
              <small id="asset-intake-help" role="status">{intakeBlocker}</small>
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
                      disabled={busy !== null}
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
                lifecycle={lifecycle}
                retirementFailure={retirementFailure}
                onVersion={(versionRef) => setSelectedRef(versionRef)}
                onRetire={async (explanation) => {
                  if (!selected || !lifecycle || !lifecycleDetail) return;
                  const versionRef = selected.memory_ref;
                  setBusy("retirement");
                  setError(null);
                  setRetirementFailure(null);
                  try {
                    const result = await retireResearchAsset(versionRef, {
                      expected_revision: lifecycle.revision,
                      expected_reference_revision: lifecycleDetail.referenceRevision,
                      explanation,
                      low_value: true,
                      obsolete: true,
                      incorrect: true,
                      impact_understood: true,
                      has_explanation_value: false,
                    });
                    setCommandReceipt({ versionRef, label: "版本退役", receipt: result.receipt });
                    const message = "退役已接纳。所选版本已退出新复用，精确历史与原始内容仍可阅读。";
                    setNotice(message);
                    await refreshAfterAcceptedCommand(message, versionRef);
                  } catch (caught) {
                    const failure = caught instanceof ProductError ? caught : new ProductError("unknown_error");
                    setRetirementFailure(failure);
                    setError(failure.code);
                    setLifecycleQueryTick((current) => current + 1);
                  } finally {
                    setBusy(null);
                  }
                }}
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

        </details>
        <footer className="asset-footer">
          <div aria-live="polite">
            <b>{error ? t("操作未完成", "Action incomplete") : t("文件保管状态", "File storage status")}</b>
            <small className={error ? "error" : ""}>{error ?? (notice === "盘点来自 Research Memory 公开 Query；浏览不会写 Owner。" ? t("原件按精确版本保留，浏览不会改变已保存内容。", "Originals are preserved by exact version. Browsing does not change saved content.") : notice)}</small>
          </div>
          <button type="button" onClick={close}>{t("完成", "Done")}</button>
        </footer>
      </div>
    </dialog>
  );
}

function ChangeFields({ draft, items, busy, onChange }: {
  draft: ChangeDraft;
  items: ResearchAssetItem[];
  busy: boolean;
  onChange: (draft: ChangeDraft) => void;
}) {
  return (
    <fieldset className="asset-change-fields" disabled={busy}>
      <legend>说明版本变更</legend>
      <label>
        <span>变更类型</span>
        <select aria-label="资产变更类型" value={draft.kind} onChange={(event) => onChange({ ...draft, kind: event.target.value as ChangeDraft["kind"] })}>
          <option value="supplement">补充</option>
          <option value="substantive_change">实质变更</option>
          <option value="correction">更正</option>
        </select>
      </label>
      <label>
        <span>变更说明与影响</span>
        <textarea aria-label="资产变更说明" rows={3} value={draft.explanation} onChange={(event) => onChange({ ...draft, explanation: event.target.value })} required />
      </label>
      {draft.kind === "correction" ? (
        <>
          <label>
            <span>发现的错误</span>
            <textarea aria-label="更正错误说明" rows={2} value={draft.error} onChange={(event) => onChange({ ...draft, error: event.target.value })} required />
          </label>
          <label>
            <span>更正范围</span>
            <textarea aria-label="更正范围" rows={2} value={draft.scope} onChange={(event) => onChange({ ...draft, scope: event.target.value })} required />
          </label>
          <details open className="asset-evidence-picker">
            <summary>更正依据的精确版本</summary>
            <small>所选版本的内容哈希、manifest 哈希和接纳凭据会随更正提交。</small>
            {items.map((item) => (
              <label className="asset-check" key={item.memory_ref}>
                <input
                  type="checkbox"
                  aria-label={`更正依据 ${item.memory_ref}`}
                  checked={draft.evidenceRefs.includes(item.memory_ref)}
                  onChange={(event) => onChange({
                    ...draft,
                    evidenceRefs: event.target.checked
                      ? [...draft.evidenceRefs, item.memory_ref]
                      : draft.evidenceRefs.filter((ref) => ref !== item.memory_ref),
                  })}
                />
                <span>{item.display_name} · v{item.version_number}<small>{item.memory_ref}</small></span>
              </label>
            ))}
          </details>
          <div className="asset-impact-fields">
            <small>逐项说明受影响工作。影响尚未查明时，添加实际工作并选“待核实”。</small>
            {!draft.impact.length ? (
              <label>
                <span>无受影响工作核查说明</span>
                <textarea aria-label="无受影响工作核查说明" rows={3} value={draft.noAffectedWorkExplanation} onChange={(event) => onChange({ ...draft, noAffectedWorkExplanation: event.target.value })} required />
                <small>写明已核查的工作范围，以及判断没有受影响工作的理由。</small>
              </label>
            ) : null}
            {draft.impact.map((impact, index) => (
              <fieldset key={index}>
                <legend>受影响工作 {index + 1}</legend>
                <label>
                  <span>工作引用</span>
                  <input aria-label={`影响工作 ${index + 1}`} value={impact.work_ref} onChange={(event) => onChange({ ...draft, impact: draft.impact.map((row, position) => position === index ? { ...row, work_ref: event.target.value } : row) })} required />
                </label>
                <label>
                  <span>影响判断</span>
                  <select aria-label={`影响判断 ${index + 1}`} value={impact.judgment} onChange={(event) => onChange({ ...draft, impact: draft.impact.map((row, position) => position === index ? { ...row, judgment: event.target.value as AssetChangeImpact["judgment"] } : row) })}>
                    <option value="unknown">待核实</option>
                    <option value="unaffected">不受影响</option>
                    <option value="recheck">需要复核</option>
                    <option value="redo">需要重做</option>
                  </select>
                </label>
                <label>
                  <span>判断理由</span>
                  <textarea aria-label={`影响理由 ${index + 1}`} rows={2} value={impact.explanation} onChange={(event) => onChange({ ...draft, impact: draft.impact.map((row, position) => position === index ? { ...row, explanation: event.target.value } : row) })} required />
                </label>
                <button type="button" onClick={() => onChange({ ...draft, impact: draft.impact.filter((_, position) => position !== index) })}>移除这项工作</button>
              </fieldset>
            ))}
            <button type="button" onClick={() => onChange({ ...draft, impact: [...draft.impact, { work_ref: "", judgment: "unknown", explanation: "" }] })}>添加受影响工作</button>
          </div>
        </>
      ) : null}
    </fieldset>
  );
}

const retirementChecks = [
  { key: "low_value", label: "已确认这份内容价值低" },
  { key: "obsolete", label: "已确认这份内容已过时" },
  { key: "incorrect", label: "已确认这份内容有误" },
  { key: "impact_understood", label: "已核清对相关工作的影响" },
  { key: "no_explanation_value", label: "已确认没有需要继续保留的解释价值" },
] as const;

const impactLabels: Record<AssetChangeImpact["judgment"], string> = {
  unaffected: "不受影响",
  recheck: "需要复核",
  redo: "需要重做",
  unknown: "待核实",
};

function AssetLifecyclePanel({ item, lifecycle, busy, failure, onVersion, onRetire }: {
  item: ResearchAssetItem;
  lifecycle: AssetLifecycle | null;
  busy: boolean;
  failure: ProductError | null;
  onVersion: (versionRef: string) => void;
  onRetire: (explanation: string) => Promise<void>;
}) {
  const [explanation, setExplanation] = useState("");
  const [confirmed, setConfirmed] = useState<string[]>([]);
  const selected = lifecycle?.versions.find((version) => version.version_ref === item.memory_ref);
  const currentVersionRef = lifecycle?.current_version_ref;
  const predecessorVersionRef = selected?.predecessor_version_ref;
  const allConfirmed = retirementChecks.every((check) => confirmed.includes(check.key));
  const strings = (key: string) => {
    const value = failure?.details?.[key];
    return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === "string") : [];
  };
  return (
    <section className="asset-lifecycle" aria-label="资产当前版本与保留历史">
      <div className="asset-lifecycle-state" data-state={selected?.state}>
        <small>所选精确版本</small>
        <b>{selected ? lifecycleLabels[selected.state] : "正在读取版本状态…"}</b>
        <code>{item.memory_ref}</code>
      </div>
      {lifecycle ? (
        <>
          <p className="asset-current-pointer">
            <span>当前复用入口</span>
            {currentVersionRef ? (
              currentVersionRef === item.memory_ref
                ? <strong>指向所选版本</strong>
                : <button type="button" disabled={busy} onClick={() => onVersion(currentVersionRef)}>打开当前版本</button>
            ) : <strong>暂无当前可复用版本</strong>}
          </p>
          <small>旧版本与退役版本保留原始内容及变更说明。下载始终读取所选精确版本。</small>
          {predecessorVersionRef ? (
            <button type="button" className="asset-version-link" disabled={busy} onClick={() => onVersion(predecessorVersionRef)}>打开前序版本 · {predecessorVersionRef}</button>
          ) : null}
          {selected?.successor_version_refs.map((ref) => (
            <button key={ref} type="button" className="asset-version-link" disabled={busy} onClick={() => onVersion(ref)}>打开后继版本 · {ref}</button>
          ))}
          <details className="asset-lifecycle-history" open>
            <summary>保留历史 · {lifecycle.versions.length} 个版本</summary>
            {lifecycle.versions.map((version) => (
              <article key={version.version_ref}>
                <button type="button" className="asset-version-link" aria-current={version.version_ref === item.memory_ref ? "true" : undefined} disabled={busy} onClick={() => onVersion(version.version_ref)}>{version.version_ref}</button>
                <b>{lifecycleLabels[version.state]}</b>
                {version.changes.map((change) => (
                  <div className="asset-change-fact" key={change.change_ref}>
                    <strong>{changeLabels[change.kind]}</strong>
                    <p>{change.explanation}</p>
                    {change.error ? <p>错误说明 · {change.error}</p> : null}
                    {change.scope ? <p>更正范围 · {change.scope}</p> : null}
                    {change.no_affected_work_explanation ? <p>无受影响工作核查 · {change.no_affected_work_explanation}</p> : null}
                    {change.evidence_bindings?.length ? (
                      <details>
                        <summary>精确更正依据</summary>
                        {change.evidence_bindings.map((binding) => (
                          <dl key={binding.version_ref}>
                            <div><dt>版本</dt><dd><button type="button" className="asset-version-link" disabled={busy} onClick={() => onVersion(binding.version_ref)}>{binding.version_ref}</button></dd></div>
                            <div><dt>内容哈希</dt><dd>{binding.content_hash}</dd></div>
                            <div><dt>manifest</dt><dd>{binding.manifest_hash}</dd></div>
                            <div><dt>凭据</dt><dd>{binding.receipt.receipt_ref}</dd></div>
                          </dl>
                        ))}
                      </details>
                    ) : null}
                    {change.impact?.map((impact) => <p key={impact.work_ref}>{impact.work_ref} · {impactLabels[impact.judgment]} · {impact.explanation}</p>)}
                    <small>变更凭据 · {change.receipt.receipt_ref}</small>
                  </div>
                ))}
              </article>
            ))}
          </details>
          {selected && selected.state !== "retired" ? (
            <details className="asset-retirement">
              <summary>谨慎退役所选版本</summary>
              <p>确认理由应包含核查范围、实际依据与受影响工作。失败、负结果或目标变化本身不构成退役理由。退役会阻止新复用，历史内容仍保留。</p>
              <form onSubmit={(event) => { event.preventDefault(); if (!busy && allConfirmed && explanation.trim()) void onRetire(explanation.trim()); }}>
                <label>
                  <span>退役理由与核查范围</span>
                  <textarea aria-label="退役理由与核查范围" rows={3} value={explanation} disabled={busy} onChange={(event) => setExplanation(event.target.value)} required />
                </label>
                {retirementChecks.map((check) => (
                  <label className="asset-check" key={check.key}>
                    <input type="checkbox" disabled={busy} checked={confirmed.includes(check.key)} onChange={(event) => setConfirmed((current) => event.target.checked ? [...current, check.key] : current.filter((key) => key !== check.key))} />
                    <span>{check.label}</span>
                  </label>
                ))}
                <button className="asset-retire-button" type="submit" disabled={busy || !allConfirmed || !explanation.trim()}>提交版本退役</button>
              </form>
            </details>
          ) : null}
          {failure ? (
            <div className="asset-retirement-failure" role="alert">
              <b>退役未接纳 · {failure.code}</b>
              {strings("reasons").map((reason) => <p key={reason}>{reason}</p>)}
              {strings("active_reference_refs").map((ref) => <p key={ref}>仍在引用 · {ref}</p>)}
              {strings("active_hold_refs").map((ref) => <p key={ref}>仍有保留要求 · {ref}</p>)}
              <small>已重新读取版本状态。请核查阻止原因后再提交。</small>
            </div>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

function AssetDetail({
  item,
  lifecycle,
  retirementFailure,
  onVersion,
  onRetire,
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
  lifecycle: AssetLifecycle | null;
  retirementFailure: ProductError | null;
  onVersion: (versionRef: string) => void;
  onRetire: (explanation: string) => Promise<void>;
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
      <AssetLifecyclePanel
        key={item.memory_ref}
        item={item}
        lifecycle={lifecycle}
        busy={busy !== null}
        failure={retirementFailure}
        onVersion={onVersion}
        onRetire={onRetire}
      />
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
        <button type="button" onClick={onRoleAccept} disabled={busy !== null || !questRef || lifecycle?.versions.find((version) => version.version_ref === item.memory_ref)?.state === "retired"}>由 RG 接纳角色</button>
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
