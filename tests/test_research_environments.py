"""Environment resources reuse exact RM content without adding an execution lifecycle."""
from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from meta_research.environment_operations import environment_operations
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import ROOT_AGENT_ENVIRONMENT_OPERATION_IDS, SemanticMcpGateway
from test_dataset_effect_scope_recovery import _scope
from test_research_datasets import _asset, _quest, _runtime
from test_dataset_research_ref_scope import accepted_research


@pytest.fixture
def runtime(tmp_path):
    result = _runtime(tmp_path / "environment-index")
    yield result
    result.close()


def _environment(graph, *, key="equipment", **values):
    arguments = dict(semantic_key="laboratory:eeg", name="Existing EEG equipment",
        meaning="Reusable recording equipment; availability and calibration must be checked for use.",
        source="Laboratory B, station EEG-7; reported by its operator",
        metadata={"capabilities": ["EEG acquisition"], "conditions": "Booking required"},
        notes="The description identifies the resource, not a frozen physical state.")
    return graph.register_environment(**{**arguments, **values}, idempotency_key=key)


def _channel(gateway, scope):
    channel, _ = gateway.issue_channel(run_ref=scope["run_ref"], attempt_ref=scope["attempt_ref"],
        root_session_ref=scope["root_session_ref"], fence_ref=scope["fence_ref"],
        capability_binding_hash=scope["runtime_binding_hash"], root_kind="companion", phase="primary",
        operation_ids=ROOT_AGENT_ENVIRONMENT_OPERATION_IDS)
    return channel


def _call(gateway, channel, operation, **arguments):
    _, response = gateway.dispatch(channel.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "research_graph.environments." + operation, "arguments": arguments}})
    return response["result"]


def _accepted(response):
    assert not response.get("isError"), response
    return response["structuredContent"]["result"]


def test_existing_equipment_directory_and_service_need_no_asset_or_build_target(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "existing-resources")
    assets_before = {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()}
    with runtime._database.read() as connection:
        targets_before = connection.execute(text("SELECT count(*) FROM rg_targets")).scalar_one()
    for key, source in (
        ("equipment", "Laboratory B, station EEG-7; reported by its operator"),
        ("installation", "/mnt/research/installed-simulator; existing installation on machine A"),
        ("service", "https://simulator.example.test; operator-provided resettable service"),
    ):
        resource = _environment(graph, key=key, semantic_key="existing:" + key,
            source=source, quest_ref=question.quest_ref)
        usage = graph.reference_environment(environment_ref=resource["environment_ref"],
            question_ref=question.question_ref, purpose="Available resource for a later study",
            idempotency_key="use-" + key)
        assert resource["asset_bindings"] == []
        assert resource["source"] == source
        assert graph.query_environment(resource["environment_ref"], quest_ref=question.quest_ref) == resource
        assert graph.query_environment_reference(usage["environment_reference_ref"]) == usage
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM rg_targets")).scalar_one() == targets_before
    assert {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()} == assets_before
    assert graph.query_environments(question_ref=question.question_ref)["total"] == 3


def test_environment_pages_preserve_metadata_and_do_not_discover_other_quests(runtime):
    graph = runtime.owners.research_graph
    one, two = _quest(runtime, "one"), _quest(runtime, "two")
    expected = {_environment(graph, key=f"one-{index}", semantic_key=f"one:{index}",
        quest_ref=one.quest_ref)["environment_ref"] for index in range(3)}
    foreign = _environment(graph, key="two", quest_ref=two.quest_ref)
    first = graph.query_environments(quest_ref=one.quest_ref, limit=2)
    second = graph.query_environments(quest_ref=one.quest_ref, offset=first["next_offset"], limit=2)
    items = first["items"] + second["items"]
    assert first["total"] == 3 and len(first["items"]) == 2
    assert len(second["items"]) == 1 and second["next_offset"] is None
    assert {item["environment_ref"] for item in items} == expected
    assert all(item["metadata"]["conditions"] == "Booking required" for item in items)
    assert graph.query_environment(foreign["environment_ref"], quest_ref=one.quest_ref) is None
    assert graph.query_environments(query="EEG-7", quest_ref=two.quest_ref)["items"] == [foreign]
    for arguments in ({"limit": 101}, {"offset": -1}, {"environment_ref": "e", "question_ref": "q"}):
        with pytest.raises(OwnerConflict, match="environment_page"):
            graph.query_environments(**arguments)


def test_new_snapshot_and_machine_adaptation_preserve_original_content_and_usage(runtime):
    graph = runtime.owners.research_graph
    question = _quest(runtime, "adaptation")
    assets_before = {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()}
    original_asset = _asset(runtime, content=b"Simulator configured for machine A.", key="original")
    graph.accept_asset_role(binding=original_asset, role="evidence", quest_ref=question.quest_ref,
        idempotency_key="original-origin")
    original = _environment(graph, key="original", semantic_key="simulator:traffic",
        source="/mnt/simulator on machine A", asset_bindings=[original_asset], quest_ref=question.quest_ref)
    usage = graph.reference_environment(environment_ref=original["environment_ref"],
        question_ref=question.question_ref, purpose="Original experiment on machine A", idempotency_key="original-use")
    adapted_asset = _asset(runtime, content=b"Simulator adapted for machine B.", key="adapted",
        asset_ref=original_asset.asset_ref)
    graph.accept_asset_role(binding=adapted_asset, role="evidence", quest_ref=question.quest_ref,
        idempotency_key="adapted-origin")
    adapted = _environment(graph, key="adapted", semantic_key=original["semantic_key"],
        source="/mnt/simulator on machine B", asset_bindings=[adapted_asset],
        source_environment_ref=original["environment_ref"], quest_ref=question.quest_ref)
    assert adapted["environment_ref"] != original["environment_ref"]
    assert adapted["source_environment_ref"] == original["environment_ref"]
    assert graph.query_environment(original["environment_ref"]) == original
    assert graph.query_environment_reference(usage["environment_reference_ref"]) == usage
    assert usage["environment_ref"] == original["environment_ref"]
    assert original["asset_bindings"][0]["version_ref"] == original_asset.version_ref
    assert adapted["asset_bindings"][0]["version_ref"] == adapted_asset.version_ref
    assert graph.query_environments(semantic_key=original["semantic_key"], quest_ref=question.quest_ref)["items"] == [original, adapted]
    assert runtime.owners.research_memory.materialize_asset(original_asset.version_ref).content == b"Simulator configured for machine A."
    assert {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()} == (
        assets_before | {original_asset.version_ref, adapted_asset.version_ref})
    with pytest.raises(OwnerConflict, match="environment_source_not_found"):
        _environment(graph, key="missing-source", source_environment_ref="environment_missing", quest_ref=question.quest_ref)


@pytest.mark.parametrize("field", ["content_hash", "manifest_hash", "receipt_hash", "receipt_subject", "receipt_issuer"])
def test_environment_rejects_forged_exact_content_binding(runtime, field):
    graph = runtime.owners.research_graph
    binding = _asset(runtime)
    if field in {"content_hash", "manifest_hash"}:
        binding = replace(binding, **{field: "0" * 64})
    else:
        key, value = {"receipt_hash": ("payload_hash", "0" * 64),
            "receipt_subject": ("subject_ref", "asset_version_other"),
            "receipt_issuer": ("issuer", "agent_runtime")}[field]
        binding = replace(binding, receipt=replace(binding.receipt, **{key: value}))
    with pytest.raises(OwnerConflict, match="asset_receipt"):
        _environment(graph, asset_bindings=[binding])
    assert graph.query_environments()["total"] == 0


def test_environment_binding_itself_retains_shared_rm_content(runtime):
    graph = runtime.owners.research_graph
    binding = _asset(runtime)
    environment = _environment(graph, asset_bindings=[binding])
    assert graph.query_environment_asset_references(binding.version_ref) == (
        "environment:" + environment["environment_ref"],)
    release = runtime.owners.research_memory.assess_release_eligibility(binding.version_ref,
        expected_reference_revision=graph.query_asset_reference_revision(), idempotency_key="release")
    assert not release.eligible
    assert "semantic_reference_active" in release.reason_codes
    assert "environment:" + environment["environment_ref"] in release.active_reference_refs


@pytest.mark.parametrize("action", ["register", "reference"])
def test_retired_real_root_cannot_commit_environment_effect(runtime, monkeypatch, action):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    question = _quest(runtime, "retired")
    scope = _scope(runtime, quest_ref=question.quest_ref)
    resource = _environment(graph, quest_ref=question.quest_ref)
    arguments = {
        "register": dict(semantic_key="late", name="Late resource", meaning="Reject retired writer", source="Lab B"),
        "reference": dict(environment_ref=resource["environment_ref"], question_ref=question.question_ref),
    }[action]
    method = {"register": "register_environment", "reference": "reference_environment"}[action]
    original = getattr(graph, method)

    def retire_before_owner_write(**kwargs):
        agent.complete_external_root_task_scope(root_kind="companion", root_runtime_scope=scope)
        assert agent.query_managed_run(scope["run_ref"])["status"] == "completed"
        return original(**kwargs)

    monkeypatch.setattr(graph, method, retire_before_owner_write)
    gateway = SemanticMcpGateway(environment_operations(graph, agent))
    with runtime._database.read() as connection:
        before = connection.execute(text("SELECT count(*) FROM rg_environment_commands")).scalar_one()
    response = _call(gateway, _channel(gateway, scope), action, effect_id="retired", **arguments)
    assert response.get("isError") and "scope_stale" in str(response), response
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM rg_environment_commands")).scalar_one() == before


def test_real_root_mcp_blocks_foreign_resources_content_and_question_writes(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    one, two = _quest(runtime, "scope-one"), _quest(runtime, "scope-two")
    own = _environment(graph, key="own", quest_ref=one.quest_ref)
    foreign_asset = _asset(runtime)
    graph.accept_asset_role(binding=foreign_asset, role="evidence", quest_ref=two.quest_ref, idempotency_key="foreign-origin")
    foreign = _environment(graph, key="foreign", quest_ref=two.quest_ref, asset_bindings=[foreign_asset])
    gateway = SemanticMcpGateway(environment_operations(graph, agent))
    channel = _channel(gateway, _scope(runtime, quest_ref=one.quest_ref))
    assert _accepted(_call(gateway, channel, "read", environment_ref=own["environment_ref"])) == own
    assert _call(gateway, channel, "read", environment_ref=foreign["environment_ref"])["structuredContent"]["status"] == "not_found"
    assert _call(gateway, channel, "page")["structuredContent"]["items"] == [own]
    cases = (
        ("reference", dict(environment_ref=foreign["environment_ref"], question_ref=one.question_ref), "asset_quest_scope_invalid"),
        ("reference", dict(environment_ref=own["environment_ref"], question_ref=two.question_ref), "environment_question_scope_invalid"),
        ("register", dict(source_environment_ref=foreign["environment_ref"]), "environment_source_not_found"),
        ("register", dict(asset_bindings=[foreign_asset.as_dict()]), "asset_quest_scope_invalid"),
    )
    for index, (action, arguments, code) in enumerate(cases):
        if action == "register":
            arguments = dict(semantic_key="forbidden", name="Foreign resource", meaning="Cannot import another Quest",
                source="foreign source", **arguments)
        response = _call(gateway, channel, action, effect_id=f"forbidden-{index}", **arguments)
        assert response.get("isError") and code in str(response), response
    assert graph.query_environments(question_ref=one.question_ref)["total"] == 0
    assert graph.query_environments(question_ref=two.question_ref)["total"] == 0
    assert graph.query_environments()["total"] == 2


def test_real_root_can_adopt_exact_equipment_from_another_quest_without_new_assets(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    origin, destination = _quest(runtime, "equipment-origin"), _quest(runtime, "equipment-adopter")
    equipment = _environment(graph, quest_ref=origin.quest_ref)
    assets_before = {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()}
    gateway = SemanticMcpGateway(environment_operations(graph, agent))
    channel = _channel(gateway, _scope(runtime, quest_ref=destination.quest_ref))

    assert _call(gateway, channel, "page")["structuredContent"]["items"] == []
    assert _call(gateway, channel, "read", environment_ref=equipment["environment_ref"])["structuredContent"]["status"] == "not_found"
    usage = _accepted(_call(gateway, channel, "reference", effect_id="adopt-equipment",
        environment_ref=equipment["environment_ref"], question_ref=destination.question_ref,
        purpose="Reuse the same recording station, subject to booking and calibration."))

    assert usage["environment_ref"] == equipment["environment_ref"]
    assert _accepted(_call(gateway, channel, "read", environment_ref=equipment["environment_ref"])) == equipment
    assert _call(gateway, channel, "page")["structuredContent"]["items"] == [equipment]
    assert graph.query_environment(equipment["environment_ref"], quest_ref=origin.quest_ref) == equipment
    assert graph.query_environments()["total"] == 1
    assert {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()} == assets_before


def test_cross_quest_digital_adoption_needs_every_binding_and_keeps_exact_shared_content(runtime):
    from meta_research.research_content import read_content

    graph, memory, agent = runtime.owners.research_graph, runtime.owners.research_memory, runtime.owners.agent_runtime
    origin, destination = _quest(runtime, "digital-origin"), _quest(runtime, "digital-adopter")
    bindings = [_asset(runtime, content=content, key=key) for key, content in
                (("simulator", b"Original simulator"), ("configuration", b"Original configuration"))]
    for index, binding in enumerate(bindings):
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=origin.quest_ref,
            idempotency_key=f"digital-origin-{index}")
    environment = _environment(graph, quest_ref=origin.quest_ref, asset_bindings=bindings)
    assets_before = {item.version_ref for item in memory.query_asset_inventory()}
    gateway = SemanticMcpGateway(environment_operations(graph, agent))
    channel = _channel(gateway, _scope(runtime, quest_ref=destination.quest_ref))
    payload = dict(effect_id="adopt-digital", environment_ref=environment["environment_ref"],
        question_ref=destination.question_ref, purpose="Use the accepted original simulator and configuration")
    graph.accept_asset_role(binding=bindings[0], role="evidence", quest_ref=destination.quest_ref,
        idempotency_key="adopt-first-binding")
    rejected = _call(gateway, channel, "reference", **payload)
    assert rejected.get("isError") and "asset_quest_scope_invalid" in str(rejected)
    assert graph.query_environments(question_ref=destination.question_ref)["total"] == 0
    with pytest.raises(OwnerConflict, match="asset_quest_scope_invalid"):
        graph.reference_environment(**{key: value for key, value in payload.items() if key != "effect_id"},
            idempotency_key="direct-incomplete-adoption")
    graph.accept_asset_role(binding=bindings[1], role="evidence", quest_ref=destination.quest_ref,
        idempotency_key="adopt-second-binding")
    with pytest.raises(OwnerConflict, match="content_source_unbound"):
        read_content(graph, memory, quest_ref=destination.quest_ref,
            source_ref=environment["environment_ref"], version_ref=bindings[0].version_ref)
    accepted = _call(gateway, channel, "reference", **payload)
    _accepted(accepted)
    assert _call(gateway, channel, "reference", **payload)["structuredContent"] == accepted["structuredContent"]
    assert _accepted(_call(gateway, channel, "read", environment_ref=environment["environment_ref"])) == environment
    for binding, expected in zip(bindings, ("Original simulator", "Original configuration")):
        page = read_content(graph, memory, quest_ref=destination.quest_ref,
            source_ref=environment["environment_ref"], version_ref=binding.version_ref)
        assert page["asset_binding"] == binding.as_dict()
        assert page["text"] == expected
    assert graph.query_environments()["items"] == [environment]
    assert {item.version_ref for item in memory.query_asset_inventory()} == assets_before


def test_damaged_reference_cannot_make_another_quests_resource_visible(runtime):
    graph = runtime.owners.research_graph
    origin, destination = _quest(runtime, "proof-origin"), _quest(runtime, "proof-adopter")
    environment = _environment(graph, quest_ref=origin.quest_ref)
    usage = graph.reference_environment(environment_ref=environment["environment_ref"],
        question_ref=destination.question_ref, idempotency_key="proof-adoption")
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE rg_environment_references SET receipt_hash=:hash "
            "WHERE environment_reference_ref=:ref"),
            {"hash": "0" * 64, "ref": usage["environment_reference_ref"]})
    with pytest.raises(OwnerConflict, match="environment_receipt_invalid"):
        graph.query_environment(environment["environment_ref"], quest_ref=destination.quest_ref)
    with pytest.raises(OwnerConflict, match="environment_receipt_invalid"):
        graph.query_environments(quest_ref=destination.quest_ref)
    assert graph.query_environment(environment["environment_ref"], quest_ref=origin.quest_ref) == environment


def test_recovered_real_root_reconciles_both_effect_families_without_duplicate_snapshots(runtime):
    graph, agent = runtime.owners.research_graph, runtime.owners.agent_runtime
    question = _quest(runtime, "recovery")
    gateway = SemanticMcpGateway(environment_operations(graph, agent))
    first_scope = _scope(runtime, quest_ref=question.quest_ref)
    old = _channel(gateway, first_scope)
    arguments = dict(effect_id="lost-response", semantic_key="recovery:eeg", name="EEG equipment",
        meaning="Existing equipment available by booking", source="Laboratory B, EEG-7")
    created = _call(gateway, old, "register", **arguments)
    resource = _accepted(created)
    reference_arguments = dict(effect_id="lost-response", environment_ref=resource["environment_ref"],
        question_ref=question.question_ref, purpose="Future observational study")
    used = _call(gateway, old, "reference", **reference_arguments)
    usage = _accepted(used)
    next_scope = _scope(runtime, quest_ref=question.quest_ref, generation=2)
    current = _channel(gateway, next_scope)
    assert _call(gateway, old, "register.reconcile", effect_id="lost-response").get("isError")
    for action, original, payload in (("register", created, arguments), ("reference", used, reference_arguments)):
        reconciled = _call(gateway, current, action + ".reconcile", effect_id="lost-response")
        assert reconciled["structuredContent"] == original["structuredContent"]
        assert _call(gateway, current, action, **payload)["structuredContent"] == original["structuredContent"]
    assert _accepted(_call(gateway, current, "read", environment_reference_ref=usage["environment_reference_ref"])) == usage
    assert _call(gateway, current, "page", question_ref=question.question_ref)["structuredContent"]["items"] == [usage]
    assert graph.query_environments(semantic_key="recovery:eeg")["total"] == 1
    changed = _call(gateway, current, "register", **{**arguments, "meaning": "A different request"})
    assert changed.get("isError") and "environment_idempotency_conflict" in str(changed)
    other_scope = _scope(runtime, quest_ref=question.quest_ref, run_ref="other-companion", root_ref="other-root")
    unrelated = _call(gateway, _channel(gateway, other_scope), "register.reconcile", effect_id="lost-response")
    assert unrelated["structuredContent"]["status"] == "not_found"


def test_accepted_target_environment_and_dataset_share_content_and_actual_run(accepted_research):
    from meta_research.research_content import read_content
    from test_research_datasets import _dataset, _version

    runtime, own, _, _, _, refs = accepted_research
    graph, memory = runtime.owners.research_graph, runtime.owners.research_memory
    facts = graph.query_target_formal_results(refs["target"])
    artifact = next(item for fact in facts for item in fact["run_artifacts"] if item["role"] == "data_asset")
    binding = memory.query_asset_version(artifact["version_ref"]).as_binding()
    with runtime._database.read() as connection:
        before = connection.execute(text("SELECT count(*) FROM rm_asset_versions")).scalar_one()
    version = _version(graph, _dataset(graph, "environment-fixtures"), binding, key="environment-fixtures-version")
    environment = _environment(graph, key="completed-environment", quest_ref=own.quest_ref,
        semantic_key="simulator:retained-fixtures", source=refs["target"],
        asset_bindings=[binding], meaning="Retained simulation fixtures from the actual completed work.")
    usage = graph.reference_environment(environment_ref=environment["environment_ref"],
        question_ref=own.question_ref, research_ref=refs["run"], purpose="Reuse the existing simulator fixtures",
        idempotency_key="completed-environment-usage")
    assert usage["research_ref"] == refs["run"]
    assert environment["asset_bindings"] == version["asset_bindings"] == [binding.as_dict()]
    page = read_content(graph, memory, quest_ref=own.quest_ref,
        source_ref=environment["environment_ref"], version_ref=binding.version_ref)
    assert page["asset_binding"] == binding.as_dict()
    assert page["text"] in {"36\n", "169\n"}
    assert graph.query_target_formal_results(refs["target"]) == facts
    with runtime._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM rm_asset_versions")).scalar_one() == before
