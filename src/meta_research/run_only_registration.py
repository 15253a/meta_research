"""Preserve verified executions whose assessment has not produced a result yet.

These are native runs with immutable input bindings, not a TargetCommit or an
EvaluationAttempt. The ordinary completion correction path remains in charge.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import text

from meta_research.bundle_protocol import projection_plain_value
from meta_research.formal_entities import (
    _checkpoint_paths, _ensure_input, _insert, _ref, _register_run_checkpoints,
    _variant_declaration, explicit_unexecuted_root_evidence, insert_variant_run,
    verified_target_input_asset_refs,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json

SCHEMA = "meta-research/unassessed-execution-inputs/v1"


def _unassessed_inventory(document):
    if explicit_unexecuted_root_evidence(document) is not None:
        return None
    declared = document.get("formal_runs")
    if declared is None:
        return None
    if not isinstance(declared, list) or not declared or len(declared) > 100:
        raise OwnerConflict("target_formal_work_inventory_invalid")
    runs, keys, has_assessment = [], set(), False
    for run in declared:
        if not isinstance(run, dict):
            raise OwnerConflict("target_formal_work_inventory_invalid")
        status = run.get("status", "executed")
        if status in {"blocked", "cancelled", "not_executed"}:
            continue
        key = run.get("run_key")
        if status != "executed" or not isinstance(key, str) or not key or len(key) > 128 or key in keys:
            raise OwnerConflict("target_formal_work_inventory_invalid")
        keys.add(key)
        revision_ref = run.get("implementation_revision_ref")
        if revision_ref is not None and (
                not isinstance(revision_ref, str)
                or not revision_ref or len(revision_ref) > 1024):
            raise OwnerConflict("target_formal_work_inventory_invalid")
        evaluations = run.get("evaluations", [])
        if not isinstance(evaluations, list) or len(evaluations) > 100:
            raise OwnerConflict("target_formal_work_inventory_invalid")
        for evaluation in evaluations:
            if not isinstance(evaluation, dict) or evaluation.get("status", "executed") not in {
                "executed", "blocked", "cancelled", "not_executed"
            }:
                raise OwnerConflict("target_formal_work_inventory_invalid")
            has_assessment |= evaluation.get("status", "executed") == "executed"
        _checkpoint_paths(run.get("checkpoint_paths"))
        runs.append(run)
    return runs if runs and not has_assessment else None


def _source_inputs(completion, manifest, target, authority, run, *, source_owner):
    proofs = projection_plain_value(completion.handle.accepted_input_asset_proofs)
    from meta_research.formal_run_bindings import implementation_binding, resolve_local_inputs
    entries = [entry.as_dict() for entry in manifest.entries]
    revision_ref, tree_hash = implementation_binding(entries, run.get('implementation_paths'),
                                                    run.get('implementation_revision_ref'))
    local_inputs = resolve_local_inputs(manifest.result_document.as_dict(), entries, run)
    defaults = list(dict.fromkeys([
        *completion.handle.accepted_input_target_commit_refs,
        *(proof["asset_ref"] for proof in proofs), manifest.implementation_revision_ref,
        revision_ref,
    ]))
    allowed = set(defaults) | verified_target_input_asset_refs(
        source_owner, target_ref=target.target_ref, proofs=proofs)
    selected = run.get("input_refs", defaults)
    if (not isinstance(selected, list) or any(not isinstance(ref, str) for ref in selected)
            or len(set(selected)) != len(selected) or not set(selected) <= allowed):
        raise OwnerConflict("target_formal_input_reference_invalid")
    return {
        "schema_ref": SCHEMA, "input_refs": list(dict.fromkeys([*selected, *local_inputs])), "run_key": run["run_key"],
        "target_ref": target.target_ref, "target_spec_hash": target.spec_hash,
        "target_run_ref": completion.handle.target_run_ref,
        "completion_ref": completion.completion_ref, "completion_payload_hash": completion.payload_hash,
        "completion_receipt": completion.receipt.as_public_dict(),
        "manifest_ref": manifest.manifest_ref, "manifest_payload_hash": manifest.payload_hash,
        "manifest_receipt": manifest.receipt.as_public_dict(),
        "authority_ref": authority.authority_ref, "authority_hash": authority.authority_hash,
        "implementation_revision_ref": revision_ref,
        "implementation_tree_hash": tree_hash,
        "accepted_input_asset_proofs": proofs,
        "accepted_input_target_commit_refs": list(completion.handle.accepted_input_target_commit_refs),
    }


def _check_source_rows(connection, completion, manifest, target, authority):
    for table, key, ref, expected in (
        ("ar_target_root_completions", "completion_ref", completion.completion_ref,
         {"payload_hash": completion.payload_hash, "receipt_ref": completion.receipt.receipt_ref,
          "receipt_hash": completion.receipt.payload_hash}),
        ("rm_target_root_completion_manifests", "manifest_ref", manifest.manifest_ref,
         {"payload_hash": manifest.payload_hash, "receipt_ref": manifest.receipt.receipt_ref,
          "receipt_hash": manifest.receipt.payload_hash}),
        ("rg_targets", "target_ref", target.target_ref, {"spec_hash": target.spec_hash}),
        ("rg_target_measurement_domain_authorities", "authority_ref", authority.authority_ref,
         {"authority_hash": authority.authority_hash}),
    ):
        row = connection.execute(text(f"SELECT * FROM {table} WHERE {key}=:ref"), {"ref": ref}).mappings().first()
        if row is None or any(row[name] != value for name, value in expected.items()):
            raise OwnerConflict("target_root_commit_issuer_stale")


def _verify_original_binding_source(owner, proof, run_ref):
    """A reused execution keeps its original immutable AR/RM execution source."""
    if proof.subject_kind != "variant_run" or proof.subject_ref != run_ref:
        raise OwnerConflict("target_formal_reused_run_invalid")
    manifest_ref = proof.inputs.get("manifest_ref")
    if manifest_ref is None:
        return  # Earlier native execution adapters use their own accepted input receipt.
    manifest = owner._target_root_manifest_reader.query(manifest_ref)
    completion = owner._target_root_completion_reader.query_completion_by_ref(proof.inputs.get("completion_ref"))
    if (manifest is None or completion is None or manifest.completion_ref != completion.completion_ref
            or manifest.target_ref != completion.handle.target_ref
            or proof.inputs.get("target_ref") != manifest.target_ref
            or proof.inputs.get("manifest_payload_hash") != manifest.payload_hash
            or proof.inputs.get("completion_ref") != manifest.completion_ref):
        raise OwnerConflict("target_unassessed_execution_integrity_invalid")
    from meta_research.formal_run_bindings import implementation_binding
    document = manifest.result_document.as_dict()
    selected_run = [run for run in document.get('formal_runs', [{'run_key': 'primary'}])
                    if run.get('run_key') == proof.inputs.get('run_key')]
    if len(selected_run) != 1:
        raise OwnerConflict("target_unassessed_execution_integrity_invalid")
    revision, tree_hash = implementation_binding([entry.as_dict() for entry in manifest.entries],
        selected_run[0].get('implementation_paths'), selected_run[0].get('implementation_revision_ref'))
    if (proof.inputs.get('implementation_revision_ref') != revision
            or proof.inputs.get('implementation_tree_hash') != tree_hash):
        raise OwnerConflict("target_unassessed_execution_integrity_invalid")
    target, authority, _, _ = owner._target_root_domain_context(completion=completion,
        manifest=manifest, result_document=manifest.result_document)
    if proof.inputs.get("schema_ref") == SCHEMA:
        runs = _unassessed_inventory(manifest.result_document.as_dict()) or []
        selected = [run for run in runs if run["run_key"] == proof.inputs.get("run_key")]
        if (len(selected) != 1 or selected[0].get("variant_run_ref")
                or _ref("variant_run", completion.completion_ref, selected[0]["run_key"]) != run_ref
                or proof.inputs != _source_inputs(completion, manifest, target, authority, selected[0], source_owner=owner)):
            raise OwnerConflict("target_unassessed_execution_integrity_invalid")


def _resolve_run_variant_declarations(connection, *, runs, target, accepted_at, verify_only):
    """Give run-only executions the method identity their work actually used.

    Same seam as the measured completion path: a declared contract resolves
    or registers the Baseline and gets-or-creates the Variant by recipe hash.
    """
    declared = {run["run_key"]: run for run in runs if _variant_declaration(run) is not None}
    if not declared:
        return
    from meta_research.baseline_identity import resolve_baseline_method_identity
    from meta_research.owners.research_graph import _get_or_create_target_measurement_identity
    quest_ref = connection.execute(text(
        "SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
        "WHERE t.target_ref=:ref"), {"ref": target.target_ref}).scalar_one_or_none()
    if not isinstance(quest_ref, str) or not quest_ref:
        raise OwnerConflict("target_formal_variant_declaration_invalid")
    for run in runs:
        declaration = _variant_declaration(run)
        if declaration is None:
            continue
        if run.get("variant_ref") or run.get("variant_run_ref"):
            raise OwnerConflict("target_formal_variant_declaration_invalid")
        if verify_only:
            # Verification only resolves what live acceptance registered.
            from meta_research.formal_entities import _lookup_declared_baseline
            baseline_ref = _lookup_declared_baseline(connection, declaration["forward"])
            natural = {"baseline_ref": baseline_ref,
                       "recipe_hash": canonical_hash(declaration["recipe"])}
            row = connection.execute(text(
                "SELECT variant_ref FROM rg_experiment_variants WHERE baseline_ref=:baseline_ref "
                "AND recipe_hash=:recipe_hash"), natural).first()
            if row is None:
                raise OwnerConflict("target_unassessed_execution_missing")
            run["variant_ref"] = str(row[0])
            continue
        baseline_ref, _created = resolve_baseline_method_identity(
            connection, forward=declaration["forward"], quest_ref=quest_ref,
            accepted_at=accepted_at)
        natural = {"baseline_ref": baseline_ref,
                   "recipe_hash": canonical_hash(declaration["recipe"])}
        run["variant_ref"], _variant_created = _get_or_create_target_measurement_identity(
            connection, table="rg_experiment_variants", ref_column="variant_ref",
            ref_prefix="variant", natural=natural,
            immutable={"recipe_json": canonical_json(declaration["recipe"])},
            insert_only={"accepted_at": accepted_at})


def _persist_runs(connection, *, completion, manifest, target, authority, runs, source_owner, verify_only=False):
    from meta_research.owners.research_graph import _accepted_experiment_input_binding

    inserted = {}
    def ensure(table, key, values, writer=None):
        row = connection.execute(text(f"SELECT * FROM {table} WHERE {key}=:ref"),
                                 {"ref": values[key]}).mappings().first()
        if row is not None:
            differing = {name for name, value in values.items() if row[name] != value}
            if differing and differing <= {"subject_kind", "subject_ref"} and table == "rg_experiment_asset_roles":
                # A later Owner attribution correction moved this role's
                # current subject; identity, version and receipts must match.
                adjusted = connection.execute(text(
                    "SELECT 1 FROM rg_experiment_asset_role_adjustments "
                    "WHERE role_ref = :ref LIMIT 1"),
                    {"ref": values["role_ref"]}).first()
                if adjusted is None:
                    raise OwnerConflict("target_unassessed_execution_integrity_invalid")
            elif differing:
                raise OwnerConflict("target_unassessed_execution_integrity_invalid")
        elif verify_only:
            raise OwnerConflict("target_unassessed_execution_missing")
        else:
            (writer or (lambda conn, vals: _insert(conn, table, vals)))(connection, values)
            inserted[table] = inserted.get(table, 0) + 1

    _check_source_rows(connection, completion, manifest, target, authority)
    entries = [entry.as_dict() for entry in manifest.entries if entry.role == "checkpoint"]
    new_runs = [run for run in runs if not run.get("variant_run_ref")]
    if len(new_runs) > 1 and entries and any(run.get("checkpoint_paths") is None for run in new_runs):
        raise OwnerConflict("target_formal_checkpoint_assignment_required")
    _resolve_run_variant_declarations(connection, runs=runs, target=target,
                                      accepted_at=completion.accepted_at,
                                      verify_only=verify_only)
    from meta_research.formal_entities import verify_formal_variant_scope
    verify_formal_variant_scope(connection, target_ref=target.target_ref,
        variant_refs=[run.get('variant_ref', authority.identities.variant_ref) for run in runs])
    records = []
    for run in runs:
        variant_ref = run.get("variant_ref", authority.identities.variant_ref)
        definition = connection.execute(text("SELECT baseline_ref,recipe_json,recipe_hash FROM "
            "rg_experiment_variants WHERE variant_ref=:ref"), {"ref": variant_ref}).first()
        # The Variant must exist and stay internally consistent, but it does
        # not have to belong to the Bundle draft's Baseline: run-only
        # executions may carry their own real method attribution.
        if (definition is None
                or canonical_hash(json.loads(definition.recipe_json)) != definition.recipe_hash):
            raise OwnerConflict("target_formal_variant_definition_invalid")
        reused = bool(run.get("variant_run_ref"))
        run_ref = run.get("variant_run_ref") or _ref("variant_run", completion.completion_ref, run["run_key"])
        at = completion.accepted_at
        if reused:
            row = connection.execute(text("SELECT r.*,b.inputs_json,b.inputs_hash,b.receipt_ref,b.receipt_hash,"
                "b.subject_kind,b.subject_ref,b.binding_ref FROM rg_variant_runs r JOIN rg_experiment_input_bindings b "
                "ON b.binding_ref=r.input_binding_ref WHERE r.variant_run_ref=:ref"), {"ref": run_ref}).mappings().first()
            if row is None or row["variant_ref"] != variant_ref or row["status"] != "executed":
                raise OwnerConflict("target_formal_reused_run_invalid")
            original = _accepted_experiment_input_binding(SimpleNamespace(**row))
            if original.subject_kind != "variant_run" or original.subject_ref != run_ref:
                raise OwnerConflict("target_formal_reused_run_invalid")
            if "input_refs" in run and run["input_refs"] != original.inputs.get("input_refs"):
                raise OwnerConflict("target_formal_reused_run_input_conflict")
            if (run.get('implementation_paths') is not None or run.get('local_inputs')
                    or run.get('implementation_revision_ref', original.inputs.get('implementation_revision_ref'))
                    != original.inputs.get('implementation_revision_ref')):
                raise OwnerConflict("target_formal_reused_run_input_conflict")
            bound_inputs = original.inputs
        else:
            inputs = _source_inputs(completion, manifest, target, authority, run, source_owner=source_owner)
            binding_ref = _ref("variant_input", run_ref)
            _ensure_input(ensure, binding_ref, "variant_run", run_ref, inputs, at)
            ensure("rg_variant_runs", "variant_run_ref", {
                "variant_run_ref": run_ref, "variant_ref": variant_ref, "input_binding_ref": binding_ref,
                "implementation_revision_ref": inputs["implementation_revision_ref"],
                "status": "executed", "created_at": at, "updated_at": at,
            }, insert_variant_run)
            bound_inputs = inputs
        association_ref = _ref("root_unassessed_run", completion.completion_ref, run["run_key"])
        prior = connection.execute(text('SELECT checkpoint_roles_json FROM rg_target_root_unassessed_runs '
            'WHERE association_ref=:ref'), {'ref': association_ref}).first()
        frozen_refs = [row['role_ref'] for row in json.loads(prior.checkpoint_roles_json)] if prior else None
        checkpoints = _register_run_checkpoints(connection, ensure=ensure,
            item={"variant_run_ref": run_ref, "reuse_variant_run": reused,
                  "checkpoint_paths": run.get("checkpoint_paths"),
                  "checkpoint_role_refs": run.get("checkpoint_role_refs"),
                  "checkpoint_version_refs": run.get("checkpoint_version_refs"),
                  "frozen_checkpoint_role_refs": frozen_refs}, entries=entries, accepted_at=at)
        if not reused:
            actual_roles = connection.execute(text("SELECT role_ref FROM rg_experiment_asset_roles WHERE "
                "subject_kind='variant_run' AND subject_ref=:ref AND role='checkpoint_artifact' ORDER BY ordinal,accepted_at,role_ref"),
                {"ref": run_ref}).scalars().all()
            adjusted = {row[0] for row in connection.execute(text(
                "SELECT role_ref FROM rg_experiment_asset_role_adjustments"))}
            # Roles whose current attribution was later corrected by an Owner
            # adjustment no longer witness this original checkpoint selection.
            if ([ref for ref in actual_roles if ref not in adjusted]
                    != [item["role_ref"] for item in checkpoints
                        if item["role_ref"] not in adjusted]):
                raise OwnerConflict("target_unassessed_execution_integrity_invalid")
        row = connection.execute(text("SELECT * FROM rg_variant_runs WHERE variant_run_ref=:ref"),
                                 {"ref": run_ref}).mappings().one()
        association_ref = _ref("root_unassessed_run", completion.completion_ref, run["run_key"])
        ensure("rg_target_root_unassessed_runs", "association_ref", {
            "association_ref": association_ref, "target_ref": target.target_ref,
            "completion_ref": completion.completion_ref, "manifest_ref": manifest.manifest_ref,
            "authority_ref": authority.authority_ref, "run_key": run["run_key"],
            "variant_run_ref": run_ref, "input_binding_ref": row["input_binding_ref"],
            "input_binding_hash": canonical_hash(bound_inputs),
            "checkpoint_roles_json": canonical_json(checkpoints), "checkpoint_roles_hash": canonical_hash(checkpoints),
        })
        records.append({"run_key": run["run_key"], "variant_run_ref": run_ref,
            "attempt_key": None, "measurement_ref": None,
            "evaluation_attempt_ref": None, "metric_result_ref": None, "target_commit_ref": None,
            "variant_run": {**dict(row), "inputs": bound_inputs}, "evaluation_attempt": None, "metric_result": None,
            "completion_ref": completion.completion_ref, "manifest_ref": manifest.manifest_ref,
            "association_ref": association_ref,
            "checkpoint_roles": checkpoints})
    for table, counter in (("rg_variant_runs", "variant_run_count"),
                           ("rg_experiment_input_bindings", "experiment_input_binding_count"),
                           ("rg_experiment_asset_roles", "experiment_asset_role_count")):
        if inserted.get(table):
            connection.execute(text(f"UPDATE research_graph_state SET {counter}={counter}+:count "
                                    "WHERE singleton='owner'"), {"count": inserted[table]})
    if inserted:
        connection.execute(text("UPDATE research_graph_state SET revision=revision+1 WHERE singleton='owner'"))
    return records


def preserve_unassessed_runs(owner, *, completion, manifest, result_document, domain_context):
    """Called only after the ordinary completion issuer/domain checks succeeded."""
    from meta_research.target_run_finalizer import TargetRootOwnerRejection

    runs = _unassessed_inventory(result_document.as_dict())
    if runs is None:
        return None
    from meta_research.owners.research_graph import _accepted_experiment_input_binding
    for run in runs:
        if not run.get("variant_run_ref"):
            continue
        with owner._database.read() as connection:
            binding = connection.execute(text("SELECT b.* FROM rg_variant_runs r JOIN rg_experiment_input_bindings b "
                "ON b.binding_ref=r.input_binding_ref WHERE r.variant_run_ref=:ref"),
                {"ref": run["variant_run_ref"]}).mappings().first()
        if binding is None:
            raise OwnerConflict("target_formal_reused_run_invalid")
        _verify_original_binding_source(owner, _accepted_experiment_input_binding(SimpleNamespace(**binding)),
                                        run["variant_run_ref"])
    current = owner._verify_target_root_issuers(completion=completion, manifest=manifest,
                                               result_document=result_document)
    context = owner._target_root_domain_context(completion=completion, manifest=manifest,
                                                result_document=result_document)
    if current != (completion, manifest) or context != domain_context:
        raise OwnerConflict("target_root_commit_issuer_stale")
    target, authority, _, _ = context
    with owner._database.fenced_write() as connection:
        records = _persist_runs(connection, completion=completion, manifest=manifest,
                                target=target, authority=authority, runs=runs, source_owner=owner)
    material = {"completion_ref": completion.completion_ref, "manifest_ref": manifest.manifest_ref,
                "manifest_payload_hash": manifest.payload_hash,
                "runs": [{"run_key": row["run_key"], "variant_run_ref": row["variant_run_ref"]} for row in records]}
    digest = canonical_hash(material)
    feedback = ("The actual executions are registered: " + canonical_json(material["runs"]) +
        ". No evaluation result, metric or TargetCommit was created because all assessments are unexecuted. "
        "Continue the research or wait for the missing prerequisite. When an assessment becomes possible, "
        "reuse these exact variant_run_ref values for the same executions and report its actual metrics.")
    rejection_ref = "rg_unassessed_execution_" + digest[:32]
    return TargetRootOwnerRejection(issuer="research_graph", rejection_ref=rejection_ref,
        code="target_formal_assessment_pending", feedback=feedback,
        receipt=AcceptanceReceipt(issuer="research_graph", kind="target_root_completion_rejected",
            receipt_ref="rg_unassessed_receipt_" + digest[:32], subject_ref=completion.completion_ref,
            payload_hash=canonical_hash({**material, "rejection_ref": rejection_ref, "feedback": feedback})))


def query_unassessed_runs(owner, target_ref):
    """Revalidate original issuer objects and native bindings, including old generations."""
    from meta_research.owners.research_graph import _accepted_experiment_input_binding

    with owner._database.read() as connection:
        rows = connection.execute(text("SELECT a.*,b.inputs_json,b.inputs_hash,b.subject_kind,b.subject_ref,"
            "b.binding_ref,b.receipt_ref,b.receipt_hash FROM rg_target_root_unassessed_runs a "
            "JOIN rg_experiment_input_bindings b ON b.binding_ref=a.input_binding_ref "
            "WHERE a.target_ref=:target ORDER BY a.completion_ref,a.run_key"),
            {"target": target_ref}).mappings().all()
    sources = {}
    for row in rows:
        proof = _accepted_experiment_input_binding(SimpleNamespace(**row))
        if (proof.subject_kind != "variant_run" or proof.subject_ref != row["variant_run_ref"]
                or proof.inputs_hash != row["input_binding_hash"]):
            raise OwnerConflict("target_unassessed_execution_integrity_invalid")
        _verify_original_binding_source(owner, proof, row["variant_run_ref"])
        sources[row["manifest_ref"]] = row["completion_ref"]
    records = []
    for manifest_ref, completion_ref in sources.items():
        manifest = owner._target_root_manifest_reader.query(manifest_ref)
        completion = owner._target_root_completion_reader.query_completion_by_ref(completion_ref)
        if (manifest is None or completion is None or manifest.completion_ref != completion_ref
                or manifest.target_ref != target_ref or completion.handle.target_ref != target_ref):
            raise OwnerConflict("target_unassessed_execution_integrity_invalid")
        target, authority, _, _ = owner._target_root_domain_context(completion=completion,
            manifest=manifest, result_document=manifest.result_document)
        runs = _unassessed_inventory(manifest.result_document.as_dict())
        if runs is None:
            raise OwnerConflict("target_unassessed_execution_integrity_invalid")
        with owner._database.read() as connection:
            records.extend(_persist_runs(connection, completion=completion, manifest=manifest,
                target=target, authority=authority, runs=runs, source_owner=owner, verify_only=True))
    if {record["association_ref"] for record in records} != {row["association_ref"] for row in rows}:
        raise OwnerConflict("target_unassessed_execution_integrity_invalid")
    return tuple({record["variant_run_ref"]: record for record in records}.values())
