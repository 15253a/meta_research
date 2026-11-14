"""Legacy failure setup uses a real helper, Owner job and signed invocation."""
from dataclasses import replace
from contextlib import ExitStack
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from meta_research.companion import CodexCompanionAdapter
from meta_research.creation_work import ProtectedCreationRunner
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.web import create_app
from test_creation_material_consumption import _open
from test_root_workspace import _make


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
    reason="requires actual Linux protected helper")


class LegacyFailureAdapter(CodexCompanionAdapter):
    """Exercise the former missing local-receipt argument without its cleanup."""

    def understand_initialization(self, request):
        work = self._workspaces.protected_creation(self._creation_workspace(request),
            request.inputs, operation_ref=request.job_ref)
        self.failed_work = work
        original_run = work.run

        def controlled_output(call):
            result = call.argv[call.argv.index("--output-last-message") + 1]
            script = "import json,pathlib,sys\npathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps({'reply':sys.argv[-1]}))\nprint(json.dumps({'type':'thread.started','thread_id':'legacy-native'}))"
            completed = original_run(replace(call, argv=("/bin/bash", "-c", 'exec /bin/python - "$@"',
                "creation", "--output-last-message", result, request.job_ref), prompt=script))
            if getattr(self, "on_native_return", None) is not None:
                self.on_native_return(work)
            return completed

        work.run = controlled_output
        access = work.channel()
        self.failed_token = access.token
        runner = ProtectedCreationRunner(work)
        return self._invoke(operation_name="initialization-understanding", prompt="Controlled legacy failure.",
            schema={"type": "object", "properties": {"reply": {"type": "string"}},
                "required": ["reply"], "additionalProperties": False},
            native_session_ref=None, job_ref=request.job_ref, workspace_binding=work.binding,
            provider_execution_binding=work.execution_binding, invocation_runner=runner,
            mcp_url=access.url, mcp_token=access.token, mcp_scope_binding_hash=access.scope_binding_hash,
            authorized_operation_ids=access.operation_ids)


@pytest.fixture
def failed_creation(tmp_path, monkeypatch, request):
    source = tmp_path / "source.txt"
    source.write_text("Original source remains readable.")
    runtime = _make(tmp_path / "data")
    try:
        with ExitStack() as stack:
            # Exercise HTTP owners without starting an unrelated background worker.
            client = TestClient(create_app(runtime, base_url="http://testserver", control_key="control"))
            stack.callback(client.close)
            anchor, _, _ = _open(client, runtime, source, "legacy-open")
            human = runtime.owners.human_collaboration
            probed = human.observe_host_compute(anchor.ref, ["GPU-plan-test"], "legacy-compute")
            draft = dict(probed["quest_draft"]["value"])
            draft.update(goal="Compare a bounded calibration.", completion_criteria="Record evidence and limitations.",
                time_budget="4h", route="direct", background_and_initial_direction="Read the supplied original.")
            saved = human.revise_quest_draft(anchor.ref, draft, probed["quest_draft"]["hash"], "legacy-save",
                probed["quest_draft"]["revision"])
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            adapter = LegacyFailureAdapter(tmp_path / "adapter", executable="/bin/bash")
            adapter.bind_workspaces(roots)
            human._proposal_drafter = adapter
            queued = human.generate_question_proposal(anchor.ref, saved["quest_draft"]["hash"], "legacy-first",
                saved["quest_draft"]["revision"])
            generation = queued["proposal_generation"]["ref"]
            if getattr(request, "param", None) in {"observe-running", "observe-synthesis"}:
                def observe_running(work):
                    if request.param == "observe-synthesis":
                        adapter.synthesis_work = roots.protected_creation(work.operation.binding, work.operation.inputs,
                            operation_ref=generation + ":proposal")
                        adapter.synthesis_work.channel()
                    repeated = human.generate_question_proposal(anchor.ref, saved["quest_draft"]["hash"],
                        "while-still-running", saved["quest_draft"]["revision"])
                    assert repeated["proposal_generation"]["ref"] == generation
                    assert repeated["proposal_generation"]["status"] == "running"
                    assert _state(runtime, work.operation.operation_ref) == "active"
                    assert work._token is not None
                    adapter.running_observed = True
                adapter.on_native_return = observe_running

            def missing_argument(*args, **kwargs):
                raise TypeError("write_exit_receipt() missing required process_tree")

            with monkeypatch.context() as patch:
                patch.setattr("meta_research.idea_skill._write_local_exit_receipt", missing_argument)
                assert human.process_drafting_once()
            view = human.query_quest_creation(anchor.ref)
            assert view["proposal_generation"]["status"] == "failed"
            assert view["proposal_generation"]["failure"]["code"] == "proposal_output_invalid"
            work = adapter.failed_work
            directory = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": work.operation.operation_ref}) / "initialization-understanding"
            launch = next(path for path in work.runtime._control.glob("launch-*.json") if not path.name.endswith(".receipt.json"))
            receipt = launch.with_suffix(".receipt.json")
            # Retain the real helper closure while representing the old shape.
            value = json.loads(receipt.read_text())
            value.pop("process_tree", None)
            receipt.write_text(json.dumps(value))
            yield runtime, adapter, work, directory, launch, receipt, saved, generation
    finally:
        runtime.close()


def _generate(runtime, saved):
    return runtime.owners.human_collaboration.generate_question_proposal(saved["initialization_id"],
        saved["quest_draft"]["hash"], "legacy-retry", saved["quest_draft"]["revision"])


def _state(runtime, operation):
    with runtime.owners.human_collaboration._database.read() as connection:
        return connection.execute(text("SELECT state FROM hc_creation_material_operations WHERE operation_ref=:ref"),
            {"ref": operation}).scalar_one()


def test_formal_retry_closes_only_exact_failed_legacy_work_and_preserves_failure(failed_creation):
    runtime, adapter, work, directory, launch, receipt, saved, generation = failed_creation
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    retry = _generate(runtime, saved)
    assert retry["proposal_generation"]["ref"] != generation
    assert retry["proposal_generation"]["status"] == "queued"
    assert _state(runtime, work.operation.operation_ref) == "failed"
    assert work._token is None
    assert runtime.root_workspaces._creation_gateway.dispatch(adapter.failed_token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0] == 401
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before
    with runtime.owners.human_collaboration._database.read() as connection:
        assert connection.execute(text("SELECT status,failure_code FROM hc_proposal_generation_attempts WHERE generation_ref=:ref"),
            {"ref": generation}).one() == ("failed", "proposal_output_invalid")
        assert connection.execute(text("SELECT count(*) FROM rm_creation_bases")).scalar_one() == 0


@pytest.mark.parametrize("corruption", ["foreign_jail", "wrong_receipt", "changed_output", "missing_receipt", "unknown_tree", "duplicate_launch", "foreign_invocation", "symlink_output"])
def test_formal_retry_blocks_unverified_legacy_closure(failed_creation, corruption):
    runtime, adapter, work, directory, launch, receipt, saved, generation = failed_creation
    if corruption == "foreign_invocation":
        path = directory / "invocation.json"
        value = json.loads(path.read_text())
        value["payload"]["provider_execution_binding"]["operation_ref"] += "-foreign"
        path.write_text(json.dumps(value))
    elif corruption == "symlink_output":
        argv = json.loads(launch.read_text())["argv"]
        path = work.runtime.jail_root / argv[argv.index("--output-last-message") + 1].lstrip("/")
        other = path.with_suffix(".original")
        path.rename(other)
        path.symlink_to(other)
    elif corruption == "changed_output":
        (directory / "last-message.json").write_text('{"reply":"different output"}')
    elif corruption == "missing_receipt":
        receipt.unlink()
    elif corruption == "unknown_tree":
        value = json.loads(receipt.read_text())
        value["descendants_ended"] = False
        receipt.write_text(json.dumps(value))
    else:
        value = json.loads(launch.read_text())
        if corruption == "foreign_jail":
            value["jail"] += "-foreign"
        elif corruption == "wrong_receipt":
            value["receipt"] = str(receipt.with_name("unrelated.receipt.json"))
        else:
            duplicate = launch.with_name("launch-" + "a" * 24 + ".json")
            value["receipt"] = str(duplicate.with_suffix(".receipt.json"))
            duplicate.write_text(json.dumps(value))
            duplicate.chmod(0o600)
            duplicate.with_suffix(".receipt.json").write_bytes(receipt.read_bytes())
            duplicate.with_suffix(".receipt.json").chmod(0o600)
        if corruption != "duplicate_launch":
            launch.write_text(json.dumps(value))
    with pytest.raises(OwnerConflict, match="protected_creation_cleanup_unverified"):
        _generate(runtime, saved)
    assert _state(runtime, work.operation.operation_ref) == "active"
    assert not (directory / "completed.json").exists()
    with runtime.owners.human_collaboration._database.read() as connection:
        assert connection.execute(text("SELECT count(*) FROM hc_proposal_generation_attempts")).scalar_one() == 1


def test_formal_retry_does_not_close_a_stale_draft_operation(failed_creation):
    runtime, adapter, work, directory, launch, receipt, saved, generation = failed_creation
    draft = dict(saved["quest_draft"]["value"])
    draft["goal"] += " Changed scope."
    current = runtime.owners.human_collaboration.revise_quest_draft(saved["initialization_id"], draft,
        saved["quest_draft"]["hash"], "changed-after-failure", saved["quest_draft"]["revision"])
    with pytest.raises(OwnerConflict, match="protected_creation_cleanup_unverified"):
        _generate(runtime, current)
    assert _state(runtime, work.operation.operation_ref) == "active"


@pytest.mark.parametrize("failed_creation", ["observe-running", "observe-synthesis"], indirect=True)
def test_running_generation_is_reused_without_failure_cleanup(failed_creation):
    runtime, adapter, work, directory, launch, receipt, saved, generation = failed_creation
    assert adapter.running_observed is True
    assert _state(runtime, work.operation.operation_ref) == "active"
    if hasattr(adapter, "synthesis_work"):
        assert _state(runtime, adapter.synthesis_work.operation.operation_ref) == "active"
        assert adapter.synthesis_work._token is not None


def test_active_deepfetch_preparation_keeps_its_existing_generation_path(tmp_path):
    from meta_research.creation_inputs import CreationAnchor
    from test_deepfetch_material_read import _accept, _queue
    from test_public_manual_question_lifecycle import _build_runtime, DeterministicAcquisitionProvider

    runtime = _build_runtime(tmp_path / "data", acquisition_provider=DeterministicAcquisitionProvider())
    try:
        human = runtime.owners.human_collaboration
        asset = _accept(runtime.owners.research_memory, b"%PDF-1.4\ncontrolled accepted input", "deepfetch-selected")
        issued = _queue(runtime, asset.as_binding().as_dict(), key="deepfetch-existing")
        view = human.query_quest_creation(issued.initialization_id)
        roots = runtime.root_workspaces
        roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
        binding = roots.bind_initialization(issued.initialization_id, view["intent_session"]["ref"])
        anchor = CreationAnchor("quest_initialization", issued.initialization_id, None,
            issued.draft_revision, issued.draft_hash)
        preparation = issued.scope.get("preparation", {"ref": issued.request_ref})["ref"]
        work = roots.protected_creation(binding, human.creation_material_snapshot(anchor),
            operation_ref=preparation + ":understanding")
        adapter = CodexCompanionAdapter(tmp_path / "adapter", executable="/bin/bash")
        adapter.bind_workspaces(roots)
        human._proposal_drafter = adapter
        repeated = human.generate_question_proposal(issued.initialization_id, issued.draft_hash,
            "repeat-active-deepfetch", issued.draft_revision)
        assert repeated["deepfetch"]["request_ref"] == issued.request_ref
        assert _state(runtime, work.operation.operation_ref) == "active"
    finally:
        runtime.close()


def test_running_material_intent_allows_queue_and_owner_waits_for_original_turn(tmp_path):
    from meta_research.quest_drafting import DraftingUnavailable

    source = tmp_path / "original.txt"
    source.write_text("Bounded original for the current intent.")
    runtime = _make(tmp_path / "data")
    try:
        with ExitStack() as stack:
            client = TestClient(create_app(runtime, base_url="http://testserver", control_key="control"))
            stack.callback(client.close)
            anchor, _, _ = _open(client, runtime, source, "running-intent")
            human = runtime.owners.human_collaboration
            observed = human.observe_host_compute(anchor.ref, ["GPU-plan-test"], "intent-compute")
            draft = dict(observed["quest_draft"]["value"])
            draft.update(goal="Read the calibration.", completion_criteria="Keep evidence and limits.", time_budget="4h")
            saved = human.revise_quest_draft(anchor.ref, draft, observed["quest_draft"]["hash"], "intent-save",
                observed["quest_draft"]["revision"])
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")

            class PendingIntent(CodexCompanionAdapter):
                def reply(self, request):
                    self.work = roots.protected_creation(self._creation_workspace(request), request.inputs,
                        operation_ref=request.job_ref)
                    self.work.channel()
                    self.queued = human.generate_question_proposal(anchor.ref, saved["quest_draft"]["hash"],
                        "generate-during-intent", saved["quest_draft"]["revision"])
                    raise DraftingUnavailable("codex_operation_reconciliation_pending")

            adapter = PendingIntent(tmp_path / "adapter", executable="/bin/bash")
            adapter.bind_workspaces(roots)
            human._proposal_drafter = human._intent_drafting_provider = adapter
            human.send_intent_message(anchor.ref, expected_draft_revision=saved["quest_draft"]["revision"],
                expected_draft_hash=saved["quest_draft"]["hash"], message="Read the current material.",
                idempotency_key="intent-message")
            assert human._process_intent_turn_once() is False
            assert adapter.queued["proposal_generation"]["status"] == "queued"
            assert _state(runtime, adapter.work.operation.operation_ref) == "active"
            assert adapter.work._token is not None
            assert human.query_quest_creation(anchor.ref)["intent_session"]["turns"][-1]["assistant_status"] == "running"
            assert human._process_proposal_generation_once() is False
    finally:
        runtime.close()
