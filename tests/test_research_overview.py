from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from meta_research.research_overview import ResearchOverviewReader
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.web import create_app
from test_public_plan_stage import (
    _DeterministicIdeaSkill, _DeterministicPlanSkill, _runtime,
    _confirm_direct_quest, _finish_idea_stage, _owner_revisions, _DeterministicDraftingAdapter,
)
from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider, DeterministicProbe, SnapshotAwareProposalDrafter,
    RecordingAcquisitionProvider,
)
from test_public_reasoning_stage import (
    _DeterministicReasoningSkill, _reasoning_runtime, _confirm_deepfetch_quest,
    _tick_reasoning, _force_question_switch, _confirm_waived_manual_question,
)


def reader(runtime):
    owners = runtime.owners
    return ResearchOverviewReader(owners.research_graph, owners.advancement_engine,
                                  owners.research_memory, owners.agent_runtime)


class CurrentReasoningSkill(_DeterministicReasoningSkill):
    # The installed provider contract uses advisory_unobserved. The older
    # shared fixture still advertises the retired child-review mode.
    def review_draft(self, request, draft):
        return replace(super().review_draft(request, draft),
                       review_mode='advisory_unobserved', reviewer_agent_ref=None)


def test_accepted_idea_and_plan_are_exact_and_reads_do_not_advance_owners(tmp_path: Path):
    runtime = _runtime(tmp_path / 'plan', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        before = reader(runtime).query(quest['quest_ref'])
        assert before['cycles'][0]['stages']['idea'] == []
        _finish_idea_stage(runtime)
        for _ in range(16):
            if runtime.plan_stage.query_current()['stage_commit'] is not None:
                break
            assert runtime.plan_stage.process_once()
        revisions = _owner_revisions(runtime)
        actual = reader(runtime).query(quest['quest_ref'])
        assert _owner_revisions(runtime) == revisions
        assert actual['status'] == 'ready'
        assert actual['cycle_ordinal'] == 1
        assert actual['findings'] == {'quest': [], 'question': [], 'cycle': []}
        idea = actual['cycles'][0]['stages']['idea'][0]
        plan = actual['cycles'][0]['stages']['plan'][0]
        assert idea['content'] == runtime.plan_stage.query_current()['stage_run_request']['accepted_idea_set_binding']['idea_set']
        plan_run = runtime.owners.agent_runtime.query_plan_stage_run(plan['source']['request_ref'])
        accepted = runtime.owners.research_memory.query_plan_document(plan_run.execution.submission_ref)
        assert plan['content'] == accepted.plan_document
        assert plan['source']['content_ref'] == accepted.content_ref
        assert plan['source']['content_hash'] == accepted.payload_hash
        assert reader(runtime).query(quest['quest_ref'])['projection_hash'] == actual['projection_hash']
    finally:
        runtime.close()


def test_wrong_accepted_outcome_reference_stays_a_gap(tmp_path: Path, monkeypatch):
    runtime = _runtime(tmp_path / 'wrong-ref', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        original = runtime.owners.research_graph.query_idea_outcome_decision
        monkeypatch.setattr(runtime.owners.research_graph, 'query_idea_outcome_decision',
                            lambda ref: replace(original(ref), outcome_ref='wrong-outcome'))
        actual = reader(runtime).query(quest['quest_ref'])
        assert actual['status'] == 'limited'
        artifact = actual['cycles'][0]['stages']['idea'][0]
        assert artifact['status'] == 'unavailable'
        assert artifact['content'] is None
        assert artifact['reason']['code'] == 'writing_stage_result_invalid'
        assert actual['findings']['quest'] == []
    finally:
        runtime.close()


def test_cycle_and_question_findings_never_borrow_other_scopes(tmp_path: Path):
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / 'reasoning'),
        proposal_drafter=SnapshotAwareProposalDrafter(),
        intent_drafting_provider=_DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(),
        deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=RecordingAcquisitionProvider(),
        idea_skill_provider=_DeterministicIdeaSkill(no_viable=True),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=False),
        reasoning_skill_provider=CurrentReasoningSkill(),
    )
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(14):
            view = _tick_reasoning(runtime)
            if view['stage_commit'] is not None:
                break
        else:
            raise AssertionError((runtime.reasoning_stage.transient_error, view['reasoning_acceptance'], view['run'].get('completion_rejection')))
        before = _owner_revisions(runtime)
        actual = reader(runtime).query(quest['quest_ref'])
        assert _owner_revisions(runtime) == before
        assert actual['status'] == 'ready'
        assert actual['cycle_ordinal'] == 2
        assert actual['cycle_ref'] != quest['cycle_ref']
        assert actual['question_ref'] == quest['question_ref']
        assert actual['findings']['cycle'] == []
        assert actual['findings']['quest'][0]['text'] == 'The frozen Goal gains bounded support.'
        assert actual['findings']['question'][0]['text'] == 'The frozen evidence advances the current Question.'
        artifact = actual['cycles'][0]['stages']['reasoning'][0]
        assert artifact['content']['research_synthesis']['cycle']['impact'] == 'One bounded finding.'
        assert artifact['source']['commit_ref'] == view['stage_commit']['commit_ref']
        assert actual['findings']['quest'][0]['text_field'] == 'research_synthesis.quest.impact'
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(human, quest_ref=quest['quest_ref'],
            parent_question_ref=quest['question_ref'], key_prefix='overview-child')
        for _ in range(8):
            if not human.reconcile_once():
                break
        child = runtime.owners.research_graph.query_question_tree(quest['quest_ref'])[-1]
        _force_question_switch(runtime, target_question_ref=child.question_ref, key_prefix='overview-switch')
        switched = reader(runtime).query(quest['quest_ref'])
        assert switched['question_ref'] == child.question_ref
        assert switched['cycle_ordinal'] == 3
        assert switched['findings']['question'] == []
        assert switched['findings']['cycle'] == []
        assert switched['findings']['quest'] == actual['findings']['quest']
        assert switched['cycles'][0] == actual['cycles'][0]
    finally:
        runtime.close()


def test_endpoint_requires_auth_and_is_read_only(tmp_path: Path, monkeypatch):
    monkeypatch.delenv('META_RESEARCH_TRUST_SSH_LOOPBACK', raising=False)
    runtime = _runtime(tmp_path / 'http', idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    client = None
    try:
        quest = _confirm_direct_quest(runtime)
        client = TestClient(create_app(runtime, base_url='http://testserver', control_key='test-only-key'))
        url = '/api/v1/research-overview'
        assert client.get(url, params={'quest_ref': quest['quest_ref']}).status_code == 401
        token = runtime.authentication.issue_bootstrap_token()
        assert client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'}, json={'token': token}).status_code == 200
        before = _owner_revisions(runtime)
        response = client.get(url, params={'quest_ref': quest['quest_ref']})
        assert response.status_code == 200
        assert response.json()['schema_ref'] == 'meta-research/research-overview/v1'
        assert response.json()['cycle_ref'] == quest['cycle_ref']
        assert _owner_revisions(runtime) == before
        assert client.get(url, params={'quest_ref': 'quest-does-not-exist'}).status_code == 404
        assert client.get(url).status_code == 422
    finally:
        if client is not None:
            client.close()
        runtime.close()
