"""Human originals outlive receiving work; access still follows current scope."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from meta_research.web import create_app
from test_root_workspace import _make, _channel, _call, _accepted, _external_context
from test_dataset_effect_scope_recovery import _scope
from test_public_plan_stage import _confirm_direct_quest
from test_public_work_materials import _source
from test_public_human_reply_delivery import _login


@pytest.mark.parametrize('entry', ['creation', 'manual'])
def test_human_directory_remains_quest_scoped_after_receiving_work_ends(tmp_path, monkeypatch, entry):
    original = tmp_path / 'human-original'
    original.mkdir()
    (original / 'note.txt').write_bytes(b'Human original remains read only.')
    saved = {}
    runtime = _make(tmp_path / 'runtime')
    human = runtime.owners.human_collaboration
    try:
        if entry == 'creation':
            confirm = human.confirm_quest
            def confirm_with_directory(initialization_id, *args, **kwargs):
                result = confirm(initialization_id, *args, **kwargs)
                # An explicit source selected at the confirmed creation boundary,
                # before dispatch; no model comprehension or adoption is claimed.
                saved.update(human.register_work_materials(anchor_kind='creation', anchor_ref=initialization_id,
                    command={'receiver': human.material_receiver('creation', initialization_id),
                        'selections': [runtime.root_workspaces.server_files.inspect(str(original))],
                        'description': 'Read original in later work.'}, idempotency_key='creation-directory'))
                return result
            monkeypatch.setattr(human, 'confirm_quest', confirm_with_directory)
        confirmed = _confirm_direct_quest(runtime)
        quest = confirmed['quest_ref']
        if entry == 'manual':
            with TestClient(create_app(runtime, base_url='http://testserver', control_key='control')) as client:
                headers = _login(client, runtime)
                response = client.post('/api/v1/manual-question-creations',
                    json={'quest_ref': quest, 'parent_question_ref': confirmed['receipts']['question_identity']['subject_ref']},
                    headers={**headers, 'Idempotency-Key': 'open-manual-directory'})
                assert response.status_code == 201, response.text
                context = response.json()['context_ref']
                response = client.post(f'/api/v1/manual-question-creations/{context}/material-references',
                    json={'receiver': human.material_receiver('manual', context),
                        'selections': [_source(client, original)], 'description': 'Original directory.'}, headers=headers)
                assert response.status_code == 201, response.text
                saved.update(response.json())
        reference = saved['references'][0]['reference_ref']
        # Four later independent authorized roots have no receiving-work directory.
        # No additional scientific Cycle execution is needed to exercise the scope boundary.
        for ordinal in range(1, 5):
            scope = _scope(runtime, quest_ref=quest, run_ref=f'later-{ordinal}', root_ref=f'root-{ordinal}')
            context = _external_context(scope)
            channel = _channel(runtime, context)
            listed = _accepted(_call(runtime, channel, 'research_workspace.materials.discover'))
            assert reference in {item['reference_ref'] for item in listed['references']}
            page = _accepted(_call(runtime, channel, 'research_workspace.materials.discover', reference_ref=reference))
            note = next(item for item in page['entries'] if item['name'] == 'note.txt')
            args = dict(reference_ref=reference, path='note.txt', observation_ref=note['observation']['observation_ref'])
            assert _accepted(_call(runtime, channel, 'research_workspace.materials.read', **args))['text'] == 'Human original remains read only.'
            runtime.owners.agent_runtime.complete_external_root_task_scope(root_kind='companion', root_runtime_scope=scope)
            status, _, _ = runtime.harnesses.dispatch_mcp_http(channel.connection.token,
                {'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
                    'params': {'name': 'research_workspace.materials.read', 'arguments': args}}, mcp_session_id=None)
            assert status == 401
        foreign = _external_context(_scope(runtime, quest_ref='other-quest', run_ref='foreign', root_ref='foreign'))
        assert _call(runtime, _channel(runtime, foreign), 'research_workspace.materials.read', **args)['structuredContent']['code'] == 'material_not_visible'
        scope = _scope(runtime, quest_ref=quest, run_ref='offline', root_ref='offline')
        channel = _channel(runtime, _external_context(scope))
        (original / 'note.txt').unlink()
        original.rmdir()
        denied = _call(runtime, channel, 'research_workspace.materials.read', **args)
        assert denied['structuredContent']['code'] == 'material_missing'
    finally:
        runtime.close()


@pytest.mark.parametrize('source_kind', ['server_reference', 'linked_local'])
def test_reply_directory_and_upload_keep_actual_receiver_and_later_quest_access(tmp_path, source_kind):
    import base64
    from test_public_human_reply_delivery import _open, _complete_receiving_batch
    runtime = _make(tmp_path / 'runtime')
    original = tmp_path / 'reply-original'
    original.mkdir()
    (original / 'note.txt').write_text('Original reply data.')
    try:
        scope, context, opened = _open(runtime)
        with TestClient(create_app(runtime, base_url='http://testserver', control_key='control')) as client:
            headers = _login(client, runtime)
            response = client.post(f"/api/v1/human-requests/{opened['request_ref']}/responses", headers=headers,
                json={'decision': 'provided', 'facts': {}, 'note': 'Original plus uploaded note.', 'materials': [
                    ({'kind': 'server_reference', 'selection': _source(client, original)} if source_kind == 'server_reference'
                        else {'kind': 'linked_local', 'locator': str(original), 'description': 'Original reply directory.'}),
                    {'kind': 'upload', 'relative_path': 'answer.bin', 'media_type': 'application/octet-stream',
                        'content_base64': base64.b64encode(b'\x00\xffanswer').decode()}]})
            assert response.status_code == 201, response.text
            delivery = response.json()['delivery']
        assert delivery['human_request_ref'] == opened['request_ref']
        assert delivery['root_session_ref'] == scope['root_session_ref']
        assert delivery['work_ref'] == scope['root_session_ref']
        upload = delivery['uploaded_readers'][0]
        page = _accepted(_call(runtime, _channel(runtime, context), 'research_workspace.read', **upload))
        assert base64.b64decode(page['base64']) == b'\x00\xffanswer'
        reference = delivery['work_materials'][0]['reference_ref']
        receiver = runtime.owners.human_collaboration.query_work_material(reference)['receiver']
        _complete_receiving_batch(runtime, scope)
        for ordinal in range(4):
            later = _external_context(_scope(runtime, quest_ref=scope['quest_ref'], run_ref=f'next-{ordinal}', root_ref=f'next-{ordinal}'))
            page = _accepted(_call(runtime, _channel(runtime, later), 'research_workspace.materials.discover', reference_ref=reference))
            entry = page['entries'][0]
            result = _accepted(_call(runtime, _channel(runtime, later), 'research_workspace.materials.read',
                reference_ref=reference, path='note.txt', observation_ref=entry['observation']['observation_ref']))
            assert result['text'] == 'Original reply data.'
        assert runtime.owners.human_collaboration.query_work_material(reference)['receiver'] == receiver
        assert (original / 'note.txt').read_text() == 'Original reply data.'
    finally:
        runtime.close()


def test_guidance_directory_is_readable_by_later_stage_without_expanding_file_scan(tmp_path, monkeypatch):
    from test_public_plan_stage import _DeterministicIdeaSkill, _DeterministicPlanSkill, _finish_idea_stage
    from test_root_workspace import _context
    from guidance_confirmation_helpers import prepare_guidance_submission
    saved = {}
    original = tmp_path / 'guidance-original'
    original.mkdir()
    (original / 'note.txt').write_text('Guidance original.')
    class Submit(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            if not saved:
                human = self.runtime.owners.human_collaboration
                receiver = human.current_material_receiver(quest)
                command = {'receiver': receiver,
                    'selections': [self.runtime.root_workspaces.server_files.inspect(str(original))],
                    'description': 'Read the source, no automatic adoption.'}
                with TestClient(create_app(self.runtime, base_url='http://testserver', control_key='control')) as client:
                    headers = _login(client, self.runtime)
                    proposal = prepare_guidance_submission(human, quest, 'Check observations.', key='cross-window', work_materials=command)
                    response = client.post('/api/v1/human-collaboration/guidance', headers=headers, json=proposal)
                    assert response.status_code == 201, response.text
                    saved.update(response.json()['work_materials'][0])
            return super().generate_draft(request)
    class Read(_DeterministicPlanSkill):
        def generate_draft(self, request):
            context = _context(request, 'plan')
            current = self.runtime.root_workspaces.bind_runtime(context).location
            # Construct the same boundary as an out-of-window receiving Cycle:
            # current authorized Stage exists, old workspace is no longer visible.
            with monkeypatch.context() as patch:
                patch.setattr(self.runtime.root_workspaces, '_visible', lambda _context: (current,))
                ref = saved['references'][0]['reference_ref']
                channel = _channel(self.runtime, context)
                listing = _accepted(_call(self.runtime, channel, 'research_workspace.materials.discover'))
                assert ref in {item['reference_ref'] for item in listing['references']}
                page = _accepted(_call(self.runtime, channel, 'research_workspace.materials.discover', reference_ref=ref))
                value = _accepted(_call(self.runtime, channel, 'research_workspace.materials.read',
                    reference_ref=ref, path='note.txt', observation_ref=page['entries'][0]['observation']['observation_ref']))
                assert value['text'] == 'Guidance original.'
            self.observed = True
            return super().generate_draft(request)
    reader = Read(no_gap=False)
    runtime = _make(tmp_path / 'runtime', idea=Submit(), plan=reader)
    try:
        quest = _confirm_direct_quest(runtime)['quest_ref']
        _finish_idea_stage(runtime)
        for _ in range(6):
            runtime.plan_stage.process_once()
            if getattr(reader, 'observed', False):
                break
        assert reader.observed
    finally:
        runtime.close()
