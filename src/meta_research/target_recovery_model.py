"""Exact Human Collaboration authorization for one failed Target's next operation."""

from __future__ import annotations

import json

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.provider_supervisor import provider_operation_ref
from meta_research.runtime_conditions import read_runtime_conditions_in_transaction


CAPABILITY = "change_failed_target_successor_model"
SCOPE_SCHEMA = "meta-research/target-recovery-model-scope/v1"
INVALID = "target_recovery_model_authorization_invalid"


def authorization_scope_ref(predecessor_operation_ref: str) -> str:
    return "target_model_retry:" + canonical_hash(predecessor_operation_ref)


def current_binding(connection, database, run_ref, generation, verify_terminal, runtime_identity):
    """Return no exception to ordinary retry unless HC has an exact scoped decision."""
    if generation < 2:
        return None
    predecessor_ref = provider_operation_ref(run_ref, "harness_turn", generation - 1)
    decision = connection.execute(text(
        "SELECT * FROM hc_capability_authorizations WHERE authorization_kind='capability' "
        "AND scope_ref=:scope AND capability=:capability ORDER BY revision DESC LIMIT 1"
    ), {"scope": authorization_scope_ref(predecessor_ref), "capability": CAPABILITY}).first()
    if decision is None:
        return None
    if not decision.is_current or decision.decision != "granted":
        raise OwnerConflict(INVALID)

    row = connection.execute(text(
        "SELECT runs.attempt_ref, runs.fence_ref, runs.profile_json, runs.profile_hash, "
        "runs.harness_family, runs.failure_code, admissions.target_ref, graphs.quest_ref, "
        "graphs.cycle_ref, op.operation_ref, op.invocation_hash, op.model_ref, op.generation, "
        "op.status, op.completed_at FROM ar_harness_runs runs "
        "JOIN ar_target_harness_admissions admissions ON admissions.target_run_ref=runs.run_ref "
        "JOIN rg_targets targets ON targets.target_ref=admissions.target_ref "
        "JOIN rg_target_graphs graphs ON graphs.graph_ref=targets.graph_ref "
        "JOIN ar_harness_provider_operations op ON op.run_ref=runs.run_ref "
        "WHERE runs.run_ref=:run_ref ORDER BY op.generation DESC LIMIT 1"
    ), {"run_ref": run_ref}).first()
    if (row is None or row.harness_family != "codex" or row.status != "failed"
            or row.completed_at is None or row.operation_ref != predecessor_ref
            or row.generation != generation - 1 or not row.model_ref):
        raise OwnerConflict(INVALID)
    conditions = read_runtime_conditions_in_transaction(connection, row.quest_ref)
    new_model = (conditions.get("stage_models") or {}).get("target")
    if not new_model or new_model == row.model_ref:
        raise OwnerConflict(INVALID)
    if runtime_identity is None:
        raise OwnerConflict(INVALID)
    physical_receipt_hash = verify_terminal(row)
    old_runtime = runtime_identity(operation_ref=predecessor_ref, owner_invocation_hash=row.invocation_hash)
    new_runtime = runtime_identity()
    for identity in (old_runtime, new_runtime):
        if (not isinstance(identity, dict) or set(identity) != {"cli_version", "harness_adapter_ref"}
                or not all(isinstance(value, str) and value for value in identity.values())):
            raise OwnerConflict(INVALID)
    scope = {
        "schema_ref": SCOPE_SCHEMA, "quest_ref": row.quest_ref, "cycle_ref": row.cycle_ref,
        "target_ref": row.target_ref, "run_ref": run_ref, "attempt_ref": row.attempt_ref,
        "fence_ref": row.fence_ref, "predecessor_operation_ref": row.operation_ref,
        "predecessor_generation": row.generation, "predecessor_invocation_hash": row.invocation_hash,
        "successor_generation": generation, "old_model_ref": row.model_ref,
        "new_model_ref": new_model, "conditions_revision": conditions["revision"],
        "predecessor_runtime": old_runtime, "successor_runtime": new_runtime,
    }
    from meta_research.owners.human_collaboration import SQLiteHumanCollaborationFactVerifier

    try:
        SQLiteHumanCollaborationFactVerifier(database).verify_capability_authorization(
            requirement={"capability": CAPABILITY, "scope": scope}, receipt_ref=decision.receipt_ref,
        )
    except OwnerConflict as error:
        raise OwnerConflict(INVALID) from error
    return {
        "scope": scope,
        "authorization_ref": decision.authorization_ref,
        "authorization_receipt_ref": decision.receipt_ref,
        "authorization_receipt_hash": decision.receipt_hash,
        "confirmation_receipt_ref": decision.basis_confirmation_ref,
        "confirmation_receipt_hash": decision.basis_confirmation_hash,
        "predecessor_supervisor_receipt_hash": physical_receipt_hash,
    }


def sealed_binding(connection, operation_ref):
    rows = connection.execute(text(
        "SELECT json_extract(f.payload_json,'$.recovery_model_binding') AS binding, "
        "json_extract(f.payload_json,'$.recovery_model_binding_hash') AS binding_hash, "
        "op.run_ref,op.generation,op.model_ref FROM durable_feed f "
        "JOIN ar_harness_provider_operations op ON op.operation_ref=:operation_ref "
        "WHERE f.event_type='agent_runtime.harness_operation_started' "
        "AND json_extract(f.payload_json,'$.operation_ref')=op.operation_ref LIMIT 2"
    ), {"operation_ref": operation_ref}).all()
    if not rows:
        return None
    if len(rows) != 1:
        raise OwnerConflict(INVALID)
    row = rows[0]
    if row.binding is None and row.binding_hash is None:
        return None
    try:
        binding = json.loads(row.binding)
        scope = binding["scope"]
        if (canonical_hash(binding) != row.binding_hash or scope["schema_ref"] != SCOPE_SCHEMA
                or scope["run_ref"] != row.run_ref or scope["successor_generation"] != row.generation
                or scope["new_model_ref"] != row.model_ref
                or scope["predecessor_operation_ref"] != provider_operation_ref(row.run_ref, "harness_turn", row.generation - 1)
                or operation_ref != provider_operation_ref(row.run_ref, "harness_turn", row.generation)):
            raise OwnerConflict(INVALID)
        return binding
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict(INVALID) from error

