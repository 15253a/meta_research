from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import text

from meta_research.experiment_logs import TargetExperimentLogs
from meta_research.harness import HarnessAdmissionError
from meta_research.human_reply import ProvidedReply, ServerReference, Upload
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError
from test_issue296_stable_workspace import (
    _SignedCeilingRunner, _runtime, _activate, _workspace, _second_target,
)
from test_root_human_request_lifecycle import _open_arguments
from test_root_material_processing import _feedback
from test_target_root_finalizer import _admit_independent_target_root


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux recovery integration")


class _HumanRequestRunner(_SignedCeilingRunner):
    def __init__(self):
        super().__init__()
        self.opened = []
        self.responses = []
        self.treatment = None
        self.open_next = True

    def __call__(self, argv, prompt, timeout, environment):
        def call(operation, **arguments):
            status, payload, _ = self.runtime.harnesses.dispatch_mcp_http(
                environment["META_RESEARCH_MCP_TOKEN"],
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": operation, "arguments": arguments}}, mcp_session_id=None)
            assert status == 200 and not payload["result"]["isError"], payload
            return payload["result"]["structuredContent"]

        if "META_RESEARCH_PROVIDER_OPERATION_REF" in environment and len(self.calls) == 2:
            response = self.responses[0]
            exact = call("human_request.read", request_ref=self.opened[0]["request_ref"],
                response_ref=response["response_ref"], limit=16384)
            assert json.loads(exact["text"])["response"]["delivery"] == response["delivery"]
            reference = response["delivery"]["work_materials"][0]["reference_ref"]
            page = call("research_workspace.materials.discover", reference_ref=reference)
            observation = page["entries"][0]["observation"]["observation_ref"]
            read = call("research_workspace.materials.read", reference_ref=reference,
                path="", observation_ref=observation)
            assert read["text"] == "Calibration remains necessary.\n"
            self.treatment = call("research_workspace.materials.feedback", **_feedback(reference,
                effect_id="issue296-historical-response-consumed", selections=[]))
            assert self.treatment["receiver"]["request_ref"] == self.opened[0]["request_ref"]
            assert self.treatment["receiver"]["roots"][0]["root_session_ref"] == response["delivery"]["root_session_ref"]
        if "META_RESEARCH_PROVIDER_OPERATION_REF" in environment and self.open_next:
            self.open_next = False
            key = f"issue296-original-request-{len(self.opened)}"
            status, payload, _ = self.runtime.harnesses.dispatch_mcp_http(
                environment["META_RESEARCH_MCP_TOKEN"],
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "human_request.open", "arguments": _open_arguments(key, "offline_action")}},
                mcp_session_id=None,
            )
            assert status == 200 and not payload["result"]["isError"], payload
            opened = payload["result"]["structuredContent"]
            self.opened.append(opened)
            original = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"]) / (key + ".txt")
            original.write_text("Calibration remains necessary.\n")
            self.responses.append(self.runtime.owners.human_collaboration.respond_to_human_request(
                opened["request_ref"], reply=ProvidedReply(facts={},
                    note="Controlled engineering response; continue this original work.",
                    materials=(Upload("calibration.txt", "text/plain", b"Calibration remains necessary.\n"),
                        ServerReference(self.runtime.root_workspaces.server_files.inspect(str(original))))),
                idempotency_key=key + "-response",
            ))
        return super().__call__(argv, prompt, timeout, environment)


def _run_and_recover(runtime, admitted, handle, generation):
    with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
        runtime.harnesses.run_or_resume_target_root(admitted[3].run.request_ref,
            prompt="Continue the bounded original work.", mcp_base_url="http://127.0.0.1:8999")
    runtime.harnesses.recover_fenced_target_root(admitted[3].run.request_ref, old_handle=handle)
    assert runtime.target_run_runtime.process_once(handle.target_ref)
    assert runtime.target_run_runtime.process_once(handle.target_ref)
    current = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(handle.target_ref)
    runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
        handle=current, idempotency_key=f"issue296-consumer-reserve-{generation}")
    return current


def test_historical_and_current_human_delivery_keep_original_binding_and_dedup(tmp_path, monkeypatch):
    runner = _HumanRequestRunner()
    path = tmp_path / "issue296-human-consumers"
    runtime = _runtime(path, runner, parallel=True)
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-human-activate")
        old = admitted[-1]
        original_ref, original_cwd = _workspace(runtime, old)
        other = _second_target(runtime, old.target_ref)
        _activate(runtime, other, "issue296-human-other")
        other_ref, other_cwd = _workspace(runtime, other[-1])
        current = old
        for generation in (1, 2):
            current = _run_and_recover(runtime, admitted, current, generation)
        denied, _, _ = runtime.harnesses.dispatch_mcp_http(admitted[3].connection.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, mcp_session_id=None)
        assert denied == 401
        historical = runner.opened[0]
        with runtime._database.read() as connection:
            original_delivery = connection.execute(text(
                "SELECT delivery_ref,destination_json,receipt_json FROM hc_reply_deliveries "
                "WHERE request_ref=:ref AND state='ready'"), {"ref": historical["request_ref"]}).one()
        runtime.close()
        runtime = _runtime(path, runner, parallel=True)
        settled = runtime.owners.agent_runtime.query_human_request(historical["request_ref"])
        assert settled["status"] == "satisfied"
        assert settled["direct_waiters"][0]["status"] == "consumed"
        assert runtime.owners.agent_runtime.reconcile_root_human_request(historical["request_ref"]) == settled
        assert len(runner.calls) == 2
        owner = runtime.owners.agent_runtime
        original_binding = settled["open_effect"]["operation_binding"]
        with pytest.raises(OwnerConflict, match="root_agent_human_request_scope_stale"):
            owner.verify_root_agent_runtime_scope(root_kind="target", run_ref=old.target_run_ref,
                attempt_ref=old.execution_attempt_ref, root_session_ref=old.root_session_ref,
                fence_ref=old.execution_fence_ref, runtime_binding_hash=original_binding["runtime_binding_hash"])
        for field, replacement in (("root_session_ref", other[-1].root_session_ref),
                ("attempt_ref", current.execution_attempt_ref), ("fence_ref", current.execution_fence_ref),
                ("task_ref", other[-1].target_run_ref), ("runtime_binding_hash", "0" * 64),
                ("generation", 999)):
            invalid = deepcopy(settled)
            invalid["open_effect"]["operation_binding"][field] = replacement
            with pytest.raises(OwnerConflict):
                owner._verify_root_human_request_operation_binding(invalid, invalid["direct_waiters"][0])

        runner.open_next = True
        with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
            runtime.harnesses.run_or_resume_target_root(admitted[3].run.request_ref,
                prompt="Read the new response in the current root.", mcp_base_url="http://127.0.0.1:8999")
        assert len(runner.opened) == 2
        assert runner.treatment["processed_by"]["root_session_ref"] == current.root_session_ref
        assert runner.treatment["processed_by"]["work_ref"] == old.target_run_ref
        for opened, handle in ((historical, old), (runner.opened[1], current)):
            destination = runtime.root_workspaces.destination_for_human_request(
                request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
            assert destination.location.root_session_ref == handle.root_session_ref
            assert destination.location.work_ref == old.target_run_ref
            assert destination.request_ref == opened["request_ref"]
            delivery_ref = "issue296-late-" + opened["request_ref"]
            delivered = runtime.root_workspaces.deliver(destination, delivery_ref=delivery_ref,
                files=(("late-reply.txt", b"Preserve the original request and work."),))
            assert runtime.root_workspaces.deliver(destination, delivery_ref=delivery_ref,
                files=(("late-reply.txt", b"Preserve the original request and work."),)) == delivered
            assert delivered["root_session_ref"] == handle.root_session_ref
            assert (destination.location.directory / delivered["files"][0]["path"]).read_bytes() == b"Preserve the original request and work."
            with pytest.raises(SemanticMcpError, match="workspace_delivery_conflict"):
                runtime.root_workspaces.deliver(destination, delivery_ref=delivery_ref,
                    files=(("late-reply.txt", b"Different bytes must not replace the original."),))
            forged = replace(destination, location=replace(destination.location,
                root_session_ref=other[-1].root_session_ref, workspace_ref=other_ref, directory=other_cwd))
            with pytest.raises(SemanticMcpError, match="workspace_destination_changed"):
                runtime.root_workspaces.deliver(forged, delivery_ref="issue296-cross-target",
                    files=(("forbidden.txt", b"Must never reach B."),))
            assert not (other_cwd / "inbox").exists()
            assert destination.location.workspace_ref == original_ref
            assert destination.location.directory == original_cwd

        query = runtime.owners.human_collaboration.query_human_request
        corrupt = deepcopy(query(historical["request_ref"]))
        corrupt["open_effect"]["operation_binding"]["root_session_ref"] = other[-1].root_session_ref
        with monkeypatch.context() as patch:
            patch.setattr(runtime.owners.human_collaboration, "query_human_request", lambda _ref: corrupt)
            with pytest.raises(SemanticMcpError, match="workspace_destination_unbound"):
                runtime.root_workspaces.destination_for_human_request(
                    request_ref=historical["request_ref"], waiter_ref=historical["waiter"]["waiter_ref"])
        with runtime._database.read() as connection:
            assert connection.execute(text(
                "SELECT delivery_ref,destination_json,receipt_json FROM hc_reply_deliveries "
                "WHERE request_ref=:ref AND state='ready'"), {"ref": historical["request_ref"]}).one() == original_delivery
        assert len(runner.calls) == 3
    finally:
        runtime.close()


def test_experiment_log_cursor_survives_two_real_identity_rotations(tmp_path):
    runner = _SignedCeilingRunner()
    runtime = _runtime(tmp_path / "issue296-log-consumer", runner)
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-log-activate")
        handle = admitted[-1]
        _ref, cwd = _workspace(runtime, handle)
        log = cwd / "logs" / "train.log"
        log.parent.mkdir()
        log.write_text("segment 0\n")
        authority = runtime.target_run_authorities.agent_runtime
        service = TargetExperimentLogs(authority, authority._workspace_root)
        entry, = service.list(handle.target_ref)["logs"]
        initial = service.read(handle.target_ref, entry["log_ref"])
        for generation in (1, 2):
            handle = _run_and_recover(runtime, admitted, handle, generation)
        _, current_cwd = _workspace(runtime, handle)
        with (current_cwd / "logs" / "train.log").open("a") as stream:
            stream.write("segment 1\n")
        current = service.read(handle.target_ref, entry["log_ref"], target_run_ref=handle.target_run_ref,
            after=initial["next_offset"], stream_ref=initial["stream_ref"])
        assert current["text"] == "segment 1\n"
        assert current["stream_ref"] == initial["stream_ref"]
        assert current_cwd == cwd
        with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
            runtime.harnesses.run_or_resume_target_root(admitted[3].run.request_ref,
                prompt="Preserve the log when this operation fails.", mcp_base_url="http://127.0.0.1:8999")
        assert runtime.owners.agent_runtime.harness_runs.latest_operation(handle.target_run_ref).status == "failed"
        after_failure = service.read(handle.target_ref, entry["log_ref"], stream_ref=initial["stream_ref"])
        assert after_failure["text"] == "segment 0\nsegment 1\n"
    finally:
        runtime.close()


class _LinkedOriginalRunner(_SignedCeilingRunner):
    def __init__(self):
        super().__init__()
        self.accepted = []
        self.directories = []

    def __call__(self, argv, prompt, timeout, environment):
        if "META_RESEARCH_PROVIDER_OPERATION_REF" in environment:
            def call(operation, **arguments):
                status, payload, _ = self.runtime.harnesses.dispatch_mcp_http(
                    environment["META_RESEARCH_MCP_TOKEN"],
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": operation, "arguments": arguments}}, mcp_session_id=None)
                assert status == 200, payload
                return payload["result"]

            def accepted(operation, **arguments):
                result = call(operation, **arguments)
                assert not result["isError"], result
                return result["structuredContent"]

            def read(retained):
                reader = dict(retained["reader"])
                return call("research_memory.content.read", **reader)

            directory = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"])
            self.directories.append(directory)
            handle = self.runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(
                self.target_ref)
            workspace_ref, _ = _workspace(self.runtime, handle)
            if len(self.calls) == 0:
                self.original = directory / "retained-source.txt"
                self.original.write_text("Original accepted observation: 296.\n")
                selected = self.original
                acquired = accepted("research_workspace.materials.acquire",
                    effect_id="issue296-acquire-original", absolute_path=str(self.original),
                    description="Controlled observation for the original Target work.")
                self.reference = acquired["references"][0]["reference_ref"]
                page = accepted("research_workspace.materials.discover", reference_ref=self.reference)
                observation = page["entries"][0]["observation"]["observation_ref"]
                assert accepted("research_workspace.materials.read", reference_ref=self.reference,
                    path="", observation_ref=observation)["text"] == self.original.read_text()
            else:
                original = read(self.accepted[0])
                assert not original["isError"], original
                assert original["structuredContent"]["text"] == "Original accepted observation: 296.\n"
                selected = directory / "selected-analysis.txt"
                selected.write_text("New analysis: verify calibration before comparison.\n")
                (directory / "unselected-draft.txt").write_text("Rejected speculative draft.\n")
            if len(self.calls) < 2:
                effect_id = f"issue296-select-{len(self.calls)}"
                selection = {"source": {"kind": "workspace_file", "workspace_ref": workspace_ref,
                    "path": selected.name, "expected_sha256": hashlib.sha256(selected.read_bytes()).hexdigest()},
                    "custody": "linked_local", "purpose": "Retain the explicitly selected original or separate analysis."}
                feedback = accepted("research_workspace.materials.feedback", **_feedback(self.reference,
                    effect_id=effect_id, selections=[selection]))
                assert accepted("research_workspace.materials.feedback.reconcile", effect_id=effect_id) == feedback
                retained, = feedback["selections"]
                assert retained["custody"] == "linked_local"
                self.accepted.append(retained)
            else:
                analysis = read(self.accepted[1])
                assert not analysis["isError"], analysis
                assert analysis["structuredContent"]["text"] == selected.read_text()
                self.original.write_text("Unauthorized overwrite of the formal original.\n")
                drift = read(self.accepted[0])
                assert drift["isError"] and drift["structuredContent"]["code"] == "content_drifted"
                assert not read(self.accepted[1])["isError"]
        return super().__call__(argv, prompt, timeout, environment)


def test_recovered_root_reads_linked_original_and_accepts_only_selected_new_file(tmp_path):
    runner = _LinkedOriginalRunner()
    runtime = _runtime(tmp_path / "issue296-linked-consumer", runner)
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-linked-activate")
        handle = admitted[-1]
        runner.target_ref = handle.target_ref
        _, original_directory = _workspace(runtime, handle)
        for generation in (1, 2):
            handle = _run_and_recover(runtime, admitted, handle, generation)
        with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
            runtime.harnesses.run_or_resume_target_root(admitted[3].run.request_ref,
                prompt="Read the accepted original and separately selected analysis.",
                mcp_base_url="http://127.0.0.1:8999")
        assert len(runner.accepted) == 2
        hashes = {asset.content_hash for asset in runtime.owners.research_memory.query_asset_inventory()}
        assert hashlib.sha256(b"Rejected speculative draft.\n").hexdigest() not in hashes
        assert runner.directories == [original_directory] * 3
    finally:
        runtime.close()


class _UnansweredRequestRunner(_SignedCeilingRunner):
    def __call__(self, argv, prompt, timeout, environment):
        if "META_RESEARCH_PROVIDER_OPERATION_REF" in environment and not self.calls:
            status, payload, _ = self.runtime.harnesses.dispatch_mcp_http(
                environment["META_RESEARCH_MCP_TOKEN"],
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                    "name": "human_request.open", "arguments": _open_arguments(
                        "issue296-pending-historical-reply", "offline_action")}}, mcp_session_id=None)
            assert status == 200 and not payload["result"]["isError"], payload
            self.opened = payload["result"]["structuredContent"]
        return super().__call__(argv, prompt, timeout, environment)


def test_startup_finishes_pending_historical_delivery_without_provider_resume(tmp_path, monkeypatch):
    class ProcessCrash(BaseException):
        pass

    runner = _UnansweredRequestRunner()
    path = tmp_path / "issue296-pending-historical-delivery"
    runtime = _runtime(path, runner)
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "issue296-pending-delivery-activate")
        original = handle = admitted[-1]
        workspace_ref, directory = _workspace(runtime, original)
        for generation in (1, 2):
            handle = _run_and_recover(runtime, admitted, handle, generation)
        reply = ProvidedReply("Actual late response to the original request.", {},
            (Upload("late.txt", "text/plain", b"Historical delivery bytes survive restart.\n"),))
        def crash(*args, **kwargs):
            raise ProcessCrash()
        with monkeypatch.context() as patch:
            patch.setattr(runtime.root_workspaces, "deliver", crash)
            with pytest.raises(ProcessCrash):
                runtime.owners.human_collaboration.respond_to_human_request(runner.opened["request_ref"],
                    reply=reply, idempotency_key="issue296-pending-historical-response")
        with runtime._database.read() as connection:
            pending = connection.execute(text("SELECT delivery_ref,response_ref,destination_json FROM hc_reply_deliveries "
                "WHERE request_ref=:ref AND state='pending'"), {"ref": runner.opened["request_ref"]}).one()
        runtime.close()
        runtime = _runtime(path, runner)
        settled = runtime.owners.agent_runtime.query_human_request(runner.opened["request_ref"])
        assert settled["status"] == "satisfied"
        assert settled["direct_waiters"][0]["status"] == "consumed"
        response, = settled["responses"]
        delivery = response["delivery"]
        assert response["response_ref"] == pending.response_ref
        assert delivery["delivery_ref"] == pending.delivery_ref
        assert delivery["root_session_ref"] == original.root_session_ref
        assert delivery["work_ref"] == original.target_run_ref
        assert delivery["workspace_ref"] == workspace_ref
        assert (directory / delivery["uploaded_files"][0]["path"]).read_bytes() == b"Historical delivery bytes survive restart.\n"
        assert runtime.owners.human_collaboration.respond_to_human_request(runner.opened["request_ref"],
            reply=reply, idempotency_key="issue296-pending-historical-response")["delivery"] == delivery
        assert runtime.owners.agent_runtime.reconcile_root_human_request(runner.opened["request_ref"]) == settled
        assert runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(original.target_ref) == handle
        assert len(runner.calls) == 2
        with runtime._database.read() as connection:
            ready = connection.execute(text("SELECT delivery_ref,response_ref,destination_json FROM hc_reply_deliveries "
                "WHERE request_ref=:ref AND state='ready'"), {"ref": runner.opened["request_ref"]}).one()
        assert ready == pending
    finally:
        runtime.close()
