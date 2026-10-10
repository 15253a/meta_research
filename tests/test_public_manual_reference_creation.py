import os

import pytest

from test_public_manual_question_creation import build_runtime, accept_root_question
from test_public_reference_creation import reference_runtime
from test_public_first_question_deepfetch import _authenticate, _write_headers
from test_public_work_materials import _source
from test_creation_companion_protected import QUESTION
from test_creation_basis_lifecycle import _retire


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires actual protected creation runtime")


def manual_reference_runtime(path, **options):
    initial = build_runtime(path / "data", power_inhibitor=options.get("power_inhibitor"))
    try:
        anchor = accept_root_question(initial)
    finally:
        initial.close()
    runtime, adapter = reference_runtime(path, **options)
    return runtime, adapter, anchor


def manual_register(runtime, client, headers, view, source, key):
    response = client.post(f"/api/v1/manual-question-creations/{view['context_ref']}/material-references",
        headers=_write_headers(headers, key), json={
            "receiver": runtime.owners.human_collaboration.material_receiver("manual", view["context_ref"]),
            "selections": [_source(client, source)], "description": "Bounded original calibration note."})
    assert response.status_code == 201, response.text
    return response.json()


def manual_ready(runtime, client, headers, anchor, source, *, route="waiver"):
    _, quest, parent = anchor
    opened = client.post("/api/v1/manual-question-creations", headers=_write_headers(headers, "manual-open"),
        json={"quest_ref": quest, "parent_question_ref": parent})
    assert opened.status_code == 201, opened.text
    view = opened.json()
    endpoint = f"/api/v1/manual-question-creations/{view['context_ref']}"
    session = view["drafting_session"]["ref"]
    manual_register(runtime, client, headers, view, source, "manual-register")
    understood = client.post(endpoint + "/understanding", headers=_write_headers(headers, "manual-understanding"), json={})
    assert understood.status_code == 200, understood.text
    view = understood.json()
    first_basis = view["creation_basis"]["basis_ref"]
    assert view["seed"] is None and view["question_anchor"] is None
    seed = {"intent": "Continue the original calibration under a new condition.", "fields": QUESTION,
        "accepted_material_bindings": [], "deepfetch_preference": "use" if route == "deepfetch" else "skip"}
    confirmed = client.post(endpoint + "/seed-confirmation", headers=_write_headers(headers, "manual-seed"), json={"seed": seed})
    assert confirmed.status_code == 201, confirmed.text
    view = confirmed.json()
    assert view["drafting_session"]["ref"] == session and view["creation_basis"]["basis_ref"] == first_basis
    selected = client.post(endpoint + ("/deepfetch" if route == "deepfetch" else "/deepfetch-waiver"),
        headers=_write_headers(headers, "manual-route"), json={"expected_seed_ref": view["seed"]["ref"], "expected_seed_hash": view["seed"]["hash"]})
    assert selected.status_code in {201, 202}, selected.text
    if route == "deepfetch":
        assert runtime.deepfetch.process_once()
    view = client.get(endpoint).json()
    assert view["status"] == "research_ready", view
    message = client.post(endpoint + "/drafting-session/messages", headers=_write_headers(headers, "manual-message"), json={
        "expected_basis_hash": view["research_path"]["basis_hash"], "message": "Read the exact basis and suggest an editable comparison."})
    assert message.status_code == 200, message.text
    assert runtime.owners.human_collaboration.process_drafting_once()
    view = client.get(endpoint).json()
    assert view["drafting_session"]["turns"][-1]["assistant_status"] == "completed", view["drafting_session"]
    assert view["proposal"] is None
    return view


def manual_save_payload(view, *, title="Human edited manual calibration"):
    return {"expected_basis_hash": view["research_path"]["basis_hash"],
        "expected_input_identity_hash": view["creation_basis"]["input_identity_hash"],
        "expected_proposal_ref": None if view["proposal"] is None else view["proposal"]["ref"],
        "expected_proposal_hash": None if view["proposal"] is None else view["proposal"]["hash"],
        "content": {**QUESTION, "title": title}}


@pytest.mark.parametrize("route", ["waiver", "deepfetch"])
def test_manual_reference_seed_drafting_append_explicit_save_confirm_and_successor(tmp_path, route):
    source = tmp_path / "note.txt"
    source.write_text("Original16 is observed only under room conditions.")
    extra = tmp_path / "extra.txt"
    extra.write_text("Original16 has an additional replication limitation.")
    runtime, adapter, anchor = manual_reference_runtime(tmp_path / "runtime", selection="both")
    client, headers = _authenticate(runtime)
    try:
        ready = manual_ready(runtime, client, headers, anchor, source, route=route)
        endpoint = f"/api/v1/manual-question-creations/{ready['context_ref']}"
        seed = ready["seed"]
        session = ready["drafting_session"]["ref"]
        if route == "deepfetch":
            assert ready["creation_basis"]["kind"] == "literature_revised"
            assert ready["creation_basis"]["corrections"][0]["literature_sources"][0]["locator"] == "loc-1"
        selected = [item for item in ready["creation_basis"]["sources"] if item["binding"] is not None]
        assert len(selected) == 2
        assert _retire(client, headers, runtime, selected[0]["binding"], "manual-pending-retire").status_code == 409
        saved = client.put(endpoint + "/proposal", headers=_write_headers(headers, "manual-save"), json=manual_save_payload(ready))
        assert saved.status_code == 200, saved.text
        saved = saved.json()
        old_confirmation = {"proposal_ref": saved["proposal"]["ref"], "proposal_hash": saved["proposal"]["hash"]}
        manual_register(runtime, client, headers, saved, extra, "manual-append")
        stale = client.get(endpoint).json()
        assert stale["seed"] == seed and stale["drafting_session"]["ref"] == session
        assert stale["creation_basis"]["freshness"] == "stale" and stale["proposal"]["status"] == "stale"
        assert client.put(endpoint + "/proposal", headers=_write_headers(headers, "manual-old-save"), json=manual_save_payload(saved)).status_code == 409
        assert client.post(endpoint + "/proposal-confirmation", headers=_write_headers(headers, "manual-old-confirm"), json=old_confirmation).status_code == 409
        refreshed = client.post(endpoint + "/understanding", headers=_write_headers(headers, "manual-refresh"), json={})
        assert refreshed.status_code == 200, refreshed.text
        view = refreshed.json()
        assert view["creation_basis"]["applicability"]["predecessor"]["basis_hash"] == ready["creation_basis"]["basis_hash"]
        assert len(view["creation_basis"]["input_identity"]["consumed"]) == 1
        inherited_original = next(item["binding"] for item in ready["creation_basis"]["sources"] if item["source"]["kind"] == "original_file")
        assert inherited_original in [item["binding"] for item in view["creation_basis"]["sources"]]
        assert view["seed"] == seed and view["generation"] == ready["generation"]
        assert client.post(endpoint + "/proposal-confirmation", headers=_write_headers(headers, "manual-old-after-refresh"), json=old_confirmation).status_code == 409
        saved = client.put(endpoint + "/proposal", headers=_write_headers(headers, "manual-fresh-save"), json=manual_save_payload(view))
        assert saved.status_code == 200, saved.text
        view = saved.json()
        final_selected = [item for item in view["creation_basis"]["sources"] if item["binding"] is not None]
        for item in final_selected:
            version_ref = item["binding"]["version_ref"]
            pending_read = client.get("/api/v1/research-content", params={
                "quest_ref": anchor[1], "source_ref": version_ref, "version_ref": version_ref})
            assert pending_read.status_code == 409, pending_read.text
            assert pending_read.json()["detail"]["code"] == "asset_quest_scope_invalid"
        confirmed = client.post(endpoint + "/proposal-confirmation", headers=_write_headers(headers, "manual-confirm"), json={
            "proposal_ref": view["proposal"]["ref"], "proposal_hash": view["proposal"]["hash"]})
        assert confirmed.status_code == 202, confirmed.text
        for _ in range(12):
            runtime.owners.human_collaboration.reconcile_once()
            view = client.get(endpoint).json()
            if view["status"] == "completed":
                break
        assert view["status"] == "completed", view["recovery"]
        basis = runtime.owners.research_memory.creation_bases.for_question(view["question_anchor"]["question_ref"], anchor[1])
        for item in basis["sources"]:
            if item["binding"] is not None:
                version_ref = item["binding"]["version_ref"]
                successor_read = client.get("/api/v1/research-content", params={
                    "quest_ref": view["question_anchor"]["quest_ref"],
                    "source_ref": version_ref, "version_ref": version_ref})
                assert successor_read.status_code == 200, successor_read.text
        assert source.read_text() == "Original16 is observed only under room conditions."
        assert view["seed"] == seed
    finally:
        client.close()
        runtime.close()
