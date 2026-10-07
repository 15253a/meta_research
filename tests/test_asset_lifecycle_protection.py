from __future__ import annotations

import io
import json
import zipfile

import pytest

from meta_research.owners.common import OwnerConflict, canonical_json
from meta_research.owners.research_memory import AssetIntakeRequest
from test_public_asset_lifecycle import intake, retire
from test_root_formal_entities import _accept
from test_target_root_finalizer import _root_finalizer_fixture


def test_two_actual_productions_keep_retirement_blocked_after_role_correction(tmp_path):
    (
        runtime,
        lifecycle,
        memory,
        _,
        handle,
        workspace,
        evidence,
    ) = _root_finalizer_fixture(tmp_path)
    try:
        path = workspace / "outputs/metrics.json"
        document = json.loads(path.read_text())
        document["formal_runs"] = [
            {
                "run_key": "first",
                "artifact_paths": ["logs/train.log"],
                "checkpoint_paths": [
                    artifact.relative_path
                    for artifact in evidence.handoff.artifacts
                    if artifact.role == "checkpoint"
                ],
                "evaluations": [
                    {"attempt_key": "assessment", "metrics": document["metrics"]}
                ],
            },
            {
                "run_key": "second",
                "artifact_paths": ["logs/train.log"],
                "checkpoint_paths": [],
                "evaluations": [],
            },
        ]
        path.write_text(canonical_json(document))
        _, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        graph, assets = runtime.owners.research_graph, runtime.owners.research_memory
        facts = {
            item["run_key"]: item
            for item in graph.query_target_formal_results(handle.target_ref)
        }
        first, second = [
            next(
                role
                for role in facts[key]["run_artifacts"]
                if role["role"] == "log_asset"
            )
            for key in ("first", "second")
        ]
        binding = next(
            entry.binding
            for entry in manifest.entries
            if entry.declared_relative_path == "logs/train.log"
        )
        asset = assets.query_asset_version(binding.version_ref)
        expected = {
            f"experiment-asset-role:{role['role_ref']}" for role in (first, second)
        }
        assert first["role_ref"] != second["role_ref"]
        with pytest.raises(OwnerConflict) as blocked:
            retire(assets, graph, asset, "two-production-retirement")
        assert expected <= set(blocked.value.details["active_reference_refs"])
        graph.adjust_experiment_artifact_role(
            role_ref=first["role_ref"],
            to_subject_kind="evaluation_attempt",
            to_subject_ref=facts["first"]["evaluation_attempt_ref"],
            reason="The report belongs to the assessment of this execution.",
            idempotency_key="correct-production-purpose",
        )
        with pytest.raises(OwnerConflict) as blocked:
            retire(assets, graph, asset, "corrected-production-retirement")
        assert expected <= set(blocked.value.details["active_reference_refs"])
        assert (
            assets.materialize_asset(binding.version_ref).content
            == b"epoch 1 complete\n"
        )
    finally:
        runtime.close()


def test_target_commit_and_manifest_keep_handoff_content_retained(tmp_path):
    runtime, lifecycle, memory, _, handle, _, evidence = _root_finalizer_fixture(
        tmp_path
    )
    try:
        accepted, manifest = _accept(runtime, lifecycle, memory, handle, evidence)
        binding = next(
            entry.binding
            for entry in manifest.entries
            if entry.role == "implementation"
        )
        graph, assets = runtime.owners.research_graph, runtime.owners.research_memory
        with pytest.raises(OwnerConflict) as blocked:
            retire(
                assets,
                graph,
                assets.query_asset_version(binding.version_ref),
                "target-content-retirement",
            )
        references = blocked.value.details["active_reference_refs"]
        assert f"target-commit:{accepted.target_commit_ref}" in references
        assert (
            f"rm_target_root_completion_manifests:{manifest.manifest_ref}" in references
        )
        assert "active_references" in blocked.value.details["reasons"]
        assert assets.materialize_asset(binding.version_ref).content
        assert memory.query(manifest.manifest_ref) == manifest
    finally:
        runtime.close()


def test_retirement_keeps_directory_entry_bytes_shared_with_another_asset(tmp_path):
    from meta_research.composition import build_production_runtime
    from meta_research.paths import prepare_data_root

    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    try:
        assets, graph = runtime.owners.research_memory, runtime.owners.research_graph
        literal = b"obsolete unit error\n"
        standalone = intake(assets, literal, "standalone")
        directory = tmp_path / "directory"
        directory.mkdir()
        (directory / "entry.txt").write_bytes(literal)
        result = assets.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="directory",
                custody_mode="managed",
                display_name="directory",
                source_locator=str(directory),
            ),
            idempotency_key="directory",
        )
        assert result.status == "accepted"
        before = assets.materialize_asset(result.asset.version_ref).content
        retire(assets, graph, standalone, "retire-shared-entry")
        after = assets.materialize_asset(result.asset.version_ref).content
        assert after == before
        with zipfile.ZipFile(io.BytesIO(after)) as archive:
            assert archive.read("entry.txt") == literal
        assert assets.materialize_asset(standalone.version_ref).content == literal
        assert (directory / "entry.txt").read_bytes() == literal
    finally:
        runtime.close()


def test_new_human_input_verifies_bytes_before_writer_and_retains_its_basis(
    tmp_path, monkeypatch
):
    from test_research_datasets import _runtime, _quest

    runtime = _runtime(tmp_path / "human-input")
    try:
        quest = _quest(runtime, "one")
        assets, graph = runtime.owners.research_memory, runtime.owners.research_graph
        basis = intake(
            assets,
            b"analyst measurement\n",
            "input-basis",
            origin_quest_ref=quest.quest_ref,
        )
        verify = assets.verify_asset_binding

        def verify_without_writer(**values):
            assert not runtime._database._write_lock._is_owned()
            return verify(**values)

        monkeypatch.setattr(assets, "verify_asset_binding", verify_without_writer)
        request = dict(
            quest_ref=quest.quest_ref,
            text_content="Review this exact measurement.",
            asset_bindings=[basis.as_binding().as_dict()],
            idempotency_key="human-input",
        )
        accepted = runtime.owners.human_collaboration.submit_research_input(**request)
        assert accepted["asset_bindings"][0]["version_ref"] == basis.version_ref
        assert (
            runtime.owners.human_collaboration.submit_research_input(**request)
            == accepted
        )
        with pytest.raises(OwnerConflict) as blocked:
            retire(assets, graph, basis, "retire-human-basis")
        assert (
            f"hc_research_inputs:{accepted['input_ref']}"
            in blocked.value.details["active_reference_refs"]
        )
        disposable = intake(
            assets,
            b"discarded analyst draft",
            "discarded-input",
            origin_quest_ref=quest.quest_ref,
        )
        retire(assets, graph, disposable, "discard-input")
        with pytest.raises(OwnerConflict, match="asset_version_retired"):
            runtime.owners.human_collaboration.submit_research_input(
                **{
                    **request,
                    "idempotency_key": "retired-input",
                    "asset_bindings": [disposable.as_binding().as_dict()],
                }
            )
        assert (
            assets.materialize_asset(disposable.version_ref).content
            == b"discarded analyst draft"
        )
    finally:
        runtime.close()


def test_pending_accepted_target_keeps_direct_input_before_proof_registration(tmp_path):
    from test_plan_asset_target_input import _runtime, _asset, _origin, _accepted_target
    import test_public_bundle_stage as fixtures

    runtime, plan, bundle = _runtime(tmp_path / "pending-target")
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        selected = _asset(runtime, "plan-source")
        _origin(runtime, selected, quest["quest_ref"], "plan-origin")
        assets, graph = runtime.owners.research_memory, runtime.owners.research_graph
        pending = intake(
            assets,
            b"exact pending Target input\n",
            "pending-input",
            origin_quest_ref=quest["quest_ref"],
        )
        plan.source_ref, bundle.source_ref = selected.version_ref, pending.version_ref
        target, _ = _accepted_target(runtime, quest)
        assert graph.query_asset_roles(version_refs=(pending.version_ref,)) == ()
        assert (
            runtime.target_run_authorities.research_memory.query_input_asset(
                target_ref=target.target_ref, asset_ref=pending.asset_ref
            )
            is None
        )
        with pytest.raises(OwnerConflict) as blocked:
            retire(assets, graph, pending, "retire-pending-target-input")
        assert (
            f"rg_targets:{target.target_ref}"
            in blocked.value.details["active_reference_refs"]
        )
        assert (
            assets.materialize_asset(pending.version_ref).content
            == b"exact pending Target input\n"
        )
    finally:
        runtime.close()


def test_human_response_exact_basis_blocks_retirement_and_retired_basis_rejects_response(
    tmp_path,
):
    from test_research_datasets import _runtime

    runtime = _runtime(tmp_path / "human-response")
    try:
        assets, graph = runtime.owners.research_memory, runtime.owners.research_graph

        def open_request(key):
            return graph.open_human_request(
                request_kind="offline_action",
                obligation="Confirm the observed unit.",
                business_purpose="Keep the expert's basis for this judgment.",
                target_assertion={"topic": key},
                acceptance_conditions=("Exact observed basis.",),
                direct_waiter={
                    "waiter_ref": key,
                    "generation": 1,
                    "target_assertion": {"topic": key},
                    "wait_scope": "local",
                    "other_blockers": [],
                },
                idempotency_key=key,
            )

        request = open_request("first-request")
        basis = intake(assets, b"expert measurement\n", "response-basis")
        response_request = dict(
            decision="provided",
            facts={"asset": basis.as_binding().as_dict()},
            note="This exact measurement supports the expert judgment.",
            idempotency_key="response",
        )
        accepted = runtime.owners.human_collaboration.respond_to_human_request(
            request["request_ref"], **response_request
        )
        assert (
            runtime.owners.human_collaboration.respond_to_human_request(
                request["request_ref"], **response_request
            )
            == accepted
        )
        with pytest.raises(OwnerConflict) as blocked:
            retire(assets, graph, basis, "retire-response-basis")
        assert (
            f"hc_human_request_responses:{accepted['response_ref']}"
            in blocked.value.details["active_reference_refs"]
        )
        disposable = intake(assets, b"discarded expert draft\n", "discarded-response")
        retire(assets, graph, disposable, "discard-response")
        other = open_request("second-request")
        with pytest.raises(OwnerConflict, match="asset_version_retired"):
            runtime.owners.human_collaboration.respond_to_human_request(
                other["request_ref"],
                **{
                    **response_request,
                    "facts": {"asset": disposable.as_binding().as_dict()},
                    "idempotency_key": "retired-response",
                },
            )
        assert graph.query_human_request(other["request_ref"])["responses"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize("timing", ["retired_before", "competing"])
def test_target_acceptance_and_retirement_cannot_both_accept_exact_pending_input(
    tmp_path, monkeypatch, timing
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from copy import copy
    from meta_research.database import Database
    from test_plan_asset_target_input import _runtime, _asset, _origin
    import test_public_bundle_stage as fixtures

    runtime, plan, bundle = _runtime(tmp_path / timing)
    second_database = Database(runtime._database._engine.url.database)
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        selected = _asset(runtime, "plan-source")
        _origin(runtime, selected, quest["quest_ref"], "plan-origin")
        assets, graph = runtime.owners.research_memory, runtime.owners.research_graph
        pending = intake(
            assets,
            b"pending exact input\n",
            "pending",
            origin_quest_ref=quest["quest_ref"],
        )
        plan.source_ref, bundle.source_ref = selected.version_ref, pending.version_ref
        fixtures._finish_idea_stage(runtime)
        fixtures._finish_plan_stage(runtime)
        other_assets = copy(assets)
        other_assets._database = second_database
        barrier = Barrier(2)
        accept = graph.accept_target_graph
        accepted = {}

        def accept_candidate(*args, **values):
            if timing == "competing":
                barrier.wait(timeout=15)
            try:
                result = accept(*args, **values)
            except OwnerConflict as error:
                accepted["outcome"] = error.code
                raise
            accepted["outcome"] = result
            return result

        monkeypatch.setattr(graph, "accept_target_graph", accept_candidate)

        def publish_target():
            for _ in range(16):
                try:
                    runtime.bundle_stage.process_once()
                except OwnerConflict as error:
                    assert accepted.get("outcome") == "asset_version_retired"
                    assert error.code == "asset_version_retired"
                    break
                if "outcome" in accepted:
                    break
            assert "outcome" in accepted

        def retire_pending():
            if timing == "competing":
                barrier.wait(timeout=15)
            try:
                return retire(other_assets, graph, pending, "competing-retirement")
            except OwnerConflict as error:
                return error.code

        if timing == "retired_before":
            retirement = retire_pending()
            publish_target()
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                target_future = pool.submit(publish_target)
                retirement_future = pool.submit(retire_pending)
                target_future.result(timeout=90)
                retirement = retirement_future.result(timeout=15)
        if isinstance(retirement, dict):
            assert accepted["outcome"] == "asset_version_retired"
            request = runtime.bundle_stage.query_current()["stage_run_request"]
            assert graph.query_target_graph(request["request_ref"]) is None
            assert assets.query_current_asset(pending.asset_ref) is None
        else:
            assert retirement == "asset_retirement_blocked"
            assert accepted["outcome"].targets
            assert (
                assets.query_current_asset(pending.asset_ref).version_ref
                == pending.version_ref
            )
        assert (
            assets.materialize_asset(pending.version_ref).content
            == b"pending exact input\n"
        )
    finally:
        second_database.close()
        runtime.close()
