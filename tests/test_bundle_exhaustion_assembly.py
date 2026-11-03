from copy import deepcopy

import pytest

from meta_research.bundle_exhaustion import validate_bundle_exhaustion_assessment
from meta_research.bundle_output_assembly import assemble_bundle_primary
from meta_research.bundle_target_contract import BundleTargetContractError
from meta_research.owners.common import OwnerConflict
import test_bundle_skill_adapter as adapter_suite


@pytest.fixture
def complete_exhaustion(tmp_path, monkeypatch):
    captured = []
    original_runner = adapter_suite._SequenceRunner

    class CaptureRunner(original_runner):
        def __init__(self, outputs, **kwargs):
            captured.extend(deepcopy(output) for output in outputs if "exhaustion_assessment" in output)
            super().__init__(outputs, **kwargs)

    monkeypatch.setattr(adapter_suite, "_SequenceRunner", CaptureRunner)
    adapter_suite.test_production_exhaustion_acceptance_does_not_require_child_trace(tmp_path)
    assert captured
    return adapter_suite._plan_document(), captured[0]


def test_compact_exhaustion_assembles_exact_prose_through_production_adapter(tmp_path, monkeypatch):
    original_runner = adapter_suite._SequenceRunner
    observed = []

    class CompactRunner(original_runner):
        def __init__(self, outputs, **kwargs):
            compact_outputs = deepcopy(outputs)
            for full, compact in zip(outputs, compact_outputs):
                if "exhaustion_assessment" not in compact:
                    continue
                for row in compact["exhaustion_assessment"]["completion_contract"]["experiments"]:
                    row.pop("semantic_inputs")
                    row["brief"].pop("semantic_delta")
                assert assemble_bundle_primary(compact, adapter_suite._plan_document()) == full
                observed.append(compact)
            super().__init__(compact_outputs, **kwargs)

    monkeypatch.setattr(adapter_suite, "_SequenceRunner", CompactRunner)
    adapter_suite.test_production_exhaustion_acceptance_does_not_require_child_trace(tmp_path)
    assert observed
    assert all("semantic_inputs" not in row
               for output in observed
               for row in output["exhaustion_assessment"]["completion_contract"]["experiments"])


@pytest.mark.parametrize("field", ["semantic_inputs", "semantic_delta"])
def test_exhaustion_cannot_rewrite_frozen_completion_prose(complete_exhaustion, field):
    plan, full = complete_exhaustion
    validate_bundle_exhaustion_assessment(full, plan_document=plan)
    row = full["exhaustion_assessment"]["completion_contract"]["experiments"][0]
    if field == "semantic_inputs":
        row[field]["goal"] = "Contradict the accepted Plan"
    else:
        row["brief"][field] = "Contradict the accepted Plan"
    with pytest.raises(BundleTargetContractError, match="completion_semantic_inputs_conflict"):
        assemble_bundle_primary(full, plan)


@pytest.mark.parametrize("field", ["semantic_inputs", "semantic_delta"])
def test_exhaustion_owner_validator_rejects_extra_root_prose(complete_exhaustion, field):
    plan, full = complete_exhaustion
    validate_bundle_exhaustion_assessment(full, plan_document=plan)
    full["exhaustion_assessment"][field] = "Unbound contradictory prose"
    assembled = assemble_bundle_primary(full, plan)
    with pytest.raises(OwnerConflict, match="^bundle_exhaustion_assessment_invalid$"):
        validate_bundle_exhaustion_assessment(assembled, plan_document=plan)
