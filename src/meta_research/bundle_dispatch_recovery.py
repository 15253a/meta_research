"""Reconcile completed rolling Bundle work before changing its input cut."""
from dataclasses import replace
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta_research.bundle_skill import (
        BundleDispatchRequest, BundleTargetBatchRequest, CodexBundleSkillAdapter,
    )

from meta_research.idea_skill import (
    IdeaSkillUnavailable, _compile_codex_output_schema,
    _decode_codex_provider_output, _operation_transport_limits,
    _read_completed_operation, _read_operation_invocation, _read_spool_text,
    _unwrap_codex_root_output, _verified_operation_inputs,
)
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import root_capability_profile


def _completed_operation(adapter, request, operation_name, raw_schema):
    """Verify the original immutable inputs and reconcile only that operation.

    Unknown/running operations remain pending.  No call here starts a provider,
    ignores its seal, rewrites its input, or accepts output against a new cut.
    """
    directory = (adapter._workspace / "provider-operations"
                 / canonical_hash({"job_ref": request.job_ref}) / operation_name)
    envelope = json.loads(_read_spool_text(directory / "invocation.json", 128 * 1024))
    invocation = envelope["payload"]
    if not isinstance(invocation, dict):
        raise ValueError("invalid invocation")
    expected = {k: v for k, v in invocation.items() if k != "transport_mode"}
    profile = root_capability_profile(adapter._root_agent_kind)
    expected.update(
        job_ref=request.job_ref, operation_name=operation_name,
        native_session_ref=request.native_session_ref, model_ref=adapter._model_ref,
        root_capability_profile=profile.as_dict(),
        root_capability_profile_hash=profile.digest,
        output_schema_hash=canonical_hash(_compile_codex_output_schema(raw_schema)),
        mcp_url=adapter._resident_mcp_base_url + "/mcp",
    )
    _key_path, key = adapter._transport_key()
    invocation = _read_operation_invocation(
        directory / "invocation.json", key=key, expected_base=expected,
    )
    invocation_hash = canonical_hash(invocation)
    _verified_operation_inputs(directory, invocation_hash=invocation_hash)
    limits = _operation_transport_limits(invocation)
    prompt = _read_spool_text(directory / "prompt.txt", limits.prompt_max_bytes)
    output, session, _stdout = _read_completed_operation(
        directory, invocation_hash=invocation_hash,
        native_session_ref=request.native_session_ref, transport_limits=limits,
    )
    output = _decode_codex_provider_output(
        _unwrap_codex_root_output(output, raw_schema), raw_schema,
    )
    return prompt, output, session


def _prompt_fields(prompt, names):
    fields = {}
    for line in prompt.splitlines():
        name, separator, value = line.partition("=")
        if separator and name in names:
            if name in fields:
                raise ValueError("duplicate request field")
            fields[name] = value
    if set(fields) != set(names):
        raise ValueError("missing request field")
    return fields


def _correction(adapter, request, operation_name, detail):
    raise adapter._sealed_result_failure(
        job_ref=request.job_ref, operation_name=operation_name,
        native_session_ref=request.native_session_ref,
        failure_code="bundle_review_result_contract_invalid", detail_code=detail,
    )


def recover_rejected_dispatch(
    adapter: "CodexBundleSkillAdapter", request: "BundleDispatchRequest", operation_name: str,
) -> None:
    """Preserve a completed result; superseded inputs require a new Attempt."""
    from meta_research.bundle_skill import (
        BundleDispatchResult, BundleSkillContractError, _dispatch_schema,
        validate_bundle_dispatch_result,
    )
    if request.job_ref is None:
        return
    try:
        prompt, output, session = _completed_operation(
            adapter, request, operation_name, _dispatch_schema(()),
        )
        identity_names = (
            "stage_request_ref", "run_ref", "attempt_ref", "fence_ref",
            "root_session_ref", "graph_ref", "generation",
        )
        fields = _prompt_fields(prompt, (*identity_names, "runtime_binding_hash",
                                       "inbox_checkpoint", "frontier", "state"))
        if any(fields[name] != str(getattr(request, name)) for name in identity_names):
            raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        if fields["runtime_binding_hash"] != canonical_hash(request.runtime_binding.as_dict()):
            raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        frontier = json.loads(fields["frontier"])
        if not isinstance(frontier, list):
            raise ValueError("invalid frontier")
        original = replace(
            request, frontier=tuple(frontier), state=json.loads(fields["state"]),
            inbox_checkpoint=json.loads(fields["inbox_checkpoint"]),
        )
    except IdeaSkillUnavailable:
        raise
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    result = BundleDispatchResult(
        action=output.get("action"), selected_target_ref=output.get("selected_target_ref"),
        rationale=output.get("rationale"), native_session_ref=session, adapter_kind="codex_cli",
    )
    try:
        validate_bundle_dispatch_result(original, result)
    except BundleSkillContractError as error:
        _correction(adapter, request, operation_name, str(error))
    if (original.frontier != request.frontier or original.state != request.state
            or original.inbox_checkpoint != request.inbox_checkpoint):
        _correction(adapter, request, operation_name, "bundle_operation_inputs_changed")


def recover_rejected_target_batch(
    adapter: "CodexBundleSkillAdapter", request: "BundleTargetBatchRequest", operation_name: str,
) -> None:
    """Use the original batch closure for validation before Owner correction."""
    from meta_research.bundle_skill import (
        BundleSkillContractError, BundleTargetBatchResult, _target_batch_schema,
        validate_bundle_target_batch_result,
    )
    if request.job_ref is None:
        return
    try:
        prompt, output, session = _completed_operation(
            adapter, request, operation_name, _target_batch_schema(request),
        )
        fields = _prompt_fields(prompt, ("canonical_batch_context",))
        context = json.loads(fields["canonical_batch_context"])
        identity_names = (
            "stage_request_ref", "run_ref", "attempt_ref", "fence_ref", "root_session_ref",
            "graph_ref", "formal_plan_ref", "context_pack_ref", "context_pack_hash",
            "base_generation", "base_head_receipt",
        )
        if (not isinstance(context, dict)
                or any(context.get(name) != getattr(request, name) for name in identity_names)
                or context.get("runtime_binding_hash") != canonical_hash(request.runtime_binding.as_dict())
                or context.get("initial_target_plan_hash") != canonical_hash(request.initial_target_plan)
                or context.get("formal_plan") != request.plan_document):
            raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        if not isinstance(context.get("current_targets"), list) or not isinstance(context.get("target_commits"), list):
            raise ValueError("invalid batch closure")
        original = replace(
            request, current_targets=tuple(context["current_targets"]),
            target_commits=tuple(context["target_commits"]),
            inbox_checkpoint=context["inbox_checkpoint"],
        )
    except IdeaSkillUnavailable:
        raise
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    result = BundleTargetBatchResult(
        strategy_update=output.get("strategy_update"), rationale=output.get("rationale"),
        native_session_ref=session, adapter_kind="codex_cli",
    )
    try:
        validate_bundle_target_batch_result(original, result)
    except BundleSkillContractError as error:
        _correction(adapter, request, operation_name, str(error))
    if (original.current_targets != request.current_targets
            or original.target_commits != request.target_commits
            or original.inbox_checkpoint != request.inbox_checkpoint):
        _correction(adapter, request, operation_name, "bundle_operation_inputs_changed")
