"""Actual Plan/Bundle grants preserve Owner failures and accepted work."""
import json

from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticCallContext
from test_bundle_exhaustion_owner import _ExhaustionBundleSkill, _advance_to_evidence
from test_public_bundle_stage import (
    _bundle_runtime, _FixtureTargetCommitEvidenceAuthority, _install_fixture_plan_evidence,
)
from test_public_plan_stage import _confirm_direct_quest, _finish_idea_stage
from test_root_workspace import _bundle_context, _channel, _call
from test_semantic_bundle_target_catalog import _implementation_content_arguments


def _accepted(result):
    assert not result['isError'], result
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    return result['structuredContent']


def _failure(result, code):
    assert result['isError'], result
    failure = result['structuredContent']
    assert failure['code'] == code, failure
    assert json.loads(result['content'][0]['text']) == failure
    assert failure['details']['message']
    assert failure['details']['correction']
    return failure['details']


def test_bundle_implementation_conflict_reconciles_original_content_and_continues(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / 'runtime', bundle_skill_provider=_ExhaustionBundleSkill())
    try:
        _advance_to_evidence(runtime)
        channel = _channel(runtime, _bundle_context(runtime))
        operation = 'research_memory.implementation_content.accept'
        submitted = _implementation_content_arguments('original-implementation')
        accepted = _accepted(_call(runtime, channel, operation, **submitted))
        changed = {**submitted, 'patch_ref': 'patch:changed-after-acceptance'}
        details = _failure(_call(runtime, channel, operation, **changed), 'implementation_content_conflict')
        assert 'original' in details['correction']
        conflict = _failure(_call(runtime, channel, operation + '.reconcile', **changed),
                            'implementation_content_reconciliation_conflict')
        assert 'patch_ref' in conflict['fields']
        assert _accepted(_call(runtime, channel, operation + '.reconcile', **submitted)) == accepted
        ref = submitted['implementation_revision_ref']
        original_read = _accepted(_call(runtime, channel, 'research_memory.implementation_content.read',
                                       implementation_revision_ref=ref))
        assert original_read['accepted'] == accepted['accepted']
        memory = runtime.owners.research_memory
        query = memory.query_implementation_content

        def unavailable(*args, **kwargs):
            raise OwnerConflict('implementation_store_unavailable', {
                'message': 'Implementation store is unavailable.',
                'correction': 'Restore the store; keep the original revision and submission.',
                'implementation_revision_ref': ref,
            })

        monkeypatch.setattr(memory, 'query_implementation_content', unavailable)
        for op, args in ((operation + '.reconcile', submitted),
                         ('research_memory.implementation_content.read', {'implementation_revision_ref': ref})):
            details = _failure(_call(runtime, channel, op, **args), 'implementation_store_unavailable')
            assert details['implementation_revision_ref'] == ref
        monkeypatch.setattr(memory, 'query_implementation_content', query)
        assert _accepted(_call(runtime, channel, operation + '.reconcile', **submitted)) == accepted
        assert _accepted(_call(runtime, channel, 'research_memory.implementation_content.read',
                               implementation_revision_ref=ref)) == original_read
    finally:
        runtime.close()


def test_plan_evidence_owner_failures_keep_exact_binding_and_same_grant_continues(tmp_path, monkeypatch):
    authority = _FixtureTargetCommitEvidenceAuthority()
    runtime = _bundle_runtime(tmp_path / 'runtime', target_commit_evidence_authority=authority)
    try:
        quest = _confirm_direct_quest(runtime)
        _install_fixture_plan_evidence(runtime, authority, quest_ref=quest['quest_ref'])
        _finish_idea_stage(runtime)
        run = None
        for _ in range(4):
            assert runtime.plan_stage.process_once()
            request = runtime.plan_stage.query_current()['stage_run_request']
            if request is not None:
                run = runtime.owners.agent_runtime.query_plan_stage_run(request['request_ref'])
                if run is not None:
                    break
        assert run is not None
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref,
                                      run.runtime_binding_hash, 'plan', 'primary', '')
        channel = _channel(runtime, context)
        page_op = 'research_graph.plan_evidence.page'
        page_args = {'offset': 0, 'limit': 16}
        page = _accepted(_call(runtime, channel, page_op, **page_args))
        binding = page['evidence_catalog'][0]
        read_args = {'target_commit_ref': binding['target_commit_root_ref'],
                     'asset_version_ref': binding['asset_version_ref'], 'content_hash': binding['content_hash'],
                     'role_ref': binding['role_ref'], 'offset': 0, 'limit': 32768}
        read_op = 'research_memory.plan_evidence.read'
        content = _accepted(_call(runtime, channel, read_op, **read_args))
        assert content['text'] == '{"accepted":true}\n'
        # Only unavailable Owner I/O is injected; recovery uses real retained
        # evidence, its immutable version/hash, and the original Plan grant.
        for owner, method, op, args in (
            (runtime.owners.research_graph, 'query_plan_evidence_page', page_op, page_args),
            (runtime.owners.research_graph, 'query_plan_evidence_catalog', read_op, read_args),
            (runtime.owners.research_memory, 'materialize_asset', read_op, read_args),
        ):
            original = getattr(owner, method)

            def unavailable(*args, **kwargs):
                raise OwnerConflict('plan_evidence_store_unavailable', {
                    'message': 'Evidence storage is temporarily unavailable.',
                    'correction': 'Restore the source; keep the selected version, hash and role.',
                    'asset_version_ref': binding['asset_version_ref'],
                })

            monkeypatch.setattr(owner, method, unavailable)
            details = _failure(_call(runtime, channel, op, **args), 'plan_evidence_store_unavailable')
            assert details['asset_version_ref'] == binding['asset_version_ref']
            monkeypatch.setattr(owner, method, original)
            assert _accepted(_call(runtime, channel, op, **args)) == (page if op == page_op else content)
    finally:
        runtime.close()
