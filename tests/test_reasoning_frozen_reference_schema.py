"""Provider generation bounds mirror the exact frozen reference contract."""
from copy import deepcopy
from dataclasses import replace

import pytest
from jsonschema import Draft202012Validator

from meta_research.idea_skill import _compile_codex_output_schema
from meta_research.owners.common import canonical_hash
from meta_research.reasoning_skill import _scientific_outcome_schema, _validate_request
from test_reasoning_skill_adapter import _request


FIELDS = (
    "target_commit_refs", "changed_axis_fact_refs", "held_fixed_fact_refs",
    "provenance_refs", "prior_accepted_outcome_refs",
)


def _frozen_reference_schema(field, refs):
    request = _request()
    context = deepcopy(request.context_pack)
    research = context["research_context"]
    if field == "prior_accepted_outcome_refs":
        research["graph_binding"]["prior_current_question_outcomes"] = [
            {"cycle_ref": f"cycle:prior:{index}", "request_ref": f"request:prior:{index}",
             "outcome_ref": ref, "disposition": "uncertain",
             "outcome_receipt_ref": f"receipt:prior:{index}"}
            for index, ref in enumerate(refs)
        ]
    else:
        research["causal_context"][field] = refs
    request = replace(request, context_pack=context, context_pack_hash=canonical_hash(context))
    _validate_request(request)
    schema = _compile_codex_output_schema(_scientific_outcome_schema(request))
    if field == "prior_accepted_outcome_refs":
        return schema["properties"]["research_synthesis"]["properties"]["current_question"]["properties"][field]
    return schema["properties"]["causal_interpretation"]["properties"][field]


@pytest.mark.parametrize("field", FIELDS)
def test_compiled_frozen_ref_schema_rejects_repeated_suffix_inside_one_string(field):
    # Minimized form of the observed public output: an exact reference prefix
    # followed by the same 32-character suffix repeatedly within one value.
    suffix = "5f64079e58344f158389e44311c6b599"
    prefix = "scientific_outcome_" if field == "prior_accepted_outcome_refs" else "target_commit_"
    schema = _frozen_reference_schema(field, [prefix + suffix])
    validator = Draft202012Validator(schema)
    assert validator.is_valid([prefix + suffix])
    assert not validator.is_valid([prefix + suffix * 3])


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("length", [0, 1, 38_000])
def test_compiled_frozen_ref_bound_preserves_empty_and_long_legal_sources(field, length):
    refs = [] if length == 0 else ["r" * length]
    schema = _frozen_reference_schema(field, refs)
    Draft202012Validator.check_schema(schema)
    assert Draft202012Validator(schema).is_valid(refs)
    assert schema["minItems"] == schema["maxItems"] == len(refs)
    assert schema["items"]["maxLength"] == max(1, length)
