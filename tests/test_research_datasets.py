from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.semantic_mcp import ROOT_AGENT_DATASET_OPERATION_IDS, SemanticMcpGateway
from meta_research.semantic_owner_gateway import _dataset_operations
from test_public_research_asset_roles import _runtime


@pytest.fixture
def runtime(tmp_path):
    result = _runtime(tmp_path / "datasets")
    yield result
    result.close()


def _quest(runtime, prefix):
    human = runtime.owners.human_collaboration
    created = human.create_quest({
        "goal": f"{prefix}: compare observational research", "completion_criteria": "interpretable observations",
        "key_configuration": "field notes, no model training", "literature_scope": "open_access",
        "initial_question_direction": "Which observation changes the interpretation?", "material_receipts": [],
    }, prefix + "-create")
    initialization = created["initialization_id"]
    human.generate_question_proposal(initialization, created["quest_draft"]["hash"], prefix + "-proposal")
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(initialization)
    args = dict(quest_draft_revision=proposed["quest_draft"]["revision"],
                quest_draft_hash=proposed["quest_draft"]["hash"],
                proposal_ref=proposed["proposal"]["ref"], proposal_hash=proposed["proposal"]["hash"])
    preview = human.preview_confirmation(initialization, **args, idempotency_key=prefix + "-preview")
    human.confirm_quest(initialization, **args,
        preview_ref=preview["confirmation_preview"]["ref"], preview_hash=preview["confirmation_preview"]["hash"],
        idempotency_key=prefix + "-confirm")
    for _step in range(8):
        if human.query_quest_creation(initialization)["status"] == "completed":
            break
        assert human.reconcile_once()
    return runtime.owners.research_graph.query_question(initialization)


def _asset(runtime, *, content=b"Observer notes: the pattern was absent.", key="notes", asset_ref=None):
    result = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="text", custody_mode="managed", display_name="field-notes.txt",
        content=content, media_type="text/plain", asset_ref=asset_ref), idempotency_key=key)
    assert result.asset is not None
    return result.asset.as_binding()


def _dataset(graph, key="observations"):
    return graph.register_dataset(semantic_key="study:" + key, name="Qualitative field observations",
        meaning="Notes collected by observers; absence is a reported observation, not a model score.",
        metadata={"method": {"setting": "field", "coding": ["absence", "uncertainty"]}},
        notes="Interpret in the observer's context.", idempotency_key=key)


def _version(graph, dataset, binding, *, label="collection-2026", key="version"):
    return graph.register_dataset_version(dataset_ref=dataset["dataset_ref"], version_label=label,
        meaning="First observed collection; qualitative interpretation remains open.",
        asset_bindings=[binding], notes="One observer did not finish the final visit.", idempotency_key=key)


def test_dataset_is_discoverable_only_after_same_quest_origin_and_reference(runtime):
    graph = runtime.owners.research_graph
    one, two = _quest(runtime, "one"), _quest(runtime, "two")
    dataset = _dataset(graph)
    binding = _asset(runtime)
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=one.quest_ref, idempotency_key="origin")
    version = _version(graph, dataset, binding)
    usage = graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
        question_ref=one.question_ref, purpose="Interpret collected observations in their original scope",
        notes="Same Quest research material", idempotency_key="usage-one")
    assert graph.query_datasets(question_ref=one.question_ref, quest_ref=one.quest_ref)["items"] == [usage]
    assert graph.query_datasets(query="Qualitative", quest_ref=one.quest_ref)["items"] == [dataset]
    assert graph.query_datasets(quest_ref=two.quest_ref)["items"] == []
    assert graph.query_dataset_version(version["dataset_version_ref"], quest_ref=two.quest_ref) is None
    inventory = runtime.owners.research_memory.query_asset_inventory()
    assert len([item for item in inventory if item.version_ref == binding.version_ref]) == 1
    assert runtime.owners.research_memory.materialize_asset(binding.version_ref).content.startswith(b"Observer notes")


def test_dataset_versions_pin_rm_content_and_reject_forged_receipt(runtime):
    graph = runtime.owners.research_graph
    dataset = _dataset(graph)
    first = _asset(runtime)
    v1 = _version(graph, dataset, first)
    second = _asset(runtime, content=b"Second collection differs.", key="second", asset_ref=first.asset_ref)
    v2 = _version(graph, dataset, second, label="collection-2027", key="v2")
    assert v1["dataset_ref"] == v2["dataset_ref"]
    assert graph.query_dataset_version(v1["dataset_version_ref"])["asset_bindings"][0]["version_ref"] == first.version_ref
    assert first.version_ref != second.version_ref
    with pytest.raises(OwnerConflict, match="dataset_semantic_identity_conflict"):
        _version(graph, dataset, second, key="conflict")
    with pytest.raises(OwnerConflict, match="asset_receipt"):
        _version(graph, dataset, replace(second, content_hash="0" * 64), label="forged", key="forged")
    assert graph.query_datasets(dataset_ref=dataset["dataset_ref"])["total"] == 2


@pytest.mark.parametrize("field", ["content_hash", "manifest_hash", "receipt_hash", "receipt_subject", "receipt_issuer"])
def test_dataset_version_rejects_inexact_rm_binding(runtime, field):
    graph = runtime.owners.research_graph
    dataset, binding = _dataset(graph), _asset(runtime)
    if field in {"content_hash", "manifest_hash"}:
        binding = replace(binding, **{field: "0" * 64})
    else:
        key, value = {"receipt_hash": ("payload_hash", "0" * 64),
                      "receipt_subject": ("subject_ref", "asset_version_other"),
                      "receipt_issuer": ("issuer", "agent_runtime")}[field]
        binding = replace(binding, receipt=replace(binding.receipt, **{key: value}))
    with pytest.raises(OwnerConflict, match="asset_receipt"):
        _version(graph, dataset, binding)
    assert graph.query_datasets(dataset_ref=dataset["dataset_ref"])["total"] == 0


def test_dataset_version_idempotency_and_reconcile_survive_linked_source_offline(runtime, tmp_path):
    graph = runtime.owners.research_graph
    dataset = _dataset(graph)
    source = tmp_path / "observations.txt"
    source.write_text("A recorded qualitative observation", encoding="utf-8")
    intake = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
        source_kind="local_path", custody_mode="linked_local", display_name=source.name,
        source_locator=str(source), media_type="text/plain"), idempotency_key="linked")
    binding = intake.asset.as_binding()
    version = _version(graph, dataset, binding)
    source.unlink()
    assert _version(graph, dataset, binding) == version
    assert _version(graph, dataset, binding, key="semantic-replay") == version
    assert graph.reconcile_dataset_operation(operation="register_version", idempotency_key="version") == version
    with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
        _version(graph, dataset, binding, label="unavailable-new", key="unavailable-new")


def test_dataset_idempotency_semantic_replay_and_reconcile(runtime):
    graph = runtime.owners.research_graph
    dataset = _dataset(graph)
    assert _dataset(graph) == dataset
    args = {key: dataset[key] for key in ("semantic_key", "name", "meaning", "metadata", "notes")}
    assert graph.register_dataset(**args, idempotency_key="different-effect") == dataset
    assert graph.reconcile_dataset_operation(operation="register", idempotency_key="different-effect") == dataset
    assert graph.reconcile_dataset_operation(operation="register", idempotency_key="missing") is None
    with pytest.raises(OwnerConflict, match="dataset_idempotency_conflict"):
        graph.register_dataset(**{**args, "meaning": "changed"}, idempotency_key="observations")
    with pytest.raises(OwnerConflict, match="dataset_semantic_identity_conflict"):
        graph.register_dataset(**{**args, "meaning": "changed"}, idempotency_key="new-meaning")
    with pytest.raises(OwnerConflict, match="dataset_idempotency_conflict"):
        graph.reconcile_dataset_operation(operation="reference", idempotency_key="observations")


def test_dataset_references_require_existing_question_and_existing_research_object(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "reference")
    version = _version(graph, _dataset(graph), _asset(runtime))
    with pytest.raises(OwnerConflict, match="dataset_question_not_found"):
        graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
            question_ref="question_missing", idempotency_key="missing-question")
    with pytest.raises(OwnerConflict, match="dataset_research_ref_not_found"):
        graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
            question_ref=question.question_ref, research_ref="variant_missing", idempotency_key="missing-variant")
    # An existing Baseline row is enough for this reference: its hierarchy and
    # execution history remain owned by the existing experiment subsystem.
    with graph._database.write() as connection:
        connection.execute(text("INSERT INTO rg_experiment_baselines (baseline_ref, quest_ref, forward_contract_json, forward_contract_hash, accepted_at) VALUES (:ref, :quest, '{}', :hash, 1.0)"),
            {"ref": "baseline_fixture", "quest": question.quest_ref, "hash": "c" * 64})
    usage = graph.reference_dataset(dataset_version_ref=version["dataset_version_ref"],
        question_ref=question.question_ref, research_ref="baseline_fixture", purpose="Shared observational source",
        idempotency_key="baseline-reference")
    assert usage["research_ref"] == "baseline_fixture"
    assert graph.reconcile_dataset_operation(operation="reference", idempotency_key="baseline-reference") == usage


@pytest.mark.parametrize("metadata", [{"bad": float("nan")}, {"bad": ("tuple",)}, {3: "non-string-key"}])
def test_metadata_is_free_research_json_but_not_nonportable_values(runtime, metadata):
    with pytest.raises(OwnerConflict, match="dataset_metadata_invalid"):
        runtime.owners.research_graph.register_dataset(semantic_key="bad", name="bad", meaning="bad",
            metadata=metadata, idempotency_key="bad")


def test_dataset_receipt_and_asset_projection_tampering_are_detected(runtime):
    graph = runtime.owners.research_graph
    dataset = _dataset(graph)
    version = _version(graph, dataset, _asset(runtime))
    with graph._database.write() as connection:
        connection.execute(text("DELETE FROM rg_dataset_version_assets WHERE dataset_version_ref = :ref"),
                           {"ref": version["dataset_version_ref"]})
    with pytest.raises(OwnerConflict, match="dataset_asset_reference_invalid"):
        graph.query_dataset_version(version["dataset_version_ref"])
    with graph._database.write() as connection:
        connection.execute(text("UPDATE rg_datasets SET payload_json = '{}' WHERE dataset_ref = :ref"),
                           {"ref": dataset["dataset_ref"]})
    with pytest.raises(OwnerConflict, match="dataset_receipt_invalid"):
        graph.query_dataset(dataset["dataset_ref"])


def test_global_search_is_bounded_and_preserves_free_notes(runtime):
    graph = runtime.owners.research_graph
    for index in range(3):
        _dataset(graph, f"observations-{index}")
    first = graph.query_datasets(limit=2)
    second = graph.query_datasets(offset=first["next_offset"], limit=2)
    assert first["total"] == 3 and len(first["items"]) == 2
    assert len(second["items"]) == 1 and second["next_offset"] is None
    assert all(item["notes"] == "Interpret in the observer's context." for item in [*first["items"], *second["items"]])
    for args in ({"limit": 101}, {"offset": -1}, {"dataset_ref": "d", "question_ref": "q"}):
        with pytest.raises(OwnerConflict, match="dataset_page"):
            graph.query_datasets(**args)


def test_dataset_mcp_effect_read_reconcile_and_stale_scope(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "mcp")
    state = {"valid": True, "checks": 0}
    def scope(**kwargs):
        state["checks"] += 1
        assert kwargs["run_ref"] == "run"
        if not state["valid"]:
            raise OwnerConflict("root_agent_human_request_scope_stale")
        return {"quest_ref": question.quest_ref}
    agent = SimpleNamespace(verify_root_agent_runtime_scope=scope, verify_root_agent_human_request_reconcile_scope=scope)
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    channel, _binding = gateway.issue_channel(run_ref="run", attempt_ref="attempt", root_session_ref="root",
        fence_ref="fence", capability_binding_hash="a" * 64, operation_ids=ROOT_AGENT_DATASET_OPERATION_IDS,
        root_kind="idea", phase="execute")
    def call(operation, arguments):
        _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "research_graph.datasets." + operation, "arguments": arguments}})
        return response["result"]
    args = {"effect_id": "register", "semantic_key": "field:2026", "name": "Field observations",
            "meaning": "Human observations, no computed metric", "notes": "Preserve this handoff context"}
    first = call("register", args)
    assert not first.get("isError")
    accepted = first["structuredContent"]["result"]
    assert call("register.reconcile", {"effect_id": "register"})["structuredContent"]["result"] == accepted
    # Unreferenced registry identities are not global discovery results.
    assert call("page", {"query": "observations"})["structuredContent"]["total"] == 0
    binding = _asset(runtime)
    graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref, idempotency_key="mcp-origin")
    version = call("register_version", {"effect_id": "version", "dataset_ref": accepted["dataset_ref"],
        "version_label": "2026-observation", "meaning": "Collected by field observers",
        "asset_bindings": [binding.as_dict()], "notes": "Uncertain observations retained"})["structuredContent"]["result"]
    assert call("register_version.reconcile", {"effect_id": "version"})["structuredContent"]["result"] == version
    usage = call("reference", {"effect_id": "use", "dataset_version_ref": version["dataset_version_ref"],
        "question_ref": question.question_ref, "purpose": "Compare interpretations", "notes": "Do not treat absence as certainty"})["structuredContent"]["result"]
    assert call("read", {"dataset_ref": accepted["dataset_ref"]})["structuredContent"]["result"]["notes"] == args["notes"]
    assert call("read", {"dataset_version_ref": version["dataset_version_ref"]})["structuredContent"]["result"]["asset_bindings"] == [binding.as_dict()]
    assert call("reference.reconcile", {"effect_id": "use"})["structuredContent"]["result"] == usage
    assert call("page", {"question_ref": question.question_ref})["structuredContent"]["items"] == [usage]
    state["valid"] = False
    assert call("register", {**args, "effect_id": "stale"})["isError"]
    assert state["checks"] == 11


def test_dataset_mcp_rejects_cross_quest_reference(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "scope")
    version = _version(graph, _dataset(graph), _asset(runtime))
    agent = SimpleNamespace(verify_root_agent_runtime_scope=lambda **_kwargs: {"quest_ref": "unrelated"})
    gateway = SemanticMcpGateway(_dataset_operations(graph, agent))
    channel, _binding = gateway.issue_channel(run_ref="run", attempt_ref="attempt", root_session_ref="root",
        fence_ref="fence", capability_binding_hash="a" * 64, operation_ids=ROOT_AGENT_DATASET_OPERATION_IDS,
        root_kind="idea", phase="execute")
    _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "research_graph.datasets.reference", "arguments": {
            "effect_id": "wrong-quest", "question_ref": question.question_ref,
            "dataset_version_ref": version["dataset_version_ref"]}}})
    assert response["result"]["isError"]
    assert "dataset_question_scope_invalid" in str(response)
    assert graph.query_datasets(question_ref=question.question_ref)["total"] == 0

