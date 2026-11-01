"""#266/#268 integration: custody protects originals, not unrelated scratch."""
from pathlib import Path
import os
import json
import time

from meta_research.owners.research_memory import AssetIntakeRequest
from test_completed_workspace_cleanup import _complete, _cleanup_runtime, _WorkspaceIdea
from test_root_workspace import _context, _channel, _call, _accepted


def test_linked_original_keeps_ancestors_but_releases_unrelated_workspace_bytes(tmp_path, monkeypatch):
    class RetainStageOriginal(_WorkspaceIdea):
        def generate_draft(self, request):
            draft = super().generate_draft(request)
            location = self.locations[request.context_pack['cycle_ref']]
            self.quest_ref, self.question_ref = location.quest_ref, request.question_ref
            self.original = location.directory / 'stage-original.bin'
            self.original.write_bytes(b'\xffstage-original')
            result = _accepted(_call(self.runtime, _channel(self.runtime, _context(request, 'idea')),
                'research_memory.assets.intake', effect_id='preserve-stage-original', intake={
                    'source_kind': 'local_path', 'custody_mode': 'linked_local', 'display_name': 'Stage original',
                    'source': {'kind': 'workspace', 'workspace_ref': location.workspace_ref, 'path': 'stage-original.bin'}}))
            self.accepted = result
            return draft
    idea = RetainStageOriginal()
    seeded = _cleanup_runtime(tmp_path / 'cleanup-root', idea=idea)
    runtime, handle, _, workspace, _, _, _ = _complete(tmp_path, runtime=seeded)
    try:
        original = workspace / 'retained' / 'original.bin'
        original.parent.mkdir()
        original.write_bytes(b'\x00\xfforiginal')
        sibling = original.parent / 'scratch.bin'
        sibling.write_bytes(b'x' * 8192)
        unrelated = workspace / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'scratch.bin').write_bytes(b'y' * 8192)
        result = runtime.owners.research_memory.submit_asset_intake(AssetIntakeRequest(
            source_kind='local_path', custody_mode='linked_local', display_name='original',
            source_locator=str(original)), idempotency_key='protected-original')
        human_directory = workspace / 'human-original-directory'
        human_directory.mkdir()
        (human_directory / 'unexamined.bin').write_bytes(b'\x00human original')
        human = runtime.owners.human_collaboration
        manual = human.open_manual_question_creation(quest_ref=idea.quest_ref,
            parent_question_ref=idea.question_ref, idempotency_key='cleanup-human-directory')
        human.register_work_materials(anchor_kind='manual', anchor_ref=manual['context_ref'],
            command={'receiver': human.material_receiver('manual', manual['context_ref']),
                'selections': [runtime.root_workspaces.server_files.inspect(str(human_directory))],
                'description': 'Original directory is neither scratch nor an adopted asset.'},
            idempotency_key='cleanup-original-reference')
        scan = os.scandir
        def no_original_scan(path):
            if not isinstance(path, int) and Path(path) == human_directory:
                raise AssertionError('Cleanup scanned a human original directory.')
            return scan(path)
        monkeypatch.setattr(os, 'scandir', no_original_scan)
        from sqlalchemy import event
        scans = {'custodies': 0, 'human_originals': 0, 'pending_intakes': 0}
        def count_queries(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith('SELECT source_locator FROM rm_asset_custodies '):
                scans['custodies'] += 1
            if statement.startswith('SELECT r.selection_json,r.selection_hash FROM hc_work_material_references '):
                scans['human_originals'] += 1
            if statement.startswith('SELECT request_json,request_hash FROM rm_asset_intakes '):
                scans['pending_intakes'] += 1
        event.listen(runtime._database._engine, 'before_cursor_execute', count_queries)
        try:
            report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        finally:
            event.remove(runtime._database._engine, 'before_cursor_execute', count_queries)
        observation = {'scans': scans, 'candidates': len(report),
            'released_bytes': sum(item['bytes'] for item in report if item['action'] in {'removed', 'pruned'}),
            'outcomes': [{'root_kind': item['root_kind'], 'action': item['action'], 'reason': item['reason']}
                for item in report]}
        (tmp_path / 'original-protection-observation.json').write_text(json.dumps(observation), encoding='utf-8')
        assert sum(item['action'] in {'removed', 'pruned'} for item in report) >= 2
        assert scans == {'custodies': 1, 'human_originals': 1, 'pending_intakes': 1}
        print('ORIGINAL_PROTECTION_BATCH ' + json.dumps(observation))
        row = next(item for item in report if item['path'] == str(workspace))
        assert original.read_bytes() == b'\x00\xfforiginal'
        assert not sibling.exists()
        assert not unrelated.exists()
        assert row['action'] == 'pruned'
        assert row['bytes'] >= 16384
        assert idea.original.read_bytes() == b'\xffstage-original'
        assert (human_directory / 'unexamined.bin').read_bytes() == b'\x00human original'
        assert not (idea.original.parent / 'working-note.txt').exists()
        assert next(item for item in report if item['path'] == str(idea.original.parent))['action'] == 'pruned'
        assert runtime.owners.research_memory.materialize_asset(result.asset.version_ref).content == original.read_bytes()
        memory = runtime.owners.research_memory
        memory.retire_asset_version(result.asset.version_ref,
            expected_revision=memory.query_asset_lifecycle(result.asset.asset_ref)['revision'],
            expected_reference_revision=runtime.owners.research_graph.query_asset_reference_revision(),
            explanation='This isolated test artifact has no scientific use; retirement is not physical deletion.',
            low_value=True, obsolete=True, incorrect=True, impact_understood=True,
            has_explanation_value=False, idempotency_key='retire-protected-original')
        # A second pass may remove nothing, but must never remove the original.
        runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        assert original.read_bytes() == b'\x00\xfforiginal'
        # Construct the durable pending submission left by a failed human upload
        # acceptance (the public failure/retry path is covered separately).
        location = runtime.root_workspaces._target_locations(handle.target_ref)[0]
        pending = workspace / 'pending-upload.bin'
        pending.write_bytes(b'pending human upload')
        command = {'receiver': {'kind': 'current', 'quest_ref': location.quest_ref,
            'roots': [location.source()]}, 'selections': [], 'description': 'Unsettled upload',
            'uploads': [{'relative_path': 'pending-upload.bin', 'media_type': 'application/octet-stream',
                'content_base64': 'cGVuZGluZyBodW1hbiB1cGxvYWQ='}]}
        with runtime._database.fenced_write() as connection:
            human._insert_material_submission(connection, command=command, key='pending-protection',
                anchor_kind='guidance', anchor_ref='pending:protection', state='pending', validate_receiver=False)
        report = runtime.target_run_runtime.cleanup_completed_workspaces(dry_run=False, now=time.time()+90000)
        row = next(item for item in report if item['path'] == str(workspace))
        assert (row['action'], row['reason']) == ('skipped', 'pending_human_upload')
        assert pending.read_bytes() == b'pending human upload'
    finally:
        runtime.close()
