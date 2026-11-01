"""Selected Stage material travels through the public intake and exact reader."""
import base64
from types import SimpleNamespace

import pytest

from meta_research.research_content import research_content_operations
from meta_research.semantic_mcp import SemanticMcpGateway, SemanticCallContext
from test_dataset_effect_scope_recovery import _scope
from test_root_workspace import _make
from test_public_plan_stage import _confirm_direct_quest


def channel(runtime, question, generation=1):
    scope = _scope(runtime, quest_ref=question.quest_ref, run_ref=question.quest_ref,
                   root_ref=question.question_ref, generation=generation)
    context = SemanticCallContext(scope['run_ref'], scope['attempt_ref'],
        scope['root_session_ref'], scope['fence_ref'], scope['runtime_binding_hash'],
        'companion', 'primary', '')
    return context, context_channel(runtime, context)


def context_channel(runtime, context):
    operations = research_content_operations(research_graph=runtime.owners.research_graph,
        research_memory=runtime.owners.research_memory, agent_runtime=runtime.owners.agent_runtime,
        human_collaboration=runtime.owners.human_collaboration)
    gateway = SemanticMcpGateway(operations)
    token, _ = gateway.issue_channel(run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        capability_binding_hash=context.capability_binding_hash, root_kind='companion',
        phase='primary', operation_ids=tuple(op.semantic_operation_id for op in operations))

    def call(operation, **arguments):
        _, result = gateway.dispatch(token.token, {'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
            'params': {'name':operation, 'arguments':arguments}})
        return result['result']
    return call


@pytest.mark.parametrize('custody', ['managed', 'linked_local'])
@pytest.mark.parametrize('directory', [False, True])
def test_selected_workspace_binary_or_directory_has_exact_reader_and_replay(tmp_path, custody, directory):
    runtime = _make(tmp_path / 'runtime')
    try:
        confirmed = _confirm_direct_quest(runtime)
        question = SimpleNamespace(quest_ref=confirmed['quest_ref'], question_ref=confirmed['question_ref'])
        context, call = channel(runtime, question)
        location = runtime.owners.human_collaboration._root_workspaces.bind_runtime(context).location
        source = location.directory / 'selected'
        literal = b'\xff\x00selected binary\x80'
        if directory:
            source.mkdir()
            (source / 'empty').mkdir()
            (source / 'data.bin').write_bytes(literal)
            (source / 'notes.txt').write_text('A separately selected directory entry.')
        else:
            source.write_bytes(literal)
        (location.directory / 'unselected.bin').write_bytes(b'not selected')
        args = {'effect_id':'select', 'intake': {'source_kind':'local_path', 'custody_mode':custody,
            'display_name':'selected', 'source': {'kind':'workspace',
                'workspace_ref':location.workspace_ref, 'path':'selected'}}}
        result = call('research_memory.assets.intake', **args)
        assert not result['isError'], result
        accepted = result['structuredContent']
        assert accepted['status'] == 'accepted', accepted
        assert accepted['asset']['custody_modes'] == [custody]
        assert 'producer_kind' not in accepted['asset']['provenance']
        reader = dict(accepted['reader'])
        operation = reader.pop('operation')
        if directory:
            manifest = call(operation, **reader)['structuredContent']
            assert {entry['path'] for entry in manifest['entries']} == {'data.bin', 'notes.txt'}
            reader['entry_path'] = 'data.bin'
        page = call(operation, **reader)
        assert not page['isError'], page
        assert page['structuredContent']['encoding'] == 'base64'
        assert base64.b64decode(page['structuredContent']['text']) == literal
        assert call('research_memory.assets.intake', **args)['structuredContent'] == accepted
        assert call('research_memory.assets.intake.reconcile', **args)['structuredContent']['result'] == accepted
        if not directory:
            source.unlink()
            assert call('research_memory.assets.intake.reconcile', **args)['structuredContent']['result'] == accepted
            after = call(operation, **reader)
            assert after['isError'] is (custody == 'linked_local')
    finally:
        runtime.close()


def test_intake_checks_current_source_authority_and_keeps_declared_provenance_unverified(tmp_path):
    runtime = _make(tmp_path / 'runtime')
    try:
        confirmed = _confirm_direct_quest(runtime)
        question = SimpleNamespace(quest_ref=confirmed['quest_ref'], question_ref=confirmed['question_ref'])
        context, call = channel(runtime, question)
        human = runtime.owners.human_collaboration
        location = human._root_workspaces.bind_runtime(context).location
        (location.directory / 'sample.bin').write_bytes(b'\xffsample')
        source = {'kind':'workspace', 'workspace_ref':location.workspace_ref, 'path':'sample.bin'}
        intake = {'source_kind':'local_path', 'custody_mode':'managed', 'display_name':'sample.bin', 'source':source}
        missing = call('research_memory.assets.intake', effect_id='missing',
            intake={**intake, 'source':{**source, 'path':'missing.bin'}})
        assert missing['structuredContent']['code'] == 'material_missing'
        denied = call('research_memory.assets.intake', effect_id='denied',
            intake={**intake, 'source':{**source, 'workspace_ref':'not-authorized'}})
        assert denied['structuredContent']['code'] == 'workspace_not_visible'
        traversal = call('research_memory.assets.intake', effect_id='traversal',
            intake={**intake, 'source':{**source, 'path':'../outside'}})
        assert traversal['structuredContent']['code'] == 'material_path_invalid'
        ambiguous = call('research_memory.assets.intake', effect_id='ambiguous', intake={**intake, 'text':'wrong'})
        assert ambiguous['isError']
        forged = call('research_memory.assets.intake', effect_id='forged',
            intake={**intake, 'provenance':{'producer_kind':'target_workspace'}})
        assert forged['structuredContent']['code'] == 'target_note_provenance_reserved'
        absent = call('research_memory.assets.intake.reconcile', effect_id='unknown',
            intake={**intake, 'source':{**source, 'path':'missing.bin'}})
        assert absent['structuredContent'] == {'status':'not_found', 'result':None}
        accepted = call('research_memory.assets.intake', effect_id='declared',
            intake={**intake, 'provenance':{'claim':'Provided by a collaborator; not independently verified.'}})
        assert not accepted['isError'], accepted
        provenance = accepted['structuredContent']['asset']['provenance']
        assert provenance['source_work']['work_ref'] == context.run_ref
        assert provenance['declared_provenance']['claim'].startswith('Provided by')
        assert 'producer_kind' not in provenance
        channel(runtime, question, generation=2)
        stale = call('research_memory.assets.intake', effect_id='stale', intake=intake)
        assert stale['isError'] and 'scope_stale' in stale['structuredContent']['code']
    finally:
        runtime.close()


@pytest.mark.parametrize('custody', ['managed', 'linked_local'])
def test_material_directory_intake_retains_original_receiver_and_selected_subtree(tmp_path, custody):
    runtime = _make(tmp_path / 'runtime')
    try:
        confirmed = _confirm_direct_quest(runtime)
        question = SimpleNamespace(quest_ref=confirmed['quest_ref'], question_ref=confirmed['question_ref'])
        context, call = channel(runtime, question)
        original = tmp_path / 'external'
        original.mkdir()
        (original / 'keep.bin').write_bytes(b'\xfeexternal')
        (original / 'not-selected.bin').write_bytes(b'untouched')
        human = runtime.owners.human_collaboration
        registered = human.acquire_work_material(context=context, effect_id='acquired',
            absolute_path=str(original), description='Received data for analysis.')
        reference = registered['references'][0]['reference_ref']
        args = {'effect_id':'external', 'intake':{'source_kind':'local_path', 'custody_mode':custody,
            'display_name':'keep.bin', 'source':{'kind':'material', 'reference_ref':reference, 'path':'keep.bin'}}}
        accepted = call('research_memory.assets.intake', **args)
        assert not accepted['isError'], accepted
        value = accepted['structuredContent']
        assert value['status'] == 'accepted', value
        assert value['asset']['provenance']['receiver'] == registered['receiver']
        assert 'producer_kind' not in value['asset']['provenance']
        reader = dict(value['reader']); operation = reader.pop('operation')
        page = call(operation, **reader)
        assert base64.b64decode(page['structuredContent']['text']) == b'\xfeexternal'
        assert (original / 'not-selected.bin').read_bytes() == b'untouched'
        (original / 'keep.bin').unlink()
        assert call('research_memory.assets.intake.reconcile', **args)['structuredContent']['result'] == value
    finally:
        runtime.close()


@pytest.mark.parametrize('boundary', ['source_change', 'revocation'])
@pytest.mark.parametrize('custody', ['managed', 'linked_local'])
@pytest.mark.parametrize('directory', [False, True])
def test_intake_rechecks_source_and_fence_before_accepting_prepared_bytes(tmp_path, monkeypatch, boundary, custody, directory):
    import hashlib
    import os
    import meta_research.server_materials as server_materials
    from meta_research.owners.research_memory import _file_matches

    runtime = _make(tmp_path / 'runtime')
    try:
        confirmed = _confirm_direct_quest(runtime)
        question = SimpleNamespace(quest_ref=confirmed['quest_ref'], question_ref=confirmed['question_ref'])
        context, call = channel(runtime, question)
        location = runtime.root_workspaces.bind_runtime(context).location
        source = location.directory / 'selected.txt'
        if directory:
            source.mkdir()
        content_source = source / 'entry.txt' if directory else source
        content_source.write_bytes(b'original bytes')
        source_stat = source.stat()
        content_stat = content_source.stat()
        observed = server_materials.observation(source_stat)
        observe = server_materials.observation
        if boundary == 'source_change':
            # Coarse timestamps and a warm fingerprint must not turn unchanged
            # metadata into proof of unchanged original bytes.
            assert _file_matches(content_source, 14, hashlib.sha256(b'original bytes').hexdigest())
            monkeypatch.setattr(server_materials, 'observation', lambda info:
                observed if (info.st_dev, info.st_ino) == (source_stat.st_dev, source_stat.st_ino)
                else observe(info))
        memory = runtime.owners.research_memory
        original = memory._accept_prepared_asset
        def intervene(*args, **kwargs):
            monkeypatch.setattr(memory, '_accept_prepared_asset', original)
            if boundary == 'source_change':
                content_source.write_bytes(b'changed bytes!')
                os.utime(content_source, ns=(content_stat.st_atime_ns, content_stat.st_mtime_ns))
                assert server_materials.observation(source.stat()) == observed
            else:
                channel(runtime, question, generation=2)
            return original(*args, **kwargs)
        monkeypatch.setattr(memory, '_accept_prepared_asset', intervene)
        result = call('research_memory.assets.intake', effect_id='changed-before-accept',
            intake={'source_kind':'directory' if directory else 'local_path', 'custody_mode':custody, 'display_name':'selected.txt',
                'source':{'kind':'workspace', 'workspace_ref':location.workspace_ref, 'path':'selected.txt'}})
        assert not result['isError'], result
        value = result['structuredContent']
        assert value['asset'] is None
        assert value['status'] == ('queued' if boundary == 'source_change' else 'failed')
        if boundary == 'revocation':
            assert 'scope_stale' in value['failure']['code']
    finally:
        runtime.close()


@pytest.mark.parametrize('source_kind', ['workspace', 'material'])
@pytest.mark.parametrize('outcome', ['accepted', 'failed', 'exhausted', 'queued'])
def test_lost_intake_ack_reconciles_exact_operation_while_suspended_without_original(tmp_path, monkeypatch, source_kind, outcome):
    from dataclasses import replace
    from test_root_workspace import _channel, _call, _accepted
    from test_root_human_request_lifecycle import _open_arguments

    class LostAck(BaseException):
        pass

    runtime = _make(tmp_path / 'runtime')
    try:
        confirmed = _confirm_direct_quest(runtime)
        question = SimpleNamespace(quest_ref=confirmed['quest_ref'], question_ref=confirmed['question_ref'])
        context, call = channel(runtime, question)
        location = runtime.root_workspaces.bind_runtime(context).location
        original = location.directory / 'original.bin'
        if outcome == 'failed':
            original.mkdir()
            outside = location.directory / 'outside.bin'
            outside.write_bytes(b'original identity')
            (original / 'unsupported-link').symlink_to(outside)
        else:
            original.write_bytes(b'\xfforiginal identity')
        if outcome == 'exhausted':
            monkeypatch.setattr('meta_research.owners.research_memory.ASSET_INTAKE_MAX_ATTEMPTS', 1)
        source = {'kind':'workspace', 'workspace_ref':location.workspace_ref, 'path':'original.bin'}
        if source_kind == 'material':
            registered = runtime.owners.human_collaboration.acquire_work_material(context=context,
                effect_id='acquired', absolute_path=str(original), description='Material used for exact reconciliation.')
            source = {'kind':'material', 'reference_ref':registered['references'][0]['reference_ref'], 'path':''}
        args = {'effect_id':'lost-ack', 'intake': {'source_kind':'directory' if outcome in {'exhausted', 'queued'} else 'local_path', 'custody_mode':'linked_local',
            'display_name':'original.bin', 'source':source}}
        memory = runtime.owners.research_memory
        submit = memory.submit_asset_intake
        committed = []
        def lose_ack(*a, **kw):
            result = submit(*a, **kw)
            committed.append(result.as_public_dict())
            raise LostAck()
        with monkeypatch.context() as fault:
            fault.setattr(memory, 'submit_asset_intake', lose_ack)
            with pytest.raises(LostAck):
                call('research_memory.assets.intake', **args)
        expected_status = outcome if outcome in {'accepted', 'queued'} else 'failed'
        assert committed[0]['status'] == expected_status
        if outcome in {'failed', 'exhausted'}:
            assert committed[0]['failure']['code'] == ('asset_intake_retry_exhausted' if outcome == 'exhausted' else 'asset_source_symlink_unsupported')
        _accepted(_call(runtime, _channel(runtime, context), 'human_request.open',
            **_open_arguments('wait-after-lost-ack', 'offline_action')))
        assert runtime.owners.agent_runtime.query_managed_run(context.run_ref)['status'] == 'suspended'
        if original.is_dir():
            (original / 'unsupported-link').unlink()
            original.rmdir()
        else:
            original.unlink()
        reconciled = call('research_memory.assets.intake.reconcile', **args)
        assert not reconciled['isError'], reconciled
        assert reconciled['structuredContent'] == {'status':expected_status, 'result':committed[0]}
        changed = call('research_memory.assets.intake.reconcile', **{**args,
            'intake':{**args['intake'], 'display_name':'different.bin'}})
        assert changed['structuredContent']['code'] == 'asset_intake_idempotency_conflict'
        changed_source = call('research_memory.assets.intake.reconcile', **{**args,
            'intake':{**args['intake'], 'source':{**source, 'path':'other.bin'}}})
        assert changed_source['structuredContent']['code'] == 'asset_intake_idempotency_conflict'
        assert call('research_memory.assets.intake', **{**args, 'effect_id':'new-work'})['isError']
        assert call('research_memory.assets.intake.reconcile', **{**args, 'effect_id':'unknown'})['structuredContent'] == {
            'status':'not_found', 'result':None}
        stale = context_channel(runtime, replace(context, fence_ref='old-fence'))
        denied = stale('research_memory.assets.intake.reconcile', **args)
        assert denied['isError'] and 'scope_stale' in denied['structuredContent']['code']
    finally:
        runtime.close()
