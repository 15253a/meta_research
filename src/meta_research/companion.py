from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, cast

from meta_research.chat_progress import (
    CHAT_REPLY_PROGRESS_INSTRUCTION,
    preserve_existing_reply_prompt,
    read_chat_reply,
)
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillUnavailable,
    _read_operation_invocation,
)
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    read_transport_key_for_operation,
)
from meta_research.quest_drafting import (
    INTENT_REPLY_MAX_LENGTH,
    DraftingUnavailable,
    IntentDraftingProvider,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
    ProposalDrafter,
    _canonical_json,
    _proposal_prompt,
    _proposal_schema,
    _reply_schema,
    _validated_agent_proposal,
    _validated_question,
)
from meta_research.root_capabilities import RootCapabilityProfile, root_capability_profile
from meta_research.creation_basis import (
    InitializationUnderstandingRequest, InitializationUnderstandingResult,
    FirstQuestionSynthesisRequest, FirstQuestionSynthesisResult,
    first_creation_instructions, understanding_schema, revision_schema, reassessment_schema,
)


class CodexCompanionAdapter(
    CodexIdeaSkillAdapter, ProposalDrafter, IntentDraftingProvider
):
    """One complete, persistent Companion Root and its short-lived proposal forks.

    Companion turns resume the same native Root Session.  A Proposal generation
    is not another narrow provider: the Companion must spawn exactly one fresh
    child with inherited context, wait for it, and return its schema-constrained
    draft.  A short child message points to the exact current materials already
    inherited.  The child identifier returned by spawn is terminal provenance
    only; a provider's canonical task path is retained when that is the exposed
    identifier, and is never used for resume.
    """

    _root_agent_kind = "companion"
    _reconciliation_operation_names = ("companion-turn", "proposal-fork", "initialization-understanding", "first-question-synthesis")

    def __init__(
        self,
        workspace: Path,
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
        super().__init__(
            workspace,
            executable=executable,
            model_ref=model_ref,
            timeout_seconds=timeout_seconds,
            process_runner=process_runner,
            codex_home=codex_home,
        )
        self._creation_call_lock = threading.RLock()
        self._creation_calls = {}
        self._creation_stops = set()
        self._creation_outcomes = {}

    def capability_profile(self) -> RootCapabilityProfile:
        return root_capability_profile("companion")

    def cancel_job(self, job_ref: str) -> bool:
        with self._creation_call_lock:
            runners = [runner for key, runner in self._creation_calls.items()
                       if key == job_ref or key.startswith(job_ref + ":")]
            outcomes = [value for key, value in self._creation_outcomes.items()
                        if key == job_ref or key.startswith(job_ref + ":")]
            self._creation_stops.add(job_ref)
        if runners:
            return all([runner.cancel_job(runner.work.operation.operation_ref)["descendants_ended"] for runner in runners])
        if outcomes:
            return all(outcome["descendants_ended"] for outcome in outcomes)
        if self._workspaces is not None:
            from sqlalchemy import text
            with self._workspaces._hc._database.read() as connection:
                rows = connection.execute(text("SELECT operation_ref,state FROM hc_creation_material_operations")).all()
            states = [row.state for row in rows if row.operation_ref == job_ref or row.operation_ref.startswith(job_ref + ":")]
            if states:
                return all(state in {"sealed", "failed"} for state in states)
        self._request_durable_job_stop(job_ref)
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_ref)
        return True

    def _protected_creation_invoke(self, request, *, operation_name, prompt, schema, native_session_ref, inputs):
        from meta_research.creation_work import ProtectedCreationRunner
        from meta_research.runtime_conditions import render_runtime_conditions
        if self._workspaces is None or not request.job_ref:
            raise IdeaSkillUnavailable("protected_creation_runtime_unavailable")
        try:
            work = self._workspaces.protected_creation(self._creation_workspace(request), inputs, operation_ref=request.job_ref)
        except OwnerConflict as error:
            raise IdeaSkillUnavailable(error.code) from error
        basis = getattr(request, "basis", None)
        if basis is None:
            basis = getattr(request, "creation_basis", None)
        extra = ()
        if basis is not None:
            work.bind_context_readers(basis, getattr(request, "literature_snapshot", None))
            extra = ("research_memory.creation_basis.read", "research_memory.content.read")
        elif getattr(request, "reassessment", None) is not None:
            work.bind_reassessment_readers(request.reassessment)
            extra = ("research_memory.creation_basis.read", "research_memory.content.read")
        guidance_root = Path(__file__).parent / "skills" / "first_creation"
        names = ("SKILL.md", "references/source-evidence.md", "references/literature-corrections.md")
        guidance = {name: str(guidance_root / name) for name in names}
        files = [Path(value) for value in guidance.values()]
        context = getattr(request, "context", None)
        if context is not None:
            original_binding = self._creation_workspace(request)
            files.extend(original_binding.directory / item["path"] for item in context["manifest"])
        proxy_environment = {key: os.environ[key] for key in
            ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy") if key in os.environ}
        for key in ("NO_PROXY", "no_proxy"):
            proxy_environment[key] = ",".join(filter(None, (os.environ.get(key), "127.0.0.1", "localhost")))
        runner = ProtectedCreationRunner(work, read_only_inputs=files, environment=proxy_environment)
        runner.runtime_conditions = render_runtime_conditions(self._workspace,
            initialization_id=request.initialization_id) + "\n" + runner.runtime_conditions
        with self._creation_call_lock:
            if request.job_ref in self._creation_calls:
                raise IdeaSkillUnavailable("protected_creation_active")
            self._creation_calls[request.job_ref] = runner
            if any(request.job_ref == key or request.job_ref.startswith(key + ":") for key in self._creation_stops):
                runner.cancel_job(request.job_ref)
        try:
            access = work.channel(extra)
            prompt += "\n\nProtected creation binding and explicit guidance file locations:\n" + _canonical_json({
                "execution": work.execution_binding, "material_references": inputs.as_dict(), "guidance_files": guidance})
            prompt += "\nRead guidance through these exact declared locations, including its reference files. "
            prompt += "Create the operation work directory before writing a program or result. The reference descriptors name unreadable external originals."
            raw, native_ref, stdout = self._invoke(operation_name=operation_name, prompt=prompt, schema=schema,
                native_session_ref=native_session_ref, job_ref=request.job_ref,
                workspace_binding=work.binding, invocation_runner=runner, provider_execution_binding=work.execution_binding,
                mcp_url=access.url, mcp_token=access.token, mcp_scope_binding_hash=access.scope_binding_hash,
                semantic_mcp_protected_environment=True, authorized_operation_ids=access.operation_ids)
            work.completed_handoff()
            identity = work.seal()
            return raw, native_ref, stdout, identity, work
        except (IdeaSkillUnavailable, OwnerConflict) as error:
            outcome = work.request_stop()
            unknown = not outcome["descendants_ended"] or error.code == "protected_creation_unknown_outcome"
            work.fail(unknown_outcome=unknown)
            if unknown:
                raise IdeaSkillUnavailable("protected_creation_unknown_outcome") from error
            raise IdeaSkillUnavailable(error.code) from error
        finally:
            outcome = work.request_stop()
            with self._creation_call_lock:
                self._creation_outcomes[request.job_ref] = outcome
                if outcome["descendants_ended"]:
                    self._creation_calls.pop(request.job_ref, None)

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name in {"proposal-fork", "first-question-synthesis"}:
            return "companion_proposal_fork_result_invalid"
        if operation_name == "initialization-understanding":
            return "companion_initialization_understanding_invalid"
        if operation_name == "companion-turn":
            return "companion_turn_result_invalid"
        raise IdeaSkillUnavailable("codex_operation_spool_invalid")

    def reconcile_job(self, job_ref: str) -> str:
        operation_root = (
            self._workspace
            / "provider-operations"
            / canonical_hash({"job_ref": job_ref})
        )
        if not operation_root.exists():
            return "absent"
        directories = tuple(
            item for item in operation_root.iterdir() if item.is_dir()
        )
        if not directories:
            return "pending"
        return (
            "terminal"
            if all((item / "completed.json").is_file() for item in directories)
            else "pending"
        )

    def observe_reply(self, job_ref: str) -> str:
        """Read only this job's verified Companion turn spool, never other turns."""
        directory = (
            self._workspace
            / "provider-operations"
            / canonical_hash({"job_ref": job_ref})
            / "companion-turn"
        )
        try:
            with (directory / "invocation.json").open("rb") as stream:
                encoded = stream.read(64 * 1024 + 1)
            if len(encoded) > 64 * 1024:
                return ""
            envelope = json.loads(encoded)
            invocation = (
                envelope.get("payload") if isinstance(envelope, dict) else None
            )
            if (
                not isinstance(invocation, dict)
                or invocation.get("job_ref") != job_ref
                or invocation.get("operation_name") != "companion-turn"
            ):
                return ""
            _key_path, key = read_transport_key_for_operation(directory)
            _read_operation_invocation(
                directory / "invocation.json",
                key=key,
                expected_base={
                    name: value
                    for name, value in invocation.items()
                    if name != "transport_mode"
                },
            )
        except (
            OSError, ValueError, TypeError, IdeaSkillUnavailable,
            ProviderSupervisorError,
        ):
            return ""
        return read_chat_reply(directory / "stdout.jsonl")

    def understand_initialization(self, request: InitializationUnderstandingRequest) -> InitializationUnderstandingResult:
        instructions = first_creation_instructions()
        prompt = (
            "You are the persistent creation Companion. Read the captured existing project using native filesystem tools in your bound cwd. "
            "Return only the understanding schema. You have no resident Owner MCP in this creation call. Never pretend to call one. "
            "Prove observed source bytes through exact range witnesses and preserve unread entrances.\n"
            + _canonical_json({"instruction_bundle": instructions, "draft_binding": {"initialization_id": request.initialization_id,
                "revision": request.draft_revision, "hash": request.draft_hash}, "draft": request.draft, "material_manifest": request.manifest})
        )
        try:
            if request.inputs is not None:
                prompt = ("Read registered material entrances only through the granted bounded discover/read/copy MCP tools. "
                    + ("Return the reference understanding schema using actual returned opaque witness_ref citations. "
                       if request.reassessment is None else "Return the reassessment delta using actual returned opaque witness_ref citations for new reads. ")
                    + "Preserve unread and partial entrances. Select exact originals, exact work results, both or neither with explicit custody. "
                    "A directory is never completely read merely because one child was read.\n"
                    + _canonical_json({"instruction_bundle": instructions, "draft": request.draft,
                        "reassessment": request.reassessment}))
                if request.reassessment is not None:
                    prompt += ("\nJudge each prior statement's applicability and conditions. "
                        "Read only affected content. Explicitly inherit eligible witnesses with their original operation identity. "
                        "Emit each inherited witness once, using eligible live_source for current reuse. Never emit the same witness "
                        "as both live_source and managed_history. Retain its selected exact version separately in inherited_selection_keys; "
                        "managed custody does not require a managed_history entry for a live witness. "
                        "Managed history supports historical statements, never current read coverage. Mark necessary first-Question "
                        "checks versus future research without a global waiting gate. Decide separately for every old literature snapshot. "
                        "Reuse selected exact versions instead of selecting them for intake again.")
                raw, native_ref, _stdout, identity, work = self._protected_creation_invoke(request,
                    operation_name="initialization-understanding", prompt=prompt,
                    schema=reassessment_schema() if request.reassessment is not None else understanding_schema(references=True),
                    native_session_ref=request.companion_native_session_ref, inputs=request.inputs)
                if native_ref is None:
                    raise DraftingUnavailable("companion_native_session_missing")
                return InitializationUnderstandingResult({} if request.reassessment is not None else raw, native_ref, identity, work,
                    raw if request.reassessment is not None else None)
            raw, native_ref, _stdout = self._invoke(operation_name="initialization-understanding", prompt=prompt,
                schema=understanding_schema(), native_session_ref=request.companion_native_session_ref,
                job_ref=request.job_ref, workspace_binding=self._creation_workspace(request))
            if native_ref is None:
                raise DraftingUnavailable("companion_native_session_missing")
            return InitializationUnderstandingResult(raw, native_ref)
        except IdeaSkillUnavailable as error:
            raise DraftingUnavailable(error.code) from error

    def synthesize_first_question(self, request: FirstQuestionSynthesisRequest) -> FirstQuestionSynthesisResult:
        instructions = first_creation_instructions()
        child_schema = {"type": "object", "additionalProperties": False, "properties": {
            "content": _proposal_schema(), "revision": revision_schema(references=request.basis.get("input_identity") is not None) if request.literature_snapshot is not None else {"type": "null"}},
            "required": ["content", "revision"]}
        schema = {"type": "object", "additionalProperties": False, "properties": {
            "proposal_fork_native_session_ref": {"type": "string", "minLength": 1},
            "result": child_schema}, "required": ["proposal_fork_native_session_ref", "result"]}
        child_prompt = (
            "Read the small exact context projection through its declared input paths. "
            "Use the granted scoped Readers for selected original sources and literature evidence, and bounded material tools for new original reads. "
            "Preserve the prepared basis and source selection. "
            "Read applicability.json and inherited-literature.json when present. Reuse applicable statements and exact selected versions; "
            "do not repeat unaffected material reads. Inherited literature retains its original snapshot, run and binding. "
            "A direct route may use explicitly inherited literature without presenting it as a new search. "
            "Return the six proposal fields and the route-specific revision. A direct route returns revision=null. "
            "A DeepFetch route returns a complete literature revision with explicit corrections or an honest unchanged/empty assessment. "
            "No Owner writes, receipts, human confirmation, or invented Quest Run.\n"
            + _canonical_json({"instruction_bundle": instructions, "draft": request.draft,
                "prepared_basis": {key: request.basis[key] for key in ("basis_ref", "basis_hash", "kind")},
                "sealed_context": request.context, "output_schema": child_schema})
        )
        prompt = (
            "You are the persistent Companion. Call spawn_agent exactly once with fork_context=true for a fresh short-lived first Question drafter. "
            "Its short message points to BEGIN_FIRST_QUESTION_TASK through END_FIRST_QUESTION_TASK in inherited context. "
            "Save the actual child identifier returned by spawn, wait for its final result, validate it against the schema, "
            "and return {proposal_fork_native_session_ref,result}. Do not reuse an old proposal child. "
            "Keep the current parent session. The child owns content only.\nBEGIN_FIRST_QUESTION_TASK\n"
            + child_prompt + "\nEND_FIRST_QUESTION_TASK"
        )
        try:
            identity, work = None, None
            if request.basis.get("input_identity") is not None:
                from meta_research.creation_inputs import CreationInputIdentity, MaterialSet
                prior = CreationInputIdentity.from_dict(request.basis["input_identity"])
                inputs = MaterialSet(prior.anchor, tuple(request.basis["material_references"]["references"]))
                raw, native_ref, _stdout, identity, work = self._protected_creation_invoke(request,
                    operation_name="first-question-synthesis", prompt=prompt, schema=schema,
                    native_session_ref=request.companion_native_session_ref, inputs=inputs)
            else:
                raw, native_ref, _stdout = self._invoke(operation_name="first-question-synthesis", prompt=prompt, schema=schema,
                    native_session_ref=request.companion_native_session_ref, job_ref=request.job_ref,
                    workspace_binding=self._creation_workspace(request))
            from jsonschema import Draft202012Validator
            Draft202012Validator(schema).validate(raw)
            if native_ref is None or raw["proposal_fork_native_session_ref"] == native_ref:
                raise DraftingUnavailable("companion_proposal_fork_invalid")
            return FirstQuestionSynthesisResult(_validated_question(raw["result"]["content"]), "codex_companion_fork",
                native_ref, raw["proposal_fork_native_session_ref"], raw["result"]["revision"], identity, work)
        except IdeaSkillUnavailable as error:
            raise DraftingUnavailable(error.code) from error

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        child_prompt = (
            "你是这一次 Proposal 窗口的短命 Proposal Drafter。只根据随附的精确"
            "研究上下文形成六字段 Proposal；不得写 Owner、确认 Proposal、创建 Quest "
            "或把自己变成长期 Session。完成后在最终输出中仅返回六字段 JSON 给父"
            " Companion；它就是父结果中的 content。子标识由父根据 spawn 返回值"
            "记录，子任务只负责内容，无需查询或返回任何 Session ID。过程进展使用"
            " commentary。以下是本次子任务的完整输出 schema：\n"
            + _canonical_json(_proposal_schema())
            + "\n\n"
            + _proposal_prompt(request)
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "proposal_fork_native_session_ref": {
                    "type": "string",
                    "minLength": 1,
                },
                "content": _proposal_schema(),
            },
            "required": ["proposal_fork_native_session_ref", "content"],
        }
        prompt = (
            "你是长期存在的全局 Companion 根智能体。为当前 Proposal 窗口调用"
            " spawn_agent 恰好一次，并使用 fork_turns=all 创建一个新的短命"
            " Proposal Drafter。完整任务和精确材料已在本次消息中，子任务会继承；"
            "spawn 的 message 只发送短指令：读取继承上下文中本次父消息的"
            " BEGIN_PROPOSAL_DRAFTER_MESSAGE 与 END_PROPOSAL_DRAFTER_MESSAGE "
            "之间的完整任务及材料，完成后在最终输出中仅返回六字段 JSON。无需"
            "在 spawn message 中复制材料或重写摘要。\n\n"
            "保存 spawn 返回的真实子标识，等待该子任务完成。收到结果后校验"
            "六字段满足给定 schema；格式问题在本次子任务中修正，合格后立即将"
            "六字段放入 content，并将保存的子标识放入"
            " proposal_fork_native_session_ref，按指定 schema 返回。若 spawn "
            "只返回 canonical task path，就如实使用该路径；该字段只记录本次"
            "交接来源，无需额外查询隐藏会话文件或另找原生 Session ID。"
            "草案合格即完成本次交接，无需追加研究。过程进展使用 commentary，"
            "最终输出仅包含指定 schema 的完整结果。\n\n"
            "父 Companion 保持当前 Session。不得 resume 或复用旧 Proposal "
            "Drafter；本次 child 在窗口结束后不再用于后续工作。\n\n"
            "BEGIN_PROPOSAL_DRAFTER_MESSAGE\n"
            + child_prompt
            + "\nEND_PROPOSAL_DRAFTER_MESSAGE"
        )
        try:
            raw, root_session_ref, _stdout = self._invoke(
                operation_name="proposal-fork",
                prompt=prompt,
                schema=schema,
                native_session_ref=request.companion_native_session_ref,
                job_ref=request.job_ref,
                workspace_binding=self._creation_workspace(request),
            )
            if root_session_ref is None:
                raise DraftingUnavailable("companion_native_session_missing")
            if set(raw) != {"proposal_fork_native_session_ref", "content"}:
                raise DraftingUnavailable("companion_proposal_fork_invalid")
            fork_ref = raw["proposal_fork_native_session_ref"]
            content_value = raw["content"]
            if not isinstance(fork_ref, str) or not fork_ref:
                raise DraftingUnavailable("companion_proposal_fork_invalid")
            if not isinstance(content_value, dict):
                raise DraftingUnavailable("codex_proposal_invalid")
            content = _validated_question(cast(dict[str, object], content_value))
        except DraftingUnavailable:
            raise
        except (IdeaSkillUnavailable, TypeError, ValueError) as error:
            raise DraftingUnavailable(
                getattr(error, "code", "companion_proposal_fork_invalid")
            ) from error
        return ProposalDraftResult(
            content=content,
            adapter_kind="codex_companion_fork",
            companion_native_session_ref=root_session_ref,
            proposal_fork_native_session_ref=fork_ref,
        )

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        companion = (
            request.creation_context_kind != "manual_question_creation"
            and request.draft.get("interaction_kind") == "conversation"
        )
        if request.creation_context_kind == "manual_question_creation":
            role_instruction = (
                "你是后续研究问题的窗口级 Drafting 助手。已确认 Seed 不可改写；"
                "只能建议如何调整六字段 Proposal，不得确认、创建问题或签发 receipt。"
            )
            context_identity = (
                f"creation_context_ref={request.creation_context_ref}\n"
                f"context_generation={request.context_generation}\n"
                f"quest_initialization_id={request.initialization_id}\n"
            )
        elif companion:
            role_instruction = (
                "你是长期存在的全局 Companion 根智能体。依据 current_draft 中已投影"
                "的事实解释研究、总结状态并提出可撤回建议；不得把聊天推断成人类授权。"
            )
            context_identity = f"scope_ref={request.initialization_id}\n"
        else:
            role_instruction = (
                "你是创建研究任务期间持续存在的 Companion 根智能体。帮助用户澄清"
                "意图，但只能回复建议；不得修改草稿、确认 bundle 或签发 receipt。"
            )
            context_identity = f"initialization_id={request.initialization_id}\n"
        prompt = (
            role_instruction
            + CHAT_REPLY_PROGRESS_INSTRUCTION
            + "\n\n"
            + context_identity
            + f"current_draft_revision={request.draft_revision}\n"
            f"current_draft_hash={request.draft_hash}\n"
            f"current_draft={_canonical_json(request.draft)}\n"
            f"user_message={request.message}"
        )
        if request.job_ref is not None:
            prompt = preserve_existing_reply_prompt(
                prompt,
                invocation_path=(
                    self._workspace / "provider-operations"
                    / canonical_hash({"job_ref": request.job_ref})
                    / "companion-turn" / "invocation.json"
                ),
                job_ref=request.job_ref,
                hash_prompt=canonical_hash,
                operation_name="companion-turn",
            )
        try:
            identity, work = None, None
            if request.inputs is not None:
                reply_schema = _reply_schema(include_agent_proposal=companion)
                if request.literature_snapshot is not None:
                    reply_schema["properties"]["revision"] = revision_schema(references=True)
                    reply_schema["required"].append("revision")
                    prompt += "\nRead the accepted literature through the exact scoped Reader. Return a complete understanding revision and honest source-backed corrections; keep the Seed and source selection immutable."
                raw, native_session_ref, _stdout, identity, work = self._protected_creation_invoke(request,
                    operation_name="companion-turn", prompt=prompt,
                    schema=reply_schema, native_session_ref=request.native_session_ref,
                    inputs=request.inputs)
            else:
                raw, native_session_ref, _stdout = (
                    self._invoke_optional_root_task_operation(
                        operation_name="companion-turn",
                        prompt=prompt,
                        schema=_reply_schema(include_agent_proposal=companion),
                        native_session_ref=request.native_session_ref,
                        job_ref=request.job_ref,
                        workspace_binding=(self._creation_workspace(request)
                            if getattr(request, "root_runtime_scope", None) is None else None),
                        root_runtime_scope=getattr(
                            request, "root_runtime_scope", None
                        ),
                    )
                )
        except IdeaSkillUnavailable as error:
            raise DraftingUnavailable(
                error.code, native_session_ref=error.native_session_ref
            ) from error
        reply = raw.get("reply")
        expected_keys = {"reply", "agent_proposal"} if companion else {"reply"}
        if request.inputs is not None and request.literature_snapshot is not None:
            expected_keys.add("revision")
        if (
            set(raw) != expected_keys
            or not isinstance(reply, str)
            or not reply.strip()
            or len(reply.strip()) > INTENT_REPLY_MAX_LENGTH
            or native_session_ref is None
        ):
            raise DraftingUnavailable(
                "codex_intent_reply_invalid",
                native_session_ref=native_session_ref,
            )
        try:
            agent_proposal = (
                _validated_agent_proposal(raw.get("agent_proposal"))
                if companion
                else None
            )
        except (TypeError, ValueError) as error:
            raise DraftingUnavailable(
                "codex_agent_proposal_invalid",
                native_session_ref=native_session_ref,
            ) from error
        return IntentTurnResult(
            reply=reply.strip(),
            native_session_ref=native_session_ref,
            adapter_kind="codex_companion_root",
            agent_proposal=agent_proposal,
            input_identity=identity,
            work=work,
            revision=raw.get("revision"),
        )


__all__ = ["CodexCompanionAdapter"]
