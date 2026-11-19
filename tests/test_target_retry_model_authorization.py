"""A human-confirmed model change applies to exactly one failed Target successor."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
from pathlib import Path
import subprocess
from threading import Barrier

import pytest
from sqlalchemy import text

from meta_research.database import Database
from meta_research.feed import DurableFeed
from meta_research.harness import HarnessAdmissionError
from meta_research.harness_adapters import CodexHarnessAdapter, HarnessAdapterUnavailable, HarnessSupervisorTransport
from meta_research.native_guidance import live_native_argv, sealed_native_cli_version
from meta_research.owners.agent_runtime_harness import AgentRuntimeHarnessError, SQLiteAgentRuntimeHarness
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import ensure_transport_key, read_transport_envelope
from meta_research.runtime_conditions import read_runtime_conditions, save_runtime_conditions
from meta_research.target_recovery_model import CAPABILITY, SCOPE_SCHEMA, authorization_scope_ref
from test_harness_target_root_recovery import _receipt_store, _record_proven_failure
from test_harness_terminal_replay import _StoppedProvider
from test_harness_full_conformance import _FullConformanceAdapter
from test_public_human_collaboration_ladder import _confirm_capability_command
from test_target_root_finalizer import _admit_independent_target_root, _current_bundle_runtime
from test_target_launch_admission import _ready_launch


OLD_MODEL = "gpt-6.1-sol"
NEW_MODEL = "gpt-6.2-sol"
INVALID = "target_recovery_model_authorization_invalid"
OLD_RUNTIME = {"cli_version": "0.159.0", "harness_adapter_ref": "test-managed-codex-sha256:" + "1" * 64}
NEW_RUNTIME = {"cli_version": "0.169.0", "harness_adapter_ref": "test-managed-codex-sha256:" + "2" * 64}


@dataclass
class FailedTarget:
    runtime: object
    owner: SQLiteAgentRuntimeHarness
    run: object
    predecessor: object
    scope: dict
    predecessor_spool: Path
    active_runtime: dict

    def original_files(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.predecessor_spool.iterdir() if path.is_file()}

    def decision(self, outcome, key, *, scope=None):
        scope = self.scope if scope is None else scope
        ref = authorization_scope_ref(self.predecessor.operation_ref)
        human = self.runtime.owners.human_collaboration
        confirmed = _confirm_capability_command(human, scope_ref=ref, capability=CAPABILITY,
            decision=outcome, capability_scope=scope, key=key)
        return human.decide_capability_authorization(ref, {
            "capability": CAPABILITY, "decision": outcome, "scope": scope,
            "confirmation_receipt_ref": confirmed["confirmation_receipt"]["receipt_ref"],
        }, key + "-authorization")

    def start(self, binding, *, model=NEW_MODEL, invocation_hash=None, material_changes=None):
        operation_ref = self.run.run_ref + ":harness_turn:2"
        prompt = "Continue the same Target."
        material = {"schema_ref": "meta-research/harness-invocation/v1", "run_ref": self.run.run_ref,
            "attempt_ref": self.run.attempt_ref, "fence_ref": self.run.fence_ref, "operation_ref": operation_ref,
            "generation": 2, "resume": False, "model_ref": model, "prompt_hash": canonical_hash(prompt)}
        if binding is not None:
            material["recovery_model_binding"] = binding
        material.update(material_changes or {})
        self.owner.start_operation(
            run_ref=self.run.run_ref, operation_ref=operation_ref, generation=2, resume=False,
            invocation_hash=invocation_hash or canonical_hash(material),
            model_ref=model, frozen_prompt=prompt, recovery_model_binding=binding,
            **({"invocation_material": material} if binding is not None else {}),
        )


@pytest.fixture()
def failed(tmp_path, monkeypatch):
    monkeypatch.setattr("meta_research.owners.agent_runtime_harness._TARGET_ROOT_RETRY_BASE_SECONDS", 0.0)
    runtime = _current_bundle_runtime(tmp_path / "research")
    try:
        ready = _ready_launch(runtime)
        graph = ready[0]
        target, candidate, plan, admission, handle = _admit_independent_target_root(runtime, ready=ready)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        runtime.target_root_lifecycle.activate(launch_ref=launch.launch_ref, handle=handle,
            candidate=candidate, formal_plan=plan, idempotency_key="activate-target-retry-authorization")
        owner = runtime.owners.agent_runtime.harness_runs
        run = admission.run
        predecessor_ref = run.run_ref + ":harness_turn:1"
        owner.start_operation(run_ref=run.run_ref, operation_ref=predecessor_ref, generation=1,
            invocation_hash=canonical_hash({"test": "predecessor", "model": OLD_MODEL}), resume=False,
            model_ref=OLD_MODEL, frozen_prompt="Original target input.")
        _record_proven_failure(owner, predecessor_ref, "provider_process_failed")
        predecessor = owner.latest_operation(run.run_ref)
        assert predecessor.status == "failed"
        active_runtime = dict(NEW_RUNTIME)
        def observed_runtime(*, operation_ref=None, owner_invocation_hash=None):
            if operation_ref is None and owner_invocation_hash is None:
                return dict(active_runtime)
            assert (operation_ref, owner_invocation_hash) == (predecessor_ref, predecessor.invocation_hash)
            return dict(OLD_RUNTIME)
        owner.bind_target_recovery_runtime_identity(observed_runtime)
        conditions = read_runtime_conditions(runtime.data_root.root, graph.quest_ref, database=runtime._database)
        changed = save_runtime_conditions(runtime.data_root.root, graph.quest_ref, text=conditions["text"],
            expected_revision=conditions["revision"], database=runtime._database,
            stage_models={**conditions["stage_models"], "target": NEW_MODEL})
        rebound = owner.reopen_failed_target_root(run.request_ref)
        assert rebound.reopened
        scope = {
            "schema_ref": SCOPE_SCHEMA, "quest_ref": graph.quest_ref, "cycle_ref": graph.cycle_ref,
            "target_ref": target.target_ref, "run_ref": run.run_ref, "attempt_ref": run.attempt_ref,
            "fence_ref": run.fence_ref, "predecessor_operation_ref": predecessor_ref,
            "predecessor_generation": 1, "predecessor_invocation_hash": predecessor.invocation_hash,
            "successor_generation": 2, "old_model_ref": OLD_MODEL, "new_model_ref": NEW_MODEL,
            "predecessor_runtime": OLD_RUNTIME, "successor_runtime": NEW_RUNTIME,
            "conditions_revision": changed["revision"],
        }
        digest = canonical_hash({"operation_ref": predecessor_ref, "invocation_hash": predecessor.invocation_hash})
        spool = _receipt_store(owner)._workspace / "provider-operations" / digest[:2] / digest
        yield FailedTarget(runtime, owner, run, predecessor, scope, spool, active_runtime)
    finally:
        runtime.close()


def test_changed_conditions_without_grant_preserve_the_failed_model(failed):
    assert failed.owner.resolve_target_retry_model(failed.run.run_ref, 2) is None
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(None)
    failed.start(None, model=OLD_MODEL)
    successor = failed.owner.latest_operation(failed.run.run_ref)
    assert successor.generation == 2 and successor.model_ref == OLD_MODEL
    assert failed.owner.sealed_target_retry_model(successor.operation_ref) is None


def test_confirmed_but_wrong_scope_cannot_change_model(failed):
    for field, wrong in [
        ("fence_ref", "stale-fence"), ("successor_generation", 3), ("predecessor_generation", 2),
        ("predecessor_invocation_hash", "e" * 64), ("conditions_revision", "outdated"),
        ("predecessor_runtime", NEW_RUNTIME), ("successor_runtime", OLD_RUNTIME),
    ]:
        failed.decision("granted", "invalid-" + field, scope={**failed.scope, field: wrong})
        with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
            failed.owner.resolve_target_retry_model(failed.run.run_ref, 2)
    assert failed.owner.latest_operation(failed.run.run_ref) == failed.predecessor


def test_revocation_between_resolve_and_start_is_effective(failed):
    failed.decision("granted", "grant")
    binding = failed.owner.resolve_target_retry_model(failed.run.run_ref, 2)
    unbound = SQLiteAgentRuntimeHarness(failed.runtime._database, DurableFeed(failed.runtime._database))
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        unbound.resolve_target_retry_model(failed.run.run_ref, 2)
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.owner.bind_target_recovery_runtime_identity(lambda **kwargs: OLD_RUNTIME)
    failed.active_runtime.update(OLD_RUNTIME)
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(binding)
    failed.active_runtime.update(NEW_RUNTIME)
    failed.decision("revoked", "revoke")
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(binding)
    assert failed.owner.latest_operation(failed.run.run_ref) == failed.predecessor


def test_missing_predecessor_exit_receipt_rejects_an_otherwise_current_grant(failed):
    failed.decision("granted", "grant")
    receipt = failed.predecessor_spool / "supervisor-exit.json"
    assert receipt.is_file()
    receipt.unlink()
    with pytest.raises(AgentRuntimeHarnessError, match="provider_terminal_proof_missing"):
        failed.owner.resolve_target_retry_model(failed.run.run_ref, 2)
    assert failed.owner.latest_operation(failed.run.run_ref) == failed.predecessor


def test_authorized_harness_entry_transmits_successor_model_once(failed, monkeypatch):
    selected_runtimes = []
    invocations = []
    class InspectExecution(_FullConformanceAdapter):
        def recovery_runtime_identity(self, **kwargs):
            return OLD_RUNTIME if kwargs else NEW_RUNTIME

        def for_recovery_runtime(self, identity):
            selected_runtimes.append(identity)
            return self

        def invoke(self, invocation):
            invocations.append(invocation)
            raise HarnessAdapterUnavailable("provider_io_unavailable", durable_outcome="unknown")

    failed.decision("granted", "grant-harness")
    failed.runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    monkeypatch.setitem(failed.runtime.harnesses._adapters, "codex", InspectExecution("codex"))
    with pytest.raises(HarnessAdmissionError, match="provider_io_unavailable"):
        failed.runtime.harnesses.run_or_resume_target_root(failed.run.request_ref,
            prompt="Continue the same Target.", mcp_base_url="http://127.0.0.1:8999")
    assert selected_runtimes == [NEW_RUNTIME]
    invocation, = invocations
    successor = failed.owner.latest_operation(failed.run.run_ref)
    assert (invocation.model_ref, successor.model_ref) == (NEW_MODEL, NEW_MODEL)
    assert (successor.generation, successor.status) == (2, "unknown_outcome")
    assert invocation.owner_invocation_hash == successor.invocation_hash
    assert failed.owner.sealed_target_retry_model(successor.operation_ref)["scope"] == failed.scope


def test_version_and_executable_identity_must_both_match_when_reselecting_adapter(tmp_path, monkeypatch):
    versions = {"0.159.0": tmp_path / "old" / "codex", "0.169.0": tmp_path / "new" / "codex"}
    for version, path in versions.items():
        path.parent.mkdir()
        path.write_text("test CLI content " + version)
        path.chmod(0o700)
    class VersionedProcess(_StoppedProvider):
        def __call__(self, argv, prompt, timeout, environment):
            assert argv[1:] == ["--version"]
            version, = [key for key, path in versions.items() if str(path) == argv[0]]
            return subprocess.CompletedProcess(argv, 0, "codex-cli " + version, "")
    runner = VersionedProcess()
    transport = HarnessSupervisorTransport(tmp_path / "spool", process_runner=runner)
    ref = "target-run:cross-version:harness_turn:1"
    owner_hash = canonical_hash({"operation": ref, "model": OLD_MODEL})
    env = {"META_RESEARCH_HARNESS_FAMILY": "codex", "META_RESEARCH_PROVIDER_OPERATION_REF": ref,
           "META_RESEARCH_OWNER_INVOCATION_HASH": owner_hash, "META_RESEARCH_MCP_TOKEN": "test",
           "META_RESEARCH_HARNESS_WORKSPACE": str(tmp_path), "META_RESEARCH_HARNESS_EVIDENCE_SCOPE_REF": "test",
           "META_RESEARCH_HARNESS_OBSERVATION_SCOPE": "test", "NO_PROXY": "", "no_proxy": ""}
    argv = [str(versions["0.159.0"]), "exec", "--model", OLD_MODEL, "--json", "-"]
    prompt = "Continue the historical Target."
    with monkeypatch.context() as old_release:
        def legacy_wrapper(native_argv, directory, **options):
            return live_native_argv(native_argv, directory, force_transport=True, cli_version="0.159.0")
        old_release.setattr("meta_research.harness_adapters.live_native_argv", legacy_wrapper)
        old_release.setattr("meta_research.harness_adapters.sys.executable", "/old-release/venv/bin/python")
        assert transport(argv, prompt, None, env).returncode == 143
    exit_path = runner.request_path.with_name("supervisor-exit.json")
    wrapper_path = exit_path.with_name("provider-argv.json")
    native_path = exit_path.with_name("native-provider-argv.json")
    original_wrapper = wrapper_path.read_bytes()
    assert b"/old-release/venv/bin/python" in original_wrapper
    original_native = native_path.read_bytes()
    original_exit = exit_path.read_bytes()
    adapter = CodexHarnessAdapter(tmp_path / "workspace", executable=str(versions["0.169.0"]),
        managed_executable_for_version=versions.__getitem__, runner=transport)
    adapter.locked_version = "0.169.0"
    previous = adapter.recovery_runtime_identity(operation_ref=ref, owner_invocation_hash=owner_hash)
    assert previous["cli_version"] == "0.159.0"
    assert adapter.for_recovery_runtime(previous).executable == str(versions["0.159.0"])
    assert transport.replay_terminal(argv, prompt, None, env).returncode == 143
    assert runner.calls == 1 and exit_path.read_bytes() == original_exit
    assert wrapper_path.read_bytes() == original_wrapper and native_path.read_bytes() == original_native
    current = adapter.recovery_runtime_identity()
    assert current["cli_version"] == "0.169.0"
    assert adapter.for_recovery_runtime(current).executable == str(versions["0.169.0"])
    with pytest.raises(HarnessAdapterUnavailable, match="provider_runtime_binding_mismatch"):
        adapter.for_recovery_runtime({**current, "cli_version": "0.159.0"})
    versions["0.169.0"].write_text("drifted test CLI content")
    with pytest.raises(HarnessAdapterUnavailable, match="provider_runtime_binding_mismatch"):
        adapter.for_recovery_runtime(current)


@pytest.mark.parametrize(("physical_version", "other_version"), [
    ("0.159.0", "0.169.0"), ("0.169.0", "0.159.0"),
])
def test_new_physical_launch_seals_its_selected_version_under_new_deployment(tmp_path, monkeypatch, physical_version,
                                                                          other_version):
    monkeypatch.setattr("meta_research.native_guidance.CODEX_LOCKED_VERSION", "0.169.0")
    _, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "aa" / ("a" * 64)
    argv = [str(tmp_path / "managed" / "codex"), "exec", "--json", "-"]
    live_native_argv(argv, directory, force_transport=True, cli_version=physical_version)
    path = directory / "native-provider-argv.json"
    signed = path.read_bytes()
    document = read_transport_envelope(path, key)
    assert document["argv"] == argv
    assert sealed_native_cli_version(document) == physical_version
    if physical_version == "0.159.0":
        assert set(document) == {"argv"}
    else:
        assert document["cli_version"] == physical_version
    live_native_argv(argv, directory, force_transport=True, cli_version=physical_version)
    assert path.read_bytes() == signed
    with pytest.raises((OSError, ValueError), match="native_provider_identity_conflict"):
        live_native_argv(argv, directory, force_transport=True, cli_version=other_version)
    assert path.read_bytes() == signed


def test_confirmed_successor_is_sealed_once_across_competition_and_restart(failed):
    original_files = failed.original_files()
    grant = failed.decision("granted", "grant")
    assert failed.decision("granted", "grant") == grant
    binding = failed.owner.resolve_target_retry_model(failed.run.run_ref, 2)
    assert binding["scope"] == failed.scope
    assert binding["authorization_receipt_ref"] == grant["receipt_ref"]
    for key in ("predecessor_supervisor_receipt_hash", "authorization_receipt_hash", "confirmation_receipt_hash"):
        assert len(binding[key]) == 64
    original = failed.run.run_ref + ":harness_turn:1"
    successor = failed.run.run_ref + ":harness_turn:2"
    wrong = {**binding, "predecessor_supervisor_receipt_hash": "0" * 64}
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(wrong)
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(binding, invocation_hash=failed.predecessor.invocation_hash)
    with pytest.raises(AgentRuntimeHarnessError, match=INVALID):
        failed.start(binding, material_changes={"fence_ref": "wrong-fence"})

    barrier = Barrier(2)
    def compete():
        barrier.wait(timeout=10)
        try:
            failed.start(binding)
        except AgentRuntimeHarnessError as exc:
            return exc.code
        return "admitted"
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete) for _ in range(2)]
        results = [item.result(timeout=20) for item in futures]
    assert "admitted" in results

    failed.decision("revoked", "revoke-after-start")
    db = Database(failed.runtime.data_root.database)
    try:
        owner = SQLiteAgentRuntimeHarness(db, DurableFeed(db))
        persisted = owner.latest_operation(failed.run.run_ref)
        assert (persisted.operation_ref, persisted.generation, persisted.model_ref) == (successor, 2, NEW_MODEL)
        assert owner.sealed_target_retry_model(original) is None
        assert owner.sealed_target_retry_model(successor) == binding
        with db.read() as conn:
            history = conn.execute(text("SELECT operation_ref,model_ref,invocation_hash,status "
                "FROM ar_harness_provider_operations WHERE run_ref=:ref ORDER BY generation"),
                {"ref": failed.run.run_ref}).all()
            events = conn.execute(text("SELECT count(*) FROM durable_feed WHERE "
                "event_type='agent_runtime.harness_operation_started' AND "
                "json_extract(payload_json,'$.operation_ref')=:ref"), {"ref": successor}).scalar_one()
        assert len(history) == 2 and events == 1
        assert tuple(history[0]) == (original, OLD_MODEL, failed.predecessor.invocation_hash, "failed")
        assert failed.original_files() == original_files
    finally:
        db.close()
