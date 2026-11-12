import json

import pytest

from meta_research.owners.common import OwnerConflict
from test_human_guidance_inboxes import _InboxIdea, _run_idea, _finish
from test_human_history_discovery import _read


def test_history_reads_original_feedback_without_consuming_delivery(tmp_path):
    observed = {}

    def primary(human, operation):
        cut = human.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        directory = human.query_research_guidance(quest_ref=cut.binding.quest_ref)
        observed['feedback'] = human.feedback_operation_guidance(
            scope=operation.scope, binding=cut.binding, delivery_ref=delivery.delivery_ref,
            effect_id='decision', understanding='Keep the negative control.',
            changes='Compare devices separately.', continuing_work='Continue the audit.',
            reasons='Pooling hides the failed replication.', disposition='considered',
            goal_impact='none')
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            human.query_research_guidance(quest_ref=cut.binding.quest_ref,
                offset=1, query_revision=directory['query_revision'])

    runtime, quest = _run_idea(tmp_path, _InboxIdea(primary, lambda *_: None))
    try:
        human = runtime.owners.human_collaboration
        guide = human.submit_human_guidance(quest_ref=quest,
            original_text='Preserve the negative result.', strength=4, idempotency_key='guide')
        args = {k: v for k, v in human.query_research_guidance(quest_ref=quest)['items'][0]['reader'].items()
                if k != 'operation'}
        pending = json.loads(_read(runtime, quest, args)['text'])
        assert pending['treatment_state'] == 'not_processed'
        _finish(runtime)
        deliveries_before = human.query_guidance_deliveries(guide['constraint_ref'])
        exact = json.loads(_read(runtime, quest, args)['text'])
        assert exact['guidance']['text'] == 'Preserve the negative result.'
        assert exact['treatment_count'] == 1
        reader = exact['treatments_reader']
        page = json.loads(_read(runtime, quest, {k: v for k, v in reader.items() if k != 'operation'})['text'])
        item, = page['items']
        assert item['root_kind'] == 'idea'
        assert item['run_ref'] == observed['feedback']['binding']['identity']['run_ref']
        selected = {k: v for k, v in item['reader'].items() if k != 'operation'}
        body = json.loads(_read(runtime, quest, selected)['text'])
        assert body == observed['feedback']
        assert body['reasons'] == 'Pooling hides the failed replication.'
        assert human.query_guidance_deliveries(guide['constraint_ref']) == deliveries_before
        with pytest.raises(OwnerConflict, match='content_version_unbound'):
            human.read_research_guidance_treatment(item['treatment_ref'], quest_ref=quest, version_ref='wrong')
        with pytest.raises(OwnerConflict, match='content_source_unbound'):
            human.read_research_guidance_treatment(item['treatment_ref'], quest_ref='foreign',
                version_ref=item['reader']['version_ref'])
    finally:
        runtime.close()


def _multi_root_history(tmp_path):
    from test_guidance_scope_alignment import _confirm_guidance
    from test_human_guidance_providers import _tool
    from test_quest_goal_concurrent_roots import _ParallelBundle, _two_admitted_roots
    from test_target_root_finalizer import _current_bundle_runtime

    runtime = _current_bundle_runtime(tmp_path / 'multi-root', bundle_skill=_ParallelBundle())
    runtime.configure_resident_mcp_endpoint('http://127.0.0.1:8999')
    quest, admissions, target_refs = _two_admitted_roots(runtime)
    # The fixture admits Harness roots; native workspace history also needs
    # the Owner activation performed by the production Target worker.
    for target_ref in sorted(target_refs):
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target_ref)
        handle = runtime.target_run_authorities.agent_runtime.query_current_target_work_handle(target_ref)
        assert handle.target_run_ref in {item.run.run_ref for item in admissions}
        graph = runtime.owners.research_graph
        candidate = graph.query_target_candidate_projection(target_ref=target_ref).candidate
        formal_plan = graph.query_target_formal_plan_projection(graph_ref=launch.graph_ref).formal_plan
        runtime.target_root_lifecycle.activate(
            launch_ref=launch.launch_ref, handle=handle, candidate=candidate,
            formal_plan=formal_plan, idempotency_key='activate-guidance-history:' + target_ref,
        )
        assert runtime.owners.agent_runtime.query_target_root_handle_history(target_ref).handle_history == (handle,)
    guide = _confirm_guidance(runtime, quest, {'kind': 'quest', 'quest_ref': quest},
        strength=2, text='Consider calibration and preserve negative evidence.')
    observed = []
    adapter = runtime.harnesses._adapters['codex']
    original_invoke = adapter.invoke

    def invoke(invocation):
        if invocation.root_kind == 'target':
            delivery, = _tool(runtime, invocation.mcp_token, 'human_guidance.read')['deliveries']
            command = dict(delivery_ref=delivery['delivery_ref'], understanding='Consider the calibration.',
                changes='Retain device-level comparisons.', continuing_work='Continue useful work.',
                reasons='The control remains informative.', disposition='considered')
            first = invocation.run_ref == admissions[0].run.run_ref
            receipt = _tool(runtime, invocation.mcp_token, 'human_guidance.feedback', **command,
                effect_id='assessment', goal_impact='undetermined' if first else 'none')
            observed.append(receipt)
            if first:
                _tool(runtime, invocation.mcp_token, 'human_guidance.read',
                    delivery_ref=delivery['delivery_ref'], effect_id='read-assessment')
                observed.append(_tool(runtime, invocation.mcp_token, 'human_guidance.feedback',
                    **{**command, 'reasons': 'The completed comparison preserves the whole-Quest goal.'},
                    effect_id='resolved', goal_impact='none',
                    resolves_receipts=[receipt['receipt']['receipt_ref']]))
        return original_invoke(invocation)

    adapter.invoke = invoke
    for admission in admissions:
        runtime.harnesses.run_or_resume_target_root(admission.run.request_ref,
            prompt='Preserve actual guidance judgments.', mcp_base_url='http://127.0.0.1:8999')
    adapter.invoke = original_invoke
    binding = runtime.owners.human_collaboration.query_active_guidance_bindings('quest:' + quest)[0]
    return runtime, quest, binding, observed


def test_history_retains_superseded_assessments_and_actual_multiple_roots(tmp_path):
    from meta_research.research_content import read_content
    runtime, quest, guide, observed = _multi_root_history(tmp_path)
    try:
        human = runtime.owners.human_collaboration
        args = dict(quest_ref=quest, version_ref=guide['guidance_hash'], limit=1)
        first = human.query_research_guidance_treatments(guide['constraint_ref'], **args)
        seen = []
        page = first
        while True:
            seen.extend(page['items'])
            if page['next_offset'] is None:
                break
            page = human.query_research_guidance_treatments(guide['constraint_ref'], **args,
                offset=page['next_offset'], query_revision=page['query_revision'])
        assert len(seen) == 3
        assert len({item['run_ref'] for item in seen}) == 2
        exact = [human.read_research_guidance_treatment(item['treatment_ref'], quest_ref=quest,
            version_ref=item['reader']['version_ref']) for item in seen]
        assert {item['receipt']['receipt_ref'] for item in exact} == {item['receipt']['receipt_ref'] for item in observed}
        resolved = next(item for item in exact if item.get('resolves_receipts'))
        assert resolved['resolves_receipts'] == [observed[0]['receipt']['receipt_ref']]
        assert observed[0] in exact
        for item in seen:
            content = read_content(runtime.owners.research_graph, runtime.owners.research_memory,
                human_collaboration=human, quest_ref=quest, **item['content_reader'])
            assert json.loads(content['text']) in observed
        _assert_progressive_readers_and_revocation(runtime, quest, observed)
        with pytest.raises(OwnerConflict, match='research_query_revision_required'):
            human.query_research_guidance_treatments(guide['constraint_ref'], **args, offset=1)
        human.submit_human_guidance(quest_ref=quest, original_text='Unrelated later guidance.', idempotency_key='later')
        # A different guidance does not invalidate this exact guidance's treatment page.
        assert human.query_research_guidance_treatments(guide['constraint_ref'], **args,
            offset=1, query_revision=first['query_revision'])['items']
        from sqlalchemy import text
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE hc_guidance_effects SET receipt_hash='drift' WHERE effect_key=:ref"),
                {'ref': seen[-1]['treatment_ref']})
        with pytest.raises(OwnerConflict, match='research_query_changed'):
            human.query_research_guidance_treatments(guide['constraint_ref'], **args,
                offset=1, query_revision=first['query_revision'])
        with pytest.raises(OwnerConflict, match='guidance_receipt_invalid'):
            human.read_research_guidance_treatment(seen[-1]['treatment_ref'], quest_ref=quest, version_ref='drift')
    finally:
        runtime.close()


def _assert_progressive_readers_and_revocation(runtime, quest, observed):
    from test_dataset_effect_scope_recovery import _scope
    from test_root_workspace import _channel, _call, _accepted, _external_context
    from test_companion_authorization_revocation import revoke_quest_authority

    scope = _scope(runtime, quest_ref='a-different-selected-quest')
    channel = _channel(runtime, _external_context(scope))
    operations = _accepted(_call(runtime, channel, 'tools.catalog.page', view='operations',
        purpose='RG', topic='Human Interact'))
    selected = next(item for item in operations['items'] if item['operation_id'] == 'human_request.read')
    contract_reader = selected['contract_reader']
    contract = _accepted(_call(runtime, channel, contract_reader['operation'], **contract_reader['arguments']))
    assert 'treatment_ref' in contract['inputSchema']['properties']
    directory = _accepted(_call(runtime, channel, 'tools.call', operation_id='human_request.read',
        arguments={'entry_kind': 'guidance', 'source_quest_ref': quest}))
    guide_reader = json.loads(directory['text'])['items'][0]['reader']

    def follow(reader):
        return _accepted(_call(runtime, channel, 'tools.call', operation_id=reader['operation'],
            arguments={k: v for k, v in reader.items() if k != 'operation'}))

    guide = json.loads(follow(guide_reader)['text'])
    treatments_reader = guide['treatments_reader']
    assert treatments_reader['source_quest_ref'] == quest
    treatments = json.loads(follow(treatments_reader)['text'])
    treatment_reader = treatments['items'][0]['reader']
    assert treatment_reader['source_quest_ref'] == quest
    assert json.loads(follow(treatment_reader)['text']) in observed
    content_reader = treatments['items'][0]['content_reader']
    content = _accepted(_call(runtime, channel, 'research_memory.content.read', **content_reader))
    assert json.loads(content['text']) in observed
    revoke_quest_authority(runtime.owners.human_collaboration, quest)
    for reader in (guide_reader, treatments_reader, treatment_reader):
        denied = _call(runtime, channel, reader['operation'], **{k: v for k, v in reader.items() if k != 'operation'})
        assert denied['isError'] and denied['structuredContent']['code'] == 'content_quest_not_authorized'
    denied = _call(runtime, channel, 'research_memory.content.read', **content_reader)
    assert denied['isError'] and denied['structuredContent']['code'] == 'content_quest_not_authorized'
    runtime.owners.agent_runtime.complete_external_root_task_scope(root_kind='companion', root_runtime_scope=scope)
    status, denied, _ = runtime.harnesses.dispatch_mcp_http(channel.connection.token,
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
         'params': {'name': 'human_request.read', 'arguments': {}}}, mcp_session_id=None)
    assert status == 401 and denied['error']['code'] == 'mcp_channel_authentication_required'
