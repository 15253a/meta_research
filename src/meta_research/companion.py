from __future__ import annotations

import json
import subprocess
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
from meta_research.owners.common import canonical_hash
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
    _reconciliation_operation_names = ("companion-turn", "proposal-fork")

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

    def capability_profile(self) -> RootCapabilityProfile:
        return root_capability_profile("companion")

    def cancel_job(self, job_ref: str) -> bool:
        self._request_durable_job_stop(job_ref)
        cancel_job = getattr(self._runner, "cancel_job", None)
        if callable(cancel_job):
            cancel_job(job_ref)
        return True

    def _transport_contract_failure_code(self, operation_name: str) -> str:
        if operation_name == "proposal-fork":
            return "companion_proposal_fork_result_invalid"
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
            raw, native_session_ref, _stdout = (
                self._invoke_optional_root_task_operation(
                    operation_name="companion-turn",
                    prompt=prompt,
                    schema=_reply_schema(include_agent_proposal=companion),
                    native_session_ref=request.native_session_ref,
                    job_ref=request.job_ref,
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
        )


__all__ = ["CodexCompanionAdapter"]
