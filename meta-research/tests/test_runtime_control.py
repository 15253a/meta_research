"""CP11.2b.3a: durable set_budget and mechanically applied reprioritize."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import conftest
from orchestrator import database as db
from orchestrator.advancer import SqliteAdvancer
from orchestrator.attack_stages import persist_selection_safe
from orchestrator.budgeting import compute_budget
from orchestrator.console import (DIRECTIVE_ACTION_SESSION_REF, Console,
                                  DirectiveApplicationError, KeywordClassifier,
                                  directive_action_text)
from orchestrator.cost_ledger import CostLedger, policy_fingerprint
from orchestrator.interfaces import CallUsage, Selection
from orchestrator.notify import DirectiveNotifier, Outbox, make_advancer_precheck
from orchestrator.runtime_control import effective_budget_config
from orchestrator.statestore_sqlite import SQLiteStateStore
from orchestrator.status_card import build_status_card
from orchestrator.stopcontroller import StopController
from orchestrator.writedaemon import WriteDaemon

SYSTEM_ROOT = Path(__file__).resolve().parent.parent
_BASE_POLICY = yaml.safe_load((SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
# Runtime ceiling mutation is tested in the armed mode even though the current
# production policy deliberately starts with the global cost guard disabled.
POLICY = {**_BASE_POLICY, "budget": {**_BASE_POLICY["budget"], "session_max": 100000}}


@pytest.fixture()
def env():
    daemon = WriteDaemon(db.connect(":memory:"))
    conftest.seed_minimal(daemon.conn)
    return daemon, Console(daemon, policy=POLICY)


def _confirm(daemon, console, result):
    did = result["directive_id"]
    source = daemon.query_one(
        "SELECT m.goal_id,m.goal_ver,m.connector,m.conversation_id,m.session_ref "
        "FROM directive d JOIN interaction_message m "
        "ON m.id=d.source_interaction_message_id WHERE d.id=?", (did,))
    session_ref = (DIRECTIVE_ACTION_SESSION_REF
                   if source[2] == "console" else
                   (source[4] + ":action" if source[4] is not None else None))
    mid = console.ingest.inbound(
        connector=source[2], raw_text=directive_action_text("confirm", did),
        idempotency_key=f"confirm-d{did}", goal_id=source[0], goal_ver=source[1],
        session_ref=session_ref, conversation_id=source[3])
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_classification(message_id,intent,directive_id) "
            "VALUES (?,'unclear',NULL)", (mid,))
    console.confirm_directive(directive_id=did, confirm_message_id=mid)


def _reject(daemon, console, result, reason="用户未确认新优先级"):
    did = result["directive_id"]
    source = daemon.query_one(
        "SELECT m.goal_id,m.goal_ver,m.connector,m.conversation_id,m.session_ref "
        "FROM directive d JOIN interaction_message m "
        "ON m.id=d.source_interaction_message_id WHERE d.id=?", (did,))
    session_ref = (DIRECTIVE_ACTION_SESSION_REF
                   if source[2] == "console" else
                   (source[4] + ":action" if source[4] is not None else None))
    mid = console.ingest.inbound(
        connector=source[2], raw_text=directive_action_text("reject", did, reason=reason),
        idempotency_key=f"reject-d{did}", goal_id=source[0], goal_ver=source[1],
        session_ref=session_ref, conversation_id=source[3])
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO interaction_classification(message_id,intent,directive_id) "
            "VALUES (?,'unclear',NULL)", (mid,))
    console.reject_directive(
        directive_id=did, reason=reason, reject_message_id=mid)


def _inbound(console, text, key):
    return console.handle_inbound(
        connector="test", raw_text=text, idempotency_key=key, goal_id=1, goal_ver=1)


def test_classifier_extracts_canonical_budget_and_priority_payloads():
    classifier = KeywordClassifier()
    assert classifier.classify({"raw_text": "当前预算多少？"})["intent"] == "query"
    budget = classifier.classify({"raw_text": "设置预算 50"})
    assert budget["structured"] == {"budget_patch": {"session_max": 50.0}}
    assert budget["hardness"] == "hard" and budget["consume_at"] == "stage_boundary"
    named = classifier.classify({"raw_text": "B0=5 B_max=20 doubling_period_m=3"})
    assert named["structured"] == {"budget_patch": {
        "B0": 5.0, "B_max": 20.0, "doubling_period_m": 3}}

    pin = classifier.classify({"raw_text": "优先 q17"})
    assert pin["structured"] == {"mode": "pin", "question_id": "q17"}
    assert pin["hardness"] == "hard"
    malformed_pin = classifier.classify({"raw_text": "pin this one"})
    assert malformed_pin["hardness"] == "hard" and "parse_error" in malformed_pin["structured"]

    boost = classifier.classify({"raw_text": "boost q17 0.25"})
    assert boost["structured"] == {"mode": "boost", "question_id": "q17", "adjust": 0.25}
    assert boost["hardness"] == "soft"
    suppress = classifier.classify({"raw_text": "降权 q17 0.4"})
    assert suppress["structured"]["adjust"] == -0.4


def test_console_without_policy_does_not_advertise_set_budget(env):
    daemon, _ = env
    result = _inbound(Console(daemon), "设置预算 50", "budget-without-policy")
    assert result["intent"] == "unclear" and result["directive_id"] is None


def test_set_budget_is_durable_authority_for_schedule_stop_and_ledger(env):
    daemon, console = env
    result = _inbound(
        console,
        'set_budget {"B0":7,"B_max":7,"doubling_period_m":3.0,"session_max":200}',
        "budget-complete")
    _confirm(daemon, console, result)
    effect = console.consume_directive(directive_id=result["directive_id"], cycle_id="c1")
    assert effect["budget"]["B0"] == 7.0
    assert effect["budget"]["doubling_period_m"] == 3
    assert effect["budget"]["session_max"] == 200.0
    assert compute_budget(daemon.conn, POLICY["budget"]) == 7.0

    # Fresh component instances derive the same override from the append-only
    # consumed decision; no process-local mutation is required.
    projected = effective_budget_config(daemon.conn, POLICY["budget"])
    assert projected == effect["budget"]
    assert StopController(daemon, POLICY)._budget_exhausted() is None
    ledger = CostLedger(daemon, POLICY)
    runner_call = ledger.record(
        cycle_id="c1", phase="reasoning", purpose="after-live-budget",
        usage=CallUsage(tokens_known=True, tokens_total=1000))
    version = daemon.query_one(
        "SELECT policy_version FROM ledger WHERE runner_call_id=?", (runner_call,))[0]
    assert version != policy_fingerprint(POLICY)  # row binds the effective runtime policy
    card_budget = build_status_card(
        daemon.conn, cycle_id="c1", policy=POLICY)["budget"]
    assert card_budget["B_t"] == 7.0
    assert card_budget["cycle_spent"] == pytest.approx(0.3)
    assert card_budget["global_remaining"] == pytest.approx(199.7)


def test_compute_budget_keeps_schedule_only_compatibility(env):
    daemon, _ = env
    assert compute_budget(daemon.conn, {
        "B0": 2, "doubling_period_m": 3, "B_max": 8,
    }) == 2.0


def test_lowering_session_ceiling_below_spent_stops_in_same_transaction(env):
    daemon, console = env
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO ledger(cycle_id,phase,evaluation_attempt_id,money,policy_version) "
            "VALUES (1,'reasoning',1,12,'old')")
    result = _inbound(console, "设置预算 10", "budget-stop")
    _confirm(daemon, console, result)
    effect = console.consume_directive(directive_id=result["directive_id"], cycle_id="c1")
    assert effect["global_stop"]["reason"] == "budget_exhausted"
    assert StopController(daemon, POLICY).already_stopped() == "budget_exhausted"
    assert daemon.query_one(
        "SELECT status,consumed_decision_id FROM directive WHERE id=?",
        (result["directive_id"],))[0] == "consumed"


def test_stage_boundary_budget_stop_cannot_leak_one_more_runner_call(env):
    daemon, console = env
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO ledger(cycle_id,phase,evaluation_attempt_id,money,policy_version) "
            "VALUES (1,'reasoning',1,12,'old')")
    result = _inbound(console, "设置预算 10", "budget-stop-before-runner")
    _confirm(daemon, console, result)
    calls = []
    advancer = SqliteAdvancer(
        SQLiteStateStore(daemon, POLICY), compiler=None,
        reasoning_provider=lambda *args: calls.append(args),
        precheck=make_advancer_precheck(console, daemon),
        stop_controller=StopController(daemon, POLICY))
    assert advancer.run_cycles(1) == []
    assert calls == []
    assert advancer.last_stop_reason == "budget_exhausted"
    assert advancer.last_block_reason is None


def test_set_budget_rejects_malformed_or_accounting_mode_toggle_without_partial_effect(env):
    daemon, console = env
    malformed = _inbound(console, "设置预算很多", "budget-malformed")
    _confirm(daemon, console, malformed)
    with pytest.raises(DirectiveApplicationError, match="参数未解析"):
        console.consume_directive(directive_id=malformed["directive_id"], cycle_id="c1")
    assert daemon.query_one(
        "SELECT status,consumed_decision_id FROM directive WHERE id=?",
        (malformed["directive_id"],)) == ("pending", None)

    toggle = _inbound(console, 'set_budget {"session_max":null}', "budget-toggle")
    _confirm(daemon, console, toggle)
    with pytest.raises(DirectiveApplicationError, match="不得启用/关闭"):
        console.consume_directive(directive_id=toggle["directive_id"], cycle_id="c1")


def _seed_priority_questions(daemon):
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (2,1,1,1,'q2','open','agent')")
        conn.execute(
            "INSERT INTO question(id,goal_id,goal_ver,born_goal_ver,text,status,source) "
            "VALUES (3,1,1,1,'q3','open','agent')")


def test_hard_pin_and_soft_adjust_are_mechanically_applied_and_audited(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    pin = _inbound(console, "pin q3", "pin-q3")
    _confirm(daemon, console, pin)
    console.consume_directive(directive_id=pin["directive_id"], cycle_id="c1")
    boost = _inbound(console, "提权 q2 0.4", "boost-q2")
    console.consume_directive(directive_id=boost["directive_id"], cycle_id="c1")

    state = SQLiteStateStore(daemon, POLICY)
    state.persist_selection("c1", Selection(
        next_question_id="q2", next_intent="attack", scores=[
            {"question_id": "q2", "score": 1.0, "est_cost": 1.0, "directive_adjust": 0.0},
            {"question_id": "q3", "score": 0.5, "est_cost": 10.0},
        ]))
    # pin wins target; est_cost 10 > B(t)=5, so the reference R3 split chooses decompose.
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (3, "decompose")
    assert daemon.query_one("SELECT score FROM question WHERE id=2")[0] == pytest.approx(1.4)
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE directive_id=? AND type='reprioritize_enforced'",
        (pin["directive_id"],))[0] == 1
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE directive_id=? AND type='reprioritize_applied'",
        (boost["directive_id"],))[0] == 1


def test_soft_boost_reranks_scored_frontier(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    boost = _inbound(console, "boost q2 1", "rerank-q2")
    console.consume_directive(directive_id=boost["directive_id"], cycle_id="c1")
    SQLiteStateStore(daemon, POLICY).persist_selection("c1", Selection(
        next_question_id="q3", next_intent="attack", scores=[
            {"question_id": "q2", "score": 0.4, "est_cost": 1.0},
            {"question_id": "q3", "score": 1.0, "est_cost": 1.0},
        ]))
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (2, "attack")


def test_priority_override_uses_decompose_when_attack_guard_is_exhausted(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    limit = POLICY["question_guard"]["max_inconclusive_per_question"]
    with daemon.transaction() as conn:
        conn.execute(
            "UPDATE question SET status='inconclusive',visit_count=? WHERE id=3", (limit,))
    pin = _inbound(console, "pin q3", "pin-decompose-only")
    _confirm(daemon, console, pin)
    console.consume_directive(directive_id=pin["directive_id"], cycle_id="c1")

    SQLiteStateStore(daemon, POLICY).persist_selection("c1", Selection(
        next_question_id="q2", next_intent="attack", scores=[
            {"question_id": "q2", "score": 1.0, "est_cost": 1.0},
            {"question_id": "q3", "score": 2.0, "est_cost": 1.0},
        ]))
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (3, "decompose")


def test_reprioritize_accepts_current_active_question_for_post_stage_selection(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    with daemon.transaction() as conn:
        conn.execute("UPDATE question SET status='active',active_cycle=1 WHERE id=2")
        conn.execute("UPDATE cycle SET route='attack',active_question_id=2 WHERE id=1")
    pin = _inbound(console, "pin q2", "pin-current-active")
    _confirm(daemon, console, pin)
    console.consume_directive(directive_id=pin["directive_id"], cycle_id="c1")

    # Attack reasoning closes or releases the active Qn before selection is
    # persisted.  Simulate the no-answer release and prove the consumed pin is
    # then a real, enforceable post-stage choice.
    state = SQLiteStateStore(daemon, POLICY)
    state.mark_inconclusive("q2")
    state.persist_selection("c1", Selection(
        next_question_id="q3", next_intent="attack", scores=[
            {"question_id": "q2", "score": 0.5, "est_cost": 1.0},
            {"question_id": "q3", "score": 1.0, "est_cost": 1.0},
        ]))
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (2, "attack")


def test_reprioritize_notification_waits_for_real_selection_effect(env, tmp_path):
    daemon, console = env
    _seed_priority_questions(daemon)
    boost = _inbound(console, "boost q2 1", "notify-q2")
    console.consume_directive(directive_id=boost["directive_id"], cycle_id="c1")
    notifier = DirectiveNotifier(daemon, Outbox(str(tmp_path / "outbox")))
    early = notifier.scan()
    assert f"directive:{boost['directive_id']}:pending_effect:v2" in early
    assert f"directive:{boost['directive_id']}:applied:v2" not in early

    SQLiteStateStore(daemon, POLICY).persist_selection("c1", Selection(
        next_question_id="q3", next_intent="attack", scores=[
            {"question_id": "q2", "score": 0.4, "est_cost": 1.0},
            {"question_id": "q3", "score": 1.0, "est_cost": 1.0},
        ]))
    assert f"directive:{boost['directive_id']}:applied:v2" in notifier.scan()


def test_soft_reprioritize_missing_score_is_terminally_declined(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    boost = _inbound(console, "boost q2 1", "decline-q2")
    console.consume_directive(directive_id=boost["directive_id"], cycle_id="c1")
    SQLiteStateStore(daemon, POLICY).persist_selection("c1", Selection(
        next_question_id="q3", next_intent="attack", scores=[
            {"question_id": "q3", "score": 1.0, "est_cost": 1.0},
        ]))
    status, payload_raw = daemon.query_one(
        "SELECT status,payload_json FROM directive WHERE id=?", (boost["directive_id"],))
    assert status == "rejected"
    assert "未包含 q2" in json.loads(payload_raw)["rejection_reason"]
    assert daemon.query_one(
        "SELECT type FROM decision WHERE directive_id=? ORDER BY id DESC LIMIT 1",
        (boost["directive_id"],))[0] == "soft_directive_declined"


def test_invalid_selection_fallback_rejects_unapplied_hard_pin(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    pin = _inbound(console, "pin q3", "invalid-pin-q3")
    _confirm(daemon, console, pin)
    console.consume_directive(directive_id=pin["directive_id"], cycle_id="c1")
    with daemon.transaction() as conn:
        conn.execute(
            "INSERT INTO question_dep(question_id,dep_type,depends_on_question_id,status,created_cycle) "
            "VALUES (3,'question',2,'pending',1)")
    state = SQLiteStateStore(daemon, POLICY)
    with state.atomic():
        persist_selection_safe(state, "c1", {
            "next_question_id": "q2", "next_intent": "attack",
            "scores": [
                {"question_id": "q2", "score": 1.0, "est_cost": 1.0},
                {"question_id": "q3", "score": 2.0, "est_cost": 1.0},
            ],
        })
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (None, "terminate")
    assert daemon.query_one(
        "SELECT status FROM directive WHERE id=?", (pin["directive_id"],))[0] == "rejected"
    assert daemon.query_one(
        "SELECT type FROM decision WHERE directive_id=? ORDER BY id DESC LIMIT 1",
        (pin["directive_id"],))[0] == "directive_application_rejected"


def test_missing_required_score_converges_through_invalid_selection(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    boost = _inbound(console, "boost q2 1", "missing-score-q2")
    console.consume_directive(directive_id=boost["directive_id"], cycle_id="c1")
    state = SQLiteStateStore(daemon, POLICY)
    with state.atomic():
        persist_selection_safe(state, "c1", {
            "next_question_id": "q2", "next_intent": "attack",
            "scores": [{"question_id": "q2", "est_cost": 1.0}],
        })
    assert daemon.query_one(
        "SELECT next_question_id,next_intent FROM cycle WHERE id=1") == (None, "terminate")
    assert daemon.query_one(
        "SELECT status FROM directive WHERE id=?", (boost["directive_id"],))[0] == "rejected"
    reason = daemon.query_one(
        "SELECT json_extract(payload_json,'$.reason') FROM decision "
        "WHERE type='selection_invalid' ORDER BY id DESC LIMIT 1")[0]
    assert "缺必填字段" in reason and "score" in reason


def test_only_confirmed_new_pin_supersedes_older_pending_pin(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    first = _inbound(console, "pin q2", "pin-first")
    _confirm(daemon, console, first)
    second = _inbound(console, "pin q3", "pin-second")
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (first["directive_id"],))[0] == "pending"
    _confirm(daemon, console, second)
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (first["directive_id"],))[0] == "superseded"
    assert daemon.query_one("SELECT status FROM directive WHERE id=?", (second["directive_id"],))[0] == "pending"


def test_rejected_new_pin_preserves_older_confirmed_pin(env):
    daemon, console = env
    _seed_priority_questions(daemon)
    first = _inbound(console, "pin q2", "pin-preserved-first")
    _confirm(daemon, console, first)
    second = _inbound(console, "pin q3", "pin-rejected-second")
    _reject(daemon, console, second)
    assert daemon.query_one(
        "SELECT status FROM directive WHERE id=?", (first["directive_id"],))[0] == "pending"
    assert daemon.query_one(
        "SELECT status FROM directive WHERE id=?", (second["directive_id"],))[0] == "rejected"
