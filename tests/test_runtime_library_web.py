"""Quest library settings use the authenticated versioned configuration boundary."""

from meta_research.runtime_conditions import render_runtime_conditions
from test_public_acquisition_session import RecordingAcquisitionProvider
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
