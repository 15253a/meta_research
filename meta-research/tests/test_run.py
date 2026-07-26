"""CP7.3 · run.py 全系统装配入口（M6）。

核心验收面：一条命令把**真组件 + StageProvider(注入 runner)** 接成全自动元循环并跑到停机；每个注入
组件（真状态机/编译器/发布器/StopController/precheck）端到端接对：assembly→run→落库+发布；重启同
work_root 续跑（goal 不重建）；durable 停机与全局等待端到端生效。多阶段 kill-9 恢复由 advancer 层
测试覆盖（同机制），本层验装配正确性。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import types
from pathlib import Path

import pytest
import yaml
from jsonschema.exceptions import ValidationError

from orchestrator import database as db
from orchestrator.interfaces import Artifact, CallUsage
from orchestrator.instance_lease import InstanceLease
from orchestrator.execution_sandbox import (
    sandbox_environment_hash,
    sandbox_workload_environment_hash,
)
from orchestrator.process_supervisor import ExecutionSupervisor, atomic_write_receipt
from orchestrator.provider_invocation import write_provider_invocation_receipt
from orchestrator.run import (
    System, _GuardedRunner, _bundle_operator_mode_for_runtime,
    _default_stage_runner, _is_tool_free_purpose, build_system,
)
from orchestrator.runner import (
    DEFAULT_CODEX_EFFORT,
    DEFAULT_CODEX_MODEL,
    RunnerError,
    tool_free_runtime_contract,
)
from orchestrator.storage_ops import SnapshotArchive
from orchestrator.stage_provider import (
    BUNDLE_OPERATOR_SESSION_CONTRACT,
    STAGE_MAIN_SESSION_CONTRACT,
)
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)
_POLICY = yaml.safe_load((Path(SYSTEM_ROOT) / "policies" / "policy.yaml").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _bind_cli_tests_to_tmp_storage(request, monkeypatch):
    """CLI behavior tests stub only the deployment-owned external mount."""
    real_storage_tests = {
        "test_main_real_storage_success_binds_work_parent",
        "test_main_web_child_reuses_real_console_storage_marker",
        "test_main_real_storage_rejects_private_inherited_marker_without_side_effects",
        "test_main_unknown_service_uid_returns_clean_storage_error",
    }
    if request.node.name in real_storage_tests:
        return
    if (not request.node.name.startswith("test_main_")
            and request.node.name != "test_stop_reason_print_prefers_block"):
        return
    import orchestrator.run as run_module

    monkeypatch.setattr(
        run_module, "configure_process_storage",
        lambda root, *, require_external_mount, private_work_root=None: {
            "METARESEARCH_STORAGE_ROOT": str(Path(root).absolute()),
        })


def _use_budgeted_policy(monkeypatch, *, session_max=100000):
    """Enable the optional cumulative accounting guard for tests devoted to it."""
    import orchestrator.run as run_module

    policy = json.loads(json.dumps(_POLICY))
    policy["budget"]["session_max"] = session_max
    monkeypatch.setattr(
        run_module, "yaml",
        types.SimpleNamespace(safe_load=lambda _text: policy))
    return policy
RUNTIME_ENV_HASH = sandbox_environment_hash(_POLICY["execution"]["sandbox"])


def test_default_runner_isolates_both_wildidea_sessions():
    assert _is_tool_free_purpose("idea-generate-n1") is True
    assert _is_tool_free_purpose("idea-audit-n2") is True
    assert _is_tool_free_purpose("plan-review-r1-n3") is True
    assert _is_tool_free_purpose("interaction-query") is True
    assert _is_tool_free_purpose("plan-n3") is False


def test_plan_review_runner_has_clean_inline_only_context(tmp_path):
    runner = _default_stage_runner(
        tmp_path / "transcripts", "plan-review-r1-n3", work=tmp_path,
        qualification=None, execution_supervisor=object())

    assert runner.workspace_dir == tmp_path.resolve()
    assert runner.no_host_tools is True
    assert runner.tool_free is True
    assert runner.isolated_host_tools is False
    assert runner.sandbox_mode == "read-only"


def test_default_stage_runner_uses_current_local_environment_with_broad_tools(tmp_path):
    runner = _default_stage_runner(
        tmp_path / "transcripts", "bundle-t1-n1", work=tmp_path,
        qualification=None, execution_supervisor=object())
    assert runner.workspace_dir == tmp_path.resolve()
    assert runner.no_host_tools is False
    assert runner.tool_free is False
    assert runner.isolated_host_tools is False
    assert runner.sandbox_mode == "danger-full-access"
    assert runner.query_user is None
    assert runner.output_uid == os.geteuid()
    assert (runner.bundle_operator_session_contract
            == BUNDLE_OPERATOR_SESSION_CONTRACT)


def test_default_cycle_wide_bundle_main_is_owner_lifecycle_bound(tmp_path):
    bundle = _default_stage_runner(
        tmp_path / "bundle-transcripts", "bundle-main-c1-n1",
        work=tmp_path, qualification=None, execution_supervisor=object())
    plan = _default_stage_runner(
        tmp_path / "plan-transcripts", "plan-main-c1-n2",
        work=tmp_path, qualification=None, execution_supervisor=object())
    assert bundle.lifecycle_bound is True and bundle.timeout_s is None
    assert plan.lifecycle_bound is False
    assert plan.timeout_s is not None and plan.timeout_s > 0


def test_qualification_default_bundle_runner_keeps_no_host_tools_and_persistence(tmp_path):
    """Qualification persistence retains context, never host execution authority."""
    runner = _default_stage_runner(
        tmp_path / "transcripts", "bundle-c1-t1-n1", work=tmp_path,
        qualification=object(), execution_supervisor=object())

    assert runner.no_host_tools is True
    assert runner.sandbox_mode == "read-only"
    assert runner.require_stage_submission is False
    assert (runner.bundle_operator_session_contract
            == BUNDLE_OPERATOR_SESSION_CONTRACT)
    runner.bind_persistent_session(session_id=None, role="bundle_operator")
    assert runner._persistent_session_bound is True
    assert runner._persistent_session_role == "bundle_operator"


@pytest.mark.parametrize("deployment_mode", ["development", "production"])
@pytest.mark.parametrize("qualification_active", [False, True])
def test_event_bundle_operator_is_retired_in_every_runtime_tier(
        deployment_mode, qualification_active):
    assert _bundle_operator_mode_for_runtime(
        None, deployment_mode=deployment_mode,
        qualification_active=qualification_active) is False


def test_injected_bundle_operator_requires_exact_explicit_contract():
    def undeclared(_transcripts, _purpose):
        raise AssertionError("activation check must not instantiate runner")

    assert _bundle_operator_mode_for_runtime(
        undeclared, deployment_mode="development",
        qualification_active=False) is False

    def compatible(_transcripts, _purpose):
        raise AssertionError("activation check must not instantiate runner")

    compatible.bundle_operator_session_contract = BUNDLE_OPERATOR_SESSION_CONTRACT
    assert _bundle_operator_mode_for_runtime(
        compatible, deployment_mode="production",
        qualification_active=False) is False

    compatible.bundle_operator_session_contract = "unknown-v99"
    with pytest.raises(ValueError, match="持久会话合同"):
        _bundle_operator_mode_for_runtime(
            compatible, deployment_mode="development",
            qualification_active=False)


def test_qualification_keeps_stronger_no_injected_runner_boundary():
    def compatible(_transcripts, _purpose):
        raise AssertionError("qualification must reject before runner creation")

    compatible.bundle_operator_session_contract = BUNDLE_OPERATOR_SESSION_CONTRACT
    with pytest.raises(ValueError, match="qualification 禁止注入"):
        _bundle_operator_mode_for_runtime(
            compatible, deployment_mode="development",
            qualification_active=True)


def test_injected_resident_factory_gets_same_turn_clean_child_review(tmp_path):
    factory = _mock_factory([])
    factory.stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT
    system = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=factory, attack=False)
    try:
        provider = system.advancer._reasoning.__self__
        assert provider.resident_stage_sessions is True
        assert provider.inline_subagent_review is True
        assert provider.review_rounds == {
            "idea": 1, "plan": 1,
            "bundle_code": 1, "bundle_result": 1,
        }
    finally:
        system.close()


@pytest.mark.parametrize(
    "missing", ["run_task", "bind_persistent_session", "bind_runner_call"])
def test_guarded_runner_rejects_declared_persistent_runner_missing_callable(
        missing):
    class DeclaredRunner:
        bundle_operator_session_contract = BUNDLE_OPERATOR_SESSION_CONTRACT

        def run_task(self):
            return "ran"

        def bind_persistent_session(self, **_kwargs):
            return "session-bound"

        def bind_runner_call(self, **_kwargs):
            return "call-bound"

    inner = DeclaredRunner()
    setattr(inner, missing, None)

    with pytest.raises(RuntimeError, match=missing):
        _GuardedRunner(inner, lambda: None)


def test_guarded_runner_required_binding_never_degrades_to_noop():
    class DeclaredRunner:
        stage_main_session_contract = STAGE_MAIN_SESSION_CONTRACT

        def run_task(self):
            return "ran"

        def bind_persistent_session(self, **_kwargs):
            return "session-bound"

        def bind_runner_call(self, **_kwargs):
            return "call-bound"

    inner = DeclaredRunner()
    guarded = _GuardedRunner(inner, lambda: None)
    inner.bind_persistent_session = None

    with pytest.raises(RuntimeError, match="bind_persistent_session.*漂移"):
        guarded.bind_persistent_session(session_id="thread-1", role="stage_main")


def test_guarded_runner_keeps_optional_bindings_for_undeclared_compat_runner():
    class CompatRunner:
        def run_task(self):
            return "ran"

    guarded = _GuardedRunner(CompatRunner(), lambda: None)

    assert guarded.bind_persistent_session(session_id=None) is None
    assert guarded.bind_runner_call(runner_call_id=1) is None
    assert guarded.run_task() == "ran"


_BOOT_TERMINATE = {
    "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题：EEG 有跨数据集通用规律吗？",
                               "local_key": "root"}]},
    "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                       "terminate_reason_md": "创世即达成（测试固定）"},
}


def _query_tool_free_contract():
    """Contract an injected interaction-query runner must explicitly promise."""
    return {
        **tool_free_runtime_contract(),
        "bin": os.environ.get("METARESEARCH_QUERY_CODEX_BIN", "/usr/local/bin/codex"),
        "model": os.environ.get("METARESEARCH_CODEX_MODEL", DEFAULT_CODEX_MODEL),
        "effort": os.environ.get("METARESEARCH_CODEX_EFFORT", DEFAULT_CODEX_EFFORT),
    }


class _PersistentQueryTestRunner:
    """Give injected query doubles the same durable session evidence as CodexRunner.

    Production query assembly requires an execution receipt, a provider receipt, and
    a provider thread/session id before it will admit a natural-language reply.  The
    ordinary stage doubles in this module intentionally do not need that capability;
    query-specific doubles subclass this helper and call ``query_artifact``.
    """

    tool_free_contract = _query_tool_free_contract()

    def __init__(self, work_root: Path):
        self._query_work_root = Path(work_root)
        self._query_call = None
        self._query_session_id = None

    def bind_runner_call(self, *, runner_call_id, reconcile_protocol, phase, purpose):
        self._query_call = {
            "runner_call_id": runner_call_id,
            "reconcile_protocol": reconcile_protocol,
            "phase": phase,
            "purpose": purpose,
        }

    def bind_persistent_session(self, *, session_id):
        self._query_session_id = session_id

    def query_artifact(self, *, context_pack, answer: str, usage: CallUsage) -> Artifact:
        call = self._query_call
        assert call is not None
        operation_id = "exec-" + hashlib.sha256(
            f"{self._query_work_root}:{call['runner_call_id']}".encode("utf-8")
        ).hexdigest()[:32]
        supervisor = ExecutionSupervisor.standalone(
            self._query_work_root / "state" / "executions")
        execution_path = supervisor.receipt_dir / f"execution-{operation_id}.json"
        execution = supervisor._prepared_receipt(  # noqa: SLF001 - deterministic fixture
            operation_id=operation_id, kind="codex-query-test",
            spec_sha256="sha256:" + "e" * 64, timeout_s=10,
            operation_context={
                "reconcile_protocol": call["reconcile_protocol"],
                "db_owner_kind": "runner_call",
                "db_owner_id": call["runner_call_id"],
                "cycle_id": context_pack.cycle_id,
                "db_phase": call["phase"],
                "db_purpose": call["purpose"],
            })
        now = time.time()
        execution.update({
            "state": "terminal", "outcome": "exit", "returncode": 0,
            "started_at_unix": now - 0.01, "finished_at_unix": now,
            "group_drained": True, "term_sent": False, "kill_sent": False,
        })
        atomic_write_receipt(execution_path, execution)
        provider_path = write_provider_invocation_receipt(
            receipt_dir=supervisor.receipt_dir,
            runner_call_id=call["runner_call_id"],
            cycle_id=context_pack.cycle_id, phase=call["phase"],
            purpose=call["purpose"], provider="codex-cli",
            model=DEFAULT_CODEX_MODEL, effort=DEFAULT_CODEX_EFFORT,
            prompt_sha256="sha256:" + "f" * 64,
            usage=usage, usage_source="json_turn_completed",
            execution_receipt_ref=str(execution_path),
            provider_invocation_id=(self._query_session_id or "thread-run-test"),
            provider_invocation_id_kind="thread_id")
        return Artifact(
            stage="reasoning", md="", usage=usage,
            files={"interaction_reply.json": {"answer": answer}},
            execution_receipt_ref=str(execution_path),
            provider_receipt_ref=provider_path)


def _mock_factory(files_seq):
    """runner 工厂：每次 run_task 吐序列里的下一份 files（Artifact）。stage 取 pack.stage（不漂移）。"""
    box = {"seq": list(files_seq)}

    class MockRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            return Artifact(stage=context_pack.stage, files=box["seq"].pop(0), md="",
                            usage=CallUsage(tokens_known=True))
    return lambda td, pt: MockRunner()


def _storage_manifest(work: Path, cycle_id: str):
    pointer = json.loads(
        (work / "state" / "storage" / "cycles" / f"{cycle_id}.json").read_text(
            encoding="utf-8"))
    return json.loads((work / pointer["manifest_path"]).read_text(encoding="utf-8"))


def test_system_rejects_unimplemented_multi_stage_session_mode(tmp_path):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    with pytest.raises(ValueError, match="dual_mode.*只支持 A"):
        System(
            advancer=IdleAdvancer(), state=None, daemon=None,
            dual_mode="B", work_root=tmp_path)

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None,
        dual_mode="A", work_root=tmp_path)
    with pytest.raises(AttributeError):
        system.dual_mode = "B"
    assert system.dual_mode == "A"


def test_system_run_rejects_negative_cycle_limit_before_advancer(tmp_path):
    class NeverAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise AssertionError("negative limit must not reach advancer")

    system = System(
        advancer=NeverAdvancer(), state=None, daemon=None,
        dual_mode="A", work_root=tmp_path)
    with pytest.raises(ValueError, match="max_cycles 须为非负整数"):
        system.run(-1)


def test_build_system_rejects_mode_b_before_work_root_side_effect(tmp_path, monkeypatch):
    policy = yaml.safe_load(yaml.safe_dump(_POLICY))
    policy["session"]["dual_mode"] = "B"
    monkeypatch.setattr("orchestrator.run.yaml.safe_load", lambda _raw: policy)
    work = tmp_path / "must-not-exist"
    with pytest.raises(ValidationError):
        build_system(SYSTEM_ROOT, str(work))
    assert not work.exists()


def test_build_system_keeps_one_lexical_absolute_work_root(tmp_path, monkeypatch):
    import orchestrator.run as R

    captured = {}
    lease = object()
    assembled = object()

    class FakeInstanceLease:
        @staticmethod
        def acquire(work_root, *, heartbeat_interval_s):
            captured["lease_work"] = work_root
            captured["heartbeat_interval_s"] = heartbeat_interval_s
            return lease

    def fake_assemble_system(**kwargs):
        captured["assembly_work"] = kwargs["work"]
        captured["instance_lease"] = kwargs["instance_lease"]
        return assembled

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(R, "InstanceLease", FakeInstanceLease)
    monkeypatch.setattr(R, "_assemble_system", fake_assemble_system)

    assert R.build_system(
        SYSTEM_ROOT, "relative-work", attack=False,
        heartbeat_interval_s=0.5) is assembled
    expected = tmp_path / "relative-work"
    assert captured == {
        "lease_work": expected,
        "heartbeat_interval_s": 0.5,
        "assembly_work": expected,
        "instance_lease": lease,
    }


def test_runtime_profile_projection_is_private_and_closes_compute_review_choices():
    import copy
    import orchestrator.run as R

    base = copy.deepcopy(_POLICY)
    original = copy.deepcopy(base)
    gpu = R._project_quest_runtime_policy(base, {
        "profile": {
            "version": 1,
            "compute_profile_id": "local-gpu",
            "review_intensity": "once",
        },
    })
    assert base == original
    assert gpu is not base
    # local-gpu never gets device/memory authority from the mutable ledger.
    assert gpu["resources"] == original["resources"]
    assert gpu["resources"] == {
        "gpus": 8,
        "gpu_mem_gb": 80,
        "disk_quota_gb": 10240,
        "gpu_target_policy": "required",
        "allowed_device_indices": list(range(8)),
    }
    assert [gpu["flow"]["retry"][field] for field in (
        "plan_review", "bundle_code_review", "bundle_result_review",
    )] == [1, 1, 1]

    selected_indices = [1, 4, 6]
    selected_profile = {
        "version": 3,
        "compute_profile_id": "local-gpu",
        "review_intensity": "once",
        "gpu_device_indices": selected_indices,
    }
    narrowed = R._project_quest_runtime_policy(base, {
        "profile": selected_profile,
    })
    expected_narrowed = copy.deepcopy(original)
    expected_narrowed["resources"]["gpus"] = len(selected_indices)
    expected_narrowed["resources"]["allowed_device_indices"] = selected_indices
    for field in (
            "plan_review", "bundle_code_review", "bundle_result_review"):
        expected_narrowed["flow"]["retry"][field] = 1
    assert narrowed == expected_narrowed
    assert narrowed["resources"]["gpus"] == len(selected_indices)
    assert original["resources"]["gpus"] == 8
    assert (narrowed["resources"]["gpu_mem_gb"]
            == original["resources"]["gpu_mem_gb"] == 80)
    assert narrowed["resources"]["disk_quota_gb"] == (
        original["resources"]["disk_quota_gb"])
    assert narrowed["resources"]["gpu_target_policy"] == (
        original["resources"]["gpu_target_policy"])
    assert base == original
    assert selected_profile["gpu_device_indices"] == [1, 4, 6]

    with pytest.raises(ValueError, match="runtime GPU"):
        R._project_quest_runtime_policy(base, {
            "profile": {
                **selected_profile,
                "gpu_device_indices": [8],
            },
        })
    two_gpu_base = copy.deepcopy(base)
    two_gpu_base["resources"]["gpus"] = 2
    with pytest.raises(ValueError, match="runtime GPU"):
        R._project_quest_runtime_policy(two_gpu_base, {
            "profile": {
                **selected_profile,
                "version": 2,
                "gpu_device_indices": [0],
            },
        })
    assert base == original

    exact_indices = [1, 4, 7]
    exact = R._project_quest_runtime_policy(base, {
        "profile": {
            "version": 3,
            "compute_profile_id": "local-gpu",
            "review_intensity": "once",
            "gpu_device_indices": exact_indices,
        },
    })
    assert exact["resources"]["gpus"] == 3
    assert exact["resources"]["allowed_device_indices"] == exact_indices
    assert exact["resources"]["gpu_mem_gb"] == 80
    assert exact["resources"]["gpu_target_policy"] == "required"
    assert exact["execution"]["sandbox"] == original["execution"]["sandbox"]
    assert base == original

    cpu = R._project_quest_runtime_policy(base, {
        "profile": {
            "version": 1,
            "compute_profile_id": "local-cpu",
            "review_intensity": "off",
        },
    })
    assert {field: cpu["resources"][field] for field in (
        "gpus", "gpu_mem_gb", "allowed_device_indices", "gpu_target_policy",
    )} == {
        "gpus": 0,
        "gpu_mem_gb": 0,
        "allowed_device_indices": [],
        "gpu_target_policy": "forbidden",
    }
    assert [cpu["flow"]["retry"][field] for field in (
        "plan_review", "bundle_code_review", "bundle_result_review",
    )] == [0, 0, 0]
    # Compute/review selection must not widen host env or Docker networking.
    assert cpu["execution"]["sandbox"]["network_mode"] == (
        original["execution"]["sandbox"]["network_mode"])
    assert cpu["execution"]["sandbox"]["local_environment"] == (
        original["execution"]["sandbox"]["local_environment"])
    assert base == original


def test_runtime_profile_monitor_waits_for_durable_cycle_boundary():
    import copy
    import threading
    import orchestrator.run as R

    applied = {
        "quest_id": "alpha",
        "revision": 1,
        "profile": {
            "version": 1, "compute_profile_id": "local-gpu",
            "review_intensity": "once",
        },
        "record_sha256": "sha256:" + "1" * 64,
        "source": "ledger",
    }
    latest = {
        **applied,
        "revision": 2,
        "record_sha256": "sha256:" + "2" * 64,
    }

    class Settings:
        def current(self):
            return copy.deepcopy(latest)

    stop = threading.Event()
    monitor = R._RuntimeProfileMonitor(Settings(), applied, stop)
    # A stage precheck may observe the update, but an old-policy cycle must
    # continue through its remaining stages and gates without interruption.
    assert monitor.probe(safe_cycle_boundary=False) is None
    assert stop.is_set() is False
    assert monitor.pending_revision == 2
    reason = monitor.probe(safe_cycle_boundary=True)
    assert "durable cycle" in reason
    assert stop.is_set() is True


def test_runtime_cycle_binding_clears_only_after_success_without_inflight(tmp_path):
    events = []

    class State:
        inflight = None

        def inflight_cycle(self):
            return self.inflight

    class Advancer:
        last_stop_reason = None
        last_block_reason = None

        def __init__(self, state, *, failure=None):
            self.state = state
            self.failure = failure

        def run_cycles(self, _max_cycles):
            if self.failure is not None:
                raise self.failure
            return []

    state = State()
    system = System(
        advancer=Advancer(state), state=state, daemon=None,
        dual_mode="A", work_root=tmp_path,
        runtime_profile_cycle_bind=lambda: events.append("bind"),
        runtime_profile_cycle_clear=lambda: events.append("clear"))
    assert system.run(1) == []
    assert events == ["bind", "clear"]

    events.clear()
    state.inflight = object()
    blocked = System(
        advancer=Advancer(state), state=state, daemon=None,
        dual_mode="A", work_root=tmp_path,
        runtime_profile_cycle_bind=lambda: events.append("bind"),
        runtime_profile_cycle_clear=lambda: events.append("clear"))
    assert blocked.run(1) == []
    assert events == ["bind"]

    events.clear()
    state.inflight = None
    failed = System(
        advancer=Advancer(state, failure=RuntimeError("provider crash")),
        state=state, daemon=None, dual_mode="A", work_root=tmp_path,
        runtime_profile_cycle_bind=lambda: events.append("bind"),
        runtime_profile_cycle_clear=lambda: events.append("clear"))
    with pytest.raises(RuntimeError, match="provider crash"):
        failed.run(1)
    assert events == ["bind"]


def test_stale_cycle_binding_without_inflight_clears_then_exits_to_latest(tmp_path):
    import orchestrator.run as R
    from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

    work = tmp_path / "alpha"
    work.mkdir(mode=0o700)
    (work / "state").mkdir(mode=0o700)
    settings = QuestRuntimeSettings(work, "alpha")
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    initial = build_system(
        SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]),
        attack=False, quest_id="alpha",
        expected_runtime_profile_revision=old["revision"],
        expected_runtime_profile_record_sha256=old["record_sha256"])
    assert initial.close() is None
    settings.bind_cycle_profile(old)
    latest = settings.update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "2" * 32)
    with pytest.raises(R.RuntimeProfileRestartRequired, match="no-inflight DB"):
        build_system(
            SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]),
            attack=False, quest_id="alpha",
            expected_runtime_profile_revision=old["revision"],
            expected_runtime_profile_record_sha256=old["record_sha256"])
    assert latest["revision"] != old["revision"]
    # The old owner exits before entering run(1), so it cannot recreate the
    # stale marker and cause a restart loop.
    assert settings.bound_cycle_profile() is None

    replacement = build_system(
        SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]),
        attack=False, quest_id="alpha",
        expected_runtime_profile_revision=latest["revision"],
        expected_runtime_profile_record_sha256=latest["record_sha256"])
    try:
        assert replacement.runtime_profile_revision == latest["revision"]
    finally:
        assert replacement.close() is None


def test_stale_gpu_binding_is_cleared_before_attack_gpu_preflight(
        tmp_path, monkeypatch):
    import orchestrator.run as R
    from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

    work = tmp_path / "alpha"
    work.mkdir(mode=0o700)
    (work / "state").mkdir(mode=0o700)
    settings = QuestRuntimeSettings(work, "alpha")
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)
    initial = build_system(
        SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]),
        attack=False, quest_id="alpha",
        expected_runtime_profile_revision=old["revision"],
        expected_runtime_profile_record_sha256=old["record_sha256"])
    assert initial.close() is None
    settings.bind_cycle_profile(old)
    settings.update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "2" * 32)

    preflight_calls = []

    def forbidden_old_gpu_preflight(*_args, **_kwargs):
        preflight_calls.append(True)
        raise AssertionError("obsolete GPU/Docker preflight was reached")

    monkeypatch.setattr(R, "DockerExecutionSandbox", forbidden_old_gpu_preflight)
    with pytest.raises(R.RuntimeProfileRestartRequired, match="no-inflight DB"):
        R.build_system(
            SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]),
            attack=True, quest_id="alpha",
            expected_runtime_profile_revision=old["revision"],
            expected_runtime_profile_record_sha256=old["record_sha256"])
    assert preflight_calls == []
    assert settings.bound_cycle_profile() is None


def test_midcycle_crash_reopens_with_bound_profile_not_latest(tmp_path, monkeypatch):
    from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

    _use_budgeted_policy(monkeypatch)

    work = tmp_path / "alpha"
    work.mkdir(mode=0o700)
    (work / "state").mkdir(mode=0o700)
    settings = QuestRuntimeSettings(work, "alpha")
    old = settings.initialize({
        "version": 1, "compute_profile_id": "local-gpu",
        "review_intensity": "once",
    }, "1" * 32)

    class SimulatedOwnerCrash(BaseException):
        pass

    class CrashRunner:
        def run_task(self, **_kwargs):
            raise SimulatedOwnerCrash("simulated owner crash during cycle")

    first = build_system(
        SYSTEM_ROOT, str(work),
        runner_factory=lambda *_args: CrashRunner(), attack=False,
        quest_id="alpha",
        expected_runtime_profile_revision=old["revision"],
        expected_runtime_profile_record_sha256=old["record_sha256"])
    with pytest.raises(SimulatedOwnerCrash, match="simulated owner crash"):
        first.run(1)
    assert first.state.inflight_cycle() is not None
    assert settings.bound_cycle_profile()["revision"] == old["revision"]
    assert first.close() is None

    latest = settings.update({
        "version": 1, "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "2" * 32)
    recovered = build_system(
        SYSTEM_ROOT, str(work),
        runner_factory=_mock_factory([_BOOT_TERMINATE]), attack=False,
        quest_id="alpha",
        expected_runtime_profile_revision=old["revision"],
        expected_runtime_profile_record_sha256=old["record_sha256"])
    try:
        assert recovered.runtime_profile_revision == old["revision"]
        assert latest["revision"] != recovered.runtime_profile_revision
        assert recovered.state.inflight_cycle() is not None
        # Core cost-accounting reconciliation conservatively terminalizes the
        # interrupted provider instead of retrying unknown usage.  The runtime
        # boundary must still retain the old binding; it may never reinterpret
        # this inflight cycle under latest CPU/review-off policy.
        assert recovered.run(1) == []
        assert recovered.last_stop_reason == "cost_accounting_failed"
        assert recovered.state.inflight_cycle() is not None
        assert settings.bound_cycle_profile()["revision"] == old["revision"]
    finally:
        assert recovered.close() is None


def test_build_system_runtime_profile_fences_expected_hash_and_legacy_is_opt_in(
        tmp_path, monkeypatch):
    import orchestrator.run as R
    from orchestrator.quest_runtime_profiles import QuestRuntimeSettings

    work = tmp_path / "alpha-work"
    work.mkdir(mode=0o700)
    (work / "state").mkdir(mode=0o700)
    settings = QuestRuntimeSettings(work, "alpha")
    current = settings.initialize({
        "version": 1,
        "compute_profile_id": "local-cpu",
        "review_intensity": "off",
    }, "a" * 32)
    captured = {}
    assembled = object()

    def fake_assemble(**kwargs):
        captured.update(kwargs)
        return assembled

    monkeypatch.setattr(R, "_assemble_system", fake_assemble)
    assert R.build_system(
        SYSTEM_ROOT, str(work), attack=False,
        enforce_instance_lease=False, quest_id="alpha",
        expected_runtime_profile_revision=current["revision"],
        expected_runtime_profile_record_sha256=current["record_sha256"],
    ) is assembled
    assert captured["policy"]["resources"]["gpus"] == 0
    assert captured["runtime_settings"].quest_id == "alpha"
    assert captured["applied_runtime_profile"]["record_sha256"] == (
        current["record_sha256"])

    called = []
    monkeypatch.setattr(
        R, "_assemble_system", lambda **_kwargs: called.append(True))
    with pytest.raises(ValueError, match="manager 捕获后发生漂移"):
        R.build_system(
            SYSTEM_ROOT, str(work), attack=False,
            enforce_instance_lease=False, quest_id="alpha",
            expected_runtime_profile_revision=current["revision"],
            expected_runtime_profile_record_sha256="sha256:" + "0" * 64,
        )
    assert called == []

    # Ordinary CLI/tests with no proven quest identity retain the exact legacy
    # base-policy path and must not even instantiate the mutable settings API.
    legacy_work = tmp_path / "plain_tmp_path_with_underscores"
    monkeypatch.setattr(
        R, "QuestRuntimeSettings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy build must not read quest runtime settings")))
    seen = {}

    def fake_legacy_assemble(**kwargs):
        seen["policy"] = kwargs["policy"]
        return assembled

    monkeypatch.setattr(R, "_assemble_system", fake_legacy_assemble)
    result = R.build_system(
        SYSTEM_ROOT, str(legacy_work), attack=False,
        enforce_instance_lease=False)
    assert result is assembled
    assert seen["policy"] == _POLICY


def test_system_run_keeps_primary_when_exit_notification_scan_also_fails(tmp_path):
    class PrimaryFailure(RuntimeError):
        pass

    class BrokenAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise PrimaryFailure("研究主链失败")

    def broken_scan():
        raise OSError("outbox 不可写")

    system = System(advancer=BrokenAdvancer(), state=None, daemon=None,
                    dual_mode="A", work_root=tmp_path, sync_notifications=broken_scan)
    with pytest.raises(PrimaryFailure, match="研究主链失败") as caught:
        system.run(1)
    assert any("notification scan" in note and "outbox 不可写" in note
               for note in getattr(caught.value, "__notes__", ()))


def test_run_forever_waits_and_counts_max_cycles_across_reentry(tmp_path, monkeypatch):
    class BlockingAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def __init__(self):
            self.budgets = []
            self.results = iter([
                (["c1"], "等待文件"), ([], "等待文件"),
                (["c2"], None), (["c3"], None),
            ])

        def run_cycles(self, max_cycles):
            self.budgets.append(max_cycles)
            result, self.last_block_reason = next(self.results)
            return result

    advancer = BlockingAdvancer()
    scans = []
    system = System(advancer=advancer, state=None, daemon=None,
                    dual_mode="A", work_root=tmp_path, sync_notifications=lambda: scans.append(1))
    monkeypatch.setattr("orchestrator.run.time.sleep", lambda _seconds: None)
    assert system.run_forever(3, poll_interval_s=0.01,
                              linger_after_terminal=False) == ["c1", "c2", "c3"]
    assert advancer.budgets == [1, 1, 1, 1]                 # 每轮归还控制；阻断不重置累计上限
    assert len(scans) == 5                                  # 四次推进边界 + 受控退出排空扫描


def test_run_forever_never_outer_retries_resident_artifact_rejection(tmp_path):
    class RetryAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def __init__(self):
            self.calls = 0

        def run_cycles(self, _max_cycles):
            self.calls += 1
            if self.calls == 1:
                raise RunnerError(
                    "plan schema repair exhausted", failure_kind="artifact_parse")
            return ["c1"]

    advancer = RetryAdvancer()
    system = System(
        advancer=advancer, state=None, daemon=None,
        dual_mode="A", work_root=tmp_path)

    with pytest.raises(RunnerError, match="schema repair exhausted"):
        system.run_forever(
            1, poll_interval_s=0.01, linger_after_terminal=False)
    assert advancer.calls == 1


def test_run_forever_does_not_retry_runner_integrity_failure(tmp_path):
    class BrokenAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise RunnerError(
                "provider thread changed", failure_kind="provider_session_drift")

    system = System(
        advancer=BrokenAdvancer(), state=None, daemon=None,
        dual_mode="A", work_root=tmp_path)

    with pytest.raises(RunnerError, match="provider thread changed"):
        system.run_forever(1, poll_interval_s=0.01, linger_after_terminal=False)


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True])
def test_run_forever_rejects_hot_spin_poll_intervals(tmp_path, interval):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise AssertionError("非法 interval 必须在推进前拒绝")

    system = System(advancer=IdleAdvancer(), state=None, daemon=None,
                    dual_mode="A", work_root=tmp_path)
    with pytest.raises(ValueError, match="0.01"):
        system.run_forever(1, poll_interval_s=interval)


def test_drain_unconditionally_probes_and_retries_transient_completion(tmp_path, monkeypatch):
    import sqlite3

    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    state = {"calls": 0, "pending": False}

    def sync():
        state["calls"] += 1
        if state["calls"] == 1:
            raise sqlite3.OperationalError("database is locked")
        state["pending"] = False

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=sync, interaction_pending=lambda: state["pending"])
    monkeypatch.setattr("orchestrator.run.time.sleep", lambda _seconds: None)
    system.drain_interactions(poll_interval_s=0.01)
    assert state["calls"] == 2       # cached pending=false 也先扫，且保留瞬时回执重试


def test_run_forever_stop_event_drains_already_accepted_interaction(tmp_path, monkeypatch):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise AssertionError("pre-set stop_event 不应推进研究")

    stop = __import__("threading").Event()
    stop.set()
    state = {"pending": True, "syncs": 0}

    def sync():
        state["syncs"] += 1
        state["pending"] = False

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=sync, interaction_pending=lambda: state["pending"])
    monkeypatch.setattr("orchestrator.run.time.sleep", lambda _seconds: None)
    assert system.run_forever(1, poll_interval_s=0.01, stop_event=stop) == []
    assert not state["pending"] and state["syncs"] >= 1


def test_run_forever_observes_stop_event_between_cycles(tmp_path):
    stop = __import__("threading").Event()

    class CountingAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def __init__(self):
            self.calls = 0

        def run_cycles(self, max_cycles):
            assert max_cycles == 1
            self.calls += 1
            stop.set()
            return [f"c{self.calls}"]

    advancer = CountingAdvancer()
    system = System(
        advancer=advancer, state=None, daemon=None, dual_mode="A", work_root=tmp_path)
    assert system.run_forever(150, poll_interval_s=0.01, stop_event=stop) == ["c1"]
    assert advancer.calls == 1


def test_drain_does_not_keep_consuming_new_spool_after_boundary(tmp_path, monkeypatch):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    state = {"accepted": True, "intake_calls": 0, "completion_calls": 0}

    def intake():
        state["intake_calls"] += 1
        # A live connector still has newer spool input, represented by interaction_pending=True below.

    def complete():
        state["completion_calls"] += 1
        state["accepted"] = False

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=intake, interaction_pending=lambda: True,
        sync_accepted_interactions=complete,
        accepted_interaction_pending=lambda: state["accepted"])
    monkeypatch.setattr("orchestrator.run.time.sleep", lambda _seconds: None)
    system.drain_interactions(poll_interval_s=0.01)
    assert state == {"accepted": False, "intake_calls": 1, "completion_calls": 1}


def test_drain_exhausts_finite_closed_connector_backlog(tmp_path):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    state = {"general_probes": 0, "closed_backlog": 3, "accepted": 0}

    def general_probe():
        state["general_probes"] += 1
        state["closed_backlog"] -= 1
        state["accepted"] += 1

    def closed_probe():
        state["closed_backlog"] -= 1
        state["accepted"] += 1

    def complete():
        state["accepted"] = 0

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=general_probe,
        sync_closed_inbound=closed_probe,
        closed_inbound_pending=lambda: state["closed_backlog"] > 0,
        sync_accepted_interactions=complete,
        accepted_interaction_pending=lambda: state["accepted"] > 0)
    system.drain_interactions(poll_interval_s=0.01)
    assert state == {"general_probes": 1, "closed_backlog": 0, "accepted": 0}


def test_drain_finishes_accepted_query_before_reporting_notification_error(tmp_path):
    class IdleAdvancer:
        last_stop_reason = None
        last_block_reason = None

    state = {"accepted": True, "completion_calls": 0}

    def complete():
        state["completion_calls"] += 1
        state["accepted"] = False

    system = System(
        advancer=IdleAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_accepted_interactions=complete,
        accepted_interaction_pending=lambda: state["accepted"],
        sync_notifications=lambda: (_ for _ in ()).throw(OSError("outbox unavailable")))
    with pytest.raises(OSError, match="outbox unavailable"):
        system.drain_interactions(poll_interval_s=0.01)
    assert state == {"accepted": False, "completion_calls": 1}


def test_pump_error_still_drains_already_accepted_query(tmp_path):
    pump_failed = __import__("threading").Event()
    state = {"accepted": True, "completion_calls": 0}

    class WaitingAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            assert pump_failed.wait(1)
            return []

    def broken_intake():
        pump_failed.set()
        raise RuntimeError("pump broke")

    def complete():
        state["completion_calls"] += 1
        state["accepted"] = False

    system = System(
        advancer=WaitingAdvancer(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=broken_intake,
        sync_accepted_interactions=complete,
        accepted_interaction_pending=lambda: state["accepted"])
    with pytest.raises(RuntimeError, match="pump broke"):
        system.run(1)
    assert state == {"accepted": False, "completion_calls": 1}


def test_run_forever_never_restarts_over_uncollected_pump_error(tmp_path):
    failed = __import__("threading").Event()
    calls = {"n": 0}

    class OneCycle:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            assert failed.wait(1)
            return ["c1"]

    def fail_once():
        calls["n"] += 1
        if calls["n"] == 1:
            failed.set()
            raise RuntimeError("resident pump evidence")

    system = System(
        advancer=OneCycle(), state=None, daemon=None, dual_mode="A", work_root=tmp_path,
        sync_interactions=fail_once)
    with pytest.raises(RuntimeError, match="resident pump evidence"):
        system.run_forever(1, poll_interval_s=0.01, linger_after_terminal=False)


def test_main_ctrl_c_exits_cleanly(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class InterruptSystem:
        dual_mode = "A"

        def __init__(self):
            self.scans = 0

        def run_forever(self, _max_cycles, *, poll_interval_s):
            raise KeyboardInterrupt

        def sync_notifications(self):
            self.scans += 1

    system = InterruptSystem()
    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: system)
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
                 "--poll-interval-s", "0.01", "--no-outbound"])
    assert rc == 130 and system.scans == 1
    assert "Ctrl-C" in capsys.readouterr().out


def test_main_derives_shared_storage_parent_and_reports_clean_error(
        tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    work = tmp_path / "quest"
    seen = []

    def reject(root, *, require_external_mount, private_work_root=None):
        seen.append((
            Path(root), require_external_mount,
            None if private_work_root is None else Path(private_work_root)))
        raise ValueError("storage rejected")

    monkeypatch.setattr(R, "configure_process_storage", reject)

    assert R.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(work),
        "--once", "--no-outbound",
    ]) == 2
    assert seen == [(tmp_path, True, work)]
    captured = capsys.readouterr()
    assert "存储绑定失败" in captured.err
    assert "storage rejected" in captured.err


def test_main_real_storage_success_binds_work_parent(
        vepfs_tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class ZeroCycleSystem:
        dual_mode = "A"
        last_stop_reason = None

        class advancer:
            last_block_reason = None

        def run(self, max_cycles):
            assert max_cycles == 0
            return []

        def close(self):
            return None

    work = vepfs_tmp_path / "quest"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(R, "build_system", lambda *_args, **_kwargs: ZeroCycleSystem())
    try:
        assert R.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(work),
            "--max-cycles", "0", "--once", "--no-outbound",
        ]) == 0
        assert os.environ["METARESEARCH_STORAGE_ROOT"] == str(vepfs_tmp_path)
        assert (vepfs_tmp_path / ".process-tmp").is_dir()
        assert not (work / ".process-tmp").exists()
        assert "zero-cycle-preflight" in capsys.readouterr().out
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_web_child_reuses_real_console_storage_marker(
        vepfs_tmp_path, monkeypatch):
    import orchestrator.console_server as CS
    import orchestrator.run as R

    class StoppedConsole:
        console_capability_token = "a" * 64
        server_address = ("127.0.0.1", 43123)

        def serve_forever(self):
            raise KeyboardInterrupt

        def shutdown(self):
            return None

        def server_close(self):
            return None

    class ZeroCycleSystem:
        dual_mode = "A"
        last_stop_reason = None

        class advancer:
            last_block_reason = None

        def run(self, max_cycles):
            assert max_cycles == 0
            return []

        def close(self):
            return None

    registry = vepfs_tmp_path / "registry"
    work = registry / "quests" / "q1" / "work"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.setattr(
        CS, "serve_quests", lambda *_args, **_kwargs: StoppedConsole())
    monkeypatch.setattr(
        R, "build_system", lambda *_args, **_kwargs: ZeroCycleSystem())
    try:
        assert CS.main([
            "--system-root", SYSTEM_ROOT, "--quests-root", str(registry),
            "--no-open-browser",
        ]) == 0
        assert os.environ["METARESEARCH_STORAGE_ROOT"] == str(registry)

        assert R.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(work),
            "--max-cycles", "0", "--once", "--no-outbound",
        ]) == 0
        assert os.environ["METARESEARCH_STORAGE_ROOT"] == str(registry)
        assert tempfile.tempdir == str(registry / ".process-tmp")
        assert (registry / ".process-tmp").is_dir()
        assert not (work / ".process-tmp").exists()
        assert not (work.parent / ".process-tmp").exists()
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_real_storage_rejects_private_inherited_marker_without_side_effects(
        vepfs_tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    work = vepfs_tmp_path / "quest"
    nested = work / "private-storage"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.setenv("METARESEARCH_STORAGE_ROOT", str(nested))
    try:
        assert R.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(work),
            "--once", "--no-outbound",
        ]) == 2
        assert not work.exists()
        assert dict(os.environ) == {
            **before_environment, "METARESEARCH_STORAGE_ROOT": str(nested)}
        assert tempfile.tempdir == before_tempdir
        assert "存储绑定失败" in capsys.readouterr().err
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


def test_main_unknown_service_uid_returns_clean_storage_error(
        vepfs_tmp_path, monkeypatch, capsys):
    import orchestrator.run as R
    import orchestrator.runtime_storage as runtime_storage

    work = vepfs_tmp_path / "quest"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.delenv("METARESEARCH_QUERY_RUN_AS_USER", raising=False)
    monkeypatch.setattr(runtime_storage.os, "geteuid", lambda: 424242)
    monkeypatch.setattr(runtime_storage.os, "getegid", lambda: 424242)

    def missing_uid(_uid):
        raise KeyError("unknown uid")

    monkeypatch.setattr(runtime_storage.pwd, "getpwuid", missing_uid)
    try:
        assert R.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(work),
            "--once", "--no-outbound",
        ]) == 2
        assert not (vepfs_tmp_path / ".process-tmp").exists()
        assert "存储绑定失败" in capsys.readouterr().err
    finally:
        os.environ.clear()
        os.environ.update(before_environment)
        tempfile.tempdir = before_tempdir


@pytest.mark.parametrize("manifest_override", [False, True])
def test_local_execution_overrides_inherited_model_cache_paths(
        tmp_path, monkeypatch, manifest_override):
    from orchestrator.execution_sandbox import LocalExecutionSandbox

    stale = tmp_path / "outside"
    for name in (
            "CODEX_HOME", "CODEX_SQLITE_HOME",
            "HF_HOME", "HF_HUB_CACHE", "HF_DATASETS_CACHE",
            "TRANSFORMERS_CACHE", "TORCH_HOME", "TORCH_EXTENSIONS_DIR",
            "TRITON_CACHE_DIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR",
            "UV_CACHE_DIR", "CUDA_CACHE_PATH", "MPLCONFIGDIR",
            "NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX"):
        monkeypatch.setenv(name, str(stale / name))
    sandbox = LocalExecutionSandbox.__new__(LocalExecutionSandbox)
    sandbox.work_root = tmp_path / "quest"
    sandbox.python_path = Path(os.sys.executable)
    sandbox.owner_guard = lambda: None
    sandbox.gpu_contract = None
    sandbox.local_environment_ca_environment = {}
    sandbox._preflight_done = True

    invocation = sandbox.prepare(
        ["/bin/true"], staging_dir=sandbox.work_root / "run",
        log_name="cache.log",
        env=({
            "CODEX_HOME": str(stale / "manifest-codex"),
            "CODEX_SQLITE_HOME": str(stale / "manifest-codex-sqlite"),
        } if manifest_override else None),
        timeout_s=1)

    runtime = sandbox.work_root / "runtime" / "local-execution"
    assert invocation.env["HOME"] == str(runtime / "home")
    assert invocation.env["TMPDIR"] == str(runtime / "tmp")
    for name in (
            "CODEX_HOME", "CODEX_SQLITE_HOME",
            "HF_HOME", "HF_HUB_CACHE", "HF_DATASETS_CACHE",
            "TRANSFORMERS_CACHE", "TORCH_HOME", "TORCH_EXTENSIONS_DIR",
            "TRITON_CACHE_DIR", "XDG_CACHE_HOME", "PIP_CACHE_DIR",
            "UV_CACHE_DIR", "CUDA_CACHE_PATH", "MPLCONFIGDIR",
            "NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX"):
        assert os.path.commonpath((str(runtime), invocation.env[name])) == str(runtime)


def test_main_exit_after_research_disables_terminal_linger(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class FiniteSystem:
        dual_mode = "A"
        last_stop_reason = None

        class advancer:
            last_block_reason = None

        def __init__(self):
            self.calls = []
            self.closed = False

        def run_forever(self, max_cycles, *, poll_interval_s, linger_after_terminal=True):
            self.calls.append((max_cycles, poll_interval_s, linger_after_terminal))
            return ["c1"]

        def close(self):
            self.closed = True
            return None

    system = FiniteSystem()
    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: system)
    rc = R.main([
        "--system-root", SYSTEM_ROOT,
        "--work-root", str(tmp_path),
        "--max-cycles", "200",
        "--poll-interval-s", "0.25",
        "--exit-after-research",
        "--no-outbound",
    ])
    assert rc == 0
    assert system.calls == [(200, 0.25, False)]
    assert system.closed is True
    assert "推进 1 轮" in capsys.readouterr().out


def test_main_rejects_once_with_exit_after_research(tmp_path, capsys):
    import orchestrator.run as R

    with pytest.raises(SystemExit) as caught:
        R.main([
            "--system-root", SYSTEM_ROOT,
            "--work-root", str(tmp_path),
            "--once", "--exit-after-research", "--no-outbound",
        ])
    assert caught.value.code == 2
    assert "not allowed with argument --once" in capsys.readouterr().err


def test_main_rejects_negative_max_cycles_before_assembly(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    assembled = []
    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: assembled.append(True))
    with pytest.raises(SystemExit) as caught:
        R.main([
            "--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
            "--max-cycles", "-1", "--once", "--no-outbound",
        ])
    assert caught.value.code == 2
    assert assembled == []
    assert "max-cycles 须为非负整数" in capsys.readouterr().err


def test_main_zero_cycle_reports_preflight_not_idle(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class ZeroCycleSystem:
        dual_mode = "A"
        last_stop_reason = None

        class advancer:
            last_block_reason = None

        def run(self, max_cycles):
            assert max_cycles == 0
            return []

        def close(self):
            return None

    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: ZeroCycleSystem())
    assert R.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
        "--max-cycles", "0", "--once", "--no-outbound",
    ]) == 0
    output = capsys.readouterr().out
    assert "停因=zero-cycle-preflight" in output
    assert "prior-terminate/idle" not in output


def test_main_reports_positive_cycle_cap_separately_from_terminate(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class CappedSystem:
        dual_mode = "A"
        last_stop_reason = None

        class advancer:
            last_block_reason = None

        def run(self, max_cycles):
            assert max_cycles == 1
            return ["c1"]

        def close(self):
            return None

    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: CappedSystem())
    assert R.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
        "--max-cycles", "1", "--once", "--no-outbound",
    ]) == 0
    assert "停因=max_cycles_reached" in capsys.readouterr().out


def test_second_ctrl_c_during_run_forever_drain_is_hard_stop(tmp_path):
    class InterruptAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise KeyboardInterrupt("first")

    system = System(
        advancer=InterruptAdvancer(), state=None, daemon=None,
        dual_mode="A", work_root=tmp_path,
        interaction_pending=lambda: True,
        accepted_interaction_pending=lambda: True,
        sync_accepted_interactions=lambda: (_ for _ in ()).throw(KeyboardInterrupt("second")))
    with pytest.raises(KeyboardInterrupt) as caught:
        system.run_forever(1, poll_interval_s=0.01)
    assert str(caught.value) == "second"
    assert system._hard_stop_requested is True


def test_second_ctrl_c_during_direct_run_drain_is_hard_stop(tmp_path):
    class InterruptAdvancer:
        last_stop_reason = None
        last_block_reason = None

        def run_cycles(self, _max_cycles):
            raise KeyboardInterrupt("first")

    system = System(
        advancer=InterruptAdvancer(), state=None, daemon=None,
        dual_mode="A", work_root=tmp_path,
        interaction_pending=lambda: True,
        accepted_interaction_pending=lambda: True,
        sync_accepted_interactions=lambda: (_ for _ in ()).throw(KeyboardInterrupt("second")))
    with pytest.raises(KeyboardInterrupt) as caught:
        system.run(1)
    assert str(caught.value) == "second"
    assert system._hard_stop_requested is True


def test_main_hard_stop_kills_registered_groups_without_redrain(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class HardInterruptSystem:
        _hard_stop_requested = True
        _interaction_exit_drained = False

        def run_forever(self, _max_cycles, *, poll_interval_s):
            raise KeyboardInterrupt("second")

        def drain_interactions(self, **_kwargs):
            raise AssertionError("hard stop must not redrain")

        def sync_notifications(self):
            raise AssertionError("hard stop must not rescan notifications")

    killed = []
    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: HardInterruptSystem())
    monkeypatch.setattr(R, "terminate_active_process_groups", lambda: killed.append(True))
    assert R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
                   "--poll-interval-s", "0.01", "--no-outbound"]) == 130
    assert killed == [True]
    assert "立即硬停" in capsys.readouterr().out


def test_main_second_ctrl_c_during_fallback_drain_kills_groups(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R

    class TwiceInterruptedSystem:
        _hard_stop_requested = False
        _interaction_exit_drained = False

        def run_forever(self, _max_cycles, *, poll_interval_s):
            raise KeyboardInterrupt("first")

        def drain_interactions(self, **_kwargs):
            raise KeyboardInterrupt("second")

        def sync_notifications(self):
            raise AssertionError("hard stop must skip notifications")

    killed = []
    monkeypatch.setattr(R, "build_system", lambda *_a, **_kw: TwiceInterruptedSystem())
    monkeypatch.setattr(R, "terminate_active_process_groups", lambda: killed.append(True))
    assert R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
                   "--poll-interval-s", "0.01", "--no-outbound"]) == 130
    assert killed == [True]
    assert "立即硬停" in capsys.readouterr().out


# ============ 全装配端到端（reasoning-only 闭环）============
@pytest.mark.parametrize("contract_attr,contract_value", [
    ("bundle_operator_session_contract", BUNDLE_OPERATOR_SESSION_CONTRACT),
    ("stage_main_session_contract", STAGE_MAIN_SESSION_CONTRACT),
])
def test_build_system_persistent_contract_checks_inner_runner_capabilities_on_creation(
        tmp_path, contract_attr, contract_value):
    class MissingRunnerCallBinding:
        def run_task(self, **_kwargs):
            raise AssertionError("capability check must fail before run_task")

        def bind_persistent_session(self, **_kwargs):
            raise AssertionError("capability check must fail before session binding")

    setattr(MissingRunnerCallBinding, contract_attr, contract_value)

    def declared_factory(_transcripts, _purpose):
        return MissingRunnerCallBinding()

    setattr(declared_factory, contract_attr, contract_value)
    system = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=declared_factory)
    try:
        stage_provider = system.advancer._reasoning.__self__
        with pytest.raises(RuntimeError, match="bind_runner_call"):
            stage_provider.runner_factory(
                tmp_path / "capability-probe", "capability-probe")
    finally:
        system.close()


def test_default_attack_assembly_shares_owner_fenced_pool_publisher(tmp_path):
    from orchestrator.pool_publication import PoolPublisher

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    try:
        attack = system.advancer.attack
        assert attack.gate.require_formal_publication is True
        assert attack.gate.require_scientific_contract is True
        assert system.state.require_reasoning_commit is True
        assert isinstance(attack.pool_publisher, PoolPublisher)
        assert attack.gate.pool_publisher is attack.pool_publisher
        assert attack.pool_publisher.work_root == tmp_path.absolute()
        assert attack.pool_publisher.owner_guard == system.instance_lease.assert_owned
    finally:
        system.close()


def test_default_attack_assembly_includes_fenced_import_worker(tmp_path):
    from orchestrator.execution_sandbox import LocalExecutionSandbox
    from orchestrator.import_fetcher import FrozenCandidateFetcher
    from orchestrator.repository_materializer import (
        GitHubRepositoryMaterializer, ProductionCandidateFetcher)
    from orchestrator.repository_adapter_generation import AdapterGenerationService
    from orchestrator.import_search import GitHubRepoSearchProvider, ImportSearchService
    from orchestrator.import_triggers import (
        BoundedReferenceSnapshotProvider, ImportTriggerRouter,
        TrustedImportTriggerService)
    from orchestrator.import_worker import ImportWorker
    from orchestrator.stage_provider import JudgeProvider

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    try:
        worker = system.advancer.import_worker
        assert isinstance(worker, ImportWorker)
        assert isinstance(worker.p["fetch"], ProductionCandidateFetcher)
        assert isinstance(worker.p["fetch"].legacy_fetcher, FrozenCandidateFetcher)
        assert isinstance(
            worker.p["fetch"].repository_fetcher,
            GitHubRepositoryMaterializer)
        assert isinstance(
            worker.p["fetch"].repository_fetcher.adapter_generator,
            AdapterGenerationService)
        assert worker.execution_supervisor is system.execution_supervisor
        assert isinstance(worker.execution_sandbox, LocalExecutionSandbox)
        assert worker.execution_sandbox.backend_name == "local-conda"
        assert system.deployment_receipt["mode"] == "development"
        assert system.deployment_receipt["production_ready"] is False
        deployment_receipts = list((tmp_path / "state" / "deployment").glob("deployment-*.json"))
        assert len(deployment_receipts) == 1
        assert system.advancer.attack.execution_sandbox is worker.execution_sandbox
        runtime_hash = worker.execution_sandbox.environment_hash
        assert system.advancer.compiler.runtime_environment_hash == runtime_hash
        assert "gpu_capability" not in system.advancer.attack.policy["execution"]["sandbox"]
        assert worker.p["fetch"].repository_fetcher.environment_hash == runtime_hash
        assert system.advancer.attack.execution_sandbox_resolver is None
        assert worker.execution_sandbox.resource_mode == "unrestricted-local"
        assert "plan_review" not in system.advancer.attack.p
        # An injected, non-resident runner cannot emit native child-review
        # receipts.  It therefore receives the explicit independent judge
        # fallback instead of silently bypassing code/result review.
        assert isinstance(system.advancer.attack.p["judge"], JudgeProvider)
        assert isinstance(worker.p["judge"], JudgeProvider)
        assert system.advancer.attack.gate.require_code_review is True
        assert system.advancer.attack.gate.require_result_review is True
        search = system.advancer.attack.p["import_search"]
        assert isinstance(search, ImportTriggerRouter)
        assert isinstance(search.new_structure, ImportSearchService)
        assert isinstance(search.new_structure.provider, GitHubRepoSearchProvider)
        assert isinstance(search.trusted_triggers, TrustedImportTriggerService)
        assert search.trusted_triggers.repo_provider is search.new_structure.provider
        assert isinstance(
            search.trusted_triggers.reference_provider,
            BoundedReferenceSnapshotProvider)
    finally:
        system.close()


def test_production_deployment_cannot_bypass_full_sandbox(tmp_path, monkeypatch):
    """Production 的 trust contract 不能借 reasoning-only/诊断装配绕过。"""
    import copy
    import types
    import orchestrator.run as R

    policy = copy.deepcopy(_POLICY)
    policy["deployment"] = {
        "mode": "production",
        "attestation_path": "/etc/meta-research/deployment.json",
        "max_attestation_age_s": 300,
    }
    policy["execution"]["sandbox"].update({
        "development_gpu_thread_limit": None,
        "local_environment": None,
        "network_mode": "none",
    })
    monkeypatch.setattr(R, "yaml", types.SimpleNamespace(safe_load=lambda _text: policy))
    with pytest.raises(ValueError, match="production deployment.*attack/sandbox"):
        R.build_system(
            SYSTEM_ROOT, str(tmp_path / "work"),
            runner_factory=_mock_factory([]), attack=False)
    assert not (tmp_path / "work").exists()
    with pytest.raises(ValueError, match="不得关闭 instance owner lease"):
        R.build_system(
            SYSTEM_ROOT, str(tmp_path / "unleased"),
            runner_factory=_mock_factory([]), attack=True,
            enforce_instance_lease=False)
    assert not (tmp_path / "unleased").exists()


def test_deployment_preflight_rejects_before_database_and_releases_lease(tmp_path, monkeypatch):
    import orchestrator.run as R
    from orchestrator.deployment_preflight import DeploymentPreflightError
    from orchestrator.instance_lease import InstanceLease

    work = tmp_path / "work"
    calls = []

    class RejectDeployment:
        def __init__(self, **_kwargs):
            calls.append("init")

        def prepare(self):
            calls.append("prepare")
            raise DeploymentPreflightError("deployment rejected")

        def finalize(self, _evidence=None):
            calls.append("forbidden-finalize")

    monkeypatch.setattr(R, "DeploymentPreflight", RejectDeployment)
    monkeypatch.setattr(
        R.ExecutionSupervisor, "recover_previous_generation",
        lambda _self: calls.append("forbidden-recovery"))
    monkeypatch.setattr(
        R.DockerExecutionSandbox, "recover_terminal_sessions",
        lambda _self, _supervisor: calls.append("forbidden-sandbox-recovery"))
    monkeypatch.setattr(
        R._db, "connect",
        lambda _path: (_ for _ in ()).throw(AssertionError("DB must not open")))
    with pytest.raises(DeploymentPreflightError, match="deployment rejected"):
        R.build_system(SYSTEM_ROOT, str(work), runner_factory=_mock_factory([]))
    assert calls == ["init", "prepare"]

    replacement = InstanceLease.acquire(work, heartbeat_interval_s=0.02)
    assert replacement.close() is None


@pytest.mark.parametrize(
    ("deployment_mode", "gpu_ready", "resource_mode", "limits_ready",
     "promoted", "canary_raises"), [
        ("development", False, "cgroup-v2", True, False, False),
        ("development", True, "cgroup-v2", True, True, False),
        # Development may use the exact canary-proved allocation while the
        # existing CPU sandbox remains on its explicit RLIMIT fallback.
        ("development", True, "rlimit-fallback", False, True, False),
        ("development", True, "cgroup-v2", False, True, False),
        ("development", True, "cgroup-v2", True, False, True),
        # Production retains its aggregate cgroup/resource-limit boundary.
        ("production", True, "cgroup-v2", True, True, False),
        ("production", True, "rlimit-fallback", False, False, False),
        ("production", True, "cgroup-v2", False, False, False),
    ])
def test_gpu_canary_runs_only_after_identity_gate_and_recovery(
        tmp_path, monkeypatch, deployment_mode, gpu_ready, resource_mode,
        limits_ready, promoted, canary_raises):
    import copy
    import types
    import orchestrator.run as R

    calls = []
    contract = {
        "version": 1, "provider": "nvidia", "driver_version": "535.129.03",
        "request": {
            "driver": "nvidia",
            "capabilities": ["compute", "utility", "gpu"], "options": {},
        },
        "devices": [{
            "uuid": "GPU-a", "model": "NVIDIA A100",
            "memory_bytes": 80 * 1024 ** 3, "compute_capability": "8.0",
        }],
    }
    candidate = {
        "gpu_contract": contract, "candidate_hash": "sha256:" + "a" * 64,
        "facts": {"docker": {"daemon": {
            # A proxy can honour the exact DeviceRequest without advertising a
            # separately named nvidia runtime.  The canary, not this list, is
            # the execution proof.
            "runtimes": ["runc"]}}},
    }

    class CanaryFailed(RuntimeError):
        pass

    class FakeSandbox:
        def __init__(self, *, config, gpu_contract=None, system_root=None, **_kwargs):
            calls.append("gpu-sandbox-init" if gpu_contract else "base-sandbox-init")
            self.config = dict(config)
            self.gpu_contract = gpu_contract
            self.system_root = system_root
            self.resource_mode = resource_mode
            self.environment_hash = sandbox_environment_hash(self.config)
            self.backend_name = (
                "local-conda" if deployment_mode == "development" else "docker")
            self.image_environment = {
                "PYTHON_VERSION": _POLICY["import_materialization"]["compiler"]["version"],
                "PYTHON_SHA256": _POLICY["import_materialization"]["compiler"][
                    "artifact_sha256"].removeprefix("sha256:"),
            }

        def preflight(self):
            calls.append("gpu-sandbox-preflight" if self.gpu_contract else "base-preflight")

        def recover_terminal_sessions(self, _supervisor):
            calls.append("sandbox-recovery")

        def run_gpu_canary(self, **_kwargs):
            calls.append("gpu-canary")
            if canary_raises:
                raise CanaryFailed("mechanical canary failed")
            return {"mechanical": "evidence"}

    class FakeDeployment:
        def __init__(self, **_kwargs):
            calls.append("identity-init")

        def prepare(self):
            calls.append("identity-prepare")
            return candidate

        def finalize(self, evidence):
            assert evidence == (
                None if deployment_mode == "development" or canary_raises
                else {"mechanical": "evidence"})
            calls.append("deployment-finalize")
            return {
                "mode": deployment_mode,
                "production_ready": deployment_mode == "production" and promoted,
                "checks": [
                    {"name": "gpu_inventory", "ok": gpu_ready},
                    {"name": "gpu_device_runtime", "ok": gpu_ready},
                    {"name": "sandbox_gpu_access", "ok": gpu_ready},
                    {"name": "docker_cgroup",
                     "ok": resource_mode in {"cgroup-v1", "cgroup-v2"}},
                    {"name": "docker_resource_limits", "ok": limits_ready},
                ],
            }

    class DownstreamReached(RuntimeError):
        pass

    def stop_downstream(**_kwargs):
        bootstrap = _kwargs["bootstrap_sandbox"]
        calls.append("downstream-gpu" if bootstrap.gpu_contract else "downstream-cpu")
        raise DownstreamReached()

    def stop_repository(**_kwargs):
        # Local development intentionally skips the Docker wheel-image builder;
        # repository materialization is the first shared downstream boundary.
        calls.append("downstream-gpu")
        raise DownstreamReached()

    policy = copy.deepcopy(_POLICY)
    policy["resources"].update({"gpus": 1, "gpu_mem_gb": 80})
    policy["deployment"]["mode"] = deployment_mode
    if deployment_mode == "production":
        # FakeDeployment owns this unit's identity boundary; the path only
        # keeps the complete policy profile schema-valid.
        policy["deployment"]["attestation_path"] = str(
            tmp_path / "deployment-attestation.json")
        policy["execution"]["sandbox"].update({
            "development_gpu_thread_limit": None,
            "local_environment": None,
            "network_mode": "none",
        })
    monkeypatch.setattr(
        R, "yaml", types.SimpleNamespace(safe_load=lambda _text: policy))
    monkeypatch.setattr(R, "DockerExecutionSandbox", FakeSandbox)
    monkeypatch.setattr(R, "LocalExecutionSandbox", FakeSandbox)
    monkeypatch.setattr(R, "DeploymentPreflight", FakeDeployment)
    monkeypatch.setattr(R, "PythonWheelImageBuilder", stop_downstream)
    monkeypatch.setattr(R, "GitHubRepositoryMaterializer", stop_repository)
    monkeypatch.setattr(
        R.ExecutionSupervisor, "recover_previous_generation",
        lambda _self: calls.append("supervisor-recovery"))

    expected_error = (
        CanaryFailed
        if deployment_mode == "production" and canary_raises
        else DownstreamReached)
    with pytest.raises(expected_error):
        R.build_system(
            SYSTEM_ROOT, str(tmp_path / "work"), runner_factory=_mock_factory([]))
    expected = [
        "base-sandbox-init", "base-preflight", "identity-init", "identity-prepare",
        "supervisor-recovery", "sandbox-recovery", "gpu-sandbox-init",
        "gpu-sandbox-preflight",
    ]
    if deployment_mode == "development":
        expected.extend(["deployment-finalize", "downstream-gpu"])
    else:
        expected.append("gpu-canary")
        expected.append("deployment-finalize")
        if not canary_raises:
            expected.append("downstream-gpu" if promoted else "downstream-cpu")
    assert calls == expected


def test_attack_assembly_accepts_deterministic_readonly_search_provider(tmp_path):
    class RepoSearch:
        name = "github_rest_v1"

        def search(self, *, query, max_candidates):
            raise AssertionError("assembly must not search eagerly")

    provider = RepoSearch()
    system = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]),
        import_search_provider=provider)
    try:
        router = system.advancer.attack.p["import_search"]
        assert router.new_structure.provider is provider
        assert router.trusted_triggers.repo_provider is provider
        assert system.daemon.query_one(
            "SELECT count(*) FROM runner_call WHERE phase='import_search'")[0] == 0
    finally:
        system.close()


def test_build_and_run_bootstrap_terminate(tmp_path):
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    ids = sys.run(max_cycles=5)
    assert len(ids) == 1                                        # bootstrap 一轮 + terminate 停机
    # 真组件落库
    assert sys.daemon.query_one("SELECT count(*) FROM goal")[0] == 1
    assert sys.daemon.query_one("SELECT status FROM cycle WHERE id=?", (int(ids[0][1:]),))[0] == "done"
    assert sys.daemon.query_one("SELECT count(*) FROM question WHERE text LIKE '根问题%'")[0] == 1
    # StatusPublisher 端到端：阶段边界发布了卡
    card = tmp_path / "state" / "status_card.json"
    assert card.exists() and json.loads(card.read_text())["snapshot_cycle"] == ids[0]
    assert (tmp_path / "research.sqlite").exists()             # 真冻结库落盘
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "research.sqlite").stat().st_mode) == 0o600
    # 生产装配端到端：同一 terminal cycle 恰一 SQLite backup + runtime views Git + manifest。
    manifest = _storage_manifest(tmp_path, ids[0])
    assert manifest["cycle_id"] == ids[0] and manifest["cycle_status"] == "done"
    assert {path.name for path in (tmp_path / "views").iterdir()} == {
        ".git", "goal.md", "tree.md", "pool.md", "digest.md"}
    assert (tmp_path / manifest["backup"]["path"]).is_file()


def test_production_assembly_uses_codex_query_responder_and_drains_on_exit(tmp_path):
    """生产 build_system 不再装模板：query 走独立 runner、interaction_query 账本并在研究到上限后收口。"""
    from orchestrator.console_spool import ConsoleSpool

    boot = {
        "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "root"}]},
        "selection.json": {
            "next_question_id": "root", "next_intent": "decompose",
            "scores": [{"question_id": "root", "score": 0.8, "est_cost": 2.0}],
        },
    }
    finish = {
        "tree_ops.json": {"ops": [{
            "op": "add_children", "parent_question_id": "q1",
            "children": [{"local_key": "a", "text": "子问题 A"},
                         {"local_key": "b", "text": "子问题 B"}],
        }]},
        "selection.json": {
            "next_question_id": None, "next_intent": "terminate",
            "scores": [{"question_id": "a", "score": 0.4, "est_cost": 1.0},
                       {"question_id": "b", "score": 0.3, "est_cost": 1.0}],
            "terminate_reason_md": "装配测试收口",
        },
    }
    research = iter([boot, finish])
    calls = []

    def factory(_transcripts, purpose):
        class Runner(_PersistentQueryTestRunner):

            def __init__(self):
                super().__init__(tmp_path)

            def run_task(self, *, system_prompt, skill, context_pack):
                calls.append(purpose)
                if purpose == "interaction-query":
                    return self.query_artifact(
                        context_pack=context_pack,
                        answer="当前已发布到快照 c1。",
                        usage=CallUsage(tokens_total=17, tokens_known=True))
                return Artifact(
                    stage=context_pack.stage, files=next(research), md="",
                    usage=CallUsage(tokens_known=True))
        return Runner()

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=factory, attack=False)
    assert system.run(max_cycles=1) == ["c1"]                  # 先有一份可答的发布卡
    ConsoleSpool(tmp_path).append({"connector": "console", "raw_text": "现在进展如何"})
    assert system.run_forever(max_cycles=1, poll_interval_s=0.01,
                              linger_after_terminal=False) == ["c2"]

    message_id = system.daemon.query_one(
        "SELECT id FROM interaction_message WHERE connector='console' ORDER BY id DESC LIMIT 1")[0]
    reply = system.daemon.query_one(
        "SELECT responder_kind,runner_call_id,snapshot_cycle FROM interaction_reply WHERE message_id=?",
        (message_id,))
    assert reply[0] == "codex" and reply[2] == 1
    assert system.daemon.query_one(
        "SELECT phase,status,purpose FROM runner_call WHERE id=?", (reply[1],)) == (
            "interaction_query", "success", f"message:{message_id}")
    assert system.daemon.query_one(
        "SELECT tokens_total FROM ledger WHERE runner_call_id=?", (reply[1],)) == (17,)
    assert calls.count("interaction-query") == 1


def test_interaction_pump_answers_query_while_research_runner_is_blocked(tmp_path):
    """Query arriving after a long research call starts is answered before that call returns."""
    import threading
    import time
    from orchestrator.console_spool import ConsoleSpool

    boot = {
        "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题", "local_key": "root"}]},
        "selection.json": {
            "next_question_id": "root", "next_intent": "decompose",
            "scores": [{"question_id": "root", "score": 0.8, "est_cost": 1.0}],
        },
    }
    finish = {
        "tree_ops.json": {"ops": [{
            "op": "add_children", "parent_question_id": "q1",
            "children": [{"local_key": "child", "text": "子问题"}],
        }]},
        "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                           "terminate_reason_md": "done"},
    }
    research_started = threading.Event()
    release_research = threading.Event()
    research_calls = {"n": 0}

    def factory(_transcripts, purpose):
        class Runner(_PersistentQueryTestRunner):

            def __init__(self):
                super().__init__(tmp_path)

            def run_task(self, *, system_prompt, skill, context_pack):
                if purpose == "interaction-query":
                    return self.query_artifact(
                        context_pack=context_pack,
                        answer="长调用仍在进行，当前可见快照为 c1。",
                        usage=CallUsage(tokens_total=7, tokens_known=True))
                research_calls["n"] += 1
                if research_calls["n"] == 1:
                    files = boot
                else:
                    research_started.set()
                    assert release_research.wait(3)
                    files = finish
                return Artifact(stage=context_pack.stage, files=files, md="",
                                usage=CallUsage(tokens_known=True))
        return Runner()

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=factory, attack=False)
    assert system.run(1) == ["c1"]
    observed = {}

    def append_and_observe():
        assert research_started.wait(2)
        ConsoleSpool(tmp_path).append({"connector": "console", "raw_text": "长调用期间进展？"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            reply = system.daemon.query_one(
                "SELECT r.runner_call_id,rc.status FROM interaction_reply r "
                "JOIN runner_call rc ON rc.id=r.runner_call_id "
                "WHERE rc.phase='interaction_query' ORDER BY r.id DESC LIMIT 1")
            if reply is not None:
                observed["reply"] = reply
                break
            time.sleep(0.01)
        release_research.set()

    observer = threading.Thread(target=append_and_observe)
    observer.start()
    assert system.run(1) == ["c2"]
    observer.join(2)
    assert not observer.is_alive()
    reply = observed.get("reply")
    assert reply is not None and reply[1] == "success"
    assert system.daemon.query_one(
        "SELECT tokens_total FROM ledger WHERE runner_call_id=?", (reply[0],)) == (7,)


def test_global_stop_keeps_query_sideband_available(tmp_path):
    """研究 durable stop 在 Advancer precheck 之前返回；System 层仍须 ingest/回答新 query。"""
    import threading
    import time
    from orchestrator.console_spool import ConsoleSpool

    research = iter([_BOOT_TERMINATE])
    calls = []

    def factory(_transcripts, purpose):
        class Runner(_PersistentQueryTestRunner):

            def __init__(self):
                super().__init__(tmp_path)

            def run_task(self, *, system_prompt, skill, context_pack):
                calls.append(purpose)
                if purpose == "interaction-query":
                    return self.query_artifact(
                        context_pack=context_pack,
                        answer="研究已停止，最后可见快照为 c1。",
                        usage=CallUsage(tokens_total=5, tokens_known=True))
                return Artifact(
                    stage=context_pack.stage, files=next(research), md="",
                    usage=CallUsage(tokens_known=True))
        return Runner()

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=factory, attack=False)
    assert system.run(1) == ["c1"]
    with system.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO decision(actor,type,payload_json) VALUES "
            "('orchestrator','global_stop','{\"reason\":\"score_floor\"}')")
    stop_event = threading.Event()
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault(
        "ids", system.run_forever(max_cycles=1, poll_interval_s=0.01,
                                  stop_event=stop_event)))
    thread.start()
    time.sleep(0.05)
    assert thread.is_alive(), "durable stop 后 interaction daemon 应保持长在线"
    ConsoleSpool(tmp_path).append({"connector": "console", "raw_text": "停止后还能查状态吗"})
    deadline = time.monotonic() + 2
    while (system.daemon.query_one(
            "SELECT 1 FROM interaction_reply WHERE responder_kind='codex' LIMIT 1") is None
           and time.monotonic() < deadline):
        time.sleep(0.01)
    stop_event.set()
    thread.join(2)
    assert not thread.is_alive() and result["ids"] == []
    assert system.last_stop_reason == "score_floor"
    assert calls.count("interaction-query") == 1
    assert system.daemon.query_one(
        "SELECT responder_kind FROM interaction_reply ORDER BY id DESC LIMIT 1") == ("codex",)


def test_build_system_validates_policy_before_opening_database(tmp_path, monkeypatch):
    """生产入口须执行 schema，并补拒 YAML 可表达但非 JSON number 的 NaN。"""
    import orchestrator.run as R
    raw = (Path(SYSTEM_ROOT) / "policies" / "policy.yaml").read_text(encoding="utf-8")
    base = R.yaml.safe_load(raw)

    missing = {
        **base,
        "budget": {
            **{k: v for k, v in base["budget"].items()
               if k != "price_per_1k_tokens"},
            # price is deliberately optional only while the cumulative budget
            # safety net is disabled.  Exercise its conditional requirement.
            "session_max": 100000,
        },
    }
    monkeypatch.setattr(
        R, "yaml", types.SimpleNamespace(safe_load=lambda _text: missing))
    with pytest.raises(ValidationError, match="price_per_1k_tokens"):
        R.build_system(SYSTEM_ROOT, str(tmp_path / "missing"), runner_factory=_mock_factory([]))
    assert not (tmp_path / "missing").exists()

    nonfinite = {**base, "budget": {**base["budget"], "price_per_1k_tokens": float("nan")}}
    monkeypatch.setattr(
        R, "yaml", types.SimpleNamespace(safe_load=lambda _text: nonfinite))
    with pytest.raises(ValueError, match="非有限数字"):
        R.build_system(SYSTEM_ROOT, str(tmp_path / "nan"), runner_factory=_mock_factory([]))
    assert not (tmp_path / "nan").exists()

    overflow = {**base, "budget": {**base["budget"], "session_max": 10 ** 10000}}
    monkeypatch.setattr(
        R, "yaml", types.SimpleNamespace(safe_load=lambda _text: overflow))
    with pytest.raises(ValueError, match="session_max"):
        R.build_system(SYSTEM_ROOT, str(tmp_path / "overflow"), runner_factory=_mock_factory([]))
    assert not (tmp_path / "overflow").exists()


def test_system_budget_crossing_stops_cleanly_without_committing_inflight_cycle(tmp_path, monkeypatch):
    """BudgetExhausted 在 run_cycles 阶段边界转成干净停；账/stop durable，在途 reasoning 不误提交。"""
    import orchestrator.run as R
    raw = (Path(SYSTEM_ROOT) / "policies" / "policy.yaml").read_text(encoding="utf-8")
    policy = R.yaml.safe_load(raw)
    policy = {**policy, "budget": {**policy["budget"], "session_max": 0.1,
                                    "price_per_1k_tokens": 0.3}}
    import types
    monkeypatch.setattr(R, "yaml", types.SimpleNamespace(safe_load=lambda text: policy))
    calls = {"n": 0}

    class CostedRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            calls["n"] += 1
            return Artifact(stage=context_pack.stage, files=_BOOT_TERMINATE, md="",
                            usage=CallUsage(tokens_total=1000, tokens_known=True))

    sys = R.build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=lambda td, pt: CostedRunner())
    assert sys.run(max_cycles=5) == []
    assert sys.last_stop_reason == "budget_exhausted" and calls["n"] == 1
    assert sys.daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] != "done"
    assert sys.daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 1
    assert sys.daemon.query_one("SELECT COUNT(*) FROM decision WHERE type='global_stop'")[0] == 1


def test_system_unknown_usage_durably_stops_without_retry(tmp_path, monkeypatch):
    """CLI 用量汇总未知时不得冒充真 0：落 durable stop，当前游标不提交/不重调。"""
    _use_budgeted_policy(monkeypatch)
    calls = {"n": 0}

    class UnknownUsageRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            calls["n"] += 1
            return Artifact(stage=context_pack.stage, files=_BOOT_TERMINATE, md="", usage=None)

    sys = build_system(SYSTEM_ROOT, str(tmp_path),
                       runner_factory=lambda td, pt: UnknownUsageRunner())
    assert sys.run(max_cycles=5) == []
    assert calls["n"] == 1 and sys.last_stop_reason == "cost_accounting_failed"
    assert sys.daemon.query_one("SELECT COUNT(*) FROM ledger")[0] == 0
    assert sys.daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] != "done"
    assert sys.daemon.query_one(
        "SELECT json_extract(payload_json,'$.reason') FROM decision WHERE type='global_stop'") == (
            "cost_accounting_failed",)


def test_resume_same_work_root_no_goal_recreate(tmp_path):
    """重启同 work_root 续跑：goal 不重建（幂等）、上轮 terminate → 本次 0 轮。"""
    sys1 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    cycle_id = sys1.run(5)[0]
    first_manifest = _storage_manifest(tmp_path, cycle_id)
    assert sys1.close() is None
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))   # 无需再调 runner
    assert sys2.run(max_cycles=5) == []                        # 已 terminate，无新轮
    assert sys2.daemon.query_one("SELECT count(*) FROM goal")[0] == 1   # goal 唯一（未重建）
    assert _storage_manifest(tmp_path, cycle_id) == first_manifest       # 0 新 backup / 0 新 commit


def test_offline_restored_db_starts_as_honest_adoption_workroot(tmp_path):
    source = tmp_path / "source"
    restored = tmp_path / "restored"
    sys1 = build_system(
        SYSTEM_ROOT, str(source), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    assert sys1.run(1) == ["c1"]
    assert sys1.close() is None

    lease = InstanceLease.acquire(source)
    try:
        receipt = SnapshotArchive(
            work_root=source, lease=lease).restore(target=restored)
    finally:
        assert lease.close() is None
    assert receipt["source_cycle"] == "c1"
    sys2 = build_system(
        SYSTEM_ROOT, str(restored), runner_factory=_mock_factory([]))
    assert sys2.run(1) == []
    adopted = _storage_manifest(restored, "c1")
    assert adopted["adoption_baseline"] is True
    assert adopted["bootstrap_before_cycle"] == 0
    assert sys2.close() is None


def test_startup_recovers_budget_stop_before_missing_terminal_snapshot(tmp_path, monkeypatch):
    """terminal commit 后、轮后 stop/snapshot 前崩溃：重启恢复的 stop 必须进同一 recovery point。"""
    _use_budgeted_policy(monkeypatch)
    sys1 = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    sys1.advancer.storage_reconciler = None       # 模拟 terminal commit 后未及发布
    assert sys1.run(1) == ["c1"]
    assert not (tmp_path / "state" / "storage" / "cycles" / "c1.json").exists()
    runner_call_id = sys1.daemon.query_one(
        "SELECT id FROM runner_call WHERE cycle_id=1 ORDER BY id DESC LIMIT 1")[0]
    with sys1.daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO ledger(cycle_id,phase,runner_call_id,money,policy_version) "
            "VALUES (1,'reasoning',?,100000,'v0')", (runner_call_id,))
    assert sys1.daemon.query_one(
        "SELECT count(*) FROM decision WHERE type='global_stop'") == (0,)
    assert sys1.close() is None

    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    assert sys2.daemon.query_one(
        "SELECT json_extract(payload_json,'$.reason') FROM decision "
        "WHERE type='global_stop'") == ("budget_exhausted",)
    manifest = _storage_manifest(tmp_path, "c1")
    backup = sqlite3.connect(
        f"file:{tmp_path / manifest['backup']['path']}?mode=ro", uri=True)
    try:
        assert backup.execute(
            "SELECT json_extract(payload_json,'$.reason') FROM decision "
            "WHERE type='global_stop'").fetchone() == ("budget_exhausted",)
    finally:
        backup.close()
    assert sys2.run(max_cycles=1) == []
    assert sys2.close() is None


# ============ 注入组件端到端接线 ============
def test_durable_stop_honored_end_to_end(tmp_path):
    """StopController 端到端：预置 global_stop → run() 启动即拒推进（provider 一次未调）。"""
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    with sys.daemon.transaction() as conn:
        conn.execute("INSERT INTO decision(actor,type,payload_json) VALUES "
                     "('orchestrator','global_stop','{\"reason\":\"budget_exhausted\"}')")
    assert sys.run(max_cycles=5) == []
    assert sys.last_stop_reason == "budget_exhausted"


def test_tau_score_floor_self_stop_end_to_end(tmp_path):
    """外审 SHOULD 回归：τ 判据①（分数衰退）经 run.py→run_cycles 的**轮后 check_after_round** 端到端
    自终止——bootstrap 造低分根+选 decompose（本会续跑），前沿全评分且 < floor → 停。"""
    boot_low = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "低价值根", "local_key": "root"}]},
                "selection.json": {"next_question_id": "root", "next_intent": "decompose",
                                   "scores": [{"question_id": "root", "score": 0.1, "est_cost": 1.0}]}}
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([boot_low]))
    sys.advancer.stop_controller.score_floor = 0.25            # 收紧 tau 到单轮即触发（测试注入）
    sys.advancer.stop_controller.consecutive_rounds = 1
    ids = sys.run(max_cycles=5)
    assert len(ids) == 1 and sys.last_stop_reason == "score_floor"   # 第 1 轮后自停、decompose 轮不开
    assert sys.daemon.query_one("SELECT count(*) FROM decision WHERE type='global_stop'")[0] == 1
    # snapshot 必须排在 check_after_round 后，故恢复点已包含本轮写下的 durable global_stop。
    manifest = _storage_manifest(tmp_path, ids[0])
    backup = sqlite3.connect(
        f"file:{tmp_path / manifest['backup']['path']}?mode=ro", uri=True)
    try:
        assert backup.execute(
            "SELECT count(*) FROM decision WHERE type='global_stop'").fetchone() == (1,)
    finally:
        backup.close()


def test_goal_body_from_db_not_edited_brief(tmp_path, monkeypatch):
    """重启后 compiler 在读快照内按 cycle 的精确 goal version 取正文。"""
    sys1 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([_BOOT_TERMINATE]))
    sys1.run(5)
    db_goal = sys1.daemon.query_one("SELECT text FROM goal WHERE id=1")[0]
    assert sys1.close() is None
    # 重启：即便 parse_goal_brief 返回被"编辑过"的 body，装配也用 DB 正文
    import orchestrator.run as R
    monkeypatch.setattr(R, "parse_goal_brief", lambda p: {"body_md": "【被篡改的目标】", "predicate_json": {},
                                                          "frontmatter": {}})
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    pack = sys2.advancer.compiler.render(cycle_id="c1", stage="reasoning")
    assert db_goal in pack.anchor_md
    assert "篡改" not in pack.anchor_md
    assert "db:goal:1:v1" in pack.sources


def test_main_cli_smoke(tmp_path, monkeypatch, capsys):
    """main() argparse→build→run→print 全路径（注入 mock runner，不调真 Codex）。"""
    import orchestrator.run as R
    # 注：不 monkeypatch CodexRunner.__new__——它继承自 object，patch 后 monkeypatch 会把 object.__new__ 显式绑到类上、
    # 使之后 CodexRunner(**kw) 构造抛 TypeError（污染全局）。真 runner 不构造已由下方 mock build_system 保证。
    # 用 build_system 的注入点：monkeypatch build_system 塞 mock runner
    orig = R.build_system
    monkeypatch.setattr(R, "build_system",
                        lambda sr, wr, **kw: orig(sr, wr, runner_factory=_mock_factory([_BOOT_TERMINATE])))
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
                 "--max-cycles", "3", "--once", "--no-outbound"])
    assert rc == 0 and "推进 1 轮" in capsys.readouterr().out


def test_main_cli_attack_clean_error(tmp_path, monkeypatch, capsys):
    """外审 NIT 回归：attack 续轮 NotImplementedError → main 干净报 exit 2（非裸 traceback）。"""
    import orchestrator.run as R
    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack",
                                      "scores": [{"question_id": "root", "score": 0.9, "est_cost": 1.0}]}}
    orig = R.build_system
    monkeypatch.setattr(R, "build_system",           # attack=False：验证退化装配仍干净拒（CP8.4 后 attack 默认全装）
                        lambda sr, wr, **kw: orig(sr, wr, runner_factory=_mock_factory([boot_attack]), attack=False))
    rc = R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
                 "--max-cycles", "3", "--no-outbound"])
    assert rc == 2 and "尚未装配的组件" in capsys.readouterr().out


def test_main_cli_reports_post_claim_fence_without_traceback(tmp_path, monkeypatch, capsys):
    import orchestrator.run as R
    from orchestrator.qualification_firewall import QualificationClaimLockedError

    monkeypatch.setattr(
        R, "build_system",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            QualificationClaimLockedError("qualification claim 已锁定")))

    rc = R.main([
        "--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
        "--once", "--no-outbound",
    ])
    captured = capsys.readouterr()
    assert rc == 2
    assert "启动预检失败" in captured.err
    assert "claim 已锁定" in captured.err


def test_stop_reason_print_prefers_block(tmp_path, monkeypatch, capsys):
    """外审 SHOULD 回归：全局等待时 CLI 停因输出阻断原因（不被 prior-terminate/idle 掩盖）。"""
    import orchestrator.run as R
    def factory(sr, wr, **kw):
        s = build_system(sr, wr, runner_factory=_mock_factory([]))   # 真 build_system + 塞 pending 请求
        with s.daemon.transaction() as conn:
            conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                         "request_hash) VALUES (1,1,'plan','pending','需数据','[]','rh')")
        return s
    monkeypatch.setattr(R, "build_system", factory)
    R.main(["--system-root", SYSTEM_ROOT, "--work-root", str(tmp_path),
            "--max-cycles", "3", "--once", "--no-outbound"])
    assert "文件请求" in capsys.readouterr().out


def test_global_wait_honored_end_to_end(tmp_path):
    """precheck 端到端：pending 文件请求 → run() 不发起新研究推进（provider 一次未调）。"""
    called = {"n": 0}

    def counting_factory(td, pt):
        class R:
            def run_task(self, **kw):
                called["n"] += 1
                return Artifact(stage="reasoning", files=_BOOT_TERMINATE, md="",
                                usage=CallUsage(tokens_known=True))
        return R()
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    with sys.daemon.transaction() as conn:                     # 造 pending 文件请求
        conn.execute("INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,"
                     "request_hash) VALUES (1,1,'plan','pending','需数据','[]','rh')")
    assert sys.run(max_cycles=5) == []
    assert called["n"] == 0                                     # 阻断：一次 runner 都未调
    assert "文件请求" in sys.advancer.last_block_reason


def test_console_backlog_over_one_bounded_batch_blocks_before_later_pause(tmp_path):
    """>4MiB backlog 后的 pause-confirm 尚未 ingest 时，precheck 不得先放行 provider。"""
    from orchestrator.console import Console
    from orchestrator.console_spool import MAX_BATCH_BYTES, MAX_RECORD_BYTES

    called = {"n": 0}

    def counting_factory(_td, _pt):
        class Runner:
            def run_task(self, **_kw):
                called["n"] += 1
                return Artifact(stage="reasoning", files=_BOOT_TERMINATE, md="",
                                usage=CallUsage(tokens_known=True))
        return Runner()

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    console = Console(system.daemon)
    pause = console.handle_inbound(
        connector="console", raw_text="暂停", idempotency_key="seed-backlog-pause")
    # 每行都超过单 record 上限，故会作为可推进 poison；总量刚越过一批，confirm 在下一批。
    oversized = b"x" * (MAX_RECORD_BYTES + 1) + b"\n"
    count = MAX_BATCH_BYTES // len(oversized) + 1
    confirm = json.dumps({
        "connector": "console", "idempotency_key": "post-backlog-confirm",
        "action": "confirm", "directive_id": pause["directive_id"],
        "raw_text": "展示文本不可信",
    }, ensure_ascii=False).encode("utf-8") + b"\n"
    inbox = tmp_path / "state" / "console_inbox.jsonl"
    inbox.write_bytes(oversized * count + confirm)

    assert system.run(max_cycles=1) == []
    assert called["n"] == 0
    # Resident pump may drain both bounded batches before precheck; either way
    # research cannot pass the backlog, and the later pause is already durable.
    assert ("入站待处理" in system.advancer.last_block_reason
            or "pause 指令生效" in system.advancer.last_block_reason)
    first_confirmed = system.daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
        (pause["directive_id"],))[0]
    assert first_confirmed in (0, 1)       # pump/precheck 谁先取第二批取决于调度

    assert system.run(max_cycles=1) == []                      # 下一拍处理 confirm，pause 成为更高优先阻断
    assert called["n"] == 0
    assert "pause" in system.advancer.last_block_reason
    assert system.daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
        (pause["directive_id"],)) == (1,)


def test_retry_at_console_head_blocks_provider_before_following_action(tmp_path):
    """队首 query 尚无 status card 而 retry 时，后置 confirm 未处理也绝不能越过并调用 provider。"""
    from orchestrator.console import Console
    from orchestrator.console_spool import ConsoleSpool

    called = {"n": 0}

    def counting_factory(_td, _pt):
        class Runner:
            def run_task(self, **_kw):
                called["n"] += 1
                return Artifact(stage="reasoning", files=_BOOT_TERMINATE, md="",
                                usage=CallUsage(tokens_known=True))
        return Runner()

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    pause = Console(system.daemon).handle_inbound(
        connector="console", raw_text="暂停", idempotency_key="seed-retry-pause")
    spool = ConsoleSpool(tmp_path)
    spool.append({"connector": "console", "raw_text": "现在进展如何"})
    spool.append({"connector": "console", "raw_text": "展示文本不可信",
                  "action": "confirm", "directive_id": pause["directive_id"]})

    assert system.run(max_cycles=1) == []                      # query 无卡 → retry at head
    assert called["n"] == 0
    assert "人机入站待处理" in system.advancer.last_block_reason
    assert system.daemon.query_one(
        "SELECT json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?",
        (pause["directive_id"],)) == (0,)                     # 后置 action 尚未越过队首


def test_broken_inbound_state_blocks_due_directive_consumption(tmp_path):
    """入站 fail-closed 必须早于 base_precheck；更晚已 ACK 的 reject 未读时不得先消费 pause。"""
    from orchestrator.console import (DIRECTIVE_ACTION_SESSION_REF, Console,
                                      directive_action_text)
    from orchestrator.console_spool import ConsoleSpool

    calls = {"n": 0}

    def counting_factory(_td, _pt):
        class Runner:
            def run_task(self, **_kw):
                calls["n"] += 1
                return Artifact(stage="reasoning", files=_BOOT_TERMINATE, md="",
                                usage=CallUsage(tokens_known=True))
        return Runner()

    first = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    console = Console(first.daemon)
    pause = console.handle_inbound(
        connector="console", raw_text="暂停", idempotency_key="ordered-pause")
    did = pause["directive_id"]
    mid = console.ingest.inbound(
        connector="console", raw_text=directive_action_text("confirm", did),
        idempotency_key="ordered-confirm", session_ref=DIRECTIVE_ACTION_SESSION_REF)
    with first.daemon.transaction() as conn:
        conn.execute("INSERT INTO interaction_classification(message_id,intent,directive_id) "
                     "VALUES (?,'unclear',NULL)", (mid,))
    console.confirm_directive(directive_id=did, confirm_message_id=mid)

    reason = "用户在 pause 生效前撤回"
    ConsoleSpool(tmp_path).append({
        "connector": "console", "action": "reject", "directive_id": did,
        "reason": reason, "raw_text": directive_action_text("reject", did, reason=reason),
    })
    (tmp_path / "state" / ".console_inbox.retry.json").write_text("{broken", encoding="utf-8")
    assert first.close() is None

    # 重启后加载到坏 sidecar；即使 pause 已确认且 immediate due，也必须停在入站顺序闸前。
    second = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=counting_factory)
    assert second.run(max_cycles=1) == []
    assert calls["n"] == 0
    assert "人机入站待处理" in second.advancer.last_block_reason
    assert second.daemon.query_one(
        "SELECT status,json_extract(payload_json,'$.confirmed') FROM directive WHERE id=?", (did,)) == (
            "pending", 1)


def test_production_system_scans_directive_and_file_notifications_on_exit(tmp_path):
    """notifier 不能只存在于单测：System.run 的退出边界须把新状态幂等派生到真实 outbox。"""
    from orchestrator.console import Console
    sys = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    directive = Console(sys.daemon).handle_inbound(
        connector="console", raw_text="暂停", idempotency_key="notify-wire")
    with sys.daemon.transaction() as conn:
        rid = conn.execute(
            "INSERT INTO interaction_request(goal_id,goal_ver,stage,status,summary_md,items_json,request_hash) "
            "VALUES (1,1,'plan','pending','需文件','[]','notify-wire-fr')").lastrowid
    assert sys.run(max_cycles=0) == []
    events = [json.loads(line) for line in (tmp_path / "state" / "outbox.jsonl").read_text().split("\n") if line]
    keys = {e["event_key"] for e in events}
    assert f"directive:{directive['directive_id']}:pending_confirmation:v2" in keys
    assert f"filereq:{rid}:pending" in keys


# ============ CP8.4 · attack 全装配端到端（真子进程执行 + 机械安全门）============
def _lazy_factory(items):
    """runner 工厂：items 元素为 dict（原样吐）或 callable(context_pack)→dict（吐前按当下 DB/staging 现算
    ——bundle 须回引 plan_slice_hash、attack reasoning 须引用真 metric_result id，均只在调用时可知）。"""
    box = {"seq": list(items)}

    class MockRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            item = box["seq"].pop(0)
            files = item(context_pack) if callable(item) else item
            return Artifact(stage=context_pack.stage, files=files, md="",
                            usage=CallUsage(tokens_known=True))
    return lambda td, pt: MockRunner()


def test_legacy_injected_full_attack_cannot_fake_green_dag_replay(
        tmp_path, request):
    """旧注入式 cycle-wide Bundle 可验证领域执行，但不能伪装成新 DAG 绿色闭包。

    新生产路径由 A→(B,C) 集成测试覆盖；这里保留旧脚本化 runner 作为负向兼容测试：
    即使训练、评估、publication 与 Reasoning 都成功，只要缺 Scheduler/Worker/review task
    receipts，terminal replay 必须 fail closed。
    """
    import sys as _sys
    import test_attack_advance as TA
    from orchestrator.manifest import canon_hash

    # This is a real nested-Docker test, not a mocked sandbox test.  The
    # deployment's rootless daemon sees GPFS/VEPFS through an exact bindfs
    # mapping, while pytest's default /tmp lives on the outer overlay.  After a
    # long full-suite run that overlay mapping can lag a freshly populated
    # input snapshot and expose an empty bind to the daemon.  Production quest
    # work roots live on VEPFS, so exercise the same mount domain here instead
    # of making an overlay propagation race look like a bundle failure.
    docker_tmp_parent = Path(SYSTEM_ROOT) / "runtime" / "pytest-docker"
    docker_tmp_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(docker_tmp_parent, 0o700)
    docker_tmp = docker_tmp_parent / f"full-attack-{os.getpid()}-{time.time_ns()}"
    docker_tmp.mkdir(mode=0o700)
    request.addfinalizer(lambda: shutil.rmtree(docker_tmp, ignore_errors=True))
    tmp_path = docker_tmp

    db_path = str(tmp_path / "research.sqlite")
    runtime_env_hash = [RUNTIME_ENV_HASH]

    def bundle_env(pack):                       # 按 pack.target_id 读切片、回引 hash、产真 toy 代码
        conn = db.connect(db_path)
        slice_ = json.loads(conn.execute("SELECT plan_ref FROM build_target WHERE id=?",
                                         (int(pack.target_id),)).fetchone()[0])
        conn.close()
        return {"execution_manifest.json": {
                    "manifest_version": 1,
                    "target_ref": {"target_key": slice_["target_key"], "target_kind": "build",
                                   "seq": slice_["seq"], "plan_slice_hash": canon_hash(slice_)},
                    "protocol_ref": {"protocol_id": slice_["protocol_id"], "protocol_ver": slice_["protocol_ver"]},
                    "env_hash": sandbox_workload_environment_hash(
                        runtime_env_hash[0], True),
                    "gpu_required": True,
                    "config_json": {"lr": 0.1},
                    "code_files": ["train.py", "eval.py", "smoke.py"],
                    "commands": {"smoke": {"argv": ["python", "{src}/smoke.py"]},
                                 "train": {"argv": ["python", "{src}/train.py"]},
                                 "eval": {"argv": ["python", "{src}/eval.py", "{ckpt}"]}},
                    "expected_outputs": {"checkpoint": "ckpt.bin"},
                    "repro_cmd_md": "python train.py 后 python eval.py <ckpt>"},
                "identity.md": "# toy 基线\n结构: 线性\n\n## 复现命令\npython train.py",
                "train.py": TA.TRAIN_OK, "eval.py": TA.EVAL_OK, "smoke.py": TA.SMOKE_OK}

    def attack_reasoning(pack):                 # 以真 metric_result 关问 + terminate
        conn = db.connect(db_path)
        mr = conn.execute("SELECT id FROM metric_result ORDER BY id DESC LIMIT 1").fetchone()[0]
        qid = conn.execute("SELECT active_question_id FROM cycle WHERE id=?",
                           (int(pack.cycle_id[1:]),)).fetchone()[0]
        conn.close()
        return {"answer.json": {"question_id": f"q{qid}", "verdict": "answered",
                                "evidence": [{"kind": "evaluation", "metric_result_id": f"mr{mr}",
                                              "note_md": "toy 基线 acc=0.93"}],
                                "answer_md": "以出厂测量关问"},
                "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                                   "terminate_reason_md": "toy 目标已以真测量关问"}}

    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "toy 基线能到 0.9 吗",
                                              "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack", "scores": []}}
    predicate_plan = TA._plan_json()
    predicate_plan["plan.json"]["targets"][0]["gpu_required"] = True
    # 此端到端用例使用固定 goal_brief.md；其 root success predicate 精确要求
    # toy-gauss-cls@1 / accuracy@1 / aggregate >= 0.9。使计划注册与该谓词同一身份，
    # 才能证明真 metric_result 关问，避免用仅数值相同但协议/指标不同的测量伪闭环。
    predicate_plan["plan.json"]["protocol"]["name"] = "toy-gauss-cls"
    predicate_plan["plan.json"]["metric_defs"][0]["name"] = "accuracy"
    # Production idea now uses the pinned adapter's two-session contract.  Give
    # the generator exactly three WildIdea candidates (9 anchors are internal
    # to the prompt) and the blind judge a separate mapping-only result.
    template = TA._idea_set()["idea_set.json"]["candidates"][1]
    wild_candidates = []
    for index in range(3):
        candidate = json.loads(json.dumps(template))
        candidate["candidate_id"] = f"wild-{index + 1}"
        candidate["novelty_queries"] = [
            f"toy Gaussian classification novelty candidate {index + 1}"]
        candidate["audit_mapping"]["source_domain"] += f"-{index + 1}"
        candidate["wildidea_extra"]["source_prototype"] = f"P0{index + 1} " + (
            candidate["wildidea_extra"]["source_prototype"])
        wild_candidates.append(candidate)
    idea_draft = {"idea_set.draft.json": {
        "need_innovation": True,
        "candidates": wild_candidates,
        "novelty_refs": [],
    }}
    idea_audit = {"idea_audit.json": {
        "audit_scores": [{
            "candidate_id": candidate["candidate_id"],
            "scores": {
                "structural_depth": 8, "domain_distance": 8,
                "applicability": 8, "novelty": 8,
                "unexpectedness": 8 - index, "non_obviousness": 8,
            },
            "decision": "pass", "rationale": "映射系统性与 research 门槛均成立",
        } for index, candidate in enumerate(wild_candidates)],
        "selected_id": "wild-1",
    }}
    review_pass = {
        "review_verdict.json": {
            "verdict": "pass", "issues": [],
            "notes_md": "脚本化独立 reviewer 接受冻结 subject",
        }
    }
    seq = [boot_attack,                          # c1 bootstrap（reasoning）
           idea_draft, idea_audit, predicate_plan,  # c2 attack：生成 → 独立盲审 → plan
           bundle_env,                           # bundle 信封（manifest+代码）
           review_pass,                          # pre-smoke code↔Plan reviewer
           review_pass,                          # post-eval result reviewer
           attack_reasoning]                     # 轮尾：真证据关问 + terminate

    class FakeNoveltyProvider:
        name = "literature_federated_v1"

        def search(self, query, *, policy_hash):
            result_hash = "sha256:" + hashlib.sha256(query.encode()).hexdigest()
            snapshot_hash = "sha256:" + hashlib.sha256(
                ("snapshot\x00" + query).encode()).hexdigest()
            return {
                "final_ref": {
                    "query": query,
                    "provider": self.name,
                    "snapshot_hash": snapshot_hash,
                    "snapshot_ref": (
                        "state/novelty/snapshots/sha256/"
                        + snapshot_hash.removeprefix("sha256:") + ".json"),
                    "raw_content_hash": "sha256:" + "a" * 64,
                    "result_content_hashes": [result_hash],
                    "ranking": [result_hash],
                    "policy_hash": policy_hash,
                },
                "results": [{
                    "rank": 1,
                    "result_content_hash": result_hash,
                    "id": "https://arxiv.org/abs/fixture",
                    "title": "Frozen E2E novelty fixture",
                }],
            }

    sys_ = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory(seq),
        novelty_search_provider=FakeNoveltyProvider())
    runtime_env_hash[0] = sys_.advancer.attack.execution_sandbox.environment_hash
    from orchestrator.cycle_replay import CycleReplayError
    with pytest.raises(CycleReplayError, match="缺 Worker task/receipt identity"):
        sys_.run(max_cycles=6)
    d = sys_.daemon
    # 全链断言：协议真注册 / 池 legal / 真测量 / 真证据关问。注入式测试
    # runner 保留 legacy Idea adapter 的一个 audit turn；默认 Codex 装配则在
    # 主阶段 turn 内启动 child reviewer，并经 runtime MCP 记录。
    assert d.query_one("SELECT count(*) FROM protocol WHERE name='toy-gauss-cls'")[0] == 1
    assert d.query_one("SELECT count(*) FROM metric_def WHERE name='accuracy'")[0] == 1
    assert d.query_one("SELECT status FROM baseline WHERE canonical_key='ck-attack'")[0] == "legal"
    assert d.query_one("SELECT status, eval_key FROM evaluation WHERE source='factory'")[0:2] == ("success", "t1")
    assert d.query_one("SELECT value FROM metric_result ORDER BY id DESC LIMIT 1")[0] == 0.93
    assert d.query_one("SELECT count(*) FROM runner_call WHERE phase='audit' AND status='success'")[0] == 3
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 3
    assert d.query_one("SELECT count(*) FROM decision WHERE type='idea_audit'")[0] == 1
    persisted_idea_audit = json.loads(d.query_one(
        "SELECT audit_json FROM idea WHERE status='selected'")[0])
    assert persisted_idea_audit["candidate_id"] == "wild-1"
    assert persisted_idea_audit["provenance"]["engine_version"] == (
        "wildidea@6ff66ada15b0047b2e03d229f2e9543c542df598")
    assert persisted_idea_audit["wildidea_extra"]["source_prototype"].startswith("P01")
    assert d.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 0
    assert d.query_one("SELECT count(*) FROM decision WHERE type IN "
                       "('bundle_code_review','bundle_result_review')")[0] == 2
    assert d.query_one("SELECT status FROM question WHERE text LIKE 'toy 基线%'")[0] == "answered"
    assert d.query_one("SELECT count(*) FROM build_target WHERE status='complete'")[0] == 1
    assert d.query_one("SELECT count(*) FROM cycle WHERE status='done'")[0] == 2
    # 执行是真子进程：checkpoint 以 work-root-relative 正式池路径登记且文件真实存在。
    ck = d.query_one("SELECT path, content_hash FROM checkpoint")
    assert (tmp_path / ck[0]).exists() and len(ck[1]) == 64
    assert sys_.last_stop_reason is None                         # 正常 terminate（非 τ/阻断）


def test_attack_assembly_optional_off(tmp_path):
    """attack=False 退化装配（诊断用）：遇 attack 续轮仍干净拒（NotImplementedError），不静默。"""
    boot_attack = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
                   "selection.json": {"next_question_id": "root", "next_intent": "attack", "scores": []}}
    sys_ = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory([boot_attack]),
                        attack=False)
    with pytest.raises(NotImplementedError):
        sys_.run(max_cycles=3)


def test_plan_reject_feedback_in_next_pack(tmp_path):
    """CP8.4 自纠环：plan 业务拒后，同一问题下一 attack 轮的 plan pack 含「上轮 plan 被拒原因」
    （冒烟实证：无此反馈真 Codex 连续 3 轮重复同一被拒 plan）。"""
    import test_attack_advance as TA
    from orchestrator.advancer import SqliteAdvancer
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w")
    TA._bootstrap_attack(state)

    def exec_plan(cyc, pack):                    # 被拒的 plan（exec 目标 CP8.6 未接）
        p = TA._plan_json()["plan.json"]
        p["targets"][0]["target_kind"] = "exec"
        p["targets"][0]["claim"] = {"baseline_ref": "b1", "variant_key": "v2", "config_json": {"lr": 1}}
        return {"plan.json": p}
    attack.p["plan"] = exec_plan
    attack.p["reasoning"] = lambda c, pk: {      # 拒后收尾：继续攻同一问题
        "selection.json": {"next_question_id": TA_root_qid(daemon), "next_intent": "attack", "scores": []}}
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=1)
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    # 下一轮同问题的 plan pack：拒因在锚区
    c2 = state.open_or_resume_cycle()
    state.set_route(c2.cycle_id, "attack")
    state.activate_question(TA_root_qid(daemon))
    pack = compiler.render(cycle_id=c2.cycle_id, stage="plan")
    assert "最近一次 plan 被拒原因" in pack.anchor_md and "legal baseline" in pack.anchor_md
    daemon.conn.close()


def TA_root_qid(daemon):
    return f"q{daemon.query_one('SELECT id FROM question ORDER BY id LIMIT 1')[0]}"


def test_plan_reject_feedback_suppressed_after_success(tmp_path):
    """codex SHOULD 回归：拒因之后本问题已有成功 plan（更晚 cycle 落过 build_target）→ 反馈不再渲染
    （陈旧拒因会在 CP8.6 后把本已合法的 exec/eval 引导走偏）。"""
    import test_attack_advance as TA
    from orchestrator.advancer import SqliteAdvancer
    path = str(tmp_path / "research.sqlite")
    daemon, state, compiler, attack = TA._mk_env(path, tmp_path / "w")
    TA._bootstrap_attack(state)
    box = {"n": 0}
    real_plan = attack.p["plan"]

    def flip_plan(cyc, pack):                    # 第 1 轮产被拒 plan（exec），第 2 轮产合法 build plan
        box["n"] += 1
        if box["n"] == 1:
            p = TA._plan_json()["plan.json"]
            p["targets"][0]["target_kind"] = "exec"
            p["targets"][0]["claim"] = {"baseline_ref": "b1", "variant_key": "v2", "config_json": {"lr": 1}}
            return {"plan.json": p}
        return real_plan(cyc, pack)
    attack.p["plan"] = flip_plan
    rq = TA_root_qid(daemon)
    sels = iter([{"selection.json": {"next_question_id": rq, "next_intent": "attack", "scores": []}},
                 {"answer.json": None, "selection.json": {"next_question_id": None, "next_intent": "terminate",
                                                          "scores": [], "terminate_reason_md": "done"}}])
    def reasoning(c, pk):
        files = dict(next(sels))
        if files.get("answer.json") is None:
            files.pop("answer.json", None)
        return files
    attack.p["reasoning"] = reasoning
    SqliteAdvancer(state, compiler, lambda c, p: None, attack=attack).run_cycles(max_cycles=3)
    assert daemon.query_one("SELECT count(*) FROM decision WHERE type='plan_rejected'")[0] == 1
    assert daemon.query_one("SELECT count(*) FROM build_target")[0] == 1     # 第 2 轮成功 plan 落了 target
    c3 = state.open_or_resume_cycle()
    state.set_route(c3.cycle_id, "attack")
    state.activate_question(rq)
    pack = compiler.render(cycle_id=c3.cycle_id, stage="plan")
    assert "plan 被拒原因" not in pack.anchor_md                # 已有更晚成功 plan → 反馈静默
    daemon.conn.close()


# ============ CP8.5 · sidecar→file_request 全等待环（E2E 经 run.py 装配）============
_SIDECAR_REQ = {"summary_md": "需要 EEG 数据集", "items": [{
    "kind": "dataset", "desc": "EEG 原始数据", "expected_files": ["eeg.zip"],
    "attempted_paths": ["/data/eeg"], "failure_reason": "无读取权限", "dest_hint": "input/user_provided/"}]}


def test_file_request_wait_loop_end_to_end(tmp_path):
    """全等待环：阶段发 sidecar → 请求单落库 + run 干净停（在途轮保持游标）→ 再 run 被 precheck 全局
    等待阻断 → 用户 resolve → 再 run 续跑同一阶段成功。"""
    from orchestrator.interaction import InteractionIngest
    from orchestrator.notify import FileRequestService
    from orchestrator.schemas import SchemaSet
    import yaml as _yaml

    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "创世即止"}}
    # 第一次 run：reasoning 阶段发 sidecar → 阻断
    sys1 = build_system(SYSTEM_ROOT, str(tmp_path),
                        runner_factory=_lazy_factory([{**boot, "resource_request.json": _SIDECAR_REQ}]))
    assert sys1.run(max_cycles=3) == []                          # 零轮完成（在途轮保持游标）
    assert "文件请求" in sys1.advancer.last_block_reason
    rid = sys1.daemon.query_one("SELECT id FROM interaction_request WHERE status='pending'")[0]
    assert sys1.daemon.query_one("SELECT status FROM cycle ORDER BY id DESC LIMIT 1")[0] == "created"
    assert sys1.close() is None

    # 第二次 run（同 work root）：precheck 全局等待阻断（provider 一次都不调）
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory([]))
    assert sys2.run(max_cycles=3) == []
    assert f"#{rid}" in sys2.advancer.last_block_reason

    # 用户 resolve 真文件 → 第三次 run 的真实 compiler/provider pack 必须看见 opaque asset 回执，
    # 不能只靠“pending 清掉 + fake runner 凭空成功”的假闭环。
    mid = InteractionIngest(sys2.daemon).inbound(connector="qq", raw_text="数据给不了，先跑",
                                                 idempotency_key="fr-1", goal_id=1, goal_ver=1)
    policy = _yaml.safe_load((Path(SYSTEM_ROOT) / "policies" / "policy.yaml").read_text(encoding="utf-8"))
    frs = FileRequestService(sys2.daemon, SchemaSet(Path(SYSTEM_ROOT) / "schemas"), policy,
                             input_root=str(tmp_path / "input"))
    up = tmp_path / "uploads"; (up / "1").mkdir(parents=True)
    (up / "1" / "eeg-user-name.zip").write_bytes(b"EEG-USER-DATA")
    resolved = frs.resolve(request_id=rid, uploads_dir=str(up), resolved_message_id=mid)
    asset = resolved["resolution"][0]["provided"][0]
    assert sys2.close() is None

    def finish_after_resource(pack):
        assert pack.refs == [f"user-file-request:r{rid}:item:1:asset:1"]
        assert "用户文件输入资产回执（非 evidence）" in pack.anchor_md
        assert asset["hash"] in pack.anchor_md
        assert "EEG-USER-DATA" in pack.anchor_md and "untrusted_non_evidence" in pack.anchor_md
        assert "eeg-user-name.zip" not in pack.anchor_md              # 外部文件名不进入 prompt
        return boot

    sys3 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory([finish_after_resource]))
    ids = sys3.run(max_cycles=3)
    assert len(ids) == 1                                         # 同一在途轮续跑完成
    assert sys3.daemon.query_one("SELECT count(*) FROM question")[0] == 1


def test_resident_build_system_ingests_spooled_file_action_and_resumes(tmp_path):
    """真实常驻拓扑：阶段阻断→受管发布入 spool→单写 resolve→自动续同阶段。

    ``source_ref`` is a private server capability.  Browser HTTP cannot
    submit it; the managed-upload publisher calls the internal spool method
    only after publication verification.
    """
    import threading
    from orchestrator import console_server as CS

    boot = {"tree_ops.json": {"ops": [{"op": "create_root", "text": "根", "local_key": "root"}]},
            "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                               "terminate_reason_md": "创世即止"}}
    upload = tmp_path / "uploads" / "r1" / "1"
    upload.mkdir(parents=True)
    (upload / "eeg-user-name.zip").write_bytes(b"RESIDENT-EEG-DATA")
    appended = threading.Event()
    response = {}
    token = "d" * 64

    def enqueue_resolve():
        response["queued"] = console_data.enqueue_file_request_action(
            action="resolve", request_id=1, source_ref="work/uploads/r1",
            client_idempotency_key="e" * 32)
        appended.set()

    def request_resource(_pack):
        # runner 返回 sidecar 后 provider 才建 r1；稍后到达的 spool 模拟独立 console_server。
        threading.Timer(0.05, enqueue_resolve).start()
        return {**boot, "resource_request.json": _SIDECAR_REQ}

    def finish_after_resource(pack):
        assert pack.refs == ["user-file-request:r1:item:1:asset:1"]
        assert "RESIDENT-EEG-DATA" in pack.anchor_md
        assert "untrusted_non_evidence" in pack.anchor_md
        return boot

    system = build_system(
        SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory([request_resource, finish_after_resource]))
    httpd = CS.serve(str(tmp_path / "research.sqlite"), str(tmp_path), SYSTEM_ROOT,
                     host="127.0.0.1", port=0, capability_token=token)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    console_data = CS.ConsoleData(
        db_path=str(tmp_path / "research.sqlite"), work_root=str(tmp_path),
        system_root=SYSTEM_ROOT)

    try:
        assert system.run_forever(max_cycles=1, poll_interval_s=0.01,
                                  linger_after_terminal=False) == ["c1"]
        assert appended.wait(1) and response["queued"]["action"] == "resolve"
    finally:
        httpd.shutdown(); httpd.server_close(); server_thread.join(timeout=5)
    assert system.daemon.query_one(
        "SELECT status FROM interaction_request WHERE id=1")[0] == "resolved"
    assert (tmp_path / "input" / "user_provided" / "1" / "1" / "asset-1").read_bytes() == b"RESIDENT-EEG-DATA"
    events = [json.loads(line) for line in (tmp_path / "state" / "outbox.jsonl").read_text().splitlines()]
    assert "filereq:1:resolved:v2" in {event["event_key"] for event in events}
