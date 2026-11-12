"""The root's explicit batch-phase synthesis, separate from candidate work."""
from __future__ import annotations


REPLAN_BASIS_FIELDS = frozenset({
    "work_summary", "source_target_commit_refs", "limitations", "unfinished_work", "next_focus",
})


def validate_replan_basis(value: object, accepted_commit_refs: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != REPLAN_BASIS_FIELDS:
        raise ValueError("bundle_batch_replan_basis_invalid")
    for key in ("work_summary", "next_focus"):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > 6000:
            raise ValueError("bundle_batch_replan_basis_invalid")
    for key in ("limitations", "unfinished_work"):
        values = value[key]
        if (not isinstance(values, list) or not 1 <= len(values) <= 32
                or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in values)):
            raise ValueError("bundle_batch_replan_basis_invalid")
    refs = value["source_target_commit_refs"]
    if (not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or not ref for ref in refs)
            or len(refs) != len(set(refs)) or set(refs) != set(accepted_commit_refs)):
        raise ValueError("bundle_batch_replan_source_commits_invalid")
    return value


def replan_basis_schema() -> dict[str, object]:
    text = {"type": "string", "minLength": 1, "maxLength": 6000}
    entries = {"type": "array", "minItems": 1, "maxItems": 32,
               "items": {"type": "string", "minLength": 1, "maxLength": 4000}}
    return {"type": "object", "additionalProperties": False,
            "properties": {"work_summary": text, "next_focus": text,
                "limitations": entries, "unfinished_work": entries,
                "source_target_commit_refs": {"type": "array", "minItems": 1,
                    "items": {"type": "string", "minLength": 1}}},
            "required": sorted(REPLAN_BASIS_FIELDS)}
