"""Owner-boundary fault injection; this is not real-model recovery evidence."""
from types import SimpleNamespace

from meta_research.owners.common import OwnerConflict
from test_public_plan_stage import _confirm_direct_quest
from test_root_workspace import _make, _channel, _call


def test_literature_owner_details_survive_and_original_channel_can_continue(tmp_path, monkeypatch):
    runtime = _make(tmp_path)
    try:
        _confirm_direct_quest(runtime)
        admitted = runtime.idea_stage.start('diagnostic-scope')
        run = runtime.owners.agent_runtime.query_idea_stage_run(admitted['stage_run_request']['request_ref'])
        assert run is not None
        channel = _channel(runtime, SimpleNamespace(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, root_session_ref=run.root_session_ref,
            fence_ref=run.fence_ref, capability_binding_hash=run.runtime_binding_hash,
            root_kind='idea', phase='primary'))

        def call(operation, **arguments):
            return _call(runtime, channel, 'research_memory.literature.' + operation, **arguments)
        registered = call('register', effect_id='paper', record_ref='doi:10.1000/diagnostic',
                          title='Available abstract', source='Publisher abstract', fulltext_status='missing')
        assert not registered['isError'], registered
        original = registered['structuredContent']['result']
        graph = runtime.owners.research_graph
        query = graph.query_literature

        def reject_obsolete_reference(ref, *, quest_ref):
            # Controlled Owner fault carrying the production asset-lifecycle
            # diagnostic shape; subsequent reads use the real Owner.
            if ref == 'obsolete-reference':
                raise OwnerConflict('asset_version_retired', {'version_ref': 'retired-version'})
            return query(ref, quest_ref=quest_ref)

        monkeypatch.setattr(graph, 'query_literature', reject_obsolete_reference)
        rejected = call('read', literature_ref='obsolete-reference')
        assert rejected['isError']
        assert rejected['structuredContent']['code'] == 'asset_version_retired'
        assert rejected['structuredContent']['details']['version_ref'] == 'retired-version'
        assert 'retired-version' in rejected['content'][0]['text']
        continued = call('read', literature_ref=original['ref'])
        assert not continued['isError'], continued
        assert continued['structuredContent']['result'] == original
        assert call('register.reconcile', effect_id='paper')['structuredContent']['result'] == original
    finally:
        runtime.close()
