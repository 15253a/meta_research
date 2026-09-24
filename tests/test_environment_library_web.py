"""The sixth entry reads exact shared material and keeps physical resources useful."""

from fastapi.testclient import TestClient
from sqlalchemy import text

from meta_research.web import create_app
from test_research_datasets import _asset, _quest, _runtime


def _client(runtime):
    client = TestClient(create_app(runtime, base_url="http://testserver", control_key="isolated-test"))
    response = client.post("/auth/bootstrap", headers={"Origin": "http://testserver"},
                           json={"token": runtime.authentication.issue_bootstrap_token()})
    assert response.status_code == 200
    return client


def test_environment_entry_reads_multiple_exact_bindings_and_rejects_foreign_quests(tmp_path):
    runtime = _runtime(tmp_path / "environments")
    try:
        graph = runtime.owners.research_graph
        quest, foreign = _quest(runtime, "one"), _quest(runtime, "foreign")
        material = _asset(runtime, content=b"Original installed simulator configuration.", key="configuration")
        instructions = _asset(runtime, content=b"Use the persistent installation; adapt hardware in the next Target.", key="instructions")
        for index, binding in enumerate((material, instructions)):
            graph.accept_asset_role(binding=binding, role="evidence", quest_ref=quest.quest_ref,
                                    idempotency_key=f"origin-{index}")
        environment = graph.register_environment(
            semantic_key="simulator:installed", name="Installed field simulator",
            meaning="A reusable simulator installation with configuration and operating notes.",
            source="/mnt/research/simulator", metadata={"conditions": "Requires a GPU"},
            asset_bindings=[material, instructions], quest_ref=quest.quest_ref,
            idempotency_key="environment")
        # A newer RM version must not replace the configuration this entry names.
        newer = _asset(runtime, content=b"Different simulator configuration.", key="new-configuration",
                       asset_ref=material.asset_ref)
        graph.accept_asset_role(binding=newer, role="evidence", quest_ref=quest.quest_ref,
                                idempotency_key="new-origin")
        client = _client(runtime)
        page = client.get("/api/v1/research-library/environments",
                          params={"quest_ref": quest.quest_ref, "query": "simulator"})
        assert page.status_code == 200, page.text
        item, = page.json()["items"]
        assert item["environment_ref"] == environment["environment_ref"]
        assert item["source"] == "/mnt/research/simulator"
        assert item["summary"] == environment["meaning"]
        assert item["reader"] == item["readers"][0]
        assert {reader["version_ref"] for reader in item["readers"]} == {material.version_ref, instructions.version_ref}
        originals = {}
        for reader in item["readers"]:
            response = client.get("/api/v1/research-content", params={"quest_ref": quest.quest_ref, **reader})
            assert response.status_code == 200, response.text
            originals[reader["version_ref"]] = response.json()["text"]
            assert response.json()["source_ref"] == environment["environment_ref"]
            assert client.get("/api/v1/research-content", params={"quest_ref": foreign.quest_ref, **reader}).status_code == 409
        assert originals == {material.version_ref: "Original installed simulator configuration.",
                             instructions.version_ref: "Use the persistent installation; adapt hardware in the next Target."}
        assert client.get("/api/v1/research-content", params={"quest_ref": quest.quest_ref,
            "source_ref": environment["environment_ref"], "version_ref": newer.version_ref}).status_code == 409
        assert client.get("/api/v1/research-library/environments",
                          params={"quest_ref": foreign.quest_ref}).json()["items"] == []
    finally:
        runtime.close()


def test_physical_resources_are_discoverable_without_a_fake_content_version_or_target(tmp_path):
    runtime = _runtime(tmp_path / "physical-environments")
    try:
        graph = runtime.owners.research_graph
        quest = _quest(runtime, "equipment")
        equipment = graph.register_environment(
            semantic_key="equipment:lab-camera", name="Existing laboratory camera",
            meaning="Available for observational studies, subject to booking.",
            source="Building B, laboratory 204, equipment CAM-7",
            metadata={"capabilities": ["high speed video"], "conditions": "Book with the laboratory operator"},
            notes="Availability describes the known booking conditions; equipment state can change.",
            quest_ref=quest.quest_ref, idempotency_key="existing-camera")
        client = _client(runtime)
        response = client.get("/api/v1/research-library/environments",
                              params={"quest_ref": quest.quest_ref, "query": "camera"})
        assert response.status_code == 200, response.text
        item, = response.json()["items"]
        assert item["environment_ref"] == equipment["environment_ref"]
        assert item["asset_bindings"] == []
        assert item["readers"] == [] and "reader" not in item
        assert item["source"] == equipment["source"]
        assert item["metadata"]["conditions"] == "Book with the laboratory operator"
        assert "environment_version_ref" not in item
        assert graph.query_environments(environment_ref=equipment["environment_ref"], quest_ref=quest.quest_ref)["items"] == []
        # A resource identity itself is not a pretend digital content version.
        assert client.get("/api/v1/research-content", params={"quest_ref": quest.quest_ref,
            "source_ref": equipment["environment_ref"], "version_ref": equipment["environment_ref"]}).status_code == 409
    finally:
        runtime.close()


def test_environment_uses_expand_to_real_research_relationships_within_the_quest(tmp_path):
    runtime = _runtime(tmp_path / "environment-uses")
    try:
        graph = runtime.owners.research_graph
        quest, foreign = _quest(runtime, "study"), _quest(runtime, "other-study")
        environment = graph.register_environment(
            semantic_key="facility:observation-room", name="Observation room",
            meaning="An existing room for observation studies.", source="Building C, room 12",
            idempotency_key="room")
        # Existing research remains in the Baseline hierarchy; only its reference
        # belongs to the resource's adoption record.
        with graph._database.write() as connection:
            connection.execute(text("INSERT INTO rg_experiment_baselines "
                "(baseline_ref,quest_ref,forward_contract_json,forward_contract_hash,accepted_at) "
                "VALUES (:ref,:quest,'{}',:hash,1.0)"),
                {"ref": "baseline_observation", "quest": quest.quest_ref, "hash": "c" * 64})
        usage = graph.reference_environment(environment_ref=environment["environment_ref"],
            question_ref=quest.question_ref, research_ref="baseline_observation",
            purpose="Observe participants in the existing room.", notes="Follow the facility booking conditions.",
            idempotency_key="room-use")
        foreign_usage = graph.reference_environment(environment_ref=environment["environment_ref"],
            question_ref=foreign.question_ref, purpose="A separate study's observation.",
            idempotency_key="foreign-room-use")
        client = _client(runtime)
        params = {"quest_ref": quest.quest_ref, "environment_ref": environment["environment_ref"],
                  "query": "Observation room"}
        response = client.get("/api/v1/research-library/environments", params=params)
        assert response.status_code == 200, response.text
        item, = response.json()["items"]
        assert item["environment_reference_ref"] == usage["environment_reference_ref"]
        assert item["ref"] == usage["environment_reference_ref"]
        assert item["question_ref"] == quest.question_ref
        assert item["research_ref"] == "baseline_observation"
        assert item["summary"] == usage["purpose"]
        assert item["notes"] == usage["notes"]
        assert "reader" not in item and "asset_bindings" not in item
        other_page = client.get("/api/v1/research-library/environments",
            params={**params, "quest_ref": foreign.quest_ref}).json()
        assert [entry["ref"] for entry in other_page["items"]] == [foreign_usage["environment_reference_ref"]]
    finally:
        runtime.close()
