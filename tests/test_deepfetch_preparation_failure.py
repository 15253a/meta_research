import os

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.deepfetch import DeepFetchUnavailable
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import DraftingUnavailable
from test_creation_companion_protected import ProtectedCreationFixture
from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider, DeterministicProbe, RecordingAcquisitionProvider, _authenticate,
    _open_and_queue_deepfetch, _write_headers,
)
from test_public_work_materials import _source


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
                                reason="requires actual protected creation runtime")


class RejectFirstUnderstanding(ProtectedCreationFixture):
    def _invoke(self, **arguments):
        result = super()._invoke(**arguments)
        if arguments["operation_name"] == "initialization-understanding" and len(self.calls) == 1:
            raise IdeaSkillUnavailable("creation_input_stale")
        return result


@pytest.fixture
def preparation(tmp_path):
    data_root = prepare_data_root(tmp_path / "data")
    drafter = RejectFirstUnderstanding(data_root.root / "companion-provider")
    provider = DeterministicDeepFetchProvider()
    runtime = build_production_runtime(data_root, proposal_drafter=drafter,
        deepfetch_provider=provider, host_compute_probe=DeterministicProbe(),
        acquisition_provider=RecordingAcquisitionProvider())
    runtime.root_workspaces.configure_creation_runtime(executable="/bin/bash",
                                                       credentials_home=tmp_path / "credentials")
    client, headers = _authenticate(runtime)
    try:
        initialization, queued = _open_and_queue_deepfetch(client, headers, key_prefix="preparation")
        source = tmp_path / "note.txt"
        source.write_text("Calibration16 only applies in room conditions.")
        human = runtime.owners.human_collaboration
        delivered = client.post(f"/api/v1/quest-initializations/{initialization}/material-references",
            headers=_write_headers(headers, "preparation-material"),
            json={"receiver": human.material_receiver("creation", initialization),
                  "selections": [_source(client, source)], "description": "Read the calibration note."})
        assert delivered.status_code == 201, delivered.text
        yield runtime, client, headers, initialization, queued, drafter, provider
    finally:
        client.close()
        runtime.close()


def retry(client, headers, initialization, view, key="retry-preparation"):
    return client.post(f"/api/v1/quest-initializations/{initialization}/proposal-generations",
        headers=_write_headers(headers, key),
        json={"expected_draft_revision": view["quest_draft"]["revision"],
              "expected_draft_hash": view["quest_draft"]["hash"]})


def test_understanding_failure_is_terminal_and_explicit_retry_uses_a_new_protected_operation(preparation):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    request_ref = queued["deepfetch"]["request_ref"]
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert failed["deepfetch"]["status"] == "failed"
    assert failed["deepfetch"]["failure"] == {"code": "creation_input_stale"}
    assert failed["deepfetch"]["run"] is None
    first = failed["deepfetch"]["preparation"]
    assert first["ref"] == request_ref
    assert first["operation_state"] == "failed"
    assert isinstance(first["started_at"], float)
    assert first["completed_at"] >= first["started_at"]
    assert runtime.deepfetch.process_once() is False
    assert len(drafter.calls) == 1
    assert provider.requests == []

    predecessor = runtime.owners.human_collaboration.query_deepfetch_request(request_ref)

    response = retry(client, headers, initialization, failed)
    assert response.status_code == 202, response.text
    retried = response.json()
    assert retried["deepfetch"]["request_ref"] == request_ref
    assert retried["deepfetch"]["correlation_ref"] == failed["deepfetch"]["correlation_ref"]
    second = retried["deepfetch"]["preparation"]
    assert second["ref"] != first["ref"]
    assert second["ref"].startswith(request_ref + ":")
    assert second["started_at"] is None
    assert second["completed_at"] is None
    assert retry(client, headers, initialization, failed).json()["deepfetch"]["preparation"] == second
    runtime.owners.human_collaboration.record_deepfetch_failed(request_ref,
        "late_predecessor_failure", expected_scope_hash=predecessor.scope_hash)
    unchanged = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert unchanged["deepfetch"]["status"] == "queued"
    assert unchanged["deepfetch"]["failure"] is None
    stale = client.get(f"/api/v1/quest-initializations/{initialization}/preparation-output",
        params={"request_ref": request_ref, "preparation_ref": first["ref"]})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "preparation_output_request_changed"

    assert runtime.deepfetch.process_once() is True
    succeeded = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert succeeded["deepfetch"]["status"] == "succeeded"
    assert succeeded["deepfetch"]["run"]["attempt_generation"] == 1
    assert len(provider.requests) == 1
    assert [call[0]["job_ref"] for call in drafter.calls] == [
        first["ref"] + ":understanding", second["ref"] + ":understanding"]
    with runtime._database.read() as connection:
        states = connection.execute(text("SELECT operation_ref,state FROM hc_creation_material_operations ORDER BY created_at")).all()
    assert [(row.operation_ref, row.state) for row in states] == [
        (first["ref"] + ":understanding", "failed"),
        (second["ref"] + ":understanding", "sealed")]


@pytest.mark.parametrize("operation_state", ["active", "unknown_outcome", "sealed"])
def test_preparation_retry_does_not_relaunch_unconfirmed_or_sealed_operation(preparation, operation_state):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE hc_creation_material_operations SET state=:state"),
                           {"state": operation_state})
    response = retry(client, headers, initialization, failed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "protected_creation_unknown_outcome"
    current = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert current["deepfetch"]["status"] == "failed"
    assert current["deepfetch"]["preparation"]["ref"] == queued["deepfetch"]["request_ref"]
    assert current["deepfetch"]["preparation"]["operation_state"] == operation_state
    assert runtime.deepfetch.process_once() is False
    assert len(drafter.calls) == 1
    assert provider.requests == []


def test_legacy_stuck_request_records_the_current_rejection_without_inventing_history(preparation):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    assert runtime.deepfetch.process_once() is True
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE hc_deepfetch_requests SET status='queued',failure_code=NULL,completed_at=NULL"))
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert failed["deepfetch"]["failure"] == {"code": "protected_creation_unknown_outcome"}
    assert failed["deepfetch"]["status"] == "failed"
    assert failed["deepfetch"]["run"] is None
    assert len(drafter.calls) == 1
    assert retry(client, headers, initialization, failed).status_code == 202


@pytest.mark.parametrize("failure_code", [
    "protected_creation_active", "protected_creation_unknown_outcome",
    "codex_operation_reconciliation_pending", "codex_job_outcome_unknown",
    "codex_job_spool_invalid", "codex_job_spool_conflict",
])
def test_active_preparation_error_without_durable_terminal_evidence_blocks_retry(preparation, monkeypatch, failure_code):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    def active(_request):
        raise DraftingUnavailable(failure_code)
    monkeypatch.setattr(drafter, "understand_initialization", active)
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert failed["deepfetch"]["status"] == "failed"
    assert failed["deepfetch"]["failure"] == {"code": failure_code}
    assert failed["deepfetch"]["preparation"]["retry_permitted"] is False
    response = retry(client, headers, initialization, failed)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "protected_creation_unknown_outcome"
    assert runtime.deepfetch.process_once() is False
    assert drafter.calls == []
    assert provider.requests == []


def test_sealed_current_basis_retries_runtime_admission_without_repeating_understanding(preparation, monkeypatch):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    monkeypatch.setattr(drafter, "_invoke", super(RejectFirstUnderstanding, drafter)._invoke)
    execute = runtime.owners.agent_runtime.execute_deepfetch
    def unavailable(_request, _provider):
        raise OwnerConflict("runtime_admission_temporarily_unavailable")
    monkeypatch.setattr(runtime.owners.agent_runtime, "execute_deepfetch", unavailable)
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert failed["deepfetch"]["status"] == "failed"
    assert failed["deepfetch"]["failure"] == {"code": "runtime_admission_temporarily_unavailable"}
    assert failed["deepfetch"]["run"] is None
    assert failed["deepfetch"]["preparation"]["operation_state"] == "sealed"
    assert failed["deepfetch"]["preparation"]["retry_permitted"] is True
    assert failed["creation_basis"]["freshness"] == "current"
    with runtime._database.read() as connection:
        operation_started = connection.execute(text(
            "SELECT created_at FROM hc_creation_material_operations")).scalar_one()
    response = retry(client, headers, initialization, failed)
    assert response.status_code == 202, response.text
    assert response.json()["deepfetch"]["preparation"]["ref"] == queued["deepfetch"]["request_ref"]
    assert response.json()["deepfetch"]["preparation"]["started_at"] == operation_started
    monkeypatch.setattr(runtime.owners.agent_runtime, "execute_deepfetch", execute)
    assert runtime.deepfetch.process_once() is True
    succeeded = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert succeeded["deepfetch"]["status"] == "succeeded"
    assert succeeded["deepfetch"]["preparation"]["started_at"] == operation_started
    assert len(drafter.calls) == 1
    assert len(provider.requests) == 1


@pytest.mark.parametrize("error_type", [OwnerConflict, DeepFetchUnavailable])
def test_late_preparation_worker_error_cannot_fail_explicit_retry(preparation, monkeypatch, error_type):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    human = runtime.owners.human_collaboration
    prepare = human.prepare_deepfetch_creation_basis
    def finish_predecessor_after_retry(request):
        try:
            return prepare(request)
        except DraftingUnavailable as error:
            human.record_deepfetch_failed(request.request_ref, error.code,
                                         expected_scope_hash=request.scope_hash)
            failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
            response = retry(client, headers, initialization, failed)
            assert response.status_code == 202, response.text
            assert response.json()["deepfetch"]["preparation"]["ref"] != request.request_ref
            raise error_type("late_preparation_failure") from error
    monkeypatch.setattr(human, "prepare_deepfetch_creation_basis", finish_predecessor_after_retry)
    assert runtime.deepfetch.process_once() is True
    successor = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert successor["deepfetch"]["status"] == "queued"
    assert successor["deepfetch"]["failure"] is None
    assert successor["deepfetch"]["run"] is None
    assert len(drafter.calls) == 1
    assert provider.requests == []
    monkeypatch.setattr(human, "prepare_deepfetch_creation_basis", prepare)
    assert runtime.deepfetch.process_once() is True
    succeeded = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert succeeded["deepfetch"]["status"] == "succeeded"
    assert succeeded["deepfetch"]["run"]["attempt_generation"] == 1
    assert len(provider.requests) == 1


def test_sealed_preparation_retry_is_available_with_a_retained_stale_proposal(preparation, monkeypatch):
    runtime, client, headers, initialization, queued, drafter, provider = preparation
    human = runtime.owners.human_collaboration
    monkeypatch.setattr(drafter, "_invoke", super(RejectFirstUnderstanding, drafter)._invoke)
    assert runtime.deepfetch.process_once() is True
    assert human.process_drafting_once() is True
    original = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert original["proposal"]["status"] == "current"
    changed_draft = {**original["quest_draft"]["value"], "goal": "Compare calibration transfer under a revised condition."}
    changed = client.put(f"/api/v1/quest-initializations/{initialization}/draft",
        headers=_write_headers(headers, "revised-goal"), json={
            "expected_draft_revision": original["quest_draft"]["revision"],
            "expected_draft_hash": original["quest_draft"]["hash"], "draft": changed_draft})
    assert changed.status_code == 200, changed.text
    assert changed.json()["proposal"]["status"] == "stale"
    basis = {"expected_draft_revision": changed.json()["quest_draft"]["revision"],
             "expected_draft_hash": changed.json()["quest_draft"]["hash"]}
    acquisition = client.post(f"/api/v1/quest-initializations/{initialization}/acquisition-session",
        headers=_write_headers(headers, "revised-acquisition"), json=basis)
    assert acquisition.status_code == 200, acquisition.text
    response = retry(client, headers, initialization, changed.json(), "revised-start")
    assert response.status_code == 202, response.text
    execute = runtime.owners.agent_runtime.execute_deepfetch
    def unavailable(_request, _provider):
        raise OwnerConflict("runtime_admission_temporarily_unavailable")
    monkeypatch.setattr(runtime.owners.agent_runtime, "execute_deepfetch", unavailable)
    assert runtime.deepfetch.process_once() is True
    failed = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert failed["proposal"]["ref"] == original["proposal"]["ref"]
    assert failed["proposal"]["status"] == "stale"
    assert failed["deepfetch"]["run"] is None
    assert failed["deepfetch"]["preparation"]["operation_state"] == "sealed"
    assert failed["deepfetch"]["preparation"]["retry_permitted"] is True
    response = retry(client, headers, initialization, failed, "revised-retry")
    assert response.status_code == 202, response.text
    assert response.json()["deepfetch"]["preparation"]["ref"] == failed["deepfetch"]["preparation"]["ref"]
    monkeypatch.setattr(runtime.owners.agent_runtime, "execute_deepfetch", execute)
    assert runtime.deepfetch.process_once() is True
    succeeded = client.get(f"/api/v1/quest-initializations/{initialization}").json()
    assert succeeded["deepfetch"]["status"] == "succeeded"
