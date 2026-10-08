import base64
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root
from test_existing_project_creation import ExistingWorkAdapter
from test_public_first_question_deepfetch import (
    DeterministicProbe,
    _authenticate,
    _deepfetch_draft,
    _write_headers,
)


class SourceIntakingUnderstandingProvider(ExistingWorkAdapter):
    """An external provider can finish while an independent caller retires a source."""

    def __init__(self, before_return):
        self.before_return = before_return

    def understand_initialization(self, request):
        result = super().understand_initialization(request)
        selected = {
            item["material_key"] for item in result.understanding["selection"]
        }
        self.accepted_sources = []
        for entry in request.manifest["entries"]:
            if entry["material_key"] not in selected:
                continue
            content = self.workspaces.read_initialization(
                request.initialization_id,
                request.root_session_ref,
                workspace_ref=entry["workspace_ref"],
                path=entry["path"],
                expected_sha256=entry["sha256"],
            )["content"]
            accepted = self.memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="file",
                    custody_mode="managed",
                    display_name=Path(entry["relative_path"]).name,
                    media_type=mimetypes.guess_type(entry["relative_path"])[0]
                    or "application/octet-stream",
                    content=content,
                    provenance={
                        "kind": "external_existing_work",
                        "origin": entry["origin"],
                        "initialization_id": request.initialization_id,
                        "material_key": entry["material_key"],
                    },
                    origin_quest_ref=None,
                ),
                idempotency_key="creation-source:"
                + canonical_hash(
                    {"id": request.initialization_id, "key": entry["material_key"]}
                ),
            )
            assert accepted.status == "accepted", accepted.failure_code
            assert accepted.asset is not None
            self.accepted_sources.append(accepted.asset)
        self.before_return(self.accepted_sources)
        return result


def _queue_existing_work(client, headers):
    response = client.post(
        "/api/v1/quest-initializations",
        headers=_write_headers(headers, "open-race"),
        json={},
    )
    assert response.status_code == 201, response.json()
    opened = response.json()
    endpoint = f"/api/v1/quest-initializations/{opened['initialization_id']}"
    response = client.post(
        endpoint + "/material-deliveries",
        headers=_write_headers(headers, "deliver-race"),
        json={
            "expected_draft_revision": opened["quest_draft"]["revision"],
            "expected_draft_hash": opened["quest_draft"]["hash"],
            "submission_ref": "existing-work",
            "folder": True,
            "complete": True,
            "files": [
                {
                    "relative_path": "project/notes.md",
                    "content_base64": base64.b64encode(
                        b"Room improves; cold worsens. More field work remains.\n"
                    ).decode(),
                },
                {
                    "relative_path": "project/data/results.csv",
                    "content_base64": base64.b64encode(
                        b"condition,mae\nroom,0.3\ncold,1.85\n"
                    ).decode(),
                },
            ],
        },
    )
    assert response.status_code == 200, response.json()
    response = client.post(
        endpoint + "/compute-probe",
        headers=_write_headers(headers, "compute-race"),
        json={"selected_device_uuids": ["GPU-deepfetch-1"]},
    )
    assert response.status_code == 200, response.json()
    probed = response.json()
    draft = _deepfetch_draft(probed)
    draft["route"] = "direct"
    response = client.put(
        endpoint + "/draft",
        headers=_write_headers(headers, "draft-race"),
        json={
            "expected_draft_revision": probed["quest_draft"]["revision"],
            "expected_draft_hash": probed["quest_draft"]["hash"],
            "draft": draft,
        },
    )
    assert response.status_code == 200, response.json()
    drafted = response.json()
    response = client.post(
        endpoint + "/proposal-generations",
        headers=_write_headers(headers, "generate-race"),
        json={
            "expected_draft_revision": drafted["quest_draft"]["revision"],
            "expected_draft_hash": drafted["quest_draft"]["hash"],
        },
    )
    assert response.status_code == 202, response.json()
    return endpoint


def _retire_source(runtime, asset, key):
    memory = runtime.owners.research_memory
    return memory.retire_asset_version(
        asset.version_ref,
        expected_revision=memory.query_asset_lifecycle(asset.asset_ref)["revision"],
        expected_reference_revision=runtime.owners.research_graph.query_asset_reference_revision(),
        explanation="The source has a verified error and no remaining research or explanatory value.",
        low_value=True,
        obsolete=True,
        incorrect=True,
        impact_understood=True,
        has_explanation_value=False,
        idempotency_key=key,
    )


def test_retired_source_cannot_enter_a_new_creation_basis_after_intake_replay(tmp_path):
    root = prepare_data_root(tmp_path / "data")
    other_runtime = build_production_runtime(root)
    retirement = {}
    provider = SourceIntakingUnderstandingProvider(
        lambda sources: retirement.update(
            _retire_source(other_runtime, sources[0], "retire-before-basis")
        )
    )
    runtime = build_production_runtime(
        root,
        proposal_drafter=provider,
        host_compute_probe=DeterministicProbe(),
    )
    provider.memory = runtime.owners.research_memory
    client, headers = _authenticate(runtime)
    try:
        endpoint = _queue_existing_work(client, headers)
        assert runtime.owners.human_collaboration.process_drafting_once()
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        failed = response.json()
        assert retirement["kind"] == "retirement"
        assert failed["proposal_generation"]["status"] == "failed"
        assert failed["proposal_generation"]["failure"] == {
            "code": "asset_version_retired"
        }
        assert failed["creation_basis"] is None
        assert failed["proposal"] is None
        assert failed["confirmation_preview"] is None
        retired = provider.accepted_sources[0]
        assert (
            other_runtime.owners.research_memory.query_asset_lifecycle(retired.asset_ref)[
                "versions"
            ][0]["state"]
            == "retired"
        )
    finally:
        client.close()
        other_runtime.close()
        runtime.close()


def test_creation_basis_and_retirement_cannot_both_accept_the_exact_source(tmp_path):
    root = prepare_data_root(tmp_path / "data")
    other_runtime = build_production_runtime(root)
    sources_ready = Event()
    competing = Barrier(2)

    def release_understanding(sources):
        sources_ready.set()
        competing.wait(timeout=15)

    provider = SourceIntakingUnderstandingProvider(release_understanding)
    runtime = build_production_runtime(
        root,
        proposal_drafter=provider,
        host_compute_probe=DeterministicProbe(),
    )
    provider.memory = runtime.owners.research_memory
    client, headers = _authenticate(runtime)
    try:
        endpoint = _queue_existing_work(client, headers)

        def retire_competing_source():
            assert sources_ready.wait(timeout=15)
            competing.wait(timeout=15)
            try:
                return _retire_source(
                    other_runtime,
                    provider.accepted_sources[0],
                    "competing-retirement",
                )
            except OwnerConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            generation = pool.submit(
                runtime.owners.human_collaboration.process_drafting_once
            )
            retirement = pool.submit(retire_competing_source)
            assert generation.result(timeout=60)
            outcome = retirement.result(timeout=15)
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        creation = response.json()
        if isinstance(outcome, dict):
            assert outcome["kind"] == "retirement"
            assert creation["proposal_generation"]["status"] == "failed"
            assert creation["proposal_generation"]["failure"] == {
                "code": "asset_version_retired"
            }
            assert creation["creation_basis"] is None
            assert creation["proposal"] is None
            assert creation["confirmation_preview"] is None
        else:
            assert outcome.code == "asset_retirement_blocked"
            assert creation["proposal_generation"]["status"] == "succeeded"
            basis = creation["creation_basis"]
            assert basis is not None
            assert f"rm_creation_bases:{basis['basis_ref']}" in outcome.details[
                "active_reference_refs"
            ]
            assert provider.accepted_sources[0].version_ref in {
                source["binding"]["version_ref"] for source in basis["sources"]
            }
    finally:
        client.close()
        other_runtime.close()
        runtime.close()


def test_creation_basis_protects_its_exact_version_without_retaining_an_unused_successor(tmp_path):
    root = prepare_data_root(tmp_path / "data")
    other_runtime = build_production_runtime(root)
    provider = SourceIntakingUnderstandingProvider(lambda sources: None)
    runtime = build_production_runtime(
        root,
        proposal_drafter=provider,
        host_compute_probe=DeterministicProbe(),
    )
    provider.memory = runtime.owners.research_memory
    client, headers = _authenticate(runtime)
    try:
        endpoint = _queue_existing_work(client, headers)
        assert runtime.owners.human_collaboration.process_drafting_once()
        ready = client.get(endpoint).json()
        assert ready["proposal_generation"]["status"] == "succeeded"
        basis = ready["creation_basis"]
        original = next(
            asset for asset in provider.accepted_sources if asset.display_name == "notes.md"
        )
        memory = runtime.owners.research_memory
        successor = memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="file",
                custody_mode="managed",
                display_name=original.display_name,
                media_type=original.media_type,
                content=b"Later notes with a verified error; the original observation remains useful.\n",
                asset_ref=original.asset_ref,
                change={
                    "kind": "supplement",
                    "predecessor_version_ref": original.version_ref,
                    "expected_revision": memory.query_asset_lifecycle(original.asset_ref)[
                        "revision"
                    ],
                    "explanation": "Record later notes without replacing the exact earlier observation.",
                },
            ),
            idempotency_key="unused-source-successor",
        )
        assert successor.status == "accepted", successor.failure_code
        assert successor.asset is not None
        assert successor.asset.asset_ref == original.asset_ref
        assert successor.asset.version_ref != original.version_ref
        retired = _retire_source(
            other_runtime, successor.asset, "retire-unused-source-successor"
        )
        assert retired["kind"] == "retirement"
        assert retired["version_ref"] == successor.asset.version_ref
        with pytest.raises(OwnerConflict) as blocked:
            _retire_source(other_runtime, original, "retire-exact-source")
        assert blocked.value.code == "asset_retirement_blocked"
        assert f"rm_creation_bases:{basis['basis_ref']}" in blocked.value.details[
            "active_reference_refs"
        ]
        unchanged = client.get(endpoint).json()["creation_basis"]
        assert unchanged["basis_ref"] == basis["basis_ref"]
        assert unchanged["basis_hash"] == basis["basis_hash"]
        retained = next(
            source for source in unchanged["sources"]
            if source["relative_path"] == "project/notes.md"
        )
        assert retained["binding"]["version_ref"] == original.version_ref
        exact = memory.creation_bases.query(basis["basis_ref"], basis["basis_hash"])
        assert memory.creation_bases.read_source(exact, retained["material_key"])[
            "content"
        ] == b"Room improves; cold worsens. More field work remains.\n"
    finally:
        client.close()
        other_runtime.close()
        runtime.close()
