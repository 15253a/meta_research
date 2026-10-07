"""Accepted Target resources reach the existing stage MCP and reusable indexes."""
import pytest
from sqlalchemy import text

from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS, ROOT_AGENT_SEMANTIC_OPERATION_IDS,
    create_semantic_owner_gateway,
)
from test_dataset_effect_scope_recovery import _scope
from test_formal_run_snapshots import _scenario


def _declare_candidates(document):
    document["dataset_candidates"] = [{
        "artifact_path": "outputs/data/run1.txt", "name": "Squared observations",
        "purpose": "Reuse the actual retained observations."}]
    document["environment_candidates"] = [{
        "artifact_path": "outputs/data/run1.txt", "name": "Simulator fixture",
        "purpose": "Use the same observations as simulator fixtures."}]


def _bundle_channel(runtime):
    request = runtime.bundle_stage.query_current()["stage_run_request"]
    run = runtime.owners.agent_runtime.query_bundle_stage_run(request["request_ref"])
    assert run is not None
    return runtime.harnesses.issue_resident_mcp_channel(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref,
        root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
        capability_binding_hash=run.runtime_binding_hash,
        operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS, root_kind="bundle",
        phase="dispatch", subject_policy="operation_tree")


def _call(runtime, channel, operation, **arguments):
    status, response, _ = runtime.harnesses.dispatch_mcp_http(
        channel.connection.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": operation, "arguments": arguments}}, mcp_session_id=None)
    assert status == 200, response
    return response["result"]


def _accepted(response):
    assert not response.get("isError"), response
    return response["structuredContent"]


def _accept(runtime, lifecycle, memory, handle, evidence, finalizer):
    frozen = finalizer.finalize(handle=handle, evidence=evidence)
    manifest = memory.query(frozen.manifest_ref)
    accepted = runtime.owners.research_graph.accept_target_commit_from_root_completion(
        completion=lifecycle.query_completion(handle.target_ref), manifest=manifest,
        result_document=manifest.result_document, idempotency_key="stage-resource-handoff")
    return manifest, accepted


def test_bundle_reads_exact_accepted_resource_candidates_with_actual_producers(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        manifest, commit = _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        channel = _bundle_channel(runtime)
        facts = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read",
            target_ref=handle.target_ref))
        candidates = facts["resource_candidates"]
        assert candidates["status"] == "accepted"
        assert candidates["target_ref"] == handle.target_ref
        assert candidates["target_run_ref"] == handle.target_run_ref
        assert candidates["target_commit_ref"] == commit.target_commit_ref
        assert candidates["manifest_ref"] == manifest.manifest_ref
        assert candidates["manifest_receipt"] == manifest.receipt.as_public_dict()
        entry = next(entry for entry in manifest.entries
                     if entry.declared_relative_path == "outputs/data/run1.txt")
        first = next(item for item in facts["items"] if item["run_key"] == "first")
        role = next(item for item in first["run_artifacts"] if item["version_ref"] == entry.binding.version_ref)
        for kind, name, purpose in (("dataset_candidates", "Squared observations",
                                    "Reuse the actual retained observations."),
                                   ("environment_candidates", "Simulator fixture",
                                    "Use the same observations as simulator fixtures.")):
            candidate, = candidates[kind]
            assert candidate["artifact_path"] == "outputs/data/run1.txt"
            assert (candidate["name"], candidate["purpose"]) == (name, purpose)
            assert candidate["asset_binding"] == entry.binding.as_dict()
            producer, = candidate["producers"]
            assert (producer["subject_kind"], producer["subject_ref"], producer["role"], producer["role_ref"]) == (
                "variant_run", first["variant_run_ref"], "data_asset", role["role_ref"])
    finally:
        runtime.close()


def test_environment_snapshot_candidate_keeps_its_exact_implementing_run(tmp_path):
    def declare(document):
        document["environment_candidates"] = [{
            "artifact_path": "implementation/v1", "name": "Squared simulator",
            "purpose": "Reuse the exact first simulator implementation."}]
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, declare)
    try:
        manifest, _ = _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        facts = _accepted(_call(runtime, _bundle_channel(runtime),
            "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        candidate, = facts["resource_candidates"]["environment_candidates"]
        entry = next(entry for entry in manifest.entries if entry.declared_relative_path == "implementation/v1")
        first = next(item for item in facts["items"] if item["run_key"] == "first")
        assert candidate["asset_binding"] == entry.binding.as_dict()
        producer, = candidate["producers"]
        assert producer["subject_ref"] == first["variant_run_ref"]
        assert producer["subject_kind"] == "variant_run"
        assert producer["role"] == "implementation_snapshot"
        assert producer["input_binding_ref"] == first["variant_run"]["input_binding_ref"]
        assert producer["implementation_revision_ref"] == first["variant_run"]["implementation_revision_ref"]
    finally:
        runtime.close()


def test_implementation_candidate_ignores_nonexecuted_work_without_a_run_key(tmp_path):
    def declare(document):
        document["formal_runs"].append({"status": "cancelled"})
        document["environment_candidates"] = [{
            "artifact_path": "implementation/v1", "name": "Squared simulator",
            "purpose": "Reuse the implementation of actual executed work."}]
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, declare)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        facts = _accepted(_call(runtime, _bundle_channel(runtime),
            "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        assert len(facts["items"]) == 2
        candidate, = facts["resource_candidates"]["environment_candidates"]
        first = next(item for item in facts["items"] if item["run_key"] == "first")
        assert candidate["producers"][0]["subject_ref"] == first["variant_run_ref"]
    finally:
        runtime.close()


def test_bundle_registers_one_original_for_dataset_and_environment_and_successor_reads_it(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        channel = _bundle_channel(runtime)
        facts = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        candidate, = facts["resource_candidates"]["dataset_candidates"]
        environment_candidate, = facts["resource_candidates"]["environment_candidates"]
        graph = runtime.owners.research_graph.query_target_graph(
            runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"])
        question_ref = runtime.owners.advancement_engine.query_bundle_stage_request(
            graph.cycle_ref).accepted_question.question_ref
        dataset = _accepted(_call(runtime, channel, "research_graph.datasets.register",
            effect_id="retained-observations", semantic_key="squared:observations",
            name=candidate["name"], meaning=candidate["purpose"]))["result"]
        version = _accepted(_call(runtime, channel, "research_graph.datasets.register_version",
            effect_id="observation-version", dataset_ref=dataset["dataset_ref"],
            version_label="first-retained", meaning=candidate["purpose"],
            asset_bindings=[candidate["asset_binding"]]))["result"]
        reference = _accepted(_call(runtime, channel, "research_graph.datasets.reference",
            effect_id="observation-use", dataset_version_ref=version["dataset_version_ref"],
            question_ref=question_ref, research_ref=handle.target_ref, purpose=candidate["purpose"]))["result"]
        environment = _accepted(_call(runtime, channel, "research_graph.environments.register",
            effect_id="fixture-environment", semantic_key="squared:fixtures",
            name=environment_candidate["name"], meaning=environment_candidate["purpose"],
            source="Retained first simulator run in " + handle.target_ref,
            asset_bindings=[environment_candidate["asset_binding"]]))["result"]
        environment_reference = _accepted(_call(runtime, channel, "research_graph.environments.reference",
            effect_id="fixture-use", environment_ref=environment["environment_ref"],
            question_ref=question_ref, research_ref=candidate["producers"][0]["subject_ref"],
            purpose=environment_candidate["purpose"]))["result"]
        assert _accepted(_call(runtime, channel, "research_graph.datasets.register_version.reconcile",
            effect_id="observation-version"))["result"] == version
        assert _accepted(_call(runtime, channel, "research_graph.environments.register.reconcile",
            effect_id="fixture-environment"))["result"] == environment
        gateway = create_semantic_owner_gateway(research_graph=runtime.owners.research_graph,
            agent_runtime=runtime.owners.agent_runtime, advancement_engine=runtime.owners.advancement_engine,
            research_memory=runtime.owners.research_memory,
            human_collaboration=runtime.owners.human_collaboration,
            human_collaboration_snapshot=runtime.owners.human_collaboration.query_snapshot)
        scope = _scope(runtime, quest_ref=graph.quest_ref,
            run_ref="resource-successor", root_ref="resource-successor-root")
        successor, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
            root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
            capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS["companion"])

        def read(operation, **arguments):
            _, response = gateway.dispatch(successor.token, {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": operation, "arguments": arguments}})
            return _accepted(response["result"])

        assert read("research_graph.datasets.page", query="squared:observations")["items"][0]["dataset_ref"] == dataset["dataset_ref"]
        assert read("research_graph.datasets.read", dataset_version_ref=version["dataset_version_ref"])["result"]["asset_bindings"] == [candidate["asset_binding"]]
        assert read("research_graph.datasets.read", dataset_reference_ref=reference["dataset_reference_ref"])["result"] == reference
        assert read("research_graph.environments.page", query="squared:fixtures")["items"][0]["environment_ref"] == environment["environment_ref"]
        assert read("research_graph.environments.read", environment_reference_ref=environment_reference["environment_reference_ref"])["result"] == environment_reference
        for source_ref in (version["dataset_version_ref"], environment["environment_ref"]):
            content = read("research_memory.content.read", source_ref=source_ref,
                version_ref=candidate["asset_binding"]["version_ref"], offset=0, limit=1024)
            assert content["text"] == "36\n"
            assert content["asset_binding"] == candidate["asset_binding"]
        unchanged = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        assert unchanged["items"] == facts["items"]
        assert unchanged["resource_candidates"] == facts["resource_candidates"]
    finally:
        runtime.close()


def test_candidate_read_preserves_exact_content_and_current_corrected_producer(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        channel = _bundle_channel(runtime)
        before = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        candidate, = before["resource_candidates"]["dataset_candidates"]
        original, = candidate["producers"]
        second = next(item for item in before["items"] if item["run_key"] == "second")
        adjustment = _accepted(_call(runtime, channel, "research_graph.artifact_roles.adjust",
            role_ref=original["role_ref"], to_subject_kind="variant_run",
            to_subject_ref=second["variant_run_ref"], reason="Correct attribution after checking the work record.",
            effect_id="correct-resource-producer"))["result"]
        after = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        for kind in ("dataset_candidates", "environment_candidates"):
            corrected, = after["resource_candidates"][kind]
            producer, = corrected["producers"]
            assert corrected["asset_binding"] == candidate["asset_binding"]
            assert (producer["subject_kind"], producer["subject_ref"]) == ("variant_run", second["variant_run_ref"])
            assert (producer["accepted_subject_kind"], producer["accepted_subject_ref"]) == (
                original["subject_kind"], original["subject_ref"])
            assert producer["role_ref"] == original["role_ref"]
            assert producer["receipt"] == original["receipt"]
            assert producer["attribution_adjustments"] == [adjustment]
    finally:
        runtime.close()


@pytest.mark.parametrize("corruption", ["payload", "receipt", "chain", "endpoint"])
def test_candidate_read_rejects_unauthenticated_or_disconnected_attribution(corruption, tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        channel = _bundle_channel(runtime)
        facts = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        candidate, = facts["resource_candidates"]["dataset_candidates"]
        producer, = candidate["producers"]
        second = next(item for item in facts["items"] if item["run_key"] == "second")

        def correct(to_ref, effect_id):
            return _accepted(_call(runtime, channel, "research_graph.artifact_roles.adjust",
                role_ref=producer["role_ref"], to_subject_kind="variant_run", to_subject_ref=to_ref,
                reason="Correct the retained observation attribution.", effect_id=effect_id))["result"]

        first_adjustment = correct(second["variant_run_ref"], "integrity-correction-1")
        if corruption == "chain":
            second_adjustment = correct(producer["subject_ref"], "integrity-correction-2")
            third_adjustment = correct(second["variant_run_ref"], "integrity-correction-3")
        # Storage fault injection is setup at the SQLite system boundary. The
        # behavior assertion below observes only the authenticated public MCP.
        with runtime._database.write() as connection:
            if corruption in {"payload", "receipt"}:
                column = "payload_hash" if corruption == "payload" else "receipt_hash"
                connection.execute(text("UPDATE rg_experiment_asset_role_adjustments SET " + column +
                    "=:hash WHERE adjustment_ref=:ref"),
                    {"hash": "0" * 64, "ref": first_adjustment["adjustment_ref"]})
            elif corruption == "chain":
                connection.execute(text("UPDATE rg_experiment_asset_role_adjustments SET accepted_at=CASE "
                    "WHEN adjustment_ref=:second THEN :third_time ELSE :second_time END "
                    "WHERE adjustment_ref IN (:second,:third)"), {
                    "second": second_adjustment["adjustment_ref"], "third": third_adjustment["adjustment_ref"],
                    "second_time": second_adjustment["accepted_at"], "third_time": third_adjustment["accepted_at"]})
            else:
                connection.execute(text("UPDATE rg_experiment_asset_roles SET subject_ref=:subject "
                    "WHERE role_ref=:role"), {"subject": producer["subject_ref"], "role": producer["role_ref"]})
        denied = _call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref)
        assert denied.get("isError") and "target_resource_candidate_adjustment_invalid" in str(denied)
    finally:
        runtime.close()


def test_frozen_candidates_are_unavailable_until_target_commit_acceptance(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        frozen = finalizer.finalize(handle=handle, evidence=evidence)
        assert memory.query(frozen.manifest_ref) is not None
        facts = _accepted(_call(runtime, _bundle_channel(runtime),
            "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        assert facts["resource_candidates"] == {
            "status": "not_found", "target_ref": handle.target_ref, "target_run_ref": None,
            "target_commit_ref": None, "manifest_ref": None, "manifest_receipt": None,
            "dataset_candidates": [], "environment_candidates": []}
    finally:
        runtime.close()


def test_empty_accepted_candidates_create_no_resource_registrations(tmp_path):
    def declare(document):
        document["dataset_candidates"] = []
        document["environment_candidates"] = []
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, declare)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        channel = _bundle_channel(runtime)
        facts = _accepted(_call(runtime, channel, "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        assert facts["resource_candidates"]["status"] == "accepted"
        assert facts["resource_candidates"]["dataset_candidates"] == []
        assert facts["resource_candidates"]["environment_candidates"] == []
        assert _accepted(_call(runtime, channel, "research_graph.datasets.page"))["total"] == 0
        assert _accepted(_call(runtime, channel, "research_graph.environments.page"))["total"] == 0
    finally:
        runtime.close()


def test_run_only_resource_handoff_does_not_manufacture_an_assessment(tmp_path):
    def declare(document):
        _declare_candidates(document)
        document["metrics"] = {}
        for run in document["formal_runs"]:
            run["evaluations"] = []
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, declare)
    try:
        manifest, commit = _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        facts = _accepted(_call(runtime, _bundle_channel(runtime),
            "research_graph.target_formal_results.read", target_ref=handle.target_ref))
        assert len(facts["items"]) == 2
        assert all(item["evaluation_attempt"] is None and item["metric_result"] is None
                   for item in facts["items"])
        candidates = facts["resource_candidates"]
        assert candidates["target_commit_ref"] == commit.target_commit_ref
        assert candidates["manifest_ref"] == manifest.manifest_ref
        first = next(item for item in facts["items"] if item["run_key"] == "first")
        assert candidates["dataset_candidates"][0]["producers"][0]["subject_ref"] == first["variant_run_ref"]
    finally:
        runtime.close()


def test_resource_candidates_remain_quest_scoped_and_stale_root_cannot_read_them(tmp_path):
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path, _declare_candidates)
    try:
        _accept(runtime, lifecycle, memory, handle, evidence, finalizer)
        graph = runtime.owners.research_graph.query_target_graph(
            runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"])
        gateway = create_semantic_owner_gateway(research_graph=runtime.owners.research_graph,
            agent_runtime=runtime.owners.agent_runtime, advancement_engine=runtime.owners.advancement_engine,
            research_memory=runtime.owners.research_memory,
            human_collaboration=runtime.owners.human_collaboration,
            human_collaboration_snapshot=runtime.owners.human_collaboration.query_snapshot)

        def channel(scope):
            return gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
                root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
                capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
                operation_ids=("research_graph.target_formal_results.read",))[0]

        def read(connection):
            _, response = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "research_graph.target_formal_results.read",
                    "arguments": {"target_ref": handle.target_ref}}})
            return response["result"]

        foreign = channel(_scope(runtime, quest_ref="foreign-quest", run_ref="foreign-resource-reader",
            root_ref="foreign-resource-root"))
        denied = read(foreign)
        assert denied.get("isError") and "formal_result_quest_scope_invalid" in str(denied)
        own_scope = _scope(runtime, quest_ref=graph.quest_ref,
            run_ref="scope-resource-reader", root_ref="scope-resource-root")
        old = channel(own_scope)
        assert _accepted(read(old))["resource_candidates"]["status"] == "accepted"
        _scope(runtime, quest_ref=graph.quest_ref, run_ref="scope-resource-reader",
            root_ref="scope-resource-root", generation=2)
        stale = read(old)
        assert stale.get("isError") and "scope_stale" in str(stale)
    finally:
        runtime.close()
