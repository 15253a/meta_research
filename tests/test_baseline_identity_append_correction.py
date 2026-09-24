from copy import deepcopy

from test_public_bundle_stage import (
    _RollingBundleSkill, _TwoGapPlanSkill, _bundle_runtime, _accept_real_target_root_commit,
)
from test_stage_hard_ceiling import _signed_contract_failure_evidence


class _CorrectingAppendSkill(_RollingBundleSkill):
    def propose_target_batch(self, request):
        result = super().propose_target_batch(request)
        if request.provider_feedback is None:
            result.strategy_update["candidates"][0]["measurement_contract"]["baseline_forward_contract"] = {
                "method_key": "unregistered-legacy-followup", "meaning": "unversioned method"}
        return result

    def terminal_contract_failure_checkpoint(self, **values):
        assert values["operation_name"] == "target-batch-1"
        return _signed_contract_failure_evidence(values["failure_code"], values["detail_code"])


def test_pending_append_identity_error_survives_restart_and_reaches_correcting_provider(tmp_path):
    provider = _CorrectingAppendSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider, plan_skill_provider=_TwoGapPlanSkill())
    try:
        _target, graph_ref = _accept_real_target_root_commit(runtime)
        current = runtime.bundle_stage.query_current()
        request_ref = current["stage_run_request"]["request_ref"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        original_commits = runtime.owners.research_graph.query_target_commits(graph_ref)
        for _step in range(5):
            assert runtime.bundle_stage.process_once()
            proposals = runtime.owners.agent_runtime.query_bundle_target_proposals(run.run_ref)
            if proposals:
                break
        else:
            raise AssertionError("The first append was not retained")
        original_proposal = deepcopy(proposals[0])
    finally:
        runtime.close()

    provider = _CorrectingAppendSkill()
    runtime = _bundle_runtime(tmp_path, bundle_skill_provider=provider, plan_skill_provider=_TwoGapPlanSkill())
    try:
        for _step in range(16):
            assert runtime.bundle_stage.process_once()
            graph = runtime.owners.research_graph.query_target_graph(request_ref)
            if graph.head_generation == 1:
                break
        else:
            raise AssertionError("The pending append identity rejection did not reach a correcting provider")
        assert len(provider.batch_requests) == 1
        correction = provider.batch_requests[0]
        assert correction.provider_feedback["detail_code"] == "baseline_method_identity_required"
        assert "method_key" in correction.provider_feedback["repair_hint"]
        assert correction.attempt_ref != run.attempt_ref
        assert runtime.owners.research_graph.query_target_commits(graph_ref) == original_commits
        proposals = runtime.owners.agent_runtime.query_bundle_target_proposals(run.run_ref)
        assert len(proposals) == 2 and proposals[0] == original_proposal
        assert len(graph.targets) == 2
    finally:
        runtime.close()
