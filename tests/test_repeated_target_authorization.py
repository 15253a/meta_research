"""Regression: a second identical grant must not strand an already resumed Target."""
from copy import deepcopy

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_public_human_collaboration_ladder import (
    _DeterministicDraftingProvider,
    _confirm_capability_command,
    _runtime,
)


SCOPE_REF = "human_request:human_request_repeat_target:r1"
REQUIREMENT = {
    "capability": "execute_high_risk_target",
    "scope": {
        "authorization_mode": "single_target",
        "quest_ref": "quest_repeat_target",
        "stage_request_ref": "stage_request_repeat_target",
        "graph_ref": "target_graph_repeat_target",
        "target_ref": "target_repeat_target",
        "target_spec_hash": "a" * 64,
    },
}


def _decide(human, requirement, outcome, key, scope_ref=SCOPE_REF):
    confirmation = _confirm_capability_command(
        human,
        scope_ref=scope_ref,
        capability=requirement["capability"],
        decision=outcome,
        capability_scope=requirement["scope"],
        key=key,
    )
    return human.decide_capability_authorization(
        scope_ref,
        {
            **requirement,
            "decision": outcome,
            "confirmation_receipt_ref": confirmation["confirmation_receipt"]["receipt_ref"],
        },
        key + "-authorization",
    )


@pytest.fixture
def runtime(tmp_path):
    value = _runtime(tmp_path / "repeated-grant", _DeterministicDraftingProvider())
    try:
        yield value
    finally:
        value.close()


def _verify(human, authorization, requirement=REQUIREMENT):
    human.verify_capability_authorization(
        requirement=requirement, receipt_ref=authorization["receipt_ref"]
    )


def test_continuous_identical_target_grants_preserve_original_receipt(runtime):
    human = runtime.owners.human_collaboration
    first = _decide(human, REQUIREMENT, "granted", "first")
    second = _decide(human, REQUIREMENT, "granted", "second")
    third = _decide(human, REQUIREMENT, "granted", "third")
    assert first["receipt_ref"] != second["receipt_ref"] != third["receipt_ref"]
    # Immutable history and the sole latest head remain exact.
    with runtime._database.read() as connection:
        records = connection.execute(
            text("SELECT revision, is_current FROM hc_capability_authorizations WHERE scope_ref = :scope ORDER BY revision DESC"),
            {"scope": SCOPE_REF},
        ).all()
    assert [r.revision for r in records] == [3, 2, 1]
    assert [bool(r.is_current) for r in records] == [True, False, False]
    for authorization in (first, second, third):
        _verify(human, authorization)


@pytest.mark.parametrize("outcome", ["denied", "revoked"])
@pytest.mark.parametrize("regrant", [False, True])
def test_denial_or_revocation_breaks_original_grant_even_after_regrant(runtime, outcome, regrant):
    human = runtime.owners.human_collaboration
    first = _decide(human, REQUIREMENT, "granted", "first")
    _decide(human, REQUIREMENT, outcome, "interrupt")
    if regrant:
        current = _decide(human, REQUIREMENT, "granted", "regrant")
        _verify(human, current)
    with pytest.raises(OwnerConflict, match="capability_authorization_receipt_invalid"):
        _verify(human, first)


@pytest.mark.parametrize("field", ["quest_ref", "stage_request_ref", "graph_ref", "target_ref", "target_spec_hash", "authorization_mode"])
def test_scope_change_interrupts_grant_even_when_latest_scope_matches_again(runtime, field):
    human = runtime.owners.human_collaboration
    first = _decide(human, REQUIREMENT, "granted", "first")
    different = deepcopy(REQUIREMENT)
    different["scope"][field] = "different"
    _decide(human, different, "granted", "different")
    current = _decide(human, REQUIREMENT, "granted", "current")
    _verify(human, current)
    with pytest.raises(OwnerConflict, match="capability_authorization_receipt_invalid"):
        _verify(human, first)


@pytest.mark.parametrize("revision", [1, 2, 3])
@pytest.mark.parametrize("tamper", ["receipt", "confirmation"])
def test_every_reaffirmation_requires_its_original_valid_receipt_and_confirmation(runtime, revision, tamper):
    human = runtime.owners.human_collaboration
    grants = [_decide(human, REQUIREMENT, "granted", f"grant-{i}") for i in range(3)]
    selected = grants[revision - 1]
    with runtime._database.write() as connection:
        if tamper == "receipt":
            connection.execute(
                text("UPDATE hc_capability_authorizations SET receipt_hash = :bad WHERE authorization_ref = :ref"),
                {"bad": "0" * 64, "ref": selected["authorization_ref"]},
            )
        else:
            connection.execute(
                text("UPDATE hc_command_confirmations SET receipt_hash = :bad WHERE confirmation_ref = :ref"),
                {"bad": "0" * 64, "ref": selected["confirmation_receipt_ref"]},
            )
    with pytest.raises(OwnerConflict, match="capability_authorization_receipt_invalid"):
        _verify(human, grants[0])


@pytest.mark.parametrize("case", ["other_capability", "quest_scope", "non_single_target"])
def test_reaffirmation_rule_is_limited_to_request_scoped_single_target_grants(runtime, case):
    human = runtime.owners.human_collaboration
    requirement = deepcopy(REQUIREMENT)
    scope_ref = SCOPE_REF
    if case == "other_capability":
        requirement["capability"] = "external_publish"
    elif case == "quest_scope":
        scope_ref = "quest:quest_repeat_target"
    else:
        requirement["scope"]["authorization_mode"] = "all_targets"
    first = _decide(human, requirement, "granted", "first", scope_ref)
    current = _decide(human, requirement, "granted", "current", scope_ref)
    _verify(human, current, requirement)
    with pytest.raises(OwnerConflict, match="capability_authorization_receipt_invalid"):
        _verify(human, first, requirement)


def test_duplicate_grant_after_root_resume_still_launches_exactly_one_target(tmp_path):
    from test_public_bundle_stage import (
        _RequestScopedHighRiskBundleSkill,
        _bundle_runtime,
        _confirm_direct_quest,
        _finish_idea_stage,
        _finish_plan_stage,
        _grant_request_capability,
        _open_root_target_authorization_request,
    )

    provider = _RequestScopedHighRiskBundleSkill()
    runtime = _bundle_runtime(tmp_path / "resumed-target", bundle_skill_provider=provider)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _ in range(12):
            assert runtime.bundle_stage.process_once()
            if provider.schedule_requests:
                break
        assert provider.schedule_requests
        request = _open_root_target_authorization_request(runtime, provider.schedule_requests[0])
        original = _grant_request_capability(runtime, request)
        request_ref = request["request_ref"]
        accepted = runtime.owners.agent_runtime.query_human_request(request_ref)
        assert accepted["status"] == "satisfied"
        assert accepted["direct_waiters"][0]["status"] == "consumed"
        target_ref = request["required_authorization"]["scope"]["target_ref"]
        assert runtime.owners.agent_runtime.query_target_launch_ack(target_ref) is None
        newer = _decide(
            runtime.owners.human_collaboration,
            request["required_authorization"], "granted", "duplicate-after-resume",
            f"human_request:{request_ref}",
        )
        assert newer["revision"] == 2
        assert newer["receipt_ref"] != original["receipt_ref"]
        for _ in range(12):
            runtime.bundle_stage.process_once()
            ack = runtime.owners.agent_runtime.query_target_launch_ack(target_ref)
            if ack is not None:
                break
        assert ack is not None, runtime.bundle_stage.transient_error
        assert ack.target_ref == target_ref
        # Replays may read the original response/consumption; never regrant or relaunch.
        persisted = runtime.owners.agent_runtime.query_human_request(request_ref)
        assert persisted["evaluation"]["accepted_evidence_refs"] == [original["receipt_ref"]]
        assert persisted["direct_waiters"] == accepted["direct_waiters"]
        runtime.bundle_stage.process_once()
        assert runtime.owners.agent_runtime.query_target_launch_ack(target_ref) == ack
    finally:
        runtime.close()
