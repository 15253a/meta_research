"""A skipped Bundle points at the accepted Plan, including unscheduled gaps."""
from dataclasses import replace

from meta_research.research_overview import ResearchOverviewReader
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request, _UnscheduledGapPlanSkill


def test_skipped_bundle_retains_exact_plan_and_does_not_close_scientific_gaps(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path, plan_skill_provider=_UnscheduledGapPlanSkill(no_gap=False))
    try:
        _prepare_bundle_request(runtime)
        for _ in range(4):
            current = runtime.bundle_stage.query_current()
            if current['stage_commit'] is not None:
                break
            assert runtime.bundle_stage.process_once()
        request = current['stage_run_request']
        quest_ref = request['accepted_question_binding']['quest_ref']
        owners = runtime.owners
        reader = ResearchOverviewReader(owners.research_graph, owners.advancement_engine,
                                        owners.research_memory, owners.agent_runtime)
        view = reader.query(quest_ref)
        stages = view['cycles'][0]['stages']
        skipped = stages['bundle'][0]
        assert skipped['status'] == 'skipped'
        assert skipped['content'] is None
        plan = next(item for cycle in view['cycles'] for item in cycle['stages']['plan']
                    if item['source']['outcome_ref'] == skipped['source']['formal_plan_ref'])
        assert plan['status'] == 'accepted'
        assert plan['content']['gap_set'] == ['rare-morphology-comparison']
        assert plan['content']['experiment_briefs'] == []
        assert plan['content']['coverage'][0]['insufficiency'].startswith('The evidence remains insufficient.')
        assert view['findings']['quest'] == []
        history = owners.advancement_engine.query_quest_stage_history_display(quest_ref)
        cycle = history[0]
        broken_commits = tuple(replace(item, outcome_ref='foreign-plan') if item.stage == 'bundle' else item
                               for item in cycle.commits)
        monkeypatch.setattr(owners.advancement_engine, 'query_quest_stage_history_display',
                            lambda _: (replace(cycle, commits=broken_commits),))
        broken = reader.query(quest_ref)['cycles'][0]['stages']['bundle'][0]
        assert broken['status'] == 'unavailable'
        assert 'formal_plan_ref' not in broken['source']
    finally:
        runtime.close()
