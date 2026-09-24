from __future__ import annotations

from copy import deepcopy

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _baseline_operations

from test_public_bundle_stage import (
    _ParallelTwoTargetBundleSkill, _bundle_runtime,
    _confirm_direct_quest, _finish_idea_stage, _finish_plan_stage,
)
from test_target_measurement_domain_authority import _accept_initial_graph
from test_bundle_exhaustion_owner import _advance_to_rg_rejection


class _StableMethodSkill(_ParallelTwoTargetBundleSkill):
    second_method_update = None

    def _target_plan(self, request):
        document = super()._target_plan(request)
        candidates = document["initial_strategy_update"]["candidates"]
        first = candidates[0]["measurement_contract"]
        for index, candidate in enumerate(candidates):
            measurement = candidate["measurement_contract"]
            measurement["baseline_forward_contract"] = {
                "method_key": "audit/partition-integrity",
                "method_version": "1",
                "method_contract": {
                    "meaning": "Audit a supplied partition for overlap",
                    "inputs": {"partition": "subject-indexed assignments"},
                    "outputs": {"overlap": "count of cross-partition subjects"},
                },
                "run_bindings": {
                    "upstream_commit_ref": f"commit:{index}",
                    "dataset_version_ref": f"dataset-version:{index}",
                    "workspace_path": f"/tmp/audit-{index}",
                },
                "notes": f"Research interpretation {index}",
            }
            measurement["evaluation_protocol_lineage"] = deepcopy(first["evaluation_protocol_lineage"])
            measurement["protocol_version"] = deepcopy(first["protocol_version"])
        if self.second_method_update:
            candidates[1]["measurement_contract"]["baseline_forward_contract"].update(
                deepcopy(self.second_method_update))
        return document


class _LegacyMethodSkill(_ParallelTwoTargetBundleSkill):
    forward = {
        "method_key": "legacy/audit",
        "inputs": "accepted observations",
        "outputs": "audit results",
        "upstream_commit_ref": "historical-commit",
        "notes": "Historical interpretation",
    }

    def _target_plan(self, request):
        document = super()._target_plan(request)
        for candidate in document["initial_strategy_update"]["candidates"]:
            candidate["measurement_contract"]["baseline_forward_contract"] = deepcopy(self.forward)
        return document


class _CorrectingIdentitySkill(_StableMethodSkill):
    initial_error = "baseline_method_identity_required"

    def __init__(self):
        self.correction_requests = []

    def _target_plan(self, request):
        document = super()._target_plan(request)
        if request.owner_feedback:
            self.correction_requests.append(request)
            return document
        candidates = document["initial_strategy_update"]["candidates"]
        if self.initial_error == "baseline_method_identity_required":
            for candidate in candidates:
                candidate["measurement_contract"]["baseline_forward_contract"] = deepcopy(_LegacyMethodSkill.forward)
        else:
            candidates[1]["measurement_contract"]["baseline_forward_contract"]["method_contract"] = {
                "inputs": "different observations", "outputs": "different claim"}
        return document


@pytest.mark.parametrize("reason", ["baseline_method_identity_required", "baseline_method_version_content_conflict"])
def test_identity_rejection_is_replayed_and_delivered_to_a_correcting_bundle(tmp_path, reason):
    skill = _CorrectingIdentitySkill()
    skill.initial_error = reason
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=skill)
    try:
        request, run, rejection = _advance_to_rg_rejection(runtime)
        owner = runtime.owners.research_graph
        assert rejection.reason_code == reason
        assert owner.query_baselines()["items"] == []
        replayed = owner.decide_target_graph_submission(
            request_ref=request.request_ref, run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref, submission_ref=rejection.submission_ref,
            context_pack_ref=request.context_pack_ref, target_plan=rejection.target_plan,
            target_plan_hash=rejection.target_plan_hash, execution_payload_hash=rejection.execution_payload_hash,
            execution_receipt=rejection.execution_receipt)
        assert replayed == rejection
        for _step in range(12):
            assert runtime.bundle_stage.process_once()
            graph = owner.query_target_graph(request.request_ref)
            if graph is not None:
                break
        else:
            raise AssertionError("Bundle did not correct its rejected method identity")
        assert len(skill.correction_requests) == 1
        correction = skill.correction_requests[0]
        assert correction.owner_rejection_kind == "domain"
        assert correction.owner_feedback == rejection.feedback
        assert reason in " ".join(correction.owner_feedback)
        assert correction.owner_rejection_receipt_ref == rejection.receipt.receipt_ref
        assert graph.attempt_ref != run.attempt_ref
        assert len(owner.query_baselines()["items"]) == 1
        assert owner.query_target_graph_rejection(rejection.submission_ref) == rejection
    finally:
        runtime.close()


def test_unregistered_legacy_contract_requires_explicit_method_identity(tmp_path):
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=_LegacyMethodSkill())
    try:
        _request, _run, rejection = _advance_to_rg_rejection(runtime)
        assert rejection.reason_code == "baseline_method_identity_required"
        assert runtime.owners.research_graph.query_baselines()["items"] == []
    finally:
        runtime.close()


def test_corrupted_historical_method_is_not_downgraded_to_a_bundle_correction(tmp_path):
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=_LegacyMethodSkill())
    try:
        _confirm_direct_quest(runtime)
        with runtime._database.write() as connection:
            quest_ref = connection.execute(text("SELECT quest_ref FROM rg_quests")).scalar_one()
            connection.execute(text(
                "INSERT INTO rg_experiment_baselines (baseline_ref, quest_ref, forward_contract_json, "
                "forward_contract_hash, accepted_at) VALUES ('baseline_corrupted', :quest, :document, :hash, 1.0)"),
                {"quest": quest_ref, "document": canonical_json({"meaning": "tampered stored bytes"}),
                 "hash": canonical_hash(_LegacyMethodSkill.forward)})
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        with pytest.raises(OwnerConflict, match="target_measurement_native_identity_integrity_invalid"):
            for _step in range(10):
                assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        run = runtime.owners.agent_runtime.query_bundle_stage_run(current["stage_run_request"]["request_ref"])
        assert runtime.owners.research_graph.query_target_graph_rejection(run.execution.submission_ref) is None
    finally:
        runtime.close()


def test_explicit_method_reuses_baseline_variant_and_evaluation_across_run_bindings(tmp_path):
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=_StableMethodSkill())
    try:
        _current, graph = _accept_initial_graph(runtime)
        authorities = [runtime.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref) for target in graph.targets]
        assert len(authorities) == 2
        assert authorities[0].identities == authorities[1].identities
        page = runtime.owners.research_graph.query_baselines(query="partition-integrity")
        assert len(page["items"]) == 1
        baseline = runtime.owners.research_graph.query_baseline(authorities[0].identities.baseline_ref)
        assert baseline["method_key"] == "audit/partition-integrity"
        assert baseline["method_version"] == "1"
        assert "run_bindings" not in baseline["method_contract"]
        assert "notes" not in baseline["method_contract"]
        variants = runtime.owners.research_graph.query_baseline_variants(baseline["baseline_ref"])
        assert len(variants["items"]) == 1
        selected = runtime.owners.research_graph.query_baseline_variants(
            baseline["baseline_ref"], variant_ref=authorities[0].identities.variant_ref)
        assert selected["items"][0]["evaluations"][0]["evaluation_ref"] == authorities[0].identities.evaluation_ref
        gateway = SemanticMcpGateway(_baseline_operations(runtime.owners.research_graph, runtime.owners.agent_runtime))
        run = runtime.owners.agent_runtime.query_bundle_stage_run(graph.request_ref)
        channel, _binding = gateway.issue_channel(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=canonical_hash(run.runtime_binding.as_dict()),
            operation_ids=gateway.operation_ids, root_kind="bundle", phase="execute")
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "research_graph.baselines.read", "arguments": {
                "baseline_ref": baseline["baseline_ref"], "variant_ref": authorities[0].identities.variant_ref}}}
        assert gateway.dispatch(None, message)[0] == 401
        status, response = gateway.dispatch(channel.token, message)
        assert status == 200 and not response["result"].get("isError"), response
        assert response["result"]["structuredContent"]["result"]["reusable_entities"]["items"][0]["variant_ref"] == authorities[0].identities.variant_ref
        gateway.revoke_channel(channel.token)
        assert gateway.dispatch(channel.token, message)[0] == 401
    finally:
        runtime.close()


def test_same_named_version_cannot_silently_change_method_semantics(tmp_path):
    skill = _StableMethodSkill()
    skill.second_method_update = {"method_contract": {"inputs": "raw events", "outputs": "causal estimate"}}
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=skill)
    try:
        _request, _run, rejection = _advance_to_rg_rejection(runtime)
        assert rejection.reason_code == "baseline_method_version_content_conflict"
        assert runtime.owners.research_graph.query_baselines()["items"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize("update", [{"method_version": "2"}, {"method_key": "audit/independent-partition-method"}])
def test_explicit_new_version_or_distinct_method_is_never_semantically_auto_merged(tmp_path, update):
    skill = _StableMethodSkill()
    skill.second_method_update = update
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=skill)
    try:
        _current, graph = _accept_initial_graph(runtime)
        refs = [runtime.owners.research_graph.query_target_measurement_domain_authority(
            target.target_ref).identities.baseline_ref for target in graph.targets]
        assert refs[0] != refs[1]
        first = runtime.owners.research_graph.query_baseline(refs[0])
        page = runtime.owners.research_graph.query_baselines(method_contract_hash=first["method_contract_hash"], limit=1)
        assert page["automatic_merge"] is False
        assert page["selection_required"] is True
        assert page["next_offset"] == 1
        next_page = runtime.owners.research_graph.query_baselines(method_contract_hash=first["method_contract_hash"], limit=1, offset=1)
        assert next_page["next_offset"] is None
        assert {page["items"][0]["baseline_ref"], next_page["items"][0]["baseline_ref"]} == set(refs)
    finally:
        runtime.close()


def test_selected_baseline_keeps_immutable_legacy_bytes_and_ignores_new_run_notes(tmp_path):
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=_LegacyMethodSkill())
    try:
        _confirm_direct_quest(runtime)
        # Restore a pre-protocol Baseline row, as a migrated database contains it.
        # Registration below must reuse these exact bytes, never mint a new row.
        legacy_ref = "baseline_historical_audit"
        with runtime._database.write() as connection:
            quest_ref = connection.execute(text("SELECT quest_ref FROM rg_quests")).scalar_one()
            connection.execute(text(
                "INSERT INTO rg_experiment_baselines (baseline_ref, quest_ref, forward_contract_json, "
                "forward_contract_hash, accepted_at) VALUES (:ref, :quest, :document, :hash, 1.0)"),
                {"ref": legacy_ref, "quest": quest_ref,
                 "document": canonical_json(_LegacyMethodSkill.forward),
                 "hash": canonical_hash(_LegacyMethodSkill.forward)})
            connection.execute(text("UPDATE research_graph_state SET "
                "experiment_baseline_count = experiment_baseline_count + 1 WHERE singleton = 'owner'"))
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(10):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["target_graph"]["status"] == "accepted":
                graph = runtime.owners.research_graph.query_target_graph(
                    current["stage_run_request"]["request_ref"])
                break
        else:
            raise AssertionError("Bundle did not accept its historical method reference")
        owner = runtime.owners.research_graph
        original = owner.query_target_measurement_domain_authority(graph.targets[0].target_ref)
        baseline = owner.query_baseline(original.identities.baseline_ref)
        assert baseline["baseline_ref"] == legacy_ref
        assert baseline["identity_kind"] == "legacy_exact_contract"
        assert len(owner.query_baselines()["items"]) == 1
        from meta_research.baseline_identity import resolve_baseline_method_identity
        with runtime._database.write() as connection:
            row = connection.execute(text("SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref"),
                {"ref": baseline["baseline_ref"]}).one()
            selected, created = resolve_baseline_method_identity(connection,
                forward={"baseline_ref": row.baseline_ref, "notes": "A later interpretation",
                    "run_bindings": {"upstream_commit_ref": "different-accepted-commit"}},
                quest_ref=row.quest_ref, accepted_at=1.0)
        assert selected == baseline["baseline_ref"]
        assert created is False
        assert owner.query_baseline(selected) == baseline
        assert owner.query_target_measurement_domain_authority(graph.targets[0].target_ref) == original
        # A changed historical contract is a new write and needs an explicit identity.
        with runtime._database.write() as connection:
            with pytest.raises(OwnerConflict, match="baseline_method_identity_required"):
                resolve_baseline_method_identity(connection,
                    forward={**_LegacyMethodSkill.forward, "upstream_commit_ref": "new-upstream"},
                    quest_ref=row.quest_ref, accepted_at=2.0)
        assert len(owner.query_baselines()["items"]) == 1
    finally:
        runtime.close()
