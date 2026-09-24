"""Light driver for one root-owned Target lifecycle.

One native Target root Session owns method implementation, evidence collection,
evaluation, and result-driven revision.  The daemon does not model those activities as
phases and does not launch a second execution service.  It only prepares the
root scope, resumes or reconciles one Harness turn, and hands one final closed
envelope to the finalizer.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from meta_research.database import Database
from meta_research.bundle_protocol import (
    FormalPlan,
    TargetCandidate,
    TargetWorkHandle,
    projection_plain_value,
)
from meta_research.target_execution_contract import (
    target_execution_context, target_execution_skill_text,
)
from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.context_presentation import bounded_text
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.agent_runtime_harness import (
    TARGET_ROOT_RECOVERY_PENDING_CODE,
    TARGET_ROOT_RECOVERY_READY_CODE,
    TargetRootCompletionEvidence,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.target_root_lifecycle import (
    SQLiteTargetRootLifecycleAuthority,
)
from meta_research.owners.target_run_runtime import (
    SQLiteTargetRunAgentAuthority,
    SQLiteTargetRunGraphAuthority,
    canonical_target_scope_binding,
)


_PROVIDER_CEILING_FAILURE_CODES = frozenset(
    {
        "provider_timeout",
        "provider_output_limit",
        "provider_descendant_process",
    }
)


class TargetRunFinalizerInterface(Protocol):
    """Single deep post-root boundary; its internal saga is crash-replayable."""

    def materialize_inputs(self, *, handle: TargetWorkHandle) -> tuple[str, ...]: ...

    def finalize(
        self,
        *,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class TargetRunRuntimeStatus:
    target_ref: str
    phase: str
    pending_code: str | None = None
    next_retry_at: float | None = None


class TargetRunRuntime:
    """Wake and resume one Target root; never interpret its internal loop."""

    def __init__(
        self,
        *,
        agent_runtime: AgentRuntimeInterface,
        research_graph: ResearchGraphInterface,
        target_graph: SQLiteTargetRunGraphAuthority,
        target_agent: SQLiteTargetRunAgentAuthority,
        target_root_lifecycle: SQLiteTargetRootLifecycleAuthority,
        harnesses: HarnessRuntime,
        finalizer: TargetRunFinalizerInterface,
        harness_family: str = "codex",
        database: Database | None = None,
    ) -> None:
        self._agent_runtime = agent_runtime
        self._database = database
        self._research_graph = research_graph
        self._target_graph = target_graph
        self._target_agent = target_agent
        self._target_root_lifecycle = target_root_lifecycle
        self._harnesses = harnesses
        self._finalizer = finalizer
        self._harness_family = harness_family
        self._mcp_base_url: str | None = None
        self._status_by_target: dict[str, TargetRunRuntimeStatus] = {}

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        if not isinstance(base_url, str) or not base_url:
            raise OwnerConflict("target_run_mcp_endpoint_invalid")
        self._mcp_base_url = base_url.rstrip("/")

    def query_status(self, target_ref: str) -> TargetRunRuntimeStatus | None:
        return self._status_by_target.get(target_ref)

    def cleanup_completed_workspaces(self, *, dry_run=True, now=None, limit=10, retention_seconds=None):
        from meta_research.target_workspace_cleanup import cleanup_completed_workspaces
        return cleanup_completed_workspaces(self, dry_run=dry_run, now=now,
            limit=limit, retention_seconds=retention_seconds)

    def has_pending_cancel(self, target_ref: str) -> bool:
        """Expose only the mechanical wake bit, never the operator's reason."""

        lifecycle = self._target_root_lifecycle.query(target_ref)
        return bool(
            lifecycle is not None
            and lifecycle.status == "running"
            and getattr(lifecycle, "cancel_ref", None) is not None
        )

    def process_once(self, target_ref: str) -> bool:
        """Wake one root turn or replay its one finalization boundary."""

        launch = self._agent_runtime.query_admitted_target_launch(target_ref)
        if launch is None:
            self._set_status(target_ref, "launch_pending")
            return False
        # One immutable read cut lets the projection-level snapshot
        # caches deduplicate the shared verification subtrees inside
        # this pass instead of re-proving them per query.
        read_cut = (
            self._database.read_snapshot()
            if self._database is not None else nullcontext()
        )
        with read_cut:
            candidate_projection = (
                self._research_graph.query_target_candidate_projection(
                    target_ref=target_ref
                )
            )
            formal_plan_projection = (
                self._research_graph.query_target_formal_plan_projection(
                    graph_ref=launch.graph_ref
                )
            )
        if candidate_projection is None or formal_plan_projection is None:
            raise OwnerConflict("target_run_projection_required")
        candidate = candidate_projection.candidate
        formal_plan = formal_plan_projection.formal_plan
        scope_binding = canonical_target_scope_binding(
            target_ref=target_ref,
            target_run_ref=launch.target_run_ref,
            target_spec_hash=launch.request.target_spec_binding.content_hash_ref,
            candidate=candidate,
            formal_plan=formal_plan,
            accepted_input_refs=tuple(
                sorted(
                    (
                        *launch.request.accepted_input_target_commit_refs,
                        *launch.request.accepted_input_asset_refs,
                    )
                )
            ),
        )

        harness = self._target_agent.query_target_harness_admission(target_ref)
        if harness is None:
            try:
                self._harnesses.admit_target_run_from_current_binding(
                    target_ref=target_ref,
                    target_run_ref=launch.target_run_ref,
                    harness_family=self._harness_family,  # type: ignore[arg-type]
                    target_scope_binding=scope_binding,
                )
            except HarnessAdmissionError as error:
                self._set_status(
                    target_ref,
                    "harness_pending",
                    error.code,
                    next_retry_at=error.next_retry_at,
                )
                return False
            self._set_status(target_ref, "harness_admitted")
            return True

        lifecycle = self._target_root_lifecycle.query(target_ref)
        if lifecycle is not None and lifecycle.status == "completed":
            self._set_status(target_ref, "completed")
            return False
        if lifecycle is not None and lifecycle.status == "cancelled":
            self._set_status(target_ref, "cancelled")
            return False
        if (
            lifecycle is not None
            and lifecycle.status == "running"
            and getattr(lifecycle, "cancel_ref", None) is not None
        ):
            # Operator cancellation bypasses provider/channel retry policy.
            # A terminal failed operation is already drained, so Harness can
            # acknowledge it without recovering or invoking the provider.
            try:
                acknowledged = self._harnesses.cancel_target_root(
                    harness.harness_request_ref
                )
            except HarnessAdmissionError as error:
                self._set_status(
                    target_ref,
                    "cancel_pending",
                    error.code,
                    next_retry_at=error.next_retry_at,
                )
                return False
            if not acknowledged:
                self._set_status(target_ref, "cancel_pending")
                return False
            cancelled = self._target_root_lifecycle.mark_cancelled(
                target_ref=target_ref
            )
            if cancelled.status != "cancelled":
                raise OwnerConflict("target_root_cancel_integrity_invalid")
            self._set_status(target_ref, "cancelled")
            return True

        if getattr(harness, "status", None) == "suspended":
            # The issuing Owner parks a root while its HumanRequest is open.
            # Wait before asking for execution authority or touching inputs or
            # results; only the Owner's consumed disposition may resume it.
            # Cancellation above remains available while the root is parked.
            self._set_status(
                target_ref, "harness_suspended", "runtime_run_suspended"
            )
            return False

        if (
            getattr(harness, "status", None) == "running"
            and getattr(harness, "failure_code", None)
            in _PROVIDER_CEILING_FAILURE_CODES
        ):
            # Read the current issuer-verified handle before Harness rotates
            # Session/Attempt/Fence.  recover_fenced_target_root performs the
            # stricter durable operation/receipt checks; this driver merely
            # selects the three signed physical-ceiling outcomes and never
            # interprets ordinary provider or unknown-outcome failures as one.
            handle = self._target_agent.query_current_target_work_handle(
                target_ref
            )
            if handle is None:
                raise OwnerConflict("target_run_handle_unavailable")
            try:
                self._harnesses.recover_fenced_target_root(
                    harness.harness_request_ref,
                    old_handle=handle,
                )
            except HarnessAdmissionError as error:
                self._set_status(
                    target_ref,
                    "harness_ceiling_recovery_pending",
                    error.code,
                    next_retry_at=error.next_retry_at,
                )
                return False
            self._set_status(target_ref, "harness_ceiling_recovered")
            return True

        if getattr(harness, "failure_code", None) == (
            TARGET_ROOT_RECOVERY_PENDING_CODE
        ):
            # A crash may land between successor reservation and fresh channel
            # activation.  Resume that exact reserved generation; the channel
            # issuer changes the marker to READY before AR may bind or CAS it.
            try:
                self._harnesses.recover_failed_target_root(
                    harness.harness_request_ref
                )
            except HarnessAdmissionError as error:
                self._set_status(
                    target_ref,
                    "harness_ceiling_channel_pending",
                    error.code,
                    next_retry_at=error.next_retry_at,
                )
                return False
            self._set_status(target_ref, "harness_ceiling_channel_ready")
            return True

        if getattr(harness, "failure_code", None) == (
            TARGET_ROOT_RECOVERY_READY_CODE
        ):
            frontier = self._agent_runtime.query_target_frontier_entry(target_ref)
            old_handle = getattr(frontier, "current_handle", None)
            if type(old_handle) is not TargetWorkHandle:
                raise OwnerConflict("target_run_handle_unavailable")
            successor_identity = (
                harness.target_run_ref,
                harness.root_session_ref,
                harness.execution_attempt_ref,
                harness.execution_fence_ref,
            )
            frontier_identity = (
                old_handle.target_run_ref,
                old_handle.root_session_ref,
                old_handle.execution_attempt_ref,
                old_handle.execution_fence_ref,
            )
            if frontier_identity != successor_identity:
                input_binding = (
                    self._target_graph.query_execution_input_binding_for_attempt(
                        target_ref=target_ref,
                        target_run_ref=harness.target_run_ref,
                        target_attempt_ref=harness.execution_attempt_ref,
                        target_fence_ref=harness.execution_fence_ref,
                    )
                )
                if input_binding is None:
                    self._target_graph.accept_execution_input_binding(
                        target_ref=target_ref,
                        target_run_ref=harness.target_run_ref,
                        target_attempt_ref=harness.execution_attempt_ref,
                        target_fence_ref=harness.execution_fence_ref,
                        target_spec_hash=(
                            launch.request.target_spec_binding.content_hash_ref
                        ),
                        target_scope_binding_hash=canonical_hash(scope_binding),
                        input_refs=tuple(
                            sorted(
                                (
                                    *launch.request.accepted_input_target_commit_refs,
                                    *launch.request.accepted_input_asset_refs,
                                )
                            )
                        ),
                        idempotency_key=(
                            "target-root-input:"
                            + target_ref
                            + ":"
                            + harness.execution_attempt_ref
                        ),
                    )
                    self._set_status(
                        target_ref,
                        "harness_ceiling_input_scope_bound",
                    )
                    return True
                replacement = self._target_root_lifecycle.recover_provider_ceiling_successor(
                    old_handle=old_handle,
                    idempotency_key=(
                        "target-root-provider-recovery:"
                        + target_ref
                        + ":"
                        + harness.execution_attempt_ref
                    ),
                )
                if (
                    replacement.target_run_ref,
                    replacement.root_session_ref,
                    replacement.execution_attempt_ref,
                    replacement.execution_fence_ref,
                ) != successor_identity:
                    raise OwnerConflict(
                        "target_root_provider_recovery_integrity_invalid"
                    )
                self._set_status(target_ref, "harness_ceiling_frontier_recovered")
                return True

        if getattr(harness, "status", None) == "failed" or (
            getattr(harness, "failure_code", None)
            == TARGET_ROOT_RECOVERY_PENDING_CODE
        ):
            # A deterministic provider/admission failure is a mechanical
            # Harness condition, not a scientific Target phase.  The Harness
            # deep seam owns its fenced ledger checks and channel restoration;
            # this light driver never continues input work, finalization, or a
            # provider turn in the same wake.
            try:
                self._harnesses.recover_failed_target_root(
                    harness.harness_request_ref
                )
            except HarnessAdmissionError as error:
                self._set_status(
                    target_ref,
                    "harness_recovery_pending",
                    error.code,
                    next_retry_at=error.next_retry_at,
                )
                return False
            self._set_status(target_ref, "harness_recovered")
            return True

        input_binding = self._target_graph.query_execution_input_binding_for_attempt(
            target_ref=target_ref,
            target_run_ref=harness.target_run_ref,
            target_attempt_ref=harness.execution_attempt_ref,
            target_fence_ref=harness.execution_fence_ref,
        )
        if input_binding is None:
            # This is a read-scope binding for already accepted inputs.  No
            # implementation, training result, or Target output crosses into
            # RM/RG here.
            self._target_graph.accept_execution_input_binding(
                target_ref=target_ref,
                target_run_ref=harness.target_run_ref,
                target_attempt_ref=harness.execution_attempt_ref,
                target_fence_ref=harness.execution_fence_ref,
                target_spec_hash=(
                    launch.request.target_spec_binding.content_hash_ref
                ),
                target_scope_binding_hash=canonical_hash(scope_binding),
                input_refs=tuple(
                    sorted(
                        (
                            *launch.request.accepted_input_target_commit_refs,
                            *launch.request.accepted_input_asset_refs,
                        )
                    )
                ),
                idempotency_key="target-root-input:" + target_ref,
            )
            self._set_status(target_ref, "input_scope_bound")
            return True

        handle = self._target_agent.query_current_target_work_handle(target_ref)
        if handle is None:
            raise OwnerConflict("target_run_handle_unavailable")
        workspace = self._target_agent.query_target_workspace(handle.target_run_ref)
        if workspace is None:
            self._target_agent.reserve_target_workspace(
                handle=handle,
                idempotency_key=(
                    "target-root-workspace:"
                    + target_ref
                    + ":"
                    + handle.execution_attempt_ref
                ),
            )
            self._set_status(target_ref, "workspace_reserved")
            return True

        frozen_input_paths = self._finalizer.materialize_inputs(handle=handle)
        if lifecycle is None:
            self._target_root_lifecycle.activate(
                launch_ref=launch.launch_ref,
                handle=handle,
                candidate=candidate,
                formal_plan=formal_plan,
                idempotency_key="target-root-activate:" + target_ref,
            )
            self._set_status(target_ref, "root_activated")
            return True
        completion = self._harnesses.query_target_root_completion_evidence(target_ref)
        if completion is not None:
            revision = self._finalize(handle=handle, evidence=completion)
            if revision is not None:
                return self._resume_after_rejection(
                    handle=handle,
                    harness_request_ref=harness.harness_request_ref,
                    candidate=candidate,
                    formal_plan=formal_plan,
                    launch=launch,
                    frozen_input_manifest_path=(
                        frozen_input_paths[0] if frozen_input_paths else None
                    ),
                    revision=revision,
                    source_spec_hash=candidate_projection.source_spec_hash,
                )
            return True

        if self._mcp_base_url is None:
            self._set_status(
                target_ref,
                "root_pending",
                "target_run_mcp_endpoint_unavailable",
            )
            return False
        # The authority binds the accepted source spec. The launch binding
        # identifies its execution projection and therefore has a different hash.
        prompt = self._root_prompt(
            research_context=self._reading_research_context(handle.target_ref,
                frozen_input_paths[0] if frozen_input_paths else None),
            execution_contract=target_execution_context(
                authority=self._research_graph.query_target_measurement_domain_authority(handle.target_ref),
                target_ref=handle.target_ref,
                graph_ref=launch.graph_ref,
                target_spec_hash=candidate_projection.source_spec_hash,
            ),
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            launch=launch,
            frozen_input_manifest_path=(
                frozen_input_paths[0] if frozen_input_paths else None
            ),
        )
        try:
            self._harnesses.run_or_resume_target_root(
                harness.harness_request_ref,
                prompt=prompt,
                mcp_base_url=self._mcp_base_url,
            )
        except HarnessAdmissionError as error:
            self._set_status(
                target_ref,
                "root_pending",
                error.code,
                next_retry_at=error.next_retry_at,
            )
            return False

        completion = self._harnesses.query_target_root_completion_evidence(target_ref)
        if completion is not None:
            revision = self._finalize(handle=handle, evidence=completion)
            if revision is not None:
                return self._resume_after_rejection(
                    handle=handle,
                    harness_request_ref=harness.harness_request_ref,
                    candidate=candidate,
                    formal_plan=formal_plan,
                    launch=launch,
                    frozen_input_manifest_path=(
                        frozen_input_paths[0] if frozen_input_paths else None
                    ),
                    revision=revision,
                    source_spec_hash=candidate_projection.source_spec_hash,
                )
            return True
        self._set_status(target_ref, "root_turn_completed")
        return True

    def _finalize(
        self,
        *,
        handle: TargetWorkHandle,
        evidence: TargetRootCompletionEvidence,
    ) -> object | None:
        result = self._finalizer.finalize(handle=handle, evidence=evidence)
        status = getattr(result, "status", None)
        if status not in {"rm_accepted", "revision_required", "completed"}:
            raise OwnerConflict("target_root_finalization_invalid")
        pending_code = getattr(result, "pending_code", None)
        if pending_code is not None and type(pending_code) is not str:
            raise OwnerConflict("target_root_finalization_invalid")
        completion_ref = getattr(result, "completion_ref", None)
        target_commit_ref = getattr(result, "target_commit_ref", None)
        if type(completion_ref) is not str or not completion_ref:
            raise OwnerConflict("target_root_finalization_invalid")
        if status == "rm_accepted":
            if target_commit_ref is not None or not pending_code:
                raise OwnerConflict("target_root_finalization_invalid")
        elif status == "revision_required":
            generation = getattr(result, "completion_generation", None)
            rejection_ref = getattr(result, "rejection_ref", None)
            rejection_issuer = getattr(result, "rejection_issuer", None)
            rejection_feedback = getattr(result, "rejection_feedback", None)
            manifest_ref = getattr(result, "manifest_ref", None)
            if (
                target_commit_ref is not None
                or not pending_code
                or type(generation) is not int
                or isinstance(generation, bool)
                or generation < 1
                or type(rejection_ref) is not str
                or not rejection_ref
                or rejection_issuer not in {"research_memory", "research_graph"}
                or type(rejection_feedback) is not str
                or not rejection_feedback
                or (
                    manifest_ref is not None
                    and (type(manifest_ref) is not str or not manifest_ref)
                )
            ):
                raise OwnerConflict("target_root_finalization_invalid")
            self._set_status(handle.target_ref, "revision_required", pending_code)
            return result
        else:
            if (
                type(target_commit_ref) is not str
                or not target_commit_ref
                or pending_code is not None
            ):
                raise OwnerConflict("target_root_finalization_invalid")
            self._agent_runtime.publish_target_root_completion(
                target_ref=handle.target_ref,
                completion_ref=completion_ref,
                target_commit_ref=target_commit_ref,
            )
        self._set_status(
            handle.target_ref,
            "finalizing" if status == "rm_accepted" else "completed",
            pending_code,
        )
        return None

    def _resume_after_rejection(
        self,
        *,
        handle: TargetWorkHandle,
        harness_request_ref: str,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
        launch: object,
        frozen_input_manifest_path: str | None,
        revision: object,
        source_spec_hash: str,
    ) -> bool:
        """Wake the same native root Session with one issuer-owned rejection."""

        if self._mcp_base_url is None:
            self._set_status(
                handle.target_ref,
                "revision_required",
                "target_run_mcp_endpoint_unavailable",
            )
            return False
        revision_request = {
            "completion_generation": getattr(revision, "completion_generation"),
            "completion_ref": getattr(revision, "completion_ref"),
            "manifest_ref": getattr(revision, "manifest_ref"),
            "rejection_ref": getattr(revision, "rejection_ref"),
            "issuer": getattr(revision, "rejection_issuer"),
            "code": getattr(revision, "pending_code"),
            "feedback": getattr(revision, "rejection_feedback"),
        }
        prompt = self._root_prompt(
            research_context=self._reading_research_context(handle.target_ref, frozen_input_manifest_path),
            execution_contract=target_execution_context(
                authority=self._research_graph.query_target_measurement_domain_authority(handle.target_ref),
                target_ref=handle.target_ref,
                graph_ref=launch.graph_ref,
                target_spec_hash=source_spec_hash,
            ),
            handle=handle,
            candidate=candidate,
            formal_plan=formal_plan,
            launch=launch,
            frozen_input_manifest_path=frozen_input_manifest_path,
            revision_request=revision_request,
        )
        try:
            self._harnesses.run_or_resume_target_root(
                harness_request_ref,
                prompt=prompt,
                mcp_base_url=self._mcp_base_url,
            )
        except HarnessAdmissionError as error:
            self._set_status(
                handle.target_ref,
                "revision_required",
                error.code,
                next_retry_at=error.next_retry_at,
            )
            return False
        self._set_status(handle.target_ref, "root_revision_turn_completed")
        return True

    def _reading_research_context(self, target_ref, frozen_input_manifest_path):
        # Research explanations can acquire later immutable versions. Keep
        # these optional reading files beside, never inside, the exact input
        # manifest whose bytes an already-running Target must retain.
        directory = (Path(frozen_input_manifest_path).parent.parent /
                     "research-note-context" / target_ref
                     if frozen_input_manifest_path else None)
        reader = getattr(self._target_graph, "query_target_reading_context", None)
        if callable(reader):
            return reader(target_ref=target_ref, research_note_directory=directory)
        return self._target_graph.query_target_research_context(target_ref=target_ref)

    @staticmethod
    def _root_prompt(
        *,
        execution_contract: dict[str, object],
        handle: TargetWorkHandle,
        candidate: TargetCandidate,
        formal_plan: FormalPlan,
        launch: object,
        frozen_input_manifest_path: str | None,
        revision_request: dict[str, object] | None = None,
        research_context: dict[str, object] | None = None,
    ) -> str:
        research = dict(research_context or {})
        question = research.get("question")
        if isinstance(question, dict):
            research["question"] = {name: bounded_text(value, max_bytes=2048)
                for name, value in question.items()}
        research["exact_context_path"] = (str(Path(frozen_input_manifest_path).with_name("research-context.json"))
            if frozen_input_manifest_path and research_context else None)
        for name in ("plan_notes", "bundle_notes"):
            if name in research:
                research[name] = bounded_text(research[name], max_bytes=4096)
        material = {
            "research_context": research,
            "execution_contract": execution_contract,
            "handle": {name: getattr(handle, name) for name in (
                "target_ref", "target_run_ref", "root_session_ref", "execution_attempt_ref",
                "execution_fence_ref", "execution_input_binding_ref", "recoverable")},
            "candidate": projection_plain_value(candidate),
            "formal_plan": {"formal_plan_ref": formal_plan.formal_plan_ref,
                "briefs": [projection_plain_value(brief) for brief in formal_plan.briefs
                    if brief.experiment_key in candidate.experiment_keys]},
            "target_spec_binding": projection_plain_value(
                getattr(launch, "request").target_spec_binding
            ),
            "accepted_input_target_commit_refs": list(
                handle.accepted_input_target_commit_refs
            ),
            "accepted_input_asset_refs": [
                proof.asset_ref for proof in handle.accepted_input_asset_proofs
            ],
            "frozen_input_manifest_path": frozen_input_manifest_path,
            "owner_revision_request": revision_request,
            "completion_binding": {
                "owner": "system",
                "required_workspace_paths": {
                    "implementation": "implementation",
                    "result": "outputs/result.json",
                },
                "optional_workspace_paths": {
                    "data": "outputs/data",
                    "checkpoint": "outputs/checkpoints",
                    "analysis": "outputs/analysis",
                    "log": "logs",
                },
                "result_document_fields": [
                    "schema_ref",
                    "metrics",
                    "result_disposition",
                ],
                "root_final_text": "exact text and UTF-8 sha256",
            },
        }
        return (
            target_execution_skill_text() + "\n\n"
            "本回合在下列 Owner 冻结范围内实施或恢复 Target。按上述 Skill 读取实际合同和所需参考，"
            "进行研究、检查与局部修订，并在最终交接前完成独立子智能体审阅。普通检索、实施和整理"
            "按需要委派，子智能体使用当前任务授予的工具与权限，按对象划分写入；根负责整体判断、"
            "停止决定、保存取舍与最终交接。正式 HumanRequest 归属于当前已认证根操作，"
            "执行身份的恢复或轮换由 Owner 根据真实执行事实处理。\n\n"
            "完成文件按 completion_binding 的固定路径交接。implementation/ 保存可核查的方法材料；"
            "当前工作与证据检查形成可交接结论后，再写 outputs/result.json 作为完成候选；"
            "在途观察写分析或日志，失败、阻塞和未评价保持真实状态，不把在途工作当已完成。"
            "result_schema 是初始表达指导；Protocol 指标集合、身份、来源与数值安全仍按实际合同核验。"
            "多个 Run、分离评价或待评价工作读取 Skill 的正式工作交接参考，使用 formal_runs 表达。\n\n"
            "实际训练或评价开始时连续保留完整 stdout 与 stderr，分别写入 logs/train.log 或 logs/eval.log；"
            "使用程序支持的无缓冲或行缓冲模式，如 Python -u 或 PYTHONUNBUFFERED=1。用 tee 镜像时，"
            "以 2>&1 合并输出并启用 pipefail，保留原进程退出码，同时报告日志写入失败。"
            "每次实施保留独立 train-*.log 或 eval-*.log，在途日志持续追加；准备检查按实际用途另行命名。"
            "没有相应进程时，相应日志保持不存在。日志用于观察，实际结果仍由 Owner 核验。\n\n"
            "frozen_input_manifest_path 是工作区之外的只读原件，以其绝对路径读取；inputs/manifest.json "
            "只是便捷指针。采用历史材料前读取所选 TargetCommit 的精确 manifest 及其中所需实现、"
            "数据和分析；上游正文提到的路径不等于已交付资产。必要输入缺失时记录精确来源与缺口，"
            "经当前 Owner 反馈或具体 HumanRequest 处理，继续可独立完成的已授权工作。\n\n"
            "owner_revision_request 存在时，按精确 issuer 反馈修订后继内容，保留被拒版本的绑定，"
            "仅重做变更失效的工作。最终用简洁正文交代结果、局限和未完成事项；Harness 绑定该回合的"
            "精确最终文本及 UTF-8 hash，Owner 从固定路径核实原件并构造内部 completion handoff。"
            "缺失或无效的必要产物沿真实反馈恢复修订；根不自行构造 Owner receipt 或执行身份。\n\n"
            "Exact Owner context:\n" + canonical_json(material)
        )

    def _set_status(
        self,
        target_ref: str,
        phase: str,
        pending_code: str | None = None,
        *,
        next_retry_at: float | None = None,
    ) -> None:
        self._status_by_target[target_ref] = TargetRunRuntimeStatus(
            target_ref=target_ref,
            phase=phase,
            pending_code=pending_code,
            next_retry_at=next_retry_at,
        )


__all__ = [
    "TargetRunFinalizerInterface",
    "TargetRunRuntime",
    "TargetRunRuntimeStatus",
]
