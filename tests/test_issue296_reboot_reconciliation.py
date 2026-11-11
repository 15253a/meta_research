from pathlib import Path
import json
import subprocess
import sys

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError, _harness_runtime_effect
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessSupervisorTransport
from meta_research.harness_control import DurableHarnessOperationCanceller
from meta_research.provider_supervisor import (
    read_transport_envelope, supervisor_host_identity, write_supervisor_request,
    write_transport_envelope,
)
from meta_research.runtime_protection import RuntimeEffectIdentity
from meta_research.streaming_provider_supervisor import reconcile_rebooted_supervisor
from meta_research.target_raw_output import TargetRawOutputStore, TargetRawOutputUnavailable
from meta_research.target_workspace_cleanup import _cycle_dependencies
from test_target_root_cancel import _running_root


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux boot identity evidence")
REBOOT_REASON = "provider_local_tree_closed_after_host_reboot"
OWNER_HASH = "f" * 64
OPERATION = "issue296-original-reboot-operation"


def _seed(workspace: Path, *, old_boot=True):
    transport = HarnessSupervisorTransport(workspace)
    key = transport._transport_key
    digest = "a" * 64
    directory = workspace / "provider-operations" / digest[:2] / digest
    directory.mkdir(parents=True)
    for name, value in (("prompt.txt", "original frozen input"), ("output-schema.json", "{}"), ("stdout.jsonl", "")):
        (directory / name).write_text(value, encoding="utf-8")
    request = directory / "supervisor-request.json"
    write_supervisor_request(request, transport._supervisor_request(directory, digest, "codex", None), key)
    dead = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    assert dead.wait(timeout=3) == 0
    identity = supervisor_host_identity()
    previous = "0" * 64 if identity["boot_identity_hash"] != "0" * 64 else "1" * 64
    write_transport_envelope(directory / "supervisor-ready.json", {
        "schema_ref": "meta-research/provider-supervisor-ready/v2", "invocation_hash": digest,
        "supervisor_process_id": dead.pid, "supervisor_process_group": dead.pid,
        **identity, "boot_identity_hash": previous if old_boot else identity["boot_identity_hash"],
    }, key)
    binding = workspace / "owner-invocation-bindings" / OWNER_HASH[:2] / (OWNER_HASH + ".json")
    binding.parent.mkdir(parents=True)
    write_transport_envelope(binding, {
        "schema_ref": "meta-research/harness-operation-binding/v1", "owner_invocation_hash": OWNER_HASH,
        "provider_operation_ref": OPERATION, "family": "codex", "transport_invocation_hash": digest,
    }, key)
    return transport, key, directory, binding


def _rewrite(path, key, **changes):
    value = read_transport_envelope(path, key)
    value.update(changes)
    path.unlink()
    write_transport_envelope(path, value, key)


@pytest.mark.parametrize("damage", [None, "same_boot", "wrong_host", "wrong_owner", "wrong_operation",
    "extra_binding_field", "recovery_tamper", "ready_hash", "request_hash", "missing_recovery"])
def test_reboot_readback_is_exact_read_only_local_evidence(tmp_path, monkeypatch, damage):
    workspace = tmp_path / "transport"
    transport, key, directory, binding = _seed(workspace)
    recovery = reconcile_rebooted_supervisor(directory / "supervisor-request.json")
    assert recovery["process_tree"]["state"] == "closed"
    if damage == "same_boot":
        monkeypatch.setattr("meta_research.streaming_provider_supervisor.supervisor_host_identity", lambda: {
            "host_identity_hash": recovery["process_tree"]["host_identity_hash"],
            "boot_identity_hash": recovery["process_tree"]["boot_identity_hash"],
        })
    elif damage == "wrong_host":
        monkeypatch.setattr("meta_research.streaming_provider_supervisor.supervisor_host_identity", lambda: {
            "host_identity_hash": "3" * 64, "boot_identity_hash": "4" * 64,
        })
    elif damage == "wrong_owner":
        _rewrite(binding, key, owner_invocation_hash="b" * 64)
    elif damage == "wrong_operation":
        _rewrite(binding, key, provider_operation_ref="another-target-operation")
    elif damage == "extra_binding_field":
        _rewrite(binding, key, unsupported_authority=True)
    elif damage == "recovery_tamper":
        path = directory / "supervisor-recovery.json"
        envelope = json.loads(path.read_text())
        envelope["payload"]["outcome"] = "success"
        path.write_text(json.dumps(envelope), encoding="utf-8")
    elif damage == "ready_hash":
        _rewrite(directory / "supervisor-ready.json", key, supervisor_process_id=11111)
    elif damage == "request_hash":
        _rewrite(directory / "supervisor-request.json", key, timeout_seconds=99)
    elif damage == "missing_recovery":
        (directory / "supervisor-recovery.json").unlink()
    before = {p.relative_to(workspace): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in workspace.rglob("*") if p.is_file()}
    store = TargetRawOutputStore(workspace)
    if damage is None:
        assert store.verify_signed_reboot_recovery(OPERATION, owner_invocation_hash=OWNER_HASH) == recovery
    else:
        with pytest.raises(TargetRawOutputUnavailable):
            store.verify_signed_reboot_recovery(OPERATION, owner_invocation_hash=OWNER_HASH)
    with pytest.raises(TargetRawOutputUnavailable):
        store.verify_signed_transport_receipt(OPERATION, None, owner_invocation_hash=OWNER_HASH)
    assert before == {p.relative_to(workspace): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in workspace.rglob("*") if p.is_file()}
    assert not (directory / "supervisor-exit.json").exists()


@pytest.mark.parametrize("reboot_before_stop", [True, False])
def test_failed_target_consumes_reboot_local_closure_without_settling_effects(tmp_path, monkeypatch, reboot_before_stop):
    runtime, handle, _ = _running_root(tmp_path)
    try:
        owner = runtime.owners.agent_runtime.harness_runs
        owner.start_operation(run_ref=handle.target_run_ref, operation_ref=OPERATION,
                              generation=1, invocation_hash=OWNER_HASH, resume=False)
        workspace = runtime.harnesses._target_raw_output_store._workspace
        transport, key, directory, binding = _seed(workspace, old_boot=reboot_before_stop)

        adapter = CodexHarnessAdapter(workspace, runner=transport)
        monkeypatch.setattr(adapter, "invoke", lambda *args, **kwargs: pytest.fail("reboot local closure must not replay a provider"))
        runtime.harnesses._adapters["codex"] = adapter
        runtime.harnesses._operation_canceller = DurableHarnessOperationCanceller(workspace)
        provider = _harness_runtime_effect(run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref, operation_ref=OPERATION, target_root=True, reconciling=False)
        external = RuntimeEffectIdentity(responsibility_ref="issue296-reboot-external-effect", owner_scope="agent_runtime",
            root_run_ref=handle.target_run_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref, operation_ref="original-independent-experiment", effect_kind="acquisition")
        protection = runtime.harnesses._runtime_protection
        protection.acquire(provider)
        protection.acquire(external)
        monkeypatch.setattr(runtime.harnesses, "_finish_runtime_effect", lambda *args, **kwargs: pytest.fail("local closure cannot finish effects"))
        runtime.target_root_lifecycle.request_cancel(handle.target_ref, reason="stop unknown original work")
        assert runtime.target_run_runtime.process_once(handle.target_ref) is True
        failed = runtime.target_root_lifecycle.query(handle.target_ref)
        assert failed.status == "failed" and failed.cancelled_at is None
        original = owner.latest_operation(handle.target_run_ref)
        assert original.status == "unknown_outcome"
        profile = owner.query_profile(handle.target_run_ref)
        assert (REBOOT_REASON in profile["terminal_proof_diagnostic"]["reasons"]) is reboot_before_stop
        run = owner.query_run_by_ref(handle.target_run_ref)
        if not reboot_before_stop:
            assert not (directory / "supervisor-recovery.json").exists()
            with pytest.raises(HarnessAdmissionError, match="provider_terminal_proof_missing"):
                runtime.harnesses.reconcile_failed_target_operation(run.request_ref)
            current = supervisor_host_identity()
            changed = "2" * 64 if current["boot_identity_hash"] != "2" * 64 else "3" * 64
            monkeypatch.setattr("meta_research.streaming_provider_supervisor.supervisor_host_identity", lambda: {
                **current, "boot_identity_hash": changed,
            })
            assert not (directory / "supervisor-recovery.json").exists()
        assert runtime.target_run_runtime.process_once(handle.target_ref) is False
        assert read_transport_envelope(directory / "supervisor-recovery.json", key)["outcome"] == "unknown"
        status = runtime.target_run_runtime.query_status(handle.target_ref)
        assert (status.phase, status.pending_code) == ("failed", "provider_terminal_proof_missing")
        assert owner.latest_operation(handle.target_run_ref) == original
        assert owner.query_run_by_ref(handle.target_run_ref).status == run.status
        assert owner.query_profile(handle.target_run_ref)["terminal_proof_diagnostic"] == {
            "provider_operation_ref": OPERATION,
            "reasons": ["provider_terminal_proof_missing", REBOOT_REASON],
        }
        with runtime._database.read() as connection:
            before = connection.execute(text("SELECT * FROM ar_harness_runs WHERE run_ref=:ref"), {"ref": handle.target_run_ref}).mappings().one()
            effects = dict(connection.execute(text("SELECT responsibility_ref,status FROM ar_execution_responsibilities "
                "WHERE responsibility_ref IN (:provider,:external)"), {"provider": provider.responsibility_ref, "external": external.responsibility_ref}).all())
            assert effects[provider.responsibility_ref] != "finished"
            assert effects[external.responsibility_ref] != "finished"
            cycle = connection.execute(text("SELECT s.cycle_ref FROM ar_stage_runs s JOIN ar_bundle_inbox_entries e "
                "ON e.run_ref=s.run_ref JOIN ar_target_work_notices n ON n.notice_ref=e.notice_ref "
                "WHERE n.target_ref=:ref"), {"ref": handle.target_ref}).scalar_one()
            assert cycle in _cycle_dependencies(connection, (cycle,))
        assert runtime.target_run_runtime.process_once(handle.target_ref) is False
        with runtime._database.read() as connection:
            after = connection.execute(text("SELECT * FROM ar_harness_runs WHERE run_ref=:ref"), {"ref": handle.target_run_ref}).mappings().one()
            assert dict(after) == dict(before)
        assert runtime.target_root_lifecycle.query(handle.target_ref) == failed
        assert not (directory / "supervisor-exit.json").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("condition", [None, "held_lock", "same_boot", "wrong_operation", "wrong_family", "request_tamper", "request_mismatch", "invalid_exit"])
def test_transport_records_reboot_only_for_verified_original_scope(tmp_path, condition):
    from meta_research.provider_supervisor import SupervisorFileLock

    transport, key, directory, binding = _seed(tmp_path / "transport", old_boot=condition != "same_boot")
    request = directory / "supervisor-request.json"
    lock = SupervisorFileLock(directory / "supervisor.lock")
    if condition == "held_lock":
        assert lock.acquire(blocking=False)
    if condition == "wrong_operation":
        _rewrite(binding, key, provider_operation_ref="foreign-operation")
    elif condition == "request_tamper":
        value = json.loads(request.read_text())
        value["payload"]["invocation_hash"] = "b" * 64
        request.write_text(json.dumps(value), encoding="utf-8")
    elif condition == "request_mismatch":
        _rewrite(request, key, invocation_hash="b" * 64)
    elif condition == "invalid_exit":
        (directory / "supervisor-exit.json").write_text("{}", encoding="utf-8")
    try:
        if condition in {"wrong_operation", "wrong_family"}:
            with pytest.raises(OSError, match="invalid provider operation binding"):
                transport.recover_transport_receipt(OPERATION, family="claude" if condition == "wrong_family" else "codex",
                                                    owner_invocation_hash=OWNER_HASH)
        else:
            assert transport.recover_transport_receipt(OPERATION, family="codex", owner_invocation_hash=OWNER_HASH) is None
        recovery_path = directory / "supervisor-recovery.json"
        assert recovery_path.exists() is (condition is None)
        if condition is None:
            recovery = read_transport_envelope(recovery_path, key)
            assert recovery["outcome"] == "unknown" and "returncode" not in recovery
            assert recovery["process_tree"]["mechanism"] == "host-reboot"
        assert (directory / "supervisor-exit.json").exists() is (condition == "invalid_exit")
        if condition == "invalid_exit":
            assert (directory / "supervisor-exit.json").read_text() == "{}"
    finally:
        lock.release()


@pytest.mark.parametrize("scope", ["operation", "shard", "provider_root", "request", "ready", "binding_root"])
def test_transport_reboot_writer_rejects_symlinked_scope(tmp_path, scope):
    workspace = tmp_path / "transport"
    transport, key, directory, binding = _seed(workspace)
    source = {
        "operation": directory, "shard": directory.parent, "provider_root": directory.parent.parent,
        "request": directory / "supervisor-request.json", "ready": directory / "supervisor-ready.json",
        "binding_root": binding.parent.parent,
    }[scope]
    outside = tmp_path / ("outside-" + scope)
    source.rename(outside)
    source.symlink_to(outside, target_is_directory=outside.is_dir())
    if scope == "binding_root":
        with pytest.raises(OSError, match="invalid provider operation binding"):
            transport.recover_transport_receipt(OPERATION, family="codex", owner_invocation_hash=OWNER_HASH)
    else:
        assert transport.recover_transport_receipt(OPERATION, family="codex", owner_invocation_hash=OWNER_HASH) is None
    assert not (directory / "supervisor-recovery.json").exists()
    if outside.is_dir():
        assert not list(outside.rglob("supervisor-recovery.json"))
