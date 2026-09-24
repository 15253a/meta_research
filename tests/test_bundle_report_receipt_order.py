"""Bundle reports preserve receipt identity across differently ordered Targets."""
from dataclasses import replace

import pytest

from meta_research.bundle_protocol import TargetWorkHandle
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.agent_runtime_harness import TargetRootCompletionEvidence
from meta_research.owners.target_run_runtime import canonical_target_scope_binding
from meta_research.target_run_finalizer import SQLiteTargetRootCompletionMemoryAuthority, TargetRunFinalizer
from meta_research.target_run_runtime_contract import decode_target_completion_handoff
from test_public_bundle_stage import _bundle_runtime, _TwoTargetBundleSkill, _TogglePowerInhibitor
from test_target_launch_admission import _ready_launch
from test_target_root_finalizer import _CurrentBindingBundleSkill, _EvidenceReader


class _TwoCurrentTargets(_CurrentBindingBundleSkill):
    _target_plan = _TwoTargetBundleSkill._target_plan

    def _second_dependencies(self):
        return ()


def _finish_native_target(runtime, graph, target, dispatch):
    """Use real admission, lifecycle, immutable memory and RG acceptance."""
    owner = runtime.owners
    key = target.target_ref
    source = owner.research_graph.accept_formal_plan_content(
        formal_plan_ref=graph.formal_plan_ref, idempotency_key='report-source')
    projection = owner.research_graph.accept_target_formal_plan_projection(
        graph_ref=graph.graph_ref, idempotency_key='report-plan-projection')
    candidate_projection = owner.research_graph.accept_target_candidate_projection(
        target_ref=key, idempotency_key=f'candidate:{key}')
    launch_request = owner.research_graph.query_target_launch_request(key)
    owner.agent_runtime.admit_target_launch(launch_request,
        dispatch_decision_ref=dispatch.decision_ref, idempotency_key=f'launch:{key}')
    launch = owner.agent_runtime.query_admitted_target_launch(key)
    assert launch is not None
    candidate, formal_plan = candidate_projection.candidate, projection.formal_plan
    scope = canonical_target_scope_binding(target_ref=key, target_run_ref=launch.target_run_ref,
        target_spec_hash=launch_request.target_spec_binding.content_hash_ref,
        candidate=candidate, formal_plan=formal_plan, accepted_input_refs=())
    admission = runtime.harnesses.admit_target_run(target_ref=key, target_run_ref=launch.target_run_ref,
        harness_family='codex', model_ref='gpt-target-run', auth_profile_ref='harness-profile:codex-default',
        target_scope_binding=scope)
    binding = runtime.target_run_authorities.research_graph.accept_execution_input_binding(
        target_ref=key, target_run_ref=admission.run.run_ref,
        target_attempt_ref=admission.run.attempt_ref, target_fence_ref=admission.run.fence_ref,
        target_spec_hash=launch_request.target_spec_binding.content_hash_ref,
        target_scope_binding_hash=canonical_hash(scope), input_refs=(), idempotency_key=f'inputs:{key}')
    handle = TargetWorkHandle(target_ref=key, target_run_ref=admission.run.run_ref,
        root_session_ref=admission.run.root_session_ref, execution_attempt_ref=admission.run.attempt_ref,
        execution_fence_ref=admission.run.fence_ref, execution_input_binding_ref=binding.proof.binding_ref,
        execution_input_binding_receipt=binding.proof.acceptance_receipt,
        accepted_input_target_commit_refs=(), accepted_input_asset_proofs=(), recoverable=True)
    runtime.target_run_authorities.agent_runtime.reserve_target_workspace(
        handle=handle, idempotency_key=f'workspace:{key}')
    lifecycle = runtime.target_root_lifecycle
    lifecycle.activate(launch_ref=launch.launch_ref, handle=handle, candidate=candidate,
        formal_plan=formal_plan, idempotency_key=f'activate:{key}')
    memory = SQLiteTargetRootCompletionMemoryAuthority(runtime._database, runtime.feed,
        owner.research_memory, lifecycle)
    authority = owner.research_graph.query_target_measurement_domain_authority(key)
    assert authority is not None
    _, workspace = runtime.target_run_authorities.agent_runtime.resolve_target_workspace(
        target_ref=key, target_run_ref=handle.target_run_ref, root_session_ref=handle.root_session_ref,
        attempt_ref=handle.execution_attempt_ref, fence_ref=handle.execution_fence_ref)
    (workspace / 'implementation/train.py').write_text("print('fixture')\n")
    (workspace / 'outputs').mkdir()
    (workspace / 'logs').mkdir()
    result = {'metrics': {metric: float(i + 1) for i, metric in enumerate(
        authority.measurement_contract.protocol_version.required_metric_keys)},
        'result_disposition': 'positive', 'schema_ref': authority.measurement_contract.result_schema_ref}
    (workspace / 'outputs/metrics.json').write_text(canonical_json(result))
    (workspace / 'logs/train.log').write_text('fixture execution complete\n')
    artifacts = [{'role': 'implementation', 'relative_path': 'implementation'},
        {'role': 'result', 'relative_path': 'outputs/metrics.json'},
        {'role': 'log', 'relative_path': 'logs/train.log'}]
    if authority.measurement_contract.checkpoint_policy == 'required':
        (workspace / 'outputs/final.ckpt').write_bytes(b'fixture-checkpoint')
        artifacts.append({'role': 'checkpoint', 'relative_path': 'outputs/final.ckpt'})
    handoff = decode_target_completion_handoff(canonical_json({'artifacts': artifacts,
        'result_document_path': 'outputs/metrics.json', 'schema_ref': 'meta-research/target-completion-handoff/v1',
        'status': 'completed', 'summary': 'Fixture native root completed.',
        'target_ref': key, 'target_run_ref': handle.target_run_ref}))
    evidence = TargetRootCompletionEvidence(target_ref=key, target_run_ref=handle.target_run_ref,
        attempt_ref=handle.execution_attempt_ref, attempt_generation=admission.run.attempt_generation,
        root_session_ref=handle.root_session_ref, native_session_ref=f'native:{key}',
        fence_ref=handle.execution_fence_ref, operation_ref=f'operation:{key}', operation_generation=1,
        evidence_ref=f'evidence:{key}', evidence_sequence=10, handoff=handoff, observed_at=1.0)
    completed = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime, evidence_reader=_EvidenceReader(evidence),
        measurement_authority=owner.research_graph, graph_authority=owner.research_graph).finalize(
            handle=handle, evidence=evidence)
    assert completed.status == 'completed'
    published = owner.agent_runtime.publish_target_root_completion(target_ref=key,
        completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
    assert published.terminal is not None
    lifecycle.mark_completed(target_ref=key, completion_ref=completed.completion_ref)
    return published.terminal, source, projection


@pytest.fixture
def committed_targets(tmp_path):
    runtime = _bundle_runtime(tmp_path / 'receipt-order', bundle_skill_provider=_TwoCurrentTargets(),
        power_inhibitor=_TogglePowerInhibitor())
    try:
        graph, target, run, dispatch, _launch = _ready_launch(runtime)
        closures = []
        closure, source, projection = _finish_native_target(runtime, graph, target, dispatch)
        closures.append(closure)
        for _step in range(16):
            runtime.bundle_stage.process_once()
            decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
            dispatch = decisions[-1]
            if dispatch.selected_target_ref and dispatch.selected_target_ref != target.target_ref:
                second = next(value for value in graph.targets if value.target_ref == dispatch.selected_target_ref)
                if runtime.owners.research_graph.query_target_candidate_projection(target_ref=second.target_ref) is not None:
                    break
        else:
            raise AssertionError('Second native Target was not dispatched')
        closure, source, projection = _finish_native_target(runtime, graph, second, dispatch)
        closures.append(closure)
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph is not None and len(closures) == 2
        canonical = tuple(sorted(closures, key=lambda value: value.target_commit_ref))
        # Deliberately exercise the differing order independent of random UUIDs.
        yield runtime, graph, run, tuple(reversed(canonical)), source, projection
    finally:
        runtime.close()


def test_report_receipts_resolve_by_identity_and_owner_returns_verified_tuple(committed_targets):
    runtime, graph, _run, closures, _source, _projection = committed_targets
    owner = runtime.owners.research_graph
    expected = tuple(commit.receipt for commit in sorted(owner.query_target_commits(graph.graph_ref),
        key=lambda value: value.commit_ref))
    assert closures[0].target_commit_ref != expected[0].subject_ref
    receipts = owner.verify_bundle_report_target_commits(graph_ref=graph.graph_ref,
        closures=closures, receipts=None, head_receipt=graph.head_receipt)
    assert type(receipts) is tuple and receipts == expected
    assert all(type(receipt) is AcceptanceReceipt for receipt in receipts)
    # AE revalidates AR's canonical receipts with independently ordered closures.
    assert owner.verify_bundle_report_target_commits(graph_ref=graph.graph_ref,
        closures=closures, receipts=receipts, head_receipt=graph.head_receipt) == expected


def test_explicit_receipts_and_closure_boundaries_still_fail_closed(committed_targets):
    runtime, graph, _run, closures, _source, _projection = committed_targets
    owner = runtime.owners.research_graph
    canonical = tuple(sorted(closures, key=lambda value: value.target_commit_ref))
    receipts = tuple(commit.receipt for commit in sorted(owner.query_target_commits(graph.graph_ref),
        key=lambda value: value.commit_ref))
    cases = [
        (closures, tuple(reversed(receipts))),
        (closures, (replace(receipts[0], payload_hash='0' * 64), receipts[1])),
        ((replace(canonical[0], target_ref=canonical[1].target_ref), canonical[1]), receipts),
        ((canonical[0],), (receipts[0],)),
        ((canonical[0], canonical[0]), receipts),
    ]
    for supplied_closures, supplied_receipts in cases:
        with pytest.raises(OwnerConflict, match='bundle_report_target_commit_invalid'):
            owner.verify_bundle_report_target_commits(graph_ref=graph.graph_ref,
                closures=supplied_closures, receipts=supplied_receipts, head_receipt=graph.head_receipt)


def test_multi_target_report_reaches_agent_runtime_and_advancement_acceptance(committed_targets):
    runtime, graph, run, _closures, source, projection = committed_targets
    # The real Bundle provider first seals the now-covered strategy.
    for _step in range(8):
        if graph.strategy_complete:
            break
        runtime.bundle_stage.process_once()
        graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
        assert graph is not None
    assert graph.strategy_complete
    owner = runtime.owners.agent_runtime
    values = dict(run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        formal_plan_content_receipt=source.receipt, formal_plan_projection_receipt=projection.receipt,
        target_graph_ref=graph.graph_ref, target_graph_receipt=graph.head_receipt)
    candidate = owner.build_bundle_report_candidate(disposition='realized', **values)
    accepted = owner.accept_bundle_report(report=candidate, idempotency_key='accept-report-order', **values)
    assert accepted.report.disposition == 'realized'
    assert len(accepted.target_commit_receipts) == 2
    completion = owner.complete_bundle_run(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
        fence_ref=run.fence_ref, report_ref=accepted.report_ref, decision_receipt=accepted.receipt,
        idempotency_key='complete-report-order')
    runtime.owners.advancement_engine.commit_bundle_stage(request_ref=run.request_ref,
        run_ref=run.run_ref, bundle_report_ref=accepted.report_ref,
        run_completion_receipt=completion.receipt, bundle_report_receipt=accepted.receipt,
        idempotency_key='advance-report-order')
    commit = runtime.owners.advancement_engine.query_bundle_stage_commit(run.request_ref)
    assert commit is not None
