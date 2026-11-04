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


_READ_OP = "research_memory.stage_context.read"
_LIMIT = 16384

def _legacy_view(prompt_context):
    return {name: value for name, value in prompt_context.items()
            if name not in _EXCLUDED_RUNTIME_KEYS}

def _plan_verified(request, prompt_context, verified_plan_reader):
    if not isinstance(verified_plan_reader, dict):
        return False
    reader = verified_plan_reader
    expected_arguments = {
        "context_pack_ref": request.context_pack_ref,
        "source": "context_pack",
        "path": ["accepted_formal_plan_binding", "plan_document"],
        "offset": 0,
        "limit": _LIMIT,
    }
    expected_identity = {
        "operation": _READ_OP,
        "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash,
        "formal_plan_ref": request.formal_plan_ref,
        "plan_hash": canonical_hash(request.plan_document),
        "native_session_ref": request.native_session_ref,
    }
    return (reader.get("arguments") == expected_arguments
            and all(reader.get(key) == value for key, value in expected_identity.items())
            and prompt_context.get("formal_plan") == request.plan_document)


def _exact_source(reader, request, source, source_ref, document, *, path=(), selected=None):
    expected = {"context_pack_ref": request.context_pack_ref, "source": source,
        "source_ref": source_ref, "version_ref": canonical_hash(document),
        "path": list(path), "offset": 0, "limit": _LIMIT}
    return (isinstance(reader, dict) and reader.get("semantic_operation_id") == _READ_OP
            and reader.get("arguments") == expected
            and reader.get("content_hash") == canonical_hash(document if selected is None else selected))


def partition_batch_records(records, baseline_records, readers, *, key):
    if key not in ("target_ref", "commit_ref"):
        raise ValueError("key must be 'target_ref' or 'commit_ref'")
    readers = readers or {}
    base = [] if baseline_records is None else list(baseline_records)
    base_counts, base_hashes = {}, {}
    for rec in base:
        base_counts[rec.get(key)] = base_counts.get(rec.get(key), 0) + 1
        base_hashes.setdefault(rec.get(key), set()).add(canonical_hash(rec))
    cur_counts = {}
    for rec in records:
        cur_counts[rec.get(key)] = cur_counts.get(rec.get(key), 0) + 1
    bodies, index, retained = [], [], []
    for rec in records:
        ref, digest = rec.get(key), canonical_hash(rec)
        reader = readers.get(ref)
        matched = reader if reader is not None and reader.get("content_hash") == digest else None
        entry = {key: ref, "content_hash": digest}
        for field in ("status", "state"):
            if field in rec: entry[field] = deepcopy(rec[field])
        entry["reader"] = deepcopy(matched)
        index.append(entry)
        old = digest in base_hashes.get(ref, ())
        reason = None
        if old:
            if cur_counts[ref] > 1: reason = "duplicate_current_ref"
            elif base_counts.get(ref, 0) > 1: reason = "duplicate_baseline_ref"
            elif reader is None: reason = "missing_reader"
            elif matched is None: reason = "reader_content_hash_mismatch"
        if reason is not None:
            retained.append({key: ref, "content_hash": digest, "reason": reason})
        if not old or reason is not None:
            bodies.append(deepcopy(rec))
    return bodies, index, retained


def render_delta_batch_reading_context(request, prompt_context, *, verified_plan_reader=None,
                                       verified_source_readers=None, verified_delivery_baseline=None):
    view = deepcopy(_legacy_view(prompt_context))
    plan_verified = _plan_verified(request, prompt_context, verified_plan_reader)
    if (not request.native_session_ref or request.native_replacement_context is not None
            or request.provider_feedback is not None
            or (not plan_verified and not verified_source_readers)):
        return view
    completion = request.initial_target_plan.get("completion_contract")
    if not isinstance(completion, dict) or _brief_plan_bindings(request.plan_document, completion) is None:
        return view
    view.pop("cycle_position", None)
    readers = verified_source_readers or {}
    completion_reader = readers.get("completion_contract")
    retained = []
    if plan_verified:
        del view["formal_plan"]
        view["formal_plan_reader"] = {
            "semantic_operation_id": _READ_OP, "arguments": deepcopy(verified_plan_reader["arguments"]),
            "content_hash": verified_plan_reader["plan_hash"], "formal_plan_ref": request.formal_plan_ref,
            "context_pack_hash": request.context_pack_hash}
    else:
        retained.append({"field": "formal_plan", "reason": "exact_reader_unavailable"})
    if _exact_source(completion_reader, request, "bundle_initial_target_plan", request.graph_ref,
                     request.initial_target_plan, path=("completion_contract",), selected=completion):
        view["completion_contract_reader"] = deepcopy(completion_reader)
    else:
        view["completion_contract_reconstruction"] = {
            "schema_ref": completion["schema_ref"], "plan_document_hash": completion["plan_document_hash"],
            "reason": "exact_reader_unavailable",
            "instructions": "For each responsibility_index experiment, use formal_plan or formal_plan_reader for the matching exact experiment_brief. "
                "semantic_inputs uses experiment_key, goal, characteristics, boundary_constraints, semantic_delta. "
                "The completion brief uses the indexed keys plus that same semantic_delta."}
    baseline = verified_delivery_baseline
    view["delivery_basis"] = (None if baseline is None else
        {key: deepcopy(baseline[key]) for key in ("operation_name", "invocation_hash", "prompt_hash", "base_generation")})
    indexes = {}
    for field, key, source in (("current_targets", "target_ref", "bundle_target"),
                                ("target_commits", "commit_ref", "bundle_target_commit")):
        previous = None if baseline is None else baseline[field]
        current = view[field]
        exact_readers = {item[key]: readers.get(field, {}).get(item[key]) for item in current
            if _exact_source(readers.get(field, {}).get(item[key]), request, source, item[key], item)}
        shown, index, exceptions = partition_batch_records(current, previous, exact_readers, key=key)
        retained.extend({"field": field, **item} for item in exceptions)
        current_refs = {item[key] for item in current}
        shown_hashes = {canonical_hash(item) for item in shown}
        for item, entry in zip(current, index):
            ref = item[key]
            if canonical_hash(item) in shown_hashes:
                entry["body_reason"] = "no_verified_delivery_basis" if baseline is None else "new_or_changed_or_reader_unavailable"
                if baseline is None:
                    retained.append({"field": field, key: ref, "reason": "no_verified_delivery_basis"})
            if field == "current_targets":
                candidate = item["spec"]["candidate"]
                entry.update({name: deepcopy(candidate[name]) for name in (
                    "local_label", "experiment_keys", "measurement_unit_keys", "held_fixed_bindings") if name in candidate})
                entry["dependency_refs"] = deepcopy(item.get("dependency_refs", []))
                entry["accepted_commit_refs"] = [commit["commit_ref"] for commit in request.target_commits
                                                  if commit["target_ref"] == ref]
            else:
                entry["target_ref"] = item["target_ref"]
                if "result_disposition" in item:
                    entry["result_disposition"] = item["result_disposition"]
        view[field] = shown
        indexes[field] = index
        if baseline is not None:
            indexes[field + "_removed_refs"] = sorted({item[key] for item in previous} - current_refs)
    coverage = request.plan_document.get("coverage", [])
    briefs = request.plan_document.get("experiment_briefs", [])
    view["responsibility_index"] = {
        "completion_contract_hash": canonical_hash(completion),
        "coverage": [{"obligation_key": item.get("obligation_key"), "disposition": item.get("disposition"),
                      "experiment_keys": [brief["experiment_key"] for brief in briefs
                          if item.get("obligation_key") in brief.get("gap_obligation_keys", [])]}
                     for item in coverage if isinstance(item, dict)],
        "gap_keys": deepcopy(request.plan_document.get("gap_set", [])),
        "experiments": [{key: deepcopy(item["brief"][key]) for key in (
            "experiment_key", "held_fixed_slots", "required_measurement_unit_keys")}
            for item in completion["experiments"]],
        **indexes}
    view["retained_body_reasons"] = retained
    return view
