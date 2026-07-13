"""CP10.2 · 成本记账写入 + 激活全局预算安全网（步⑩ M6）。

验收：CostLedger 每次 LLM 调用写 runner_call + ledger（money=tokens/1000×price）；Stage/Judge 每次重试都记账；
预算启用时记账 fail-closed；judge 最终三写原子；**累计 SUM(ledger.money)≥session_max → budget_exhausted 转活**
（此前无写者、SUM 恒 0、网休眠）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.cost_ledger import BudgetExhausted, CostAccountingFailed, CostLedger, policy_fingerprint
from orchestrator.interfaces import Artifact, CallUsage
from orchestrator.runner import RunnerError
from orchestrator.schemas import SchemaSet
from orchestrator.stage_provider import JudgeProvider, StageProvider
from orchestrator.stopcontroller import StopController
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))   # price_per_1k_tokens=0.3
SCHEMAS = SchemaSet(SYSTEM_ROOT / "schemas")
_IDEA = json.loads((SYSTEM_ROOT / "tests" / "fixtures" / "valid" / "idea_set" / "wildidea.json").read_text(encoding="utf-8"))
_SKILLS = {s: f"[{s}]" for s in ("idea", "plan", "bundle", "reasoning")}


@pytest.fixture()
def daemon(tmp_path):
    d = WriteDaemon(db.connect(str(tmp_path / "r.sqlite")))
    conftest.seed_minimal(d.conn); d.conn.commit()
    return d


def _pack(stage="idea"):
    return NS(cycle_id="c1", stage=stage, target_id=None, anchor_md="", neighborhood_md="", retrieval_md="", refs=[])


def _known_usage(**kwargs):
    """测试替身显式声明「已观测」；CallUsage() 默认必须保持 unknown/fail-closed。"""
    return CallUsage(tokens_known=True, **kwargs)


# ---------------- CostLedger 单元 ----------------
def test_money_for():
    cl = CostLedger(None, {"budget": {"session_max": None, "price_per_1k_tokens": 0.3}})
    assert cl.money_for(_known_usage(tokens_total=5000)) == 1.5      # 5000/1000×0.3
    assert cl.money_for(_known_usage(tokens_total=0)) == 0.0
    assert cl.money_for(None) == 0.0


def test_record_writes_runner_call_and_ledger(daemon):
    cl = CostLedger(daemon, POLICY)
    rc = cl.record(cycle_id="c1", phase="idea", purpose="idea-n1",
                   usage=_known_usage(tokens_total=10000, wallclock_sec=2.5))
    assert daemon.query_one("SELECT phase,purpose,status FROM runner_call WHERE id=?", (rc,)) == ("idea", "idea-n1", "success")
    lg = daemon.query_one("SELECT cycle_id,phase,runner_call_id,tokens_total,wallclock_sec,money,policy_version "
                          "FROM ledger WHERE runner_call_id=?", (rc,))
    assert lg[:5] == (1, "idea", rc, 10000, 2.5)
    assert lg[5] == 3.0 and lg[6] == policy_fingerprint(POLICY)   # policy_version=整份 policy 规范化 hash


def test_record_ledger_only_reuses_existing_runner_call(daemon):
    cl = CostLedger(daemon, POLICY)
    with daemon.transaction() as conn:
        rc = conn.execute("INSERT INTO runner_call(cycle_id,phase,purpose,status) "
                          "VALUES (1,'audit','judge','success')").lastrowid
    cl.record_ledger_only(runner_call_id=rc, usage=_known_usage(tokens_total=2000))
    # cycle_id/phase 从 runner_call 派生（不信调用方）；money=2000/1000×0.3
    assert daemon.query_one("SELECT cycle_id,phase,money,runner_call_id FROM ledger WHERE runner_call_id=?",
                            (rc,)) == (1, "audit", 0.6, rc)
    assert daemon.query_one("SELECT COUNT(*) FROM runner_call WHERE id=?", (rc,))[0] == 1   # 未重复建 runner_call


def test_fail_existing_unaccounted_call_reuses_intent_and_stops(daemon):
    """interaction_query 崩溃恢复：原 running intent 终态化；不另造伪调用，预算开启则 durable stop。"""
    cl = CostLedger(daemon, POLICY)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (1,'interaction_query','message:99','running')").lastrowid
    with daemon.transaction() as conn:
        payload = cl.fail_existing_unaccounted_call(
            conn, runner_call_id=rc, failure_kind="orphaned_query_intent",
            cause=RuntimeError("unknown external state"))
    assert payload["runner_call_id"] == rc
    assert daemon.query_one(
        "SELECT status,failure_kind FROM runner_call WHERE id=?", (rc,)) == (
            "failed", "orphaned_query_intent")
    assert daemon.query_one("SELECT COUNT(*) FROM runner_call")[0] == 1
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id=?", (rc,))[0] == 0
    stop = json.loads(daemon.query_one(
        "SELECT payload_json FROM decision WHERE actor='orchestrator' AND type='global_stop'")[0])
    assert stop["reason"] == "cost_accounting_failed" and stop["runner_call_id"] == rc


def test_fail_existing_unaccounted_call_without_budget_only_marks_failure(daemon):
    policy = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
    cl = CostLedger(daemon, policy)
    with daemon.transaction() as conn:
        rc = conn.execute(
            "INSERT INTO runner_call(cycle_id,phase,purpose,status) "
            "VALUES (1,'interaction_query','message:100','created')").lastrowid
        assert cl.fail_existing_unaccounted_call(
            conn, runner_call_id=rc, failure_kind="orphaned_query_intent",
            cause=RuntimeError("unknown")) is None
    assert daemon.query_one("SELECT status FROM runner_call WHERE id=?", (rc,)) == ("failed",)
    assert daemon.query_one(
        "SELECT COUNT(*) FROM decision WHERE actor='orchestrator' AND type='global_stop'")[0] == 0


def test_duplicate_ledger_for_same_runner_call_fails_loud(daemon):
    cl = CostLedger(daemon, POLICY)
    rc = cl.record(cycle_id="c1", phase="idea", purpose="once", usage=_known_usage(tokens_total=10))
    with pytest.raises(CostAccountingFailed, match="重复记账"):
        cl.record_ledger_only(runner_call_id=rc, usage=_known_usage(tokens_total=20))
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id=?", (rc,))[0] == 1


def test_missing_runner_call_in_ledger_only_durably_stops(daemon):
    with pytest.raises(CostAccountingFailed, match="不存在"):
        CostLedger(daemon, POLICY).record_ledger_only(
            runner_call_id=999999, usage=_known_usage(tokens_total=1))
    assert daemon.query_one("SELECT cycle_id,phase,status,failure_kind FROM runner_call") == (
        None, "orchestrator", "failed", "cost_accounting")
    assert StopController(daemon, POLICY).already_stopped() == "cost_accounting_failed"


def test_call_usage_defaults_to_unknown():
    assert CallUsage().tokens_known is False


@pytest.mark.parametrize("usage", [
    _known_usage(tokens_total=-1),
    _known_usage(tokens_total=1.5),
    _known_usage(tokens_total=1 << 63),
    _known_usage(tokens_input=-1),
    _known_usage(tokens_output=True),
    _known_usage(tokens_input=8, tokens_output=4, tokens_total=10),
    _known_usage(wallclock_sec=-0.1),
    _known_usage(wallclock_sec=float("nan")),
    _known_usage(wallclock_sec=float("inf")),
])
def test_invalid_call_usage_rejected_without_partial_rows(daemon, usage):
    with pytest.raises(CostAccountingFailed, match="CallUsage"):
        CostLedger(daemon, POLICY).record(cycle_id="c1", phase="idea", purpose="bad", usage=usage)
    assert daemon.query_one("SELECT status,failure_kind FROM runner_call") == ("failed", "cost_accounting")
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 0


def test_tiny_positive_price_is_not_rounded_to_zero():
    pol = {"budget": {"session_max": 1, "price_per_1k_tokens": 1e-9}}
    money = CostLedger(None, pol).money_for(_known_usage(tokens_total=1))
    assert money == pytest.approx(1e-12) and money > 0


def test_price_underflow_for_positive_tokens_fails_loud():
    pol = {"budget": {"session_max": 1, "price_per_1k_tokens": 5e-324}}
    with pytest.raises(ValueError, match="下溢"):
        CostLedger(None, pol).money_for(_known_usage(tokens_total=1))


def test_disabled_budget_allows_explicit_zero_price_and_records_audit_row(daemon):
    pol = {"budget": {"session_max": None, "price_per_1k_tokens": 0}}
    cl = CostLedger(daemon, pol)
    rc = cl.record(cycle_id="c1", phase="idea", purpose="zero-price",
                   usage=_known_usage(tokens_total=123))
    assert daemon.query_one(
        "SELECT tokens_total,money FROM ledger WHERE runner_call_id=?", (rc,)) == (123, 0.0)


def test_cost_ledger_rejects_boolean_price_even_without_schema_boundary():
    with pytest.raises(ValueError, match="price_per_1k_tokens"):
        CostLedger(None, {"budget": {"session_max": None, "price_per_1k_tokens": True}})


def test_cost_ledger_requires_explicit_session_max():
    with pytest.raises(ValueError, match="session_max 必须显式存在"):
        CostLedger(None, {"budget": {"price_per_1k_tokens": 0.3}})


def test_policy_fingerprint_is_canonical_and_content_derived():
    p1 = {"budget": {"session_max": None, "price_per_1k_tokens": 0}, "flow": {"x": 1}}
    p2 = {"flow": {"x": 1}, "budget": {"price_per_1k_tokens": 0, "session_max": None}}
    assert policy_fingerprint(p1) == policy_fingerprint(p2)
    assert len(policy_fingerprint(p1)) == 64
    assert policy_fingerprint(p1) != policy_fingerprint({**p1, "flow": {"x": 2}})


def test_unknown_usage_with_active_budget_durably_stops(daemon):
    cl = CostLedger(daemon, POLICY)
    with pytest.raises(CostAccountingFailed, match="cost_accounting_failed"):
        cl.record(cycle_id="c1", phase="reasoning", purpose="r-n1", usage=None)
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 0
    assert daemon.query_one("SELECT status,failure_kind FROM runner_call") == ("failed", "cost_accounting")
    assert StopController(daemon, POLICY).already_stopped() == "cost_accounting_failed"


def test_ledger_stays_append_only(daemon):
    CostLedger(daemon, POLICY).record(cycle_id="c1", phase="idea", purpose="x",
                                      usage=_known_usage(tokens_total=1000))
    with pytest.raises(sqlite3.IntegrityError):                   # 触发器仍守 append-only（本类只 INSERT）
        daemon.conn.execute("UPDATE ledger SET money=99")


# ---------------- StageProvider 接线记账 ----------------
class _UsageRunner:
    def __init__(self, files, usage):
        self.files, self.usage = files, usage
        self.calls = 0

    def run_task(self, *, system_prompt, skill, context_pack):
        self.calls += 1
        return Artifact(stage=context_pack.stage, files=self.files, md="", usage=self.usage)


def _sp(daemon, tmp_path, runner, cost_ledger, policy=POLICY):
    return StageProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=policy,
                         system_prompt="S", skills=_SKILLS, work_root=str(tmp_path), cost_ledger=cost_ledger)


def test_stage_provider_records_cost(daemon, tmp_path):
    cl = CostLedger(daemon, POLICY)
    sp = _sp(daemon, tmp_path, _UsageRunner({"idea_set.json": _IDEA}, _known_usage(tokens_total=15000)), cl)
    out = sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert "idea_set.json" in out                                # 产物照常返回
    assert daemon.query_one("SELECT phase,tokens_total,money FROM ledger ORDER BY id DESC LIMIT 1") == ("idea", 15000, 4.5)


def test_fresh_stage_providers_keep_distinct_durable_heartbeats(daemon, tmp_path):
    """Two post-restart n1 calls retain both heartbeat files instead of reusing one local-seq path."""
    purposes = []

    class BoundRunner(_UsageRunner):
        def __init__(self):
            super().__init__(
                {"idea_set.json": _IDEA}, _known_usage(tokens_total=1))
            self.runner_call_ids = []

        def bind_runner_call(self, **kwargs):
            self.runner_call_ids.append(kwargs["runner_call_id"])

    runners = []
    for _restart in range(2):
        runner = BoundRunner()
        runners.append(runner)

        def factory(_transcripts, purpose, *, current=runner):
            purposes.append(purpose)
            return current

        StageProvider(
            runner_factory=factory, schemas=SCHEMAS, policy=POLICY,
            system_prompt="S", skills=_SKILLS, work_root=str(tmp_path),
            cost_ledger=CostLedger(daemon, POLICY),
        ).idea(NS(cycle_id="c1"), _pack("idea"))

    rows = daemon.query(
        "SELECT id,transcript_ref FROM runner_call ORDER BY id")
    assert purposes == ["idea-n1", "idea-n1"]
    assert [runner.runner_call_ids for runner in runners] == [
        [rows[0][0]], [rows[1][0]]]
    refs = [Path(row[1]) for row in rows]
    assert [path.name for path in refs] == [
        f"idea-rc{rows[0][0]}.heartbeat.json",
        f"idea-rc{rows[1][0]}.heartbeat.json"]
    assert refs[0] != refs[1] and all(path.is_file() for path in refs)
    assert [json.loads(path.read_text(encoding="utf-8"))["runner_call_id"]
            for path in refs] == [rows[0][0], rows[1][0]]


def test_stage_provider_cost_failure_is_fatal_when_budget_enabled(daemon, tmp_path, monkeypatch):
    cl = CostLedger(daemon, POLICY)

    def boom(**kw):
        raise RuntimeError("记账崩")
    monkeypatch.setattr(cl, "finish_call", boom)
    sp = _sp(daemon, tmp_path, _UsageRunner({"idea_set.json": _IDEA}, _known_usage(tokens_total=1000)), cl)
    with pytest.raises(RuntimeError, match="记账崩"):
        sp.idea(NS(cycle_id="c1"), _pack("idea"))              # 预算网启用 → 不许带病继续制造隐形成本


def test_stage_provider_cost_failure_best_effort_when_budget_disabled(daemon, tmp_path, monkeypatch):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
    cl = CostLedger(daemon, pol)
    monkeypatch.setattr(cl, "finish_call", lambda **kw: (_ for _ in ()).throw(RuntimeError("记账崩")))
    sp = _sp(daemon, tmp_path, _UsageRunner({"idea_set.json": _IDEA}, _known_usage(tokens_total=1000)), cl, pol)
    assert "idea_set.json" in sp.idea(NS(cycle_id="c1"), _pack("idea"))


def test_no_cost_ledger_no_records(daemon, tmp_path):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
    sp = _sp(daemon, tmp_path, _UsageRunner({"idea_set.json": _IDEA}, _known_usage(tokens_total=1000)), None, pol)
    sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 0   # cost_ledger=None → 不记账


def test_budget_enabled_provider_requires_cost_ledger(daemon, tmp_path):
    runner = _UsageRunner({"idea_set.json": _IDEA}, _known_usage(tokens_total=1))
    with pytest.raises(ValueError, match="StageProvider.*cost_ledger"):
        _sp(daemon, tmp_path, runner, None, POLICY)
    with pytest.raises(ValueError, match="JudgeProvider.*cost_ledger"):
        JudgeProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=POLICY,
                      system_prompt="S", skill="J", daemon=daemon, work_root=str(tmp_path),
                      cost_ledger=None)


class _ScriptedUsageRunner:
    """按次序吐 (files, usage)——测「产物被拒重试」时每次真调用都记账。"""
    def __init__(self, script):
        self.script = list(script)

    def run_task(self, *, system_prompt, skill, context_pack):
        files, usage = self.script.pop(0)
        return Artifact(stage=context_pack.stage, files=files, md="", usage=usage)


def test_records_every_call_including_rejected_artifact(daemon, tmp_path):
    """外审 SHOULD：模型产非法产物→重试，**每次真 LLM 调用都记账**（失控路径成本不被系统性低估）。"""
    cl = CostLedger(daemon, POLICY)
    runner = _ScriptedUsageRunner([
        ({"idea_set.json": {"bad": "非法 schema"}}, _known_usage(tokens_total=8000)),   # 第 1 次：过信封但 schema 拒 → 重试
        ({"idea_set.json": _IDEA}, _known_usage(tokens_total=12000)),                     # 第 2 次：合法
    ])
    sp = _sp(daemon, tmp_path, runner, cl)
    sp.idea(NS(cycle_id="c1"), _pack("idea"))
    rows = daemon.query(
        "SELECT rc.status,rc.failure_kind,l.tokens_total,l.money FROM runner_call rc "
        "JOIN ledger l ON l.runner_call_id=rc.id ORDER BY rc.id")
    assert rows[0][:3] == ("failed", "artifact_parse", 8000)
    assert rows[1][:3] == ("success", None, 12000)
    assert [r[3] for r in rows] == pytest.approx([2.4, 3.6])         # 两次调用都落账（含被拒那次）


def test_runner_error_usage_is_recorded_before_retry(daemon, tmp_path):
    class Runner:
        def __init__(self):
            self.n = 0

        def run_task(self, **kw):
            self.n += 1
            if self.n == 1:
                raise RunnerError("坏信封", usage=_known_usage(tokens_total=7000, wallclock_sec=1.2))
            return Artifact(stage="idea", files={"idea_set.json": _IDEA},
                            usage=_known_usage(tokens_total=3000))

    sp = _sp(daemon, tmp_path, Runner(), CostLedger(daemon, POLICY))
    sp.idea(NS(cycle_id="c1"), _pack("idea"))
    rows = daemon.query("SELECT rc.status,rc.failure_kind,l.tokens_total FROM runner_call rc "
                        "JOIN ledger l ON l.runner_call_id=rc.id ORDER BY rc.id")
    assert rows == [("failed", "runner_error", 7000), ("success", None, 3000)]


def test_unknown_usage_records_zero_only_when_budget_explicitly_disabled(daemon, tmp_path):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": None}}
    cl = CostLedger(daemon, pol)
    sp = _sp(daemon, tmp_path, _UsageRunner({"idea_set.json": _IDEA}, None), cl, pol)
    sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert daemon.query_one("SELECT tokens_total,money FROM ledger WHERE runner_call_id IS NOT NULL")[:2] == (0, 0.0)


def test_stage_unknown_usage_stops_without_retry(daemon, tmp_path):
    runner = _UsageRunner({"idea_set.json": _IDEA}, None)
    sp = _sp(daemon, tmp_path, runner, CostLedger(daemon, POLICY))
    with pytest.raises(CostAccountingFailed):
        sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert runner.calls == 1
    assert StopController(daemon, POLICY).already_stopped() == "cost_accounting_failed"


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_armed_budget_rejects_missing_nonpositive_or_nonfinite_price(price):
    budget = {"session_max": 100}
    if price is not None:
        budget["price_per_1k_tokens"] = price
    with pytest.raises(ValueError, match="price_per_1k_tokens"):
        CostLedger(None, {"budget": budget})


# ---------------- JudgeProvider：每次重试记账 + 最终三写原子 ----------------
class _JudgeRunner:
    def __init__(self, artifacts):
        self.artifacts = list(artifacts)
        self.calls = 0

    def run_task(self, **kw):
        self.calls += 1
        item = self.artifacts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _judge(daemon, tmp_path, runner, ledger, policy=POLICY):
    return JudgeProvider(runner_factory=lambda td, pt: runner, schemas=SCHEMAS, policy=policy,
                         system_prompt="S", skill="J", daemon=daemon, work_root=str(tmp_path),
                         cost_ledger=ledger)


def test_judge_records_rejected_artifact_and_atomically_records_final(daemon, tmp_path):
    runner = _JudgeRunner([
        RunnerError("judge 坏信封", usage=_known_usage(tokens_total=500)),
        Artifact(stage="bundle", files={"review_verdict.json": {"verdict": "fail", "issues": []}},
                 usage=_known_usage(tokens_total=1000)),
        Artifact(stage="bundle", files={"review_verdict.json": {"verdict": "pass", "issues": []}},
                 usage=_known_usage(tokens_total=2000)),
    ])
    _judge(daemon, tmp_path, runner, CostLedger(daemon, POLICY))(
        "c1", 2, "bundle_code_review", "subject")
    rows = daemon.query("SELECT rc.status,l.tokens_total FROM runner_call rc "
                        "JOIN ledger l ON l.runner_call_id=rc.id ORDER BY rc.id")
    assert rows == [("failed", 500), ("failed", 1000), ("success", 2000)]
    decision_rc = daemon.query_one("SELECT json_extract(payload_json,'$.runner_call_id') FROM decision "
                                   "WHERE actor='judge'")[0]
    assert daemon.query_one("SELECT tokens_total FROM ledger WHERE runner_call_id=?", (decision_rc,)) == (2000,)


def test_judge_final_cost_failure_rolls_back_runner_and_decision(daemon, tmp_path, monkeypatch):
    cl = CostLedger(daemon, POLICY)
    monkeypatch.setattr(cl, "insert_ledger_for_runner",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ledger write failed")))
    runner = _JudgeRunner([Artifact(stage="bundle",
                                    files={"review_verdict.json": {"verdict": "pass", "issues": []}},
                                    usage=_known_usage(tokens_total=2000))])
    with pytest.raises(CostAccountingFailed, match="ledger write failed"):
        _judge(daemon, tmp_path, runner, cl)("c1", 2, "bundle_code_review", "subject")
    assert daemon.query_one("SELECT status,failure_kind FROM runner_call") == ("failed", "cost_accounting")
    assert daemon.query_one("SELECT COUNT(*) FROM decision WHERE actor='judge'")[0] == 0
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 0
    assert StopController(daemon, POLICY).already_stopped() == "cost_accounting_failed"


def test_stage_budget_crossing_commits_stop_and_prevents_retry(daemon, tmp_path):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": 0.1,
                                 "price_per_1k_tokens": 0.3}}

    class InvalidRunner:
        def __init__(self):
            self.calls = 0

        def run_task(self, **kw):
            self.calls += 1
            return Artifact(stage="idea", files={"idea_set.json": {"bad": True}},
                            usage=_known_usage(tokens_total=1000))

    runner = InvalidRunner()
    sp = _sp(daemon, tmp_path, runner, CostLedger(daemon, pol), pol)
    with pytest.raises(BudgetExhausted) as ei:
        sp.idea(NS(cycle_id="c1"), _pack("idea"))
    assert runner.calls == 1 and ei.value.spent == pytest.approx(0.3)
    assert daemon.query_one("SELECT COUNT(*) FROM ledger WHERE runner_call_id IS NOT NULL")[0] == 1
    assert StopController(daemon, pol).already_stopped() == "budget_exhausted"
    assert daemon.query_one("SELECT COUNT(*) FROM decision WHERE type='global_stop'")[0] == 1


def test_judge_budget_crossing_commits_verdict_then_raises(daemon, tmp_path):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": 0.1,
                                 "price_per_1k_tokens": 0.3}}
    runner = _JudgeRunner([Artifact(stage="bundle",
                                    files={"review_verdict.json": {"verdict": "pass", "issues": []}},
                                    usage=_known_usage(tokens_total=1000))])
    with pytest.raises(BudgetExhausted):
        _judge(daemon, tmp_path, runner, CostLedger(daemon, pol), pol)(
            "c1", 2, "bundle_code_review", "subject")
    assert runner.calls == 1
    decision_rc = daemon.query_one("SELECT json_extract(payload_json,'$.runner_call_id') FROM decision "
                                   "WHERE actor='judge'")[0]
    assert daemon.query_one("SELECT tokens_total FROM ledger WHERE runner_call_id=?", (decision_rc,)) == (1000,)
    assert daemon.query_one("SELECT COUNT(*) FROM runner_call WHERE id=?", (decision_rc,))[0] == 1
    assert StopController(daemon, pol).already_stopped() == "budget_exhausted"


# ---------------- 激活安全网（步⑩ 核心目的）----------------
def test_budget_exhausted_activated_by_cost_ledger(daemon):
    pol = {**POLICY, "budget": {**POLICY["budget"], "session_max": 5, "price_per_1k_tokens": 0.3}}
    cl = CostLedger(daemon, pol)
    sc = StopController(daemon, pol)
    assert sc.check_after_round() is None                        # 记账前 SUM=0 → 网休眠
    cl.record(cycle_id="c1", phase="idea", purpose="a", usage=_known_usage(tokens_total=10000))      # money 3
    with pytest.raises(BudgetExhausted):
        cl.record(cycle_id="c1", phase="reasoning", purpose="b", usage=_known_usage(tokens_total=10000))  # money 3 → SUM 6≥5
    hit = sc.check_after_round()
    assert hit and hit["reason"] == "budget_exhausted" and hit["spent"] == 6.0
    assert sc.already_stopped() == "budget_exhausted"           # durable global_stop 落库（恢复也拒推进）
    with pytest.raises(BudgetExhausted):                         # 即使误调用，stop 决策仍幂等不重复
        cl.record(cycle_id="c1", phase="idea", purpose="late", usage=_known_usage(tokens_total=1))
    assert daemon.query_one("SELECT COUNT(*) FROM decision WHERE type='global_stop'")[0] == 1
