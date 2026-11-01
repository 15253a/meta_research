"""Protected creation can load reader contracts without gaining research effects."""
from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_creation_material_consumption import _open
from test_root_human_request_lifecycle import _initialized_child, _tool_call, _structured
from test_root_workspace import _make


def test_protected_creation_can_load_its_readers_contract_without_research_grants(tmp_path):
    original = tmp_path / 'input.txt'
    original.write_text('Original research material.', encoding='utf-8')
    runtime = _make(tmp_path / 'runtime')
    work = None
    try:
        with TestClient(create_app(runtime, base_url='http://testserver', control_key='control')) as client:
            anchor, binding, reference = _open(client, runtime, original, 'contract-creation')
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable='/bin/bash', credentials_home=tmp_path / 'credentials')
            work = roots.protected_creation(binding,
                runtime.owners.human_collaboration.creation_material_snapshot(anchor),
                operation_ref='creation-contract')
            access = work.channel(('research_memory.content.read',))
            headers = _initialized_child(client, access.token)

            def call(name, arguments):
                return _tool_call(client, headers, operation_id=name, arguments=arguments, request_id=20)

            # The known material reader remains directly callable before discovery.
            assert call('research_workspace.materials.discover', {
                'reference_ref': reference['reference_ref'],
            })['isError'] is False
            contract = _structured(call('tools.contracts.read', {
                'operation_id': 'research_memory.content.read',
            }))
            assert 'UTF-8 byte offsets' in contract['returns']
            assert 'content_version_unbound' in contract['errors']['operation_codes']
            catalog = _structured(call('tools.catalog.page', {}))
            assert {item['operation_id'] for item in catalog['items']} == set(access.operation_ids)
            assert call('tools.contracts.read', {
                'operation_id': 'research_graph.datasets.register',
            })['structuredContent']['code'] == 'capability_unavailable'
            assert call('research_graph.datasets.register', {})['structuredContent']['code'] == 'capability_unavailable'
    finally:
        if work is not None:
            work.fail()
        runtime.close()
