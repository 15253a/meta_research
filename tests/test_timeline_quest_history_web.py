from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_public_plan_stage import _runtime, _owner_revisions, _DeterministicIdeaSkill, _DeterministicPlanSkill


def _quest(runtime, prefix):
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, prefix + '-open')
    ref = opened['initialization_id']
    probed = human.observe_host_compute(ref, ['GPU-plan-test'], prefix + '-probe')
    draft = {**probed['quest_draft']['value'], 'goal': prefix + ': compare observational research',
        'completion_criteria': 'interpretable observations', 'time_budget': '30d', 'route': 'direct',
        'literature': {'mode': 'oa_only', 'library_entry_url': '', 'scope_exclusions': '', 'accepted_material_bindings': []},
        'background_and_initial_direction': 'compare existing observations'}
    human.revise_quest_draft(ref, draft, probed['quest_draft']['hash'], prefix + '-draft', probed['quest_draft']['revision'])
    current = human.query_quest_creation(ref)
    human.generate_question_proposal(ref, current['quest_draft']['hash'], prefix + '-proposal', current['quest_draft']['revision'])
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(ref)
    args = dict(quest_draft_revision=proposed['quest_draft']['revision'], quest_draft_hash=proposed['quest_draft']['hash'],
        proposal_ref=proposed['proposal']['ref'], proposal_hash=proposed['proposal']['hash'])
    preview = human.preview_confirmation(ref, **args, idempotency_key=prefix + '-preview')
    human.confirm_quest(ref, **args, preview_ref=preview['confirmation_preview']['ref'],
        preview_hash=preview['confirmation_preview']['hash'], idempotency_key=prefix + '-confirm')
    for _ in range(16):
        if human.query_quest_creation(ref)['status'] == 'completed':
            break
        if not human.reconcile_once():
            break
    assert human.query_quest_creation(ref)['status'] == 'completed'
    return runtime.owners.research_graph.query_question(ref)


def test_timeline_lists_every_readable_quest_across_pages_without_changing_research(tmp_path):
    runtime = _runtime(tmp_path / 'timeline-quests', idea_skill=_DeterministicIdeaSkill(), plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        one, two = _quest(runtime, 'first-history'), _quest(runtime, 'second-history')
        with TestClient(create_app(runtime, base_url='http://testserver', control_key='test-control')) as client:
            endpoint = '/api/v1/research-timeline/quests'
            assert client.get(endpoint).status_code == 401
            assert client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'},
                json={'token': runtime.authentication.issue_bootstrap_token()}).status_code == 200
            before = _owner_revisions(runtime)
            first = client.get(endpoint, params={'limit': 1}).json()
            assert first['schema_ref'] == 'meta-research/timeline-quests/v1'
            assert first['items'][0]['quest_ref'] == one.quest_ref
            assert first['items'][0]['goal'] == 'first-history: compare observational research'
            second = client.get(endpoint, params={'limit': 1, 'offset': first['next_offset']}).json()
            assert second['items'][0]['quest_ref'] == two.quest_ref
            assert second['next_offset'] is None
            # History belongs to the selected Quest, including a different foreground Quest.
            questions = client.get('/api/v1/research-library/questions', params={'quest_ref': one.quest_ref}).json()
            assert questions['items'][0]['question_ref'] == one.question_ref
            assert questions['items'][0]['status'] == 'active'
            overview = client.get('/api/v1/research-overview', params={'quest_ref': one.quest_ref}).json()
            assert overview['quest_ref'] == one.quest_ref
            assert overview['cycles'][0]['question_ref'] == one.question_ref
            assert _owner_revisions(runtime) == before
    finally:
        runtime.close()


def test_public_timeline_history_keeps_a_pruned_formal_question_without_a_cycle_readable(tmp_path):
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_public_advancement_runtime_control import _confirmed_control, _execute_control

    runtime = _runtime(tmp_path / 'timeline-question', idea_skill=_DeterministicIdeaSkill(), plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        root = _quest(runtime, 'root-history')
        human = runtime.owners.human_collaboration
        _confirm_waived_manual_question(human, quest_ref=root.quest_ref, parent_question_ref=root.question_ref, key_prefix='timeline-child')
        for _ in range(8):
            if not human.reconcile_once():
                break
        graph = runtime.owners.research_graph
        child = next(item for item in graph.query_question_tree(root.quest_ref) if item.question_ref != root.question_ref)
        foreground = runtime.owners.advancement_engine.query_foreground(root.quest_ref)
        command = _confirmed_control(human, scope_ref=f'quest:{root.quest_ref}', payload={'action': 'prune', 'target': {
            'quest_ref': root.quest_ref, 'cycle_ref': foreground['cycle_ref'], 'question_ref': foreground['question_ref'],
            'epoch': foreground['epoch'], 'target_question_ref': child.question_ref}, 'reason': 'operator_requested'}, key='timeline-prune')
        _execute_control(human, command, 'timeline-prune')
        with TestClient(create_app(runtime, base_url='http://testserver', control_key='test-control')) as client:
            assert client.post('/auth/bootstrap', headers={'Origin': 'http://testserver'},
                json={'token': runtime.authentication.issue_bootstrap_token()}).status_code == 200
            first = client.get('/api/v1/research-library/questions', params={'quest_ref': root.quest_ref, 'limit': 1}).json()
            second = client.get('/api/v1/research-library/questions', params={'quest_ref': root.quest_ref, 'limit': 1, 'offset': first['next_offset']}).json()
            pruned = next(item for item in first['items'] + second['items'] if item['question_ref'] == child.question_ref)
            assert pruned['status'] == 'pruned'
            assert second['next_offset'] is None
            overview = client.get('/api/v1/research-overview', params={'quest_ref': root.quest_ref}).json()
            assert all(item['question_ref'] != child.question_ref for item in overview['cycles'])
            original = client.get('/api/v1/research-content', params={'quest_ref': root.quest_ref, **pruned['reader']})
            assert original.status_code == 200
            assert original.json()['text']
    finally:
        runtime.close()
