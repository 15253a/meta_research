"""Issue #296 stable TargetRun workspace integration contract.

The provider process seam below returns signed terminal transport fixtures. It
does not prove Linux process ownership; test_provider_process_tree supplies that
separate proof. All Harness, Owner, recovery CAS and workspace calls are real.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import text

from meta_research.harness import HarnessAdmissionError
from meta_research.harness_adapters import CODEX_LOCKED_VERSION, CodexHarnessAdapter
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    ensure_transport_key,
    supervisor_host_identity,
    write_exit_receipt,
    write_transport_envelope,
)
from test_harness_full_conformance import _FullConformanceAdapter
from test_target_root_finalizer import (
    _CurrentBindingBundleSkill,
    _admit_independent_target_root,
    _current_bundle_runtime,
)
from test_public_bundle_stage import _ParallelTwoTargetBundleSkill


class _SignedCeilingRunner:
    """Control native transport and verify the expected execution policy."""

    def __init__(self, *, expected_sandbox="workspace-write") -> None:
        self.runtime = None
        self.expected_sandbox = expected_sandbox
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.pause_run_ref = None
        self.pause_operation_ref = None
        self.started = Event()
        self.release = Event()

    def __call__(self, argv, prompt, timeout, environment):
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, CODEX_LOCKED_VERSION, "")
        if argv[-2:] == ["features", "list"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert self.runtime is not None
        self.calls.append((list(argv), dict(environment)))
        assert argv[argv.index("--sandbox") + 1] == self.expected_sandbox
        # Perform real, bounded local work at the adapter's exact cwd. This is
        # checkpoint continuation evidence, not a native-provider tree fixture.
        subprocess.run(
            [sys.executable, "-c", (
                "import json,pathlib; p=pathlib.Path('issue296-checkpoint.json'); "
                "s=json.loads(p.read_text()) if p.exists() else {'segments':0,'sum':0}; "
                "start=s['segments']*10000; "
                "s['sum']+=sum(i*i for i in range(start,start+10000)); "
                "s['segments']+=1; p.write_text(json.dumps(s))"
            )],
            cwd=environment["META_RESEARCH_HARNESS_WORKSPACE"],
            check=True,
            timeout=10,
            capture_output=True,
            text=True,
        )
        if environment["META_RESEARCH_PROVIDER_OPERATION_REF"] == self.pause_operation_ref:
            self.started.set()
            assert self.release.wait(60), "parallel Target was not released by test"
        returncode, reason, stream = self._terminal(environment)
        owner = self.runtime.owners.agent_runtime.harness_runs
        store = owner._target_provider_ceiling_receipt_verifier
        operation_ref = environment["META_RESEARCH_PROVIDER_OPERATION_REF"]
        owner_hash = environment["META_RESEARCH_OWNER_INVOCATION_HASH"]
        digest = canonical_hash({"operation_ref": operation_ref, "invocation_hash": owner_hash})
        directory = store._workspace / "provider-operations" / digest[:2] / digest
        directory.mkdir(parents=True)
        (directory / "prompt.txt").write_text(prompt)
        (directory / "output-schema.json").write_text("{}")
        (directory / "stdout.jsonl").write_text(stream)
        _, key = ensure_transport_key(store._workspace)
        write_exit_receipt(
            directory / "supervisor-exit.json",
            key=key,
            invocation_hash=digest,
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
            returncode=returncode,
            input_bytes=len(prompt.encode()),
            termination_reason=reason,
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            process_tree={
                "schema_ref": "meta-research/provider-process-tree/v1",
                "state": "closed",
                "mechanism": "linux-subreaper-echild",
                **supervisor_host_identity(),
            },
        )
        binding = store._workspace / "owner-invocation-bindings" / owner_hash[:2] / (owner_hash + ".json")
        binding.parent.mkdir(parents=True, exist_ok=True)
        write_transport_envelope(binding, {
            "schema_ref": "meta-research/harness-operation-binding/v1",
            "owner_invocation_hash": owner_hash,
            "provider_operation_ref": operation_ref,
            "family": "codex",
            "transport_invocation_hash": digest,
        }, key)
        store.bind_operation(operation_ref, digest, family="codex")
        receipt = {
            "schema_ref": "meta-research/harness-provider-transport-receipt/v1",
            "spool_ref": "provider-spool:" + digest,
            "transport_invocation_hash": digest,
            "termination_reason": reason,
            "provider_returncode": returncode,
            "supervisor_receipt_hash": canonical_hash(
                json.loads((directory / "supervisor-exit.json").read_text())
            ),
        }
        completed = subprocess.CompletedProcess(argv, returncode, stream, "fixture ceiling" if returncode else "")
        completed.meta_research_transport_receipt = receipt
        return completed

    def _terminal(self, environment):
        stream = json.dumps(
            {"type": "thread.started", "thread_id": f"issue296-native-{len(self.calls)}"}
        ) + "\n"
        return 1, "descendant_process", stream


class _RecoveryAdapter(_FullConformanceAdapter):
    def __init__(self, root: Path, runner: _SignedCeilingRunner) -> None:
        super().__init__("codex")
        self.runner = runner
        self.target = CodexHarnessAdapter(root, runner=runner)

    def invoke(self, invocation):
        if invocation.root_kind == "target":
            if invocation.run_ref == self.runner.pause_run_ref:
                self.runner.pause_operation_ref = invocation.provider_operation_ref
            return self.target.invoke(invocation)
        return super().invoke(invocation)


class _ParallelBundle(_CurrentBindingBundleSkill, _ParallelTwoTargetBundleSkill):
    pass


def _runtime(path: Path, runner: _SignedCeilingRunner, *, parallel=False):
    runtime = _current_bundle_runtime(
        path,
        bundle_skill=_ParallelBundle() if parallel else None,
        harness_adapters=(
            _RecoveryAdapter(path.with_name(path.name + "-native"), runner),
        ),
    )
    runner.runtime = runtime
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    return runtime


def _activate(runtime, admitted, key):
    target, candidate, plan, _admission, handle = admitted
    launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
    runtime.target_root_lifecycle.activate(
        launch_ref=launch.launch_ref,
        handle=handle,
        candidate=candidate,
        formal_plan=plan,
        idempotency_key=key,
    )


def _second_target(runtime, first_target_ref):
    for _ in range(12):
        runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        run = runtime.owners.agent_runtime.query_bundle_stage_run(
            current["stage_run_request"]["request_ref"]
        )
        decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
        decision = next((item for item in decisions if item.selected_target_ref != first_target_ref), None)
        if decision is not None:
            graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
            target = next(item for item in graph.targets if item.target_ref == decision.selected_target_ref)
            if runtime.owners.research_graph.query_target_candidate_projection(target_ref=target.target_ref) is not None:
                return _admit_independent_target_root(
                    runtime, ready=(graph, target, run, decision, None), key_prefix="issue296-second-"
                )
    raise AssertionError("Second independent Target did not become dispatchable")


def _workspace(runtime, handle):
    return runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
        target_ref=handle.target_ref,
        target_run_ref=handle.target_run_ref,
        root_session_ref=handle.root_session_ref,
        attempt_ref=handle.execution_attempt_ref,
        fence_ref=handle.execution_fence_ref,
    )


def _exercise_two_recoveries(
    tmp_path: Path, runner: _SignedCeilingRunner, interrupt_after_reservation: bool,
    *, run_after_second_recovery: bool = False,
) -> None:
    """Shared real Owner exercise for controlled and actual native runners."""
    path = tmp_path / "issue296-stable-workspace"
    runtime = _runtime(path, runner)
    try:
        target, candidate, plan, admission, handle = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref,
            handle=handle,
            candidate=candidate,
            formal_plan=plan,
            idempotency_key="issue296-activate",
        )
        workspace_ref, cwd = _workspace(runtime, handle)
        work_file = cwd / "issue296-live-work.bin"
        work_file.write_bytes(b"issue296-generation-0\x00")
        original_inode = work_file.stat().st_ino
        original = cwd / "issue296-formal-original.csv"
        original.write_bytes(b"sample,value\none,296\n")
        asset = runtime.owners.research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path", custody_mode="linked_local",
                display_name=original.name, source_locator=str(original),
            ), idempotency_key="issue296-linked-original",
        ).asset
        assert asset is not None
        handles = [handle]
        channels = [admission.connection]
        observed = []
        for generation in (1, 2):
            with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
                runtime.harnesses.run_or_resume_target_root(
                    admission.run.request_ref,
                    prompt=f"issue296 recovery {generation}",
                    mcp_base_url="http://127.0.0.1:8999",
                )
            old = handles[-1]
            successor = runtime.harnesses.recover_fenced_target_root(
                admission.run.request_ref, old_handle=old
            )
            assert successor.run.attempt_ref != old.execution_attempt_ref
            assert successor.run.fence_ref != old.execution_fence_ref
            assert successor.run.root_session_ref != old.root_session_ref
            assert successor.connection.grant_ref != channels[-1].grant_ref
            assert successor.connection.token != channels[-1].token
            denied, _, _ = runtime.harnesses.dispatch_mcp_http(
                channels[-1].token,
                {"jsonrpc": "2.0", "id": generation, "method": "tools/list", "params": {}},
                mcp_session_id=None,
            )
            assert denied == 401
            channels.append(successor.connection)
            if interrupt_after_reservation:
                runtime.close()
                runtime = _runtime(path, runner)
            # These are the actual daemon stages: new input authority then the
            # lifecycle CAS. No recovery/history rows are invented by this test.
            assert runtime.target_run_runtime.process_once(target.target_ref)
            assert runtime.target_run_runtime.process_once(target.target_ref)
            handle = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(
                target.target_ref
            )
            assert handle is not None and handle != old
            runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
                handle=handle,
                idempotency_key=f"issue296-workspace-replay-{generation}",
            )
            current_ref, current_cwd = _workspace(runtime, handle)
            observed.append(
                (current_ref, current_cwd, (current_cwd / work_file.name).stat().st_ino)
            )
            assert (current_cwd / work_file.name).read_bytes() == work_file.read_bytes()
            assert original.read_bytes() == b"sample,value\none,296\n"
            assert runtime.owners.research_memory.materialize_asset(asset.version_ref).content == original.read_bytes()
            edited = current_cwd / "issue296-editable-analysis.csv"
            edited.write_bytes(b"sample,value\none,297\n")
            assert runtime.owners.research_memory.materialize_asset(asset.version_ref).content != edited.read_bytes()
            with pytest.raises(OwnerConflict):
                _workspace(runtime, old)
            handles.append(handle)
            # Change the working bytes between recoveries; they are not accepted
            # inputs and may not be reset from an old snapshot on the next CAS.
            (current_cwd / work_file.name).write_bytes(
                f"issue296-generation-{generation}\x00".encode()
            )
            work_file = current_cwd / work_file.name

        assert len({item.execution_attempt_ref for item in handles}) == 3
        assert len(runner.calls) == 2
        assert json.loads((work_file.parent / "issue296-checkpoint.json").read_text()) == {
            "segments": 2,
            "sum": sum(i * i for i in range(20000)),
        }
        operation_refs = [call[1]["META_RESEARCH_PROVIDER_OPERATION_REF"] for call in runner.calls]
        assert len(set(operation_refs)) == 2
        operations = runtime.owners.agent_runtime.harness_runs.query_status_records()[1]
        recorded = [item for item in operations if item.operation_ref in operation_refs]
        assert len(recorded) == 2
        assert all(item.status == "failed" and item.outcome_code == "provider_descendant_process"
                   for item in recorded)
        if run_after_second_recovery:
            with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
                runtime.harnesses.run_or_resume_target_root(
                    admission.run.request_ref,
                    prompt="issue296 work after second recovery",
                    mcp_base_url="http://127.0.0.1:8999",
                )
            assert len(runner.calls) == 3
            assert json.loads((cwd / "issue296-checkpoint.json").read_text()) == {
                "segments": 3,
                "sum": sum(i * i for i in range(30000)),
            }
        original.write_bytes(b"sample,value\none,999\n")
        with pytest.raises(OwnerConflict, match="asset_custody_unavailable"):
            runtime.owners.research_memory.materialize_asset(asset.version_ref)
        assert observed == [(workspace_ref, cwd, original_inode)] * 2
        with runtime._database.read() as connection:
            workspace_count = connection.execute(
                text("SELECT COUNT(*) FROM ar_target_run_workspaces WHERE target_run_ref=:ref"),
                {"ref": handle.target_run_ref},
            ).scalar_one()
        assert workspace_count == 1
    finally:
        runtime.close()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux acceptance")
@pytest.mark.parametrize("interrupt_after_reservation", (False, True))
def test_two_real_recoveries_keep_workspace_and_rotate_execution_authority(
    tmp_path: Path, interrupt_after_reservation: bool
) -> None:
    _exercise_two_recoveries(tmp_path, _SignedCeilingRunner(), interrupt_after_reservation)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux acceptance")
def test_recovering_target_keeps_parallel_target_operation_and_work_untouched(tmp_path):
    runner = _SignedCeilingRunner()
    runtime = _runtime(tmp_path / "issue296-parallel", runner, parallel=True)
    try:
        first = _admit_independent_target_root(runtime)
        _activate(runtime, first, "issue296-activate-a")
        second = _second_target(runtime, first[0].target_ref)
        _activate(runtime, second, "issue296-activate-b")
        first_handle, second_handle = first[-1], second[-1]
        first_ref, first_cwd = _workspace(runtime, first_handle)
        second_ref, second_cwd = _workspace(runtime, second_handle)
        assert first_handle.target_run_ref != second_handle.target_run_ref
        assert first_cwd != second_cwd
        b_work = second_cwd / "issue296-parallel-original.bin"
        b_work.write_bytes(b"B's working bytes survive A recovery\x00")
        b_stat = b_work.stat()
        runner.pause_run_ref = second_handle.target_run_ref
        first_locations = []

        def execute_b():
            with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
                runtime.harnesses.run_or_resume_target_root(
                    second[3].run.request_ref,
                    prompt="issue296 independent B work",
                    mcp_base_url="http://127.0.0.1:8999",
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(execute_b)
            try:
                assert runner.started.wait(30)
                b_run = runtime.owners.agent_runtime.harness_runs.query_run(second[3].run.request_ref)
                b_operation = runtime.owners.agent_runtime.harness_runs.latest_operation(second_handle.target_run_ref)
                assert b_run.status == b_operation.status == "running"
                for generation in (1, 2):
                    with pytest.raises(HarnessAdmissionError, match="provider_descendant_process"):
                        runtime.harnesses.run_or_resume_target_root(
                            first[3].run.request_ref,
                            prompt=f"issue296 A recovery {generation}",
                            mcp_base_url="http://127.0.0.1:8999",
                        )
                    runtime.harnesses.recover_fenced_target_root(first[3].run.request_ref, old_handle=first_handle)
                    assert runtime.target_run_runtime.process_once(first_handle.target_ref)
                    assert runtime.target_run_runtime.process_once(first_handle.target_ref)
                    first_handle = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(first_handle.target_ref)
                    runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
                        handle=first_handle, idempotency_key=f"issue296-parallel-replay-{generation}"
                    )
                    first_locations.append(_workspace(runtime, first_handle))
                    assert _workspace(runtime, second_handle) == (second_ref, second_cwd)
                    assert b_work.read_bytes() == b"B's working bytes survive A recovery\x00"
                    assert (b_work.stat().st_dev, b_work.stat().st_ino) == (b_stat.st_dev, b_stat.st_ino)
                    assert runtime.owners.agent_runtime.harness_runs.query_run(second[3].run.request_ref) == b_run
                    assert runtime.owners.agent_runtime.harness_runs.latest_operation(second_handle.target_run_ref) == b_operation
                    assert not pending.done()
                assert first_locations == [(first_ref, first_cwd)] * 2
            finally:
                runner.release.set()
                pending.result(timeout=20)
    finally:
        runtime.close()
