import time

import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from test_existing_project_creation import (
    ExistingWorkAdapter,
    PreparedBasisReadingDeepFetch,
    DeterministicProbe,
    RecordingAcquisitionProvider,
    _authenticate,
    _ready,
    _write_headers,
)
from test_public_quest_initialization import _confirmation_payload


def _retire(client, headers, runtime, binding, key):
    memory = runtime.owners.research_memory
    return client.post(
        f"/api/v1/research-assets/{binding['version_ref']}/retirement",
        headers=_write_headers(headers, key),
        json={
            "expected_revision": memory.query_asset_lifecycle(binding["asset_ref"])["revision"],
            "expected_reference_revision": runtime.owners.research_graph.query_asset_reference_revision(),
            "explanation": "The source has a verified error and no remaining research or explanatory value.",
            "low_value": True,
            "obsolete": True,
            "incorrect": True,
            "impact_understood": True,
            "has_explanation_value": False,
        },
    )


@pytest.mark.parametrize("route", ["direct", "deepfetch"])
def test_creation_basis_blocks_retirement_before_and_after_confirmation(tmp_path, route):
    provider = PreparedBasisReadingDeepFetch()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "data"),
        proposal_drafter=ExistingWorkAdapter(),
        host_compute_probe=DeterministicProbe(),
        deepfetch_provider=provider,
        acquisition_provider=RecordingAcquisitionProvider(),
    )
    provider.runtime = runtime
    client, headers = _authenticate(runtime)
    try:
        ready = _ready(runtime, client, headers, route)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        basis = ready["creation_basis"]
        source = next(item for item in basis["sources"] if item["relative_path"] == "project/notes.md")
        response = _retire(client, headers, runtime, source["binding"], "retire-pending-source")
        assert response.status_code == 409, response.json()
        detail = response.json()["detail"]
        assert detail["code"] == "asset_retirement_blocked"
        assert f"rm_creation_bases:{basis['basis_ref']}" in detail["details"]["active_reference_refs"]
        memory = runtime.owners.research_memory
        exact = memory.creation_bases.query(basis["basis_ref"], basis["basis_hash"])
        assert memory.creation_bases.read_source(exact, source["material_key"])["content"] == b"Room improves; cold worsens. More field work remains.\n"
        assert memory.query_current_asset(source["binding"]["asset_ref"]).version_ref == source["binding"]["version_ref"]
        preview_payload = {key: value for key, value in _confirmation_payload(ready).items() if key not in ("preview_ref", "preview_hash")}
        preview = client.post(endpoint + "/confirmation-preview", headers=_write_headers(headers, "fresh-preview"), json=preview_payload)
        assert preview.status_code == 201, preview.json()
        fresh = client.get(endpoint).json()
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(fresh))
        assert confirmed.status_code == 202, confirmed.json()
        for _ in range(20):
            runtime.owners.human_collaboration.reconcile_once()
            completed = client.get(endpoint).json()
            if completed["status"] == "completed":
                break
        assert completed["status"] == "completed", completed["recovery"]
        after = _retire(client, headers, runtime, source["binding"], "retire-confirmed-source")
        assert after.status_code == 409, after.json()
        assert after.json()["detail"]["code"] == "asset_retirement_blocked"
        successor_basis = memory.creation_bases.for_question(completed["question_ref"], completed["quest_ref"])
        assert successor_basis["basis_ref"] == basis["basis_ref"]
        assert successor_basis["basis_hash"] == basis["basis_hash"]
        assert memory.creation_bases.read_source(successor_basis, source["material_key"])["content"] == b"Room improves; cold worsens. More field work remains.\n"
    finally:
        client.close()
        runtime.close()


def test_basis_only_material_failure_is_readable_and_recovers_without_new_sources(tmp_path):
    data_root = prepare_data_root(tmp_path / "data")
    runtime = build_production_runtime(data_root, proposal_drafter=ExistingWorkAdapter(), host_compute_probe=DeterministicProbe())
    client, headers = _authenticate(runtime)
    unavailable = tmp_path / "unavailable-objects"
    try:
        ready = _ready(runtime, client, headers, "direct")
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        basis = ready["creation_basis"]
        assert ready["quest_draft"]["value"]["literature"]["accepted_material_bindings"] == []
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.json()
        data_root.objects.rename(unavailable)
        for _ in range(10):
            runtime.owners.human_collaboration.reconcile_once()
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            partial = response.json()
            if partial["recovery"] and partial["recovery"]["first_missing_step"] == "quest_source_material":
                break
        assert partial["status"] == "partial"
        assert partial["receipts"]["quest_source_material"]["reason"]["code"] == "asset_custody_unavailable"
        assert partial["recovery"]["first_missing_step"] == "quest_source_material"
        for layer in ("question_content", "question_identity", "cycle_activation"):
            assert partial["receipts"][layer]["status"] == "not_attempted"
        unavailable.rename(data_root.objects)
        time.sleep(max(0, partial["recovery"]["next_retry_at"] - time.time()) + 0.01)
        for _ in range(20):
            runtime.owners.human_collaboration.reconcile_once()
            completed = client.get(endpoint).json()
            if completed["status"] == "completed":
                break
        assert completed["status"] == "completed", completed["recovery"]
        assert completed["creation_basis"]["basis_hash"] == basis["basis_hash"]
        material_receipt = completed["receipts"]["quest_source_material"]
        assert material_receipt["status"] == "accepted"
        assert len(material_receipt["role_refs"]) == len(set(material_receipt["role_refs"])) == 2
        exact = runtime.owners.research_memory.creation_bases.for_question(completed["question_ref"])
        assert exact["basis_ref"] == basis["basis_ref"]
        source = next(item for item in exact["sources"] if item["relative_path"] == "project/notes.md")
        content = client.get(f"/api/v1/research-assets/{source['binding']['version_ref']}/content")
        assert content.status_code == 200
        assert content.content == b"Room improves; cold worsens. More field work remains.\n"
    finally:
        if unavailable.exists():
            unavailable.rename(data_root.objects)
        client.close()
        runtime.close()
