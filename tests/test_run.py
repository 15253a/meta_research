"""CP7.3 · run.py 全系统装配入口（M6）。

核心验收面：一条命令把**真组件 + StageProvider(注入 runner)** 接成全自动元循环并跑到停机；每个注入
组件（真状态机/编译器/发布器/StopController/precheck）端到端接对：assembly→run→落库+发布；重启同
work_root 续跑（goal 不重建）；durable 停机与全局等待端到端生效。多阶段 kill-9 恢复由 advancer 层
测试覆盖（同机制），本层验装配正确性。
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml
from jsonschema.exceptions import ValidationError

from orchestrator import database as db
from orchestrator.interfaces import Artifact, CallUsage
from orchestrator.execution_sandbox import sandbox_environment_hash
from orchestrator.run import System, build_system
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = str(Path(__file__).resolve().parent.parent)
_POLICY = yaml.safe_load((Path(SYSTEM_ROOT) / "policies" / "policy.yaml").read_text(encoding="utf-8"))
RUNTIME_ENV_HASH = sandbox_environment_hash(_POLICY["execution"]["sandbox"])

_BOOT_TERMINATE = {
    "tree_ops.json": {"ops": [{"op": "create_root", "text": "根问题：EEG 有跨数据集通用规律吗？",
                               "local_key": "root"}]},
    "selection.json": {"next_question_id": None, "next_intent": "terminate", "scores": [],
                       "terminate_reason_md": "创世即达成（测试固定）"},
}


def _mock_factory(files_seq):
    """runner 工厂：每次 run_task 吐序列里的下一份 files（Artifact）。stage 取 pack.stage（不漂移）。"""
    box = {"seq": list(files_seq)}

    class MockRunner:
        def run_task(self, *, system_prompt, skill, context_pack):
            return Artifact(stage=context_pack.stage, files=box["seq"].pop(0), md="",
                            usage=CallUsage(tokens_known=True))
    return lambda td, pt: MockRunner()


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
def test_default_attack_assembly_includes_fenced_import_worker(tmp_path):
    from orchestrator.execution_sandbox import DockerExecutionSandbox
    from orchestrator.import_fetcher import FrozenCandidateFetcher
    from orchestrator.repository_materializer import (
        GitHubRepositoryMaterializer, ProductionCandidateFetcher)
    from orchestrator.import_search import GitHubRepoSearchProvider, ImportSearchService
    from orchestrator.import_triggers import (
        BoundedReferenceSnapshotProvider, ImportTriggerRouter,
        TrustedImportTriggerService)
    from orchestrator.import_worker import ImportWorker
    from orchestrator.stage_provider import PlanReviewProvider

    system = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))
    try:
        worker = system.advancer.import_worker
        assert isinstance(worker, ImportWorker)
        assert isinstance(worker.p["fetch"], ProductionCandidateFetcher)
        assert isinstance(worker.p["fetch"].legacy_fetcher, FrozenCandidateFetcher)
        assert isinstance(
            worker.p["fetch"].repository_fetcher,
            GitHubRepositoryMaterializer)
        assert worker.execution_supervisor is system.execution_supervisor
        assert isinstance(worker.execution_sandbox, DockerExecutionSandbox)
        assert system.advancer.attack.execution_sandbox is worker.execution_sandbox
        assert worker.execution_sandbox.resource_mode in {
            "cgroup-v1", "cgroup-v2", "rlimit-fallback"}
        assert isinstance(system.advancer.attack.p["plan_review"], PlanReviewProvider)
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
        class Runner:
            def run_task(self, *, system_prompt, skill, context_pack):
                calls.append(purpose)
                if purpose == "interaction-query":
                    return Artifact(
                        stage="reasoning", md="", usage=CallUsage(
                            tokens_total=17, tokens_known=True),
                        files={"interaction_reply.json": {
                            "facts": [{"path": "snapshot_cycle", "value": "c1"}],
                        }})
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
        class Runner:
            def run_task(self, *, system_prompt, skill, context_pack):
                if purpose == "interaction-query":
                    return Artifact(
                        stage="reasoning", md="", usage=CallUsage(tokens_total=7, tokens_known=True),
                        files={"interaction_reply.json": {
                            "facts": [{"path": "snapshot_cycle", "value": "c1"}],
                        }})
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
        class Runner:
            def run_task(self, *, system_prompt, skill, context_pack):
                calls.append(purpose)
                if purpose == "interaction-query":
                    return Artifact(
                        stage="reasoning", md="", usage=CallUsage(
                            tokens_total=5, tokens_known=True),
                        files={"interaction_reply.json": {
                            "facts": [{"path": "snapshot_cycle", "value": "c1"}],
                        }})
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

    missing = {**base, "budget": {k: v for k, v in base["budget"].items()
                                    if k != "price_per_1k_tokens"}}
    monkeypatch.setattr(R.yaml, "safe_load", lambda text: missing)
    with pytest.raises(ValidationError, match="price_per_1k_tokens"):
        R.build_system(SYSTEM_ROOT, str(tmp_path / "missing"), runner_factory=_mock_factory([]))
    assert not (tmp_path / "missing").exists()

    nonfinite = {**base, "budget": {**base["budget"], "price_per_1k_tokens": float("nan")}}
    monkeypatch.setattr(R.yaml, "safe_load", lambda text: nonfinite)
    with pytest.raises(ValueError, match="非有限数字"):
        R.build_system(SYSTEM_ROOT, str(tmp_path / "nan"), runner_factory=_mock_factory([]))
    assert not (tmp_path / "nan").exists()

    overflow = {**base, "budget": {**base["budget"], "session_max": 10 ** 10000}}
    monkeypatch.setattr(R.yaml, "safe_load", lambda text: overflow)
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


def test_system_unknown_usage_durably_stops_without_retry(tmp_path):
    """CLI 用量汇总未知时不得冒充真 0：落 durable stop，当前游标不提交/不重调。"""
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
    sys1.run(5)
    assert sys1.close() is None
    sys2 = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_mock_factory([]))   # 无需再调 runner
    assert sys2.run(max_cycles=5) == []                        # 已 terminate，无新轮
    assert sys2.daemon.query_one("SELECT count(*) FROM goal")[0] == 1   # goal 唯一（未重建）


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


# ============ CP8.4 · attack 全装配端到端（真子进程执行 + 真 judge 落库链）============
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


def test_full_attack_flow_end_to_end(tmp_path):
    """步⑧步级验证①：run.py 装配的**全系统**跑通完整流程——bootstrap→attack（idea→plan[真 gate 注册
    协议/占坑]→bundle[manifest→harness 真子进程 smoke/train/eval]→双评审[JudgeProvider 真落库链]→
    注册入池→真证据关问）→terminate。runner 为脚本化 mock（Codex 替身），其余全为真组件。"""
    import sys as _sys
    import test_attack_advance as TA
    from orchestrator.manifest import canon_hash

    db_path = str(tmp_path / "research.sqlite")

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
                    "env_hash": RUNTIME_ENV_HASH, "config_json": {"lr": 0.1},
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
    plan_review_pass = {
        "plan_review.json": {"verdict": "pass", "round_no": 1, "issues": []}}
    verdict_pass = {"review_verdict.json": {"verdict": "pass", "issues": []}}
    seq = [boot_attack,                          # c1 bootstrap（reasoning）
           TA._idea_set(), TA._plan_json(),      # c2 attack：idea → plan（冻结 schema 真形态）
           plan_review_pass,                     # plan answerability 独立 reviewer
           bundle_env,                           # bundle 信封（manifest+代码）
           verdict_pass, verdict_pass,           # judge：code review → result review（经 JudgeProvider 落库）
           attack_reasoning]                     # 轮尾：真证据关问 + terminate
    sys_ = build_system(SYSTEM_ROOT, str(tmp_path), runner_factory=_lazy_factory(seq))
    ids = sys_.run(max_cycles=6)
    assert len(ids) == 2                                         # bootstrap + attack 两轮后 terminate
    d = sys_.daemon
    # 全链断言：协议真注册 / 池 legal / 真测量 / 双评审真落库（JudgeProvider 链）/ 真证据关问
    assert d.query_one("SELECT count(*) FROM protocol WHERE name='toy-proto'")[0] == 1
    assert d.query_one("SELECT status FROM baseline WHERE canonical_key='ck-attack'")[0] == "legal"
    assert d.query_one("SELECT status, eval_key FROM evaluation WHERE source='factory'")[0:2] == ("success", "t1")
    assert d.query_one("SELECT value FROM metric_result ORDER BY id DESC LIMIT 1")[0] == 0.93
    assert d.query_one("SELECT count(*) FROM runner_call WHERE phase='audit' AND status='success'")[0] == 3
    assert d.query_one("SELECT count(*) FROM decision WHERE actor='judge'")[0] == 3
    assert d.query_one("SELECT count(*) FROM decision WHERE type='plan_review'")[0] == 1
    assert d.query_one(
        "SELECT count(*) FROM ledger l JOIN runner_call rc ON rc.id=l.runner_call_id "
        "WHERE rc.phase='audit' AND rc.purpose='plan_review'")[0] == 1
    assert d.query_one("SELECT status FROM question WHERE text LIKE 'toy 基线%'")[0] == "answered"
    assert d.query_one("SELECT count(*) FROM build_target WHERE status='complete'")[0] == 1
    # 执行是真子进程：checkpoint 文件真实存在且被登记
    ck = d.query_one("SELECT path, content_hash FROM checkpoint")
    assert Path(ck[0]).exists() and len(ck[1]) == 64
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
    """真实常驻拓扑：阶段阻断→HTTP 只入 spool→run 单写 resolve→自动续同阶段。"""
    import threading
    import urllib.request
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
        request = urllib.request.Request(
            base + "/api/file-request", method="POST",
            data=json.dumps({"action": "resolve", "request_id": 1,
                             "source_ref": "work/uploads/r1"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                     "Idempotency-Key": "e" * 32})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        response.update(json.loads(opener.open(request, timeout=5).read()))
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
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

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
