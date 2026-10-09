import os
import time

import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from test_creation_companion_protected import ProtectedCreationFixture
from test_creation_basis_lifecycle import _retire
from test_public_first_question_deepfetch import (
    DeterministicProbe, DeterministicDeepFetchProvider, RecordingAcquisitionProvider,
    _authenticate, _deepfetch_draft, _write_headers,
)
from test_public_quest_initialization import _confirmation_payload
from test_public_work_materials import _source
from test_root_workspace import _context, _channel, _call, _accepted


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires actual protected creation runtime")


class ReferenceReadingDeepFetch(DeterministicDeepFetchProvider):
    def execute(self, request):
        context = _context(request, "deepfetch")
        channel = _channel(self.runtime, context)
        exact = request.scope["creation_basis"]
        prior = _accepted(_call(self.runtime, channel, "research_memory.creation_basis.read", basis_ref=exact["basis_ref"],
            expected_basis_hash=exact["basis_hash"], view="understanding", limit=16384))
        assert "Original16" in str(prior)
        assert "Original16" in str(request.scope["existing_work_retrieval"])
        self.input_basis = exact
        self.query_basis = request.scope["existing_work_retrieval"]
        return super().execute(request)


def reference_runtime(path, *, selection="both", original_custody="managed", power_inhibitor=None):
    adapter = ProtectedCreationFixture(path / "adapter", selection=selection, original_custody=original_custody)
    provider = ReferenceReadingDeepFetch()
    runtime = build_production_runtime(prepare_data_root(path / "data"), proposal_drafter=adapter,
        intent_drafting_provider=adapter, host_compute_probe=DeterministicProbe(),
        deepfetch_provider=provider, acquisition_provider=RecordingAcquisitionProvider(), power_inhibitor=power_inhibitor)
    provider.runtime = runtime
    runtime.root_workspaces.configure_creation_runtime(executable="/bin/bash", credentials_home=path / "credentials")
    return runtime, adapter


def register(client, headers, runtime, view, source, key):
    endpoint = f"/api/v1/quest-initializations/{view['initialization_id']}"
    response = client.post(endpoint + "/material-references", headers=_write_headers(headers, key), json={
        "receiver": runtime.owners.human_collaboration.material_receiver("creation", view["initialization_id"]),
        "selections": [_source(client, source)], "description": "Read this small note on demand."})
    assert response.status_code == 201, response.text
    return response.json()


def reference_ready(runtime, client, headers, source, *, route="direct", key="ready"):
    response = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, key + "-open"), json={})
    assert response.status_code == 201, response.text
    opened = response.json()
    endpoint = f"/api/v1/quest-initializations/{opened['initialization_id']}"
    register(client, headers, runtime, opened, source, key + "-register")
    probe = client.post(endpoint + "/compute-probe", headers=_write_headers(headers, key + "-compute"),
        json={"selected_device_uuids": ["GPU-deepfetch-1"]})
    assert probe.status_code == 200, probe.text
    view = probe.json()
    draft = _deepfetch_draft(view)
    draft["route"] = route
    saved = client.put(endpoint + "/draft", headers=_write_headers(headers, key + "-draft"), json={
        "expected_draft_revision": view["quest_draft"]["revision"], "expected_draft_hash": view["quest_draft"]["hash"], "draft": draft})
    assert saved.status_code == 200, saved.text
    return generate(runtime, client, headers, saved.json(), key=key, route=route)


def generate(runtime, client, headers, view, *, key, route="direct"):
    endpoint = f"/api/v1/quest-initializations/{view['initialization_id']}"
    expected = {"expected_draft_revision": view["quest_draft"]["revision"], "expected_draft_hash": view["quest_draft"]["hash"]}
    if route == "deepfetch":
        acquired = client.post(endpoint + "/acquisition-session", headers=_write_headers(headers, key + "-acquire"), json=expected)
        assert acquired.status_code == 200, acquired.text
    queued = client.post(endpoint + "/proposal-generations", headers=_write_headers(headers, key + "-generate"), json=expected)
    assert queued.status_code == 202, queued.text
    if route == "deepfetch":
        assert runtime.deepfetch.process_once()
    assert runtime.owners.human_collaboration.process_drafting_once()
    ready = client.get(endpoint).json()
    assert ready["proposal_generation"]["status"] == "succeeded", ready["proposal_generation"]
    assert ready["creation_basis"]["freshness"] == "current"
    return ready


def save_payload(view, title):
    return {"expected_draft_revision": view["quest_draft"]["revision"], "expected_draft_hash": view["quest_draft"]["hash"],
        "expected_proposal_ref": view["proposal"]["ref"], "expected_proposal_hash": view["proposal"]["hash"],
        "content": {**view["proposal"]["content"], "title": title}}


def complete(runtime, client, endpoint):
    for _ in range(20):
        runtime.owners.human_collaboration.reconcile_once()
        view = client.get(endpoint).json()
        if view["status"] == "completed":
            return view
    raise AssertionError(view.get("recovery"))


@pytest.mark.parametrize("selection", ["original", "result", "both", "neither"])
def test_public_reference_selection_edit_confirm_and_successor_readback(tmp_path, selection):
    source = tmp_path / "original"
    source.mkdir()
    original = b"Original16 applies only at room temperature.\n"
    (source / "note.txt").write_bytes(original)
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(4 * 1024**3)
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection=selection)
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        selected = [item for item in ready["creation_basis"]["sources"] if item["binding"] is not None]
        assert len(selected) == {"original": 1, "result": 1, "both": 2, "neither": 0}[selection]
        assert all(item["intake_state"] == "accepted" for item in selected)
        if selected:
            blocked = _retire(client, headers, runtime, selected[0]["binding"], "pending-retire")
            assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "asset_retirement_blocked"
        edited = client.put(endpoint + "/proposal", headers=_write_headers(headers, "edit"), json=save_payload(ready, "Human selected calibration"))
        assert edited.status_code == 200, edited.text
        ready = edited.json()
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.text
        completed = complete(runtime, client, endpoint)
        basis = runtime.owners.research_memory.creation_bases.for_question(completed["question_ref"], completed["quest_ref"])
        assert basis["basis_ref"] == ready["creation_basis"]["basis_ref"]
        for item in selected:
            page = client.get(f"/api/v1/research-assets/{item['binding']['version_ref']}/content")
            assert page.status_code == 200
            assert page.content == (original if item["source"]["kind"] == "original_file" else b"trial 25\n")
            assert _retire(client, headers, runtime, item["binding"], "after-" + item["material_key"]).status_code == 409
        assert (source / "note.txt").read_bytes() == original and (source / "large.bin").stat().st_blocks == 0
        assert len(adapter.calls) == 2
    finally:
        client.close()
        runtime.close()


def test_material_only_append_rejects_old_save_preview_confirm_and_regenerates(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Original16 applies in room conditions.")
    extra = tmp_path / "extra.txt"
    extra.write_text("Additional input28 must be considered.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="original")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        old_hash = ready["quest_draft"]["hash"]
        old_confirmation = _confirmation_payload(ready)
        register(client, headers, runtime, ready, extra, "append-only")
        current = client.get(endpoint).json()
        assert current["quest_draft"]["hash"] == old_hash
        assert current["creation_basis"]["freshness"] == "stale"
        stale_save = client.put(endpoint + "/proposal", headers=_write_headers(headers, "stale-save"), json=save_payload(ready, "Old basis edited"))
        assert stale_save.status_code == 409 and stale_save.json()["detail"]["code"] == "creation_input_stale"
        preview = {key: value for key, value in old_confirmation.items() if key not in {"preview_ref", "preview_hash"}}
        assert client.post(endpoint + "/confirmation-preview", headers=_write_headers(headers, "stale-preview"), json=preview).status_code == 409
        assert client.post(endpoint + "/confirmation", headers=_write_headers(headers, "stale-confirm"), json=old_confirmation).status_code == 409
        refreshed = generate(runtime, client, headers, current, key="refresh")
        assert refreshed["creation_basis"]["applicability"]["predecessor"]["basis_hash"] == ready["creation_basis"]["basis_hash"]
        assert len(refreshed["creation_basis"]["input_identity"]["consumed"]) == 1
        assert refreshed["creation_basis"]["input_identity"]["consumed"][0]["reference_ref"] != ready["creation_basis"]["input_identity"]["consumed"][0]["reference_ref"]
        original_version = next(item["binding"]["version_ref"] for item in ready["creation_basis"]["sources"] if item["binding"])
        assert original_version in {item["binding"]["version_ref"] for item in refreshed["creation_basis"]["sources"] if item["binding"]}
        assert refreshed["creation_basis"]["input_identity_hash"] != ready["creation_basis"]["input_identity_hash"]
        assert len(refreshed["creation_basis"]["material_references"]["references"]) == 2
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "fresh-confirm"), json=_confirmation_payload(refreshed))
        assert confirmed.status_code == 202, confirmed.text
        assert complete(runtime, client, endpoint)["status"] == "completed"
    finally:
        client.close()
        runtime.close()


def test_goal_change_reuses_exact_selected_version_and_snapshot_without_new_deepfetch(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Original16 needs a condition-specific comparison.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="original")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source, route="deepfetch")
        old_basis = ready["creation_basis"]
        old_snapshot = old_basis["literature_snapshot"]
        old_confirmation = _confirmation_payload(ready)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        draft = {**ready["quest_draft"]["value"], "goal": "Compare calibration in the cold condition.", "route": "direct"}
        edited = client.put(endpoint + "/draft", headers=_write_headers(headers, "goal-edit"), json={
            "expected_draft_revision": ready["quest_draft"]["revision"], "expected_draft_hash": ready["quest_draft"]["hash"], "draft": draft})
        assert edited.status_code == 200, edited.text
        assert client.put(endpoint + "/proposal", headers=_write_headers(headers, "old-goal-save"), json=save_payload(ready, "Old goal")).status_code == 409
        assert client.post(endpoint + "/confirmation", headers=_write_headers(headers, "old-goal-confirm"), json=old_confirmation).status_code == 409
        refreshed = generate(runtime, client, headers, edited.json(), key="goal-refresh")
        basis = refreshed["creation_basis"]
        assert basis["input_identity"]["consumed"] == []
        assert basis["literature_snapshot"] is None
        assert basis["inherited_literature"][0]["snapshot"] == old_snapshot
        assert basis["inherited_literature"][0]["original_basis"] == old_basis["predecessor"]
        memory = runtime.owners.research_memory.creation_bases
        assert memory.query(basis["basis_ref"])["understanding"] == memory.query(old_basis["basis_ref"])["understanding"]
        assert basis["applicability"]["decisions"][0]["disposition"] == "retain"
        assert len(runtime.deepfetch._provider.requests) == 1
        selected = next(item for item in basis["sources"] if item["binding"] is not None)
        assert selected["binding"] == next(item["binding"] for item in old_basis["sources"] if item["binding"] is not None)
        assert _retire(client, headers, runtime, selected["binding"], "inherited-pending-retire").status_code == 409
        assert client.get(f"/api/v1/research-assets/{selected['binding']['version_ref']}/content").content == source.read_bytes()
        assert client.put(endpoint + "/proposal", headers=_write_headers(headers, "current-goal-save"), json=save_payload(refreshed, "Cold comparison")).status_code == 200
        current = client.get(endpoint).json()
        accepted = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "current-goal-confirm"), json=_confirmation_payload(current))
        assert accepted.status_code == 202, accepted.text
        assert complete(runtime, client, endpoint)["status"] == "completed"
    finally:
        client.close()
        runtime.close()


def test_material_append_during_actual_protected_call_rejects_late_understanding(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    source = tmp_path / "note.txt"
    source.write_text("Original16 applies in room conditions.")
    extra = tmp_path / "extra.txt"
    extra.write_text("An additional condition must be read.")
    late = tmp_path / "late.txt"
    late.write_text("This fact arrived while the provider was active.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="original")
    client, headers = _authenticate(runtime)
    entered, release = Event(), Event()
    invoke = adapter._invoke
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        register(client, headers, runtime, ready, extra, "before-active")
        def paused(**arguments):
            result = invoke(**arguments)
            entered.set()
            assert release.wait(20)
            return result
        adapter._invoke = paused
        queued = client.post(endpoint + "/proposal-generations", headers=_write_headers(headers, "active-generate"), json={
            "expected_draft_revision": ready["quest_draft"]["revision"], "expected_draft_hash": ready["quest_draft"]["hash"]})
        assert queued.status_code == 202, queued.text
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(runtime.owners.human_collaboration.process_drafting_once)
            try:
                assert entered.wait(20)
                register(client, headers, runtime, ready, late, "during-active")
            finally:
                release.set()
            assert pending.result(timeout=30)
        rejected = client.get(endpoint).json()
        assert rejected["proposal_generation"]["status"] == "failed", rejected["proposal_generation"]
        assert rejected["creation_basis"]["basis_ref"] == ready["creation_basis"]["basis_ref"]
        assert rejected["proposal"]["hash"] == ready["proposal"]["hash"]
        assert rejected["creation_basis"]["freshness"] == "stale"
        adapter._invoke = invoke
        fresh = generate(runtime, client, headers, rejected, key="after-active")
        assert len(fresh["creation_basis"]["material_references"]["references"]) == 3
        assert source.read_text() == "Original16 applies in room conditions."
    finally:
        release.set()
        client.close()
        runtime.close()


def test_reference_deepfetch_consumes_scoped_literature_before_confirmation(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Original16 needs a condition-specific comparison.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="original")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source, route="deepfetch")
        assert ready["creation_basis"]["kind"] == "literature_revised"
        assert ready["creation_basis"]["predecessor"]
        assert ready["creation_basis"]["corrections"][0]["disposition"] == "qualified"
        assert "counterproof" in ready["creation_basis"]["understanding"]["claims_and_conditions"][0]["text"]
        assert adapter.calls[-1][1].read_literature
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.text
        assert complete(runtime, client, endpoint)["status"] == "completed"
    finally:
        client.close()
        runtime.close()


def test_linked_original_drift_marks_public_basis_stale(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Original16 before linked drift.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="original", original_custody="linked_local")
    client, headers = _authenticate(runtime)
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        selected = next(item for item in ready["creation_basis"]["sources"] if item["binding"] is not None)
        assert selected["custody"] == "linked_local"
        source.write_text("Changed29 after linked drift.")
        current = client.get(endpoint)
        assert current.status_code == 200, current.text
        assert current.json()["creation_basis"]["freshness"] == "stale"
        assert current.json()["proposal"]["status"] == "stale"
        content = client.get(f"/api/v1/research-assets/{selected['binding']['version_ref']}/content")
        assert content.status_code == 409
        assert client.post(endpoint + "/confirmation", headers=_write_headers(headers, "drift-confirm"), json=_confirmation_payload(ready)).status_code == 409
        refreshed = generate(runtime, client, headers, current.json(), key="drift-refresh")
        assert refreshed["creation_basis"]["basis_hash"] != ready["creation_basis"]["basis_hash"]
    finally:
        client.close()
        runtime.close()


def test_new_reference_consumer_custody_failure_readback_and_recovery(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Original16 preserved for the successor.")
    runtime, adapter = reference_runtime(tmp_path / "runtime", selection="both")
    client, headers = _authenticate(runtime)
    unavailable = tmp_path / "unavailable-objects"
    objects = runtime.data_root.objects
    try:
        ready = reference_ready(runtime, client, headers, source)
        endpoint = f"/api/v1/quest-initializations/{ready['initialization_id']}"
        confirmed = client.post(endpoint + "/confirmation", headers=_write_headers(headers, "confirm"), json=_confirmation_payload(ready))
        assert confirmed.status_code == 202, confirmed.text
        objects.rename(unavailable)
        partial = None
        for _ in range(10):
            runtime.owners.human_collaboration.reconcile_once()
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            partial = response.json()
            if partial["recovery"] and partial["recovery"]["first_missing_step"] == "quest_source_material":
                break
        assert partial["status"] == "partial"
        assert partial["receipts"]["quest_source_material"]["reason"]["code"] == "asset_custody_unavailable"
        assert partial["receipts"]["question_content"]["status"] == "not_attempted"
        unavailable.rename(objects)
        time.sleep(max(0, partial["recovery"]["next_retry_at"] - time.time()) + 0.01)
        completed = complete(runtime, client, endpoint)
        assert completed["creation_basis"]["basis_hash"] == ready["creation_basis"]["basis_hash"]
        selected = [item for item in completed["creation_basis"]["sources"] if item["binding"] is not None]
        assert len(selected) == 2
        for item in selected:
            assert client.get(f"/api/v1/research-assets/{item['binding']['version_ref']}/content").status_code == 200
    finally:
        if unavailable.exists():
            unavailable.rename(objects)
        client.close()
        runtime.close()
