"""Model view for an authorized new Bundle batch; acquisition stays with the host."""
from __future__ import annotations

from copy import deepcopy

from meta_research.owners.common import canonical_hash


_EXCLUDED_RUNTIME_KEYS = frozenset({
    "run_ref", "attempt_ref", "fence_ref", "root_session_ref",
    "runtime_binding_hash", "provider_feedback",
})
_REFERENCED_PLAN_FIELDS = frozenset({
    "schema_ref", "kind", "question_ref", "idea_set_ref", "context_pack_ref",
    "source_bindings", "idea_trace", "experiment_briefs",
})


_SEMANTIC_FIELDS = frozenset({
    "experiment_key", "goal", "characteristics", "boundary_constraints", "semantic_delta",
})


def _brief_plan_bindings(plan, completion):
    briefs = plan.get("experiment_briefs")
    experiments = completion.get("experiments")
    if not isinstance(briefs, list) or not isinstance(experiments, list):
        return None
    semantics = {}
    for experiment in experiments:
        if not isinstance(experiment, dict):
            return None
        semantic = experiment.get("semantic_inputs")
        normalized = experiment.get("brief")
        if not isinstance(semantic, dict) or not isinstance(normalized, dict):
            return None
        key = semantic.get("experiment_key")
        if (not isinstance(key, str) or key in semantics
                or normalized.get("experiment_key") != key):
            return None
        semantics[key] = semantic
    bindings = []
    seen = set()
    for brief in briefs:
        if not isinstance(brief, dict):
            return None
        key = brief.get("experiment_key")
        if (not isinstance(key, str) or key in seen
                or not _SEMANTIC_FIELDS.issubset(brief)
                or semantics.get(key) != {name: brief[name] for name in _SEMANTIC_FIELDS}):
            return None
        seen.add(key)
        bindings.append({name: deepcopy(value) for name, value in brief.items()
                         if name == "experiment_key" or name not in _SEMANTIC_FIELDS})
    return bindings if seen == set(semantics) else None


def render_batch_reading_context(request, prompt_context, *, verified_plan_reader=None):
    """Project the host-verified source without changing the sealed business input.

    The reader metadata is not an authorization grant. The host must resolve the
    source through the current authenticated reader before calling this helper.
    """
    legacy = {name: value for name, value in prompt_context.items()
              if name not in _EXCLUDED_RUNTIME_KEYS}
    if (not request.native_session_ref or request.native_replacement_context is not None
            or request.provider_feedback is not None
            or not isinstance(verified_plan_reader, dict)):
        return legacy
    reader = verified_plan_reader
    expected_arguments = {
        "context_pack_ref": request.context_pack_ref,
        "source": "context_pack",
        "path": ["accepted_formal_plan_binding", "plan_document"],
        "offset": 0,
        "limit": 16384,
    }
    expected_identity = {
        "operation": "research_memory.stage_context.read",
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "formal_plan_ref": request.formal_plan_ref,
        "plan_hash": canonical_hash(request.plan_document),
        "native_session_ref": request.native_session_ref,
    }
    if (reader.get("arguments") != expected_arguments
            or any(reader.get(key) != value for key, value in expected_identity.items())
            or prompt_context.get("formal_plan") != request.plan_document):
        return legacy
    completion = request.initial_target_plan.get("completion_contract")
    if not isinstance(completion, dict):
        return legacy
    brief_bindings = _brief_plan_bindings(request.plan_document, completion)
    if brief_bindings is None:
        return legacy
    view = deepcopy(legacy)
    del view["formal_plan"]
    view["formal_plan_reader"] = {
        "semantic_operation_id": reader["operation"],
        "arguments": deepcopy(reader["arguments"]),
        "formal_plan_ref": request.formal_plan_ref,
        "content_hash": reader["plan_hash"],
        "context_pack_hash": request.context_pack_hash,
    }
    view["plan_responsibilities"] = {
        key: deepcopy(value) for key, value in request.plan_document.items()
        if key not in _REFERENCED_PLAN_FIELDS
    }
    view["plan_responsibilities"]["brief_plan_bindings"] = brief_bindings
    view["completion_contract"] = deepcopy(completion)
    view["cycle_position"] = deepcopy(request.cycle_position)
    return view
