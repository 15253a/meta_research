"""The host supplies directories from Owner bindings, never a manifest of their contents."""
from pathlib import Path
from types import SimpleNamespace

from meta_research.root_workspace import RootWorkspaces, WorkspaceBinding, WorkspaceLocation


def test_native_paths_follow_the_foregrounds_three_cycle_window(tmp_path):
    def work(cycle, kind):
        directory = tmp_path / (cycle + kind)
        directory.mkdir()
        (directory / 'observation').write_text(cycle)
        return WorkspaceLocation('workspace:' + directory.name, kind, 'root:' + directory.name,
            'work:' + directory.name, directory, 'quest', cycle, 'request:' + directory.name)

    cycles = {cycle: [work(cycle, kind) for kind in ('idea', 'plan')]
              for cycle in ('old', 'previous2', 'previous1', 'now', 'foreign')}
    missing = cycles['previous2'][0]
    (missing.directory / 'observation').unlink()
    missing.directory.rmdir()
    current = cycles['now'][1]
    requests = {item.request_ref: SimpleNamespace(request_ref=item.request_ref, stage=item.root_kind)
                for values in cycles.values() for item in values}
    locations = {item.work_ref: item for values in cycles.values() for item in values}
    def run(request_ref):
        return SimpleNamespace(run_ref='work:' + request_ref.split(':', 1)[1])
    ae = SimpleNamespace(query_recent_workspace_cycles=lambda quest: ('now', 'previous1', 'previous2'),
        query_cycle_stage_requests=lambda cycle: [requests[item.request_ref] for item in cycles[cycle]])
    host = RootWorkspaces(advancement_engine=ae, agent_runtime=SimpleNamespace(
        query_idea_stage_run=run, query_plan_stage_run=run), human_collaboration=None,
        research_graph=None, target_run_agent=None, bases={})
    host._runtime_location = locations.__getitem__
    entries = host.native_directories(WorkspaceBinding(current, 'fence'))
    assert {item['work_ref'] for item in entries} == {item.work_ref for cycle in ('previous1','previous2','now')
                                                      for item in cycles[cycle] if item != missing}
    assert [item['work_ref'] for item in entries if item['access'] == 'read_write'] == [current.work_ref]
    assert all((Path(item['directory']) / 'observation').read_text() == item['cycle_ref'] for item in entries)
    assert not missing.directory.exists()
