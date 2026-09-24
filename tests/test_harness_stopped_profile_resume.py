"""A stopped first turn can resume and publish verified completion evidence."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError, HarnessRuntime
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    read_supervisor_request,
    read_transport_key_for_operation,
    write_exit_receipt,
)
from meta_research.quest_drafting import _ProcessStopped
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.target_raw_output import TargetRawOutputStore
from test_harness_frozen_terminal_recovery import _StoppedWithLostReply
from test_harness_target_root import _Gateway, _WorkspaceResolver
from test_harness_target_root_recovery import _owner_with_active_root
from test_harness_terminal_replay import _StoppedProvider


NATIVE = "retained-native-session"
FINAL_TEXT = "The original Target completed its retained research outputs."


class _StoppedThenSuccessfulProvider(_StoppedWithLostReply):
    """Replace process execution only; keep real request/exit seals and adapters."""

    def __init__(self, *, stop_first=True, lose_stop_reply=False):
        super().__init__()
        self.stop_first = stop_first
        self.lose_stop_reply = lose_stop_reply
        self.success_native = NATIVE
        self.successful_request_paths = []

    def run_durable_job(self, *args, **kwargs):
        if self.calls == 0 and self.stop_first:
            result = _StoppedProvider.run_durable_job(self, *args, **kwargs)
            if self.lose_stop_reply:
                raise _ProcessStopped("Normal shutdown interrupted the reply")
            return result
        self.calls += 1
        request_path = args[6]
        self.successful_request_paths.append(request_path)
        _, key = read_transport_key_for_operation(request_path.parent)
        request = read_supervisor_request(request_path, key)
        self.continuation_argv = json.loads(request_path.with_name("provider-argv.json").read_text())
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started", "thread_id": self.success_native},
            {"type": "item.completed", "item": {
                "id": "command-1", "type": "command_execution", "command": "python inspect.py",
                "aggregated_output": "Retained outputs verified.", "exit_code": 0,
                "status": "completed",
            }},
            {"type": "item.completed", "item": {
                "id": "final-1", "type": "agent_message", "text": FINAL_TEXT,
            }},
            {"type": "turn.completed"},
        )) + "\n"
        stdout_path = Path(request["stdout_path"])
        stdout_path.write_text(stream, encoding="utf-8")
        prompt_path = Path(request["prompt_path"])
        write_exit_receipt(
            Path(request["receipt_path"]), key=key,
            invocation_hash=request["invocation_hash"], prompt_path=prompt_path,
            schema_path=Path(request["schema_path"]), stdout_path=stdout_path,
            result_path=Path(request["result_path"]), returncode=0,
            input_bytes=prompt_path.stat().st_size, termination_reason="completed",
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )
        return subprocess.CompletedProcess(args[1], 0, stream, "")


@pytest.fixture
def harness_case(tmp_path, monkeypatch):
    monkeypatch.setattr("meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS", 0.0)
    database, owner, request, handle = _owner_with_active_root(tmp_path / "owner.sqlite3")
    runner = _StoppedThenSuccessfulProvider()
    gateway = _Gateway()
    store = TargetRawOutputStore(tmp_path / "supervisor")
    owner.bind_target_provider_ceiling_receipt_verifier(store)

    def runtime():
        adapter = CodexHarnessAdapter(tmp_path / "adapter", runner=HarnessSupervisorTransport(
            tmp_path / "supervisor", process_runner=runner,
            event_sink=owner.append_target_root_events, raw_output_store=store,
        ))
        harness = HarnessRuntime(owner, gateway, (adapter,), target_raw_output_store=store)
        harness.bind_target_workspace_resolver(_WorkspaceResolver(tmp_path))
        return harness

    try:
        yield database, owner, request, handle, runner, runtime, store
    finally:
        database.close()


def _turn(runtime, request):
    return runtime().run_or_resume_target_root(
        request.request_ref, prompt="Continue the original retained Target work.",
        mcp_base_url="http://127.0.0.1:8999",
    )


def _checkpoint_stop(case):
    _database, owner, request, _handle, runner, runtime, _store = case
    first_error = "provider_io_unavailable" if runner.lose_stop_reply else "provider_stopped"
    with pytest.raises(HarnessAdmissionError, match=first_error):
        _turn(runtime, request)
    if runner.lose_stop_reply:
        with pytest.raises(HarnessAdmissionError, match="provider_stopped"):
            _turn(runtime, request)
    failed = owner.query_run(request.request_ref)
    assert failed.native_session_ref == NATIVE and failed.status == "failed"
    profile = owner.query_profile(failed.run_ref)
    assert set(profile) == {"schema_ref", "harness_family", "run_ref", "provider_transport_receipts"}
    assert profile["schema_ref"] == "meta-research/harness-failed-transport-profile/v1"
    assert len(profile["provider_transport_receipts"]) == 1
    return failed, profile


@pytest.mark.parametrize("lose_stop_reply", (False, True))
def test_signed_stop_resumes_same_native_and_owner_accepts_completion(harness_case, lose_stop_reply):
    _database, owner, request, handle, runner, runtime, store = harness_case
    runner.lose_stop_reply = lose_stop_reply
    original, failed_profile = _checkpoint_stop(harness_case)
    stopped_operation = owner.latest_operation(original.run_ref)
    old_files = {name: runner.request_path.with_name(name).read_bytes()
                 for name in (runner.request_path.name, "stdout.jsonl", "supervisor-exit.json")}
    runtime().recover_failed_target_root(request.request_ref)

    completed = _turn(runtime, request)

    assert completed.status == "executed" and completed.native_session_ref == NATIVE
    accepted = owner.query_run(request.request_ref)
    assert accepted.status == "executed" and accepted.native_session_ref == NATIVE
    assert (accepted.run_ref, accepted.attempt_ref, accepted.root_session_ref, accepted.fence_ref) == (
        original.run_ref, original.attempt_ref, original.root_session_ref, original.fence_ref,
    )
    operation = owner.latest_operation(original.run_ref)
    assert operation.generation == 2 and operation.status == "executed"
    assert runner.calls == 2
    assert runner.continuation_argv[-3:] == ["resume", NATIVE, "-"]
    profile = owner.query_profile(original.run_ref)
    assert profile["schema_ref"] == "meta-research/harness-capability-profile/v1"
    assert profile["capabilities"]["resume"]["status"] == "available"
    receipts = profile["provider_transport_receipts"]
    assert receipts[0] == failed_profile["provider_transport_receipts"][0]
    assert [r["provider_operation_ref"] for r in receipts] == [stopped_operation.operation_ref, operation.operation_ref]
    assert [r["termination_reason"] for r in receipts] == ["stopped", "completed"]
    store.verify_signed_transport_receipt(stopped_operation.operation_ref,
                                         {k: v for k, v in receipts[0].items() if k != "provider_operation_ref"})
    assert {name: runner.request_path.with_name(name).read_bytes() for name in old_files} == old_files
    evidence = owner.query_target_root_completion_evidence(handle.target_ref)
    assert evidence is not None
    assert evidence.operation_ref == operation.operation_ref
    assert evidence.native_session_ref == NATIVE and evidence.final_text == FINAL_TEXT
    assert owner.verify_target_root_completion_evidence(handle, evidence, evidence.handoff)


def test_complete_profiles_still_merge_two_successful_native_turns(harness_case):
    _database, owner, request, _handle, runner, runtime, _store = harness_case
    runner.stop_first = False
    first = _turn(runtime, request)
    first_profile = owner.query_profile(first.run_ref)
    second = _turn(runtime, request)
    merged = owner.query_profile(second.run_ref)
    assert second.status == "executed" and second.native_session_ref == first.native_session_ref == NATIVE
    assert merged["provider_operation_refs"] == [f"{first.run_ref}:harness_turn:1", f"{first.run_ref}:harness_turn:2"]
    assert merged["provider_transport_receipts"][0] == first_profile["provider_transport_receipts"][0]
    assert len(merged["provider_transport_receipts"]) == 2
    assert merged["capabilities"]["resume"]["status"] == "available"
    assert runner.calls == 2


def test_resumed_provider_cannot_replace_the_stopped_native_session(harness_case):
    _database, owner, request, handle, runner, runtime, _store = harness_case
    original, failed_profile = _checkpoint_stop(harness_case)
    runtime().recover_failed_target_root(request.request_ref)
    runner.success_native = "different-native-session"
    # The real streaming sink detects the foreign native identity before the
    # adapter consumes the completed spool; its transport boundary reports I/O.
    with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable") as failure:
        _turn(runtime, request)
    cause = failure.value
    cause_codes = []
    while cause is not None:
        cause_codes.append(getattr(cause, "code", None))
        cause = cause.__cause__
    assert "native_session_identity_changed" in cause_codes
    assert owner.query_run(request.request_ref).native_session_ref == NATIVE
    assert owner.latest_operation(original.run_ref).status != "executed"
    assert owner.query_profile(original.run_ref) == failed_profile
    assert owner.query_target_root_completion_evidence(handle.target_ref) is None


def _replace_profile(database, run_ref, profile):
    """Inject an isolated corrupt owner record, retaining its normal JSON seal."""
    with database.fenced_write() as connection:
        connection.execute(text(
            "UPDATE ar_harness_runs SET profile_json=:profile,profile_hash=:hash WHERE run_ref=:ref"
        ), {"profile": None if profile is None else canonical_json(profile),
            "hash": None if profile is None else canonical_hash(profile), "ref": run_ref})


@pytest.mark.parametrize("damage", (
    "receipt_seal", "stdout_hash", "profile_family", "profile_run",
    "missing_receipt", "foreign_operation", "not_stopped", "malformed_receipt",
))
def test_untrusted_failed_history_rejects_then_reconciles_same_successful_spool(harness_case, damage):
    database, owner, request, handle, runner, runtime, _store = harness_case
    original, failed_profile = _checkpoint_stop(harness_case)
    runtime().recover_failed_target_root(request.request_ref)
    original_profile = json.loads(json.dumps(failed_profile))
    changed_path = None
    old_bytes = None
    if damage in {"receipt_seal", "stdout_hash"}:
        changed_path = runner.request_path.with_name(
            "supervisor-exit.json" if damage == "receipt_seal" else "stdout.jsonl")
        old_bytes = changed_path.read_bytes()
        changed_path.write_bytes(old_bytes + b"untrusted")
    else:
        if damage == "profile_family":
            failed_profile["harness_family"] = "claude"
        elif damage == "profile_run":
            failed_profile["run_ref"] = "unrelated-target-run"
        elif damage == "missing_receipt":
            failed_profile["provider_transport_receipts"] = []
        elif damage == "foreign_operation":
            failed_profile["provider_transport_receipts"][0]["provider_operation_ref"] = "another-run:harness_turn:1"
        elif damage == "not_stopped":
            failed_profile["provider_transport_receipts"][0]["termination_reason"] = "completed"
        else:
            failed_profile["provider_transport_receipts"] = ["not-a-transport-receipt"]
        _replace_profile(database, original.run_ref, failed_profile)

    with pytest.raises(HarnessAdmissionError):
        _turn(runtime, request)

    operation = owner.latest_operation(original.run_ref)
    assert operation.generation == 2
    assert operation.status == "unknown_outcome", "A merge rejection must not strand a running operation."
    assert owner.query_target_root_completion_evidence(handle.target_ref) is None
    assert owner.query_run(request.request_ref).native_session_ref == NATIVE
    assert runner.calls == 2
    successful_spool = runner.successful_request_paths[-1]
    success_bytes = {name: successful_spool.with_name(name).read_bytes()
                     for name in (successful_spool.name, "stdout.jsonl", "supervisor-exit.json")}

    if changed_path is not None:
        changed_path.write_bytes(old_bytes)
    _replace_profile(database, original.run_ref, original_profile)
    recovered = _turn(runtime, request)

    assert recovered.status == "executed"
    reconciled = owner.latest_operation(original.run_ref)
    assert reconciled.operation_ref == operation.operation_ref
    assert reconciled.generation == 2 and reconciled.reconciliation_generation == 1
    assert reconciled.status == "executed" and runner.calls == 2
    assert {name: successful_spool.with_name(name).read_bytes() for name in success_bytes} == success_bytes
    evidence = owner.query_target_root_completion_evidence(handle.target_ref)
    assert evidence is not None and evidence.operation_ref == operation.operation_ref
    assert owner.verify_target_root_completion_evidence(handle, evidence, evidence.handoff)


def test_native_resume_with_no_previous_profile_remains_unavailable(harness_case):
    database, owner, request, handle, _runner, runtime, _store = harness_case
    original, _profile = _checkpoint_stop(harness_case)
    runtime().recover_failed_target_root(request.request_ref)
    _replace_profile(database, original.run_ref, None)
    with pytest.raises(HarnessAdmissionError, match="native_session_resume_unavailable"):
        _turn(runtime, request)
    assert owner.query_target_root_completion_evidence(handle.target_ref) is None


@pytest.mark.parametrize("identity", (
    "schema_ref", "harness_family", "locked_version", "provider_version", "native_session_ref",
    "run_ref", "attempt_ref", "attempt_generation", "root_session_ref", "fence_ref",
))
def test_complete_profile_identity_comparison_remains_strict(harness_case, identity):
    database, owner, request, handle, runner, runtime, _store = harness_case
    runner.stop_first = False
    original = _turn(runtime, request)
    profile = owner.query_profile(original.run_ref)
    profile[identity] = 99 if identity == "attempt_generation" else "changed-identity"
    _replace_profile(database, original.run_ref, profile)
    with pytest.raises(HarnessAdmissionError, match="harness_profile_identity_conflict"):
        _turn(runtime, request)
    assert owner.latest_operation(original.run_ref).status == "unknown_outcome"
    assert owner.query_target_root_completion_evidence(handle.target_ref) is None
