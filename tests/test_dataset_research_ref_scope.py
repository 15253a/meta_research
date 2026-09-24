"""Dataset uses may name only research actually owned by their Question's Quest."""
import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.semantic_owner_gateway import _dataset_operations
from meta_research.target_run_finalizer import TargetRunFinalizer
from test_formal_run_snapshots import _scenario
from test_research_notes_and_call_observations import _SystemEvidenceReader
from test_target_root_finalizer import _CurrentBindingBundleSkill
import test_public_bundle_stage as bundle_fixtures
from test_dataset_effect_scope_recovery import _scope, _channel, _call
from test_research_datasets import _quest, _asset, _dataset, _version


@pytest.fixture(scope="module")
def accepted_research(tmp_path_factory):
    # Produces actual result.json -> finalizer -> RM manifest -> RG Commit and
    # native Baseline/Variant/Run/Evaluation/Attempt rows; no receipt replacement.
    path = tmp_path_factory.mktemp("dataset-research-lineage")
    drafting = bundle_fixtures._DeterministicDraftingAdapter()
    runtime = bundle_fixtures.build_production_runtime(bundle_fixtures.prepare_data_root(path),
        proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=bundle_fixtures._DeterministicProbe(),
        idea_skill_provider=bundle_fixtures._DeterministicIdeaSkill(),
        plan_skill_provider=bundle_fixtures._DeterministicPlanSkill(no_gap=False),
        bundle_skill_provider=_CurrentBindingBundleSkill(),
        harness_adapters=(bundle_fixtures._FullConformanceAdapter("codex"),),
        power_inhibitor=bundle_fixtures._TogglePowerInhibitor())
    runtime, lifecycle, memory, handle, evidence, _ = _scenario(path, runtime=runtime)
    finalizer = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
        workspace_resolver=runtime.target_run_authorities.agent_runtime,
        evidence_reader=_SystemEvidenceReader(), measurement_authority=runtime.owners.research_graph,
        graph_authority=runtime.owners.research_graph)
    completed = finalizer.finalize(handle=handle, evidence=evidence)
    assert completed.status == "completed"
    runtime.owners.agent_runtime.publish_target_root_completion(target_ref=handle.target_ref,
        completion_ref=completed.completion_ref, target_commit_ref=completed.target_commit_ref)
    graph = runtime.owners.research_graph
    facts = graph.query_target_formal_results(handle.target_ref)
    evaluated = next(item for item in facts if item.get("evaluation_attempt_ref"))
    with runtime._database.read() as connection:
        own_question_ref = connection.execute(text(
            "SELECT question_ref FROM rg_question_lifecycle")).scalar_one()
        lineage = connection.execute(text(
            "SELECT b.baseline_ref,v.variant_ref,e.evaluation_ref "
            "FROM rg_evaluation_attempts a JOIN rg_evaluations e USING(evaluation_ref) "
            "JOIN rg_experiment_variants v USING(variant_ref) "
            "JOIN rg_experiment_baselines b USING(baseline_ref) "
            "WHERE a.evaluation_attempt_ref=:ref"),
            {"ref": evaluated["evaluation_attempt_ref"]}).one()
    own = graph.query_question_history_by_ref(own_question_ref)
    foreign = _quest(runtime, "foreign-research-scope")
    binding = _asset(runtime, key="scope-data")
    for label, question in (("own", own), ("foreign", foreign)):
        graph.accept_asset_role(binding=binding, role="evidence", quest_ref=question.quest_ref,
            idempotency_key=label + "-data-origin")
    dataset = _dataset(graph, "research-scope")
    source = _version(graph, dataset, binding, key="scope-source")
    derived = _version(graph, dataset, binding, label="derived", key="scope-derived")
    refs = dict(baseline=lineage.baseline_ref, variant=lineage.variant_ref,
        run=evaluated["variant_run_ref"], evaluation=lineage.evaluation_ref,
        attempt=evaluated["evaluation_attempt_ref"], target=handle.target_ref,
        commit=completed.target_commit_ref)
    yield runtime, own, foreign, source, derived, refs
    runtime.close()


def _arguments(action, question, source, derived, research_ref):
    common = dict(question_ref=question.question_ref, research_ref=research_ref)
    if action == "reference":
        return dict(common, dataset_version_ref=source["dataset_version_ref"])
    return dict(common, source_dataset_version_ref=source["dataset_version_ref"],
        derived_dataset_version_ref=derived["dataset_version_ref"], processing="Scope checked transformation")


def _counts(runtime):
    with runtime._database.read() as connection:
        return tuple(connection.execute(text("SELECT COUNT(*) FROM " + table)).scalar_one()
            for table in ("rg_dataset_references", "rg_dataset_derivations", "rg_dataset_commands"))


@pytest.mark.parametrize("action", ["reference", "derive"])
@pytest.mark.parametrize("boundary", ["owner", "gateway"])
def test_all_research_lineages_require_same_quest(accepted_research, action, boundary):
    runtime, own, foreign, source, derived, refs = accepted_research
    graph = runtime.owners.research_graph
    gateway = SemanticMcpGateway(_dataset_operations(graph, runtime.owners.agent_runtime))
    channels = {question.quest_ref: _channel(gateway, _scope(runtime,
        quest_ref=question.quest_ref, run_ref=f"{boundary}-{action}-{label}",
        root_ref=f"{boundary}-{action}-{label}-root"))
        for label, question in (("own", own), ("foreign", foreign))}
    for kind, ref in refs.items():
        arguments = _arguments(action, own, source, derived, ref)
        key = f"{boundary}-{action}-{kind}"
        if boundary == "owner":
            result = getattr(graph, action + "_dataset")(**arguments, idempotency_key=key)
            assert result["research_ref"] == ref
            assert getattr(graph, action + "_dataset")(**arguments, idempotency_key=key) == result
        else:
            result = _call(gateway, channels[own.quest_ref], action, effect_id=key, **arguments)
            assert not result.get("isError"), result
            assert result["structuredContent"]["result"]["research_ref"] == ref
            assert _call(gateway, channels[own.quest_ref], action, effect_id=key, **arguments) == result
        before = _counts(runtime)
        arguments = _arguments(action, foreign, source, derived, ref)
        if boundary == "owner":
            with pytest.raises(OwnerConflict, match="dataset_research_ref_scope_invalid"):
                getattr(graph, action + "_dataset")(**arguments, idempotency_key=key + "-foreign")
        else:
            result = _call(gateway, channels[foreign.quest_ref], action,
                effect_id=key + "-foreign", **arguments)
            assert result.get("isError"), (kind, result)
            assert "dataset_research_ref_scope_invalid" in str(result)
        assert _counts(runtime) == before


@pytest.mark.parametrize("action", ["reference", "derive"])
def test_owner_revalidates_research_lineage_inside_fenced_write(accepted_research, action, monkeypatch):
    runtime, own, foreign, source, derived, refs = accepted_research
    graph = runtime.owners.research_graph
    original_accept = graph._accept_dataset_fact
    original_verify = graph.verify_dataset_research_ref_scope
    def change_lineage_before_acceptance(*args, **kwargs):
        # Test-only corruption between public admission and writer acquisition.
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_target_graphs SET quest_ref=:foreign "
                "WHERE graph_ref=(SELECT graph_ref FROM rg_targets WHERE target_ref=:target)"),
                {"foreign": foreign.quest_ref, "target": refs["target"]})
        return original_accept(*args, **kwargs)
    def verify_inside_writer(*args, **kwargs):
        assert runtime._database._write_lock._is_owned()
        return original_verify(*args, **kwargs)
    before = _counts(runtime)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(graph, "_accept_dataset_fact", change_lineage_before_acceptance)
            patch.setattr(graph, "verify_dataset_research_ref_scope", verify_inside_writer)
            with pytest.raises(OwnerConflict, match="dataset_research_ref_scope_invalid"):
                getattr(graph, action + "_dataset")(
                    **_arguments(action, own, source, derived, refs["target"]),
                    idempotency_key=f"owner-{action}-target")
            assert _counts(runtime) == before
    finally:
        with runtime._database.write() as connection:
            connection.execute(text("UPDATE rg_target_graphs SET quest_ref=:own "
                "WHERE graph_ref=(SELECT graph_ref FROM rg_targets WHERE target_ref=:target)"),
                {"own": own.quest_ref, "target": refs["target"]})
    graph.verify_dataset_research_ref_scope(refs["target"], quest_ref=own.quest_ref)
