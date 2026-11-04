"""Delivery evidence from existing sealed Bundle operations, without a cursor."""
from __future__ import annotations

import json
from dataclasses import replace

from meta_research.idea_skill import (
    IdeaSkillUnavailable, _read_completed_operation, _read_sealed_operation_invocation,
    _read_spool_text, _read_operation_business_input, _operation_transport_limits,
    _decode_codex_provider_output,
    _unwrap_codex_root_output,
)
from meta_research.owners.common import canonical_hash


def verified_batch_delivery_baseline(adapter, request, *, verified_proposals=(),
                                     verified_proposal_checkpoints=None):
    if request.job_ref is None or not request.native_session_ref:
        return None
    from meta_research.bundle_skill import _rolling_operation_identity, _target_batch_schema

    directory = (adapter._workspace / "provider-operations"
                 / canonical_hash({"job_ref": request.job_ref}))
    candidates = []
    for path in directory.glob("target-batch-*/invocation.json"):
        suffix = path.parent.name.removeprefix("target-batch-")
        if suffix.isdecimal() and int(suffix) <= request.base_generation:
            candidates.append((int(suffix), path.parent))
    if not candidates or max(generation for generation, _ in candidates) != request.base_generation:
        return None
    _, key = adapter._transport_key()
    delivered = {"current_targets": set(), "target_commits": set()}
    latest = None
    operations = []
    identity = _rolling_operation_identity(request)
    initial_plan_hash = canonical_hash(request.initial_target_plan)
    proposals_by_generation = {}
    for proposal in verified_proposals:
        proposals_by_generation.setdefault(proposal.base_generation, []).append(proposal)
    for generation, operation in sorted(candidates):
        invocation = _read_sealed_operation_invocation(operation / "invocation.json", key=key)
        digest = canonical_hash(invocation)
        limits = _operation_transport_limits(invocation)
        body = _read_operation_business_input(operation, invocation, limits)
        fields = body.get("request", {})
        if (body.get("kind") != "bundle_target_batch"
                or body.get("identity") != identity
                or invocation.get("job_ref") != request.job_ref
                or invocation.get("operation_name") != operation.name
                or invocation.get("native_session_ref") != request.native_session_ref
                or fields.get("base_generation") != generation - 1
                or fields.get("formal_plan_ref") != request.formal_plan_ref
                or fields.get("context_pack_ref") != request.context_pack_ref
                or fields.get("context_pack_hash") != request.context_pack_hash
                or fields.get("formal_plan") != request.plan_document
                or fields.get("initial_target_plan_hash") != initial_plan_hash):
            raise IdeaSkillUnavailable("codex_operation_identity_conflict")
        output, _, _ = _read_completed_operation(operation, invocation_hash=digest,
                                                native_session_ref=request.native_session_ref,
                                                transport_limits=limits)
        from meta_research.bundle_output_assembly import assemble_strategy_update

        previous = replace(request, base_generation=fields["base_generation"],
            base_head_receipt=fields["base_head_receipt"], inbox_checkpoint=fields["inbox_checkpoint"],
            current_targets=tuple(fields["current_targets"]), target_commits=tuple(fields["target_commits"]))
        schema = adapter._operation_output_schema(_target_batch_schema(previous),
                                                  request.job_ref, operation.name)
        output = _decode_codex_provider_output(_unwrap_codex_root_output(output, schema), schema)
        strategy = assemble_strategy_update(output.get("strategy_update"), request.plan_document)
        matching = [proposal for proposal in proposals_by_generation.get(fields["base_generation"], ())
                    if proposal.base_head_receipt.as_public_dict() == fields["base_head_receipt"]
                    and (verified_proposal_checkpoints or {}).get(proposal.proposal_ref) == fields["inbox_checkpoint"]
                    and proposal.strategy_update == strategy]
        if not matching:
            return None
        prompt = _read_spool_text(operation / "prompt.txt", limits.prompt_max_bytes)
        lines = [line[len("batch_context="):] for line in prompt.splitlines()
                 if line.startswith("batch_context=")]
        if len(lines) != 1:
            return None
        try:
            view = json.loads(lines[0])
            for name in delivered:
                records = fields[name]
                shown = view.get(name, [])
                if not isinstance(records, list) or not isinstance(shown, list):
                    raise ValueError("batch records invalid")
                frozen_hashes = {canonical_hash(record) for record in records}
                for record in shown:
                    record_hash = canonical_hash(record)
                    if record_hash not in frozen_hashes:
                        raise ValueError("view does not match frozen records")
                    delivered[name].add(record_hash)
        except (ValueError, KeyError, TypeError) as error:
            raise IdeaSkillUnavailable("codex_operation_spool_invalid") from error
        latest = fields
        operations.append({"operation_name": operation.name, "invocation_hash": digest,
                           "prompt_hash": invocation["prompt_hash"],
                           "proposal_refs": [proposal.proposal_ref for proposal in matching]})
    if latest is None:
        return None
    return {**operations[-1], "base_generation": latest["base_generation"],
            "operations": operations,
            **{name: [record for record in latest[name]
                      if canonical_hash(record) in delivered[name]] for name in delivered}}
