"""Explicit next-decision runtime admission; historical operations stay frozen.

This is a new Owner transition after a human system-help retry, not equality or
compatibility between runtime hashes. Shared guidance and the current tool
contract are admitted anew, including schema changes. Model, adapter code,
explicit capabilities and transport stay exact; Harness revalidates current
operations and HC revalidates existing research authorization.
"""
import json
import time
from types import SimpleNamespace

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref

_REASON = "bundle_runtime_refreshed"
_FIELDS = ("runtime_binding_json", "runtime_binding_hash", "admission_hash", "request_ref",
           "cycle_ref", "stage", "epoch", "context_pack_ref", "context_pack_hash",
           "request_receipt_ref", "request_receipt_hash")
_POLICY_PREFIXES = (
    "package:meta_research.skills/references/research-guidance.md@sha256:",
    "package:meta_research.skills/references/human-guidance.md@sha256:",
    "harness-artifact:operation-binding-set:",
    "harness-artifact:operation-binding-contract:",
)


def _values(row):
    return dict(row._mapping) if hasattr(row, "_mapping") else vars(row).copy()


def _ledger(connection, run_ref):
    rows = connection.execute(text("SELECT * FROM ar_bundle_runtime_refreshes WHERE run_ref=:run ORDER BY retired_generation"),
                              {"run": run_ref}).all()
    result = []
    for row in rows:
        value = json.loads(row.payload_json)
        replacement = connection.execute(text("SELECT * FROM ar_stage_attempt_replacements WHERE replacement_attempt_ref=:attempt"),
                                         {"attempt": row.replacement_attempt_ref}).first()
        retired = connection.execute(text("SELECT * FROM ar_stage_attempts WHERE attempt_ref=:attempt"),
                                     {"attempt": row.retired_attempt_ref}).first()
        successor = connection.execute(text("SELECT * FROM ar_stage_attempts WHERE attempt_ref=:attempt"),
                                       {"attempt": row.replacement_attempt_ref}).first()
        if (canonical_json(value) != row.payload_json or canonical_hash(value) != row.payload_hash
                or value["run_ref"] != row.run_ref or value["retired_attempt_ref"] != row.retired_attempt_ref
                or value["replacement_attempt_ref"] != row.replacement_attempt_ref
                or value["retired_generation"] != row.retired_generation
                or replacement is None or replacement.reason_code != _REASON
                or replacement.run_ref != row.run_ref or replacement.retired_attempt_ref != row.retired_attempt_ref
                or retired is None or successor is None
                or retired.run_ref != row.run_ref or successor.run_ref != row.run_ref
                or retired.generation != row.retired_generation or successor.generation != retired.generation + 1
                or retired.root_session_ref != value["root_session_ref"] or successor.root_session_ref != value["root_session_ref"]
                or retired.fence_ref != value["retired_fence_ref"] or successor.fence_ref != value["replacement_fence_ref"]
                or canonical_hash(json.loads(value["prior_identity"]["runtime_binding_json"])) != value["prior_identity"]["runtime_binding_hash"]
                or canonical_hash(json.loads(value["runtime_binding_json"])) != value["runtime_binding_hash"]):
            raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
        if result and {key: value["prior_identity"][key] for key in ("runtime_binding_json", "runtime_binding_hash")} != {
                "runtime_binding_json": result[-1]["runtime_binding_json"],
                "runtime_binding_hash": result[-1]["runtime_binding_hash"]}:
            raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
        result.append(value)
    if result:
        current = connection.execute(text("SELECT * FROM ar_stage_runs WHERE run_ref=:run"),
                                     {"run": run_ref}).one()
        if (current.runtime_binding_json != result[-1]["runtime_binding_json"]
                or current.runtime_binding_hash != result[-1]["runtime_binding_hash"]):
            raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
        if current.admission_hash != result[-1]["effective_admission_hash"]:
            prior_scope = result[-1]["prior_identity"]
            cursor = (prior_scope["request_ref"], prior_scope["epoch"])
            for scope in connection.execute(text("SELECT * FROM ar_stage_run_rebindings WHERE run_ref=:run ORDER BY new_epoch"), {"run": run_ref}):
                if (scope.old_request_ref, scope.old_epoch) == cursor:
                    if scope.new_epoch <= scope.old_epoch:
                        raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
                    cursor = (scope.new_request_ref, scope.new_epoch)
            if cursor == (prior_scope["request_ref"], prior_scope["epoch"]) or cursor != (current.request_ref, current.epoch):
                raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
            from meta_research.owners.agent_runtime import _runtime_binding_from_row
            _runtime_binding_from_row(current)
    return result


def historical_bundle_runtime(connection, row, *, generation=None):
    """Project the binding that actually governed this attempt, without writes."""
    if row.stage != "bundle":
        return row
    generation = int(row.generation if generation is None else generation)
    entries = _ledger(connection, row.run_ref)
    selected = next((entry for entry in entries if entry["retired_generation"] >= generation), None)
    if not entries:
        return row
    projected = row if selected is None else SimpleNamespace(**{**_values(row), **selected["prior_identity"]})
    projected = _scope_from_original_invocation(connection, projected, generation)
    if hasattr(projected, "native_session_ref"):
        source = next((entry for entry in reversed(entries)
                       if entry["prior_identity"]["request_ref"] == projected.request_ref), None)
        if source is not None and row.request_ref != projected.request_ref:
            projected = SimpleNamespace(**{**_values(projected), "native_session_ref": source["native_session_ref"]})
    return projected


def _admission_hash(value):
    return canonical_hash({"command": "admit_bundle_stage",
        **{key: value[key] for key in ("request_ref", "cycle_ref", "stage", "epoch", "context_pack_ref", "context_pack_hash")},
        "request_receipt": {"status": "accepted", "issuer": "advancement_engine", "kind": "stage_run_request",
            "receipt_ref": value["request_receipt_ref"], "subject_ref": value["request_ref"],
            "payload_hash": value["request_receipt_hash"]},
        "runtime_binding": json.loads(value["runtime_binding_json"]), "runtime_binding_hash": value["runtime_binding_hash"]})


def _scope_from_original_invocation(connection, row, generation):
    """Resolve old scope through existing rebindings and the exact request hash."""
    rebindings = connection.execute(text("SELECT * FROM ar_stage_run_rebindings WHERE run_ref=:run"), {"run": row.run_ref}).all()
    if not rebindings:
        return row
    from meta_research.owners.agent_runtime import _provider_invocation_request_hash
    attempt = connection.execute(text("SELECT * FROM ar_stage_attempts WHERE run_ref=:run AND generation=:generation"),
                                 {"run": row.run_ref, "generation": generation}).one()
    invocation = connection.execute(text("SELECT * FROM ar_stage_provider_invocations WHERE attempt_ref=:attempt AND phase='primary'"),
                                    {"attempt": attempt.attempt_ref}).one()
    refs = {row.request_ref, *(r.old_request_ref for r in rebindings), *(r.new_request_ref for r in rebindings)}
    matches = []
    for ref in refs:
        request = connection.execute(text("SELECT * FROM ae_stage_run_requests WHERE request_ref=:ref AND cycle_ref=:cycle AND stage='bundle'"),
                                     {"ref": ref, "cycle": row.cycle_ref}).first()
        if request is None:
            raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
        identity = {**_values(row), "request_ref": ref, "epoch": request.epoch,
                    "context_pack_ref": request.context_pack_ref, "context_pack_hash": request.context_pack_hash,
                    "request_receipt_ref": request.receipt_ref, "request_receipt_hash": request.receipt_hash}
        expected = _provider_invocation_request_hash(invocation_ref=invocation.invocation_ref, phase="primary",
            request_ref=ref, run_ref=row.run_ref, attempt_ref=attempt.attempt_ref, generation=generation,
            root_session_ref=attempt.root_session_ref, fence_ref=attempt.fence_ref,
            context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash,
            runtime_binding_hash=row.runtime_binding_hash, predecessor_attempt_ref=attempt.predecessor_attempt_ref, stage="bundle")
        if expected == invocation.request_hash:
            identity["admission_hash"] = _admission_hash(identity)
            recorded = connection.execute(text("SELECT 1 FROM ar_stage_commands WHERE command_kind='admit_bundle_stage' "
                "AND result_ref=:run AND request_hash=:hash LIMIT 1"),
                {"run": row.run_ref, "hash": identity["admission_hash"]}).first()
            refreshed = connection.execute(text("SELECT 1 FROM ar_bundle_runtime_refreshes WHERE run_ref=:run "
                "AND json_extract(payload_json,'$.effective_admission_hash')=:hash LIMIT 1"),
                {"run": row.run_ref, "hash": identity["admission_hash"]}).first()
            if recorded is None and refreshed is None:
                raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
            matches.append(SimpleNamespace(**identity))
    if len(matches) != 1:
        raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")
    return matches[0]


def verify_runtime_refresh_replacement(connection, run_ref, replacement_attempt_ref):
    if not any(value["replacement_attempt_ref"] == replacement_attempt_ref for value in _ledger(connection, run_ref)):
        raise OwnerConflict("bundle_runtime_refresh_integrity_invalid")


def inherited_primary_binding_hash(connection, run, attempt):
    """A copied primary is evidence from its original execution, not new work."""
    for value in reversed(_ledger(connection, run.run_ref)) if run.stage == "bundle" else ():
        if (int(attempt.generation) > value["retired_generation"]
                and all(getattr(attempt, name) == expected for name, expected in value["copied_primary"].items())):
            return value["primary_source_runtime_binding_hash"]
    return run.runtime_binding_hash


def _same_execution_scope(prior, current):
    if (prior.schema_ref != current.schema_ref or prior.model_ref != current.model_ref
            or prior.harness_adapter_ref != current.harness_adapter_ref):
        return False
    # The aggregate profile also includes shared prose. Every explicit capability
    # remains exact; current admission validates the current complete profile.
    def without_profile(values):
        return tuple(v for v in values if not v.startswith("root-capability-profile:sha256:"))
    def execution_resources(values):
        return tuple(v for v in values if not v.startswith(_POLICY_PREFIXES))
    def other_connections(values):
        return tuple(v for v in values if not v.startswith("harness-operation-binding:semantic-mcp-"))
    return (without_profile(prior.capability_bindings) == without_profile(current.capability_bindings)
            and other_connections(prior.mcp_bindings) == other_connections(current.mcp_bindings)
            and execution_resources(prior.resource_bindings) == execution_resources(current.resource_bindings))


def refresh_bundle_runtime(owner, *, run_ref, attempt_ref, fence_ref, runtime_binding, harness_authority):
    """Adopt the installed provider only after the original retry is reconciled."""
    from meta_research.owners.agent_runtime import (
        _load_stage_fence, _runtime_binding_from_row, _validated_runtime_binding,
        _assert_runtime_control_allows, _require_current_fence, _prepared_control_affects_run,
    )
    current, binding_json, binding_hash = _validated_runtime_binding(runtime_binding, stage="bundle")
    from meta_research.bundle_skill import bind_bundle_runtime_to_full_conformance
    from meta_research.semantic_owner_gateway import BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
    operation_binding = harness_authority.require_operation_binding(harness_family="codex",
        required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS, required_capabilities=("semantic_mcp",))
    if bind_bundle_runtime_to_full_conformance(current, operation_binding,
            required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS) != current:
        raise OwnerConflict("bundle_runtime_refresh_current_contract_invalid")
    with owner._database.fenced_write() as connection:
        run, attempt, session, fence = _load_stage_fence(connection, run_ref, attempt_ref, fence_ref)
        prior = _runtime_binding_from_row(run)
        if run.runtime_binding_hash == binding_hash:
            return False
        control = connection.execute(text("SELECT * FROM ar_run_controls WHERE run_ref=:run"), {"run": run_ref}).one()
        _assert_runtime_control_allows(connection, run_ref, fence_ref)
        _require_current_fence(run, attempt, fence, "running", "current")
        replacement = connection.execute(text("SELECT reason_code FROM ar_stage_attempt_replacements WHERE replacement_attempt_ref=:attempt"),
                                         {"attempt": attempt_ref}).first()
        units = connection.execute(text("SELECT * FROM ar_provider_units WHERE run_ref=:run"), {"run": run_ref}).all()
        if (run.stage != "bundle" or control.status != "running" or control.cleanup_status != "completed"
                or replacement is None or replacement.reason_code != "human_request_resume_after_restart"
                or _prepared_control_affects_run(connection, run_ref)
                or any(unit.status not in {"completed", "revoked"} for unit in units)
                or not units or not session.native_session_ref or attempt.primary_draft_json is None
                or attempt.submission_ref is not None or not _same_execution_scope(prior, current)):
            raise OwnerConflict("bundle_runtime_refresh_not_ready")
        # A broad research grant remains necessary. A refreshed clock or guidance
        # cannot itself grant access, resources, or high-risk Target execution.
        owner._authorization_verifier.verify_broad_research_authorization(quest_ref=control.quest_ref)
        now = time.time()
        prior_identity = {field: getattr(run, field) for field in _FIELDS}
        primary_source = inherited_primary_binding_hash(connection, run, attempt)
        effective_admission = canonical_hash({
            "command": "admit_bundle_stage", "request_ref": run.request_ref,
            "cycle_ref": run.cycle_ref, "stage": run.stage, "epoch": int(run.epoch),
            "context_pack_ref": run.context_pack_ref, "context_pack_hash": run.context_pack_hash,
            "request_receipt": {"status": "accepted", "issuer": "advancement_engine", "kind": "stage_run_request",
                "receipt_ref": run.request_receipt_ref, "subject_ref": run.request_ref,
                "payload_hash": run.request_receipt_hash},
            "runtime_binding": current.as_dict(), "runtime_binding_hash": binding_hash,
        })
        connection.execute(text("UPDATE ar_stage_runs SET runtime_binding_json=:binding, runtime_binding_hash=:hash, admission_hash=:admission WHERE run_ref=:run"),
                           {"binding": binding_json, "hash": binding_hash, "admission": effective_admission, "run": run_ref})
        successor = owner._replace_fenced_managed_attempt(connection, control, now,
            reuse_checkpoint=True, reuse_operation_refs=False, preserve_native_session=True,
            replacement_reason_code=_REASON)
        connection.execute(text("UPDATE ar_execution_fences SET status='rejected',closed_at=:now WHERE fence_ref=:fence"),
                           {"now": now, "fence": fence_ref})
        connection.execute(text("UPDATE ar_run_controls SET attempt_ref=:attempt,fence_ref=:fence,control_revision=control_revision+1,updated_at=:now WHERE run_ref=:run"),
                           {"attempt": successor.attempt_ref, "fence": successor.fence_ref, "now": now, "run": run_ref})
        refresh_ref = new_ref("bundle_runtime_refresh")
        value = {"refresh_ref": refresh_ref, "run_ref": run_ref, "root_session_ref": session.session_ref,
                 "native_session_ref": session.native_session_ref, "retired_attempt_ref": attempt_ref,
                 "retired_fence_ref": fence_ref, "retired_generation": int(attempt.generation),
                 "replacement_attempt_ref": successor.attempt_ref, "replacement_fence_ref": successor.fence_ref,
                 "prior_identity": prior_identity, "runtime_binding_json": binding_json,
                 "runtime_binding_hash": binding_hash, "effective_admission_hash": effective_admission,
                 "current_operation_binding": operation_binding.as_dict(),
                 "primary_draft_hash": attempt.primary_draft_hash,
                 "copied_primary": {name: getattr(attempt, name) for name in (
                     "primary_draft_hash", "primary_recorded_at", "primary_adapter_kind", "primary_provider_unit_ref")},
                 "primary_source_runtime_binding_hash": primary_source,
                 "terminal_units": [{"unit_ref": u.unit_ref,"status": u.status,"operation_ref": u.operation_ref} for u in units]}
        connection.execute(text("INSERT INTO ar_bundle_runtime_refreshes VALUES (:ref,:run,:retired,:replacement,:generation,:payload,:hash,:now)"),
                           {"ref": refresh_ref, "run": run_ref, "retired": attempt_ref,
                            "replacement": successor.attempt_ref, "generation": int(attempt.generation),
                            "payload": canonical_json(value), "hash": canonical_hash(value), "now": now})
        connection.execute(text("UPDATE agent_runtime_state SET revision=revision+1 WHERE singleton='owner'"))
        owner._feed.record(connection, "agent_runtime.bundle_runtime_refreshed", value)
    return True
