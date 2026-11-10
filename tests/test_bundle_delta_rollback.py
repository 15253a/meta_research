from dataclasses import replace
import json

import pytest

from meta_research.idea_skill import IdeaSkillUnavailable
from test_bundle_batch_access import _bind
from test_bundle_input_drift_recovery import _adapter, _output


@pytest.mark.parametrize("start,end", [("delta-v2", "full"), ("full", "delta-v2"),
                                      ("plan-reference-v1", "delta-v2"), ("delta-v2", "plan-reference-v1")])
@pytest.mark.parametrize("unknown", [False, True])
def test_retired_rollout_values_cannot_change_delta_or_sealed_operation(tmp_path, monkeypatch, start, end, unknown):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request, reader, *_ = _bind(adapter)
    monkeypatch.setenv("META_RESEARCH_BUNDLE_BATCH_VIEW", start)
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    if unknown:
        for name in ("exit.json", "completed.json", "supervisor-exit.json"):
            (operation / name).unlink()
    frozen = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    monkeypatch.setenv("META_RESEARCH_BUNDLE_BATCH_VIEW", end)
    adapter.bind_batch_plan_reader(lambda _: pytest.fail("sealed operation consulted current reader"))
    adapter.bind_batch_source_readers(lambda _: pytest.fail("sealed operation consulted current sources"))
    if unknown:
        with pytest.raises(IdeaSkillUnavailable, match="codex_operation_reconciliation_pending"):
            adapter.propose_target_batch(request)
    else:
        adapter.propose_target_batch(request)
    assert len(runner.calls) == 1
    assert {name: (operation / name).read_bytes() for name in frozen} == frozen
    if unknown:
        return
    adapter.bind_batch_plan_reader(reader)
    adapter.bind_batch_source_readers(None)
    output = _output("batch")
    output["strategy_update"]["revision"] = 3
    runner._outputs = iter([output])
    adapter.propose_target_batch(replace(request, base_generation=1))
    view = json.loads(next(line.partition("=")[2] for line in runner.calls[-1][1].splitlines()
                           if line.startswith("batch_context=")))
    assert "formal_plan" not in view
    assert "responsibility_index" in view
    assert len(runner.calls) == 2
    assert {name: (operation / name).read_bytes() for name in frozen} == frozen
