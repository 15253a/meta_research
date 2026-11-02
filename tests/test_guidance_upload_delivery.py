import base64
from contextlib import closing
import pytest
from sqlalchemy import text

from fastapi.testclient import TestClient

from meta_research.web import create_app
from guidance_confirmation_helpers import prepare_guidance_submission
from test_public_human_reply_delivery import _login
from test_public_idea_stage import _DeterministicIdeaSkill, _runtime, _confirm_direct_quest


@pytest.mark.parametrize('failed_commit', [False, True])
def test_guidance_binary_upload_keeps_receiving_work_and_exact_replay(tmp_path, monkeypatch, failed_commit):
    observed = {}
    literal = b'\xff\x00human measurement\x80'

    class Submit(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            human = self.runtime.owners.human_collaboration
            # This test drives the Stage synchronously; starting application
            # workers here would re-enter this provider during fault injection.
            with closing(TestClient(create_app(self.runtime, base_url='http://testserver', control_key='control'))) as client:
                headers = _login(client, self.runtime)
                receiver = client.get('/api/v1/work-materials/receiver', params={'quest_ref': self.quest}).json()
                command = {'receiver': receiver, 'selections': [], 'description': 'Supplied measurement',
                    'uploads': [{'relative_path': 'measurement.bin', 'media_type': 'application/octet-stream',
                        'content_base64': base64.b64encode(literal).decode()}]}
                body = prepare_guidance_submission(human, self.quest, 'Use this measurement as a reference.',
                    key='binary-guidance', work_materials=command)
                if failed_commit:
                    record = human._feed.record
                    def fail_acceptance(connection, kind, payload):
                        if kind == 'human_collaboration.work_materials_submitted':
                            raise OSError('injected acceptance transaction failure')
                        return record(connection, kind, payload)
                    with monkeypatch.context() as patch:
                        patch.setattr(human._feed, 'record', fail_acceptance)
                        with pytest.raises(OSError, match='injected acceptance'):
                            client.post('/api/v1/human-collaboration/guidance', json=body,
                                headers={**headers, 'Idempotency-Key': 'confirm-binary-guidance'})
                    # The durable pending command owns any published files;
                    # acceptance rollback must not erase its recovery identity.
                    with human._database.read() as connection:
                        pending = connection.execute(text('SELECT state FROM hc_work_material_submissions WHERE idempotency_key=:key'),
                            {'key': 'confirm-binary-guidance'}).scalar()
                    assert pending == 'pending'
                    assert human.query_collaboration_projection(('quest:' + self.quest,))['soft_constraints'] == []
                response = client.post('/api/v1/human-collaboration/guidance', json=body,
                    headers={**headers, 'Idempotency-Key': 'confirm-binary-guidance'})
                assert response.status_code == 201, response.text
                material = response.json()['work_materials'][0]
                assert material['receiver'] == receiver
                assert material['references'] == []
                delivery = material['uploads'][0]
                assert delivery['work_ref'] == request.run_ref
                assert delivery['root_session_ref'] == receiver['roots'][0]['root_session_ref']
                file = delivery['file']
                location = human._root_workspaces._runtime_location(request.run_ref)
                assert (location.directory / file['path']).read_bytes() == literal
                again = client.post('/api/v1/human-collaboration/guidance', json=body,
                    headers={**headers, 'Idempotency-Key': 'confirm-binary-guidance'})
                assert again.json() == response.json()
                observed.update(material)
            return super().generate_draft(request)

    provider = Submit()
    runtime = _runtime(tmp_path / 'runtime', provider)
    provider.runtime = runtime
    try:
        provider.quest = _confirm_direct_quest(runtime)['quest_ref']
        before = {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()}
        for _ in range(6):
            runtime.idea_stage.process_once()
            if observed:
                break
        assert observed
        assert {item.version_ref for item in runtime.owners.research_memory.query_asset_inventory()} == before
    finally:
        runtime.close()
