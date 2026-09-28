"""Bundle can choose exact research inputs beyond Plan's evidence selection."""
from test_plan_asset_target_input import _accepted_target, _asset, _origin, _runtime
import test_public_bundle_stage as fixtures


def test_bundle_dispatch_prepares_same_quest_asset_not_selected_as_plan_evidence(tmp_path):
    runtime, plan, bundle = _runtime(tmp_path / "bundle-direct-input")
    try:
        quest = fixtures._confirm_direct_quest(runtime)
        evidence = _asset(runtime, "plan-evidence")
        _origin(runtime, evidence, quest["quest_ref"], "plan-evidence-origin")
        source = _asset(runtime, "bundle-input")
        _origin(runtime, source, quest["quest_ref"], "bundle-input-origin")
        plan.source_ref, bundle.source_ref = evidence.version_ref, source.version_ref
        target, _ = _accepted_target(runtime, quest)
        newer = _asset(runtime, "newer-bundle-input", asset_ref=source.asset_ref)
        _origin(runtime, newer, quest["quest_ref"], "newer-bundle-input-origin")
        for _ in range(16):
            runtime.bundle_stage.process_once()
            ack = runtime.owners.agent_runtime.query_target_launch_ack(target.target_ref)
            if ack is not None:
                break
        assert ack is not None, runtime.bundle_stage.query_current()
        authority = runtime.target_run_authorities.research_graph
        proof = authority.query_input_asset_projection(
            target_ref=target.target_ref, asset_ref=source.asset_ref)
        assert proof.asset == source
        assert proof.asset != newer
        assert authority.resolve_input_asset_ref(
            target_ref=target.target_ref, input_ref=source.version_ref) == source.asset_ref
        assert runtime.owners.research_memory.materialize_asset(source.version_ref).content == b"bundle-input: observed input bytes\n"
        assert authority.prepare_input_assets(target.target_ref) is False
    finally:
        runtime.close()
