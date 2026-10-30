"""The delivered view distinguishes exact science from optional reading previews."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from meta_research.context_presentation import stage_context_json, stage_context_view
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.target_run_runtime import TargetRunRuntime
from test_bundle_input_drift_recovery import _adapter, _output, _request
from test_target_run_owner import _records


@pytest.mark.parametrize("stage,science", [
    ("idea", "prior_accepted_bindings"), ("bundle", "accepted_formal_plan_binding"),
    ("reasoning", "upstream_stage_closure"),
])
def test_view_marks_complete_science_and_only_actual_optional_previews(stage, science):
    body = {"notes": "完整的当前科学正文。" * 12000}
    pack = {science: body, "small_optional": {"facts": ["known fact"]},
            "large_optional": {"notes": "optional history " * 1000}}
    view = stage_context_view(stage, pack, context_pack_ref="context", context_pack_hash=canonical_hash(pack))
    assert "summary_only" not in view
    assert view["section_delivery"] == {science: "complete", "small_optional": "complete",
                                        "large_optional": "preview"}
    assert view["sections"][science] == body
    assert stage_context_json(stage, pack, context_pack_ref="context", context_pack_hash=canonical_hash(pack)) == canonical_json(view)


def test_omitted_optional_section_does_not_relabel_complete_contracts(monkeypatch):
    import meta_research.context_presentation as presentation
    monkeypatch.setattr(presentation, "CONTEXT_VIEW_MAX_BYTES", 6000)
    plan = {"notes": "Full scientific controls. " * 1000}
    contract = {"protocol": "Complete frozen protocol. " * 1000}
    pack = {"accepted_formal_plan_binding": plan, "execution_contract": contract,
            "optional_catalog": {str(index): "Optional detail. " * 100 for index in range(100)}}
    view = stage_context_view("bundle", pack, context_pack_ref="context", context_pack_hash=canonical_hash(pack))
    assert view["section_delivery"] == {"accepted_formal_plan_binding": "complete", "optional_catalog": "omitted"}
    assert view["sections"]["accepted_formal_plan_binding"] == plan
    assert view["required_material"]["execution_contract"] == contract


def test_plan_binding_marks_idea_body_supplied_elsewhere_as_a_partial_view():
    binding = {"idea_set_ref": "ideas:1", "idea_set": {"notes": "Full scientific IdeaSet."},
               "receipt": {"receipt_ref": "receipt:1", "payload_hash": "a" * 64, "proof": "proof"}}
    view = stage_context_view("plan", {"accepted_idea_set_binding": binding},
        context_pack_ref="context", context_pack_hash=canonical_hash(binding))
    assert view["section_delivery"]["accepted_idea_set_binding"] == "preview"
    assert view["sections"]["accepted_idea_set_binding"]["idea_set"]["provided_as"] == "accepted_idea_set"


@pytest.mark.parametrize("kind", ["dispatch", "batch"])
def test_rolling_provider_refuses_cycle_position_from_another_cycle(tmp_path, kind):
    from meta_research.bundle_skill import BundleSkillUnavailable
    adapter, runner = _adapter(tmp_path, _output(kind))
    request = _request(adapter, kind)
    request = replace(request, cycle_position={**request.cycle_position, "cycle_ref": "cycle:foreign"})
    with pytest.raises(BundleSkillUnavailable, match="bundle_cycle_position_invalid"):
        (adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch)(request)
    assert runner.calls == []


@pytest.mark.parametrize("mismatch", ["position", "graph", "request", "target", "missing"])
def test_target_prompt_binds_position_to_accepted_launch_graph(mismatch):
    candidate, plan, handle, _, request = _records()
    context = {"cycle_ref": "cycle:target", "graph_ref": "graph:target",
        "stage_request_ref": "request:target", "target_ref": handle.target_ref,
        "cycle_position": {"cycle_ref": "cycle:target", "round": 1, "cycle_focus": "Measure exact scope",
                           "focus_source": {"request_ref": "request:target"}}}
    if mismatch == "position":
        context["cycle_position"]["cycle_ref"] = "cycle:foreign"
    elif mismatch == "missing":
        context.pop("cycle_position")
    else:
        context[{"graph": "graph_ref", "request": "stage_request_ref", "target": "target_ref"}[mismatch]] = "foreign"
    with pytest.raises(OwnerConflict, match="target_cycle_position_invalid"):
        TargetRunRuntime._root_prompt(execution_contract={}, handle=handle, candidate=candidate,
            formal_plan=plan, launch=SimpleNamespace(graph_ref="graph:target", stage_request_ref="request:target", request=request),
            frozen_input_manifest_path=None, research_context=context)
