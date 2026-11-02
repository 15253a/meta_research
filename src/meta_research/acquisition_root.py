from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Callable

from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionItemResult,
    AcquisitionPreflightRequest,
    AcquisitionPreflightResult,
    AcquisitionProvider,
    AcquisitionRuntimeBinding,
    AcquisitionUnavailable,
    validate_item_results,
    validate_preflight_result,
)
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    ensure_transport_key,
    read_transport_envelope,
    write_transport_envelope,
)
from meta_research.root_capabilities import merge_root_capability_bindings
from meta_research.root_operation_diagnostics import (
    RootOperationDiagnosticRecorder,
)
from meta_research.root_resident_mcp import RootResidentMcpAuthority
from meta_research.root_workspace import RootWorkspaces


_ROOT_SESSION_RECEIPT_SCHEMA = "meta-research/acquisition-root-session/v1"


class CodexAcquisitionRootAdapter(AcquisitionProvider):
    """Durable download worker using the packaged Nature Downloader tools.

    The native root chooses and executes the lawful route. Owner custody still
    verifies the returned files; a second model acceptance pass is unnecessary.
    """

    def __init__(
        self,
        workspace: Path,
        delegate: AcquisitionProvider,
        *,
        executable: str = "codex",
        model_ref: str = CODEX_MODEL_REF,
        timeout_seconds: float | None = None,
        process_runner: Callable[
            [list[str], str, float | None], subprocess.CompletedProcess[str]
        ]
        | None = None,
        codex_home: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._delegate = delegate
        self._root = _AcquisitionCodexAdapter(
            workspace / "codex-root",
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
            codex_home=codex_home,
        )
        _key_path, self._transport_key = ensure_transport_key(self._workspace)

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        delegate = self._delegate.runtime_binding()
        delegate_hash = canonical_hash(delegate.as_dict())
        root_binding_hash = canonical_hash(self._root.runtime_binding().as_dict())
        return AcquisitionRuntimeBinding(
            provider_ref=(
                "meta_research.acquisition_root.CodexAcquisitionRootAdapter"
            ),
            provider_version=(
                "v4+tools-sha256:"
                + delegate_hash
                + "+root-sha256:"
                + root_binding_hash
            ),
            capability_bindings=merge_root_capability_bindings(
                (
                    "acquisition-root-session-durable",
                    "acquisition-codex-root-binding:sha256:"
                    + root_binding_hash,
                    "nature-downloader-tools:sha256:" + delegate_hash,
                    *delegate.capability_bindings,
                ),
                "acquisition",
            ),
        )

    def bind_resident_mcp_authority(
        self, authority: RootResidentMcpAuthority
    ) -> None:
        self._root.bind_resident_mcp_authority(authority)

    def bind_external_mcp(self, runtime) -> None:
        self._root.bind_external_mcp(runtime)

    @property
    def research_workspace_root(self) -> Path:
        return self._root.research_workspace_root

    def bind_workspaces(self, workspaces: RootWorkspaces) -> None:
        self._root.bind_workspaces(workspaces)

    def configure_resident_mcp_endpoint(self, base_url: str) -> None:
        self._root.configure_resident_mcp_endpoint(base_url)

    def preflight(
        self, request: AcquisitionPreflightRequest
    ) -> AcquisitionPreflightResult:
        if type(request.generation) is not int or request.generation < 1:
            raise AcquisitionUnavailable("acquisition_preflight_generation_invalid")
        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "status": {"enum": ["ready", "waiting_user", "unavailable"]},
                "browser_context_ref": {"type": ["string", "null"]},
                "reason_code": {"type": ["string", "null"]},
                "evidence": {"type": "object", "additionalProperties": False,
                    "properties": {key: {"type": "string"} for key in
                        ("configuration_health", "browser_control", "authorized_resource")},
                    "required": ["configuration_health", "browser_control", "authorized_resource"]},
            },
            "required": ["status", "browser_context_ref", "reason_code", "evidence"],
        }
        raw = self._work(
            session_ref=request.session_ref,
            job_ref=f"acquisition:{request.session_ref}:preflight:{request.config_hash}:{request.generation}",
            phase="preflight", schema=schema,
            task={"session_mode": request.mode, "library_entry_url": request.library_entry_url,
                  "provider_state_dir": request.private_state_dir,
                  "previous_browser_context_ref": request.previous_browser_context_ref},
            root_runtime_scope=request.root_runtime_scope,
        )
        try:
            result = AcquisitionPreflightResult(**raw)
        except TypeError as error:
            raise AcquisitionUnavailable("acquisition_root_result_invalid") from error
        return validate_preflight_result(request, result)

    def acquire(self, request: AcquisitionBatchRequest) -> tuple[AcquisitionItemResult, ...]:
        return self._acquire(request)

    def reconcile(self, request: AcquisitionBatchRequest) -> tuple[AcquisitionItemResult, ...]:
        # The same Owner request may resume this native in a new bounded attempt.
        # Its prior spool and files are reconciled before any download repeats.
        return self._acquire(request)

    def _acquire(self, request: AcquisitionBatchRequest) -> tuple[AcquisitionItemResult, ...]:
        if (request.session_mode not in {"oa_only", "provided_only", "oa_then_institution"}
            or not request.session_ref or not Path(request.target_dir).is_absolute()):
            raise AcquisitionUnavailable("acquisition_provider_binding_invalid")
        failure = {"type": "object", "additionalProperties": False,
                   "properties": {"code": {"type": "string"}, "detail": {"type": "string"}},
                   "required": ["code", "detail"]}
        item = {"type": "object", "additionalProperties": False,
                "properties": {"paper_id": {"type": "string"},
                    "status": {"enum": ["obtained", "waiting_user", "missing"]},
                    "path": {"type": ["string", "null"]},
                    "format": {"enum": ["pdf", "html", "xml", None]},
                    "failure": {"anyOf": [{"type": "null"}, failure]}},
                "required": ["paper_id", "status", "path", "format", "failure"]}
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"results": {"type": "array", "items": item}}, "required": ["results"]}
        scope = request.root_runtime_scope
        attempt = "unmanaged" if scope is None else str(scope["attempt_ref"])
        raw = self._work(session_ref=request.session_ref,
            job_ref=f"acquisition:{request.session_ref}:batch:{request.request_id}:{attempt}",
            phase="batch", schema=schema, root_runtime_scope=scope,
            task={"request_id": request.request_id, "session_mode": request.session_mode,
                  "route_policy": request.route_policy, "target_dir": request.target_dir,
                  "provider_state_dir": request.provider_state_dir,
                  "browser_context_ref": request.browser_context_ref,
                  "papers": [paper.as_dict() for paper in request.papers]})
        try:
            results = tuple(AcquisitionItemResult(**item) for item in raw["results"])
        except (KeyError, TypeError) as error:
            raise AcquisitionUnavailable("acquisition_root_result_invalid") from error
        return validate_item_results(request, results)

    def request_stop(self) -> None:
        self._root.request_stop()
        request_stop = getattr(self._delegate, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def bind_root_operation_diagnostics_recorder(
        self, recorder: RootOperationDiagnosticRecorder
    ) -> None:
        self._root.bind_root_operation_diagnostics_recorder(recorder)

    def _work(self, *, session_ref: str, job_ref: str, phase: str,
              task: dict[str, object], schema: dict[str, object],
              root_runtime_scope: object) -> dict[str, object]:
        previous_native_session_ref = self._latest_native_session_ref(session_ref)
        invocation_path = (
            self._root._workspace / "provider-operations"
            / canonical_hash({"job_ref": job_ref})
            / "acquisition-root-turn" / "invocation.json"
        )
        if invocation_path.exists():
            # A replay keeps its original native-session input even after a
            # later successful turn advances this Acquisition session.
            try:
                _key_path, key = self._root._transport_key()
                invocation = read_transport_envelope(invocation_path, key)
                previous = invocation.get("native_session_ref")
                if (
                    invocation.get("job_ref") != job_ref
                    or invocation.get("operation_name") != "acquisition-root-turn"
                    or (previous is not None and not isinstance(previous, str))
                ):
                    raise ProviderSupervisorError("acquisition_invocation_invalid")
                previous_native_session_ref = previous
            except (OSError, ProviderSupervisorError) as error:
                raise AcquisitionUnavailable("acquisition_root_session_invalid") from error
        prompt = self._prompt(phase, task, initial=previous_native_session_ref is None)
        replacement = None
        try:
            raw, native_session_ref, _stdout = self._invoke_turn(
                session_ref=session_ref, job_ref=job_ref, prompt=prompt, schema=schema,
                native_session_ref=previous_native_session_ref, task=task,
                root_runtime_scope=root_runtime_scope)
        except IdeaSkillUnavailable as error:
            checkpoint = error.recovery_checkpoint
            if (error.code != "codex_native_session_unavailable"
                or previous_native_session_ref is None
                or not isinstance(checkpoint, dict)
                or checkpoint.get("schema_ref") != "meta-research/provider-native-session-unavailable/v1"
                or checkpoint.get("native_session_ref") != previous_native_session_ref
                or checkpoint.get("error_code") != -32600):
                raise AcquisitionUnavailable(error.code) from error
            # Only the native bridge's signed, exact no-rollout rejection proves
            # this turn never started. Unknown effects stay on their old spool.
            replacement = {"job_ref": job_ref, "recovery_checkpoint": checkpoint}
            job_ref += ":replacement:" + canonical_hash(checkpoint)
            context = self._replacement_context(session_ref, job_ref, phase, task, replacement)
            prompt = self._prompt(phase, task, initial=True) + (
                "\n\n这是原native已确证永久不可用后的替代Session，不是连续resume。"
                "沿原工作区先读下载笔记与已有manifest，核实下列已存在文件；它们不是新的下载授权。"
                "只完成当前请求尚缺项，保留已完成文件，不重放任何未知效果。\n"
                "replacement_context=" + canonical_json(context))
            try:
                raw, native_session_ref, _stdout = self._invoke_turn(
                    session_ref=session_ref, job_ref=job_ref, prompt=prompt, schema=schema,
                    native_session_ref=None, task=task, root_runtime_scope=root_runtime_scope)
            except IdeaSkillUnavailable as replacement_error:
                # One bounded replacement job; reconciliation reads this same
                # invocation rather than recursively replacing a failed start.
                raise AcquisitionUnavailable(replacement_error.code) from replacement_error
        if native_session_ref is None or (
            replacement is not None and native_session_ref == previous_native_session_ref
        ):
            raise AcquisitionUnavailable("acquisition_root_result_invalid")
        self._append_session_receipt(session_ref=session_ref, native_session_ref=native_session_ref,
            previous_native_session_ref=previous_native_session_ref, job_ref=job_ref, phase=phase,
            replacement=replacement)
        return raw

    def _invoke_turn(self, *, session_ref, job_ref, prompt, schema, native_session_ref,
                     task, root_runtime_scope):
        return self._root._invoke_optional_root_task_operation(
            operation_name="acquisition-root-turn", prompt=prompt, schema=schema,
            native_session_ref=native_session_ref, job_ref=job_ref,
            root_runtime_scope=root_runtime_scope,
            execution_access_mode=str(task["session_mode"]),
            execution_write_root=Path(str(task.get("target_dir") or task["provider_state_dir"])),
            workspace_binding=(self._root._session_workspace(session_ref)
                               if root_runtime_scope is None else None))

    def _replacement_context(self, session_ref, job_ref, phase, task, replacement):
        # Freeze the handoff once. A completed replacement may add artifacts;
        # those must not change the sealed original input on reconciliation.
        path = self._session_directory(session_ref) / ("replacement-" + canonical_hash({"job_ref": job_ref}) + ".json")
        identity = {"session_ref": session_ref, "job_ref": job_ref, "phase": phase,
                    "task_hash": canonical_hash(task), "replacement": replacement}
        try:
            if path.exists():
                context = read_transport_envelope(path, self._transport_key)
                if any(context.get(key) != value for key, value in identity.items()):
                    raise ProviderSupervisorError("acquisition_replacement_context_invalid")
                return context
            artifacts = []
            truncated = False
            if task.get("target_dir"):
                target = Path(str(task["target_dir"])).resolve()
                for artifact in target.rglob("*"):
                    if artifact.is_file() and not artifact.is_symlink() and artifact.resolve().is_relative_to(target):
                        if len(artifacts) == 128:
                            truncated = True
                            break
                        artifacts.append({"path": str(artifact.resolve()), "size_bytes": artifact.stat().st_size})
            context = {**identity, "existing_artifacts": sorted(artifacts, key=lambda item: item["path"]),
                       "artifacts_truncated": truncated}
            path.parent.mkdir(parents=True, exist_ok=True)
            write_transport_envelope(path, context, self._transport_key)
            return context
        except (OSError, ProviderSupervisorError) as error:
            raise AcquisitionUnavailable("acquisition_root_session_invalid") from error

    @staticmethod
    def _prompt(phase, task, *, initial):
        provider_root = Path(__file__).with_name("skills") / "deepfetch_v4" / "providers" / "nature-downloader"
        skill_path = provider_root / "SKILL.md"
        prompt = (
            "你是长期专用 Acquisition 下载 Session。按本请求明确模式自主使用真实工具，"
            "取得并核实指定论文；逐篇交回实际状态。先查看本工作区已有记录，复用有效快捷路径，"
            "失效时仅修相关环节。普通笔记保留路径、适用条件与失败弯路，凭据留受控配置；"
            "笔记不建立跨账号授权。已完成文件不重新下载。\n"
            "若需要人处理访问，调用 human_request.open 并核对正式回执；waiting_user"
            "或求助文本不代表已投递。收到回应先沿原 effect 调用 human_request.open.reconcile，"
            "读取回应并复验连接，再接续未完成项。未知效果先检查既有 manifest／产物／操作。\n"
            f"Nature Downloader installed source: {skill_path.resolve()}\n"
            "脚本与相对链接均相对此目录；原生 shell/file 工具可直接运行脚本。"
            "压缩后若缺少获取规则，从该source重读完整Skill及触发的参考，保留同一工作区及现有产物。"
            "调用所有下载和配置脚本时将 LIT_DL_CONFIG_DIR 设为本请求 provider_state_dir/nature-downloader-config，"
            "使用这份私有配置；本次 provided_only 仅核实已提供正文，oa_only 传 --no-institutional-access。\n"
            f"phase={phase}\nrequest={canonical_json(task)}"
        )
        if initial:
            prompt += "\n\n" + skill_path.read_text(encoding="utf-8")
        return prompt

    def _session_directory(self, session_ref: str) -> Path:
        return (
            self._workspace
            / "root-sessions"
            / hashlib.sha256(session_ref.encode("utf-8")).hexdigest()
        )

    def _session_receipts(self, session_ref: str) -> tuple[dict[str, object], ...]:
        directory = self._session_directory(session_ref)
        if not directory.exists():
            return ()
        receipts: list[dict[str, object]] = []
        for generation, path in enumerate(sorted(directory.glob("turn-*.json")), 1):
            try:
                receipt = read_transport_envelope(path, self._transport_key)
            except (OSError, ProviderSupervisorError) as error:
                raise AcquisitionUnavailable(
                    "acquisition_root_session_invalid"
                ) from error
            if (
                receipt.get("schema_ref") != _ROOT_SESSION_RECEIPT_SCHEMA
                or receipt.get("session_ref") != session_ref
                or receipt.get("generation") != generation
                or not isinstance(receipt.get("native_session_ref"), str)
                or not receipt["native_session_ref"]
                or (
                    generation == 1
                    and receipt.get("previous_native_session_ref") is not None
                )
                or (
                    generation > 1
                    and receipt.get("previous_native_session_ref")
                    != receipts[-1]["native_session_ref"]
                )
            ):
                raise AcquisitionUnavailable("acquisition_root_session_invalid")
            replacement = receipt.get("replacement")
            if replacement is not None:
                if (not isinstance(replacement, dict)
                    or set(replacement) != {"job_ref", "recovery_checkpoint"}
                    or not isinstance(replacement["job_ref"], str)
                    or not isinstance(replacement["recovery_checkpoint"], dict)):
                    raise AcquisitionUnavailable("acquisition_root_session_invalid")
                checkpoint = replacement["recovery_checkpoint"]
                if (generation == 1 or receipt["native_session_ref"] == receipt["previous_native_session_ref"]
                    or checkpoint.get("schema_ref") != "meta-research/provider-native-session-unavailable/v1"
                    or checkpoint.get("native_session_ref") != receipt["previous_native_session_ref"]
                    or checkpoint.get("error_code") != -32600
                    or receipt.get("job_ref") != replacement["job_ref"] + ":replacement:" + canonical_hash(checkpoint)):
                    raise AcquisitionUnavailable("acquisition_root_session_invalid")
            elif generation > 1 and receipt["native_session_ref"] != receipt["previous_native_session_ref"]:
                raise AcquisitionUnavailable("acquisition_root_session_invalid")
            receipts.append(receipt)
        return tuple(receipts)

    def retired_native_sessions(self, session_ref: str) -> set[str]:
        receipts = self._session_receipts(session_ref)
        if not receipts:
            return set()
        return {str(item["native_session_ref"]) for item in receipts} - {str(receipts[-1]["native_session_ref"])}

    def _latest_native_session_ref(self, session_ref: str) -> str | None:
        receipts = self._session_receipts(session_ref)
        return None if not receipts else str(receipts[-1]["native_session_ref"])

    def _append_session_receipt(
        self,
        *,
        session_ref: str,
        native_session_ref: str,
        previous_native_session_ref: str | None,
        job_ref: str,
        phase: str,
        replacement: dict[str, object] | None = None,
    ) -> None:
        existing = self._session_receipts(session_ref)
        replay = next((item for item in existing if item.get("job_ref") == job_ref), None)
        if replay is not None:
            if (
                replay.get("native_session_ref") != native_session_ref
                or replay.get("previous_native_session_ref") != previous_native_session_ref
                or replay.get("phase") != phase
                or replay.get("replacement") != replacement
            ):
                raise AcquisitionUnavailable("acquisition_root_session_conflict")
            return
        latest = None if not existing else existing[-1]["native_session_ref"]
        if (previous_native_session_ref != latest
            or replacement is None and latest is not None and native_session_ref != latest):
            raise AcquisitionUnavailable("acquisition_root_session_conflict")
        directory = self._session_directory(session_ref)
        directory.mkdir(parents=True, exist_ok=True)
        generation = len(existing) + 1
        receipt = {
            "schema_ref": _ROOT_SESSION_RECEIPT_SCHEMA,
            "session_ref": session_ref,
            "generation": generation,
            "native_session_ref": native_session_ref,
            "previous_native_session_ref": previous_native_session_ref,
            "job_ref": job_ref,
            "phase": phase,
            **({} if replacement is None else {"replacement": replacement}),
        }
        try:
            write_transport_envelope(
                directory / f"turn-{generation:08d}.json",
                receipt,
                self._transport_key,
            )
        except (OSError, ProviderSupervisorError) as error:
            raise AcquisitionUnavailable(
                "acquisition_root_session_unavailable"
            ) from error


class _AcquisitionCodexAdapter(CodexIdeaSkillAdapter):
    _root_agent_kind = "acquisition"

    def _session_workspace(self, session_ref):
        if self._workspaces is None:
            return None
        return self._workspaces.bind_acquisition_session(session_ref)
    _reconciliation_operation_names = ("acquisition-root-turn",)

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "acquisition-root-turn":
            return "acquisition_root_result_invalid"
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")


__all__ = ["CodexAcquisitionRootAdapter"]
