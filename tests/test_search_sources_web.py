from __future__ import annotations

from pathlib import Path
import json

import pytest
from sqlalchemy import text

from meta_research.acquisition import AcquisitionPreflightResult, canonical_hash
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from test_public_acquisition_session import RecordingAcquisitionProvider, _authenticated_client, _write_headers
from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider,
    DeterministicProbe,
    SnapshotAwareProposalDrafter,
    _open_and_queue_deepfetch,
)


def test_shared_source_selection_is_independent_of_creation_drafts_and_other_scopes(tmp_path: Path) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"))
    client, headers = _authenticated_client(runtime)
    try:
        form = {"kind": "website", "name": "Paper pages", "url": "https://arxiv.org", "instructions": "Read pages."}
        saved = client.post("/api/v1/search-sources", headers=headers, json={"form": form})
        assert saved.status_code == 201
        source = saved.json()["source"]
        assert saved.json()["receipt"]["scope"] == "shared"
        opened = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, "open-1"), json={}).json()
        first_id = opened["initialization_id"]
        response = client.put(f"/api/v1/quest-initializations/{first_id}/search-sources", headers=headers,
            json={"allowed_source_ids": [source["source_id"]], "expected_revision": 0})
        assert response.status_code == 200
        assert response.json()["receipt"]["scope"]["kind"] == "initialization"
        assert response.json()["selection"]["allowed_source_ids"] == [source["source_id"]]
        current = client.get(f"/api/v1/quest-initializations/{first_id}").json()
        assert current["quest_draft"] == opened["quest_draft"]
        stale = client.put(f"/api/v1/quest-initializations/{first_id}/search-sources", headers=headers,
            json={"allowed_source_ids": [], "expected_revision": 0})
        assert stale.status_code == 409
        cancelled = client.post(f"/api/v1/quest-initializations/{first_id}/cancel", headers=_write_headers(headers, "cancel-1"), json={})
        assert cancelled.status_code == 200
        second = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, "open-2"), json={}).json()
        assert second["initialization_id"] != first_id
        other = client.get(f"/api/v1/quest-initializations/{second['initialization_id']}/search-sources").json()
        assert other["selection"]["allowed_source_ids"] == []
        changed = client.put(f"/api/v1/search-sources/{source['source_id']}", headers=headers,
            json={"form": {**form, "name": "Updated pages"}, "expected_version": source["version"]})
        assert changed.status_code == 200
        assert client.get(f"/api/v1/quest-initializations/{first_id}/search-sources").json()["selection"] == response.json()["selection"]
        assert client.get("/api/v1/search-sources").json()["templates"][0]["template"] == "crossref"
    finally:
        client.close()
        runtime._database.close()


def test_new_deepfetch_request_captures_selected_versions_before_reuse(tmp_path: Path) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"),
        proposal_drafter=SnapshotAwareProposalDrafter(), deepfetch_provider=DeterministicDeepFetchProvider(),
        host_compute_probe=DeterministicProbe(), acquisition_provider=RecordingAcquisitionProvider())
    client, headers = _authenticated_client(runtime)
    try:
        initialization_id, first = _open_and_queue_deepfetch(client, headers, key_prefix="basis")
        request = runtime.owners.human_collaboration.query_next_deepfetch_request()
        assert request is not None
        original_ref = request.request_ref
        assert request.scope["search_source_basis"]["sources"] == []
        source = runtime.search_sources.save({"kind": "website", "name": "Pages", "url": "https://arxiv.org"})
        selected = client.put(f"/api/v1/quest-initializations/{initialization_id}/search-sources", headers=headers,
            json={"allowed_source_ids": [source["source_id"]], "expected_revision": 0})
        assert selected.status_code == 200
        draft = first["quest_draft"]
        queued = client.post(f"/api/v1/quest-initializations/{initialization_id}/proposal-generations",
            headers=_write_headers(headers, "basis-second-start"), json={
                "expected_draft_revision": draft["revision"], "expected_draft_hash": draft["hash"]})
        assert queued.status_code == 202
        with runtime._database.read() as connection:
            rows = connection.execute(text("SELECT request_ref, scope_json FROM hc_deepfetch_requests WHERE initialization_id=:ref ORDER BY created_at"), {"ref": initialization_id}).all()
        assert len(rows) == 2
        assert rows[0].request_ref == original_ref
        assert json.loads(rows[0].scope_json)["search_source_basis"]["sources"] == []
        assert json.loads(rows[1].scope_json)["search_source_basis"]["sources"] == [{"source_id": source["source_id"], "source_version": source["version"]}]
    finally:
        client.close()
        runtime._database.close()


class OptionalInstitutionProvider(RecordingAcquisitionProvider):
    def preflight(self, request) -> AcquisitionPreflightResult:
        self.preflights.append(request)
        waiting = request.mode == "oa_then_institution" and not request.library_entry_url
        return AcquisitionPreflightResult(status="waiting_user" if waiting else "ready", browser_context_ref=None,
            reason_code="institution_entry_url_required" if waiting else None, evidence={"configuration_health": "waiting" if waiting else "ready"})


@pytest.mark.parametrize(("required", "mode", "status"), [(False, "oa_only", "ready"), (True, "oa_then_institution", "waiting_user")])
def test_optional_institution_mode_is_resolved_before_hash_and_preflight(tmp_path: Path, required: bool, mode: str, status: str) -> None:
    provider = OptionalInstitutionProvider()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), acquisition_provider=provider)
    client, headers = _authenticated_client(runtime)
    try:
        opened = client.post("/api/v1/quest-initializations", headers=_write_headers(headers, "optional-open"), json={}).json()
        initialization_id = opened["initialization_id"]
        draft = opened["quest_draft"]
        value = {**draft["value"], "literature": {**draft["value"]["literature"], "institution_required": required}}
        saved = client.put(f"/api/v1/quest-initializations/{initialization_id}/draft", headers=_write_headers(headers, "optional-save"),
            json={"expected_draft_revision": draft["revision"], "expected_draft_hash": draft["hash"], "draft": value})
        assert saved.status_code == 200
        draft = saved.json()["quest_draft"]
        prepared = client.post(f"/api/v1/quest-initializations/{initialization_id}/acquisition-session", headers=_write_headers(headers, "optional-preflight"),
            json={"expected_draft_revision": draft["revision"], "expected_draft_hash": draft["hash"]})
        assert prepared.status_code == 200
        session = runtime.owners.agent_runtime.query_acquisition_session(initialization_id=initialization_id)
        assert session.mode == mode
        assert session.status == status
        assert provider.preflights[0].mode == mode
        assert provider.preflights[0].config_hash == canonical_hash({"schema_ref": "meta-research/acquisition-session-config/v1", "mode": mode, "library_entry_url": ""})
        assert runtime.owners.agent_runtime.query_acquisition_session(session_ref=session.session_ref).mode == mode
    finally:
        client.close()
        runtime._database.close()
