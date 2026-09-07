"""Recover rejected, completed dispatches without replaying their effects."""
from dataclasses import replace
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meta_research.bundle_skill import BundleDispatchRequest, CodexBundleSkillAdapter

from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    _operation_transport_limits,
    _read_completed_operation,
    _read_operation_invocation,
    _read_spool_text,
    _verified_operation_inputs,
)
from meta_research.owners.common import canonical_hash
from meta_research.root_capabilities import root_capability_profile


def recover_rejected_dispatch(
    adapter: "CodexBundleSkillAdapter", request: "BundleDispatchRequest", operation_name: str,
) -> None:
    """Route a proven malformed old result to the existing Owner correction.

    A changed frontier must never be submitted under the old operation identity.
    It must also not hide a completed result's rejection from the correction
    path. Verify the old request and result, then validate against that request.
    Valid results and changed execution scopes remain identity conflicts.
    """
    from meta_research.bundle_skill import (
        BundleDispatchResult,
        BundleSkillContractError,
        validate_bundle_dispatch_result,
    )

    if request.job_ref is None:
        return
    directory = (
        adapter._workspace / "provider-operations"
        / canonical_hash({"job_ref": request.job_ref}) / operation_name
    )
    try:
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
        )
        _key_path, key = adapter._transport_key()
        invocation = _read_operation_invocation(
            directory / "invocation.json", key=key, expected_base=expected,
        )
        invocation_hash = canonical_hash(invocation)
        _verified_operation_inputs(directory, invocation_hash=invocation_hash)
        limits = _operation_transport_limits(invocation)
        prompt = _read_spool_text(directory / "prompt.txt", limits.prompt_max_bytes)
        names = (
            "stage_request_ref", "run_ref", "attempt_ref", "fence_ref",
            "graph_ref", "generation", "inbox_checkpoint", "frontier", "state",
        )
        fields = {}
        for line in prompt.splitlines():
            name, separator, value = line.partition("=")
            if separator and name in names:
                if name in fields:
                    raise ValueError("duplicate request field")
                fields[name] = value
        if set(fields) != set(names):
            raise ValueError("missing request field")
        for name in names[:6]:
            if fields[name] != str(getattr(request, name)):
                raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        frontier = json.loads(fields["frontier"])
        if not isinstance(frontier, list):
            raise ValueError("invalid frontier")
        original = replace(
            request, frontier=tuple(frontier), state=json.loads(fields["state"]),
            inbox_checkpoint=json.loads(fields["inbox_checkpoint"]),
        )
        output, session, _stdout = _read_completed_operation(
            directory, invocation_hash=invocation_hash,
            native_session_ref=request.native_session_ref, transport_limits=limits,
        )
    except IdeaSkillUnavailable:
        raise
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError) as error:
        raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
    result = BundleDispatchResult(
        action=output.get("action"), selected_target_ref=output.get("selected_target_ref"),
        rationale=output.get("rationale"), native_session_ref=session,
        adapter_kind="codex_cli",
    )
    try:
        validate_bundle_dispatch_result(original, result)
    except BundleSkillContractError as error:
        raise adapter._sealed_result_failure(
            job_ref=request.job_ref, operation_name=operation_name,
            native_session_ref=request.native_session_ref,
            failure_code="bundle_review_result_contract_invalid", detail_code=str(error),
        ) from error
