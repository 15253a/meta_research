"""Restore immutable Plan prose after the root chooses its scientific design."""
from copy import deepcopy

from meta_research.bundle_target_contract import (
    BundleTargetContractError, build_normalized_completion_contract,
    normalized_completion_contract_to_dict,
)
from meta_research.owners.common import canonical_hash


_SEMANTIC_FIELDS = ("experiment_key", "goal", "characteristics", "boundary_constraints", "semantic_delta")


def assemble_completion_contract(value, plan_document):
    if not isinstance(value, dict) or value.get("plan_document_hash") != canonical_hash(plan_document):
        raise BundleTargetContractError("completion_contract_plan_hash_mismatch")
    rows = value.get("experiments")
    if not isinstance(rows, list):
        raise BundleTargetContractError("completion_contract_invalid")
    normalizations = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) not in ({"brief"}, {"brief", "semantic_inputs"})
                or not isinstance(row["brief"], dict)):
            raise BundleTargetContractError("completion_contract_invalid")
        normalizations.append({key: val for key, val in row["brief"].items() if key != "semantic_delta"})
    complete = normalized_completion_contract_to_dict(
        build_normalized_completion_contract(plan_document, tuple(normalizations)))
    expected = {row["brief"]["experiment_key"]: row for row in complete["experiments"]}
    for index, row in enumerate(rows):
        frozen = expected[row["brief"]["experiment_key"]]
        if "semantic_inputs" in row and row["semantic_inputs"] != frozen["semantic_inputs"]:
            raise BundleTargetContractError(f"completion_semantic_inputs_conflict:experiments[{index}].semantic_inputs")
        if "semantic_delta" in row["brief"] and row["brief"]["semantic_delta"] != frozen["brief"]["semantic_delta"]:
            raise BundleTargetContractError(f"completion_semantic_inputs_conflict:experiments[{index}].brief.semantic_delta")
    result = deepcopy(value)
    result["experiments"] = [deepcopy(expected[row["brief"]["experiment_key"]]) for row in rows]
    return result


def assemble_strategy_update(value, plan_document):
    result = deepcopy(value)
    if not isinstance(result, dict) or not isinstance(result.get("candidates"), list):
        raise BundleTargetContractError("formal_strategy_update_invalid")
    semantics = {brief["experiment_key"]: {key: deepcopy(brief[key]) for key in _SEMANTIC_FIELDS}
                 for brief in plan_document["experiment_briefs"]}
    for index, candidate in enumerate(result["candidates"]):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("candidate"), dict):
            raise BundleTargetContractError("formal_target_candidate_invalid")
        keys = candidate["candidate"].get("experiment_keys")
        if (not isinstance(keys, list) or not keys or any(not isinstance(key, str) or key not in semantics for key in keys)
                or len(keys) != len(set(keys))):
            raise BundleTargetContractError(f"formal_target_candidate_experiment_invalid:candidates[{index}].candidate.experiment_keys")
        frozen = [deepcopy(semantics[key]) for key in keys]
        if "semantic_inputs" in candidate:
            supplied = candidate["semantic_inputs"]
            if (not isinstance(supplied, list) or len(supplied) != len(frozen)
                    or any(not isinstance(item, dict) or item not in frozen for item in supplied)
                    or any(item not in supplied for item in frozen)):
                raise BundleTargetContractError(f"formal_target_candidate_semantic_conflict:candidates[{index}].semantic_inputs")
        else:
            candidate["semantic_inputs"] = frozen
    return result


def assemble_bundle_primary(output, plan_document):
    result = deepcopy(output)
    if not isinstance(result, dict):
        return result
    if set(result) == {"target_plan"} and isinstance(result["target_plan"], dict):
        plan = result["target_plan"]
        plan["completion_contract"] = assemble_completion_contract(plan.get("completion_contract"), plan_document)
        plan["initial_strategy_update"] = assemble_strategy_update(plan.get("initial_strategy_update"), plan_document)
    elif set(result) == {"exhaustion_assessment"} and isinstance(result["exhaustion_assessment"], dict):
        assessment = result["exhaustion_assessment"]
        assessment["completion_contract"] = assemble_completion_contract(assessment.get("completion_contract"), plan_document)
    return result
