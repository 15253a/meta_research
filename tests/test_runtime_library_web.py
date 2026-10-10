"""Quest library settings use the authenticated versioned configuration boundary."""

import pytest

from meta_research.runtime_conditions import render_runtime_conditions
from test_public_acquisition_session import RecordingAcquisitionProvider
from test_public_autonomous_creation import _AutonomousReasoningSkill, _reach_autonomous_checkpoint
from test_public_plan_stage import _DeterministicDraftingAdapter, _DeterministicProbe
from test_public_reasoning_stage import _reasoning_runtime
from test_public_manual_question_lifecycle import (
    _accept_root_question, _authenticated_client, _build_runtime, _seed_value,
)


def test_library_configuration_round_trip_drives_new_deepfetch_without_prompt_leak(tmp_path):
    provider = RecordingAcquisitionProvider()
    runtime = _build_runtime(tmp_path / "library", acquisition_provider=provider)
    try:
        quest_ref, question_ref = _accept_root_question(runtime, "library")
        client, headers = _authenticated_client(runtime)
        url = f"/api/v1/quests/{quest_ref}/runtime-conditions"
        initial = client.get(url).json()
        assert initial["literature_configuration"] == {"mode": "oa_only", "library_entry_url": "", "institution_required": False}
        config = {"mode": "oa_then_institution", "library_entry_url": "https://library.example.org/resource", "institution_required": True}
        payload = {"text": initial["text"], "expected_revision": initial["revision"], "literature_configuration": config}
        saved = client.put(url, headers=headers, json=payload)
        assert saved.status_code == 200, saved.text
        assert client.get(url).json() == saved.json()
        assert saved.json()["literature_configuration"] == config
        assert "library.example.org" not in render_runtime_conditions(runtime.data_root.root, quest_ref=quest_ref)
        assert client.put(url, headers=headers, json=payload).status_code == 409
        write = lambda key: {**headers, "Idempotency-Key": key}
        opened = client.post("/api/v1/manual-question-creations", headers=write("library-manual"), json={"quest_ref": quest_ref, "parent_question_ref": question_ref}).json()
        context_ref = opened["context_ref"]
        seeded = client.post(f"/api/v1/manual-question-creations/{context_ref}/seed-confirmation", headers=write("library-seed"), json={"seed": _seed_value(deepfetch_preference="use")}).json()
        request = {"expected_seed_ref": seeded["seed"]["ref"], "expected_seed_hash": seeded["seed"]["hash"]}
        started = client.post(f"/api/v1/manual-question-creations/{context_ref}/deepfetch", headers=write("library-start"), json=request)
        assert started.status_code == 202, started.text
        assert provider.preflights[-1].library_entry_url == config["library_entry_url"]
        assert provider.preflights[-1].mode == "oa_then_institution"
        changed = client.put(url, headers=headers, json={**payload, "expected_revision": saved.json()["revision"], "literature_configuration": {**config, "library_entry_url": "https://other-library.example.org/"}})
        assert changed.status_code == 200, changed.text
        count = len(provider.preflights)
        replay = client.post(f"/api/v1/manual-question-creations/{context_ref}/deepfetch", headers=write("library-start"), json=request)
        assert replay.status_code == 202, replay.text
        assert replay.json()["research_path"]["deepfetch"]["request_ref"] == started.json()["research_path"]["deepfetch"]["request_ref"]
        assert len(provider.preflights) == count
        repeat = client.post(f"/api/v1/manual-question-creations/{context_ref}/deepfetch", headers=write("library-start-again"), json=request)
        assert repeat.status_code == 202, repeat.text
        assert repeat.json()["research_path"]["deepfetch"]["request_ref"] == started.json()["research_path"]["deepfetch"]["request_ref"]
        assert len(provider.preflights) == count
    finally:
        runtime.close()


@pytest.mark.parametrize(("direct_quest", "library_url", "pending_manual"), [
    (False, "https://library.example.org/autonomous", False),
    (True, "", False),
    (False, "https://library.example.org/autonomous", True),
])
def test_saved_library_configuration_drives_new_autonomous_deepfetch_and_freezes_issued_request(tmp_path, direct_quest, library_url, pending_manual):
    provider = RecordingAcquisitionProvider()
    runtime = _reasoning_runtime(
        tmp_path / "autonomous-library",
        reasoning_skill=_AutonomousReasoningSkill(require_source_literature=not direct_quest),
        acquisition_provider=provider,
        host_compute_probe=_DeterministicProbe() if direct_quest else None,
        proposal_drafter=_DeterministicDraftingAdapter(),
    )
    try:
        quest, _reasoning, checkpoint = _reach_autonomous_checkpoint(runtime, direct_quest=direct_quest)
        quest_ref = quest["quest_ref"]
        client, headers = _authenticated_client(runtime)
        url = f"/api/v1/quests/{quest_ref}/runtime-conditions"
        initial = client.get(url).json()
        if pending_manual:
            write = lambda key: {**headers, "Idempotency-Key": key}
            opened = client.post("/api/v1/manual-question-creations", headers=write("autonomous-library-manual"), json={"quest_ref": quest_ref, "parent_question_ref": checkpoint["scientific_outcome"]["question_ref"]})
            assert opened.status_code == 201, opened.text
            manual_ref = opened.json()["context_ref"]
            seeded = client.post(f"/api/v1/manual-question-creations/{manual_ref}/seed-confirmation", headers=write("autonomous-library-seed"), json={"seed": _seed_value(deepfetch_preference="use")}).json()
            manual = client.post(f"/api/v1/manual-question-creations/{manual_ref}/deepfetch", headers=write("autonomous-library-manual-start"), json={"expected_seed_ref": seeded["seed"]["ref"], "expected_seed_hash": seeded["seed"]["hash"]})
            assert manual.status_code == 202, manual.text
            frozen_manual_session = runtime.owners.agent_runtime.query_acquisition_session(quest_ref=quest_ref)
        config = {"mode": "oa_then_institution", "library_entry_url": library_url, "institution_required": True}
        payload = {"text": initial["text"], "expected_revision": initial["revision"], "literature_configuration": config}
        saved = client.put(url, headers=headers, json=payload)
        assert saved.status_code == 200, saved.text
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=checkpoint["checkpoint_ref"],
            source_scientific_outcome_ref=checkpoint["scientific_outcome"]["outcome_ref"],
            idempotency_key="autonomous-library-start",
        )
        if pending_manual:
            assert not runtime.autonomous_creation.process_once()
            assert runtime.owners.agent_runtime.query_acquisition_session(quest_ref=quest_ref) == frozen_manual_session
            assert runtime.deepfetch.process_once()
            assert client.get(f"/api/v1/manual-question-creations/{manual_ref}").json()["research_path"]["deepfetch"]["status"] == "succeeded"
        for _step in range(3):
            assert runtime.autonomous_creation.process_once()
            queued = runtime.autonomous_creation.query_context(started["context_ref"])
            if queued["deepfetch"]["status"] == "queued":
                break
        else:
            raise AssertionError("Autonomous DeepFetch was not queued")
        assert provider.preflights[-1].library_entry_url == library_url
        assert provider.preflights[-1].mode == "oa_then_institution"
        request = runtime.owners.advancement_engine.query_autonomous_deepfetch_request(started["context_ref"])
        session = runtime.owners.agent_runtime.query_acquisition_session(quest_ref=quest_ref)
        assert request.acquisition_config_hash == session.config_hash
        changed = client.put(url, headers=headers, json={**payload, "expected_revision": saved.json()["revision"], "literature_configuration": {"mode": "oa_only", "library_entry_url": "https://later-library.example.org/", "institution_required": False}})
        assert changed.status_code == 200, changed.text
        count = len(provider.preflights)
        assert not runtime.autonomous_creation.process_once()
        assert runtime.owners.advancement_engine.query_autonomous_deepfetch_request(started["context_ref"]) == request
        assert runtime.owners.agent_runtime.query_acquisition_session(quest_ref=quest_ref) == session
        assert len(provider.preflights) == count
        assert runtime.deepfetch.process_once()
        assert runtime.autonomous_creation.query_context(started["context_ref"])["deepfetch"]["status"] == "succeeded"
        assert runtime.owners.advancement_engine.query_autonomous_deepfetch_request(started["context_ref"]) == request
    finally:
        runtime.close()
