"""Current artifact attribution is correctable through the RG Owner (Q2).

Moving a role's subject keeps the immutable receipt, the exact content version
and every historical reference resolvable; acceptance replays tolerate the
adjusted current attribution.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_json
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def _accepted_run_with_two_work_items(tmp_path):
    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    path = workspace / "outputs/metrics.json"
    document = json.loads(path.read_text())
    metrics = document["metrics"]
    document["formal_runs"] = [
        {"run_key": "measured-work", "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
        {"run_key": "audit-work", "artifact_paths": ["logs/train.log"], "evaluations": []},
    ]
    document["metrics"] = {}
    path.write_text(canonical_json(document))
    accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
    return runtime, authority, handle, accepted


def _role_row(runtime, subject_ref):
    with runtime._database.read() as connection:
        return connection.execute(text(
            "SELECT * FROM rg_experiment_asset_roles WHERE subject_ref = :ref "
            "ORDER BY role, ordinal LIMIT 1"), {"ref": subject_ref}).mappings().one()


def _other_run_ref(runtime, handle):
    with runtime._database.read() as connection:
        return connection.execute(text(
            "SELECT r.variant_run_ref FROM rg_variant_runs r "
            "JOIN rg_target_root_formal_entities l ON l.variant_run_ref = r.variant_run_ref "
            "JOIN rg_target_root_measurements m ON m.measurement_ref = l.measurement_ref "
            "WHERE m.target_ref = :target AND l.run_key = 'audit-work'"),
            {"target": handle.target_ref}).scalar_one()


def _attempt_ref(runtime, handle):
    with runtime._database.read() as connection:
        return connection.execute(text(
            "SELECT l.evaluation_attempt_ref FROM rg_target_root_formal_entities l "
            "JOIN rg_target_root_measurements m ON m.measurement_ref = l.measurement_ref "
            "WHERE m.target_ref = :target AND l.evaluation_attempt_ref IS NOT NULL"),
            {"target": handle.target_ref}).scalar_one()


def test_adjustment_moves_current_attribution_and_keeps_receipts(tmp_path):
    runtime, authority, handle, accepted = _accepted_run_with_two_work_items(tmp_path)
    try:
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        original_receipt = (role["receipt_ref"], role["receipt_hash"])
        original_version = role["version_ref"]

        result = graph.adjust_experiment_artifact_role(
            role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=attempt_ref,
            reason="Mis-filed: this report belongs to the assessment, not the run.",
            idempotency_key="adjust-1")
        assert result["from_subject_ref"] == other_run
        assert result["to_subject_ref"] == attempt_ref

        with runtime._database.read() as connection:
            moved = connection.execute(text(
                "SELECT * FROM rg_experiment_asset_roles WHERE role_ref = :ref"),
                {"ref": role["role_ref"]}).mappings().one()
            adjustment = connection.execute(text(
                "SELECT * FROM rg_experiment_asset_role_adjustments WHERE role_ref = :ref"),
                {"ref": role["role_ref"]}).mappings().one()
        assert moved["subject_ref"] == attempt_ref
        assert moved["version_ref"] == original_version
        assert (moved["receipt_ref"], moved["receipt_hash"]) == original_receipt
        assert adjustment["reason"].startswith("Mis-filed")

        # Idempotent replay of the same effect returns the same adjustment.
        replay = graph.adjust_experiment_artifact_role(
            role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=attempt_ref,
            reason="Mis-filed: this report belongs to the assessment, not the run.",
            idempotency_key="adjust-1")
        assert replay == result
        with pytest.raises(OwnerConflict, match="artifact_role_adjustment_idempotency_conflict"):
            graph.adjust_experiment_artifact_role(
                role_ref=role["role_ref"], to_subject_kind="variant_run",
                to_subject_ref=other_run, reason="different target",
                idempotency_key="adjust-1")
        assert graph.reconcile_artifact_role_adjustment(idempotency_key="adjust-1") == result
        assert graph.reconcile_artifact_role_adjustment(idempotency_key="adjust-other") is None
    finally:
        runtime.close()


def test_adjustment_rejects_missing_subject_and_noop(tmp_path):
    runtime, authority, handle, accepted = _accepted_run_with_two_work_items(tmp_path)
    try:
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        role = _role_row(runtime, attempt_ref)
        with pytest.raises(OwnerConflict, match="artifact_role_adjustment_subject_not_found"):
            graph.adjust_experiment_artifact_role(
                role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
                to_subject_ref="evaluation_attempt_does_not_exist", reason="x",
                idempotency_key="adjust-2")
        with pytest.raises(OwnerConflict, match="artifact_role_adjustment_noop"):
            graph.adjust_experiment_artifact_role(
                role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
                to_subject_ref=attempt_ref, reason="same place",
                idempotency_key="adjust-3")
        with pytest.raises(OwnerConflict, match="artifact_role_not_found"):
            graph.adjust_experiment_artifact_role(
                role_ref="role_does_not_exist", to_subject_kind="variant_run",
                to_subject_ref=handle.target_run_ref, reason="x",
                idempotency_key="adjust-4")
    finally:
        runtime.close()


def test_historical_acceptance_replay_tolerates_adjusted_attribution(tmp_path):
    runtime, lifecycle, memory, _authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        document["formal_runs"] = [
            {"run_key": "measured-work", "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
            {"run_key": "audit-work", "artifact_paths": ["logs/train.log"], "evaluations": []},
        ]
        document["metrics"] = {}
        path.write_text(canonical_json(document))
        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        graph.adjust_experiment_artifact_role(
            role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=attempt_ref,
            reason="Refiled after acceptance.", idempotency_key="adjust-replay")

        # Replaying the immutable acceptance must not report an integrity fault
        # for the legitimately adjusted subject columns.
        replay, _ = _accept(runtime, lifecycle, memory, handle, evidence)
        assert replay == accepted
        with runtime._database.read() as connection:
            moved = connection.execute(text(
                "SELECT subject_ref FROM rg_experiment_asset_roles WHERE role_ref = :ref"),
                {"ref": role["role_ref"]}).scalar_one()
        assert moved == attempt_ref
    finally:
        runtime.close()


def test_accepted_role_hash_validates_against_pre_adjustment_subject():
    """The role receipt hash validates against the subject at issuance time.

    Pure unit level (no database): a row whose current subject was moved by an
    Owner adjustment validates when it carries the earliest adjustment origin
    (_adjusted_from_kind/_adjusted_from_ref) and is rejected without it. The
    returned role keeps the current, corrected subject.
    """
    from types import SimpleNamespace

    from meta_research.owners.research_graph import (
        EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
        AcceptanceReceipt,
        AcceptedAssetBinding,
        _accepted_experiment_asset_role,
        _receipt_hash,
    )

    binding = AcceptedAssetBinding(
        asset_ref="asset_ref_1",
        version_ref="rmver_ref_1",
        content_hash="c" * 8,
        manifest_hash="m" * 8,
        receipt=AcceptanceReceipt(
            issuer="research_memory",
            kind="asset_acceptance",
            receipt_ref="rmar_ref_1",
            subject_ref="rmver_ref_1",
            payload_hash="p" * 8,
        ),
    )
    original_kind, original_ref = "variant_run", "variant_run_ref_1"
    receipt_hash = _receipt_hash(
        EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
        "role_ref_1",
        {
            "subject_kind": original_kind,
            "subject_ref": original_ref,
            "role": "log_asset",
            "ordinal": 0,
            "asset": binding.as_dict(),
        },
    )

    def make_row(with_origin):
        row = SimpleNamespace(
            role_ref="role_ref_1",
            subject_kind="evaluation_attempt",
            subject_ref="evaluation_attempt_ref_1",
            role="log_asset",
            ordinal=0,
            asset_ref=binding.asset_ref,
            version_ref=binding.version_ref,
            content_hash=binding.content_hash,
            manifest_hash=binding.manifest_hash,
            asset_receipt_ref=binding.receipt.receipt_ref,
            asset_receipt_hash=binding.receipt.payload_hash,
            receipt_ref="role_receipt_ref_1",
            receipt_hash=receipt_hash,
        )
        if with_origin:
            row._adjusted_from_kind = original_kind
            row._adjusted_from_ref = original_ref
        return row

    accepted_role = _accepted_experiment_asset_role(make_row(with_origin=True))
    assert accepted_role.subject_kind == "evaluation_attempt"
    assert accepted_role.subject_ref == "evaluation_attempt_ref_1"
    with pytest.raises(OwnerConflict, match="experiment_asset_role_invalid"):
        _accepted_experiment_asset_role(make_row(with_origin=False))

    # Invariant negative with a fully legal hash: result_content never
    # belongs to a variant_run subject, adjustment origin or not.
    invariant_hash = _receipt_hash(
        EXPERIMENT_ASSET_ROLE_RECEIPT_KIND,
        "role_ref_2",
        {
            "subject_kind": "variant_run",
            "subject_ref": original_ref,
            "role": "result_content",
            "ordinal": 0,
            "asset": binding.as_dict(),
        },
    )
    invariant_row = SimpleNamespace(
        role_ref="role_ref_2",
        subject_kind="variant_run",
        subject_ref=original_ref,
        role="result_content",
        ordinal=0,
        asset_ref=binding.asset_ref,
        version_ref=binding.version_ref,
        content_hash=binding.content_hash,
        manifest_hash=binding.manifest_hash,
        asset_receipt_ref=binding.receipt.receipt_ref,
        asset_receipt_hash=binding.receipt.payload_hash,
        receipt_ref="role_receipt_ref_2",
        receipt_hash=invariant_hash,
    )
    with pytest.raises(OwnerConflict, match="experiment_asset_role_invalid"):
        _accepted_experiment_asset_role(invariant_row)


def test_adjusted_role_public_readback_keeps_receipt_valid(tmp_path):
    """Public read paths keep resolving a role after it was moved.

    Red line for the P0-1 conflict family: this fixture produces a root-flow
    attempt (not a native measurement attempt), so the public red line here is
    `query_target_formal_results` — the accepted hierarchy must keep reading
    back with the corrected attribution and the unchanged content version.
    The hash-validating readbacks are pinned by the pure unit test and by the
    reuse replay test below.
    """
    runtime, authority, handle, accepted = _accepted_run_with_two_work_items(tmp_path)
    try:
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        original_receipt = (role["receipt_ref"], role["receipt_hash"])

        graph.adjust_experiment_artifact_role(
            role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=attempt_ref,
            reason="Refiled onto the assessment before readback.",
            idempotency_key="adjust-readback")

        results = graph.query_target_formal_results(handle.target_ref)
        measured = [item for item in results
                    if item.get("evaluation_attempt_ref") == attempt_ref]
        assert measured, "measured work item missing from public formal results"
        moved = [entry for entry in measured[0]["evaluation_artifacts"]
                 if entry["version_ref"] == role["version_ref"]]
        assert moved, "moved role missing from public formal results readback"
        assert moved[0]["role"] == role["role"]
        assert (moved[0]["content_hash"], moved[0]["manifest_hash"]) == (
            role["content_hash"], role["manifest_hash"])
        audit = [item for item in results if item.get("run_key") == "audit-work"]
        assert audit and all(
            entry["version_ref"] != role["version_ref"]
            for entry in audit[0]["run_artifacts"])
        with runtime._database.read() as connection:
            kept = connection.execute(text(
                "SELECT receipt_ref, receipt_hash FROM rg_experiment_asset_roles "
                "WHERE role_ref = :ref"),
                {"ref": role["role_ref"]}).mappings().one()
        assert (kept["receipt_ref"], kept["receipt_hash"]) == original_receipt
    finally:
        runtime.close()


def test_artifact_roles_adjust_operation_dispatches_through_gateway(tmp_path):
    """The MCP operation layer reaches the owner method with matching kwargs.

    Pins the P0-2 class of signature drift between the gateway handler and
    `adjust_experiment_artifact_role`: a mismatch surfaces as an uncaught
    TypeError, while proper dispatch translates OwnerConflict into
    SemanticMcpError carrying the owner error code. Same-effect replay through
    the operation must stay idempotent.
    """
    from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError
    from meta_research.semantic_owner_gateway import _artifact_role_adjust

    runtime, authority, handle, accepted = _accepted_run_with_two_work_items(tmp_path)
    try:
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        with runtime._database.read() as connection:
            quest_ref = connection.execute(text(
                "SELECT g.quest_ref FROM rg_target_graphs g JOIN rg_targets t "
                "ON t.graph_ref = g.graph_ref WHERE t.target_ref = :target"),
                {"target": handle.target_ref}).scalar_one()

        class _AgentRuntime:
            def verify_root_agent_runtime_scope(self, **kwargs):
                return {"quest_ref": quest_ref}

        context = SemanticCallContext(
            run_ref=handle.target_run_ref,
            attempt_ref=handle.execution_attempt_ref,
            root_session_ref=handle.root_session_ref,
            fence_ref=handle.execution_fence_ref,
            capability_binding_hash="fixture-capability-hash",
            root_kind="target",
            phase="bundle",
            operation_id="research_graph.artifact_roles.adjust")

        with pytest.raises(SemanticMcpError) as excinfo:
            _artifact_role_adjust(graph, _AgentRuntime(), context, {
                "role_ref": "role_does_not_exist",
                "to_subject_kind": "variant_run",
                "to_subject_ref": handle.target_run_ref,
                "reason": "x", "effect_id": "gateway-missing"})
        assert excinfo.value.code == "artifact_role_not_found"

        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        arguments = {
            "role_ref": role["role_ref"],
            "to_subject_kind": "evaluation_attempt",
            "to_subject_ref": attempt_ref,
            "reason": "Refiled through the gateway operation.",
            "effect_id": "gateway-adjust"}
        result = _artifact_role_adjust(graph, _AgentRuntime(), context, arguments)
        assert result["status"] == "accepted"
        replay = _artifact_role_adjust(graph, _AgentRuntime(), context, arguments)
        assert replay == result
        with pytest.raises(SemanticMcpError) as excinfo:
            _artifact_role_adjust(graph, _AgentRuntime(), context, {
                **arguments, "reason": "different payload, same effect id"})
        assert excinfo.value.code == "artifact_role_adjustment_idempotency_conflict"
    finally:
        runtime.close()


def test_reuse_reconciles_after_artifact_moved_away(tmp_path):
    """Reusing a subject whose artifact was adjusted away still reconciles.

    Red line for the reuse set comparison: the original manifest selection
    keeps naming the moved-away version's deterministic role_ref, so reuse must
    exclude both adjustment directions before comparing against current roles.
    """
    from meta_research.formal_entities import register_root_entities, root_work_items

    runtime, lifecycle, memory, authority, handle, workspace, evidence = (
        _root_finalizer_fixture(tmp_path))
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        metrics = document["metrics"]
        document["formal_runs"] = [
            {"run_key": "measured-work",
             "evaluations": [{"attempt_key": "main", "metrics": metrics}]},
            {"run_key": "audit-work", "evaluations": [],
             "artifact_paths": ["logs/train.log"]},
        ]
        document["metrics"] = {}
        path.write_text(canonical_json(document))
        accepted, _manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        assert role["role"] == "log_asset"
        graph.adjust_experiment_artifact_role(
            role_ref=role["role_ref"], to_subject_kind="evaluation_attempt",
            to_subject_ref=attempt_ref,
            reason="Refiled before reuse reconciliation.",
            idempotency_key="adjust-reuse")

        with runtime._database.read() as connection:
            root = connection.execute(text(
                "SELECT * FROM rg_target_root_measurements WHERE target_ref = :ref"),
                {"ref": handle.target_ref}).mappings().one()
            authority_row = connection.execute(text(
                "SELECT * FROM rg_target_measurement_domain_authorities "
                "WHERE authority_ref = :ref"),
                {"ref": root["authority_ref"]}).mappings().one()
            manifest_row = connection.execute(text(
                "SELECT * FROM rm_target_root_completion_manifests "
                "WHERE manifest_ref = :ref"),
                {"ref": root["manifest_ref"]}).mappings().one()
            completion = connection.execute(text(
                "SELECT * FROM ar_target_root_completions WHERE completion_ref = :ref"),
                {"ref": root["completion_ref"]}).mappings().one()
            commit_ref = connection.execute(text(
                "SELECT commit_ref FROM rg_target_commits WHERE target_ref = :ref"),
                {"ref": handle.target_ref}).scalar_one()
            items = root_work_items(
                payload=json.loads(root["measurement_payload_json"]),
                identities=authority_row,
                result_document=json.loads(manifest_row["result_document_json"]))
            for item in items:
                item["reuse_variant_run"] = True
                if item["evaluation_attempt_ref"] is not None:
                    item["reuse_evaluation_attempt"] = True
            reconciled = register_root_entities(
                connection, root=root, authority=authority_row,
                manifest=manifest_row, completion=completion,
                commit_ref=commit_ref, work_items=items, verify_only=True)
        assert reconciled is not None
    finally:
        runtime.close()


def test_adjustment_rejects_role_subject_invariant_and_foreign_quest(tmp_path):
    runtime, authority, handle, accepted = _accepted_run_with_two_work_items(tmp_path)
    try:
        graph = runtime.owners.research_graph
        attempt_ref = _attempt_ref(runtime, handle)
        other_run = _other_run_ref(runtime, handle)
        role = _role_row(runtime, other_run)
        with runtime._database.read() as connection:
            quest_ref = connection.execute(text(
                'SELECT quest_ref FROM rg_quests LIMIT 1')).scalar_one()
            result_content = connection.execute(text(
                "SELECT * FROM rg_experiment_asset_roles WHERE role = 'result_content' "
                'LIMIT 1')).mappings().one()
        # result_content belongs to the assessment: moving it onto a run is
        # rejected by the role/subject invariant before anything is written.
        with pytest.raises(OwnerConflict, match='artifact_role_adjustment_role_subject_invalid'):
            graph.adjust_experiment_artifact_role(
                role_ref=result_content['role_ref'], to_subject_kind='variant_run',
                to_subject_ref=other_run, reason='Invariant probe.',
                idempotency_key='adjust-invariant')
        # A scoped effect only moves artifacts inside its own Quest.
        with pytest.raises(OwnerConflict, match='artifact_role_adjustment_quest_scope_invalid'):
            graph.adjust_experiment_artifact_role(
                role_ref=role['role_ref'], to_subject_kind='evaluation_attempt',
                to_subject_ref=attempt_ref,
                reason='Foreign quest probe.', idempotency_key='adjust-scope',
                quest_ref=quest_ref + '-foreign')
        scoped = graph.adjust_experiment_artifact_role(
            role_ref=role['role_ref'], to_subject_kind='evaluation_attempt',
            to_subject_ref=attempt_ref,
            reason='Own quest probe.', idempotency_key='adjust-scope-own',
            quest_ref=quest_ref)
        assert scoped['to_subject_ref'] == attempt_ref
    finally:
        runtime.close()
